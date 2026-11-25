# TinySnake

A minimal native iPhone Snake game in SwiftUI. Swipe or use the arrow buttons;
press Play/Pause, eat orange food, and avoid the walls and your tail. Requires iOS 16+.
No network access, accounts, analytics, ads, or external app dependencies.

## Build without a local Mac

GitHub Actions uses the standard `macos-15` hosted runner and its installed Xcode.
On each push to `main` (or manual dispatch), it:

1. Compiles and runs game logic checks.
2. Builds the iPhone Simulator app, installs and launches it, and captures a screenshot.
3. Compiles the native arm64 iPhone app with signing disabled.
4. Uploads `TinySnake-unsigned.ipa`, SHA-256 checksum, build logs and simulator evidence for one day.

The Actions run records its actual Xcode/SDK versions. Native compilation happens
on the macOS runner. No Apple signing credentials are used by the workflow.

**The unsigned IPA cannot be installed directly on an iPhone.** It must be signed
and provisioned for the device using a legitimate Apple account route first.
A simulator launch proves simulator operation only, never installation on a physical phone.

[GitHub documents standard public-repository runners as free](https://docs.github.com/en/billing/concepts/product-billing/github-actions).
Larger runners and storage have separate billing rules; this workflow uses a
standard runner, no cache, and one-day artifact retention.

## Local build on a Mac (optional)

Open `TinySnake.xcodeproj` with Xcode, select TinySnake, and choose a simulator.
For a physical device, configure your own signing team locally. Never commit
credentials, provisioning profiles or device identifiers.

## License

MIT. See [LICENSE](LICENSE).
