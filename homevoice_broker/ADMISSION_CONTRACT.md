# Phone admission contract — source only

This code integrates the custodian-reviewed canonical phone authority API. It
grants no runtime authority. Canonical lifecycle source remains owned by its
existing custodian and is imported in place; it is not copied or edited here.

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
a fresh call UUID, an SDP offer and the server-issued `controlID`. The iPhone sends it after Start is tapped.
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

## Trusted capability and coordinator

After constant-time pairing authentication, server middleware creates an opaque
`AuthContext`. The authenticated WebSocket gets a random connection ID. The POST
handler mints an identity-based `RequestCapability` bound to that actual HTTP
context, call UUID, device fingerprint, offer SHA-256 and live control connection.
No intent fields, claimed fingerprint, arbitrary constructed capability or JSON
object can recreate registry membership. Request authority expires after thirty
seconds and is revoked on control loss. The paired-client credential is separate
from all provider/account credentials. It does not prove a physical tap.

The built-in `PhoneCoordinator` implements the canonical abstract root verifier.
It verifies the actual registry entry against the canonical binding and requires
explicit root-issued custody state selected by trusted private configuration.
`RootReleaseStore` does not create production releases. Its absent/default state
denies readiness. A release is a hash-pinned, private regular JSON file in a
private directory. It binds owner reference, frontend, saved thread, exact model,
device fingerprint, explicit possible same-thread retirement, time window,
release identity and at most one operation. Hashing an invented manifest is not
a grant: its publisher/selection remains the runtime custodian's responsibility.

The verifier exclusively fsyncs a release-consumption record containing the
complete canonical binding, intent digest and actual request-evidence digest.
It rechecks current release and control state, then returns the exact canonical
`VerifiedPhoneOperation` with fresh challenge and a monotonic validity deadline
bounded by startup, request and release expiry. No default runtime release or
production release-creation CLI is supplied. Failed/partial consumption stays
spent; it is never removed or retried automatically.

Canonical `NativeAttempt.submit(phone_authority=...,
authority_verifier=...)` independently validates the persisted raw intent/ledger,
exact thread/model/both nonce IDs/SDP and proof. It consumes its own exclusive
call-UUID receipt before one native submission. UUID spelling is preserved for
correlation; replay receipt filenames normalize UUID case. No legacy retirement
reference, fabricated empty observation or raw-start bypass is used.

At the actual RPC dispatch boundary, the coordinator rechecks the complete native
parameters, current root release and actual request/control authority. Observed
revocation or disconnect before dispatch refuses submission. Native work already
dispatched follows canonical owned cleanup. A concurrent revocation cannot recall
an already dispatched operation; physical connectivity and device behavior still
require runtime qualification.

Pin both canonical `voice_ownership.py` and `phone_operation_authority.py`, plus
built-in coordinator/trusted-registry sources. The real configuration stays
`enabled=false`; no private source pins or custody releases are refreshed by
source integration or tests. No provider fallback, retry or public exposure.

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
