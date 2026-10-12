#!/usr/bin/env python3
"""Regenerate Muse Code metadata, then apply the shared privacy audit."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from censor_transcripts import main as censor_transcripts

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    if any(
        arg == "--harbor-job" or arg.startswith("--harbor-job=") for arg in sys.argv[1:]
    ):
        from import_harbor_muse import main as import_native

        return import_native(sys.argv[1:])
    subprocess.run(
        [
            sys.executable,
            ROOT / "meta" / "muse-spark-1.3" / "build_metadata.py",
            *sys.argv[1:],
        ],
        cwd=ROOT,
        check=True,
    )
    censor_transcripts(["--write", str(ROOT / "meta")])
    return censor_transcripts([str(ROOT / "meta")])


if __name__ == "__main__":
    raise SystemExit(main())
