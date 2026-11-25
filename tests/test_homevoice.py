"""Real loopback HTTP/WebSocket tests, with native service replaced at its boundary.

Mutations caught: bypass auth/origin checks, accept thread/model injection,
replay an offer, stop a foreign call, or announce unconfirmed cleanup as closed.
No Codex daemon, provider, microphone, account credential or shared slot touched.
"""
import asyncio
import unittest
import uuid
from aiohttp.test_utils import TestClient, TestServer
from homevoice_broker.server import create_app
from homevoice_broker.native import start_parameters, ManagedNativeHost, NativeUnavailable

SECRET = 'offline-test-pairing-secret-with-no-real-access'

class FixtureHost:
    def __init__(self):
        self.call_id = None
        self.starts = 0
        self.stops = []
        self.confirm_close = True
    def status(self): return {"ready": True, "message": "offline fixture"}
    async def start(self, call_id, sdp, publish, *, device_binding, request_capability=None):
        self.starts += 1; self.call_id = call_id; self.publish = publish
        return {"callID": call_id, "sdp": "v=0\r\no=offline-answer", "model": "gpt-live-1.1-codex"}
    async def close(self, call_id):
        self.stops.append(call_id)
        if self.confirm_close: self.call_id = None
        return self.confirm_close

class BrokerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.host = FixtureHost()
        self.client = TestClient(TestServer(create_app(SECRET, self.host)))
        await self.client.start_server()
        self.headers = {'Authorization': 'Bearer '+SECRET}
        self.ws = None
        self.control_id = str(uuid.uuid4())
    async def asyncTearDown(self):
        if self.ws: await self.ws.close()
        await self.client.close()
    async def control(self):
        self.ws = await self.client.ws_connect('/v1/events', headers=self.headers)
        hello = await self.ws.receive_json()
        self.assertEqual(hello['type'], 'home.connected')
        self.control_id = hello['connectionID']
    def offer(self): return {'callID': str(uuid.uuid4()), 'sdp': 'v=0\r\no=offline-offer', 'controlID': self.control_id}
    async def test_auth_and_origin_precede_native_operations(self):
        for headers in [{}, {'Authorization': 'Bearer wrong'}, dict(self.headers, Origin='https://other.example')]:
            response = await self.client.post('/v1/calls', json=self.offer(), headers=headers)
            self.assertEqual(response.status, 401)
        self.assertEqual(self.host.starts, 0)
    async def test_control_is_required_before_admission(self):
        response = await self.client.post('/v1/calls', json=self.offer(), headers=self.headers)
        self.assertEqual(response.status, 409)
        self.assertEqual(self.host.starts, 0)
    async def test_roundtrip_and_duplicate_offer(self):
        await self.control(); offer = self.offer()
        response = await self.client.post('/v1/calls', json=offer, headers=self.headers)
        self.assertEqual(response.status, 201)
        result = await response.json()
        self.assertEqual(result['model'], 'gpt-live-1.1-codex')
        self.assertEqual(result['callID'], offer['callID'])
        response = await self.client.post('/v1/calls', json=offer, headers=self.headers)
        self.assertEqual(response.status, 409)
        self.assertEqual(self.host.starts, 1)
        self.host.publish({'type': 'agent.working', 'callID': offer['callID']})
        self.assertEqual((await self.ws.receive_json())['type'], 'agent.working')
        response = await self.client.post('/v1/calls/close', json={'callID': offer['callID']}, headers=self.headers)
        self.assertEqual(await response.json(), {'closed': True})
        response = await self.client.post('/v1/calls', json=offer, headers=self.headers)
        self.assertEqual(response.status, 409, 'Previously attempted call IDs cannot replay')
    async def test_foreign_stop_and_injected_routing_are_rejected(self):
        await self.control()
        for extra in [{'threadId': 'foreign'}, {'model': 'other'}, {'apiKey': 'not-a-key'}, {'url': 'https://other.example'}]:
            response = await self.client.post('/v1/calls', json=self.offer()|extra, headers=self.headers)
            self.assertEqual(response.status, 400)
        response = await self.client.post('/v1/calls/close', json={'callID': str(uuid.uuid4())}, headers=self.headers)
        self.assertEqual(response.status, 409)
        self.assertEqual(self.host.starts, 0); self.assertEqual(self.host.stops, [])
    async def test_uncertain_close_retains_slot(self):
        await self.control(); offer = self.offer()
        await self.client.post('/v1/calls', json=offer, headers=self.headers)
        self.host.confirm_close = False
        response = await self.client.post('/v1/calls/close', json={'callID': offer['callID']}, headers=self.headers)
        self.assertEqual(await response.json(), {'closed': False})
        response = await self.client.post('/v1/calls', json=self.offer(), headers=self.headers)
        self.assertEqual(response.status, 409)
        self.assertEqual(self.host.starts, 1)
    async def test_websocket_disconnect_closes_only_owned_call(self):
        await self.control(); offer = self.offer()
        await self.client.post('/v1/calls', json=offer, headers=self.headers)
        await self.ws.close()
        # A following HTTP request is an observable synchronization point.
        for _ in range(10):
            if self.host.stops: break
            await asyncio.sleep(0)
        self.assertEqual(self.host.stops, [offer['callID']])
    async def test_disabled_native_host_does_not_load_coordinator_or_rpc(self):
        host = ManagedNativeHost({'enabled': False})
        self.assertFalse(host.status()['ready'])
        with self.assertRaises(NativeUnavailable): await host.start(str(uuid.uuid4()), 'v=0', lambda e: None, device_binding='0'*64)
        self.assertIsNone(host.rpc); self.assertIsNone(host.slot)

