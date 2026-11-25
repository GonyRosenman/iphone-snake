"""Server-owned request capabilities and read-only root-issued custody releases.

No JSON field creates a capability. No production release is created here.
Release consumption is local durable bookkeeping, never an owner grant.
"""
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import math
import stat
import threading
import time
import uuid

OWNER_REF = 'iphone-apps-tg-20'
MODEL = 'gpt-live-1.1-codex'
EFFECT = 'one start; may retire stale voice on this exact backing thread only'

class TrustError(RuntimeError): pass
class AuthContext: pass
class RequestCapability: pass

def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)+'\n').encode()
def sha(value): return hashlib.sha256(value).hexdigest()

@dataclass
class RequestRecord:
    auth: AuthContext
    call_id: str
    device_binding: str
    sdp_sha256: str
    control_id: str
    issued_at: float
    expires_at: float
    active: bool = True
    operation: tuple | None = None
    submitting: bool = False

class RequestRegistry:
    def __init__(self, device_binding, *, clock=time.monotonic):
        self.device_binding = device_binding
        self.clock = clock
        self.lock = threading.RLock()
        self.auth = {}
        self.controls = {}
        self.requests = {}

    def authenticated(self):
        # Called by trusted middleware only AFTER constant-time pairing auth.
        context = AuthContext()
        with self.lock: self.auth[context] = self.device_binding
        return context

    def finish_auth(self, context):
        with self.lock: self.auth.pop(context, None)

    def add_control(self, context, websocket):
        with self.lock:
            if self.auth.get(context) != self.device_binding: raise TrustError('Unauthenticated control')
            identifier = str(uuid.uuid4())
            self.controls[identifier] = (context, websocket)
            return identifier

    def revoke_control(self, identifier):
        with self.lock:
            self.controls.pop(identifier, None)
            for record in self.requests.values():
                if record.control_id == identifier: record.active = False

    def issue_request(self, context, call_id, sdp, control_id):
        uuid.UUID(call_id)
        with self.lock:
            control = self.controls.get(control_id)
            if (self.auth.get(context) != self.device_binding or not control
                    or self.auth.get(control[0]) != self.device_binding or control[1].closed):
                raise TrustError('An active authenticated control connection is required')
            capability = RequestCapability()
            now = self.clock()
            self.requests[capability] = RequestRecord(context, call_id, self.device_binding,
                sha(sdp.encode()), control_id, now, now+30)
            return capability

    def _active(self, capability):
        # Identity membership, never reconstructed call/fingerprint/source strings.
        if type(capability) is not RequestCapability: raise TrustError('Unissued request capability type')
        record = self.requests.get(capability)
        if record is None: raise TrustError('Unknown request capability')
        control = self.controls.get(record.control_id)
        if (not record.active or record.auth not in self.auth or not control
                or control[0] not in self.auth or control[1].closed
                or self.clock() >= record.expires_at):
            raise TrustError('Request/control capability expired or revoked')
        return record

    def register_call(self, capability, call_id):
        with self.lock:
            record = self._active(capability)
            if record.call_id != call_id: raise TrustError('Capability belongs to another call')
            return record

    def verify_operation(self, capability, binding):
        with self.lock:
            record = self._active(capability)
            if (record.call_id != binding.call_id or record.device_binding != binding.device_binding
                    or record.sdp_sha256 != binding.sdp_sha256):
                raise TrustError('Intent is not the authenticated request')
            operation = (binding.native_nonce, binding.params_sha256)
            if record.operation is not None and record.operation != operation:
                raise TrustError('Request capability already bound to another native operation')
            record.operation = operation
            evidence = {'call_id': record.call_id, 'device_binding': record.device_binding,
                        'sdp_sha256': record.sdp_sha256, 'control_id': record.control_id,
                        'issued_at_monotonic': record.issued_at, 'native_nonce': binding.native_nonce,
                        'params_sha256': binding.params_sha256}
            return sha(encoded(evidence)), record.expires_at

    def begin_submission(self, capability, binding):
        with self.lock:
            self.verify_operation(capability, binding)
            record = self.requests[capability]
            if record.submitting: raise TrustError('No request capability replay')
            record.submitting = True

    def revoke_request(self, capability):
        with self.lock:
            if capability in self.requests: self.requests[capability].active = False


