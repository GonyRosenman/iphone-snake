"""Authenticated, loopback-only SDP/control broker. It never receives audio.

Use an already approved private HTTPS transport for the phone. This CLI does
not expose a public port, alter a firewall, install a service or mint OAuth.
"""
import argparse
import asyncio
import hmac
import hashlib
import json
from pathlib import Path
import re
import ssl
import uuid
from aiohttp import web
from .native import ManagedNativeHost, NativeUnavailable
from .trusted import RequestRegistry, RootReleaseStore, TrustError


class Broker:
    def __init__(self, host, registry):
        self.host, self.registry = host, registry
        self.current = self.current_control = self.current_capability = None
        self.seen = set()
        self.sockets = {}
        self.lock = asyncio.Lock()
        self.operation_lock = asyncio.Lock()
        self.tasks = set()

    def publish(self, event):
        async def send():
            if event.get("type") in ("call.closed", "call.error") and event.get("callID") == self.current:
                await self._close_current(event.get("callID"))
            for ws in tuple(self.sockets.values()):
                if not ws.closed:
                    try: await ws.send_json(event)
                    except ConnectionError: pass
        task = asyncio.create_task(send())
        self.tasks.add(task); task.add_done_callback(self.tasks.discard)

    async def status(self, request):
        return web.json_response(self.host.status(), headers={"Cache-Control": "no-store"})

    async def start(self, request):
        data = await body(request)
        identifier, sdp, control_id = data.get("callID"), data.get("sdp"), data.get("controlID")
        try: uuid.UUID(identifier)
        except (ValueError, TypeError, AttributeError): raise web.HTTPBadRequest(text="Invalid call identity")
        if set(data) != {"callID", "sdp", "controlID"} or not isinstance(control_id, str) or not isinstance(sdp, str) or not sdp.startswith("v=0") or len(sdp.encode()) > 60000:
            raise web.HTTPBadRequest(text="Only a WebRTC offer and call identity are accepted")
        async with self.lock:
            if self.current or identifier in self.seen: raise web.HTTPConflict(text="Reconcile existing call; no replay")
            if not any(not ws.closed for ws in self.sockets.values()): raise web.HTTPConflict(text="Home control connection is required")
            if not self.host.status()["ready"]: raise web.HTTPServiceUnavailable(text="Native voice is not released")
            try:
                capability = self.registry.issue_request(request["auth_context"], identifier, sdp, control_id)
            except TrustError:
                raise web.HTTPConflict(text="Authenticated control connection changed")
            self.current, self.current_control, self.current_capability = identifier, control_id, capability
            self.seen.add(identifier)
        try:
            async with self.operation_lock:
                if not any(not ws.closed for ws in self.sockets.values()):
                    self.current = None
                    raise NativeUnavailable("Home control disconnected before admission")
                result = await self.host.start(identifier, sdp, self.publish, device_binding=request["device_binding"],
                                               request_capability=capability)
            # Native signaling is not a credential-export surface.
            if set(result) != {"callID", "sdp", "model"}:
                raise NativeUnavailable("Unexpected signaling fields")
            return web.json_response(result, status=201, headers={"Cache-Control": "no-store"})
        except Exception:
            # No automatic retry. Keep current when admission/cleanup is uncertain.
            if getattr(self.host, "call_id", None) is None:
                self.registry.revoke_request(capability)
                self.current = self.current_control = self.current_capability = None
            raise web.HTTPServiceUnavailable(text="Voice could not start; inspect the home service")

    async def close(self, request):
        data = await body(request)
        if set(data) != {"callID"} or data.get("callID") != self.current or self.current is None:
            raise web.HTTPConflict(text="Call identity mismatch")
        # Revoke immediately; a serialized close must also cancel pending admission.
        self.registry.revoke_request(self.current_capability)
        closed = await self._close_current(data["callID"])
        return web.json_response({"closed": bool(closed)}, headers={"Cache-Control": "no-store"})

    async def _close_current(self, expected_call=None):
        async with self.operation_lock:
            if self.current is None:
                return True
            if expected_call is not None and self.current != expected_call:
                return False
            identifier = self.current
            closed = await self.host.close(identifier)
            if closed and self.current == identifier:
                self.registry.revoke_request(self.current_capability)
                self.current = self.current_control = self.current_capability = None
            return closed

    async def events(self, request):
        ws = web.WebSocketResponse(heartbeat=10, max_msg_size=4096)
        await ws.prepare(request)
        control_id = self.registry.add_control(request["auth_context"], ws)
        self.sockets[control_id] = ws
        try:
            await ws.send_json({"type": "home.connected", "connectionID": control_id})
            async for message in ws:
                # Native audio/control events go directly to OpenAI/managed Codex.
                # The phone cannot inject turn/start, provider URLs, thread IDs or commands.
                if message.type == web.WSMsgType.TEXT:
                    if message.data == 'ping': continue
                    try: event = json.loads(message.data)
                    except ValueError: event = None
                    if (not isinstance(event, dict) or set(event) != {'type','callID'}
                            or event.get('type') != 'peer.connected' or event.get('callID') != self.current
                            or self.current_control != control_id):
                        await ws.close(code=1008, message=b"Unsupported control event")
                        continue
                    async def greet(identifier=event['callID']):
                        try: await self.host.peer_connected(identifier, control_id)
                        except Exception:
                            if not ws.closed:
                                try: await ws.send_json({'type':'greeting.unavailable','callID':identifier})
                                except ConnectionError: pass
                    task = asyncio.create_task(greet())
                    self.tasks.add(task); task.add_done_callback(self.tasks.discard)
        finally:
            self.registry.revoke_control(control_id)
            self.sockets.pop(control_id, None)
            if self.current_control == control_id:
                identifier = self.current
                async def finish_owned():
                    try: await self._close_current(identifier)
                    except Exception: pass  # Retain unresolved canonical ledger.
                task = asyncio.create_task(finish_owned())
                self.tasks.add(task); task.add_done_callback(self.tasks.discard)
        return ws

    async def shutdown(self, app):
        if self.current:
            try:
                await self._close_current()
            except Exception: pass
        for ws in tuple(self.sockets.values()): await ws.close(code=1001)
        if self.tasks: await asyncio.gather(*tuple(self.tasks), return_exceptions=True)


