#!/usr/bin/env python3
"""Regenerate Antigravity metadata, then apply the shared privacy audit."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from censor_transcripts import main as censor_transcripts


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    subprocess.run(
        [sys.executable, ROOT / "google" / "import_antigravity_runs.py", *sys.argv[1:]],
        cwd=ROOT,
        check=True,
    )
    if "--check-sources" not in sys.argv[1:]:
        censor_transcripts(["--write", str(ROOT / "google")])
    return censor_transcripts([str(ROOT / "google")])


if __name__ == "__main__":
    raise SystemExit(main())
