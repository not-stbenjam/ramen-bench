"""Import genuine Codex generations and rewards from completed native Harbor trials."""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from backfill_session_metadata import (
    DISPLAY_NAMES,
    EFFORT_NAMES,
    ROOT,
    final_codex_usage,
    normalize_codex_events,
    read_jsonl,
    run_base,
    transcript_base,
    verify_codex_result,
    write_json,
)
from censor_transcripts import censor_value


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def native_artifact(trial: Path) -> Path:
    records = json.loads((trial / "artifacts/manifest.json").read_text())
    matches = [
        record for record in records if record.get("source") == "/app/index.html"
    ]
    if len(matches) != 1 or matches[0].get("status") != "ok":
        raise ValueError(
            "Harbor did not collect exactly one successful index.html artifact"
        )
    artifact = (trial / matches[0]["destination"]).resolve()
    if (
        not artifact.is_relative_to((trial / "artifacts").resolve())
        or not artifact.is_file()
    ):
        raise ValueError(
            "Harbor artifact is missing or outside the trial artifact directory"
        )
    return artifact


def recorded_rollouts(trial: Path) -> list[tuple[Path, list[dict]]]:
    sessions = []
    for path in sorted((trial / "agent/sessions").rglob("rollout-*.jsonl")):
        rows = read_jsonl(path)
        if rows and rows[0].get("type") == "session_meta":
            # Forked rollouts can contain inherited parent metadata later on.
            sessions.append((path, rows))
    return sessions


def main_rollout(trial: Path, trajectory: dict) -> tuple[Path, list[dict]]:
    sessions = recorded_rollouts(trial)
    stdout = trial / "agent/codex.txt"
    started_ids = set()
    if stdout.is_file():
        for line in stdout.read_text().splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("type") == "thread.started":
                started_ids.add(event.get("thread_id"))
    if len(started_ids) > 1:
        raise ValueError("Codex stdout identifies multiple main threads")
    expected_id = next(iter(started_ids), trajectory["session_id"])
    matches = [
        (path, rows)
        for path, rows in sessions
        if rows[0]["payload"].get("id") == expected_id
        and not rows[0]["payload"].get("forked_from_id")
        and not isinstance(rows[0]["payload"].get("source"), dict)
    ]
    if len(matches) != 1:
        raise ValueError(
            "Harbor trajectory does not identify exactly one recorded main session"
        )
    return matches[0]


def trial_usage(
    trial: Path, result: dict | None, trajectory: dict, rows: list[dict]
) -> dict:
    """Keep native accounting intact when Harbor selected a review subagent."""
    root_id = rows[0]["payload"]["id"]
    if trajectory["session_id"] == root_id:
        return native_usage(result, trajectory, rows)
    sessions = recorded_rollouts(trial)
    by_id = {events[0]["payload"]["id"]: events for _, events in sessions}
    if len(by_id) != len(sessions):
        raise ValueError("Duplicate recorded Codex sessions")
    accounting_rows = by_id.get(trajectory["session_id"])
    if accounting_rows is None:
        raise ValueError("Missing rollout for native Harbor accounting")
    native = native_usage(result, trajectory, accounting_rows)
    if native["cost"] is not None:
        raise ValueError("Subagent-only Harbor cost cannot represent the whole run")
    totals = {}
    for session_id, events in by_id.items():
        ancestor = session_id
        visited = set()
        while ancestor != root_id:
            if ancestor in visited or ancestor not in by_id:
                raise ValueError("Codex rollout is not a descendant of the main session")
            visited.add(ancestor)
            ancestor = by_id[ancestor][0]["payload"].get("forked_from_id")
        recorded = final_codex_usage(events)
        for key in (
            "input_tokens", "cached_input_tokens", "output_tokens", "total_tokens",
            *(key for key in ("reasoning_output_tokens", "cache_write_input_tokens") if key in recorded),
        ):
            value = recorded.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"Invalid recorded session token category: {key}")
        if recorded["total_tokens"] != recorded["input_tokens"] + recorded["output_tokens"]:
            raise ValueError("Recorded session token total is inconsistent")
        if recorded["cached_input_tokens"] > recorded["input_tokens"]:
            raise ValueError("Recorded cached tokens exceed session input tokens")
        if recorded.get("reasoning_output_tokens", 0) > recorded["output_tokens"]:
            raise ValueError("Recorded reasoning tokens exceed session output tokens")
        totals[session_id] = recorded
    categories = {
        "input": "input_tokens",
        "cachedInput": "cached_input_tokens",
        "output": "output_tokens",
        "total": "total_tokens",
        "reasoning": "reasoning_output_tokens",
        "cacheCreationInput": "cache_write_input_tokens",
    }
    native["tokens"] = {
        name: sum(record[key] for record in totals.values())
        for name, key in categories.items()
        if all(key in record for record in totals.values())
    }
    events, _ = normalize_codex_events(rows, preserve_task=True)
    native["toolCalls"] = sum(event["type"] == "tool_call" for event in events)
    native["raw"].update(
        harborAccountingSessionId=trajectory["session_id"],
        codexMainSessionId=root_id,
        codexSessionTokenTotals=totals,
        accountingNote="Harbor exported a review subagent rather than the main session. Tokens sum the recorded per-session cumulative totals once each. Original Harbor accounting is retained separately; native cost was not recorded. Public transcript and artifact verification use the main session.",
    )
    return native