async def body(request):
    try: value = await request.json()
    except (ValueError, UnicodeDecodeError): raise web.HTTPBadRequest(text="JSON required")
    if not isinstance(value, dict): raise web.HTTPBadRequest(text="JSON object required")
    return value


def create_app(secret, host, *, release_store=None):
    if not isinstance(secret, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", secret):
        raise ValueError("A separate 32+ character device pairing secret is required")
    registry = RequestRegistry(hashlib.sha256(secret.encode()).hexdigest())
    if isinstance(host, ManagedNativeHost):
        host.bind_server(registry, release_store or RootReleaseStore(
            host.config.get("root_release_file"), host.config.get("root_release_sha256")))
    @web.middleware
    async def authenticate(request, handler):
        if request.headers.get("Origin") or not hmac.compare_digest(request.headers.get("Authorization", ""), "Bearer " + secret):
            raise web.HTTPUnauthorized(text="Unauthorized")
        request["device_binding"] = registry.device_binding
        context = registry.authenticated()
        request["auth_context"] = context
        try: return await handler(request)
        finally: registry.finish_auth(context)
    app = web.Application(middlewares=[authenticate], client_max_size=65536)
    broker = Broker(host, registry)
    app[web.AppKey("broker", Broker)] = broker
    app.router.add_get('/v1/status', broker.status)
    app.router.add_get('/v1/events', broker.events)
    app.router.add_post('/v1/calls', broker.start)
    app.router.add_post('/v1/calls/close', broker.close)
    app.on_shutdown.append(broker.shutdown)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--cert', type=Path)
    parser.add_argument('--key', type=Path)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    secret_path = Path(config['pairing_secret_file'])
    if secret_path.stat().st_mode & 0o077: parser.error('Pairing secret file must be private (0600)')
    secret = secret_path.read_text().strip()
    tls = None
    if bool(args.cert) != bool(args.key): parser.error('Both certificate and key are required')
    if args.cert:
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.minimum_version = ssl.TLSVersion.TLSv1_2
        tls.load_cert_chain(args.cert, args.key)
    # Local only. Private phone reachability is a separate reviewed deployment.
    web.run_app(create_app(secret, ManagedNativeHost(config)), host='127.0.0.1', port=args.port,
                ssl_context=tls, access_log=None)


if __name__ == '__main__': main()
