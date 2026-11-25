"""Phone admission coordinator source; no default release or runtime issuer."""
import asyncio
import hashlib
import json
import os
from time import monotonic
from .trusted import TrustError, encoded, sha

GREETING = 'שלום גוני, אני כאן. זו בדיקת קול חיה. אתה לא צריך לדבר כדי לשמוע אותי.'
GREETING_SHA256 = sha(GREETING.encode())

class GuardedRPC:
    """Recheck observed disconnect/revocation at the native submission boundary."""
    def __init__(self, rpc, verifier): self.rpc, self.verifier = rpc, verifier
    def call(self, method, params, timeout=10):
        if method == 'thread/realtime/start': self.verifier.before_rpc(params)
        return self.rpc.call(method, params, timeout)


def make_verifier(api, registry, releases, capability):
    class TrustedRootVerifier(api.RootPhoneAuthorityVerifier):
        def __init__(self):
            self.binding = self.authority = self.request_digest = None
            self.valid_until = 0
        async def verify_phone_operation(self, authority, binding, challenge, startup_deadline):
            request_digest, expires = registry.verify_operation(capability, binding)
            release_digest, remaining = releases.claim(binding, authority.intent_sha256, request_digest)
            # Recheck after durable work, before returning a canonical proof.
            registry.verify_operation(capability, binding)
            releases.assert_claim(binding, authority.intent_sha256, request_digest)
            now = asyncio.get_running_loop().time()
            valid_until = min(startup_deadline, expires, now+remaining)
            if now >= valid_until: raise TrustError('Request/release expired during verification')
            self.binding, self.authority, self.request_digest = binding, authority, request_digest
            self.valid_until = valid_until
            return api.VerifiedPhoneOperation(binding, challenge, request_digest, release_digest, now, valid_until)
        def before_rpc(self, params):
            if self.binding is None: raise TrustError('No verified root operation')
            if monotonic() >= self.valid_until: raise TrustError('Verified submission deadline expired')
            digest = hashlib.sha256(json.dumps(params, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
            if digest != self.binding.params_sha256: raise TrustError('Native parameters changed')
            releases.assert_claim(self.binding, self.authority.intent_sha256, self.request_digest)
            registry.begin_submission(capability, self.binding)
    return TrustedRootVerifier()


class PhoneCoordinator:
    def __init__(self, api, registry, releases):
        self.api, self.registry, self.releases = api, registry, releases
        self.requests = {}
        self.verifiers = {}
    def readiness(self, config):
        return {'ready': self.releases.available(thread=config['thread_id'], device=self.registry.device_binding)}
    def release_reference(self, config):
        value, _, _ = self.releases.inspect(thread=config['thread_id'], device=self.registry.device_binding)
        return value['release_ref']
    def register_request(self, call_id, capability):
        self.registry.register_call(capability, call_id)
        if call_id in self.requests: raise TrustError('Coordinator request replay')
        self.requests[call_id] = capability
    def forget_request(self, call_id):
        self.verifiers.pop(call_id, None)
        capability = self.requests.pop(call_id, None)
        if capability is not None: self.registry.revoke_request(capability)
    async def submit_phone_offer(self, attempt, rpc, params, startup_deadline, *, authority):
        capability = self.requests.get(authority.get('call_id'))
        if capability is None: raise TrustError('No trusted authenticated request capability')
        claims = self.api.PhoneOperationAuthority.from_intent(authority)
        verifier = make_verifier(self.api, self.registry, self.releases, capability)
        # No raw start, historic retirement reference or empty-slot claim.
        result = await attempt.submit(GuardedRPC(rpc, verifier), params, startup_deadline=startup_deadline,
                                      phone_authority=claims, authority_verifier=verifier)
        self.verifiers[authority['call_id']] = verifier
        return result

    def activate_call(self, call_id, attempt, fsync_directory):
        verifier = self.verifiers[call_id]
        if not attempt.admitted or attempt.ownership_lost or attempt.closing:
            raise TrustError('Exact owned Started required before call activation')
        capability = self.registry.activate_call(self.requests[call_id], verifier.binding)
        return ActivePhoneCall(self.registry, self.releases, capability, verifier, attempt, fsync_directory)


class ActivePhoneCall:
    """One static speak-first greeting, using server-owned active-call authority."""
    def __init__(self, registry, releases, capability, verifier, attempt, fsync_directory):
        self.registry, self.releases, self.capability = registry, releases, capability
        self.verifier, self.attempt, self.fsync_directory = verifier, attempt, fsync_directory
        self.dispatch_task = None

    def assert_owned(self):
        attempt, binding = self.attempt, self.verifier.binding
        record = self.registry.assert_active_call(self.capability, binding)
        if (not attempt.admitted or not attempt.native_started or attempt.ownership_lost or attempt.closing
                or attempt.error_event.is_set() or attempt.closed_event.is_set()
                or attempt.session_nonce != binding.native_nonce):
            raise TrustError('Greeting requires this exact live owned native session')
        state = attempt.slot.assert_owned()
        if (state.get('realtime_session_id') != binding.native_nonce
                or state.get('voice_session_id') != binding.native_nonce):
            raise TrustError('Greeting native ownership changed')
        self.releases.assert_greeting(binding, self.verifier.authority.intent_sha256,
                                     self.verifier.request_digest, GREETING_SHA256)
        return record, state

    async def greet(self, rpc, control_id):
        binding = self.verifier.binding
        self.registry.report_connected(self.capability, binding, control_id)
        self.assert_owned()
        record = self.registry.begin_greeting(self.capability, binding)
        # No await between validation, exclusive intent and durable consumption.
        payload = encoded({'version':1, 'owner_ref':'main-tg-1532', 'call_id':binding.call_id,
            'native_nonce':binding.native_nonce, 'device_binding':binding.device_binding,
            'control_id':record.control_id, 'text_sha256':GREETING_SHA256,
            'release_ref':binding.runtime_release_ref, 'greeting_deadline':record.greeting_deadline})
        path = self.attempt.slot.directory / ('phone-greeting-'+binding.native_nonce+'.json')
        fd = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload); stream.flush(); os.fsync(stream.fileno())
        self.fsync_directory(path.parent)
        _, state = self.assert_owned()
        self.attempt.slot.write(state['phase'], phone_greeting_requested=True,
                                phone_greeting_sha256=sha(payload), greeting_text_sha256=GREETING_SHA256)
        def dispatch():
            current, _ = self.assert_owned()
            if not current.connected or not current.greeting_requested:
                raise TrustError('Greeting connection authority missing')
            timeout = current.greeting_deadline-monotonic()
            if timeout <= 0: raise TrustError('One greeting deadline expired')
            return rpc.call('thread/realtime/appendSpeech',
                {'threadId':binding.thread_id, 'text':GREETING}, timeout)
        # One deadline established on active-call promotion, no retry/new budget.
        self.dispatch_task = asyncio.create_task(asyncio.to_thread(dispatch))
        await asyncio.wait_for(asyncio.shield(self.dispatch_task), max(0,record.greeting_deadline-monotonic()))
        # An ACK does not assert model output PCM or owner hearing.
        _, state = self.assert_owned()
        self.attempt.slot.write(state['phase'], phone_greeting_rpc_acknowledged=True)
        return {'acknowledged':True, 'audio_verified':False, 'owner_hearing_verified':False}
