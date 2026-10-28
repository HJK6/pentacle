"""One fixed read-only monitor's bounded desktop-lease authority."""
import os
import re

ACTOR='codex-reset-monitor'
TOKEN_FILE_ENV='PENTACLE_CODEX_RESET_MONITOR_TOKEN_FILE'
HOST_ENV='PENTACLE_CODEX_RESET_MONITOR_HOST'
VERBS=frozenset('screen.lease.'+v for v in ('acquire','renew','release'))


def host():
    value=os.environ.get(HOST_ENV,'')
    return value if re.fullmatch(r'[a-z0-9_-]+',value) else None


def valid_request(message):
    if message.get('type') not in VERBS or host() is None:return False
    allowed={'type','request_id','from_stream_id','stream_token','host','reason','ttl','lease_id'}
    if not set(message).issubset(allowed):return False
    if message.get('from_stream_id') not in (None,'',ACTOR):return False
    if 'host' in message and message['host']!=host():return False
    verb=message['type'].rpartition('.')[2]
    if verb=='acquire' and message.get('host')!=host():return False
    if verb in ('acquire','renew'):
        ttl=message.get('ttl',120)
        if type(ttl) is not int or not 1<=ttl<=120:return False
    return True
