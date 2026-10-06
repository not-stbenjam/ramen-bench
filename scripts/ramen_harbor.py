#!/usr/bin/env python3
"""Export immutable legacy artifacts to Harbor, rescore locally, or collect real Harbor grades."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "harbor" / "ramen"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TASK / "tests"))
from grade import (
    AGGREGATION,
    CRITERIA,
    DEFAULT_JUDGE,
    RUBRIC,
    StageError,
    configured_efforts,
    configured_models,
    digest,
    grade,
    summarize_ensemble,
    summarize_judge,
    verifier_hash,
    write_json,
)

from scripts.censor_transcripts import censor_value


def registered_runs(root: Path = ROOT) -> list[tuple[dict, Path]]:
    registry = json.loads((root / "registry.json").read_text())
    runs = []
    for name in registry["runs"]:
        manifest = root / name
        run = json.loads(manifest.read_text())
        artifact = (manifest.parent / run["artifacts"]["result"]).resolve()
        if not artifact.is_relative_to(root.resolve()):
            raise ValueError("Artifact is outside the repository")
        runs.append((run, artifact))
    return runs


def task_name(run_id: str) -> str:
    return "ramen--" + run_id.replace("/", "--")


def export(
    destination: Path, runs: list[tuple[dict, Path]], verifier_image: str | None = None
) -> None:
    if (
        destination.exists()
        and not (destination / "replay-map.json").is_file()
        and any(destination.iterdir())
    ):
        raise ValueError("Export destination must be empty or a previous ramen export")
    destination.mkdir(parents=True, exist_ok=True)
    mapping = {}
    for run, artifact in runs:
        name = task_name(run["id"])
        if name in mapping:
            raise ValueError("Duplicate replay task name")
        target = destination / name
        shutil.copytree(
            TASK,
            target,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        if verifier_image:
            config = target / "task.toml"
            config.write_text(
                config.read_text().replace(
                    "[verifier.environment]\n",
                    "[verifier.environment]\ndocker_image = "
                    + json.dumps(verifier_image)
                    + "\n",
                )
            )
        solution = target / "solution"
        solution.mkdir(exist_ok=True)
        shutil.copyfile(artifact, solution / "index.html")
        (solution / "solve.sh").write_text(
            "#!/bin/bash\nset -euo pipefail\ncp /solution/index.html /app/index.html\n"
        )
        mapping[name] = {
            "runId": run["id"],
            "artifactSha256": digest(artifact.read_bytes()),
        }
    write_json(destination / "replay-map.json", {"schemaVersion": 1, "tasks": mapping})
    print(
        f"Exported {len(mapping)} byte-for-byte artifact replay tasks to {destination}"
    )


def validate_grade(result: dict, artifact: Path) -> None:
    if result.get("status") != "scored":
        raise ValueError("Grade is incomplete or failed")
    if (
        result.get("schemaVersion") != 1
        or result.get("rubricVersion") != RUBRIC["version"]
    ):
        raise ValueError("Unsupported grade schema or rubric")
    if result.get("verifierSha256") != verifier_hash():
        raise ValueError("Stale verifier fingerprint; regrade before collecting")
    if result.get("artifactSha256") != digest(artifact.read_bytes()):
        raise ValueError("Grade does not match the immutable artifact")
    judge_config = result["judge"]
    models = judge_config.get("models")
    if (
        not isinstance(models, list)
        or not models
        or any(not isinstance(model, str) or not model for model in models)
        or len(models) != len(set(models))
        or type(judge_config.get("samples")) is not int
        or judge_config["samples"] < 1
    ):
        raise ValueError("Invalid judge configuration")
    efforts = configured_efforts(judge_config.get("efforts"), models)
    if judge_config.get("efforts") != efforts:
        raise ValueError("Judge efforts must be explicitly recorded")
    scores = result["scores"]
    if any(
        type(scores.get(key)) not in (int, float) or scores[key] not in (0, 1)
        for key in ("standalone", "procedural")
    ):
        raise ValueError("Compliance gates must be binary")
    if scores["standalone"] == 0 or scores["procedural"] == 0:
        if (
            set(scores) != {"standalone", "procedural"}
            or (scores["standalone"] == 0 and not result.get("standaloneViolations"))
            or (scores["procedural"] == 0 and not result.get("complianceViolations"))
        ):
            raise ValueError(
                "Gate failure requires evidence and no invented visual scores"
            )
        expected = 0.0
    else:
        if set(scores) != {"standalone", "procedural", *CRITERIA}:
            raise ValueError("Unexpected visual criterion set")
        recorded_judges = result["judgeResults"]
        if [item["model"] for item in recorded_judges] != models:
            raise ValueError("Incomplete or mismatched judge ensemble")
        reconstructed = []
        for item in recorded_judges:
            if len(item["judgments"]) != judge_config["samples"]:
                raise ValueError("Judge sample count mismatch")
            verified = summarize_judge(
                item["model"], item["judgments"], efforts[item["model"]]
            )
            if item != verified:
                raise ValueError("Per-judge scores do not match recorded samples")
            reconstructed.append(verified)
        ensemble = summarize_ensemble(reconstructed)
        if result.get("diagnostics") != ensemble["diagnostics"]:
            raise ValueError("Compliance diagnostic does not match recorded judgments")
        if result.get("rewardStdDev") != ensemble["rewardStdDev"]:
            raise ValueError("Judge disagreement does not match recorded samples")
        for name, value in ensemble["scores"].items():
            if not math.isclose(scores.get(name, float("nan")), value, abs_tol=1e-12):
                raise ValueError("Criterion does not match equal-weight judge means")
        expected = ensemble["reward"]
    if type(result.get("reward")) not in (int, float) or not math.isclose(
        result["reward"], expected, abs_tol=1e-12
    ):
        raise ValueError("Composite reward mismatch")


def publish(results: list[tuple[dict, dict]], destination: Path) -> None:
    evaluations = {json.dumps(result["judge"], sort_keys=True) for _, result in results}
    if len(evaluations) > 1:
        raise ValueError(
            "Do not combine different judges or sample counts in one ranking"
        )
    rows = []
    for run, result in results:
        row = {
            key: result[key]
            for key in ("artifactSha256", "gradedAt", "scores", "reward")
        }
        row.update(
            runId=run["id"],
            rewardStdDev=result.get("rewardStdDev"),
            judgeResults=result.get("judgeResults", []),
            standaloneViolations=result.get("standaloneViolations", []),
            complianceViolations=result.get("complianceViolations", []),
            diagnostics=result.get("diagnostics", {}),
            performance=result.get("rendering", {}).get("measurements", {}),
        )
        rows.append(row)
    evaluation = (
        None
        if not results
        else {
            "rubricVersion": RUBRIC["version"],
            "verifierSha256": verifier_hash(),
            "judge": results[0][1]["judge"],
            "criteria": list(CRITERIA),
            "weights": RUBRIC["weights"],
            "aggregation": AGGREGATION,
        }
    )
    # Public output contains no images, provider responses, private paths, or raw session content.
    write_json(
        destination,
        censor_value({"schemaVersion": 1, "evaluation": evaluation, "results": rows}),
    )
    print(f"Published {len(rows)} scored artifacts to {destination}")


def score(runs: list[tuple[dict, Path]], args) -> int:
    completed, failures = [], 0
    for index, (run, artifact) in enumerate(runs, 1):
        output = args.work / task_name(run["id"])
        cached = output / "grade.json"
        try:
            result = None
            if cached.is_file() and not args.force:
                candidate = json.loads(cached.read_text())
                try:
                    validate_grade(candidate, artifact)
                    if candidate["judge"] == {
                        "models": configured_models(args.judge_model),
                        "samples": args.samples,
                        "efforts": configured_efforts(
                            args.judge_efforts, configured_models(args.judge_model)
                        ),
                    }:
                        result = candidate
                except (KeyError, TypeError, ValueError):
                    pass
            if result is None:
                result = grade(
                    artifact,
                    output,
                    args.judge_model,
                    args.samples,
                    capture_only=args.capture_only,
                    efforts=args.judge_efforts,
                )
            if result["status"] == "scored":
                validate_grade(result, artifact)
                completed.append((run, result))
            print(f"[{index}/{len(runs)}] {run['id']}: {result['status']}", flush=True)
        except Exception as error:  # noqa: BLE001 -- isolate provider/browser failures per artifact
            failures += 1
            # Fixed stage/codes are actionable without exposing provider text.
            diagnostic = (
                f"{error.stage}: {error.code}"
                + (f", HTTP {error.status_code}" if error.status_code else "")
                if isinstance(error, StageError)
                else type(error).__name__
            )
            print(
                f"[{index}/{len(runs)}] {run['id']}: failed ({diagnostic})",
                file=sys.stderr,
            )
    if not args.capture_only:
        publish(completed, args.output)
    return 1 if failures else 0


def collect(
    runs: list[tuple[dict, Path]], jobs: list[Path], mapping_path: Path, output: Path
) -> None:
    mapping = json.loads(mapping_path.read_text())["tasks"]
    by_id = {run["id"]: (run, artifact) for run, artifact in runs}
    results, seen = [], set()
    for trial in sorted(trial for job in jobs for trial in job.iterdir()):
        result_path = trial / "result.json"
        grade_path = trial / "verifier" / "grade.json"
        if not result_path.is_file():
            continue
        trial_result = json.loads(result_path.read_text())
        name = trial_result.get("task_name")
        if name not in mapping:
            continue
        if trial_result.get("exception_info") or not grade_path.is_file():
            print(f"Skipping unsuccessful trial: {trial.name}", file=sys.stderr)
            continue
        run_id = mapping[name]["runId"]
        if run_id not in by_id:
            raise ValueError("Replay map contains an unregistered run")
        if run_id in seen:
            raise ValueError(
                "Multiple trials per artifact: select a single job/attempt before collecting"
            )
        run, artifact = by_id[run_id]
        result = json.loads(grade_path.read_text())
        if mapping[name]["artifactSha256"] != result["artifactSha256"]:
            raise ValueError("Replay grade does not match its export")
        validate_grade(result, artifact)
        recorded = (trial_result.get("verifier_result") or {}).get("rewards")
        if recorded != {"reward": result["reward"], **result["scores"]}:
            raise ValueError("Harbor rewards do not match the recorded grade")
        results.append((run, result))
        seen.add(run_id)
    if not results:
        raise ValueError(
            "No successful ramen trials found; public scores were not changed"
        )
    publish(results, output)


def audit(path: Path, runs: list[tuple[dict, Path]]) -> None:
    from jsonschema import Draft202012Validator, FormatChecker

    data = json.loads(path.read_text())
    schema = json.loads((ROOT / "schemas" / "scores.schema.json").read_text())
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(data)
    if censor_value(data) != data:
        raise ValueError("Public scores need shared censoring")
    by_id = {run["id"]: artifact for run, artifact in runs}
    seen = set()
    for row in data["results"]:
        if row["runId"] not in by_id or row["runId"] in seen:
            raise ValueError("Unknown or duplicate scored run")
        seen.add(row["runId"])
        result = {
            **row,
            "schemaVersion": 1,
            "status": "scored",
            **{
                key: data["evaluation"][key]
                for key in ("rubricVersion", "verifierSha256", "judge")
            },
        }
        validate_grade(result, by_id[row["runId"]])
    print(
        f"Validated {len(seen)} public scores, artifact hashes, rubric and censor state"
    )


def audit_display_config(runs: list[tuple[dict, Path]]) -> None:
    from jsonschema import Draft202012Validator

    data = {}
    for name in ("human-scores", "weights"):
        value = json.loads((ROOT / "harbor" / f"{name}.json").read_text())
        schema = json.loads((ROOT / "schemas" / f"{name}.schema.json").read_text())
        Draft202012Validator(schema).validate(value)
        if censor_value(value) != value:
            raise ValueError(f"Public {name} needs shared censoring")
        data[name] = value
    weights = data["weights"]["weights"]
    if any(not math.isfinite(value) for value in weights.values()) or not math.isclose(
        sum(weights.values()), 1, abs_tol=1e-10
    ):
        raise ValueError("Display weights must be finite and sum to one")
    panel = data["human-scores"]
    known = {run["id"] for run, _ in runs}
    seen = set()
    if not math.isfinite(panel["weight"]):
        raise ValueError("Human panel weight must be finite")
    for rating in panel["ratings"]:
        if rating["runId"] not in known or rating["runId"] in seen:
            raise ValueError("Unknown or duplicate human rating")
        if rating["humanScore"] is not None and not math.isfinite(rating["humanScore"]):
            raise ValueError("Human scores must be finite or null")
        seen.add(rating["runId"])
    print(f"Validated display weights and {len(seen)} editable human rating slots")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    exporter = commands.add_parser("export", help="Create Harbor oracle replay tasks")
    exporter.add_argument("--output", type=Path, default=ROOT / ".harbor" / "replay")
    exporter.add_argument(
        "--verifier-image", help="Reuse an image built from harbor/ramen/tests"
    )
    exporter.add_argument(
        "--run-id", action="append", help="Select registered runs (repeatable)"
    )
    scorer = commands.add_parser(
        "score", help="Grade saved artifacts with the same Harbor verifier"
    )
    scorer.add_argument("--work", type=Path, default=ROOT / ".harbor" / "grades")
    scorer.add_argument("--output", type=Path, default=ROOT / "scores.json")
    scorer.add_argument("--judge-model")
    scorer.add_argument(
        "--judge-efforts",
        help="JSON map of judge model to effort; default omits the API effort setting",
    )
    scorer.add_argument("--samples", type=int)
    scorer.add_argument("--env-file", type=Path, default=ROOT / ".harbor" / "judge.env")
    scorer.add_argument("--capture-only", action="store_true")
    scorer.add_argument("--force", action="store_true")
    scorer.add_argument(
        "--run-id", action="append", help="Select registered runs (repeatable)"
    )
    collector = commands.add_parser(
        "collect", help="Collect actual Harbor replay/regrade results"
    )
    collector.add_argument("job", type=Path, nargs="+")
    collector.add_argument(
        "--map", type=Path, default=ROOT / ".harbor" / "replay" / "replay-map.json"
    )
    collector.add_argument("--output", type=Path, default=ROOT / "scores.json")
    regrader = commands.add_parser(
        "regrade", help="Native Harbor regrade with local judge credentials"
    )
    regrader.add_argument("job", type=Path)
    regrader.add_argument("--tasks", type=Path, default=ROOT / ".harbor" / "replay")
    regrader.add_argument("--jobs-dir", type=Path, default=ROOT / ".harbor" / "jobs")
    regrader.add_argument("--job-name", required=True)
    regrader.add_argument("--concurrent", type=int, default=2)
    regrader.add_argument(
        "--env-file", type=Path, default=ROOT / ".harbor" / "judge.env"
    )
    auditor = commands.add_parser("audit")
    auditor.add_argument("--scores", type=Path, default=ROOT / "scores.json")
    args = parser.parse_args()
    runs = registered_runs()
    if args.command == "export":
        if args.run_id:
            selected = set(args.run_id)
            runs = [(run, artifact) for run, artifact in runs if run["id"] in selected]
            if {run["id"] for run, _ in runs} != selected:
                parser.error("Unknown --run-id")
        export(args.output, runs, args.verifier_image)
    elif args.command == "score":
        if args.env_file.is_file():
            from dotenv import load_dotenv

            load_dotenv(args.env_file, override=False)
        args.judge_model = (
            args.judge_model
            or os.environ.get("RAMEN_JUDGE_MODELS")
            or os.environ.get("RAMEN_JUDGE_MODEL", DEFAULT_JUDGE)
        )
        args.samples = (
            args.samples
            if args.samples is not None
            else int(os.environ.get("RAMEN_JUDGE_SAMPLES", "3"))
        )
        args.judge_efforts = args.judge_efforts or os.environ.get("RAMEN_JUDGE_EFFORTS")
        if args.run_id:
            selected = set(args.run_id)
            runs = [(run, artifact) for run, artifact in runs if run["id"] in selected]
            if {run["id"] for run, _ in runs} != selected:
                parser.error("Unknown --run-id")
        if args.samples < 1:
            parser.error("--samples must be positive")
        if not args.capture_only and not args.judge_model:
            parser.error(
                "Set RAMEN_JUDGE_MODEL or --judge-model (vision-capable provider/model)"
            )
        return score(runs, args)
    elif args.command == "collect":
        collect(runs, args.job, args.map, args.output)
    elif args.command == "regrade":
        from dotenv import dotenv_values

        environment = {
            **{
                key: value
                for key, value in dotenv_values(args.env_file).items()
                if value is not None
            },
            **os.environ,
        }
        executable = ROOT / ".harbor" / "venv" / "bin" / "harbor"
        return subprocess.run(
            [
                str(executable),
                "job",
                "regrade",
                str(args.job.resolve()),
                "-p",
                str(args.tasks.resolve()),
                "-n",
                str(args.concurrent),
                "--jobs-dir",
                str(args.jobs_dir.resolve()),
                "--job-name",
                args.job_name,
            ],
            env=environment,
            check=False,
        ).returncode
    else:
        audit(args.scores, runs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
