# Phone admission contract proposal — source only

This document and `intent.py` prepare a reviewable operation. They grant no
runtime authority. The canonical lifecycle source remains owned by its existing
custodian; no shared API extension is implemented here.

## Identity and custody

Keep the existing saved home-agent binding and canonical ProjectSlot lock/ledger.
The phone cannot choose a thread, model, transport service or reasoning backend.
All frontends use the same `ProjectSlot.for_project()` and `NativeAttempt`.
Require the prior operation's reconciliation to be closed before acquisition;
that ledger is necessary but is not proof the native daemon's voice slot is empty.
A new start can retire a stale voice session on the same backing thread. Any
reviewed authority must name that possible effect explicitly; it cannot authorize
stopping another thread or an observed foreign session.

## One authenticated operation

`POST /v1/calls` checks the separate device-pairing bearer before native work and
requires an active authenticated home-control channel. The request contains only
a fresh call UUID and an SDP offer. The iPhone sends it after Start is tapped.
A bearer authenticates the paired client; it is not cryptographic proof of a
physical tap. Runtime qualification must verify that UI behavior on the device.

After acquiring the canonical lock, construct and fsync one exclusive
`phone-intent-<native nonce>.json`, then fsync its directory and record its SHA-256
in the canonical ledger. `intent.make_intent` binds:

- owner instruction reference for the native phone voice app;
- authenticated request source and paired-device fingerprint (never the secret);
- app call UUID and both native caller IDs using the exact shared nonce;
- fixed saved thread, exact `gpt-live-1.1-codex`, WebRTC transport and SDP SHA-256;
- reviewed runtime-custody release reference;
- one start and the explicitly described possible same-thread stale-voice retirement.

`intent.validate_intent` checks an exact expected envelope, including rejection
of extra fields. Its return value is a content digest, not a grant. A coordinator
must independently verify the actual owner instruction and current runtime
custody release, not accept an arbitrary matching string as approval.

## Proposed coordinator API

A locally configured, hash-pinned coordinator exports:

```python
def readiness(config) -> dict:
    # Read-only custody/config qualification. Never start voice as a probe.
    return {"ready": False}

async def submit_phone_offer(attempt, rpc, params, startup_deadline, *, authority):
    # Validate exact authority against the held canonical slot and durable intent.
    # Refuse if closing, deadline expired, custody missing or previous work unresolved.
    # Consume one reviewed, operation-bound authority before the one native submission.
    # Use the reviewed CANONICAL API extension, not a copied lifecycle or raw-RPC bypass.
    raise NotImplementedError("Canonical phone authority extension requires custodian review")
```

The exact canonical extension shape is a shared-owner decision. It should accept
a structured one-operation authority or fresh authoritative empty-slot
observation. Do not pass a historic one-probe retirement reference, fabricate a
`NativeObservation`, clear a prior intent, or label `thread/read` a vacancy check.
No implicit account/model/key fallback, retry, provider spend or public exposure
is added. `enabled` remains false while this extension/release is unresolved.

## Lifecycle and return path

Subscribe before submission. An RPC response is not native admission. Match
Started to the exact nonce before accepting thread-scoped SDP or stopping.
Native Codex handles the backing agent and returns its commentary; the phone
must not launch a second task for the same handoff.

All acquired-resource setup is exception-safe. If intent persistence fails before
submission, durably refuse and release; if even that refusal cannot be persisted,
retain the lock and mark the local host held. A pending coordinator is a possible
submission even if Started has not arrived.

On close, set one monotonic cleanup deadline before draining pending work. Start
owned cleanup under that same deadline and keep observing. A matching late
Started within the deadline can be stopped only through the canonical attempt.
After the deadline, retain the observer and unresolved ledger for reconciliation;
do not create a new cleanup budget on another event or silently claim closed.
Foreign Started/ownership loss must never cause a stop of that foreign session.

Release only after coordinator drain and exact native Closed are confirmed. A
failed or late close remains held/durable, including a known native close when
other submission work is unresolved. Ending voice does not claim to cancel
backing-agent tasks or undo effects already performed.

## Qualification ceiling

Source/contract tests use fake native RPC and temporary canonical slots. They do
not prove current service access, phone audio, real handoff execution, latency,
private HTTPS reachability or physical installation. Those need a separately
reviewed one-phone attempt after custody release. Never export account OAuth,
API keys or private native identities into the app/build artifacts.
