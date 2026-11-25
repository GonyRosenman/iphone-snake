#!/bin/bash
set -euo pipefail
simulator_id=$(xcrun simctl list devices available -j | python3 -c 'import json,sys; d=json.load(sys.stdin); print(next(v["udid"] for values in d["devices"].values() for v in values if "iPhone" in v["name"]))')
xcrun simctl boot "$simulator_id"
xcrun simctl bootstatus "$simulator_id" -b
xcrun simctl install "$simulator_id" build/simulator/Build/Products/Debug-iphonesimulator/TinySnake.app
xcrun simctl launch "$simulator_id" org.tinysnakedemo.TinySnake | tee build/simulator-launch.txt
# Allow the first frame to render before capturing visual evidence.
sleep 3
xcrun simctl io "$simulator_id" screenshot build/simulator.png
