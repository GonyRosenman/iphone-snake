"""Phone admission coordinator source; no default release or runtime issuer."""
import asyncio
import hashlib
import json
from time import monotonic
from .trusted import TrustError

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
        capability = self.requests.pop(call_id, None)
        if capability is not None: self.registry.revoke_request(capability)
    async def submit_phone_offer(self, attempt, rpc, params, startup_deadline, *, authority):
        capability = self.requests.get(authority.get('call_id'))
        if capability is None: raise TrustError('No trusted authenticated request capability')
        claims = self.api.PhoneOperationAuthority.from_intent(authority)
        verifier = make_verifier(self.api, self.registry, self.releases, capability)
        # No raw start, historic retirement reference or empty-slot claim.
        return await attempt.submit(GuardedRPC(rpc, verifier), params, startup_deadline=startup_deadline,
                                    phone_authority=claims, authority_verifier=verifier)
