"""No-input daemon restoration proof; inference is intentionally untested."""
from __future__ import annotations

from pathlib import Path
import ast
import copy
import re
import json
import time
import uuid

from deploy.coordinated import Invalid, atomic_json, digest, read_json
from tools.live_window import OwnedSessionRegistry
from tools.spawn_fleet_smoke import _operator_connection, DEFAULT_URL, DEFAULT_TOKEN_PATH

# A restarted daemon refuses satellite-host spawns until that satellite's
# first admitted push to the new process (provider_cli_pin.consumer_current).
SATELLITE_WAIT_SECONDS, SATELLITE_BACKOFF_SECONDS = 60.0, 2.0


def _satellite_not_current(exc):
    """True only for the spawn refusal that waits on a fresh satellite push."""
    try:
        frame = ast.literal_eval(str(exc))
    except (ValueError, SyntaxError):
        return False
    return (isinstance(frame, dict) and frame.get('error_code') == 'provider_cli_verification_unavailable'
            and frame.get('reason') == 'satellite_not_current')


BINDING_FIELDS = ('host', 'provider', 'alias', 'generation', 'executable_sha256', 'version')
QUALIFICATION_FIELDS = ('sha', 'daemon_runtime_readback', 'daemon_boot_readback', 'fleet_smoke')
PROVENANCE_FIELDS = {'version', 'kind', 'sha', 'prior_sha', 'receipt', 'receipt_sha256', 'qualification_sha256'}


def qualification_stamp(stamp):
    """Retain the exact forward payload and bounded provenance, not recursive history."""
    result = {k: copy.deepcopy(stamp[k]) for k in (*QUALIFICATION_FIELDS,
              'coordinated_receipt', 'receipt_sha256', 'ordinary_provenance', 'prior_sha') if k in stamp}
    prior = stamp.get('prior_qualification')
    if isinstance(prior, dict) and isinstance(prior.get('stamp'), dict):
        result['prior_qualification'] = {'sha': prior.get('sha'), 'stamp': {
            k: prior['stamp'][k] for k in ('sha', 'coordinated_receipt') if k in prior['stamp']}}
    return result


def inherited_receipt_seal(path):
    """Validate existing receipt bytes; never manufacture an inherited seal."""
    return _inherited_receipt(path)['receipt_sha256']


def _inherited_receipt(path):
    receipt = read_json(path)
    sealed = dict(receipt)
    seal = sealed.pop('receipt_sha256', None)
    if (receipt.get('schema') not in ('pentacle.coordinated-deploy.receipt.v1',
                                     'pentacle.coordinated-deploy.receipt.v2')
            or seal != digest(sealed)):
        raise Invalid('prior production receipt reference invalid')
    if (receipt.get('receipt_path', path) != path
            or not re.fullmatch('[0-9a-f]{40}', str(receipt.get('intent', {}).get('target_sha', '')))):
        raise Invalid('prior production receipt reference invalid')
    return receipt


def _declared_skip(row, smoke):
    """A release-exclusion row the smoke runner itself declared; it carries no binding."""
    named = {entry.get('host') for source in ('skipped', 'not_selected')
             for entry in smoke.get(source) or [] if isinstance(entry, dict)}
    return (row['outcome'] == 'SKIPPED_DECLARED'
            and all(isinstance(row.get(k), str) and row[k] for k in ('authority', 'reason'))
            and row['host'] in named)


def _ordinary_record(stamp, seal):
    """Canonical v1 linkage under the trusted writer/uid consistency boundary."""
    try:
        sha, prior_sha = stamp['sha'], stamp['prior_sha']
        prior = stamp['prior_qualification']
        pointer = stamp['coordinated_receipt']
        if (not all(isinstance(v, str) and re.fullmatch('[0-9a-f]{40}', v) for v in (sha, prior_sha))
                or prior['sha'] != prior_sha or prior['stamp']['sha'] != prior_sha
                or prior['stamp']['coordinated_receipt'] != pointer
                or 'receipt_sha256' in stamp or not isinstance(pointer, str) or not pointer):
            raise ValueError()
        payload = {k: stamp[k] for k in QUALIFICATION_FIELDS}
        smoke = payload['fleet_smoke']
        if (payload['daemon_runtime_readback']['sha'] != sha
                or payload['daemon_boot_readback']['outcome'] != 'observed'
                or smoke['outcome'] != 'passed' or not smoke['cells']):
            raise ValueError()
        modes = {}
        for row in smoke['cells']:
            if _declared_skip(row, smoke):
                continue
            binding = row['account_binding']
            if (row['outcome'] != 'passed' or row['prompt_mode'] not in ('prompted', 'promptless')
                    or any(not isinstance(binding[k], str) or not binding[k] for k in BINDING_FIELDS)
                    or row['host'] != binding['host'] or row['provider'] != binding['provider']
                    or not re.fullmatch('[0-9a-f]{64}', binding['executable_sha256'])):
                raise ValueError()
            key = tuple(binding[k] for k in BINDING_FIELDS)
            modes.setdefault(key, []).append(row['prompt_mode'])
        if not modes or any(sorted(value) != ['prompted', 'promptless'] for value in modes.values()):
            raise ValueError()
        return {'version': 1, 'kind': 'ordinary_inherited', 'sha': sha, 'prior_sha': prior_sha,
                'receipt': pointer, 'receipt_sha256': seal, 'qualification_sha256': digest(payload)}
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise Invalid('invalid ordinary inherited qualification') from exc


