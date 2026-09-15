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
    def __init__(self, store):
        self.store = store
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
        await self.store.append_session_event('hosta:v2-test',
            {'stream_id':'hosta:v2-test','provider':'codex','kind':'USER','text':text},
            identity='native:'+str(len(self.pastes))+':'+text,limit=500)
    async def run(self, *args, **kwargs): return 0, ''

@asynccontextmanager
async def fixture(tmp_path):
    store = Store(str(tmp_path/'sessions.db')); store.start()
    provider = Provider(store)
    sessions = Sessions(store, tmux=provider, local_host='hosta')
    await sessions.open('hosta','v2-test',provider='codex',visibility='visible')
    comms = Comms(store,sessions,SpawnCtl(store,sessions,tmux=provider),attachment_root=tmp_path/'attachments')
    queue = OutboundNoticeQueue(store,comms,config=OutboundNoticeConfig(lease_s=.1,max_attempts=2))
    notify = Notify(str(tmp_path/'notifications.db'),comms=comms,sessions=sessions,notice_store=store,outbound=queue)
    await notify.start()
    try: yield notify, queue, comms, provider, sessions, store
    finally: await notify.stop(); store.stop()

