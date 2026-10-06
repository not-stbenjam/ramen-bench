#!/bin/bash
set -euo pipefail
mkdir -p /logs/verifier
# A failed judge must never leave a stale reward behind.
rm -f /logs/verifier/reward.json /logs/verifier/reward.txt
python /tests/grade.py --artifact /app/index.html --output /logs/verifier