def complete_ordinary_qualification(stamp, expected_seal):
    """Prepare the complete record before atomic publication; receipt drift refuses."""
    seal = inherited_receipt_seal(stamp.get('coordinated_receipt'))
    if seal != expected_seal:
        raise Invalid('inherited receipt changed during ordinary deployment')
    stamp['ordinary_provenance'] = _ordinary_record(stamp, seal)


def qualified_prior(stamp, sha, bindings):
    """Malformed records are refusals, not partially accepted references."""
    try:
        return _qualified_prior(stamp, sha, bindings)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise Invalid('malformed prior production qualification') from exc


def _qualified_prior(stamp, sha, bindings):
    """Bind rollback to the prior's existing production forward qualification."""
    smoke = stamp.get('fleet_smoke', {})
    if (stamp.get('sha') != sha or smoke.get('outcome') != 'passed'
            or stamp.get('daemon_runtime_readback', {}).get('sha') != sha
            or stamp.get('daemon_boot_readback', {}).get('outcome') != 'observed'):
        raise Invalid('prior lacks accepted production forward qualification')
    rows = smoke.get('cells', [])
    if any(row.get('outcome') != 'passed' and not _declared_skip(row, smoke) for row in rows):
        raise Invalid('prior production qualification holds an undeclared skip or failed cell')
    for binding in bindings:
        for mode in ('prompted', 'promptless'):
            matches = [row for row in rows if (row.get('host'), row.get('provider'), row.get('prompt_mode')) ==
                       (binding['host'], binding['provider'], mode) and row.get('outcome') == 'passed'
                       and all((row.get('account_binding') or {}).get(k) == binding[k]
                               for k in ('host', 'provider', 'alias', 'generation', 'executable_sha256', 'version'))]
            if len(matches) != 1:
                raise Invalid('prior production qualification does not cover selected binding')
    reference = {'sha': sha, 'stamp_sha256': digest(stamp), 'bindings': [
        {k: b[k] for k in ('host','provider','alias','generation')} for b in bindings]}
    prior = stamp.get('prior_qualification')
    witness = prior.get('stamp') if isinstance(prior, dict) else None
    claims_ancestry = isinstance(witness, dict) and bool(witness.get('coordinated_receipt'))
    if ('ordinary_provenance' in stamp or 'receipt_sha256' in stamp or claims_ancestry) and not stamp.get('coordinated_receipt'):
        raise Invalid('prior production receipt reference invalid')
    if stamp.get('coordinated_receipt'):
        try:
            receipt = _inherited_receipt(stamp['coordinated_receipt'])
            seal = receipt['receipt_sha256']
            if 'ordinary_provenance' in stamp:
                record = stamp['ordinary_provenance']
                if (not isinstance(record, dict) or set(record) != PROVENANCE_FIELDS
                        or type(record['version']) is not int
                        or record != _ordinary_record(stamp, seal)):
                    raise Invalid('invalid ordinary inherited qualification')
                reference.update(ancestry_receipt=stamp['coordinated_receipt'], ancestry_receipt_sha256=seal)
            else:
                # Direct origin is an explicit seal claim, never SHA equality alone.
                if ('prior_qualification' in stamp or receipt.get('intent', {}).get('target_sha') != sha
                        or seal != stamp.get('receipt_sha256')):
                    raise Invalid('prior production receipt reference invalid')
                reference.update(receipt=stamp['coordinated_receipt'], receipt_sha256=seal)
        except Invalid as exc:
            raise Invalid('prior production receipt reference invalid') from exc
    return reference


