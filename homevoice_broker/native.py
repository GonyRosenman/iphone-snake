"""Adapter to the EXISTING managed Codex voice lifecycle, not a credential reader.

The shared coordinator retains admission custody. Importing this module performs
no RPC, starts no provider session, and acquires no shared lock.
"""
import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time

MODEL = "gpt-live-1.1-codex"


def start_parameters(thread, nonce, sdp):
    if not isinstance(sdp, str) or not sdp.startswith("v=0") or len(sdp.encode()) > 60000:
        raise ValueError("A bounded WebRTC offer is required")
    return {
        "threadId": thread, "model": MODEL, "version": "v3", "outputModality": "audio",
        "realtimeSessionId": nonce, "voiceSessionId": nonce,
        "transport": {"type": "webrtc", "sdp": sdp},
        "clientManagedHandoffs": False, "codexResponseHandoffMode": "commentary",
        "backendReasoningStatus": False, "flushTranscriptTailOnSessionEnd": False,
        "includeStartupContext": False, "initialItems": [],
        "prompt": "Speak naturally and briefly in the caller's language. This is a private iPhone conversation. "
                  "Delegate tasks requiring tools, current facts or actions to the backing local Codex agent. "
                  "Relay its questions and verified results. Never claim an action succeeded without its result.",
        "realtimeStartInstructions": "The authenticated owner is using the private native phone voice app. "
                  "Keep existing permissions and confirmations. Voice transcripts can be incomplete or mistaken. "
                  "Return public-facing progress and final results through the native voice handoff. "
                  "Do not post to Telegram as a side effect of this conversation.",
    }


class NativeUnavailable(RuntimeError):
    pass


