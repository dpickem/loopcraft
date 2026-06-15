#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

CONFIG_PATH="${LOOPCRAFT_X_INTEL_CONFIG:-config/x_intel.json}"
if [[ ! -f "$CONFIG_PATH" ]]; then
  CONFIG_PATH="config/x_intel.example.json"
fi

PYTHONPATH="${PYTHONPATH:-src}" python -m loopcraft.x_intel.cli run --config "$CONFIG_PATH"
