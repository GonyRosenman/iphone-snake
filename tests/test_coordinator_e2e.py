"""Actual HTTP -> post-auth capability -> coordinator -> canonical lifecycle.

The native boundary is fake RPC and the canonical slot is temporary. Root release
files are explicit TEMPORARY FIXTURES, never runtime authority/deployment.
Mutations caught: accepting claims as a capability, skipping custody release,
ignoring control revocation, or submitting after current release revocation.
"""
import asyncio
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid
from aiohttp.test_utils import TestClient, TestServer
from homevoice_broker.native import ManagedNativeHost, NativeUnavailable
from homevoice_broker.server import create_app
from homevoice_broker.trusted import RootReleaseStore, RequestCapability, TrustError, encoded, sha
from homevoice_broker.coordinator import GREETING, GREETING_SHA256

SHARED = os.environ.get('HOMEVOICE_SHARED_SOURCE')
SECRET = 'offline-only-pairing-fixture-not-an-account-token'

@unittest.skipUnless(SHARED, 'Canonical source is external; no fake claim of CI runtime qualification')
class CoordinatorEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        sys.path.insert(0, SHARED)
        self.shared = importlib.import_module('voice_ownership')
        self.api = importlib.import_module('phone_operation_authority')
        self.thread = importlib.import_module('delegation_contract').THREAD
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.slot_path = self.directory/'slot'
        seed = self.shared.ProjectSlot(self.slot_path)
        seed.acquire(); seed.write('native_closed'); seed.release()
        self.slots = []; self.calls = []; self.rpc_objects = []
        self.resume_entered = asyncio.Event(); self.resume_gate = threading.Event()
        self.block_resume = False
        self.block_speech = False
        self.speech_entered = asyncio.Event();self.speech_gate = threading.Event()
        self.loop = asyncio.get_running_loop()
        fixture = self
        class TemporaryCanonicalSlot(self.shared.ProjectSlot):
            @classmethod
            def for_project(cls):
                value = cls(fixture.slot_path); fixture.slots.append(value); return value
            def assert_project_scope(self):
                self.assert_owned()
                if self.directory != fixture.slot_path: raise AssertionError('Non-temporary test slot')
        class NativeRPCFixture:
            def __init__(self, **kwargs):
                self.notify = kwargs['on_notify']; self.closed = False; fixture.rpc_objects.append(self)
            def call(self, method, params, timeout=10):
                if method == 'thread/resume' and fixture.block_resume:
                    fixture.loop.call_soon_threadsafe(fixture.resume_entered.set)
                    if not fixture.resume_gate.wait(2): raise RuntimeError('Fixture resume barrier expired')
                fixture.calls.append((method, params))
                if method == 'thread/realtime/appendSpeech' and fixture.block_speech:
                    fixture.loop.call_soon_threadsafe(fixture.speech_entered.set)
                    if not fixture.speech_gate.wait(2):raise RuntimeError('Fixture speech barrier expired')
                if method == 'account/read': return {'account': {'type': 'chatgpt'}}
                if method == 'thread/realtime/start':
                    self.notify('thread/realtime/started', {'threadId': fixture.thread,
                        'realtimeSessionId': params['realtimeSessionId']})
                    self.notify('thread/realtime/sdp', {'threadId': fixture.thread,'sdp':'v=0\r\no=offline-answer'})
                if method == 'thread/realtime/stop': self.notify('thread/realtime/closed', {'threadId': fixture.thread})
                return {}
            def close(self): self.closed = True
        self.runtime = SimpleNamespace(ProjectSlot=TemporaryCanonicalSlot, NativeAttempt=self.shared.NativeAttempt,
            rpc_factory=NativeRPCFixture, thread=self.thread, phone_api=self.api,
            fsync_directory=self.shared.fsync_directory)
        folder = Path(__file__).parent.parent/'homevoice_broker'
        self.config = {'enabled':True, 'thread_id':self.thread,
            'coordinator_sha256': sha((folder/'coordinator.py').read_bytes()),
            'trusted_registry_sha256':sha((folder/'trusted.py').read_bytes())}
        self.host = ManagedNativeHost(self.config, runtime_factory=lambda:self.runtime, cleanup_seconds=0.1)
        self.release_path = self.directory/'root-release-FIXTURE.json'
        self.release = {'version':1,'enabled':True,'owner_ref':'iphone-apps-tg-20','frontend':'homevoice',
            'release_ref':'offline-fixture-release','thread_id':self.thread,'model':'gpt-live-1.1-codex',
            'device_binding':sha(SECRET.encode()),
            'requested_effect':'one start; may retire stale voice on this exact backing thread only',
            'not_before':time.time()-1,'expires_at':time.time()+120,'max_operations':1}
        self.write_release()
        self.releases = RootReleaseStore(self.release_path, sha(self.release_path.read_bytes()))
        self.client = None; self.ws = None; self.control_id = None
        self.headers = {'Authorization':'Bearer '+SECRET}
        self.pending = []
        await self.open_app()
    def write_release(self):
        self.release_path.write_bytes(encoded(self.release)); self.release_path.chmod(0o600)
    async def open_app(self):
        self.client = TestClient(TestServer(create_app(SECRET, self.host, release_store=self.releases)))
        await self.client.start_server()
    async def asyncTearDown(self):
        self.resume_gate.set()
        self.speech_gate.set()
        for task in self.pending:
            if not task.done(): task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self.ws: await self.ws.close()
        if self.client: await self.client.close()
        for task in [self.host.timer,self.host._coordinator_task,self.host._cleanup_task,self.host._late_task]:
            if task and not task.done(): task.cancel()
            if task: await asyncio.gather(task,return_exceptions=True)
        if self.host.attempt and self.host.attempt.cleanup_task:
            task=self.host.attempt.cleanup_task
            if not task.done():task.cancel()
            await asyncio.gather(task,return_exceptions=True)
        for rpc in self.rpc_objects:rpc.close()
        for slot in self.slots:
            if slot.handle is not None:slot.abandon()
        self.temp.cleanup()
    async def control(self):
        self.ws = await self.client.ws_connect('/v1/events',headers=self.headers)
        self.control_id = (await self.ws.receive_json())['connectionID']
    def offer(self):return {'callID':str(uuid.uuid4()).upper(),'sdp':'v=0\r\no=offline-offer','controlID':self.control_id}
    def native_starts(self):return [p for m,p in self.calls if m=='thread/realtime/start']
    def speeches(self):return [p for m,p in self.calls if m=='thread/realtime/appendSpeech']
    async def begin_greeting_call(self, *, scoped=True):
        if scoped:
            # Generated private fixture only, never a production release/pin.
            self.release['greeting']={'owner_ref':'main-tg-1532','text_sha256':GREETING_SHA256,'max_operations':1}
            self.write_release();self.releases.expected_sha256=sha(self.release_path.read_bytes())
        await self.control();offer=self.offer()
        response=await self.client.post('/v1/calls',json=offer,headers=self.headers)
        self.assertEqual(response.status,201)
        return offer
    async def report_peer(self, offer):
        await self.ws.send_json({'type':'peer.connected','callID':offer['callID']})
    async def event(self, kind):
        async with asyncio.timeout(2):
            while True:
                value=await self.ws.receive_json()
                if value['type']==kind or (isinstance(kind,tuple) and value['type'] in kind):return value
    def hold_greeting_dispatch(self):
        original=self.host.active_authority.assert_owned
        entered=asyncio.Event();gate=threading.Event()
        def hold():
            if threading.current_thread() is not threading.main_thread():
                self.loop.call_soon_threadsafe(entered.set)
                if not gate.wait(2):raise RuntimeError('Fixture dispatch barrier expired')
            return original()
        self.host.active_authority.assert_owned=hold
        self.addCleanup(gate.set)
        return entered,gate

    async def test_speak_first_once_after_peer_report_without_owner_audio(self):
        # Mutation: speak before peer report, appendText instead of appendSpeech,
        # accept duplicate reports, or allow caller-provided text.
        offer=await self.begin_greeting_call()
        self.assertEqual(self.speeches(),[])
        self.assertEqual(list(self.slot_path.glob('phone-greeting-*.json')),[])
        await self.report_peer(offer);await self.event('greeting.acknowledged')
        self.assertEqual(self.speeches(),[{'threadId':self.thread,'text':GREETING}])
        self.assertEqual(len(self.native_starts()),1)
        self.assertEqual(len(list(self.slot_path.glob('phone-greeting-*.json'))),1)
        state=json.loads((self.slot_path/'voice-owner.json').read_text())
        self.assertTrue(state['phone_greeting_rpc_acknowledged'])
        await self.report_peer(offer);await self.event('greeting.unavailable')
        self.assertEqual(len(self.speeches()),1)
    async def test_greeting_requires_explicit_root_speech_scope(self):
        offer=await self.begin_greeting_call(scoped=False)
        await self.report_peer(offer);await self.event('greeting.unavailable')
        self.assertEqual(self.speeches(),[])
    async def test_early_peer_report_before_owned_sdp_cannot_speak(self):
        await self.control();self.block_resume=True;offer=self.offer()
        start=asyncio.create_task(self.client.post('/v1/calls',json=offer,headers=self.headers));self.pending.append(start)
        await asyncio.wait_for(self.resume_entered.wait(),2)
        await self.report_peer(offer);await self.event('greeting.unavailable')
        self.assertEqual(self.speeches(),[])
        self.resume_gate.set();self.assertEqual((await start).status,201)
    async def test_wrong_call_or_arbitrary_speech_payload_is_rejected(self):
        offer=await self.begin_greeting_call()
        await self.ws.send_json({'type':'peer.connected','callID':offer['callID'],'text':'untrusted speech'})
        async with asyncio.timeout(2):
            while not self.ws.closed:await self.ws.receive()
        self.assertEqual(self.speeches(),[])
    async def test_wrong_call_peer_report_never_speaks(self):
        await self.begin_greeting_call()
        await self.ws.send_json({'type':'peer.connected','callID':str(uuid.uuid4())})
        async with asyncio.timeout(2):
            while not self.ws.closed:await self.ws.receive()
        self.assertEqual(self.speeches(),[])
    async def test_foreign_control_cannot_report_owned_peer(self):
        offer=await self.begin_greeting_call()
        other=await self.client.ws_connect('/v1/events',headers=self.headers)
        await other.receive_json()
        await other.send_json({'type':'peer.connected','callID':offer['callID']})
        async with asyncio.timeout(2):
            while not other.closed:await other.receive()
        self.assertEqual(self.speeches(),[])
        # Closing an unrelated control must leave the actual owner's call alive.
        self.assertEqual(self.host.call_id,offer['callID'])
        await self.report_peer(offer);await self.event('greeting.acknowledged')
        self.assertEqual(len(self.speeches()),1)
    async def test_greeting_persistence_failure_never_dispatches_or_replays(self):
        offer=await self.begin_greeting_call()
        def fail(_):raise OSError('Injected greeting durability failure')
        self.host.active_authority.fsync_directory=fail
        await self.report_peer(offer);await self.event('greeting.unavailable')
        await self.report_peer(offer);await self.event('greeting.unavailable')
        self.assertEqual(self.speeches(),[])
        self.assertEqual(len(list(self.slot_path.glob('phone-greeting-*.json'))),1)
    async def test_timed_out_greeting_worker_must_drain_before_slot_release(self):
        # Mutation: drain only the greeting coroutine, omit its surviving worker.
        offer=await self.begin_greeting_call();self.block_speech=True
        # Advance both greeting clocks consistently near the ONE original expiry.
        advanced=lambda:time.monotonic()+9.9
        self.host.request_registry.clock=advanced
        with patch('homevoice_broker.coordinator.monotonic',side_effect=advanced):
            await self.report_peer(offer);await asyncio.wait_for(self.speech_entered.wait(),2)
            await self.event('greeting.unavailable')
        self.assertTrue(self.host._greeting_task.done())
        self.assertFalse(self.host.active_authority.dispatch_task.done())
        close=await self.client.post('/v1/calls/close',json={'callID':offer['callID']},headers=self.headers)
        self.assertEqual(await close.json(),{'closed':False})
        self.assertTrue(self.host._held);self.assertIsNotNone(self.host.slot.handle)
        self.speech_gate.set()
        await asyncio.gather(self.host.active_authority.dispatch_task,return_exceptions=True)
    async def test_foreign_native_started_never_receives_greeting(self):
        offer=await self.begin_greeting_call()
        self.host._notification('thread/realtime/started',{'threadId':self.thread,'realtimeSessionId':str(uuid.uuid4())})
        await self.report_peer(offer);await self.event('greeting.unavailable')
        self.assertEqual(self.speeches(),[])
        self.assertEqual([m for m,_ in self.calls if m=='thread/realtime/stop'],[])
    async def test_expired_greeting_does_not_receive_fresh_budget(self):
        offer=await self.begin_greeting_call()
        self.host.request_registry.clock=lambda:time.monotonic()+11
        await self.report_peer(offer);await self.event('greeting.unavailable')
        await self.report_peer(offer);await self.event('greeting.unavailable')
        self.assertEqual(self.speeches(),[])
    async def test_disconnect_revokes_pending_greeting_before_dispatch(self):
        offer=await self.begin_greeting_call();entered,gate=self.hold_greeting_dispatch()
        await self.report_peer(offer);await asyncio.wait_for(entered.wait(),2)
        await self.ws.close();gate.set()
        await asyncio.gather(self.host._greeting_task,return_exceptions=True)
        self.assertEqual(self.speeches(),[])
    async def test_end_revokes_pending_greeting_before_dispatch(self):
        offer=await self.begin_greeting_call();entered,gate=self.hold_greeting_dispatch()
        await self.report_peer(offer);await asyncio.wait_for(entered.wait(),2)
        revoked=asyncio.Event();original=self.host.request_registry.revoke_request
        def observe(capability):original(capability);revoked.set()
        self.host.request_registry.revoke_request=observe
        close=asyncio.create_task(self.client.post('/v1/calls/close',json={'callID':offer['callID']},headers=self.headers));self.pending.append(close)
        await asyncio.wait_for(revoked.wait(),2);gate.set()
        self.assertEqual(await (await close).json(),{'closed':True})
        self.assertEqual(self.speeches(),[])
    async def test_root_revocation_blocks_pending_greeting_at_dispatch(self):
        offer=await self.begin_greeting_call();entered,gate=self.hold_greeting_dispatch()
        await self.report_peer(offer);await asyncio.wait_for(entered.wait(),2)
        self.release['enabled']=False;self.write_release();gate.set()
        result=await self.event(('greeting.unavailable','greeting.acknowledged'))
        self.assertEqual(result['type'],'greeting.unavailable');self.assertEqual(self.speeches(),[])
    async def test_real_capability_and_fixture_release_reach_one_canonical_start(self):
        await self.control(); offer=self.offer()
        response=await self.client.post('/v1/calls',json=offer,headers=self.headers)
        self.assertEqual(response.status,201)
        self.assertEqual((await response.json())['callID'],offer['callID'])
        self.assertEqual(len(self.native_starts()),1)
        receipts=list(self.slot_path.glob('phone-consumed-*.json'))
        self.assertEqual(len(receipts),1)
        receipt=json.loads(receipts[0].read_text())
        self.assertEqual(receipt['binding']['call_id'],offer['callID'])
        self.assertEqual(receipt['binding']['device_binding'],sha(SECRET.encode()))
        self.assertEqual(receipt['binding']['sdp_sha256'],sha(offer['sdp'].encode()))
        response=await self.client.post('/v1/calls/close',json={'callID':offer['callID']},headers=self.headers)
        self.assertEqual(await response.json(),{'closed':True})
        self.assertEqual(len([1 for m,_ in self.calls if m=='thread/realtime/stop']),1)
        self.assertEqual(len(list(self.directory.glob('root-release-consumed-*.json'))),1)
        response=await self.client.post('/v1/calls',json=self.offer(),headers=self.headers)
        self.assertEqual(response.status,503,'One root release cannot authorize a second call')
    async def test_missing_root_release_never_creates_rpc(self):
        self.release_path.unlink()  # Generated test fixture only.
        await self.control()
        response=await self.client.post('/v1/calls',json=self.offer(),headers=self.headers)
        self.assertEqual(response.status,503);self.assertEqual(self.rpc_objects,[])
    async def test_wrong_pairing_and_forged_control_do_not_mint_request_authority(self):
        await self.control()
        response=await self.client.post('/v1/calls',json=self.offer(),headers={})
        self.assertEqual(response.status,401)
        response=await self.client.post('/v1/calls',json=self.offer()|{'controlID':str(uuid.uuid4())},headers=self.headers)
        self.assertEqual(response.status,409);self.assertEqual(self.rpc_objects,[])
        with self.assertRaises(TrustError):self.host.coordinator.register_request(str(uuid.uuid4()),RequestCapability())
    async def test_persisted_matching_looking_intent_is_not_authentication(self):
        await self.control()
        original=self.host._persist_intent
        def forged(runtime,sdp,device):return original(runtime,sdp,'f'*64)
        self.host._persist_intent=forged
        response=await self.client.post('/v1/calls',json=self.offer(),headers=self.headers)
        self.assertEqual(response.status,503);self.assertEqual(self.native_starts(),[])
        self.assertEqual(list(self.slot_path.glob('phone-consumed-*.json')),[])
    async def test_native_offer_substitution_cannot_borrow_authenticated_capability(self):
        # Mutation: omit registry's exact SDP binding; internally consistent native
        # claims would then authorize a different offer from the paired POST.
        await self.control()
        original=self.host.start
        async def substitute(call_id,sdp,*args,**kwargs):
            return await original(call_id,sdp+'\r\na=substituted',*args,**kwargs)
        self.host.start=substitute
        response=await self.client.post('/v1/calls',json=self.offer(),headers=self.headers)
        self.assertEqual(response.status,503);self.assertEqual(self.native_starts(),[])
        self.assertEqual(list(self.slot_path.glob('phone-consumed-*.json')),[])
    async def test_disconnect_before_verification_refuses_native_start(self):
        await self.control();self.block_resume=True
        task=asyncio.create_task(self.client.post('/v1/calls',json=self.offer(),headers=self.headers));self.pending.append(task)
        await asyncio.wait_for(self.resume_entered.wait(),2)
        await asyncio.wait_for(self.ws.close(),2)
        self.resume_gate.set()
        self.assertEqual((await task).status,503);self.assertEqual(self.native_starts(),[])
    async def test_explicit_close_before_verification_revokes_request(self):
        # Mutation: defer request revocation until the admission lock is drained.
        await self.control();self.block_resume=True;offer=self.offer()
        revoked=asyncio.Event()
        original=self.host.request_registry.revoke_request
        def observe(capability):original(capability);revoked.set()
        self.host.request_registry.revoke_request=observe
        start=asyncio.create_task(self.client.post('/v1/calls',json=offer,headers=self.headers));self.pending.append(start)
        await asyncio.wait_for(self.resume_entered.wait(),2)
        close=asyncio.create_task(self.client.post('/v1/calls/close',json={'callID':offer['callID']},headers=self.headers));self.pending.append(close)
        await asyncio.wait_for(revoked.wait(),2)
        self.resume_gate.set()
        self.assertEqual((await start).status,503);self.assertEqual(self.native_starts(),[])
        self.assertEqual(await (await close).json(),{'closed':True})
    async def test_root_revocation_before_verification_refuses_native_start(self):
        await self.control();self.block_resume=True
        task=asyncio.create_task(self.client.post('/v1/calls',json=self.offer(),headers=self.headers));self.pending.append(task)
        await asyncio.wait_for(self.resume_entered.wait(),2)
        self.release['enabled']=False;self.write_release();self.resume_gate.set()
        self.assertEqual((await task).status,503);self.assertEqual(self.native_starts(),[])
    async def test_root_revocation_after_proof_is_checked_at_rpc_boundary(self):
        await self.control()
        original=self.releases.assert_claim
        count=0
        def revoked(*args):
            nonlocal count
            count+=1
            if count==2:self.release['enabled']=False;self.write_release()
            return original(*args)
        self.releases.assert_claim=revoked
        response=await self.client.post('/v1/calls',json=self.offer(),headers=self.headers)
        self.assertEqual(response.status,503);self.assertEqual(self.native_starts(),[])
        self.assertEqual(len(list(self.slot_path.glob('phone-consumed-*.json'))),1)
        self.assertTrue(self.host._held,'Consumed but refused dispatch must not silently replay')
    async def test_delayed_rpc_dispatch_cannot_outlive_verified_deadline(self):
        # Mutation: omit dispatch-time proof expiry; a queued worker could start
        # after the event loop's canonical startup timeout has already elapsed.
        await self.control()
        with patch('homevoice_broker.coordinator.monotonic',return_value=float('inf')):
            response=await self.client.post('/v1/calls',json=self.offer(),headers=self.headers)
        self.assertEqual(response.status,503);self.assertEqual(self.native_starts(),[])
        self.assertEqual(len(list(self.slot_path.glob('phone-consumed-*.json'))),1)
