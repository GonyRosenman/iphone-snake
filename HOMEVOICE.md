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
- A native adapter that imports the existing canonical `ProjectSlot` and
  `NativeAttempt` lifecycle in place, instead of creating competing ownership.
  Native Codex owns handoffs; the phone never dispatches a duplicate backend task.

## Readiness and remaining integration

The application is not a claim of a completed phone call. Native admission
and real phone audio must be qualified separately from compilation, simulator
rendering and offline HTTP tests.

The broker is **disabled by default**. A reviewed local coordinator must supply
`readiness(config)` and async `submit_phone_offer(attempt, rpc, params,
startup_deadline)` before native admission can run. The coordinator owns the
fresh shared-slot admission decision. This code never borrows an old probe's
retirement grant, calls `thread/read` an empty-slot oracle, or starts another
backing thread. The existing runtime custodian must review and release this
integration. Private home HTTPS reachability, pairing and physical iPhone
installation/audio are additional unverified steps.

## Local development

```sh
python3 -m venv .venv-homevoice
.venv-homevoice/bin/pip install -r homevoice/requirements.txt
.venv-homevoice/bin/python -m unittest discover -s tests -v
```

Tests use actual loopback HTTP/WebSocket connections with an explicit native
service fixture. They do not call Codex, OpenAI, a microphone, or a shared slot.

For a reviewed deployment, create a private config based on
`homevoice/config.example.json`, a separate high-entropy device-pairing secret
file (0600), and the reviewed coordinator. Keep actual identities, keys and
config outside Git. Start the broker manually:

```sh
.venv-homevoice/bin/python -m homevoice.server --config /private/homevoice-config.json
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
