"""Owned store/queue fixture with a synthetic provider counterpart."""
import asyncio
from contextlib import asynccontextmanager
import pytest
from comms import Comms
from notify import Notify, ANSWER_TELL_ID_PREFIX
from outbound_notices import OutboundNoticeQueue, OutboundNoticeConfig
from sessions import Sessions
from spawnctl import SpawnCtl
from store import Store

class Provider:
    def __init__(self, store, host="hosta"):
        self.store = store
        self.host = host
        self.pastes = []
        self.pause_before = self.pause_after = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
    async def capture(self, name):
        return 'OpenAI Codex\n─────────\n› \n  gpt-6-astra high'
    async def paste(self, name, text):
        if self.pause_before:
            self.entered.set()
            await self.release.wait()
        self.pastes.append(text)
        await self.user(text)
        if self.pause_after:
            self.entered.set()
            await self.release.wait()
    async def user(self, text):
        session = await self.store.fetch_session(self.host, 'v2-test')
        await self.store.append_session_event(f'{self.host}:v2-test',
            {'stream_id':f'{self.host}:v2-test','host':self.host,'provider':'codex',
             'session_name':'v2-test','session_id':session['session_generation'],
             'timestamp':'2026-09-16T05:03:01.000Z','kind':'USER','text':text},
            identity='native:'+str(len(self.pastes))+':'+text,limit=500)
    async def run(self, *args, **kwargs): return 0, ''

@asynccontextmanager
async def fixture(tmp_path, *, host="hosta"):
    store = Store(str(tmp_path/'sessions.db')); store.start()
    provider = Provider(store, host=host)
    sessions = Sessions(store, tmux=provider, local_host=host)
    await sessions.open(host,'v2-test',provider='codex',visibility='visible')
    comms = Comms(store,sessions,SpawnCtl(store,sessions,tmux=provider),attachment_root=tmp_path/'attachments')
    queue = OutboundNoticeQueue(store,comms,config=OutboundNoticeConfig(lease_s=.1,max_attempts=2))
    notify = Notify(str(tmp_path/'notifications.db'),comms=comms,sessions=sessions,notice_store=store,outbound=queue)
    await notify.start()
    try: yield notify, queue, comms, provider, sessions, store
    finally: await notify.stop(); store.stop()