def verify_no_input(bindings, state_path, *, url=DEFAULT_URL, token_path=DEFAULT_TOKEN_PATH):
    """One fresh raw spawn/ready/owned-close per selected included binding.

    The existing wire/ownership library supplies authentication and fenced
    teardown. No CLI baseline or model-response smoke is invoked here.
    """
    if not bindings:
        raise Invalid('restoration has no included bindings')
    keys = [(b['host'], b['provider'], b['alias'], b['generation']) for b in bindings]
    if len(set(keys)) != len(keys) or any(not b.get('enabled') for b in bindings):
        raise Invalid('invalid restoration binding population')
    cells = []
    for index, binding in enumerate(bindings):
        checkpoint = Path(str(state_path) + f'.{index}.json')
        intent = checkpoint.with_suffix('.spawn.json')
        waits_path, waits = checkpoint.with_suffix('.satellite-waits.json'), []
        payload = read_json(intent).get('spawn') if intent.exists() else None
        expected_stream = f'{payload["host"]}:{payload["session_name"]}' if payload else None
        pending = []
        if checkpoint.exists():
            registry = OwnedSessionRegistry.recover(checkpoint, allow_legacy_close=True)
            pending = [row['stream_id'] for row in json.loads(checkpoint.read_bytes())['owned']]
        else:
            registry = OwnedSessionRegistry(checkpoint, allow_legacy_close=True)
        with _operator_connection(url, Path(token_path), 180.0, registry) as wire:
            rpc, wait_ready, _unused_wait_event, register_owned, close_owned = wire
            # Recover the same ambiguous/owned activation; do not replace it.
            for stream in pending:
                if stream != expected_stream:
                    close_owned(stream)
            catalog = rpc({'type':'spawn_catalog_get'}, 'spawn_catalog_get')
            status = rpc({'type':'account', 'action':'status', 'host':binding['host'],
                          'provider':binding['provider'], 'account':binding['alias']}, 'account')
            observed = [b for b in status.get('accounts', []) if all(b.get(k) == binding[k]
                        for k in ('host','provider','alias','generation')) and b.get('enabled')]
            if len(observed) != 1:
                raise Invalid('restored account catalog mismatch')
            try:
                model, effort = catalog['profiles']['desktop_manual'][binding['provider']]
                version = catalog['catalog_version']
            except (KeyError, TypeError, ValueError) as exc:
                raise Invalid('restored work catalog unavailable') from exc
            if payload:
                if any(payload.get(k) != v for k, v in (
                        ('host', binding['host']), ('provider', binding['provider']),
                        ('account', binding['alias']))):
                    raise Invalid('pending restoration spawn binding mismatch')
                # The unique stream is bound to the persisted request. The
                # existing registrar still requires its authoritative generation.
                result = {'stream_id': expected_stream}
            else:
                started = time.monotonic()
                while True:
                    key = uuid.uuid4().hex
                    payload = {'type':'spawn', 'host':binding['host'], 'provider':binding['provider'],
                        'account':binding['alias'], 'session_name':f'v2-restore-{binding["provider"]}-{key[:12]}',
                        'visibility':'hidden', 'schema':'SpawnRequestV2', 'spawn_profile':'desktop_manual',
                        'model':model, 'effort':effort, 'catalog_version':version,
                        'resolution_source':'explicit_override', 'request_id':str(uuid.uuid4()), 'idempotency_key':key}
                    register_owned.prepare_owned_spawn(payload)
                    # Persist before dispatch: a committed spawn can outlive a lost
                    # reply or failed registration. Re-entry must not dispatch again.
                    atomic_json(intent, {'spawn': payload})
                    try:
                        result = rpc(payload, 'spawn')
                        break
                    except RuntimeError as exc:
                        if not _satellite_not_current(exc):
                            raise
                        # Refused before any spawn: nothing is owned. A fresh
                        # daemon admits a satellite only after its next push.
                        atomic_json(intent, {'spawn': None})
                        elapsed = time.monotonic() - started
                        waits.append({'attempt': len(waits) + 1, 'reason': 'satellite_not_current',
                                      'request_id': payload['request_id'], 'elapsed_seconds': round(elapsed, 3)})
                        atomic_json(waits_path, {'host': binding['host'], 'waits': waits})
                        if elapsed + SATELLITE_BACKOFF_SECONDS > SATELLITE_WAIT_SECONDS:
                            exc.satellite_waits = waits
                            raise
                        time.sleep(SATELLITE_BACKOFF_SECONDS)
            stream = str(result.get('stream_id') or '')
            if not stream:
                raise Invalid('restoration spawn returned no stream')
            # Ownership registration is required even when readiness/binding fails.
            if stream not in pending:
                register_owned(payload, result)
            try:
                wait_ready(stream)
                inventory = rpc({'type':'list_sessions'}, 'list_sessions')
                rows = [r for r in inventory.get('active', []) if r.get('stream_id') == stream]
                if (len(rows) != 1 or rows[0].get('bootstrap_state') != 'ready'
                        or not rows[0].get('session_generation')
                        or any((rows[0].get('account_binding') or {}).get(k) != binding[k]
                               for k in ('host','provider','alias','generation'))):
                    raise Invalid('restoration readiness/binding mismatch')
                cell = {k: binding[k] for k in ('host','provider','alias','generation')}
                cell.update(stream_id=stream, session_generation=rows[0]['session_generation'],
                            readiness='ready', outcome='passed', model_input='none',
                            satellite_waits=waits)
            finally:
                close_owned(stream)
                atomic_json(intent, {'spawn': None})
            cell['cleanup'] = 'passed'
            cells.append(cell)
    return {'outcome':'passed', 'mode':'zero_input', 'cells':cells,
            'model_canary':{'outcome':'not_run_by_design'}}
