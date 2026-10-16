#!/bin/bash
set -euo pipefail
python /tests/vegan_review.py --input /app/input.json --output /logs/verifier
