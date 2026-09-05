#!/usr/bin/env python3
"""Regenerate Claude Code metadata, then apply the shared privacy audit."""

from __future__ import annotations

import sys
from pathlib import Path

from backfill_session_metadata import main as import_sessions
from censor_transcripts import main as censor_transcripts

ROOT = Path(__file__).resolve().parents[1]
CLAUDE_VENDOR_ROOTS = (ROOT / "anthropic", ROOT / "z.ai")


def main() -> int:
    import_sessions(["--harness", "claude-code", *sys.argv[1:]])
    if "--dry-run" not in sys.argv[1:]:
        censor_transcripts(["--write", *(str(path) for path in CLAUDE_VENDOR_ROOTS)])
    return censor_transcripts([str(path) for path in CLAUDE_VENDOR_ROOTS])


if __name__ == "__main__":
    raise SystemExit(main())
