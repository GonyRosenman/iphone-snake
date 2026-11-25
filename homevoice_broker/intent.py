"""A concrete phone-intent envelope and validator, NOT an admission grant.

Canonical NativeAttempt does not yet accept this authority. Only a reviewed
coordinator/shared API extension may turn it into a one-submission permission.
"""
import hashlib
import json
import re
import uuid

MODEL = "gpt-live-1.1-codex"
OWNER_REF = "iphone-apps-tg-20"


def make_intent(*, call_id, nonce, thread, sdp, device_binding, release_ref):
    uuid.UUID(call_id); uuid.UUID(nonce)
    if not isinstance(device_binding, str) or not re.fullmatch('[a-f0-9]{64}', device_binding):
        raise ValueError('Authenticated device binding is required')
    return {"version": 1, "frontend": "homevoice", "owner_ref": OWNER_REF,
            "source": "authenticated POST /v1/calls", "user_action": "explicit_connect_request",
            "call_id": call_id, "native_nonce": nonce, "thread_id": thread, "model": MODEL,
            "transport": "webrtc", "sdp_sha256": hashlib.sha256(sdp.encode()).hexdigest(),
            "device_binding": device_binding, "runtime_release_ref": release_ref,
            "requested_effect": "one start; may retire stale voice on this exact backing thread only"}


def encode_intent(intent):
    return (json.dumps(intent, sort_keys=True, separators=(',', ':'), ensure_ascii=True)+'\n').encode()


def validate_intent(intent, *, thread, nonce, sdp, call_id, device_binding, release_ref):
    expected = make_intent(call_id=call_id, nonce=nonce, thread=thread, sdp=sdp,
                           device_binding=device_binding, release_ref=release_ref)
    if not release_ref or intent != expected:
        raise ValueError('Phone intent does not match the reviewed single operation')
    return hashlib.sha256(encode_intent(intent)).hexdigest()
