"""Managed file upload and a single delegated assistant.publish operation.

The path guard is location-based, not a content secret detector. Receipts are
lookup carriers; publication authority and provenance remain daemon-owned.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

from _shared.chat_attachment_types import (
    ATTACHMENT_MAX_BYTES, CHAT_ATTACHMENT_PURPOSE, AttachmentError,
    sanitized_filename, validated_media_type,
)

_RECEIPT_FIELDS = ('upload_id', 'blob_sha', 'bytes', 'media_type', 'filename',
    'uploader', 'generation', 'uploaded_at', 'auth_kind', 'seat_stream_id',
    'seat_generation', 'credential_id', 'assistant_scope')
_SECRET_DIRS = ('.ssh', '.aws', '.gnupg', '.azure', '.kube', '.agent-orch',
    '.config/pentacle-stream', '.config/pentacle', '.config/agent-orch',
    '.config/dot', '.config/gcloud', '.config/gh', '.config/op',
    'Library/Keychains')


def _nonempty(value, limit=512):
    return isinstance(value, str) and 0 < len(value) <= limit and value == value.strip() and not any(ord(c) < 32 or ord(c) == 127 for c in value)


def _upload_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
        raise AttachmentError('upload_id_invalid')
    return value


def guarded_path(value):
    """Check lexical and canonical locations before opening; never read secrets."""
    lexical = Path(os.path.abspath(Path(value).expanduser()))
    canonical = lexical.resolve(strict=True)
    home = Path.home()
    roots = [home / p for p in _SECRET_DIRS]
    xdg = os.environ.get('XDG_CONFIG_HOME')
    if xdg:
        roots += [Path(xdg).expanduser() / p for p in ('pentacle-stream', 'pentacle', 'agent-orch', 'dot', 'gcloud', 'gh', 'op')]
    explicit = [Path(os.environ[k]).expanduser() for k in
        ('AGENT_ORCH_STREAM_TOKEN_FILE', 'AWS_SHARED_CREDENTIALS_FILE',
         'AWS_CONFIG_FILE', 'GOOGLE_APPLICATION_CREDENTIALS', 'KUBECONFIG')
        if os.environ.get(k)]
    for candidate in (lexical, canonical):
        if any(candidate == root or candidate.is_relative_to(root) or
               candidate == root.resolve() or candidate.is_relative_to(root.resolve()) for root in roots):
            raise AttachmentError('secret_path_refused')
        if any(candidate == p.absolute() or candidate == p.resolve() for p in explicit):
            raise AttachmentError('secret_path_refused')
        if candidate.name == '.env' or candidate.name.startswith('.env.'):
            raise AttachmentError('secret_path_refused')
    return canonical


def bounded_read(value, limit):
    path = guarded_path(value)
    # Walk resolved components using directory descriptors. Replacing a parent
    # with a symlink between validation and open fails closed.
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)
    with os.fdopen(fd, 'rb') as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise AttachmentError('file_not_regular')
        if before.st_size > limit:
            raise AttachmentError('attachment_too_large')
        data = handle.read(limit + 1)
        after = os.fstat(handle.fileno())
        if len(data) > limit:
            raise AttachmentError('attachment_too_large')
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise AttachmentError('file_changed')
    return data


def receipt_upload_id(value):
    if len(value) > 65536:
        raise AttachmentError('receipt_invalid')
    if value.lstrip().startswith('{'):
        raw = value
    else:
        raw = bounded_read(value, 65536).decode('utf-8')
    obj = json.loads(raw)
    if not isinstance(obj, dict):
        raise AttachmentError('receipt_invalid')
    return _upload_id(obj.get('upload_id'))


def checked_receipt(response, data, filename, media_type):
    if not isinstance(response, dict):
        raise AttachmentError('upload_receipt_invalid')
    _upload_id(response.get('upload_id'))
    expected = {'blob_sha': hashlib.sha256(data).hexdigest(), 'bytes': len(data),
                'filename': filename, 'media_type': media_type}
    if any(response.get(k) != v for k, v in expected.items()) or type(response.get('bytes')) is not int:
        raise AttachmentError('upload_receipt_mismatch')
    if any(k not in response for k in _RECEIPT_FIELDS) or not _nonempty(response.get('uploader')):
        raise AttachmentError('upload_receipt_invalid')
    kind = response['auth_kind']
    if kind == 'seat':
        if response['seat_stream_id'] != response['uploader'] or not _nonempty(response['generation']) or response['generation'] != response['seat_generation']:
            raise AttachmentError('upload_receipt_invalid')
    elif kind in ('scoped', 'operator'):
        if any(response[k] is not None for k in ('generation', 'seat_generation', 'seat_stream_id')) or not _nonempty(response['credential_id']):
            raise AttachmentError('upload_receipt_invalid')
    else:
        raise AttachmentError('upload_receipt_invalid')
    try:
        if not _nonempty(response['uploaded_at']) or datetime.fromisoformat(response['uploaded_at'].replace('Z', '+00:00')).utcoffset() is None:
            raise ValueError()
    except (TypeError, ValueError):
        raise AttachmentError('upload_receipt_invalid') from None
    return {k: response[k] for k in _RECEIPT_FIELDS}


def _print(value):
    print(json.dumps(value, separators=(',', ':')))


def run(args, *, load_config, upload_blob_once, assistant_once):
    try:
        sources = [getattr(args, k, None) for k in ('path', 'upload_id', 'from_receipt')]
        if sum(v is not None for v in sources) != 1:
            raise AttachmentError('file_source_required')
        target = getattr(args, 'to', None)
        correlation = {k: getattr(args, k, None) for k in
            ('dispatch_id', 'reply_to_message_id', 'publish_kind', 'request_id')}
        if target:
            if not _nonempty(target) or any(not _nonempty(correlation[k]) for k in ('dispatch_id', 'reply_to_message_id', 'publish_kind')):
                raise AttachmentError('publish_correlation_required')
            if correlation['publish_kind'] not in ('prose', 'question', 'result', 'status'):
                raise AttachmentError('publish_kind_invalid')
            if not correlation['request_id'] and correlation['publish_kind'] == 'prose':
                correlation['request_id'] = 'publish:' + correlation['dispatch_id']
            if not _nonempty(correlation['request_id'], 1024):
                raise AttachmentError('publish_request_id_required')
        elif any(correlation.values()) or not args.path:
            raise AttachmentError('publish_target_required')
        evidence = None
        if getattr(args, 'evidence_refs_json', None):
            evidence = json.loads(args.evidence_refs_json)
            if not isinstance(evidence, list) or len(evidence) > 16 or any(not _nonempty(v) for v in evidence) or len(set(evidence)) != len(evidence):
                raise AttachmentError('publish_evidence_invalid')
            if not target:
                raise AttachmentError('publish_target_required')
        data = None
        if args.path:
            filename = sanitized_filename(Path(args.path).name)
            data = bounded_read(args.path, ATTACHMENT_MAX_BYTES)
            if not data:
                raise AttachmentError('empty_file')
            mime = validated_media_type(filename, data[:16])
        else:
            upload_id = receipt_upload_id(args.from_receipt) if args.from_receipt else _upload_id(args.upload_id)
        config = load_config()
        if data is not None:
            response = asyncio.run(upload_blob_once(config, data, timeout=args.timeout,
                purpose=CHAT_ATTACHMENT_PURPOSE, filename=filename,
                request_id=getattr(args, 'upload_request_id', None)))
            if not isinstance(response, dict) or response.get('type') != 'upload_blob.ok':
                _print({'type': 'send_file.error', 'error_code': 'upload_failed'})
                return 1
            verified = checked_receipt(response, data, filename, mime)
            _print(verified)  # Durable handoff survives a later publish refusal.
            upload_id = verified['upload_id']
        if not target:
            return 0
        payload = dict(type='assistant.publish', composite_stream_id=target,
            **correlation, message=getattr(args, 'caption', '') or '',
            attachment_ids=[upload_id])
        for field in ('reply_to_question_id', 'response_state'):
            if getattr(args, field, None):
                payload[field] = getattr(args, field)
        if correlation['publish_kind'] == 'prose' and 'response_state' not in payload:
            payload['response_state'] = 'final'
        if evidence is not None:
            payload['evidence_refs'] = evidence
        response = asyncio.run(assistant_once(config, payload, timeout=args.timeout))
        # Never echo arbitrary server text or advisory receipt fields.
        if isinstance(response, dict) and response.get('type') == 'assistant.publish.ok':
            result = {'type': 'assistant.publish.ok', 'request_id': correlation['request_id'], 'upload_id': upload_id}
            if type(response.get('event_id')) is int:
                result['event_id'] = response['event_id']
            if type(response.get('duplicate')) is bool:
                result['duplicate'] = response['duplicate']
            _print(result)
            return 0
        code = response.get('error_code') if isinstance(response, dict) else None
        safe = code if code in ('publish_not_authorized', 'upload_unknown', 'blob_unknown',
            'attachment_invalid', 'publish_correlation_invalid', 'publish_replay_conflict') else 'publish_failed'
        _print({'type': 'assistant.publish.error', 'error_code': safe, 'upload_id': upload_id})
        return 1
    except AttachmentError as exc:
        _print({'type': 'send_file.error', 'error_code': exc.code})
        return 2
    except (OSError, UnicodeError, ValueError):
        _print({'type': 'send_file.error', 'error_code': 'file_or_receipt_invalid'})
        return 2
    except Exception:
        # Transport exceptions can contain credentials, URLs or request payloads.
        _print({'type': 'send_file.error', 'error_code': 'transport_error'})
        return 1