class ManagedNativeHost:
    """Imports the canonical lock/attempt; requires reviewed coordinator injection.

    The coordinator module must expose readiness(config) and async
    submit_phone_offer(attempt, rpc, params, startup_deadline). It owns fresh
    admission evidence/authority. This adapter does NOT reuse a historic
    retirement grant or infer vacancy from thread/read or a closed file.
    """
    def __init__(self, config):
        self.config = config
        self.call_id = None
        self.rpc = self.slot = self.attempt = None
        self.publish = None
        self.answer = None
        self.coordinator = None
        self.timer = None
        self._coordinator_task = None
        self._closing = False
        self._held = False

    def status(self):
        if self._held:
            return {"ready": False, "message": "סיום השיחה הקודמת דורש בדיקה במחשב."}
        if self.call_id:
            return {"ready": False, "message": "השיחה בבית כבר בשימוש."}
        if self.config.get("enabled") is not True:
            return {"ready": False, "message": "המחשב זמין. השירות הקולי ממתין להפעלה מאושרת."}
        try:
            module = self._load_coordinator()
            result = module.readiness(dict(self.config))
            if result.get("ready") is not True:
                return {"ready": False, "message": "השירות הקולי בבית עדיין אינו זמין."}
        except Exception:
            return {"ready": False, "message": "הגדרת החיבור הקולי במחשב דורשת בדיקה."}
        return {"ready": True, "message": "המחשב מוכן לשיחה"}

    def _load_coordinator(self):
        if self.coordinator is not None:
            return self.coordinator
        path = Path(self.config["coordinator_file"])
        expected = self.config["coordinator_sha256"]
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise NativeUnavailable("Reviewed coordinator identity is unavailable")
        spec = importlib.util.spec_from_file_location("homevoice_reviewed_coordinator", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not callable(getattr(module, "readiness", None)) or not callable(getattr(module, "submit_phone_offer", None)):
            raise NativeUnavailable("Coordinator contract is unavailable")
        self.coordinator = module
        return module

    async def start(self, call_id, sdp, publish):
        if self.call_id or self._held:
            raise NativeUnavailable("Voice slot is occupied or unresolved")
        if not self.status()["ready"]:
            raise NativeUnavailable("Native admission has not been released")
        coordinator = self._load_coordinator()
        shared = Path(self.config["shared_voice_directory"]).resolve()
        lifecycle = shared / "voice_ownership.py"
        if hashlib.sha256(lifecycle.read_bytes()).hexdigest() != self.config["lifecycle_sha256"]:
            raise NativeUnavailable("Shared lifecycle changed; reconcile before admission")
        # Load the canonical implementation in place. Never copy it or use our own lock.
        sys.path.insert(0, str(shared))
        from voice_ownership import ProjectSlot, NativeAttempt, fsync_directory
        from delegation_contract import THREAD
        if THREAD != self.config["thread_id"]:
            raise NativeUnavailable("The fixed backing identity changed")
        sys.path.insert(0, str(Path(self.config["peerbridge_directory"]).resolve()))
        from cxrpc import CodexRPC
        self.slot = ProjectSlot.for_project().acquire(require_previous_closed=True)
        self.slot.assert_project_scope()
        self.attempt = NativeAttempt(self.slot)
        self.call_id, self.publish = call_id, publish
        self.answer = asyncio.get_running_loop().create_future()
        loop = asyncio.get_running_loop()
        # An intent records one attempt; neither timeout nor reconnection replays it.
        intent = {"frontend": "homevoice", "call_id": call_id, "nonce": self.slot.nonce,
                  "thread_id": THREAD, "model": MODEL, "time": time.time(),
                  "sdp_sha256": hashlib.sha256(sdp.encode()).hexdigest()}
        import os
        intent_path = self.slot.directory / ("phone-intent-" + self.slot.nonce + ".json")
        with intent_path.open("x") as stream:
            json.dump(intent, stream); stream.flush(); os.fsync(stream.fileno())
        fsync_directory(self.slot.directory)
        self.slot.write("reserved", frontend="homevoice", call_id=call_id)

        def notify(method, params):
            loop.call_soon_threadsafe(self._notification, method, params)
        try:
            self.rpc = await asyncio.to_thread(CodexRPC, mode="unix", client_name="homevoice", on_notify=notify)
            account = await asyncio.to_thread(self.rpc.call, "account/read", {"refreshToken": False}, 5)
            if (account.get("account") or {}).get("type") != "chatgpt":
                raise NativeUnavailable("Managed subscription authentication is required; no fallback")
            await asyncio.to_thread(self.rpc.call, "thread/resume", {"threadId": THREAD, "excludeTurns": True}, 5)
            params = start_parameters(THREAD, self.slot.nonce, sdp)
            deadline = loop.time() + 15
            self._coordinator_task = asyncio.create_task(coordinator.submit_phone_offer(
                self.attempt, self.rpc, params, deadline))
            await asyncio.wait_for(asyncio.shield(self._coordinator_task), max(0, deadline-loop.time()))
            await asyncio.wait_for(asyncio.shield(self.answer), max(0, deadline-loop.time()))
            self.timer = asyncio.create_task(self._limit())
            return {"callID": call_id, "sdp": self.answer.result(), "model": MODEL}
        except BaseException:
            # Retain the observer after an uncertain submission so late Started is owned.
            if self.attempt.start_requested:
                self._held = True
                try: await self.close(call_id)
                except Exception: pass
            else:
                self.slot.release()
                if self.rpc: self.rpc.close()
                self.rpc = self.slot = self.attempt = None
                self.call_id = None
            raise

    def _emit(self, kind, text=None):
        if self.publish:
            self.publish({"type": kind, "callID": self.call_id, **({"text": text} if text else {})})

    def _notification(self, method, params):
        if not self.attempt or params.get("threadId") != self.config["thread_id"]:
            return
        try:
            if method == "thread/realtime/started":
                if self.attempt.on_started(params): self._emit("call.started")
            elif method == "thread/realtime/sdp":
                if (not self.attempt.admitted or self.attempt.ownership_lost or self.answer.done()
                        or not str(params.get("sdp", "")).startswith("v=0")):
                    return
                self.answer.set_result(params["sdp"])
            elif method == "thread/realtime/closed":
                if self.attempt.on_closed(params): self._emit("call.closed")
            elif method == "thread/realtime/error":
                self.attempt.on_error(params)
                self._held = True
                self._emit("call.error", "השיחה נעצרה במחשב; נדרשת בדיקת החיבור.")
                if not self.answer.done(): self.answer.set_exception(NativeUnavailable("Native voice refused"))
            elif method == "thread/realtime/item/started": self._emit("agent.working")
            elif method == "thread/realtime/item/completed": self._emit("agent.done")
        except Exception:
            self._held = True
            self._emit("call.error", "בעלות החיבור השתנתה. לא תיעצר שיחה אחרת.")

    async def _limit(self):
        await asyncio.sleep(300)
        await self.close(self.call_id)

    async def close(self, call_id):
        if call_id != self.call_id or not self.attempt:
            raise NativeUnavailable("Call identity does not belong to this frontend")
        if self._closing:
            raise NativeUnavailable("Close is already in progress")
        self._closing = True
        try:
            if self._coordinator_task and not self._coordinator_task.done():
                # Do not cancel a submitted start and lose its late-admission observer.
                await asyncio.wait_for(asyncio.shield(self._coordinator_task), 15)
            await self.attempt.close(self.rpc, wait_seconds=10)
            if self.attempt.start_requested and not self.attempt.closed_event.is_set():
                self._held = True
                return False
            self.slot.release()
            if self.timer and self.timer is not asyncio.current_task(): self.timer.cancel()
            if self.rpc: self.rpc.close()
            self.call_id = None
            self.rpc = self.slot = self.attempt = None
            self._held = False
            return True
        except Exception:
            self._held = True
            return False
        finally:
            self._closing = False
