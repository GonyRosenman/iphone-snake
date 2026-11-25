"""Actual ManagedNativeHost + canonical lifecycle, isolated native-RPC fixture.

Requires HOMEVOICE_SHARED_SOURCE to point to the existing canonical source.
CI without that external runtime reports these cases skipped, not qualified.
Only temporary test slots are acquired; no provider/account/native daemon is used.
Mutations caught: resource setup outside the exception boundary, release after
failed durability, missing late-stop scheduling, fresh per-event cleanup budgets,
and stopping a foreign native session.
"""
import asyncio
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from types import SimpleNamespace
from homevoice_broker.native import ManagedNativeHost

SHARED = os.environ.get('HOMEVOICE_SHARED_SOURCE')

@unittest.skipUnless(SHARED, 'Canonical shared runtime source is not available in this environment')
class ManagedLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        sys.path.insert(0, SHARED)
        self.shared = importlib.import_module('voice_ownership')
        self.thread = importlib.import_module('delegation_contract').THREAD
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        seed = self.shared.ProjectSlot(self.directory)
        seed.acquire(); seed.write("native_closed"); seed.release()
        self.created_slots = []
        self.rpc_calls = []
        self.start_tasks = []
        self.rpc_objects = []
        self.cleanup_entered = asyncio.Event()
        self.fail_refusal = False
        self.loop = asyncio.get_running_loop()
        fixture = self
        class IsolatedSlot(self.shared.ProjectSlot):
            @classmethod
            def for_project(cls):
                value = cls(fixture.directory)
                fixture.created_slots.append(value)
                return value
            def assert_project_scope(self):
                # Explicit test seam: real canonical locking/ledger on a TEMP slot.
                self.assert_owned()
                if self.directory != fixture.directory: raise AssertionError('Unexpected test path')
            def write(self, phase, **fields):
                if fixture.fail_refusal and phase == 'refused': raise OSError('Injected ledger durability failure')
                return super().write(phase, **fields)
        class ObservedAttempt(self.shared.NativeAttempt):
            async def close(self, *args, **kwargs):
                fixture.cleanup_entered.set()
                return await super().close(*args, **kwargs)
        class RPC:
            def __init__(self, **kwargs):
                self.notify = kwargs['on_notify']; self.closed = False
                fixture.rpc_objects.append(self)
            def call(self, method, params, timeout=10):
                fixture.rpc_calls.append((method, params))
                if method == 'account/read': return {'account': {'type': 'chatgpt'}}
                if method == 'thread/realtime/stop':
                    self.notify('thread/realtime/closed', {'threadId': fixture.thread})
                return {}
            def close(self): self.closed = True
        self.runtime = SimpleNamespace(ProjectSlot=IsolatedSlot, NativeAttempt=ObservedAttempt,
            rpc_factory=RPC, thread=self.thread, fsync_directory=self.shared.fsync_directory)
        self.host = ManagedNativeHost({'enabled': True, 'thread_id': self.thread},
                                      runtime_factory=lambda: self.runtime, cleanup_seconds=0.1)
        class Coordinator:
            def readiness(self, config): return {'ready': True}
            async def submit_phone_offer(self, attempt, rpc, params, deadline, *, authority):
                await attempt.submit(rpc, params, startup_deadline=deadline,
                    observe_native=lambda thread, challenge: fixture.shared.NativeObservation(
                        thread, None, challenge, 'daemon-runtime'))  # OFFLINE observer fixture only
                raise RuntimeError('Injected post-submit signaling failure')
        self.host.coordinator = Coordinator()
        self.events = []
        self.call = str(uuid.uuid4())
    async def asyncTearDown(self):
        for task in self.start_tasks:
            if not task.done(): task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for task in [self.host.timer, self.host._coordinator_task, self.host._cleanup_task, self.host._late_task]:
            if task and not task.done(): task.cancel()
            if task: await asyncio.gather(task, return_exceptions=True)
        attempt = self.host.attempt
        if attempt and attempt.cleanup_task:
            if not attempt.cleanup_task.done(): attempt.cleanup_task.cancel()
            await asyncio.gather(attempt.cleanup_task, return_exceptions=True)
        for rpc in self.rpc_objects: rpc.close()
        for slot in self.created_slots:
            if slot.handle is not None: slot.abandon()  # Test-only transport/temporary resources.
        self.temp.cleanup()
    async def start(self):
        return await self.host.start(self.call, 'v=0\r\no=offline-offer', self.events.append, device_binding='0'*64)
    def inject_started(self, nonce):
        self.host._notification('thread/realtime/started', {'threadId': self.thread, 'realtimeSessionId': nonce})
    async def test_intent_persistence_failure_refuses_before_any_rpc(self):
        def fail(*args): raise OSError('Injected intent fsync failure')
        self.host._persist_intent = fail
        with self.assertRaises(OSError): await self.start()
        self.assertEqual(self.rpc_calls, [])
        self.assertIsNone(self.host.call_id)
        self.assertFalse(self.host._held)
        self.assertIsNone(self.created_slots[0].handle)
        self.assertTrue(json.loads((self.directory/'voice-owner.json').read_text())['setup_failed_before_submission'])
    async def test_failed_refusal_durability_keeps_lock_and_does_not_claim_closed(self):
        self.fail_refusal = True
        def fail(*args): raise OSError('Injected intent failure')
        self.host._persist_intent = fail
        with self.assertRaises(OSError): await self.start()
        self.assertTrue(self.host._held)
        self.assertEqual(self.host.call_id, self.call)
        self.assertEqual(self.rpc_calls, [])
        competitor = self.shared.ProjectSlot(self.directory)
        with self.assertRaises(BlockingIOError): competitor.acquire()
        self.assertNotEqual(json.loads((self.directory/'voice-owner.json').read_text())['phase'], 'closed')
    async def test_matching_late_started_stops_once_and_confirms_closed(self):
        task = asyncio.create_task(self.start()); self.start_tasks.append(task)
        await asyncio.wait_for(self.cleanup_entered.wait(), 2)
        nonce = self.host.slot.nonce
        self.inject_started(nonce)
        with self.assertRaises(RuntimeError): await task
        stops = [params for method, params in self.rpc_calls if method == 'thread/realtime/stop']
        self.assertEqual(stops, [{'threadId': self.thread}])
        self.assertEqual(json.loads((self.directory/'voice-owner.json').read_text())['native_state'], 'closed')
        self.assertIsNone(self.host.call_id)
        self.assertFalse(self.host._held)
    async def test_started_after_deadline_remains_observed_and_held(self):
        with self.assertRaises(RuntimeError): await self.start()
        self.assertTrue(self.host._held)
        deadline = self.host._cleanup_deadline
        self.inject_started(self.host.slot.nonce)
        self.assertEqual(self.host._cleanup_deadline, deadline)
        self.assertEqual(self.host.attempt.cleanup_deadline, deadline)
        self.assertTrue(self.host._held)
        self.assertFalse(self.rpc_objects[0].closed, 'Late-admission observer must be retained')
        ledger = json.loads((self.directory/'voice-owner.json').read_text())
        self.assertTrue(ledger['late_admission_after_deadline'])
        self.assertNotEqual(ledger['phase'], 'closed')
        self.assertEqual([m for m, _ in self.rpc_calls if m == 'thread/realtime/stop'], [])
        self.assertFalse(await self.host.close(self.call))
        self.assertEqual(self.host._cleanup_deadline, deadline, 'Repeated close cannot reset its deadline')
    async def test_foreign_started_never_stops_foreign_session(self):
        task = asyncio.create_task(self.start()); self.start_tasks.append(task)
        await asyncio.wait_for(self.cleanup_entered.wait(), 2)
        self.inject_started(str(uuid.uuid4()))
        with self.assertRaises(RuntimeError): await task
        self.assertTrue(self.host._held)
        self.assertEqual([m for m, _ in self.rpc_calls if m == 'thread/realtime/stop'], [])

    async def test_coordinator_drain_and_native_close_share_one_deadline(self):
        drain = asyncio.Event()
        fixture = self
        class SlowCoordinator:
            def readiness(self, config): return {"ready": True}
            async def submit_phone_offer(self, attempt, rpc, params, deadline, *, authority):
                await attempt.submit(rpc, params, startup_deadline=deadline,
                    observe_native=lambda thread, challenge: fixture.shared.NativeObservation(
                        thread, None, challenge, 'daemon-runtime'))
                await drain.wait()
        self.host.coordinator = SlowCoordinator()
        task = asyncio.create_task(self.start()); self.start_tasks.append(task)
        # Synchronize to the actual fixture RPC submission, without sleeps.
        while not any(method == 'thread/realtime/start' for method, _ in self.rpc_calls):
            await asyncio.sleep(0)
        close = asyncio.create_task(self.host.close(self.call))
        await asyncio.wait_for(self.cleanup_entered.wait(), 2)
        deadline = self.host._cleanup_deadline
        self.inject_started(self.host.slot.nonce)
        self.assertFalse(await close, 'Unsettled coordinator must retain ownership')
        self.assertEqual(self.host.attempt.cleanup_deadline, deadline)
        self.assertEqual(self.host._cleanup_deadline, deadline)
        ledger = json.loads((self.directory/'voice-owner.json').read_text())
        self.assertTrue(ledger['phone_cleanup_unresolved'])
        self.assertNotEqual(ledger['phase'], 'closed')
        self.assertEqual(len([m for m, _ in self.rpc_calls if m == 'thread/realtime/stop']), 1)
        drain.set()
        self.host.answer.set_exception(RuntimeError('Offline fixture has finished'))
        with self.assertRaises(RuntimeError): await task
