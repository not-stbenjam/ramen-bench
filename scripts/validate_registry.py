#!/usr/bin/env python3
"""Validate every registered benchmark manifest, artifact, and transcript."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from censor_transcripts import main as audit_censoring


ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDER_TRANSCRIPT = "Created the requested single-file ramen scene in index.html."


def validate(instance: Any, schema_path: Path, label: str) -> None:
    schema = json.loads(schema_path.read_text())
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(instance), key=lambda error: list(error.path))
    if errors:
        details = "; ".join(
            f"{'/'.join(map(str, error.path)) or '<root>'}: {error.message}"
            for error in errors
        )
        raise ValueError(f"{label}: {details}")


def main() -> int:
    registry_path = ROOT / "registry.json"
    registry = json.loads(registry_path.read_text())
    validate(registry, ROOT / "schemas" / "registry.schema.json", "registry.json")
    manifests = registry["runs"]
    if len(manifests) != len(set(manifests)):
        raise ValueError("registry.json contains duplicate manifest paths")

    run_count = 0
    event_count = 0
    for relative_manifest in manifests:
        manifest_path = ROOT / relative_manifest
        if not manifest_path.is_file():
            raise FileNotFoundError(manifest_path)
        run = json.loads(manifest_path.read_text())
        validate(
            run,
            ROOT / "schemas" / "run.schema.json",
            str(manifest_path.relative_to(ROOT)),
        )
        if relative_manifest != f"{run['id']}/run.json":
            raise ValueError(f"{relative_manifest}: run id/path mismatch")

        result_path = manifest_path.parent / run["artifacts"]["result"]
        transcript_path = manifest_path.parent / run["artifacts"]["transcript"]
        for artifact_path in (result_path, transcript_path):
            if not artifact_path.is_file():
                raise FileNotFoundError(artifact_path)
        raw_path = run["artifacts"].get("rawTranscript")
        if raw_path and not (manifest_path.parent / raw_path).is_file():
            raise FileNotFoundError(manifest_path.parent / raw_path)

        transcript_text = transcript_path.read_text()
        if PLACEHOLDER_TRANSCRIPT in transcript_text:
            raise ValueError(f"{transcript_path}: fabricated placeholder transcript remains")
        transcript = json.loads(transcript_text)
        validate(
            transcript,
            ROOT / "schemas" / "transcript.schema.json",
            str(transcript_path.relative_to(ROOT)),
        )
        if transcript["runId"] != run["id"]:
            raise ValueError(f"{transcript_path}: transcript/run id mismatch")

        result_stats = run.get("resultStats", {})
        if "bytes" in result_stats and result_stats["bytes"] != result_path.stat().st_size:
            raise ValueError(f"{manifest_path}: result byte count is stale")
        if "lines" in result_stats:
            line_count = len(result_path.read_text(errors="replace").splitlines())
            if result_stats["lines"] != line_count:
                raise ValueError(f"{manifest_path}: result line count is stale")

        run_count += 1
        event_count += len(transcript["events"])

    if audit_censoring(
        [
            str(ROOT / name)
            for name in ("anthropic", "openai", "google", "meta", "z.ai")
        ]
    ):
        raise ValueError("one or more public JSON files need censoring")
    print(f"Validated {run_count} registered runs and {event_count} public transcript events.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
