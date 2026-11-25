"""Adapter to the EXISTING managed Codex voice lifecycle, not a credential reader.

The shared coordinator retains admission custody. Importing this module performs
no RPC, starts no provider session, and acquires no shared lock.
"""
import asyncio
import hashlib
import json
from pathlib import Path
import sys

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
    """Managed signaling; admission still belongs to the reviewed coordinator.

    runtime_factory is an in-process test seam, never an HTTP/config option.
    No runtime object, slot or RPC is created by import/status when disabled.
    """
    def __init__(self, config, *, runtime_factory=None, cleanup_seconds=10):
        self.config = config
        self.call_id = self.rpc = self.slot = self.attempt = None
        self.publish = self.answer = self.coordinator = self.timer = None
        self._runtime_factory = runtime_factory
        self._coordinator_task = self._late_task = None
        self._cleanup_task = None
        self._cleanup_deadline = None
        self._cleanup_requested = False
        self._cleanup_seconds = cleanup_seconds
        self._close_lock = asyncio.Lock()
        self._held = False
        self._submission_possible = False
        self._last_closed_call_id = None
        self.request_registry = self.release_store = None
        self.active_authority = self._greeting_task = None

    def status(self):
        if self._held:
            return {"ready": False, "message": "סיום השיחה הקודמת דורש בדיקה במחשב."}
        if self.call_id:
            return {"ready": False, "message": "השיחה בבית כבר בשימוש."}
        if self.config.get("enabled") is not True:
            return {"ready": False, "message": "המחשב זמין. השירות הקולי ממתין להפעלה מאושרת."}
        try:
            if self._load_coordinator().readiness(dict(self.config)).get("ready") is not True:
                return {"ready": False, "message": "השירות הקולי בבית עדיין אינו זמין."}
        except Exception:
            return {"ready": False, "message": "הגדרת החיבור הקולי במחשב דורשת בדיקה."}
        return {"ready": True, "message": "המחשב מוכן לשיחה"}

    def bind_server(self, registry, release_store):
        if self.call_id is not None: raise NativeUnavailable("Cannot replace active server trust")
        self.request_registry, self.release_store = registry, release_store

    def _load_coordinator(self):
        if self.coordinator is not None:
            return self.coordinator
        if self.request_registry is None or self.release_store is None:
            raise NativeUnavailable("Trusted server request registry is not bound")
        for filename, field in [("coordinator.py", "coordinator_sha256"), ("trusted.py", "trusted_registry_sha256")]:
            path = Path(__file__).with_name(filename)
            if hashlib.sha256(path.read_bytes()).hexdigest() != self.config.get(field):
                raise NativeUnavailable("Reviewed coordinator/request registry source changed")
        from .coordinator import PhoneCoordinator
        self.coordinator = PhoneCoordinator(self._runtime().phone_api, self.request_registry, self.release_store)
        return self.coordinator

    def _runtime(self):
        if self._runtime_factory:
            return self._runtime_factory()
        from types import SimpleNamespace
        shared = Path(self.config["shared_voice_directory"]).resolve()
        lifecycle = shared / "voice_ownership.py"
        phone_authority = shared / "phone_operation_authority.py"
        for path, field in [(lifecycle, "lifecycle_sha256"), (phone_authority, "phone_authority_sha256")]:
            if hashlib.sha256(path.read_bytes()).hexdigest() != self.config.get(field):
                raise NativeUnavailable("Reviewed canonical phone API source changed")
        sys.path.insert(0, str(shared))
        import voice_ownership
        import phone_operation_authority
        from delegation_contract import THREAD
        if (Path(voice_ownership.__file__).resolve() != lifecycle.resolve()
                or Path(phone_operation_authority.__file__).resolve() != phone_authority.resolve()
                or THREAD != self.config["thread_id"]):
            raise NativeUnavailable("Canonical lifecycle/backing identity changed")
        sys.path.insert(0, str(Path(self.config["peerbridge_directory"]).resolve()))
        from cxrpc import CodexRPC
        return SimpleNamespace(ProjectSlot=voice_ownership.ProjectSlot, NativeAttempt=voice_ownership.NativeAttempt,
                               rpc_factory=CodexRPC, thread=THREAD, fsync_directory=voice_ownership.fsync_directory,
                               phone_api=phone_operation_authority)

    def _persist_intent(self, runtime, sdp, device_binding):
        from .intent import make_intent, encode_intent
        import os
        intent = make_intent(call_id=self.call_id, nonce=self.slot.nonce, thread=runtime.thread,
                             sdp=sdp, device_binding=device_binding,
                             release_ref=self._load_coordinator().release_reference(self.config))
        payload = encode_intent(intent)
        path = self.slot.directory / ("phone-intent-" + self.slot.nonce + ".json")
        with path.open("xb") as stream:
            stream.write(payload); stream.flush(); os.fsync(stream.fileno())
        runtime.fsync_directory(self.slot.directory)
        self.slot.write("reserved", frontend="homevoice", call_id=self.call_id,
                        phone_intent_sha256=hashlib.sha256(payload).hexdigest())
        return intent

    async def start(self, call_id, sdp, publish, *, device_binding, request_capability=None):
        if self.call_id or self._held:
            raise NativeUnavailable("Voice slot is occupied or unresolved")
        if not self.status()["ready"]:
            raise NativeUnavailable("Native admission has not been released")
        self.call_id, self.publish = call_id, publish
        self._cleanup_deadline = None
        self._cleanup_requested = self._submission_possible = False
        self._coordinator_task = self._cleanup_task = self._late_task = None
        self.active_authority = self._greeting_task = None
        self.answer = asyncio.get_running_loop().create_future()
        try:
            self._load_coordinator().register_request(call_id, request_capability)
            runtime = self._runtime()
            # Every acquired-resource operation is inside this exception boundary.
            self.slot = runtime.ProjectSlot.for_project()
            self.slot.acquire(require_previous_closed=True)
            self.slot.assert_project_scope()
            self.attempt = runtime.NativeAttempt(self.slot)
            intent = self._persist_intent(runtime, sdp, device_binding)
            loop = asyncio.get_running_loop()
            def notify(method, params):
                loop.call_soon_threadsafe(self._notification, method, params)
            self.rpc = await asyncio.to_thread(runtime.rpc_factory, mode="unix", client_name="homevoice", on_notify=notify)
            account = await asyncio.to_thread(self.rpc.call, "account/read", {"refreshToken": False}, 5)
            if (account.get("account") or {}).get("type") != "chatgpt":
                raise NativeUnavailable("Managed subscription required; no fallback")
            await asyncio.to_thread(self.rpc.call, "thread/resume", {"threadId": runtime.thread, "excludeTurns": True}, 5)
            params = start_parameters(runtime.thread, self.slot.nonce, sdp)
            deadline = loop.time() + 15
            self._submission_possible = True
            self._coordinator_task = asyncio.create_task(self._load_coordinator().submit_phone_offer(
                self.attempt, self.rpc, params, deadline, authority=intent))
            await asyncio.wait_for(asyncio.shield(self._coordinator_task), self._remaining(deadline))
            await asyncio.wait_for(asyncio.shield(self.answer), self._remaining(deadline))
            self.active_authority = self._load_coordinator().activate_call(call_id, self.attempt, runtime.fsync_directory)
            self.timer = asyncio.create_task(self._limit())
            return {"callID": call_id, "sdp": self.answer.result(), "model": MODEL}
        except BaseException:
            task_pending = self._coordinator_task is not None and not self._coordinator_task.done()
            submitted = self.attempt is not None and self.attempt.start_requested
            if submitted or task_pending:
                self._held = True
                await self.close(call_id)
            else:
                self._refuse_before_submit()
            raise

    async def peer_connected(self, call_id, control_id):
        if (call_id != self.call_id or self.active_authority is None or self._held
                or self._cleanup_requested or self._greeting_task is not None
                or self.answer is None or not self.answer.done()):
            raise NativeUnavailable('No unused owned active-call greeting authority')
        self._greeting_task = asyncio.create_task(self.active_authority.greet(self.rpc, control_id))
        result = await asyncio.shield(self._greeting_task)
        self._emit('greeting.acknowledged')
        return result

    def _refuse_before_submit(self):
        """No start was submitted. A failed durability repair still retains the lock."""
        self._held = True
        if self.rpc: self.rpc.close()
        self.rpc = None
        try:
            if self.slot is not None and self.slot.handle is not None and not self.slot.handle.closed:
                self.slot.assert_owned()
                self.slot.write("refused", setup_failed_before_submission=True)
                self.slot.release()
            self._load_coordinator().forget_request(self.call_id)
            self.call_id = self.slot = self.attempt = None
            self._held = False
        except Exception:
            # Deliberate hold, not an unobserved leak or a false closed claim.
            self._emit("call.error", "שמירת מצב החיבור נכשלה. נדרשת בדיקה במחשב.")

    @staticmethod
    def _remaining(deadline):
        value = deadline - asyncio.get_running_loop().time()
        if value <= 0: raise TimeoutError("Shared cleanup/startup deadline expired")
        return value

    def _emit(self, kind, text=None):
        if self.publish:
            self.publish({"type": kind, "callID": self.call_id, **({"text": text} if text else {})})

    def _notification(self, method, params):
        if not self.attempt or params.get("threadId") != self.config["thread_id"]:
            return
        try:
            if method == "thread/realtime/started":
                if self.attempt.on_started(params):
                    self._emit("call.started")
                    if self._cleanup_requested:
                        self._held = True
                        if self._cleanup_deadline <= asyncio.get_running_loop().time():
                            self._emit("call.error", "התקבל חיבור מאוחר לאחר חלון הסגירה. נדרשת בדיקה במחשב.")
                        elif self._late_task is None or self._late_task.done():
                            self._late_task = asyncio.create_task(self.close(self.call_id))
            elif method == "thread/realtime/sdp":
                if (not self.attempt.admitted or self.attempt.ownership_lost or self.answer.done()
                        or not str(params.get("sdp", "")).startswith("v=0")):
                    return
                self.answer.set_result(params["sdp"])
            elif method == "thread/realtime/closed":
                if self.attempt.on_closed(params): self._emit("call.closed")
            elif method == "thread/realtime/error":
                self.attempt.on_error(params); self._held = True
                self._emit("call.error", "השיחה נעצרה במחשב; נדרשת בדיקת החיבור.")
                if not self.answer.done(): self.answer.set_exception(NativeUnavailable("Native voice refused"))
            elif method == "turn/started" and self.attempt.admitted: self._emit("agent.working")
            elif method == "turn/completed" and self.attempt.admitted: self._emit("agent.done")
            # Realtime transcript items are not evidence of backend work.
        except Exception:
            self._held = True
            self._emit("call.error", "בעלות החיבור השתנתה. לא תיעצר שיחה אחרת.")

    async def _limit(self):
        await asyncio.sleep(300)
        await self.close(self.call_id)

    def _hold_cleanup(self):
        self._held = True
        if self.slot is not None:
            try:
                self.slot.assert_owned()
                self.slot.write("native_uncertain", phone_cleanup_unresolved=True,
                    native_closed_observed=bool(self.attempt and self.attempt.closed_event.is_set()),
                    cleanup_deadline=self._cleanup_deadline,
                    ownership_conflict=bool(self.attempt and self.attempt.ownership_lost))
            except Exception:
                self._emit("call.error", "לא ניתן לאשר שמירת מצב הסגירה; נדרשת בדיקה במחשב.")

    async def close(self, call_id):
        if call_id != self.call_id or self.attempt is None:
            if call_id is not None and call_id == self._last_closed_call_id:
                return True
            raise NativeUnavailable("Call identity does not belong to this frontend")
        if self._cleanup_deadline is None:
            self._cleanup_deadline = asyncio.get_running_loop().time() + self._cleanup_seconds
        deadline = self._cleanup_deadline
        self._cleanup_requested = True
        self.attempt.closing = True
        # Canonical on_started can now schedule owned late cleanup within THIS deadline.
        self.attempt.cleanup_deadline = deadline
        try:
            timeout = self._remaining(deadline)
            await asyncio.wait_for(self._close_lock.acquire(), timeout)
        except TimeoutError:
            if call_id == self._last_closed_call_id:
                return True
            self._hold_cleanup()
            return False
        try:
            if call_id != self.call_id or self.attempt is None:
                return call_id == self._last_closed_call_id
            if self._cleanup_task is None:
                self._cleanup_task = asyncio.create_task(self.attempt.close(self.rpc, cleanup_deadline=deadline))
            if self._coordinator_task and not self._coordinator_task.done():
                await asyncio.wait_for(asyncio.shield(self._coordinator_task), self._remaining(deadline))
            # A timed-out greeting can still have an in-flight RPC worker. Drain
            # both tasks before release; errors are settled, pending work is not.
            greeting_tasks = [self._greeting_task,
                getattr(self.active_authority, 'dispatch_task', None)]
            for task in greeting_tasks:
                if task is not None:
                    await asyncio.wait_for(asyncio.gather(asyncio.shield(task), return_exceptions=True),
                                           self._remaining(deadline))
            if self.attempt.start_requested and not self.attempt.closed_event.is_set():
                timeout = self._remaining(deadline)
                await asyncio.wait_for(self.attempt.closed_event.wait(), timeout)
            await asyncio.wait_for(asyncio.shield(self._cleanup_task), self._remaining(deadline))
            if self.attempt.ownership_lost:
                self._hold_cleanup()
                return False
            self.slot.release()
            if self.timer and self.timer is not asyncio.current_task(): self.timer.cancel()
            if self.rpc: self.rpc.close()
            self._load_coordinator().forget_request(call_id)
            self._last_closed_call_id = call_id
            self.call_id = self.rpc = self.slot = self.attempt = None
            self._held = False
            return True
        except Exception:
            self._hold_cleanup()
            # Observe task failures without losing a live late-admission observer.
            if self._cleanup_task and self._cleanup_task.done():
                try: self._cleanup_task.exception()
                except asyncio.CancelledError: pass
            return False
        finally:
            self._close_lock.release()
