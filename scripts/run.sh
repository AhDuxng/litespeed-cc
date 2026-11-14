#!/usr/bin/env bash
# Runs the measurement CLI inside the provisioned virtual environment.
#   ./run.sh run                     full campaign as configured in client.env
#   ROUNDS=1 ./run.sh run            one complete block, as a smoke test
#   ./run.sh run --no-dish           off-Starlink test without dish telemetry
#   ./run.sh dish [--raw]            one Starlink status sample
#   ./run.sh summarize results/<id>  per-cell statistics of a campaign
set -euo pipefail

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
exec .venv/bin/python -m bench "$@"
