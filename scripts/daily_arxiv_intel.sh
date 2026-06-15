#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

CONFIG_PATH="${LOOPCRAFT_ARXIV_INTEL_CONFIG:-config/arxiv_intel.json}"
if [[ ! -f "$CONFIG_PATH" ]]; then
  CONFIG_PATH="config/arxiv_intel.example.json"
fi

PYTHONPATH="${PYTHONPATH:-src}" python -m loopcraft.arxiv_intel.cli run --config "$CONFIG_PATH"
