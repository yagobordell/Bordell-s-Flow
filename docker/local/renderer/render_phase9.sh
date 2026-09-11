#!/usr/bin/env bash
set -Eeuo pipefail

python scripts/run_phase9_compositor.py
python scripts/run_phase9_motion.py
python scripts/run_phase9_final.py

printf '%s\n' "Local Phase 9 render pipeline: OK"
