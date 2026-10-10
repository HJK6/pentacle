"""Portable, production-free communication contracts.

Functions return values and conditional commands. They never perform a provider,
persistence, credential or configuration effect. Receiving owners authenticate all
injected evidence and implement atomic command application.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class ParserConfig:
    email_default_contacts: tuple[str, ...] = ()
    default_voice_token: str = 'as_agent'
    legacy_outreach_scope: str = 'legacy_outreach'


_SCOPE = re.compile(r'[a-z][a-z0-9_]{0,63}\Z')
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
_LINE = re.compile(r'- (answer|outreach|change) ([a-z][a-z0-9_]{0,63})(?: \[([^\[\]\r\n]*)\])?: ([\s\S]*)\Z')
_CHANNELS = frozenset({'sms', 'email', 'botcomm'})


def _identifier(value):
    return type(value) is str and _ID.fullmatch(value) is not None


def _config_valid(config):
    return (type(config) is ParserConfig and config.default_voice_token == 'as_agent'
            and type(config.legacy_outreach_scope) is str and _SCOPE.fullmatch(config.legacy_outreach_scope)
            and type(config.email_default_contacts) is tuple
            and all(_identifier(x) for x in config.email_default_contacts)
            and len(set(config.email_default_contacts)) == len(config.email_default_contacts))


def _parse_line(line, contact_id, config):
    match = _LINE.fullmatch(line) if type(line) is str else None
    if match is None:
        raise ValueError('malformed_grant')
    original_mode, scope, block, handling = match.groups()
    qualifiers = {}
    if block is not None:
        for entry in block.split(';'):
            if '=' not in entry:
                raise ValueError('malformed_grant')
            key, value = (part.strip() for part in entry.split('=', 1))
            if key not in {'channels', 'voice', 'autonomy', 'authority'} or key in qualifiers or not value:
                raise ValueError('malformed_grant')
            qualifiers[key] = value
    channels = qualifiers.get('channels')
    channels = ([part.strip() for part in channels.split(',')] if channels is not None
                else ['sms'] + (['email'] if contact_id in config.email_default_contacts else []))
    if len(channels) != len(set(channels)) or not channels or any(c not in _CHANNELS for c in channels):
        raise ValueError('malformed_grant')
    voice, autonomy = qualifiers.get('voice', config.default_voice_token), qualifiers.get('autonomy', 'send')
    if voice not in {'as_agent', 'as_operator'} or autonomy not in {'send', 'draft'}:
        raise ValueError('malformed_grant')
    authority = qualifiers.get('authority')
    if authority is not None:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2} [A-Za-z0-9][A-Za-z0-9_.-]{0,127}', authority):
            raise ValueError('malformed_grant')
        date.fromisoformat(authority[:10])
    if voice == 'as_operator' and authority is None:
        raise ValueError('malformed_grant')
    mode = 'outreach' if original_mode == 'answer' and scope == config.legacy_outreach_scope else original_mode
    return dict(contact_id=contact_id, line_id=f'{contact_id}:{mode}:{scope}', mode=mode,
                original_mode=original_mode, scope=scope, line=line, handling=handling,
                channels=channels, voice=voice, autonomy=autonomy, authority=authority)


def parse_sections(contacts, config):
    """Parse whole contacts atomically. Invalid input yields no eligible lines.

    A contact has ``id`` and either ``heading``/``lines`` or ``sections`` containing
    exactly one heading/lines object. Fixture-only additional metadata is ignored.
    All duplicate contact identities are invalidated, rather than first-wins.
    """
    if type(contacts) is not list:
        return []
    ids = [c.get('id') if type(c) is dict else None for c in contacts]
    result = []
    for contact, contact_id in zip(contacts, ids):
        row = dict(contact_id=contact_id if _identifier(contact_id) else None,
                   valid=False, reason='malformed_grant', grants=[])
        try:
            if not _config_valid(config) or not _identifier(contact_id) or ids.count(contact_id) != 1:
                raise ValueError('malformed_grant')
            if 'sections' in contact:
                if 'heading' in contact or 'lines' in contact:
                    raise ValueError('malformed_grant')
                sections = contact['sections']
            else:
                sections = [contact]
            if type(sections) is not list or len(sections) != 1 or type(sections[0]) is not dict:
                raise ValueError('malformed_grant')
            section = sections[0]
            if section.get('heading') not in {'SMS grant', 'Communication grant'}:
                raise ValueError('malformed_grant')
            lines = section.get('lines')
            if type(lines) is not list or not lines:
                raise ValueError('malformed_grant')
            grants = [_parse_line(line, contact_id, config) for line in lines]
            if len({g['line_id'] for g in grants}) != len(grants):
                raise ValueError('malformed_grant')
            row.update(valid=True, reason='parsed', grants=grants)
        except (ValueError, TypeError, KeyError):
            pass
        result.append(row)
    return result

# CG2 typed boundary and logical-envelope primitives.
import base64
import hashlib
import json
from datetime import datetime, timezone
from fractions import Fraction
from typing import Protocol

from receipt_proof import epoch


class ContractError(ValueError):
    """Malformed portable input; callers must hold without an egress effect."""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def _sha(value):
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class Evidence:
    """Validated owner response, never self-authenticating caller input.

    Only the injected resolver may supply this record. It authenticates the
    opaque reference, binds purpose/subject/version/data and rejects revocation.
    ``data_json`` freezes the verified payload against subsequent mutation.
    """
    kind: str
    subject: str
    version: str
    issued_at: str
    expires_at: str
    data_json: str

    @property
    def data(self):
        return json.loads(self.data_json)


class EvidenceResolver(Protocol):
    def resolve(self, reference: str, kind: str) -> Evidence | None:
        """Return authenticated current evidence, or None. Never trust flags."""


@dataclass(frozen=True)
class TrustedContext:
    resolver: EvidenceResolver
    now: str
    proofs: tuple[tuple[str, str], ...]

    def reference(self, kind):
        matches = [ref for name, ref in self.proofs if name == kind]
        return matches[0] if len(matches) == 1 else None


@dataclass(frozen=True)
class BoundEnvelope:
    logical_json: str
    digest: str
    wire_digest: str | None = None

    @property
    def data(self):
        return json.loads(self.logical_json)



_TIMESTAMP = re.compile(r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})\Z')
_REF = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:/#-]{0,255}\Z')
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_MEDIA = re.compile(r'[a-zA-Z0-9!#$&^_.+-]+/[a-zA-Z0-9!#$&^_.+-]+(?:;[ -~]+)?\Z')
_ENVELOPE_KEYS = frozenset({'schema_version','channel','account_ref','sender_ref','recipients','subject','body',
    'text','html','introduction','mime_parts','protected_refs','thread_id','reply_id','references','attachments',
    'voice','voice_config_version','voice_authority_ref'})


def _ref(value):
    return type(value) is str and _REF.fullmatch(value) is not None


def _instant(value):
    """Reuse epoch for validity only; Fraction preserves every fractional digit."""
    match = _TIMESTAMP.fullmatch(value) if type(value) is str else None
    if match is None or epoch(value) is None:
        raise ContractError('invalid_timestamp')
    main, fraction, offset = match.groups()
    try:
        integral = datetime.fromisoformat(main + offset.replace('Z', '+00:00')).timestamp()
        return Fraction(int(integral)) + (Fraction(int(fraction),10 ** len(fraction)) if fraction else Fraction(0))
    except (ValueError, TypeError, OverflowError) as exc:
        raise ContractError('invalid_timestamp') from exc


def _resolve(ctx, kind, subject=None, reference=None):
    """Every lookup crosses the explicitly trusted receiving-owner resolver."""
    try:
        if type(ctx) is not TrustedContext or type(ctx.proofs) is not tuple:
            return None
        now = _instant(ctx.now)
        ref = ctx.reference(kind) if reference is None else reference
        if not _ref(ref):
            return None
        try:
            ev = ctx.resolver.resolve(ref, kind)
        except Exception:
            return None
        if (type(ev) is not Evidence or ev.kind != kind or not _identifier(ev.version)
                or not _ref(ev.subject) or (subject is not None and ev.subject != subject)
                or not (_instant(ev.issued_at) <= now < _instant(ev.expires_at))):
            return None
        if type(ev.data) is not dict or _json(ev.data) != ev.data_json:
            return None
        return ev
    except (ValueError, TypeError, AttributeError, KeyError, OverflowError):
        return None


def bind_envelope(envelope, wire_bytes=None):
    """Validate and freeze logical v1, normalizing only within-role ordering."""
    try:
        if type(envelope) is not dict or set(envelope) != _ENVELOPE_KEYS:
            raise ContractError('invalid_envelope')
        data = json.loads(_json(envelope))
        if type(data['schema_version']) is not int or data['schema_version'] != 1 or data['channel'] not in _CHANNELS:
            raise ContractError('invalid_envelope')
        if not all(_ref(data[k]) for k in ('account_ref','sender_ref','voice_config_version')):
            raise ContractError('invalid_envelope')
        if data['voice'] not in {'as_agent','as_operator'} or (data['voice_authority_ref'] is not None and not _ref(data['voice_authority_ref'])):
            raise ContractError('invalid_envelope')
        if not all(type(data[k]) is str for k in ('subject','body','text','html','introduction')):
            raise ContractError('invalid_envelope')
        for name in ('thread_id','reply_id'):
            if data[name] is not None and not _ref(data[name]):
                raise ContractError('invalid_envelope')
        for name in ('protected_refs','references'):
            if type(data[name]) is not list or not all(_ref(x) for x in data[name]):
                raise ContractError('invalid_envelope')
        recipients = data['recipients']
        if type(recipients) is not list or not recipients:
            raise ContractError('invalid_envelope')
        unique = {}
        for recipient in recipients:
            if (type(recipient) is not dict or set(recipient) != {'contact_id','role','destination'}
                    or (recipient['contact_id'] is not None and not _identifier(recipient['contact_id'])) or recipient['role'] not in {'to','cc','bcc'}
                    or type(recipient['destination']) is not str or not recipient['destination']
                    or any(ord(c) < 32 for c in recipient['destination'])):
                raise ContractError('invalid_envelope')
            unique[(recipient['role'],recipient['contact_id'],recipient['destination'])] = recipient
        # Roles remain distinct. Only recipient order inside each role is insignificant.
        data['recipients'] = [unique[key] for key in sorted(unique, key=lambda key: ({'to':0,'cc':1,'bcc':2}[key[0]],key[1] or '',key[2]))]
        if type(data['attachments']) is not list or type(data['mime_parts']) is not list:
            raise ContractError('invalid_envelope')
        for attachment in data['attachments']:
            if (type(attachment) is not dict or set(attachment) != {'sha256','size','media_type','name_ref','content_ref'}
                    or type(attachment['sha256']) is not str or not _SHA.fullmatch(attachment['sha256'])
                    or type(attachment['size']) is not int or attachment['size'] < 0
                    or type(attachment['media_type']) is not str or not _MEDIA.fullmatch(attachment['media_type'])
                    or not _ref(attachment['name_ref']) or not _ref(attachment['content_ref'])):
                raise ContractError('invalid_envelope')
        for part in data['mime_parts']:
            if (type(part) is not dict or set(part) not in ({'content_type','text'},{'content_type','base64'})
                    or type(part['content_type']) is not str or not _MEDIA.fullmatch(part['content_type'])):
                raise ContractError('invalid_envelope')
            if 'text' in part and type(part['text']) is not str:
                raise ContractError('invalid_envelope')
            if 'base64' in part:
                raw = base64.b64decode(part['base64'],validate=True)
                if base64.b64encode(raw).decode('ascii') != part['base64']:
                    raise ContractError('invalid_envelope')
        if wire_bytes is not None and type(wire_bytes) is not bytes:
            raise ContractError('invalid_wire_bytes')
        logical = _json(data)
        return BoundEnvelope(logical,_sha(logical.encode('utf-8')),
                             _sha(wire_bytes) if wire_bytes is not None else None)
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise ContractError('invalid_envelope') from exc


def envelope_digest(envelope):
    if type(envelope) is not BoundEnvelope:
        raise ContractError('unbound_envelope')
    rebound = bind_envelope(envelope.data)
    if (rebound.logical_json != envelope.logical_json or rebound.digest != envelope.digest
            or (envelope.wire_digest is not None and (type(envelope.wire_digest) is not str or not _SHA.fullmatch(envelope.wire_digest)))):
        raise ContractError('invalid_envelope_binding')
    return rebound.digest


def _deny(reason, versions=None):
    return dict(admitted=False,reason=reason,versions=versions or {},snapshots=[])


def _source_grants(source):
    try:
        config = source.data['config']
        if type(config) is not dict or set(config) != {'email_default_contacts','default_voice_token','legacy_outreach_scope'}:
            return None
        if type(config['email_default_contacts']) is not list:
            return None
        cfg = ParserConfig(tuple(config['email_default_contacts']),config['default_voice_token'],config['legacy_outreach_scope'])
        if not _config_valid(cfg):
            return None
        commit = source.data['commit']
        if type(commit) is not str or not re.fullmatch('[0-9a-f]{40}',commit) or source.data.get('status') != 'current':
            return None
        return parse_sections(source.data['contacts'],cfg)
    except (KeyError, TypeError, ValueError):
        return None


def admit(envelope, trusted_context, grants, capabilities=None, approval=None):
    """Shared all-recipient admission. A false result is a draft/hold, never send."""
    ctx = trusted_context
    try:
        envelope_digest(envelope)
        env = envelope.data
        request = _resolve(ctx,'request')
        if request is None or request.data.get('request_id') != request.subject:
            return _deny('request_untrusted')
        evidence = {'request':request}
        for kind in ('identity','source','capability','workflow','actor','config'):
            item = _resolve(ctx,kind,request.subject)
            if item is None:
                return _deny(kind + '_untrusted')
            evidence[kind] = item
        versions = {kind:item.version for kind,item in evidence.items()}
        if capabilities is not None and capabilities != ctx.reference('capability'):
            return _deny('capability_untrusted',versions)
        identity, source, cap, workflow, actor, config = (evidence[k].data for k in
            ('identity','source','capability','workflow','actor','config'))
        if config.get('default_voice_token') != 'as_agent' or env['voice_config_version'] != evidence['config'].version:
            return _deny('config_untrusted',versions)
        if env['voice']=='as_agent' and (not env['body'].endswith('\n\nSynthetic assistant')
                or not env['text'].endswith('\n\nSynthetic assistant') or not env['html'].endswith('<p>Synthetic assistant</p>')
                or env['introduction']!=('Synthetic assistant: ' if env['channel']=='sms' else '')
                or env['voice_authority_ref'] is not None):
            return _deny('voice_not_composed',versions)
        if identity.get('account_ref') != env['account_ref'] or identity.get('sender_ref') != env['sender_ref'] or actor.get('account_ref') != env['account_ref'] or not _ref(actor.get('actor_ref')):
            return _deny('identity_mismatch',versions)
        current_grants = _source_grants(evidence['source'])
        if current_grants is None or grants != current_grants:
            return _deny('source_changed',versions)
        if type(cap.get('channels')) is not list or any(c not in _CHANNELS for c in cap['channels']) or env['channel'] not in cap['channels']:
            return _deny('channel_disabled',versions)
        if cap.get('content_allowed') is not True:
            return _deny('content_restricted',versions)
        if workflow.get('classification') != 'ordinary':
            return _deny('workflow_untrusted',versions)
        mode, scope = workflow.get('mode'), workflow.get('scope')
        if mode == 'change':
            return _deny('action_metadata',versions)
        if mode not in {'answer','outreach'} or type(scope) is not str or not _SCOPE.fullmatch(scope):
            return _deny('workflow_untrusted',versions)
        if mode == 'answer':
            if (not _ref(workflow.get('inbound_id')) or workflow['inbound_id'] != env['reply_id']
                    or type(workflow.get('inbound_contact_ids')) is not list
                    or set(workflow['inbound_contact_ids']) != {r['contact_id'] for r in env['recipients']}):
                return _deny('reply_untrusted',versions)
        elif not _ref(workflow.get('dispatch_ref')):
            return _deny('dispatch_untrusted',versions)
        snapshots, approval_reasons = [], []
        for recipient in env['recipients']:
            if recipient['contact_id'] is None:
                return _deny('recipient_unresolved',versions)
            resolved = identity.get('recipients',{}).get(recipient['destination'])
            if (type(resolved) is not dict or resolved.get('contact_ids') != [recipient['contact_id']]
                    or type(resolved.get('channels')) is not list or env['channel'] not in resolved['channels']
                    or resolved.get('status') != 'active'):
                return _deny('recipient_unresolved',versions)
            if env['channel'] == 'email' and resolved.get('recorded_email') is not True:
                return _deny('email_not_recorded',versions)
            if env['channel'] == 'botcomm' and (cap.get('bot_allowed') is not True or resolved.get('bot_type') not in {'contact','registered_bot'}):
                return _deny('bot_restricted',versions)
            rows = [row for row in grants if row.get('contact_id') == recipient['contact_id']]
            if not rows:
                approval_reasons.append('unmatched')
                continue
            if len(rows) != 1 or rows[0].get('valid') is not True:
                return _deny('malformed_grant',versions)
            matching = [g for g in rows[0]['grants'] if g['mode'] == mode and g['scope'] == scope]
            if len(matching) != 1:
                approval_reasons.append('unmatched')
                continue
            grant = matching[0]
            if env['channel'] not in grant['channels']:
                return _deny('channel_not_granted',versions)
            if grant['voice'] != env['voice']:
                return _deny('voice_mismatch',versions)
            if grant['voice'] == 'as_operator':
                if env['introduction'] or _assistant_signed(env):
                    return _deny('voice_not_composed',versions)
                reason, voice_versions = _voice_authority(env,ctx,grant,recipient['contact_id'])
                if reason:
                    return _deny(reason,versions)
                versions.update(voice_versions)
            if grant['autonomy'] == 'draft':
                approval_reasons.append('draft_autonomy')
            snapshots.append(dict(grant,source_commit=source['commit'],source_version=evidence['source'].version))
        if approval_reasons:
            if _approval_ready(envelope,ctx,versions,approval):
                return dict(admitted=True,reason='one_shot',versions=versions,snapshots=snapshots)
            return _deny(approval_reasons[0],versions)
        return dict(admitted=True,reason='standing_grant',versions=versions,snapshots=snapshots)
    except (ContractError, ValueError, TypeError, KeyError, AttributeError):
        return _deny('malformed_input')

@dataclass(frozen=True)
class Command:
    kind: str
    request_id: str
    body_json: str
    conditions_json: str

    @property
    def command_id(self):
        return _sha(_json([self.kind,self.request_id,self.body_json,self.conditions_json]).encode())

    @property
    def body(self):
        return json.loads(self.body_json)

    @property
    def conditions(self):
        return json.loads(self.conditions_json)



_ADMISSION_KINDS = ('request','identity','source','capability','workflow','actor','config')


def _command(kind, request_id, body, conditions):
    return Command(kind,request_id,_json(body),_json(conditions))


def _current_versions(ctx, subject):
    evidence = {kind:_resolve(ctx,kind,subject) for kind in _ADMISSION_KINDS}
    return ({kind:item.version for kind,item in evidence.items()} if all(evidence.values()) else None)


def _draft_write(draft):
    return _command('put_draft_and_handoff',draft['request_id'],draft,
                    dict(draft_id=draft['draft_id'],expected_prior_version=draft['version']-1,
                         create_or_identical=True,envelope_digest=draft['envelope_digest'],previous_digest=draft.get('previous_digest')))


def _read_draft(ctx):
    request = _resolve(ctx,'request')
    saved = _resolve(ctx,'draft',request.subject) if request else None
    if saved is None:
        return None
    try:
        row = saved.data
        ack = row.pop('committed_command_id')
        bound = bind_envelope(json.loads(row['envelope_json'])) if row['envelope_json'] is not None else None
        if (row['request_id'] != request.subject or (bound is not None and (row['envelope_digest']!=bound.digest or row['envelope_json']!=bound.logical_json))
                or (bound is None and row.get('content_disposition') not in {'expired','protected_unavailable'}) or row['state'] != 'draft'
                or type(row['version']) is not int or row['version'] < 1
                or ack != _draft_write(row).command_id):
            return None
        return row
    except (KeyError, TypeError, ValueError):
        return None


def _verified_draft(ctx, envelope, proposed=None):
    row = _read_draft(ctx)
    if row is None or row.get('content_disposition')!='available':
        return None
    try:
        if row.get('source_expires_at') is not None and _instant(row['source_expires_at'])<=_instant(ctx.now):
            return None
    except ContractError:
        return None
    return row if (row['envelope_digest']==envelope.digest and row['envelope_json']==envelope.logical_json
                   and (proposed is None or proposed==row)) else None


def draft_command(envelope, trusted_context, decision):
    """Request one durable draft/marker or restricted atomic staging; no send."""
    ctx = trusted_context
    held = dict(status='held',reason='draft_untrusted',commands=[],draft=None)
    try:
        envelope_digest(envelope)
        request = _resolve(ctx,'request')
        if request is None or request.data.get('request_id') != request.subject:
            return held
        data = request.data
        version = data.get('draft_version',1)
        if type(version) is not int or version < 1 or not _identifier(data.get('send_id')):
            return held
        _instant(data['original_utc'])
        versions = _current_versions(ctx,request.subject)
        # Missing authority still creates a protected draft; never fakes a version.
        token = _sha(request.subject.encode())[:24]
        reason = decision.get('reason') if type(decision) is dict else 'malformed_input'
        if not _identifier(reason):
            reason = 'malformed_input'
        prior = _read_draft(ctx)
        draft = dict(draft_id='draft-'+token,request_id=request.subject,version=version,
                     envelope_digest=envelope.digest,envelope_json=envelope.logical_json,state='draft',
                     reason=reason,handoff_id='handoff-'+token,versions=versions or {},created_at=data['original_utc'],
                     previous_digest=prior['envelope_digest'] if prior and version==prior['version']+1 else prior.get('previous_digest') if prior else None)
        protection = _protected(ctx,request.subject,envelope)
        source_proof = _resolve(ctx,'protected',request.subject)
        source_expiry = source_proof.data.get('source_expires_at') if source_proof else None
        if source_expiry is not None:
            try:
                _instant(source_expiry)
            except ContractError:
                source_expiry=None
        draft['source_expires_at']=source_expiry
        draft['content_disposition']='available' if protection else ('expired' if source_expiry is not None and _instant(source_expiry)<=_instant(ctx.now) else 'protected_unavailable')
        draft['content_ref']=source_proof.data.get('content_ref') if source_proof and _ref(source_proof.data.get('content_ref')) else None
        if protection is None:
            draft['envelope_json']=None
        ids = [r['contact_id'] for r in envelope.data['recipients']]
        draft['pk'] = ('CONTACT#UNRESOLVED#'+draft['draft_id'] if any(x is None for x in ids) else 'CONTACT#'+min(ids))
        draft['sk'] = 'DRAFT#'+draft['created_at']+'#'+draft['draft_id']
        persistence = _resolve(ctx,'persistence',request.subject)
        if persistence is None:
            return dict(held,reason='persistence_untrusted',draft=draft)
        storage = persistence.data
        if storage.get('restricted') is not True or storage.get('atomic') is not True or type(storage.get('policy_version')) is not int or storage['policy_version'] != 1:
            return dict(held,reason='persistence_untrusted',draft=draft)
        if storage.get('status') == 'unavailable':
            stage = _command('stage_draft',request.subject,dict(draft=draft,restricted=True,atomic=True),
                             dict(create_or_identical=True,draft_id=draft['draft_id'],version=version))
            staging = _resolve(ctx,'staging',request.subject)
            if staging:
                item = staging.data
                if (item.get('draft_id') != draft['draft_id'] or item.get('version') != version
                        or item.get('envelope_digest') != envelope.digest or item.get('restricted') is not True
                        or item.get('atomic') is not True or item.get('committed_command_id') != stage.command_id):
                    return dict(held,reason='staging_untrusted',draft=draft)
                if item.get('status') in {'failed','stored'}:
                    return dict(held,reason='staging_failed' if item['status']=='failed' else 'staged_unadmitted',draft=draft)
                return dict(held,reason='staging_untrusted',draft=draft)
            return dict(status='held',reason='staging_required',commands=[stage],draft=draft)
        if storage.get('status') != 'available':
            return dict(held,reason='persistence_untrusted',draft=draft)
        existing_ref = ctx.reference('draft')
        existing = prior
        if existing_ref and existing is None:
            return dict(held,reason='draft_conflict',draft=draft)
        if existing:
            if version==existing['version']+1:
                return dict(status='draft_pending',reason='new_version_requires_approval',commands=[_draft_write(draft)],draft=draft)
            if existing != draft:
                return dict(held,reason='draft_conflict',draft=draft)
            handoff = _command('request_handoff',request.subject,
                               dict(handoff_id=draft['handoff_id'],draft_id=draft['draft_id'],version=version),
                               dict(draft_committed=_draft_write(draft).command_id))
            proof = _resolve(ctx,'handoff',request.subject)
            if proof and proof.data.get('handoff_id') == draft['handoff_id'] and proof.data.get('status') == 'acknowledged' and proof.data.get('committed_command_id') == handoff.command_id:
                return dict(status='drafted',reason='handoff_acknowledged',commands=[],draft=draft)
            return dict(status='drafted',reason='handoff_pending',commands=[handoff],draft=draft)
        return dict(status='draft_pending',reason='persistence_required',commands=[_draft_write(draft)],draft=draft)
    except (ContractError, ValueError, TypeError, KeyError, AttributeError):
        return held


def _binding_fields(envelope, draft, versions):
    env = envelope.data
    return dict(draft_id=draft['draft_id'],draft_version=draft['version'],envelope_digest=envelope.digest,
                channel=env['channel'],account_ref=env['account_ref'],recipients=env['recipients'],
                versions=versions,voice='as_agent')


def _proof_slots(ctx, kind):
    try:
        return [reference for name, reference in ctx.proofs if name==kind]
    except (TypeError, ValueError, AttributeError):
        return [None, None]


def _approval_command(draft, binding):
    return _command('bind_approval',draft['request_id'],binding,
                    dict(draft_committed=_draft_write(draft).command_id,versions=binding['versions'],unclaimed=True,
                         approval_state=binding['prior_approval_state'],approval_state_version=binding['prior_approval_state_version']))


def _approval_state(ctx, draft):
    state = _resolve(ctx,'approval_state',draft['request_id'])
    if (state is None or state.data.get('draft_id')!=draft['draft_id']
            or type(state.data.get('draft_version')) is not int or state.data['draft_version']!=draft['version']
            or state.data.get('status') not in {'absent','available','consumed','canceled','revoked'}):
        return None
    return state


def _committed_approval(ctx, envelope, draft, versions, reference=None):
    state = _approval_state(ctx,draft)
    if state is None or state.data['status']!='available':
        return None
    slots = _proof_slots(ctx,'approval')
    if len(slots)!=1 or not _ref(slots[0]) or (reference is not None and reference!=slots[0]):
        return None
    proof = _resolve(ctx,'approval',draft['request_id'])
    if proof is None:
        return None
    try:
        data, binding = proof.data, proof.data['binding']
        fields = _binding_fields(envelope,draft,versions)
        if (data.get('status')!='available' or any(binding.get(k)!=v for k,v in fields.items())
                or type(binding.get('draft_version')) is not int
                or binding.get('prior_approval_state')!='absent' or not _identifier(binding.get('prior_approval_state_version'))
                or not _identifier(binding.get('approval_id')) or state.data.get('approval_id')!=binding['approval_id']
                or not _identifier(binding.get('event_id')) or not _ref(binding.get('actor_ref'))
                or not (_instant(draft['created_at']) <= _instant(binding['occurred_at']) <= _instant(ctx.now))
                or data.get('committed_command_id')!=_approval_command(draft,binding).command_id):
            return None
        return proof
    except (ContractError, ValueError, TypeError, KeyError):
        return None


def approval_binding(envelope, trusted_context, draft, event_ref=None):
    """Preserve historical acknowledgment; separately verify current authority."""
    ctx = trusted_context
    refused = dict(bound=False,reason='approval_untrusted',commands=[],binding=None)
    try:
        envelope_digest(envelope)
        saved = _verified_draft(ctx,envelope,draft)
        if saved is None or envelope.data['voice']!='as_agent':
            return refused
        versions = _current_versions(ctx,draft['request_id'])
        if versions is None or versions!=draft['versions']:
            return dict(refused,reason='authority_changed')
        event = _resolve(ctx,'operator_event',draft['request_id'],event_ref)
        state = _approval_state(ctx,draft)
        if event is None or state is None:
            return refused
        data = event.data
        if (data.get('origin')!='operator' or data.get('original') is not True
                or not _identifier(data.get('event_id')) or not _ref(data.get('actor_ref'))
                or type(data.get('draft_version')) is not int
                or not (_instant(draft['created_at']) <= _instant(data['occurred_at']) <= _instant(ctx.now))):
            return refused
        fields = _binding_fields(envelope,draft,versions)
        if any(data.get(k)!=v for k,v in fields.items() if k!='voice'):
            return dict(refused,reason='approval_mismatch')
        slots = _proof_slots(ctx,'approval')
        if slots and (len(slots)!=1 or not _ref(slots[0]) or _resolve(ctx,'approval',draft['request_id']) is None):
            return dict(refused,reason='approval_state_untrusted')
        status = state.data['status']
        if status in {'consumed','canceled','revoked'}:
            return dict(refused,reason='approval_'+status)
        if data.get('action') in {'cancel','revoke'}:
            command = _command('cancel_approval',draft['request_id'],dict(fields,event_id=data['event_id'],action=data['action']),
                dict(draft_id=draft['draft_id'],version=draft['version'],unclaimed=True,approval_state_version=state.version))
            return dict(refused,reason='approval_'+data['action'],commands=[command])
        if data.get('action')!='approve':
            return refused
        if status=='available':
            prior = _committed_approval(ctx,envelope,draft,versions)
            if prior is None:
                return dict(refused,reason='approval_conflict')
            binding = prior.data['binding']
            if any(binding.get(k)!=data[k] for k in ('event_id','actor_ref','occurred_at')):
                return dict(refused,reason='approval_conflict')
            return dict(bound=True,reason='approval_committed',commands=[_approval_command(draft,binding)],binding=binding)
        # Verified absent differs from missing or invalid proof. No old record may coexist.
        if slots:
            return dict(refused,reason='approval_conflict')
        binding = dict(fields,approval_id='approval-'+_sha(_json([draft['request_id'],data['event_id']]).encode())[:24],
                       event_id=data['event_id'],actor_ref=data['actor_ref'],occurred_at=data['occurred_at'],
                       prior_approval_state='absent',prior_approval_state_version=state.version)
        return dict(bound=True,reason='approval_pending_commit',commands=[_approval_command(draft,binding)],binding=binding)
    except (ContractError, ValueError, TypeError, KeyError, AttributeError):
        return refused


def _approval_ready(envelope, ctx, versions, reference):
    draft = _verified_draft(ctx,envelope)
    return bool(reference is not None and draft is not None and envelope.data['voice']=='as_agent'
                and _committed_approval(ctx,envelope,draft,versions,reference))


_PREVIEW_FIELDS = ['channel','direction','state','attachment_count']
_STATES = frozenset({'draft','held','ready','attempting','accepted','rejected','acceptance_unknown','canceled'})
_DELIVERY = frozenset({'not_attempted','not_applicable','pending','delivered','failed','unknown'})
_MUTABLE_RECORD = frozenset({'state','attempt_id','acceptance_at','delivery_state','reconciled_at','terminal_at',
    'provider_message_id','provider_thread_id','receipt_conflict','receipts','revision'})


def _plan(status='held', reason='untrusted', commands=None, record=None):
    return dict(status=status,reason=reason,commands=commands or [],record=record)


def validate_preview_policy(trusted_context):
    request = _resolve(trusted_context,'request')
    proof = _resolve(trusted_context,'preview',request.subject) if request else None
    valid = bool(proof and set(proof.data)=={'policy_version','fields'}
                 and type(proof.data['policy_version']) is int and proof.data['policy_version']==1
                 and proof.data['fields']==_PREVIEW_FIELDS)
    return dict(valid=valid,reason='metadata_only' if valid else 'preview_untrusted',fields=_PREVIEW_FIELDS[:] if valid else [])


def _clock(ctx, subject):
    clock = _resolve(ctx,'clock',subject)
    return bool(clock and clock.data == dict(now=ctx.now,source='trusted_utc') and ctx.now.endswith('Z'))


def check_request_replay(trusted_context, digest):
    ctx = trusted_context
    request = _resolve(ctx,'request')
    if not request or request.data.get('request_id') != request.subject:
        return dict(status='held',reason='request_untrusted')
    replay = _resolve(ctx,'replay',request.subject)
    if not replay or replay.data.get('request_id') != request.subject:
        return dict(status='held',reason='replay_untrusted')
    data = replay.data
    if data.get('status') in {'expired','consumed'}:
        return dict(status='rejected',reason='expired_request_rejected')
    if data.get('status') == 'new' and data.get('issuer_ref') == ctx.reference('request') and data.get('canonical_key') is None and data.get('envelope_digest') is None:
        return dict(status='new',reason='new_request_proved',version=replay.version)
    if data.get('status') == 'active':
        key = data.get('canonical_key')
        if data.get('envelope_digest') == digest and type(key) is list and len(key)==2 and all(_ref(x) for x in key):
            return dict(status='existing',reason='existing_request',canonical_key=key,version=replay.version)
        return dict(status='held',reason='request_conflict')
    return dict(status='held',reason='replay_untrusted')


_ENFORCEMENT_KINDS = ('persistence','preview','protected','attempt_control')


def _enforcement_versions(ctx, subject, versions, approval=None):
    result = dict(versions)
    for kind in _ENFORCEMENT_KINDS:
        proof = _resolve(ctx,kind,subject)
        if proof is None:
            return None
        result[kind] = proof.version
    if approval is not None:
        proof = _resolve(ctx,'approval',subject,approval)
        if proof is None:
            return None
        result['approval'] = proof.version
        state = _resolve(ctx,'approval_state',subject)
        if state is None:
            return None
        result['approval_state'] = state.version
    return result


def _attempt_open(ctx, subject, digest):
    proof = _resolve(ctx,'attempt_control',subject)
    return bool(proof and proof.data.get('status')=='open' and proof.data.get('envelope_digest')==digest
                and _ref(proof.data.get('transaction_ref')))


def _publication(ctx, subject, versions):
    evidence = _resolve(ctx,'publication',subject)
    if (evidence and evidence.data.get('status') == 'committed'
            and _ref(evidence.data.get('transaction_ref')) and evidence.data.get('versions') == versions
            and evidence.data.get('pending') == [] and evidence.data.get('tombstones') == []):
        return evidence
    return None


def publish_version(trusted_context, kind, publication_ref=None):
    ctx = trusted_context
    request = _resolve(ctx,'request')
    if not request or kind not in _ADMISSION_KINDS:
        return _plan(reason='publication_untrusted')
    refresh = _resolve(ctx,'refresh',request.subject,publication_ref)
    if not refresh:
        return _plan(reason='publication_untrusted')
    data = refresh.data
    target = _resolve(ctx,kind,request.subject,data.get('target_ref'))
    if (data.get('kind') != kind or data.get('status') != 'validated'
            or not _identifier(data.get('previous_version')) or not _identifier(data.get('version'))
            or data['version'] == data['previous_version'] or type(data.get('tombstone')) is not bool
            or target is None or target.version != data['version']):
        return _plan(reason='publication_untrusted')
    if kind=='source' and not data['tombstone'] and _source_grants(target) is None:
        return _plan(reason='source_untrusted')
    command = _command('publish_version',request.subject,
        dict(kind=kind,version=data['version'],proof_ref=data['target_ref'],tombstone=data['tombstone']),
        dict(previous_version=data['previous_version'],validated_refresh_ref=ctx.reference('refresh') if publication_ref is None else publication_ref))
    return _plan('publication_pending','commit_before_acknowledgment',[command])


def _protected(ctx, request_id, envelope):
    proof = _resolve(ctx,'protected',request_id)
    if not proof:
        return None
    data = proof.data
    if (data.get('access') != 'allowed' or data.get('envelope_digest') != envelope.digest
            or data.get('immutable_snapshot') is not True or data.get('retention') != 'owner_enforced'
            or not all(_ref(data.get(k)) for k in ('content_ref','subject_ref','snapshot_ref'))
            or data.get('attachment_refs') != [a['content_ref'] for a in envelope.data['attachments']]
            or data['content_ref'] not in envelope.data['protected_refs']):
        return None
    addresses = data.get('recipient_refs')
    recipients = envelope.data['recipients']
    if (type(addresses) is not list or len(addresses)!=len(recipients)
            or any(type(a) is not dict or set(a)!={'contact_id','role','destination_ref'}
                   or a['contact_id']!=r['contact_id'] or a['role']!=r['role'] or not _ref(a['destination_ref'])
                   for a,r in zip(addresses,recipients))):
        return None
    if 'source_expires_at' not in data:
        return None
    expires = data.get('source_expires_at')
    if expires is not None:
        try:
            if _instant(expires) <= _instant(ctx.now):
                return None
        except ContractError:
            return None
    return proof


def _decode_command(value):
    if type(value) is not dict or set(value) != {'kind','request_id','body_json','conditions_json'}:
        raise ContractError('command_untrusted')
    result = Command(**value)
    if (_json(result.body) != result.body_json or _json(result.conditions) != result.conditions_json
            or not _identifier(result.kind) or not _ref(result.request_id)):
        raise ContractError('command_untrusted')
    return result


_RECORD_KEYS = frozenset(('schema_version send_id request_id draft_id version original_utc pk sk channel account_ref sender_ref '
    'recipients direction mode scope voice voice_config_version voice_authority_ref actor_ref grant_line_ids authority_ref '
    'operator_dispatch_ref envelope_digest wire_digest preview_policy_version content_ref subject_ref snapshot_ref attachment_refs '
    'attachment_count source_expires_at provider_message_id provider_thread_id thread_id reply_id references state attempt_id '
    'acceptance_at delivery_state reconciled_at terminal_at receipt_conflict receipts revision observed_versions').split())


def _record_valid(record):
    try:
        if type(record) is not dict or set(record)!=_RECORD_KEYS:
            return False
        if any(type(record[k]) is not int or record[k]<1 for k in ('schema_version','version','revision','preview_policy_version')):
            return False
        if record['schema_version']!=1 or record['preview_policy_version']!=1:
            return False
        if (record['state'] not in _STATES or record['delivery_state'] not in _DELIVERY
                or record['channel'] not in _CHANNELS or record['direction']!='outbound'
                or record['mode'] not in {'answer','outreach'} or not _SCOPE.fullmatch(record['scope'])
                or record['voice'] not in {'as_agent','as_operator'} or type(record['receipt_conflict']) is not bool
                or type(record['attachment_count']) is not int or record['attachment_count']<0):
            return False
        if not all(_ref(record[k]) for k in ('send_id','request_id','pk','sk','account_ref','sender_ref','voice_config_version',
                                               'actor_ref','content_ref','subject_ref','snapshot_ref')):
            return False
        if not all(record[k] is None or _ref(record[k]) for k in ('draft_id','voice_authority_ref','authority_ref',
                       'operator_dispatch_ref','provider_message_id','provider_thread_id','thread_id','reply_id','attempt_id')):
            return False
        if not _SHA.fullmatch(record['envelope_digest']) or (record['wire_digest'] is not None and not _SHA.fullmatch(record['wire_digest'])):
            return False
        if not all(type(record[k]) is list and all(_ref(x) for x in record[k]) for k in ('grant_line_ids','attachment_refs','references')):
            return False
        if record['attachment_count']!=len(record['attachment_refs']) or type(record['recipients']) is not list or not record['recipients']:
            return False
        for r in record['recipients']:
            if (type(r) is not dict or set(r)!={'contact_id','role','destination_ref'} or not _identifier(r['contact_id'])
                    or r['role'] not in {'to','cc','bcc'} or not _ref(r['destination_ref'])):
                return False
        if record['pk']!='CONTACT#'+min(r['contact_id'] for r in record['recipients']) or record['sk']!='SEND#'+record['original_utc']+'#'+record['send_id']:
            return False
        original = _instant(record['original_utc'])
        if not record['original_utc'].endswith('Z'):
            return False
        for key in ('acceptance_at','reconciled_at','terminal_at','source_expires_at'):
            if record[key] is not None:
                instant = _instant(record[key])
                if key!='source_expires_at' and instant<original:
                    return False
        if (type(record['observed_versions']) is not dict or not record['observed_versions']
                or not all(_identifier(k) and _identifier(v) for k,v in record['observed_versions'].items())
                or type(record['receipts']) is not list or not all(type(r) is dict for r in record['receipts'])):
            return False
        for receipt in record['receipts']:
            if (set(receipt)!={'receipt_id','attempt_id','provider_message_id','acceptance','delivery_state','occurred_at'}
                    or not all(_ref(receipt[k]) for k in ('receipt_id','attempt_id','provider_message_id'))
                    or receipt['acceptance'] not in {'accepted','rejected','unknown'}
                    or receipt['delivery_state'] not in {'pending','delivered','failed','unknown'}
                    or _instant(receipt['occurred_at'])<original):
                return False
        state, delivery = record['state'],record['delivery_state']
        if state=='accepted' and (record['acceptance_at'] is None or record['provider_message_id'] is None or delivery not in {'pending','delivered','failed','unknown'}):
            return False
        if state in {'attempting','accepted','rejected','acceptance_unknown'} and record['attempt_id'] is None:
            return False
        if state=='rejected' and delivery!='not_applicable' or state=='acceptance_unknown' and delivery!='unknown':
            return False
        if (state in {'draft','held','ready','attempting','acceptance_unknown'} or record['receipt_conflict']
                or (state=='accepted' and delivery not in {'delivered','failed'})) and record['terminal_at'] is not None:
            return False
        return True
    except (KeyError, TypeError, ValueError, ContractError, AttributeError):
        return False


def _verified_record(ctx, proposed=None):
    request = _resolve(ctx,'request')
    proof = _resolve(ctx,'intent',request.subject) if request else None
    try:
        if proof is None:
            return None
        data = proof.data
        creation = _decode_command(data['creation'])
        original, row = creation.body['canonical'], data['record']
        if (creation.kind!='create_intent' or creation.request_id!=request.subject
                or creation.command_id!=data.get('committed_command_id') or not _ref(data.get('transaction_ref'))
                or not _record_valid(row) or set(row)!=set(original)
                or row['request_id']!=request.subject or (proposed is not None and row!=proposed)
                or any(row[k]!=original[k] for k in original if k not in _MUTABLE_RECORD)):
            return None
        now = _instant(ctx.now)
        if any(row[k] is not None and _instant(row[k])>now for k in ('acceptance_at','terminal_at','reconciled_at')):
            return None
        if any(_instant(r['occurred_at'])>now for r in row['receipts']):
            return None
        return row
    except (ContractError, KeyError, TypeError, ValueError):
        return None


def create_intent(envelope, trusted_context, grants, approval=None):
    ctx = trusted_context
    try:
        envelope_digest(envelope)
        replay = check_request_replay(ctx,envelope.digest)
        if replay['status'] not in {'new','existing'}:
            return _plan(reason=replay['reason'])
        request = _resolve(ctx,'request')
        if not request or not _clock(ctx,request.subject):
            return _plan(reason='clock_untrusted')
        if replay['status']=='existing':
            row = _verified_record(ctx)
            if row and [row['pk'],row['sk']]==replay['canonical_key'] and row['envelope_digest']==envelope.digest:
                return _plan('existing','intent_committed',record=row)
            return _plan(reason='canonical_unavailable')
        decision = admit(envelope,ctx,grants,approval=approval)
        if not decision['admitted']:
            return _plan(reason=decision['reason'])
        versions = _enforcement_versions(ctx,request.subject,decision['versions'],approval if decision['reason']=='one_shot' else None)
        if versions is None or not _attempt_open(ctx,request.subject,envelope.digest):
            return _plan(reason='enforcement_untrusted')
        publication = _publication(ctx,request.subject,versions)
        protected = _protected(ctx,request.subject,envelope)
        storage = _resolve(ctx,'persistence',request.subject)
        if not publication:
            return _plan(reason='publication_pending')
        if not protected:
            return _plan(reason='protected_unavailable')
        if (not storage or type(storage.data.get('policy_version')) is not int
                or storage.data != dict(status='available',restricted=True,atomic=True,policy_version=1)):
            return _plan(reason='persistence_untrusted')
        if not validate_preview_policy(ctx)['valid']:
            return _plan(reason='preview_untrusted')
        req, env, secret = request.data, envelope.data, protected.data
        if (not _identifier(req.get('send_id')) or not _identifier(req.get('request_id'))
                or type(req.get('original_utc')) is not str or not req['original_utc'].endswith('Z')
                or _instant(req['original_utc']) > _instant(ctx.now)):
            return _plan(reason='request_untrusted')
        workflow, actor = _resolve(ctx,'workflow',request.subject).data, _resolve(ctx,'actor',request.subject).data
        contacts = sorted({r['contact_id'] for r in env['recipients']})
        pk, sk = 'CONTACT#'+contacts[0], 'SEND#'+req['original_utc']+'#'+req['send_id']
        draft = _verified_draft(ctx,envelope)
        approval_proof = _resolve(ctx,'approval',request.subject,approval) if decision['reason']=='one_shot' else None
        row = dict(schema_version=1,send_id=req['send_id'],request_id=request.subject,
            draft_id=draft['draft_id'] if draft else None,version=draft['version'] if draft else 1,
            original_utc=req['original_utc'],pk=pk,sk=sk,channel=env['channel'],account_ref=env['account_ref'],
            sender_ref=env['sender_ref'],recipients=secret['recipient_refs'],direction='outbound',mode=workflow['mode'],scope=workflow['scope'],
            voice=env['voice'],voice_config_version=env['voice_config_version'],voice_authority_ref=env['voice_authority_ref'],
            actor_ref=actor['actor_ref'],grant_line_ids=[s['line_id'] for s in decision['snapshots']],
            authority_ref=approval if approval_proof else None,operator_dispatch_ref=workflow.get('dispatch_ref'),
            envelope_digest=envelope.digest,wire_digest=envelope.wire_digest,preview_policy_version=1,
            content_ref=secret['content_ref'],subject_ref=secret['subject_ref'],snapshot_ref=secret['snapshot_ref'],
            attachment_refs=secret['attachment_refs'],attachment_count=len(env['attachments']),source_expires_at=secret.get('source_expires_at'),
            provider_message_id=None,provider_thread_id=None,thread_id=env['thread_id'],reply_id=env['reply_id'],references=env['references'],
            state='ready',attempt_id=None,acceptance_at=None,delivery_state='not_attempted',reconciled_at=None,terminal_at=None,
            receipt_conflict=False,receipts=[],revision=1,observed_versions=versions)
        snapshot = dict(reference=secret['snapshot_ref'],grants=decision['snapshots'],versions=versions,
                        approval=approval_proof.data['binding'] if approval_proof else None)
        associations = [dict(pk='CONTACT#'+cid,sk=sk,target_pk=pk,target_sk=sk) for cid in contacts[1:]]
        pointer = dict(pk='REQUEST#'+request.subject,sk='POINTER',canonical_key=[pk,sk],envelope_digest=envelope.digest,status='active')
        command = _command('create_intent',request.subject,
            dict(canonical=row,associations=associations,request_pointer=pointer,protected_snapshot=snapshot),
            dict(request_status='new',replay_version=replay['version'],request_proof_ref=ctx.reference('request'),
                 publication_version=publication.version,versions=versions,protected_proof_ref=ctx.reference('protected')))
        return _plan('intent_pending','commit_required',[command],row)
    except (ContractError, ValueError, TypeError, KeyError, AttributeError):
        return _plan(reason='malformed_input')


def claim_attempt(record, envelope, trusted_context, grants, approval=None):
    ctx = trusted_context
    try:
        envelope_digest(envelope)
        replay = check_request_replay(ctx,envelope.digest)
        if replay['status']!='existing':
            return _plan(reason=replay['reason'],record=record)
        row = _verified_record(ctx,record)
        if row is None or row['envelope_digest']!=envelope.digest or replay['canonical_key']!=[row['pk'],row['sk']]:
            return _plan(reason='intent_uncommitted',record=record)
        if row['state']!='ready':
            return _plan(reason='explicit_attempt_authorization_required' if row['state']=='rejected' else 'attempt_consumed',record=row)
        if (not _clock(ctx,row['request_id']) or not _protected(ctx,row['request_id'],envelope) or not validate_preview_policy(ctx)['valid']
                or (row['source_expires_at'] is not None and _instant(row['source_expires_at'])<=_instant(ctx.now))):
            return _plan(reason='protected_unavailable',record=row)
        decision = admit(envelope,ctx,grants,approval=approval)
        if not decision['admitted']:
            return _plan(reason=decision['reason'],record=row)
        versions = _enforcement_versions(ctx,row['request_id'],decision['versions'],approval if decision['reason']=='one_shot' else None)
        if versions is None or not _attempt_open(ctx,row['request_id'],envelope.digest):
            return _plan(reason='enforcement_untrusted',record=row)
        publication = _publication(ctx,row['request_id'],versions)
        if versions!=row['observed_versions'] or publication is None:
            return _plan(reason='authority_changed',record=row)
        creation = _decode_command(_resolve(ctx,'intent',row['request_id']).data['creation'])
        if creation.body['protected_snapshot']['grants'] != decision['snapshots']:
            return _plan(reason='historical_authority_changed',record=row)
        approval_proof = _resolve(ctx,'approval',row['request_id'],approval) if decision['reason']=='one_shot' else None
        approval_id = approval_proof.data['binding']['approval_id'] if approval_proof else None
        attempt_id = 'attempt-'+_sha(_json([row['request_id'],row['send_id'],row['version']]).encode())[:24]
        updated = dict(row,state='attempting',attempt_id=attempt_id,revision=row['revision']+1)
        condition = dict(canonical_revision=row['revision'],state='ready',versions=versions,
            publication_version=publication.version,request_pointer=replay['canonical_key'],approval_id=approval_id,
            approval_version=approval_proof.version if approval_proof else None,approval_status='available' if approval_proof else None)
        attempt = dict(pk=row['pk'],sk='ATTEMPT#'+attempt_id,request_id=row['request_id'],send_id=row['send_id'],
                       attempt_id=attempt_id,envelope_digest=envelope.digest,state='attempting',claimed_at=ctx.now,approval_id=approval_id)
        command = _command('claim_attempt',row['request_id'],dict(record=updated,attempt=attempt,consume_approval=approval_id),condition)
        return _plan('claim_pending','commit_before_transport',[command],updated)
    except (ContractError, ValueError, TypeError, KeyError, AttributeError):
        return _plan(reason='malformed_input',record=record)


def _verified_claim(ctx, row):
    claim = _resolve(ctx,'claim',row['request_id'])
    try:
        if not claim:
            return None
        command = _decode_command(claim.data['command'])
        proposed = command.body['record']
        if (command.kind!='claim_attempt' or command.request_id!=row['request_id']
                or command.command_id!=claim.data.get('committed_command_id') or not _ref(claim.data.get('transaction_ref'))
                or proposed['attempt_id']!=row['attempt_id'] or proposed['envelope_digest']!=row['envelope_digest']
                or proposed['state']!='attempting' or proposed['revision']>row['revision']):
            return None
        return command
    except (ContractError, KeyError, TypeError, ValueError):
        return None


def record_result(record, trusted_context, result_ref=None):
    ctx = trusted_context
    row = _verified_record(ctx,record)
    if row is None or row['state']!='attempting' or not _verified_claim(ctx,row):
        return _plan(reason='claim_uncommitted_or_consumed',record=record)
    result = _resolve(ctx,'provider_result',row['request_id'],result_ref)
    if not result or not _clock(ctx,row['request_id']):
        return _plan(reason='provider_result_untrusted',record=row)
    try:
        data = result.data
        if (data.get('attempt_id')!=row['attempt_id'] or data.get('envelope_digest')!=row['envelope_digest']
                or not (_instant(row['original_utc']) <= _instant(data['occurred_at']) <= _instant(ctx.now))
                or data.get('outcome') not in {'accepted','rejected','timeout','ambiguous'}):
            return _plan(reason='provider_result_untrusted',record=row)
        state = data['outcome'] if data['outcome'] in {'accepted','rejected'} else 'acceptance_unknown'
        if state=='accepted' and (not _ref(data.get('provider_message_id')) or data.get('delivery_state') not in {'pending','delivered','failed','unknown'}):
            return _plan(reason='provider_result_untrusted',record=row)
        delivery = data['delivery_state'] if state=='accepted' else 'not_applicable' if state=='rejected' else 'unknown'
        terminal = ctx.now if state=='rejected' or (state=='accepted' and delivery in {'delivered','failed'}) else None
        updated = dict(row,state=state,delivery_state=delivery,provider_message_id=data.get('provider_message_id') if state=='accepted' else None,
                       acceptance_at=data['occurred_at'] if state=='accepted' else None,terminal_at=terminal,revision=row['revision']+1)
        command = _command('record_result',row['request_id'],dict(record=updated,attempt_result=dict(attempt_id=row['attempt_id'],state=state,provider_message_id=updated['provider_message_id'],
                                acceptance_at=updated['acceptance_at'],delivery_state=delivery),result_ref=ctx.reference('provider_result') if result_ref is None else result_ref),
                           dict(canonical_revision=row['revision'],attempt_id=row['attempt_id'],state='attempting'))
        return _plan('result_pending','evidence_commit_required',[command],updated)
    except (ContractError, KeyError, TypeError, ValueError):
        return _plan(reason='provider_result_untrusted',record=row)


def reconcile_receipt(record, trusted_context, receipt_ref=None):
    ctx = trusted_context
    row = _verified_record(ctx,record)
    if row is None or not row['attempt_id'] or not _verified_claim(ctx,row):
        return _plan(reason='claim_uncommitted',record=record)
    receipt = _resolve(ctx,'receipt',row['request_id'],receipt_ref)
    if not receipt or not _clock(ctx,row['request_id']):
        return _plan(reason='receipt_untrusted',record=row)
    try:
        data = receipt.data
        if (not _identifier(data.get('receipt_id')) or data.get('attempt_id')!=row['attempt_id']
                or data.get('envelope_digest')!=row['envelope_digest'] or not _ref(data.get('provider_message_id'))
                or data.get('acceptance') not in {'accepted','rejected','unknown'}
                or data.get('delivery_state') not in {'pending','delivered','failed','unknown'}
                or not (_instant(row['original_utc']) <= _instant(data['occurred_at']) <= _instant(ctx.now))):
            return _plan(reason='receipt_untrusted',record=row)
        evidence = {k:data[k] for k in ('receipt_id','attempt_id','provider_message_id','acceptance','delivery_state','occurred_at')}
        if evidence in row['receipts']:
            return _plan('unchanged','duplicate_receipt',record=row)
        updated = dict(row,receipts=row['receipts']+[evidence],reconciled_at=ctx.now,revision=row['revision']+1)
        same_provider = row['provider_message_id'] in {None,data['provider_message_id']}
        if same_provider:
            if data['acceptance']=='accepted':
                updated.update(state='accepted',provider_message_id=data['provider_message_id'],
                               acceptance_at=row['acceptance_at'] or data['occurred_at'])
            # Accepted is monotone even when a later receipt says rejected.
            elif data['acceptance']=='rejected' and row['state'] not in {'accepted','rejected'}:
                updated.update(state='rejected',delivery_state='not_applicable')
            finals = {r['delivery_state'] for r in updated['receipts'] if r['provider_message_id']==data['provider_message_id'] and r['acceptance']=='accepted' and r['delivery_state'] in {'delivered','failed'}}
            if row['state']=='accepted' and row['delivery_state'] in {'delivered','failed'}:
                finals.add(row['delivery_state'])
            acceptance_states = {r['acceptance'] for r in updated['receipts'] if r['provider_message_id']==data['provider_message_id'] and r['acceptance'] in {'accepted','rejected'}}
            if row['state'] in {'accepted','rejected'}:
                acceptance_states.add(row['state'])
            if len(finals)>1 or len(acceptance_states)>1 or row['receipt_conflict']:
                updated.update(delivery_state='unknown',receipt_conflict=True,terminal_at=None)
            elif updated['state']=='accepted':
                delivery = next(iter(finals)) if finals else (row['delivery_state'] if row['state']=='accepted' else data['delivery_state'])
                updated['delivery_state']=delivery
                if delivery in {'delivered','failed'}:
                    updated['terminal_at']=(row['terminal_at'] if row['state']==updated['state'] else None) or ctx.now
                else:
                    updated['terminal_at']=None
            elif updated['state']=='rejected':
                updated['terminal_at']=(row['terminal_at'] if row['state']==updated['state'] else None) or ctx.now
        command = _command('reconcile_receipt',row['request_id'],dict(record=updated,receipt_ref=ctx.reference('receipt') if receipt_ref is None else receipt_ref),
                           dict(canonical_revision=row['revision'],attempt_id=row['attempt_id'],evidence_only=True))
        return _plan('reconciliation_pending','evidence_only',[command],updated)
    except (ContractError, KeyError, TypeError, ValueError):
        return _plan(reason='receipt_untrusted',record=row)


def _add_seconds(timestamp, seconds):
    _instant(timestamp)
    match = _TIMESTAMP.fullmatch(timestamp)
    main, fraction, offset = match.groups()
    whole = int(datetime.fromisoformat(main+offset.replace('Z','+00:00')).timestamp()) + seconds
    return datetime.fromtimestamp(whole,timezone.utc).strftime('%Y-%m-%dT%H:%M:%S') + ('.'+fraction if fraction else '') + 'Z'


def retention_policy(record, trusted_context):
    refused = dict(eligible=False,reason='nonterminal',metadata_expires_at=None,snapshot_expires_at=None,
                   source_expires_at=record.get('source_expires_at') if type(record) is dict else None,barrier=None)
    try:
        if _verified_record(trusted_context,record) is None or not _clock(trusted_context,record['request_id']):
            return dict(refused,reason='retention_untrusted')
        if (record['pk'].startswith('CONTACT#UNRESOLVED#') or not record['recipients']
                or record['receipt_conflict'] or record['state'] not in {'accepted','rejected','canceled'}
                or (record['state']=='accepted' and record['delivery_state'] not in {'delivered','failed'})
                or record['terminal_at'] is None):
            return refused
        expiry = _add_seconds(record['terminal_at'],365*86400)
        barrier = dict(pk='REQUEST#'+record['request_id'],sk='EXPIRED',request_id=record['request_id'],status='expired')
        return dict(eligible=True,reason='paired_terminal_expiry',metadata_expires_at=expiry,snapshot_expires_at=expiry,
                    source_expires_at=record['source_expires_at'],barrier=barrier)
    except (ContractError, KeyError, TypeError, ValueError, OverflowError):
        return dict(refused,reason='retention_untrusted')


def staging_cleanup(trusted_context):
    ctx = trusted_context
    row = _verified_record(ctx)
    if row is None:
        return _plan(reason='canonical_acknowledgment_required')
    staging = _resolve(ctx,'staging',row['request_id'])
    if (not staging or staging.data.get('status')!='stored' or staging.data.get('envelope_digest')!=row['envelope_digest']
            or staging.data.get('restricted') is not True or staging.data.get('atomic') is not True
            or not _ref(staging.data.get('protected_ref'))):
        return _plan(reason='staging_untrusted')
    command = _command('delete_staging',row['request_id'],dict(protected_ref=staging.data['protected_ref']),
                       dict(canonical_key=[row['pk'],row['sk']],envelope_digest=row['envelope_digest']))
    cleanup = _resolve(ctx,'cleanup',row['request_id'])
    if cleanup and cleanup.data.get('status')=='deleted' and cleanup.data.get('command_id')==command.command_id:
        return _plan('cleaned','staging_deleted')
    return _plan('cleanup_pending','deletion_only_retry',[command])



def _assistant_signed(env):
    return any(env[k].endswith('\n\nSynthetic assistant') for k in ('body','text')) or env['html'].endswith('<p>Synthetic assistant</p>')


def _voice_authority(envelope, ctx, grant, contact_id):
    request = _resolve(ctx,'request')
    if request is None:
        return 'authority_missing', {}
    authority = _resolve(ctx,'voice_authority',request.subject)
    if (authority is None or not grant.get('authority') or authority.data.get('authority')!=grant['authority']
            or authority.data.get('origin')!='operator_ledger' or authority.data.get('account_ref')!=envelope['account_ref']
            or type(authority.data.get('contact_ids')) is not list or contact_id not in authority.data['contact_ids']
            or envelope['voice_authority_ref']!=ctx.reference('voice_authority')):
        return 'authority_missing', {}
    capability = _resolve(ctx,'capability',request.subject)
    setup = _resolve(ctx,'voice_setup',request.subject)
    if (capability is None or capability.data.get('operator_voice') is not True or setup is None
            or capability.data.get('setup_ref')!=ctx.reference('voice_setup')
            or setup.data.get('status')!='provisioned' or setup.data.get('account_ref')!=envelope['account_ref']
            or setup.data.get('channel')!=envelope['channel'] or setup.data.get('capability_version')!=capability.version):
        return 'operator_voice_disabled', {}
    if type(capability.data.get('contacts')) is not list or contact_id not in capability.data['contacts']:
        return 'contact_not_enabled', {}
    return None, dict(voice_authority=authority.version,voice_setup=setup.version)


def compose_voice(envelope, trusted_context, grants):
    """Compose final text before freeze. Never accepts an already bound payload."""
    ctx = trusted_context
    refused = dict(status='held',reason='config_untrusted',envelope=None,evidence=None)
    try:
        if type(envelope) is not dict:
            return dict(refused,reason='already_bound')
        # Validate raw input without normalizing any payload text.
        env = bind_envelope(envelope).data
        request = _resolve(ctx,'request')
        config = _resolve(ctx,'config',request.subject) if request else None
        source = _resolve(ctx,'source',request.subject) if request else None
        if config is None or config.data.get('default_voice_token')!='as_agent' or env['voice_config_version']!=config.version:
            return refused
        if source is None or _source_grants(source)!=grants:
            return dict(refused,reason='malformed_grant')
        if env['voice']=='as_agent':
            for field in ('body','text'):
                if not env[field].endswith('\n\nSynthetic assistant'):
                    env[field]+='\n\nSynthetic assistant'
            if not env['html'].endswith('<p>Synthetic assistant</p>'):
                env['html']+='<p>Synthetic assistant</p>'
            env['introduction']='Synthetic assistant: ' if env['channel']=='sms' else ''
            env['voice_authority_ref']=None
            return dict(status='composed',reason='default_voice',envelope=env,
                        evidence=dict(voice='as_agent',authority=None,config_version=config.version))
        if _assistant_signed(env):
            return dict(refused,reason='incompatible_voice_payload')
        env['voice_authority_ref']=ctx.reference('voice_authority')
        versions = {}
        for recipient in env['recipients']:
            rows = [r for r in grants if r['contact_id']==recipient['contact_id']]
            if len(rows)!=1 or not rows[0]['valid']:
                return dict(refused,reason='malformed_grant')
            workflow = _resolve(ctx,'workflow',request.subject)
            matched = [g for g in rows[0]['grants'] if workflow and g['mode']==workflow.data.get('mode') and g['scope']==workflow.data.get('scope')]
            if len(matched)!=1 or matched[0]['voice']!='as_operator':
                return dict(refused,reason='authority_missing')
            reason, observed = _voice_authority(env,ctx,matched[0],recipient['contact_id'])
            if reason:
                return dict(refused,reason=reason)
            versions.update(observed)
        env['introduction']=''
        # First-person authored fixture payload stays exact; no assistant signature.
        return dict(status='composed',reason='operator_voice',envelope=env,
                    evidence=dict(voice='as_operator',authority=env['voice_authority_ref'],config_version=config.version,
                                  capability_version=_resolve(ctx,'capability',request.subject).version,versions=versions))
    except (ContractError, ValueError, TypeError, KeyError, AttributeError):
        return dict(refused,reason='malformed_input')


from zoneinfo import ZoneInfo

_COVERED_PATHS = {'sms_desk':'sms','sms_legacy_primary':'sms','sms_legacy_analytics':'sms','sms_legacy_chat':'sms',
    'email_helper':'email','email_cli':'email','analytics_email':'email','botcomm':'botcomm','bot_direct_handoff':'botcomm'}
_DISABLED_PATHS = frozenset({'personal_google_voice_direct','personal_google_voice_queue','whatsapp','imessage','apple_mail_direct','voice_call'})
_TRACE_PRIOR = frozenset({'prompt:inbound','prompt:acknowledged','cancel:observed'})
_HISTORY_SOURCES = frozenset({'sms','email','botcomm','outbox','archive','legacy_sms'})


def _gate(plan, trace):
    return dict(plan,trace=trace)


def _draft_gate(envelope, ctx, reason, prior):
    try:
        # An invalid binding may be re-frozen only as an unadmitted draft.
        if type(envelope) is BoundEnvelope:
            envelope = bind_envelope(envelope.data)
        plan = draft_command(envelope,ctx,_deny(reason))
        labels = {'put_draft_and_handoff':['storage:draft_requested','handoff:pending'],
                  'request_handoff':['prompt:handoff'], 'stage_draft':['storage:stage_requested','hold:unadmitted']}
        trace = list(prior)
        for command in plan['commands']:
            trace += labels[command.kind]
        if not plan['commands']:
            trace += ['hold:unadmitted']
        return dict(status='held',reason=reason,commands=plan['commands'],trace=trace,record=plan['draft'])
    except (ContractError, ValueError, TypeError, KeyError):
        return _gate(_plan(reason='malformed_input'),list(prior)+['hold:unadmitted'])


def _gate_path_next(path, envelope, trusted_context, grants, approval=None):
    """Compute new proposals only; prior observations are never effects."""
    ctx = trusted_context
    prior = []
    try:
        request = _resolve(ctx,'request')
        if request is None:
            return _gate(_plan(reason='request_untrusted'),['hold:unadmitted'])
        path_proof = _resolve(ctx,'path',request.subject)
        # Expired/unknown requests must not allocate drafts/intents or consume attempts.
        replay = check_request_replay(ctx,envelope.digest if type(envelope) is BoundEnvelope else '')
        if replay['status'] not in {'new','existing'}:
            return _gate(_plan(reason=replay['reason']),prior+['hold:replay'])
        workflow = _resolve(ctx,'workflow',request.subject)
        classification = workflow.data.get('classification') if workflow else None
        if classification in {'delegated_assignment','business_product_texting'}:
            exclusion = _resolve(ctx,'exclusion',request.subject)
            if (exclusion and workflow.data.get('owner_evidence_ref')==ctx.reference('exclusion')
                    and exclusion.data.get('classification')==classification
                    and exclusion.data.get('workflow_id')==workflow.data.get('workflow_id')
                    and _ref(exclusion.data.get('owner_ref')) and _ref(exclusion.data.get('workflow_id'))):
                return dict(status='excluded-with-owner-evidence',reason=classification,commands=[],trace=['workflow:excluded'],
                            record=None,owner_evidence_ref=ctx.reference('exclusion'))
            return _draft_gate(envelope,ctx,'workflow_untrusted',prior)
        if path in _DISABLED_PATHS:
            return _draft_gate(envelope,ctx,'transport_disabled',prior)
        if path not in _COVERED_PATHS:
            return _draft_gate(envelope,ctx,'path_untrusted',prior)
        envelope_digest(envelope)
        if (path_proof is None or path_proof.data.get('path')!=path or path_proof.data.get('channel')!=_COVERED_PATHS[path]
                or envelope.data['channel']!=_COVERED_PATHS[path]):
            return _draft_gate(envelope,ctx,'path_untrusted',prior)
        row = _verified_record(ctx) if replay['status']=='existing' else None
        if replay['status']=='existing' and (row is None or replay['canonical_key']!=[row['pk'],row['sk']] or row['envelope_digest']!=envelope.digest):
            return _gate(_plan(reason='request_conflict'),prior+['hold:replay'])
        # Evidence recovery precedes current admission/access/voice checks and never sends.
        if row and row['attempt_id'] and ctx.reference('receipt') is not None:
            plan = reconcile_receipt(row,ctx)
            return _gate(plan,['receipt:reconcile_requested'] if plan['commands'] else ['hold:receipt'])
        if row and row['state']=='attempting' and ctx.reference('provider_result') is not None:
            plan = record_result(row,ctx)
            return _gate(plan,['receipt:result_requested'] if plan['commands'] else ['hold:receipt'])
        if row and row['state'] in {'accepted','rejected','acceptance_unknown','canceled'}:
            return _gate(_plan('complete','attempt_consumed',record=row),[])
        if path_proof.data.get('status')!='enabled':
            return _draft_gate(envelope,ctx,'path_disabled',prior)
        if path_proof.data.get('source_status')!='available' or path_proof.data.get('access')!='allowed':
            return _draft_gate(envelope,ctx,'source_unavailable',prior)
        if row and ctx.reference('staging') is not None:
            cleanup = staging_cleanup(ctx)
            if cleanup['status']!='cleaned':
                return _gate(cleanup,['cleanup:delete_staging'] if cleanup['commands'] else ['hold:cleanup'])
        control = _resolve(ctx,'attempt_control',request.subject)
        if row and control and control.data.get('status')=='canceled':
            if control.data.get('envelope_digest')!=row['envelope_digest'] or not _ref(control.data.get('transaction_ref')):
                return _gate(_plan(reason='cancellation_untrusted',record=row),['hold:unadmitted'])
            if row['state']!='ready':
                return _gate(_plan(reason='cannot_recall_claimed_attempt',record=row),['cancel:after_claim'])
            if not _clock(ctx,row['request_id']):
                return _gate(_plan(reason='clock_untrusted',record=row),['hold:unadmitted'])
            canceled = dict(row,state='canceled',terminal_at=ctx.now,revision=row['revision']+1)
            command = _command('cancel_intent',row['request_id'],dict(record=canceled),
                               dict(canonical_revision=row['revision'],state='ready',attempt_control_version=control.version))
            return _gate(_plan('cancellation_pending','unclaimed_only',[command],canceled),['cancel:requested'])
        if row is None:
            plan = create_intent(envelope,ctx,grants,approval)
            if not plan['commands']:
                return _draft_gate(envelope,ctx,plan['reason'],prior)
            return _gate(plan,prior+['storage:intent_requested'])
        execution = _resolve(ctx,'execution',request.subject)
        if row['state']=='ready':
            if not execution or execution.data.get('phase')!='live' or not _identifier(execution.data.get('invocation_id')):
                return _gate(_plan(reason='live_invocation_required',record=row),['hold:unadmitted'])
            plan = claim_attempt(row,envelope,ctx,grants,approval)
            if not plan['commands']:
                return _draft_gate(envelope,ctx,plan['reason'],prior)
            claim = plan['commands'][0]
            plan['commands'] = [_command(claim.kind,claim.request_id,dict(claim.body,execution_id=execution.data['invocation_id']),claim.conditions)]
            return _gate(plan,prior+['version:checked','storage:claim_requested'])
        if row['state']=='attempting':
            claim = _verified_claim(ctx,row)
            activation = _resolve(ctx,'dispatch_activation',request.subject)
            if (claim is None or activation is None or execution is None or execution.data.get('phase')!='live'
                    or activation.data.get('status')!='fresh' or not _identifier(activation.data.get('activation_id'))
                    or activation.data.get('attempt_id')!=row['attempt_id'] or activation.data.get('claim_command_id')!=claim.command_id
                    or not _ref(activation.data.get('transaction_ref'))
                    or not _identifier(execution.data.get('invocation_id')) or not _identifier(claim.body.get('execution_id'))
                    or not _identifier(activation.data.get('invocation_id'))
                    or execution.data.get('invocation_id')!=claim.body.get('execution_id')
                    or activation.data.get('invocation_id')!=claim.body.get('execution_id')):
                return _gate(_plan(reason='claim_activation_required',record=row),['hold:claimed_unknown'])
            if not _clock(ctx,row['request_id']):
                return _gate(_plan(reason='clock_untrusted',record=row),['hold:claimed_unknown'])
            protection = _protected(ctx,row['request_id'],envelope)
            if protection is None:
                return _gate(_plan(reason='protected_unavailable',record=row),['hold:claimed_unknown'])
            deadlines=[value for value in (row['source_expires_at'],protection.data.get('source_expires_at')) if value is not None]
            deadline=min(deadlines,key=_instant) if deadlines else None
            if deadline is not None and _instant(deadline)<=_instant(ctx.now):
                return _gate(_plan(reason='protected_unavailable',record=row),['hold:claimed_unknown'])
            command = _command('invoke_provider_once',row['request_id'],
                dict(attempt_id=row['attempt_id'],envelope_digest=row['envelope_digest'],content_ref=row['content_ref'],claim_command_id=claim.command_id),
                dict(activation_id=activation.data['activation_id'],execution_id=execution.data['invocation_id'],
                     unused_attempt=row['attempt_id'],live_invocation=True,consume_before_effect=True,
                     protected_version=protection.version,protected_proof_ref=ctx.reference('protected'),source_expires_at=deadline))
            return _gate(_plan('effect_pending','single_live_activation',[command],row),['provider:send_once'])
        return _gate(_plan(reason='state_untrusted',record=row),['hold:unadmitted'])
    except (ContractError, ValueError, TypeError, KeyError, AttributeError):
        return _draft_gate(envelope,ctx,'malformed_input',prior)


def gate_path(path, envelope, trusted_context, grants, approval=None):
    """Return prior history separately from next-step proposals; execute nothing."""
    prior = []
    try:
        request = _resolve(trusted_context,'request')
        proof = _resolve(trusted_context,'path',request.subject) if request else None
        candidate = proof.data.get('prior_trace') if proof else None
        if (proof and proof.data.get('path')==path and type(candidate) is list
                and all(type(label) is str and label in _TRACE_PRIOR for label in candidate)):
            prior = candidate[:]
    except (ContractError, ValueError, TypeError, KeyError, AttributeError):
        pass
    plan = _gate_path_next(path,envelope,trusted_context,grants,approval)
    next_trace = plan['trace']
    return dict(plan,prior_trace=prior,next_trace=next_trace,trace=prior+next_trace)


def _history_key(row):
    return (_instant(row['original_utc']),row['source_type'],row['stable_id'])


def _cursor_encode(row, version):
    value = [version,row['original_utc'],row['source_type'],row['stable_id']]
    return 'cursor-'+_sha(_json(value).encode())


def project_thread(rows, trusted_context, cursor=None, limit=50):
    """Read-only metadata projection; no content copies or current-grant lookup."""
    ctx = trusted_context
    held = dict(status='held',rows=[],missing_sources=[],cursor=None,cli='protected-unavailable')
    try:
        request = _resolve(ctx,'request')
        history = _resolve(ctx,'history',request.subject) if request else None
        if (history is None or history.data.get('rows')!=rows or not validate_preview_policy(ctx)['valid']
                or not _clock(ctx,request.subject) or type(rows) is not list
                or type(limit) is not int or not 1<=limit<=200 or type(history.data.get('sources')) is not dict):
            return held
        sources, now = history.data['sources'],_instant(ctx.now)
        missing, normalized = set(), []
        for row in rows:
            if (type(row) is not dict or row.get('source_type') not in _HISTORY_SOURCES
                    or not _identifier(row.get('source_version')) or not _identifier(row.get('source_record_id')) or not _identifier(row.get('stable_id'))
                    or row.get('channel') not in _CHANNELS or row.get('direction') not in {'inbound','outbound'}
                    or row.get('state') not in _STATES|{'received'} or row.get('delivery_state') not in _DELIVERY
                    or not _ref(row.get('account_ref')) or type(row.get('attachment_count')) is not int or row['attachment_count']<0
                    or row.get('role') not in {'to','cc','bcc'} or type(row.get('receipt_conflict',False)) is not bool
                    or row.get('identity_status','resolved') not in {'resolved','unresolved'} or _instant(row['original_utc'])>now):
                return held
            if row.get('provider_message_id') is not None and not _ref(row['provider_message_id']):
                return held
            if (type(row.get('grant_line_ids')) is not list or not all(type(x) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}:(?:answer|outreach|change):[a-z][a-z0-9_]{0,63}',x) for x in row['grant_line_ids'])
                    or type(row.get('references')) is not list or not all(_ref(x) for x in row['references'])
                    or any(row.get(k) is not None and not _ref(row[k]) for k in ('thread_id','reply_id'))):
                return held
            source = sources.get(row['source_type'])
            placeholder = 'metadata_only'
            if type(source) is not dict or source.get('status')!='available':
                missing.add(row['source_type']);placeholder='source_unavailable'
            elif source.get('access')!='allowed' or not _identifier(source.get('version')) or source.get('version')!=row.get('source_version'):
                missing.add(row['source_type']);placeholder='protected_unavailable'
            elif source.get('expires_at') is not None and _instant(source['expires_at'])<=now:
                missing.add(row['source_type']);placeholder='source_unavailable'
            elif row.get('snapshot_status')=='expired':
                placeholder='snapshot_expired'
            elif row.get('source_expires_at') is not None and _instant(row['source_expires_at'])<=now:
                placeholder='content_expired'
            elif row.get('content_status') in {'expired','unavailable'}:
                placeholder='content_expired' if row['content_status']=='expired' else 'content_unavailable'
            elif row.get('content_status')!='available' or row.get('snapshot_status')!='available':
                placeholder='protected_unavailable'
            elif row['role']=='bcc':
                placeholder='protected_recipient'
            terminal = (row['identity_status'] if 'identity_status' in row else 'resolved')=='resolved' and not row.get('receipt_conflict',False) and (row['state'] in {'rejected','canceled'} or row['state']=='accepted' and row['delivery_state'] in {'delivered','failed'})
            if terminal and row.get('metadata_expires_at') is not None and _instant(row['metadata_expires_at'])<=now:
                placeholder='terminal_expired'
            elif row.get('identity_status')=='unresolved' and placeholder=='metadata_only':
                placeholder='identity_unresolved'
            item = dict(row,placeholder=placeholder)
            normalized.append(item)
        # Named sources that failed before returning rows must still be disclosed.
        for name, source in sources.items():
            if name not in _HISTORY_SOURCES:
                return held
            if (type(source) is not dict or source.get('status')!='available' or source.get('access')!='allowed'
                    or not _identifier(source.get('version')) or (source.get('expires_at') is not None and _instant(source['expires_at'])<=now)):
                missing.add(name)
        normalized.sort(key=_history_key)
        unique = {}
        for row in normalized:
            key = ((row['channel'],row['account_ref'],row['provider_message_id']) if row.get('provider_message_id')
                   else (row['source_type'],row['source_record_id']))
            if key not in unique:
                unique[key]=dict(row)
            else:
                first=unique[key]
                if row['role']=='bcc':first['role']='bcc'
                if row.get('identity_status')=='unresolved':first['identity_status']='unresolved'
                severity=['metadata_only','protected_recipient','identity_unresolved','content_unavailable','content_expired','source_unavailable','protected_unavailable','snapshot_expired','conflicting_history','terminal_expired']
                if severity.index(row['placeholder'])>severity.index(first['placeholder']):first['placeholder']=row['placeholder']
                if any(first[k]!=row[k] for k in ('original_utc','direction','state','delivery_state','attachment_count','grant_line_ids')) and first['placeholder']!='terminal_expired':
                    first['placeholder']='conflicting_history';missing.update((first['source_type'],row['source_type']))
        selected = sorted(unique.values(),key=_history_key)
        if len({_history_key(row) for row in selected})!=len(selected):
            return held
        if cursor is not None:
            matches = [i for i,row in enumerate(selected) if _cursor_encode(row,ctx.reference('history')+':'+history.version)==cursor]
            if len(matches)!=1:
                return held
            selected=selected[matches[0]+1:]
        page = selected[:limit]
        output=[]
        for row in page:
            # Terminal TTL covers audit metadata and historical snapshot together.
            if row['placeholder'] in {'terminal_expired','conflicting_history'}:
                output.append(dict(placeholder=row['placeholder']))
                continue
            instant=datetime.fromisoformat(row['original_utc'].replace('Z','+00:00')).astimezone(ZoneInfo('America/Chicago'))
            line_ids = row['grant_line_ids'] if row['direction']=='outbound' and row['role']!='bcc' and row.get('identity_status','resolved')=='resolved' and row['placeholder'] not in {'snapshot_expired','protected_unavailable','source_unavailable','protected_recipient','identity_unresolved'} else []
            # Link only verified exact provider/thread/reply references; no identity guessing.
            linked = sum(1 for other in unique.values() if other is not row and other['role']!='bcc' and other['placeholder'] not in {'terminal_expired','conflicting_history'} and other['account_ref']==row['account_ref']
                         and other['channel']==row['channel'] and ((row['thread_id'] and row['thread_id']==other['thread_id'])
                         or (other.get('provider_message_id') and other['provider_message_id'] in ([row['reply_id']]+row['references']))))
            output.append(dict(channel=row['channel'],direction=row['direction'],state=row['state'],attachment_count=row['attachment_count'],
                acceptance=row['state'],delivery=row['delivery_state'],line_ids=line_ids,time_local=instant.isoformat(sep=' ',timespec='seconds'),
                placeholder=row['placeholder'],linked_messages=0 if row['role']=='bcc' else linked))
        cli='\n'.join('['+row['placeholder']+']' if row['placeholder'] in {'terminal_expired','conflicting_history'} else
            f"{row['time_local']} {row['channel']} {row['direction']} {row['acceptance']} delivery={row['delivery']} attachments={row['attachment_count']} "
            + ('line='+','.join(row['line_ids'])+' ' if row['line_ids'] else '') + '['+row['placeholder']+']' for row in output)
        return dict(status='partial_history' if missing else 'complete',rows=output,missing_sources=sorted(missing),
                    cursor=_cursor_encode(page[-1],ctx.reference('history')+':'+history.version) if len(selected)>limit else None,cli=cli)
    except (ContractError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return held