class RootReleaseStore:
    """Reads an explicitly root-issued, hash-pinned private local release.

    Missing configuration/file, disabled state, expiry, identity changes or a
    consumed release all deny readiness. No issuer/runtime release is invented.
    """
    def __init__(self, path=None, expected_sha256=None, *, wall_clock=time.time):
        self.path = Path(path) if path else None
        self.expected_sha256 = expected_sha256
        self.wall_clock = wall_clock
        self.lock = threading.RLock()

    @staticmethod
    def _read_private(path):
        parent = path.parent.stat()
        if parent.st_uid != os.getuid() or parent.st_mode & 0o077:
            raise TrustError('Release directory must be private to its trusted publisher')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise TrustError('Trusted private regular release file required')
            raw = stream.read(65537)
        if len(raw) > 65536: raise TrustError('Release state exceeds its limit')
        return raw

    def inspect(self, *, thread, device):
        if not self.path or not self.expected_sha256: raise TrustError('No root custody release configured')
        raw = self._read_private(self.path)
        if sha(raw) != self.expected_sha256: raise TrustError('Root release identity changed')
        release = json.loads(raw)
        required = {'version','enabled','owner_ref','frontend','release_ref','thread_id','model',
                    'device_binding','requested_effect','not_before','expires_at','max_operations'}
        now = self.wall_clock()
        if type(release) is not dict or set(release) != required:
            raise TrustError('Malformed root release state')
        if any(type(release[name]) not in (int,float) or not math.isfinite(release[name]) for name in ('not_before','expires_at')):
            raise TrustError('Finite root release validity required')
        if (set(release) != required or type(release['version']) is not int or release['version'] != 1
                or release['enabled'] is not True or release['owner_ref'] != OWNER_REF
                or release['frontend'] != 'homevoice' or release['thread_id'] != thread
                or release['model'] != MODEL or release['device_binding'] != device
                or release['requested_effect'] != EFFECT or type(release['max_operations']) is not int
                or release['max_operations'] != 1 or not isinstance(release['release_ref'], str)
                or not release['release_ref'] or not release['not_before'] <= now < release['expires_at']):
            raise TrustError('No current matching root custody release')
        return release, sha(raw), release['expires_at']-now

    def _claim_path(self):
        return self.path.parent / ('root-release-consumed-'+self.expected_sha256+'.json')

    def available(self, *, thread, device):
        try:
            self.inspect(thread=thread, device=device)
            return not self._claim_path().exists()
        except (OSError, ValueError, TypeError, TrustError): return False

    def claim(self, binding, intent_sha256, request_evidence_sha256):
        with self.lock:
            release, digest, remaining = self.inspect(thread=binding.thread_id, device=binding.device_binding)
            if release['release_ref'] != binding.runtime_release_ref: raise TrustError('Release reference mismatch')
            claim = {'root_release_sha256': digest, 'binding': asdict(binding),
                     'intent_sha256': intent_sha256, 'request_evidence_sha256': request_evidence_sha256}
            raw = encoded(claim)
            fd = os.open(self._claim_path(), os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            directory = os.open(self.path.parent, os.O_RDONLY|os.O_DIRECTORY)
            try: os.fsync(directory)
            finally: os.close(directory)
            return sha(raw), remaining

    def assert_claim(self, binding, intent_sha256, request_evidence_sha256):
        with self.lock:
            release, digest, _ = self.inspect(thread=binding.thread_id, device=binding.device_binding)
            if release['release_ref'] != binding.runtime_release_ref: raise TrustError('Release revoked')
            raw = self._read_private(self._claim_path())
            expected = {'root_release_sha256': digest, 'binding': asdict(binding),
                        'intent_sha256': intent_sha256, 'request_evidence_sha256': request_evidence_sha256}
            if raw != encoded(expected): raise TrustError('Root consumption binding changed')
