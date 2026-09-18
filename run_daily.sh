#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
mkdir -p reports
PYTHON="python3"
if [[ -x ".venv/bin/python" ]]; then
	PYTHON=".venv/bin/python"
fi
"$PYTHON" market_report.py --output "reports/$(date +%F)"
