"""Existing Expo-to-APNs transport; only opaque navigation hints leave the host."""
import asyncio
import json
import os
import urllib.error
import urllib.request

SEND_URL = 'https://exp.host/--/api/v2/push/send'
RECEIPTS_URL = 'https://exp.host/--/api/v2/push/getReceipts'


def payload(job):
    return {'to': job['token'], 'title': 'Approval key setup requested' if job['kind'] == 'enrollment' else 'Approval requested',
        'body': 'Open Pentacle to review this request.', 'sound': 'default',
        'data': {'kind': job['kind'], 'host_id': job['host_id'], 'request_id': job['request_id']}}


def post(url, data):
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
    access = os.environ.get('EXPO_ACCESS_TOKEN')
    if access:
        headers['Authorization'] = 'Bearer ' + access
    request = urllib.request.Request(url, json.dumps(data).encode(), headers, method='POST')
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def exchange(job, transport=post):
    try:
        if job.get('ticket_id'):
            result = transport(RECEIPTS_URL, {'ids': [job['ticket_id']]})
            data = result.get('data', {}).get(job['ticket_id'])
            if data is None:
                return {'pending': True, 'code': 'receipt_pending'}
        else:
            result = transport(SEND_URL, payload(job))
            data = result.get('data')
            if isinstance(data, list):
                data = data[0] if len(data) == 1 else None
        if not isinstance(data, dict):
            return {'transient': True, 'code': 'provider_invalid_response'}
        if data.get('status') == 'ok':
            return {'receipt_ok': True, 'code': 'provider_receipt_ok'} if job.get('ticket_id') else {'ticket_id': data.get('id'), 'code': 'provider_ticket_ok'}
        code = str((data.get('details') or {}).get('error') or 'provider_rejected')
        return {'code': code, 'transient': code in {'MessageRateExceeded', 'ExpoServerError'}}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return {'transient': True, 'code': 'transport_unavailable'}


async def send(job):
    return await asyncio.to_thread(exchange, job)
