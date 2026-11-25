#!/bin/bash
set -euo pipefail
simulator_id=$(xcrun simctl list devices available -j | python3 -c 'import json,sys; d=json.load(sys.stdin); print(next(v["udid"] for values in d["devices"].values() for v in values if "iPhone" in v["name"]))')
xcrun simctl boot "$simulator_id"
xcrun simctl bootstatus "$simulator_id" -b
diagnose() {
  smoke_exit_code=$?
  trap - ERR
  xcrun simctl spawn "$simulator_id" log show --last 3m --style compact --predicate 'process == "HomeVoice" OR eventMessage CONTAINS "HomeVoice"' > build/homevoice-simulator-diagnostics.log 2>&1 || true
  exit "$smoke_exit_code"
}
trap diagnose ERR
xcrun simctl install "$simulator_id" build/homevoice-simulator/Build/Products/Debug-iphonesimulator/HomeVoice.app
xcrun simctl launch "$simulator_id" org.homevoicedemo.HomeVoice --demo | tee build/homevoice-launch.txt
sleep 3
xcrun simctl io "$simulator_id" screenshot build/homevoice-preview.png
