"""Scan frozen boot-to-end logs without a byte cap or abnormal-close exclusions.

Usage: full-window.py SOURCE OFFSETS END_UTC ATTEMPTS WINDOW_OUT RECEIPT_OUT [WATCHDOG_PROOF|-] [GENERATION_PROOF]
Offsets name path, inode, size, captured_utc and optionally device.
"""
import hashlib, importlib.util, json, re, sys
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from deploy.packet.log_scan import AttributionAmbiguous, freeze, generation_ranges, lines, lines_many

def dt(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('timezone missing')
    return result

def stamp(line):
    match = re.match(r'(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d))\s', line)
    return dt(match[1]) if match else None

def timed_group(group):
    if not group:
        return
    previous = dt(group[0]['captured_utc'])
    for line in lines_many(group):
        previous = stamp(line) or previous
        if line.strip():
            yield previous, line


def scan(source, offsets, end, attempts, window, out, expected_endpoint='ws://127.0.0.1:7791', watchdog_proof=None, generation_proof=None):
    sys.path.insert(0, str(source/'services/chat-stream-v2'))
    from deploy import deploy
    inputs = []
    receipt = {'schema': 'pentacle.complete-window.v1', 'outcome': 'failed',
               'bind_expected': False, 'scan_complete': False, 'excluded_records': 0}
    try:
        end = dt(end)
        groups = []
        for row in json.loads(offsets.read_text()):
            from managed_logs import retained_paths
            paths = (retained_paths(Path(row['path']), row['inode'], row['device'], row['backups'])
                     if 'backups' in row else [Path(row['path'])])
            group = []
            for index, path in enumerate(paths):
                item = freeze(path, row['size'] if index == 0 else 0,
                              row['inode'] if index == 0 else None, row.get('device') if index == 0 else None)
                item['captured_utc'] = row['captured_utc']
                group.append(item)
            groups.append(group)
            inputs.extend(group)
        if generation_proof is None:
            raise AttributionAmbiguous('missing attributed deployment generation receipt')
        proof = json.loads(Path(generation_proof).read_text())
        if not isinstance(proof, dict):
            raise AttributionAmbiguous('invalid deployment generation receipt')
        proof = proof.get('slow_consumer', proof)
        if not isinstance(proof, dict) or not isinstance(proof.get('attribution'), dict):
            raise AttributionAmbiguous('invalid deployment attribution proof')
        attribution = proof['attribution']
        identity = attribution.get('generation', {})
        if not isinstance(identity, dict):
            raise AttributionAmbiguous('invalid deployment process identity')
        if (proof.get('scan_complete') is not True or attribution.get('outcome') != 'attributed'
                or attribution.get('prior_writer_exited') is not True
                or type(attribution.get('prior_pid')) is not int
                or attribution['prior_pid'] <= 0
                or attribution['prior_pid'] == identity.get('pid') or not attribution.get('prior_start')):
            raise AttributionAmbiguous('deployment receipt lacks activated generation/writer proof')
        selected, preceding, markers = generation_ranges(groups, identity.get('pid'), identity.get('start'))
        def boundaries(rows):
            return [[(item['device'], item['inode'], item['start_offset'], item['end_offset'])
                     for item in marker['inputs']] for marker in rows]
        if (identity != markers[0]['identity']
                or boundaries(attribution.get('markers', [])) != boundaries(markers)):
            raise AttributionAmbiguous('deployment receipt and selected boot boundaries differ')
        # Boot time is consistency evidence for probe joins only. Byte markers,
        # not timestamp ordering, select the generation in each sink.
        boot = min(time for marker in markers for time, _line in timed_group(marker['inputs']))
        receipt.update(generation=identity, generation_proof_sha256=hashlib.sha256(Path(generation_proof).read_bytes()).hexdigest(),
                       generation_attribution='attributed', selected_inputs=[item for group in selected for item in group],
                       old_generation={'basis': 'pre-marker range: outgoing tail plus incoming boot logs',
                                       'inputs': [item for group in preceding for item in group],
                                       'classifier': deploy.classify_slow_consumer_lines(
                                           (line for group in preceding for line in lines_many(group)), max_examples=100)})
        records = []
        warning_count = 0
        warning_dispositions = []
        unexpected_warnings = 0
        timeouts = 0
        abnormal_closes = []
        abnormal_close_count = 0
        spec = importlib.util.spec_from_file_location('activation_evidence', Path(__file__).with_name('activation-evidence.py'))
        ae = importlib.util.module_from_spec(spec); spec.loader.exec_module(ae)
        # Evidence failure rejects the expectation, retaining the warning.
        try:
            proof_ok = ae.watchdog_expectation(watchdog_proof)
        except (OSError, KeyError, ValueError, AssertionError):
            proof_ok = False
        exact = "WARNING _shared.specs_service specs watcher attach failed; falling back to polling: No module named 'watchdog'"
        window.parent.mkdir(parents=True, exist_ok=True)
        with window.open('w', encoding='utf-8') as output:
            for group in selected:
                for time, line in timed_group(group):
                    if time > end:
                        continue
                    output.write(line)
                    if re.search(r'\b(?:WARNING|ERROR|CRITICAL)\b', line):
                        warning_count += 1
                        expected = proof_ok and line.rstrip().split(' ', 1)[-1] == exact
                        unexpected_warnings += not expected
                        if len(warning_dispositions) < 100:
                            warning_dispositions.append({'line_sha256': hashlib.sha256(line.rstrip().encode()).hexdigest(), 'expected': expected})
                    if 'conn_diag ' in line:
                        try:
                            record = json.loads(line.split('conn_diag ', 1)[1])
                        except (ValueError, RecursionError):
                            continue  # classifier retains malformed diagnostics
                        if isinstance(record, dict) and record.get('schema') == 1:
                            if 'handshake' in json.dumps(record).lower() and 'timeout' in json.dumps(record).lower():
                                timeouts += 1
                            if record.get('event') == 'close':
                                codes = [record[key] for key in ('close_code', 'close_sent_code', 'close_received_code') if key in record]
                                if not codes or any(code not in (1000, 1001) for code in codes):
                                    abnormal_close_count += 1
                                    if len(abnormal_closes) < 100:
                                        abnormal_closes.append({'conn_id': record.get('conn_id'), 'codes': codes})
                            if record.get('event') in ('connect', 'close'):
                                if len(records) >= 100000:
                                    raise ValueError('connection evidence resource limit')
                                records.append((time, record))
        window.chmod(0o600)
        # Classify the complete selected artifact, streamed with capped examples.
        selected = freeze(window)
        raw = deploy.classify_slow_consumer_lines(lines(selected), max_examples=100)
        raw['boot_marker_seen'] = True
        runs = json.loads(attempts.read_text())['attempts']
        attribution = []
        ambiguous = []
        normal_unbound = []
        used = set()
        for index, attempt in enumerate(runs):
            if not attempt.get('connected'):
                continue
            # Nexus contract: proven clean normal probe closures need no log join.
            sent, received = attempt.get('close_sent_code'), attempt.get('close_received_code')
            conn = attempt.get('welcome_conn_id')
            normal = sent in (1000, 1001) and received in (1000, 1001)
            if not conn and normal:
                normal_unbound.append(index)
                continue
            begin, finish = dt(attempt['start']), dt(attempt['end'])
            found = [(time, record) for time, record in records if record.get('conn_id') == conn]
            connects = [(t, r) for t, r in found if r.get('event') == 'connect']
            closes = [(t, r) for t, r in found if r.get('event') == 'close']
            # Time is consistency evidence only; never a connection selector.
            valid = (isinstance(conn, str) and bool(re.fullmatch('[0-9a-f]{32}', conn))
                     and conn not in used and attempt['endpoint'] == expected_endpoint
                     and len(connects) == len(closes) == 1)
            if valid:
                ct, connect = connects[0]; et, close = closes[0]
                valid = (boot <= ct <= et <= end and connect.get('transport') == 'loopback'
                         and begin.replace(microsecond=begin.microsecond//1000*1000) <= ct
                         and et <= finish and sent == close.get('close_received_code')
                         and received == close.get('close_sent_code'))
            if not valid:
                ambiguous.append(index)
                continue
            used.add(conn)
            attribution.append({'conn_id': conn, 'attempt': index, 'start': attempt['start'],
                                'end': attempt['end'], 'basis': 'welcome conn_id + exact diagnostic join; no exclusion', 'normal_close': normal})
        failure_count = raw['failure_count']
        accepted = (failure_count == 0 and not ambiguous and raw['unparsed_conn_diag'] == 0
                    and not timeouts and not unexpected_warnings and not abnormal_close_count)
        receipt.update(boot_utc=boot.isoformat(), end_utc=end.isoformat(), inputs=inputs,
                       bytes_read=sum(i['bytes_read'] for i in inputs), window_bytes=window.stat().st_size,
                       window_sha256=ae.digest(window), classifier=raw, attribution=attribution,
                       normal_unbound_attempts=normal_unbound, unattributed_attempts=ambiguous,
                       unattributed_failures=[f for f in raw['failures'] if f.get('client') not in used],
                       attributed_probe_failures=[f for f in raw['failures'] if f.get('client') in used],
                       handshake_timeouts=timeouts, abnormal_close_count=abnormal_close_count, abnormal_closes=abnormal_closes, warning_count=warning_count,
                       unexpected_warning_count=unexpected_warnings, watcher_warning_dispositions=warning_dispositions,
                       watchdog_proof_sha256=ae.digest(watchdog_proof) if watchdog_proof else None,
                       scan_complete=True, bind_expected=accepted, outcome='passed' if accepted else 'failed')
    except (OSError, ValueError, KeyError, TypeError, AssertionError) as error:
        receipt['generation_attribution'] = 'attribution_ambiguous'
        receipt['incomplete_reason'] = str(error)
        receipt['inputs'] = inputs
    out.write_text(json.dumps(receipt, indent=2)+'\n')
    return receipt

if __name__ == '__main__':
    source, offsets, end, attempts, window, out = sys.argv[1:7]
    result = scan(Path(source).resolve(), Path(offsets), end, Path(attempts), Path(window), Path(out), watchdog_proof=sys.argv[7] if len(sys.argv)>7 and sys.argv[7]!='-' else None,
                  generation_proof=sys.argv[8] if len(sys.argv)>8 else None)
    print(json.dumps({k: result[k] for k in ('outcome', 'scan_complete', 'bind_expected')}))
    raise SystemExit(0 if result['bind_expected'] else 8)