class NativeContractTests(unittest.TestCase):
    def test_native_route_owns_backend_handoff_without_token_export(self):
        value = start_parameters('fixed-home-agent', 'owned-call', 'v=0\r\no=offer')
        self.assertEqual(value['model'], 'gpt-live-1.1-codex')
        self.assertEqual(value['version'], 'v3')
        self.assertEqual(value['transport'], {'type': 'webrtc', 'sdp': 'v=0\r\no=offer'})
        self.assertEqual(value['realtimeSessionId'], value['voiceSessionId'])
        self.assertFalse(value['clientManagedHandoffs'])
        self.assertEqual(value['codexResponseHandoffMode'], 'commentary')
        self.assertFalse(value['backendReasoningStatus'])
        for forbidden in ('apiKey','token','backendUrl','delegation','serviceTier'):
            self.assertNotIn(forbidden, value)
    def test_sdp_is_required(self):
        for sdp in ['', 'not SDP', None, 'v=0'+'x'*60000]:
            with self.assertRaises(ValueError): start_parameters('agent', 'call', sdp)

if __name__ == '__main__': unittest.main()

class ConcurrentLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_waits_for_submitted_start_then_closes_exact_call(self):
        class SlowHost(FixtureHost):
            def __init__(self):
                super().__init__(); self.entered = asyncio.Event(); self.release = asyncio.Event()
            async def start(self, call_id, sdp, publish, *, device_binding, request_capability=None):
                self.entered.set(); await self.release.wait()
                return await super().start(call_id, sdp, publish, device_binding=device_binding, request_capability=request_capability)
        host = SlowHost()
        client = TestClient(TestServer(create_app(SECRET, host)))
        await client.start_server()
        headers = {'Authorization': 'Bearer '+SECRET}
        ws = await client.ws_connect('/v1/events', headers=headers)
        control_id = (await ws.receive_json())['connectionID']
        identifier = str(uuid.uuid4())
        start = asyncio.create_task(client.post('/v1/calls', json={'callID': identifier, 'sdp': 'v=0', 'controlID': control_id}, headers=headers))
        await host.entered.wait()
        close = asyncio.create_task(client.post('/v1/calls/close', json={'callID': identifier}, headers=headers))
        await asyncio.sleep(0)
        self.assertEqual(host.stops, [], 'Cannot release ownership while admission is pending')
        host.release.set()
        self.assertEqual((await start).status, 201)
        self.assertEqual(await (await close).json(), {'closed': True})
        self.assertEqual(host.stops, [identifier])
        await ws.close(); await client.close()


class NativeProjectionTests(unittest.TestCase):
    def test_transcript_items_are_not_backend_work(self):
        from types import SimpleNamespace
        host = ManagedNativeHost({"enabled": False, "thread_id": "fixed-home-agent"})
        host.call_id = "offline-call"
        host.attempt = SimpleNamespace(admitted=True)
        events = []
        host.publish = events.append
        host._notification("thread/realtime/item/started", {"threadId": "fixed-home-agent"})
        self.assertEqual(events, [])
        host._notification("turn/started", {"threadId": "foreign-agent"})
        self.assertEqual(events, [])
        host._notification("turn/started", {"threadId": "fixed-home-agent"})
        host._notification("turn/completed", {"threadId": "fixed-home-agent"})
        self.assertEqual([event["type"] for event in events], ["agent.working", "agent.done"])