def native_usage(result: dict | None, trajectory: dict, rows: list[dict]) -> dict:
    """Retain Harbor accounting; never calculate prices in the importer."""
    context = result["agent_result"] if result is not None else None
    metrics = trajectory["final_metrics"]
    recorded = final_codex_usage(rows)
    categories = {
        "input": ("n_input_tokens", "total_prompt_tokens", "input_tokens"),
        "cachedInput": ("n_cache_tokens", "total_cached_tokens", "cached_input_tokens"),
        "output": ("n_output_tokens", "total_completion_tokens", "output_tokens"),
    }
    tokens = {}
    for name, (context_key, metric_key, recorded_key) in categories.items():
        value = recorded.get(recorded_key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(
                f"Missing or invalid recorded token category: {recorded_key}"
            )
        # Native ATIF uses null for recorded zero totals.
        if (
            context is not None
            and context.get(context_key)
            not in (
                value,
                None if value == 0 else value,
            )
        ) or metrics.get(metric_key) not in (value, None if value == 0 else value):
            raise ValueError(
                f"Harbor accounting disagrees with its rollout: {recorded_key}"
            )
        tokens[name] = value
    for name, key in (
        ("total", "total_tokens"),
        ("reasoning", "reasoning_output_tokens"),
        ("cacheCreationInput", "cache_write_input_tokens"),
    ):
        if key in recorded:
            tokens[name] = recorded[key]
    if tokens.get("total") != tokens["input"] + tokens["output"]:
        raise ValueError(
            "Recorded cumulative total is inconsistent with input and output tokens"
        )
    if metrics.get("extra", {}).get("total_tokens") != tokens["total"]:
        raise ValueError("Harbor trajectory total disagrees with its rollout")
    cost_usd = (
        context.get("cost_usd")
        if context is not None
        else metrics.get("total_cost_usd")
    )
    if context is not None and cost_usd != metrics.get("total_cost_usd"):
        raise ValueError("Harbor cost differs between AgentContext and trajectory")
    final_info = next(
        (
            row["payload"]["info"]
            for row in reversed(rows)
            if row.get("type") == "event_msg"
            and row.get("payload", {}).get("type") == "token_count"
            and row["payload"].get("info", {}).get("total_token_usage")
        ),
        {},
    )
    provider_cost = final_info.get("total_cost")
    if provider_cost is None:
        provider_cost = final_info.get("cost_usd")
    cost = None
    if cost_usd is not None:
        if (
            isinstance(cost_usd, bool)
            or not isinstance(cost_usd, (int, float))
            or not math.isfinite(cost_usd)
            or cost_usd < 0
        ):
            raise ValueError("Invalid native Harbor cost")
        if provider_cost is not None and provider_cost != cost_usd:
            raise ValueError("Provider cost disagrees with native Harbor accounting")
        cost = {
            "currency": "USD",
            "total": cost_usd,
            "estimated": provider_cost is None,
        }
    events, _ = normalize_codex_events(rows, preserve_task=True)
    return {
        "tokens": tokens,
        "cost": cost,
        "toolCalls": sum(event["type"] == "tool_call" for event in events),
        "providerReported": True,
        "raw": {
            **({"harborAgentContext": context} if context is not None else {}),
            "harborFinalMetrics": metrics,
            "codexCumulativeTokens": recorded,
            "inputIncludesCachedInput": True,
        },
    }


def build_trial(trial: Path) -> tuple[dict, dict, Path]:
    result = read_json(trial / "result.json")
    if result.get("exception_info") or not result.get("agent_result"):
        raise ValueError("Cannot import an unsuccessful Harbor trial")
    config = read_json(trial / "config.json")["agent"]
    model = result["agent_info"]["model_info"]
    effort = (
        config.get("kwargs", {})
        .get("config", {})
        .get("model_reasoning_effort", config.get("kwargs", {}).get("reasoning_effort"))
    )
    if (
        result["agent_info"]["name"] != "codex"
        or config["name"] != "codex"
        or model["provider"] != "openai"
        or model["name"] not in DISPLAY_NAMES
        or effort not in EFFORT_NAMES
    ):
        raise ValueError("Unsupported Harbor Codex model or effort")
    if config["model_name"] != f"openai/{model['name']}":
        raise ValueError("Requested and recorded Harbor models differ")
    run_id = f"openai/{model['name']}/{effort}"
    artifact = native_artifact(trial)
    trajectory = read_json(trial / "agent/trajectory.json")
    source, rows = main_rollout(trial, trajectory)
    metadata = next(row["payload"] for row in rows if row.get("type") == "session_meta")
    contexts = [row["payload"] for row in rows if row.get("type") == "turn_context"]
    if (
        metadata.get("cwd") != "/app"
        or not contexts
        or any(
            context.get("model") != model["name"] or context.get("effort") != effort
            for context in contexts
        )
    ):
        raise ValueError(
            "Codex session model, effort, or working directory does not match Harbor"
        )
    if metadata.get("cli_version") != result["agent_info"]["version"]:
        raise ValueError("Recorded Codex versions differ")
    verification = verify_codex_result(rows, run_id, artifact)
    events, omitted = normalize_codex_events(rows, preserve_task=True)
    execution = result["agent_execution"]
    started = datetime.fromisoformat(execution["started_at"])
    finished = datetime.fromisoformat(execution["finished_at"])
    if started.tzinfo is None or finished.tzinfo is None or finished < started:
        raise ValueError("Invalid native generation timestamps")
    timing = {
        "startedAt": execution["started_at"],
        "completedAt": execution["finished_at"],
        "wallDurationMs": round((finished - started).total_seconds() * 1000),
    }
    usage = trial_usage(trial, result, trajectory, rows)
    usage["raw"].update(
        harborTrial=trial.name,
        harborTaskChecksum=result["task_checksum"],
        artifactVerification=verification,
    )
    estimated = usage["cost"] is not None and usage["cost"]["estimated"]
    run = run_base(
        run_id,
        model["name"],
        {
            "name": "Codex",
            "version": metadata["cli_version"],
            "runtime": "Harbor",
            "invocation": f"Native Harbor codex agent; {model['name']} @ {effort}; ChatGPT subscription authentication",
        },
        timing,
        usage,
        "Generated and verified by native Harbor. Cost, tokens, and generation duration come from Harbor's recorded trial and Codex trajectory. Cached input is included in input; reasoning is included in output."
        + (" " + usage["raw"]["accountingNote"] if "accountingNote" in usage["raw"] else ""),
        "Harbor native AgentContext.cost_usd"
        + (" (Harbor LiteLLM estimate)" if estimated else ""),
        artifact,
    )
    transcript = transcript_base(
        run_id, metadata["id"], "harbor-codex-jsonl", source, events, omitted
    )
    return run, transcript, artifact


def recovery_grade(trial: Path, artifact: Path) -> dict:
    """Require a fully recorded native reward before recovering an unfinalized trial."""
    import ramen_harbor as workflow

    grade = read_json(trial / "verifier/grade.json")
    workflow.validate_grade(grade, artifact)
    if read_json(trial / "verifier/reward.json") != {
        "reward": grade["reward"],
        **grade["scores"],
    }:
        raise ValueError("Saved native verifier reward disagrees with its grade")
    return grade


def build_recovered_trial(trial: Path) -> tuple[dict, dict, Path]:
    """Recover recorded generation evidence without synthesizing a TrialResult."""
    if (trial / "result.json").exists():
        raise ValueError("Finalized trials must use the normal importer")
    config = read_json(trial / "config.json")["agent"]
    trajectory = read_json(trial / "agent/trajectory.json")
    agent = trajectory["agent"]
    model = agent["model_name"]
    effort = (
        config.get("kwargs", {})
        .get("config", {})
        .get("model_reasoning_effort", config.get("kwargs", {}).get("reasoning_effort"))
    )
    if (
        agent["name"] != "codex"
        or config["name"] != "codex"
        or model not in DISPLAY_NAMES
        or config["model_name"] != f"openai/{model}"
        or effort not in EFFORT_NAMES
    ):
        raise ValueError("Unsupported or inconsistent recovery model and effort")
    source, rows = main_rollout(trial, trajectory)
    metadata = next(row["payload"] for row in rows if row.get("type") == "session_meta")
    contexts = [row["payload"] for row in rows if row.get("type") == "turn_context"]
    if (
        metadata.get("cwd") != "/app"
        or not contexts
        or any(
            context.get("model") != model or context.get("effort") != effort
            for context in contexts
        )
    ):
        raise ValueError(
            "Recovered session does not match the requested model and effort"
        )
    if (
        metadata.get("cli_version") != agent["version"]
        or config.get("kwargs", {}).get("version") != agent["version"]
    ):
        raise ValueError("Recovered Codex versions differ")
    completions = [
        row
        for row in rows
        if row.get("type") == "event_msg"
        and row.get("payload", {}).get("type") == "task_complete"
    ]
    if not completions:
        raise ValueError("Cannot recover a generation without recorded task completion")
    started_text, finished_text = metadata["timestamp"], completions[-1]["timestamp"]
    started, finished = (
        datetime.fromisoformat(started_text),
        datetime.fromisoformat(finished_text),
    )
    if started.tzinfo is None or finished.tzinfo is None or finished < started:
        raise ValueError("Invalid recovered session timestamps")
    artifact = native_artifact(trial)
    run_id = f"openai/{model}/{effort}"
    verification = verify_codex_result(rows, run_id, artifact)
    recovery_grade(trial, artifact)
    events, omitted = normalize_codex_events(rows, preserve_task=True)
    usage = trial_usage(trial, None, trajectory, rows)
    lock = read_json(trial / "lock.json")
    usage["raw"].update(
        harborTrial=trial.name,
        harborTaskLock=lock["task"],
        artifactVerification=verification,
        harborTrialResultMissing=True,
        timingSource="Recorded Codex session start through task_complete",
    )
    run = run_base(
        run_id,
        model,
        {
            "name": "Codex",
            "version": agent["version"],
            "runtime": "Harbor",
            "invocation": f"Native Harbor codex agent; {model} @ {effort}; ChatGPT subscription authentication",
        },
        {
            "startedAt": started_text,
            "completedAt": finished_text,
            "wallDurationMs": round((finished - started).total_seconds() * 1000),
        },
        usage,
        "Recovered from the completed Codex session, Harbor ATIF trajectory, collected artifact, and saved native verifier grade/reward. Harbor did not finalize result.json. Generation timing uses the recorded Codex session bounds; cost and tokens use Harbor's saved trajectory. No trial result or AgentContext was reconstructed.",
        "Harbor native ATIF final_metrics.total_cost_usd"
        + (
            " (Harbor LiteLLM estimate)"
            if usage["cost"] and usage["cost"]["estimated"]
            else ""
        ),
        artifact,
    )
    transcript = transcript_base(
        run_id, metadata["id"], "harbor-codex-jsonl", source, events, omitted
    )
    return run, transcript, artifact


def register(root: Path, runs: list[dict]) -> None:
    registry_path = root / "registry.json"
    registry = read_json(registry_path)
    new_models = {f"{run['vendor']['slug']}/{run['model']['slug']}" for run in runs}
    all_paths = list(
        dict.fromkeys([*registry["runs"], *(f"{run['id']}/run.json" for run in runs)])
    )
    model_order = {model: index for index, model in enumerate(sorted(new_models))}
    fresh = sorted(
        (path for path in all_paths if "/".join(path.split("/")[:2]) in new_models),
        key=lambda path: (
            model_order["/".join(path.split("/")[:2])],
            -list(EFFORT_NAMES).index(path.split("/")[2]),
            path,
        ),
    )
    remaining = [path for path in all_paths if path not in fresh]
    insertion = next(
        (i for i, path in enumerate(remaining) if path.startswith("openai/")),
        len(remaining),
    )
    registry["runs"] = remaining[:insertion] + fresh + remaining[insertion:]
    for model in sorted(new_models):
        registry.setdefault("modelAddedAt", {}).setdefault(
            model, datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        )
    registry_path.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n")
    human_path = root / "harbor/human-scores.json"
    human = read_json(human_path)
    ids = {rating["runId"] for rating in human["ratings"]}
    for run in runs:
        if run["id"] not in ids:
            human["ratings"].append({"runId": run["id"], "humanScore": None})
    human_path.write_text(json.dumps(human, indent=2, ensure_ascii=False) + "\n")


def publish_grades(trials: list[tuple[Path, dict, Path]], root: Path) -> None:
    # Import lazily: generation-only source checks don't need browser dependencies.
    import ramen_harbor as workflow

    path = root / "scores.json"
    workflow.audit(path, workflow.registered_runs(root))
    current = read_json(path)
    additions = []
    for trial, run, artifact in trials:
        grade = read_json(trial / "verifier/grade.json")
        workflow.validate_grade(grade, artifact)
        if (trial / "result.json").is_file():
            native = (
                read_json(trial / "result.json")
                .get("verifier_result", {})
                .get("rewards")
            )
        elif run["usage"]["raw"].get("harborTrialResultMissing"):
            recovery_grade(trial, artifact)
            native = read_json(trial / "verifier/reward.json")
        else:
            raise ValueError("Missing native Harbor trial result")
        if native != {"reward": grade["reward"], **grade["scores"]}:
            raise ValueError("Native Harbor rewards disagree with the verified grade")
        if any(
            grade[key] != current["evaluation"][key]
            for key in ("rubricVersion", "verifierSha256", "judge")
        ):
            raise ValueError("Cannot mix different verifiers or judge configurations")
        row = {
            key: grade[key]
            for key in ("artifactSha256", "gradedAt", "scores", "reward")
        }
        row.update(
            runId=run["id"],
            rewardStdDev=grade.get("rewardStdDev"),
            judgeResults=grade.get("judgeResults", []),
            standaloneViolations=grade.get("standaloneViolations", []),
            complianceViolations=grade.get("complianceViolations", []),
            diagnostics=grade.get("diagnostics", {}),
            performance=grade.get("rendering", {}).get("measurements", {}),
        )
        additions.append(censor_value(row))
    existing = {row["runId"]: row for row in current["results"]}
    for row in additions:
        if row["runId"] in existing and existing[row["runId"]] != row:
            raise ValueError("Refusing to overwrite a different published judgment")
        existing[row["runId"]] = row
    current["results"] = list(existing.values())
    workflow.write_json(path, current)
    workflow.audit(path, workflow.registered_runs(root))
    print(
        f"Published {len(additions)} native grades; {len(existing)} total bowls scored"
    )


def build_registered_trial(run_id: str) -> tuple[dict, dict, Path]:
    recorded = read_json(ROOT / run_id / "run.json")
    trial_name = recorded["usage"]["raw"]["harborTrial"]
    if Path(trial_name).name != trial_name:
        raise ValueError("Invalid native Harbor trial identifier")
    matches = list((ROOT / ".harbor/jobs").glob(f"*/{trial_name}"))
    if len(matches) != 1:
        raise ValueError("Expected exactly one retained native Harbor trial")
    builder = (
        build_recovered_trial
        if recorded["usage"]["raw"].get("harborTrialResultMissing")
        else build_trial
    )
    run, transcript, artifact = builder(matches[0])
    if (
        run["id"] != run_id
        or artifact.read_bytes() != (ROOT / run_id / "index.html").read_bytes()
    ):
        raise ValueError("Registered bowl differs from its native Harbor source")
    return run, transcript, artifact


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harbor-job", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--publish-scores", action="store_true")
    parser.add_argument(
        "--recover-completed",
        action="store_true",
        help="recover a completed generation with a saved native grade/reward but no final trial result",
    )
    args = parser.parse_args(argv)
    built = []
    for trial in sorted(args.harbor_job.iterdir()):
        if not trial.is_dir():
            continue
        finalized = (trial / "result.json").is_file()
        if not finalized and (
            not args.recover_completed or not (trial / "verifier/grade.json").is_file()
        ):
            continue
        if finalized and read_json(trial / "result.json").get("exception_info"):
            print(f"Skipping unsuccessful trial: {trial.name}")
            continue
        run, transcript, artifact = (
            build_trial if finalized else build_recovered_trial
        )(trial)
        if any(previous[1]["id"] == run["id"] for previous in built):
            raise ValueError("Multiple successful trials for the same model and effort")
        built.append((trial, run, artifact))
        destination = ROOT / run["id"]
        public_artifact = destination / "index.html"
        if (
            public_artifact.exists()
            and public_artifact.read_bytes() != artifact.read_bytes()
        ):
            raise ValueError("Refusing to overwrite an immutable benchmark artifact")
        if not args.dry_run:
            destination.mkdir(parents=True, exist_ok=True)
            if not public_artifact.exists():
                public_artifact.write_bytes(artifact.read_bytes())
        write_json(destination / "run.json", run, args.dry_run)
        write_json(destination / "transcript.json", transcript, args.dry_run)
        print(
            f"Verified {run['id']} against native Harbor rollout; cost={run['usage']['cost']}"
        )
    if not built:
        raise ValueError("No successful, completed Harbor Codex trials found")
    if not args.dry_run:
        register(ROOT, [run for _, run, _ in built])
        if args.publish_scores:
            publish_grades(built, ROOT)
