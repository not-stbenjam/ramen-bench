#!/usr/bin/env python3
"""Export and collect native Harbor reviews of image-only vegan failures."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY_DIR = ROOT / "harbor/trajectory-review"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(POLICY_DIR))
import vegan_review as policy

from scripts.censor_transcripts import censor_value


def sources(root: Path = ROOT) -> tuple[dict, dict, dict]:
    registry = json.loads((root / "registry.json").read_text())
    runs = {}
    for name in registry["runs"]:
        run = json.loads((root / name).read_text())
        runs[run["id"]] = run
    scores = json.loads((root / "scores.json").read_text())
    return (
        runs,
        {r["runId"]: r for r in scores["results"]},
        scores["evaluation"]["judge"],
    )


def bundle_for(run_id: str, root: Path = ROOT) -> dict:
    runs, scores, judge = sources(root)
    run = runs[run_id]
    directory = root / run_id
    return policy.build_input(
        run,
        (directory / run["artifacts"]["result"]).read_bytes(),
        (directory / run["artifacts"]["transcript"]).read_bytes(),
        scores[run_id],
        judge,
    )


def validate_review(review: dict, bundle: dict) -> None:
    for key in (
        "schemaVersion",
        "runId",
        "artifactSha256",
        "transcriptSha256",
        "sourceScoreSha256",
        "sourceReward",
        "policySha256",
        "policyVersion",
        "judge",
        "omittedEvidenceChunks",
    ):
        if review[key] != bundle[key]:
            raise ValueError(f"Stale trajectory review: {key}")
    if review["inputSha256"] != policy.digest(policy.canonical(bundle)):
        raise ValueError("Trajectory-review input changed")
    if datetime.fromisoformat(review["reviewedAt"]).tzinfo is None:
        raise ValueError("Review timestamp needs a timezone")
    restored = policy.summarize(review["judgeResults"], bundle["judge"], bundle)
    if review["evidenceSources"] != policy.citation_sources(
        bundle, review["judgeResults"]
    ):
        raise ValueError("Trajectory-review citation locations differ")
    if (
        type(review["restoreVeganGate"]) is not bool
        or review["restoreVeganGate"] != restored
    ):
        raise ValueError("Trajectory-review consensus mismatch")
    for judge in review["judgeResults"]:
        expected = policy.digest(
            policy.canonical(policy.request(bundle, judge["model"], judge["effort"]))
        )
        if any(sample["requestSha256"] != expected for sample in judge["judgments"]):
            raise ValueError("Trajectory-review sample request differs")
        for sample in judge["judgments"]:
            attempts = sample["invalidAttempts"]
            if (
                not isinstance(attempts, list)
                or len(attempts) > 2
                or any(
                    attempt["requestSha256"] != expected
                    or attempt["error"] != "invalid_format_or_citation"
                    for attempt in attempts
                )
            ):
                raise ValueError("Invalid trajectory-review retry provenance")


def audit_reviews(root: Path = ROOT) -> None:
    from jsonschema import Draft202012Validator, FormatChecker

    path = root / "harbor/vegan-reviews.json"
    document = json.loads(path.read_text())
    schema = json.loads((root / "schemas/vegan-reviews.schema.json").read_text())
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)
    if censor_value(document, workspace=root) != document:
        raise ValueError("Trajectory reviews need shared public censoring")
    seen = set()
    for review in document["reviews"]:
        if review["runId"] in seen:
            raise ValueError("Duplicate trajectory review")
        seen.add(review["runId"])
        validate_review(review, bundle_for(review["runId"], root))
    print(f"Validated {len(seen)} source-bound native trajectory reviews")


def export(destination: Path, run_ids: list[str]) -> None:
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("Trajectory-review export destination must be empty")
    destination.mkdir(parents=True, exist_ok=True)
    reviewed = {
        item["runId"]
        for item in json.loads((ROOT / "harbor/vegan-reviews.json").read_text())[
            "reviews"
        ]
    }
    if not run_ids:
        _, scores, _ = sources()
        run_ids = [
            r["runId"]
            for r in scores.values()
            if r.get("diagnostics", {}).get("veganFailed")
            and r["runId"] not in reviewed
        ]
    if not run_ids or any(run_id in reviewed for run_id in run_ids):
        raise ValueError("Export requires unreviewed vegan-gated runs")
    mapping = {}
    for run_id in run_ids:
        bundle = bundle_for(run_id)
        if censor_value(bundle, workspace=ROOT) != bundle:
            raise ValueError("Review input needs public censoring")
        name = "vegan--" + run_id.replace("/", "--")
        if name in mapping or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise ValueError("Invalid or duplicate review task")
        target = destination / name
        shutil.copytree(
            ROOT / "harbor/ramen", target, ignore=shutil.ignore_patterns("__pycache__")
        )
        config = target / "task.toml"
        text = config.read_text().replace(
            'artifacts = ["/app/index.html"]', 'artifacts = ["/app/input.json"]'
        )
        text = re.sub(r"(\[agent\]\ntimeout_sec = )[^\n]+", r"\g<1>60.0", text)
        config.write_text(text)
        (target / "instruction.md").write_text(
            "Copy the supplied review input to /app/input.json. Do not alter it.\n"
        )
        solution = target / "solution"
        solution.mkdir(exist_ok=True)
        policy.write_json(solution / "input.json", bundle)
        (solution / "solve.sh").write_text(
            "#!/bin/bash\nset -euo pipefail\ncp /solution/input.json /app/input.json\n"
        )
        tests = target / "tests"
        shutil.rmtree(tests)
        tests.mkdir()
        for filename in policy.FILES:
            shutil.copyfile(POLICY_DIR / filename, tests / filename)
        mapping[name] = {
            "runId": run_id,
            "inputSha256": policy.digest(policy.canonical(bundle)),
        }
    policy.write_json(
        destination / "review-map.json", {"schemaVersion": 1, "tasks": mapping}
    )
    print(f"Exported {len(mapping)} trajectory reviews for native Harbor oracle trials")


def collect(job: Path, mapping_path: Path) -> None:
    mapping = json.loads(mapping_path.read_text())["tasks"]
    path = ROOT / "harbor/vegan-reviews.json"
    document = json.loads(path.read_text())
    existing = {r["runId"]: r for r in document["reviews"]}
    seen = set()
    for trial in sorted(job.iterdir()):
        if not trial.is_dir() or not (trial / "result.json").exists():
            continue
        result = json.loads((trial / "result.json").read_text())
        task = result["task_name"]
        if task not in mapping or task in seen:
            raise ValueError("Unexpected or duplicate native review trial")
        seen.add(task)
        if result.get("exception_info") or result["agent_info"]["name"] != "oracle":
            raise ValueError("Require a successful native Harbor oracle review")
        bundle = bundle_for(mapping[task]["runId"])
        collected = json.loads((trial / "artifacts/app/input.json").read_text())
        if policy.canonical(collected) != policy.canonical(bundle) or mapping[task][
            "inputSha256"
        ] != policy.digest(policy.canonical(bundle)):
            raise ValueError("Native review input differs from public sources")
        review = json.loads((trial / "verifier/review.json").read_text())
        validate_review(review, bundle)
        if (result.get("verifier_result") or {}).get("rewards") != {
            "restore_vegan_gate": float(review["restoreVeganGate"])
        }:
            raise ValueError(
                "Native Harbor review reward differs from recorded consensus"
            )
        review["harborTrial"] = trial.name
        review = censor_value(review, workspace=ROOT)
        if review["runId"] in existing and existing[review["runId"]] != review:
            raise ValueError("Refusing to overwrite an existing trajectory review")
        existing[review["runId"]] = review
    if seen != set(mapping):
        raise ValueError("All exported native reviews must finish before publishing")
    document["reviews"] = list(existing.values())
    policy.write_json(path, document)
    audit_reviews()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    exporter = commands.add_parser("export")
    exporter.add_argument("--output", type=Path, required=True)
    exporter.add_argument("--run-id", action="append", default=[])
    collector = commands.add_parser("collect")
    collector.add_argument("--job", type=Path, required=True)
    collector.add_argument("--mapping", type=Path, required=True)
    commands.add_parser("audit")
    args = parser.parse_args()
    if args.command == "export":
        export(args.output, args.run_id)
    elif args.command == "collect":
        collect(args.job, args.mapping)
    else:
        audit_reviews()


if __name__ == "__main__":
    main()
