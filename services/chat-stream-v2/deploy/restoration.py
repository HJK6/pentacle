"""No-input daemon restoration proof; inference is intentionally untested."""
from __future__ import annotations

from pathlib import Path
import json
import uuid

from deploy.coordinated import Invalid, digest, read_json
from tools.live_window import OwnedSessionRegistry
from tools.spawn_fleet_smoke import _operator_connection, DEFAULT_URL, DEFAULT_TOKEN_PATH


def qualification_stamp(stamp):
    """Keep the production witness without recursively nesting deploy history."""
    result = {k: stamp[k] for k in ('sha','daemon_runtime_readback','daemon_boot_readback',
              'coordinated_receipt','receipt_sha256') if k in stamp}
    smoke = stamp.get('fleet_smoke', {})
    result['fleet_smoke'] = {'outcome':smoke.get('outcome'), 'cells':[
        {k:r[k] for k in ('host','provider','prompt_mode','outcome','account_binding') if k in r}
        for r in smoke.get('cells', [])]}
    return result


def qualified_prior(stamp, sha, bindings):
    """Bind rollback to the prior's existing production forward qualification."""
    smoke = stamp.get('fleet_smoke', {})
    if (stamp.get('sha') != sha or smoke.get('outcome') != 'passed'
            or stamp.get('daemon_runtime_readback', {}).get('sha') != sha
            or stamp.get('daemon_boot_readback', {}).get('outcome') != 'observed'):
        raise Invalid('prior lacks accepted production forward qualification')
    rows = smoke.get('cells', [])
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
    if stamp.get('coordinated_receipt'):
        receipt = read_json(stamp['coordinated_receipt'])
        sealed = dict(receipt); seal = sealed.pop('receipt_sha256', None)
        if (seal != digest(sealed) or seal != stamp.get('receipt_sha256')
                or receipt.get('intent', {}).get('target_sha') != sha):
            raise Invalid('prior production receipt reference invalid')
        reference.update(receipt=stamp['coordinated_receipt'], receipt_sha256=seal)
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
        pending = []
        if checkpoint.exists():
            registry = OwnedSessionRegistry.recover(checkpoint, allow_legacy_close=True)
            pending = [row['stream_id'] for row in json.loads(checkpoint.read_bytes())['owned']]
        else:
            registry = OwnedSessionRegistry(checkpoint, allow_legacy_close=True)
        with _operator_connection(url, Path(token_path), 180.0, registry) as wire:
            rpc, wait_ready, _unused_wait_event, register_owned, close_owned = wire
            # A failed close must not lose its ownership fence on re-entry.
            for stream in pending:
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
            key = uuid.uuid4().hex
            payload = {'type':'spawn', 'host':binding['host'], 'provider':binding['provider'],
                'account':binding['alias'], 'session_name':f'v2-restore-{binding["provider"]}-{key[:12]}',
                'visibility':'hidden', 'schema':'SpawnRequestV2', 'spawn_profile':'desktop_manual',
                'model':model, 'effort':effort, 'catalog_version':version,
                'resolution_source':'explicit_override', 'request_id':str(uuid.uuid4()), 'idempotency_key':key}
            register_owned.prepare_owned_spawn(payload)
            result = rpc(payload, 'spawn')
            stream = str(result.get('stream_id') or '')
            if not stream:
                raise Invalid('restoration spawn returned no stream')
            # Ownership registration is required even when readiness/binding fails.
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
                            readiness='ready', outcome='passed', model_input='none')
            finally:
                close_owned(stream)
            cell['cleanup'] = 'passed'
            cells.append(cell)
    return {'outcome':'passed', 'mode':'zero_input', 'cells':cells,
            'model_canary':{'outcome':'not_run_by_design'}}
