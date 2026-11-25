# HomeVoice

A native SwiftUI iPhone app for a private conversation with an existing home
Codex agent. The phone negotiates native WebRTC audio directly with OpenAI.
The home computer handles managed subscription authentication, SDP signaling,
and the backing agent's native handoffs and spoken return path. Telegram is
not in the app's communication path.

This uses the installed native Codex experimental WebRTC V3 surface and the
exact `gpt-live-1.1-codex` frontend. It does not implement the separate metered
public Live API, export account OAuth tokens, or substitute Realtime models.
The native interface may change; qualified shared source hashes are required.

## What is implemented

- Hebrew interface, home connectivity status, call start/end, microphone mute,
  transcript display, and separate home-agent work status.
- Microphone permission only after an explicit Start action; five-minute call
  limit; close on backgrounding/control loss. No automatic redial.
- Device pairing secret in iOS Keychain; HTTPS to the home endpoint (loopback
  HTTP is accepted only for local development). No provider credential on phone.
- An authenticated home SDP/control broker that receives no audio. It cannot
  accept arbitrary agent IDs, commands, model choices or provider URLs from the
  phone. Failed or uncertain admission is not retried automatically.
- One fixed Hebrew speak-first greeting after the app reports remote SDP applied
  and its WebRTC peer connected. No microphone input/unmute is required to trigger
  speech. A separate server-owned active-call capability and explicit root speech
  scope guard this one operation; End/disconnect revoke pending speech. A speech
  RPC acknowledgment does not prove generated audio or owner hearing.
- A native adapter that imports the existing canonical `ProjectSlot` and
  `NativeAttempt` lifecycle in place, instead of creating competing ownership.
  Native Codex owns handoffs; the phone never dispatches a duplicate backend task.

## Readiness and remaining integration

The application is not a claim of a completed phone call. Native admission
and real phone audio must be qualified separately from compilation, simulator
rendering and offline HTTP tests.

The broker is **disabled by default**. Its built-in coordinator creates no
runtime authority. Pairing middleware mints an opaque in-process request
capability after authentication; it binds the exact call, SDP and authenticated
control connection. A disconnected control channel cannot authorize a start.
The phone includes its server-issued `controlID` with the offer.

Admission also requires explicit current root-issued private custody state,
whose file and SHA-256 must be selected by the existing runtime custodian.
Missing, disabled, expired, changed or consumed release state denies admission.
One release authorizes at most one attempt. This repository contains no production
release. Configuring matching strings does not prove an owner grant or physical tap.

The coordinator implements the reviewed canonical `RootPhoneAuthorityVerifier`
interface and uses `NativeAttempt.submit(phone_authority=...,
authority_verifier=...)`. Both canonical modules and the built-in coordinator
and request registry require reviewed source hashes. The phone cannot supply
a verifier. No old probe retirement reference or empty-slot observation is used.
The shared runtime custodian still must review and release deployment. Private
home HTTPS reachability, pairing and physical iPhone installation/audio remain
unverified.

## Local development

```sh
python3 -m venv .venv-homevoice
.venv-homevoice/bin/pip install -r homevoice_broker/requirements.txt
.venv-homevoice/bin/python -m unittest discover -s tests -v
```

Tests use actual loopback HTTP/WebSocket connections with an explicit native
service fixture. They do not call Codex, OpenAI, a microphone, or a shared slot.

For a reviewed deployment, create a private config based on
`homevoice_broker/config.example.json`, a separate high-entropy device-pairing secret
file (0600), and the root-reviewed source pins/custody release. Keep actual identities, keys and
config outside Git. Start the broker manually:

```sh
.venv-homevoice/bin/python -m homevoice_broker.server --config /private/homevoice-config.json
```

The CLI binds only to `127.0.0.1`; it changes no firewall or startup service.
The phone needs an already authorized private HTTPS route to this listener.
No public tunnel/port, certificate trust bypass or routing deployment is enabled
by these instructions. `--cert` and `--key` can configure TLS on the local
listener when a trusted certificate is available.

## Build evidence

The HomeVoice Actions workflow builds the device IPA and simulator app on a
standard macOS runner. Its simulator preview uses `--demo`: it shows sample
captions and is explicitly labeled as a preview, with no microphone or network.
The unsigned IPA requires a separate signing/install route. Artifacts expire
after one day; retain needed outputs locally.

## Dependency and protocol sources

WebRTC is pinned to [stasel/WebRTC 158.0.0](https://github.com/stasel/WebRTC/tree/158.0.0),
whose manifest pins the binary archive checksum. This is a community distribution
of Google's WebRTC framework; retain its bundled license notices when distributing.

Native integration follows the matching installed Codex source:
[realtime lifecycle](https://github.com/openai/codex/blob/rust-v0.172.0/codex-rs/core/src/realtime_conversation.rs),
[start schema](https://github.com/openai/codex/blob/rust-v0.172.0/codex-rs/app-server-protocol/src/protocol/v2/realtime.rs).
The public [Live WebRTC guide](https://developers.openai.com/api/docs/guides/voice-webrtc)
explains direct media versus server signaling, but its API-key/session creation
contract is a different transport from the managed native route implemented here.


## Shared admission review contract

See [the concrete phone-intent proposal](homevoice_broker/ADMISSION_CONTRACT.md).
This integrates the existing reviewed canonical phone API as source only. It
does not release the existing native session slot.

Optional tests against the existing canonical lifecycle source:

```sh
HOMEVOICE_SHARED_SOURCE=/private/path/existing-agent-voice \
  .venv-homevoice/bin/python -W error::RuntimeWarning -m unittest discover -s tests -v
```

These use the actual ManagedNativeHost and canonical locking/lifecycle methods,
but an explicit temporary slot and fake RPC. They acquire no production lock
and make no native/provider calls. CI without the external canonical source
reports those cases skipped. A skipped integration case is not runtime evidence.

The device IPA check reads actual Mach-O dependencies/runpaths and the embedded
framework. Successful compilation alone cannot establish that a dynamic
framework can load. Simulator preview remains different from physical-phone
microphone, playback, interruption, network and backend execution qualification.
