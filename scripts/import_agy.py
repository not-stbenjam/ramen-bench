"""Import recorded native agy runs without reconstructing missing model output."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from agy_list_prices import AS_OF as PRICE_DATE
from agy_list_prices import SOURCE as PRICE_SOURCE
from agy_list_prices import argon_list_cost
from backfill_session_metadata import replay_codex_command, result_stats
from censor_transcripts import censor_value
from censor_transcripts import main as censor_transcripts
from import_harbor_claude import select_grade
from import_harbor_codex import native_artifact, publish_grades

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "google"))
from import_antigravity_runs import extract_user_request

EFFORTS = ("low", "medium", "high")
VERIFIER_FAILURES = {"RewardFileNotFoundError", "VerifierTimeoutError"}
PRIVATE_TOOLS = re.compile(
    r"inbox|agent.mail|send_message|schedule|manage_subagents|invoke_subagent|define_subagent",
    re.IGNORECASE,
)


def read_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def transcript_rows(path: Path) -> list[dict]:
    """Collapse only superseded in-progress snapshots of a native step."""
    records, latest = [], {}
    for row in read_rows(path):
        key = (row["step_index"], row["type"], row["source"])
        old = latest.get(key)
        if old is not None:
            previous = records[old]
            if previous == row or previous["status"] not in ("DONE", "ERROR", "FAILED"):
                records[old] = None
        latest[key] = len(records)
        records.append(row)
    return sorted((r for r in records if r is not None), key=lambda r: r["step_index"])


def normalize(rows: list[dict]) -> tuple[list[dict], int]:
    """Keep recorded visible messages and paired tool results; drop private payloads."""
    events, pending, omitted = [], [], 0
    for row in rows:
        kind, source = row["type"], row["source"]
        stamp = {"timestamp": row["created_at"]}
        if kind == "USER_INPUT" and source == "USER_EXPLICIT":
            events.append(
                {
                    "type": "message",
                    "role": "user",
                    "content": extract_user_request(row["content"]),
                    **stamp,
                }
            )
        elif kind == "PLANNER_RESPONSE" and source == "MODEL":
            if pending:
                raise ValueError("Native agy tool results are incomplete")
            if row.get("content"):
                events.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": row["content"],
                        **stamp,
                    }
                )
            for index, call in enumerate(row.get("tool_calls", [])):
                if PRIVATE_TOOLS.search(call["name"]):
                    pending.append(None)
                    omitted += 1
                    continue
                call_id = f"agy-{row['step_index']}-{index}"
                pending.append(call_id)
                events.append(
                    {
                        "type": "tool_call",
                        "id": call_id,
                        "tool": call["name"],
                        "input": call.get("args") or {},
                        **stamp,
                    }
                )
        elif kind == "GENERIC" and source == "MODEL" and pending:
            call_id = pending.pop(0)
            if call_id is not None:
                events.append(
                    {
                        "type": "tool_result",
                        "callId": call_id,
                        "status": "success" if row["status"] == "DONE" else "error",
                        "output": row.get("content", ""),
                        **stamp,
                    }
                )
        elif kind == "ERROR_MESSAGE" and isinstance(row.get("error"), str):
            events.append({"type": "error", "message": row["error"], **stamp})
    if pending:
        raise ValueError("Native agy tool results are incomplete")
    return censor_value(events, workspace=ROOT), omitted


def recorded_calls(rows: list[dict]):
    for row in rows:
        if row["type"] == "PLANNER_RESPONSE" and row["source"] == "MODEL":
            yield from row.get("tool_calls", [])


def successful_calls(rows: list[dict]):
    pending = []
    for row in rows:
        if row["type"] == "PLANNER_RESPONSE" and row["source"] == "MODEL":
            if pending:
                raise ValueError("Missing native agy tool result")
            pending.extend(row.get("tool_calls", []))
        elif row["type"] == "GENERIC" and row["source"] == "MODEL" and pending:
            call = pending.pop(0)
            if row["status"] == "DONE":
                yield call
    if pending:
        raise ValueError("Missing native agy tool result")


def replay_artifact(rows: list[dict], artifact: Path, workspace: str) -> str:
    """Interpret recorded file operations without executing model-written code."""
    content, operations = None, 0
    targets = {"index.html", str(Path(workspace) / "index.html")}
    for call in successful_calls(rows):
        name, args = call["name"], call.get("args") or {}
        if args.get("TargetFile") in targets:
            if name == "write_to_file":
                content = args["CodeContent"]
                operations += 1
            elif name in ("replace_file_content", "multi_replace_file_content"):
                edits = args.get("ReplacementChunks", [args])
                for edit in edits:
                    old, new = edit["TargetContent"], edit["ReplacementContent"]
                    if content is None or not old or content.count(old) != 1:
                        raise ValueError(
                            "Recorded agy edit must match one source range"
                        )
                    content = content.replace(old, new, 1)
                    operations += 1
            elif name in ("sed_file", "notebook_edit"):
                raise ValueError("Extend agy replay for this recorded mutation")
        elif name == "run_command":
            content, count = replay_codex_command(
                content, args["CommandLine"], "gemini-4-argon"
            )
            operations += count
    if content is None or content.encode() != artifact.read_bytes():
        raise ValueError("Artifact differs from recorded agy file operations")
    return f"Recorded agy file-operation replay ({operations} operations)"


def recovered_completion(rows: list[dict], final: dict) -> bool:
    """Recognize agy's retained ERROR only with completed post-timeout evidence."""
    if final.get("status") != "ERROR":
        return False
    errors = [r for r in rows if r["type"] == "ERROR_MESSAGE"]
    if not errors or any(
        not str(r.get("error", "")).startswith(
            ("API error", "The stream was interrupted.")
        )
        for r in errors
    ):
        return False
    last_error = max(r["step_index"] for r in errors)
    after = [r for r in rows if r["step_index"] > last_error]
    if not after or any(r["status"] != "DONE" or r["source"] != "MODEL" for r in after):
        return False
    last = after[-1]
    if (
        last["type"] != "PLANNER_RESPONSE"
        or last.get("tool_calls")
        or not last.get("content", "").strip()
        or str(final.get("response", "")).strip() != last["content"].strip()
    ):
        return False
    # The exact replay independently checks the successful result of these
    # writes. A final apology with no post-error artifact write is insufficient.
    return any(
        call["name"]
        in ("write_to_file", "replace_file_content", "multi_replace_file_content")
        and (call.get("args") or {}).get("TargetFile")
        in ("index.html", "/app/index.html")
        for call in successful_calls(after)
    )


def stream_usage(
    stream: list[dict], *, allow_recovered_error: bool = False
) -> tuple[dict, int, dict]:
    """Require a completed native aggregate and matching per-step counters."""
    results = [r["result"] for r in stream if r.get("event") == "result"]
    if len(results) != 1:
        raise ValueError("Expected one native agy completion result")
    final = results[0]
    if (
        final.get("status") != "SUCCESS"
        and not (allow_recovered_error and final.get("status") == "ERROR")
    ) or final.get("num_turns") != 1:
        raise ValueError("Native agy did not successfully complete a single turn")
    usage = final["usage"]
    keys = (
        "input_tokens",
        "output_tokens",
        "thinking_tokens",
        "cache_read_tokens",
        "total_tokens",
    )
    totals = dict.fromkeys(keys, 0)
    steps = {}
    for row in stream:
        if row.get("event") != "step_update":
            continue
        step = row["step_update"]
        if "usage" not in step or step.get("state") != "DONE":
            continue
        index = step["step_index"]
        if index in steps and steps[index] != step["usage"]:
            raise ValueError("Conflicting repeated native usage counters")
        steps[index] = step["usage"]
    for entry in [usage, *steps.values()]:
        for key in keys:
            value = entry.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"Missing or invalid agy usage category: {key}")
        # agy 1.3.6's native total excludes its disjoint cache-read category.
        # Retain that counter in raw usage; include cache reads once publicly.
        if entry["total_tokens"] != entry["input_tokens"] + entry["output_tokens"]:
            raise ValueError("Inconsistent native agy token total")
        if entry["thinking_tokens"] > entry["output_tokens"]:
            raise ValueError("Thinking exceeds recorded inclusive output")
    for entry in steps.values():
        for key in keys:
            totals[key] += entry[key]
    if not steps or totals != {k: usage[k] for k in keys}:
        raise ValueError("Native aggregate differs from recorded step usage")
    return totals, len(steps), final


def build_trial(trial: Path) -> tuple[dict, dict, Path]:
    result = json.loads((trial / "result.json").read_text())
    config = json.loads((trial / "config.json").read_text())["agent"]
    effort = config.get("kwargs", {}).get("reasoning_effort")
    model = f"gemini-4-argon-{effort}"
    failure = result.get("exception_info")
    if (
        (failure and failure.get("exception_type") not in VERIFIER_FAILURES)
        or effort not in EFFORTS
        or config["name"] != "scripts.harbor_agy:SubscriptionAgy"
        or config["model_name"] != f"google/{model}"
        or result["agent_info"]["name"] != "antigravity-cli"
        or result["agent_info"]["model_info"] != {"name": model, "provider": "google"}
    ):
        raise ValueError("Require a successful native Harbor Argon trial")
    stream = read_rows(trial / "agent/antigravity-stream.jsonl")
    starts = [r for r in stream if r.get("event") == "init"]
    if (
        len(starts) != 1
        or starts[0]["init"]["model"] != model
        or starts[0]["init"]["cwd"] != "/app"
    ):
        raise ValueError("Native agy initialized a different model or workspace")
    log = (trial / "trial.log").read_text()
    if f"--model {model} --effort {effort} " not in log:
        raise ValueError("Recorded native invocation differs from requested effort")
    rows = transcript_rows(trial / "agent/antigravity-cli.trajectory.jsonl")
    events, omitted = normalize(rows)
    api_errors = [
        r["error"]
        for r in rows
        if r["type"] == "ERROR_MESSAGE"
        and str(r.get("error", "")).startswith(
            ("API error", "The stream was interrupted.")
        )
    ]
    users = [e["content"].strip() for e in events if e.get("role") == "user"]
    if users != [(ROOT / "harbor/ramen/instruction.md").read_text().strip()]:
        raise ValueError("Native agy did not receive the exact benchmark instruction")
    artifact = native_artifact(trial)
    replay = replay_artifact(rows, artifact, "/app")
    final_records = [r["result"] for r in stream if r.get("event") == "result"]
    recovered = len(final_records) == 1 and recovered_completion(rows, final_records[0])
    usage, requests, _final = stream_usage(stream, allow_recovered_error=recovered)
    context = result["agent_result"]
    if (
        context["n_input_tokens"] != usage["input_tokens"] + usage["cache_read_tokens"]
        or context["n_cache_tokens"] != usage["cache_read_tokens"]
        or context["n_output_tokens"] != usage["output_tokens"]
    ):
        raise ValueError("Harbor accounting differs from native agy stream usage")
    execution = result["agent_execution"]
    start, end = (
        datetime.fromisoformat(execution[k]) for k in ("started_at", "finished_at")
    )
    if start.tzinfo is None or end.tzinfo is None or end < start:
        raise ValueError("Invalid native Harbor generation timestamps")
    priced, pricing = argon_list_cost(usage, execution["finished_at"])
    if api_errors:
        pricing["recordedUsageSubtotalUsd"] = priced["total"]
        pricing["fullRunCostUnknown"] = True
        pricing["reason"] = "Failed generation-stream requests have no recorded usage"
    run_id = f"google/gemini-4-argon/{effort}"
    run = {
        "$schema": "../../../schemas/run.schema.json",
        "schemaVersion": 1,
        "id": run_id,
        "vendor": {"slug": "google", "displayName": "Google"},
        "model": {
            "slug": "gemini-4-argon",
            "displayName": "Gemini 4 Argon",
            "providerModelId": model,
        },
        "variation": {
            "slug": effort,
            "displayName": effort.title(),
            "reasoningEffort": effort,
            "parameters": {"modelSelection": f"Gemini 4 Argon ({effort.title()})"},
        },
        "harness": {
            "name": "Antigravity CLI",
            "version": result["agent_info"]["version"],
            "runtime": "Harbor",
            "invocation": f"Native Harbor agy with pinned executable and subscription login; {model} --effort {effort}",
        },
        "artifacts": {"result": "index.html", "transcript": "transcript.json"},
        "timing": {
            "startedAt": execution["started_at"],
            "completedAt": execution["finished_at"],
            "wallDurationMs": round((end - start).total_seconds() * 1000),
        },
        "usage": {
            "tokens": {
                "input": usage["input_tokens"],
                "cachedInput": usage["cache_read_tokens"],
                "output": usage["output_tokens"] - usage["thinking_tokens"],
                "reasoning": usage["thinking_tokens"],
                "total": usage["input_tokens"]
                + usage["cache_read_tokens"]
                + usage["output_tokens"],
            },
            "cost": priced if not api_errors else None,
            **({"requests": requests} if not api_errors else {}),
            "turns": 1,
            "toolCalls": sum(1 for _ in recorded_calls(rows)),
            "providerReported": True,
            "raw": {
                "nativeStreamUsage": usage,
                "nativeCompletionStatus": _final["status"],
                "pricing": pricing,
                **({"recoveredAfterAPIErrors": True} if recovered else {}),
                "harborAgentTokens": {
                    key: context[key]
                    for key in ("n_input_tokens", "n_cache_tokens", "n_output_tokens")
                },
                "harborTrial": trial.name,
                "harborTaskChecksum": result["task_checksum"],
                "artifactVerification": replay,
                **(
                    {
                        "accountingIncomplete": True,
                        "nativeAPIErrors": api_errors,
                        "recordedCompletedRequests": requests,
                    }
                    if api_errors
                    else {}
                ),
                **(
                    {"harborVerifierFailure": failure["exception_type"]}
                    if failure
                    else {}
                ),
            },
        },
        "resultStats": result_stats(run_id, artifact),
        "provenance": {
            "pricingSource": f"Google Standard published list prices, pinned {PRICE_DATE}; {PRICE_SOURCE}"
        },
        "notes": "Generated entirely by native agy inside Harbor. HTML is unchanged and matches recorded successful file operations. Generation time comes from Harbor and excludes setup and grading. Token counters come from the native completion aggregate, checked against individual steps; input and cache reads are disjoint, and thinking is included once in output then split into reasoning. Native total_tokens excludes cache reads; the public total includes them once. Pricing uses pinned Google Standard API-equivalent list prices, with cache hits discounted and thinking included once; it is not a recorded subscription charge. Hidden reasoning, system/developer prompts, credentials, private agent activity, opaque payloads and personal paths are excluded from the public transcript."
        + (
            " Native API errors occurred before recovery; recorded usage covers completed requests and is a lower bound. Usage for failed requests and a full request count remain unknown."
            if api_errors
            else ""
        )
        + (
            f" The priced recorded-usage subtotal is ${priced['total']:.7f}; the full run cost remains unknown because failed requests have no usage counters."
            if api_errors
            else ""
        )
        + (
            " The native final status remains ERROR following an earlier API timeout. Harbor exited normally; a recorded successful file write and the final assistant response followed the timeout."
            if recovered
            else ""
        ),
    }
    transcript = {
        "$schema": "../../../schemas/transcript.schema.json",
        "schemaVersion": 1,
        "runId": run_id,
        "sourceFormat": "Native Harbor agy full transcript and stream-json",
        "events": events,
        "metadata": {
            "providerModelId": model,
            "reasoningEffort": effort,
            "omittedPrivateToolCalls": omitted,
            "normalization": "Recorded visible user and assistant messages and tool activity; shared public censor.",
        },
    }
    return censor_value(run, workspace=ROOT), transcript, artifact


def register(runs: list[dict]):
    path = ROOT / "registry.json"
    registry = json.loads(path.read_text())
    prefix = "google/gemini-4-argon/"
    fresh = {f"{r['id']}/run.json" for r in runs}
    fresh |= {p for p in registry["runs"] if p.startswith(prefix)}
    ordered = sorted(fresh, key=lambda p: -EFFORTS.index(p.split("/")[2]))
    others = [p for p in registry["runs"] if not p.startswith(prefix)]
    index = next(
        (i for i, p in enumerate(others) if p.startswith("google/")), len(others)
    )
    registry["runs"] = others[:index] + ordered + others[index:]
    registry.setdefault("modelAddedAt", {}).setdefault(
        "google/gemini-4-argon", max(r["timing"]["completedAt"] for r in runs)
    )
    path.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n")
    path = ROOT / "harbor/human-scores.json"
    human = json.loads(path.read_text())
    ids = {r["runId"] for r in human["ratings"]}
    human["ratings"].extend(
        {"runId": r["id"], "humanScore": None} for r in runs if r["id"] not in ids
    )
    path.write_text(json.dumps(human, indent=2, ensure_ascii=False) + "\n")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harbor-job", type=Path, required=True, action="append")
    parser.add_argument("--grade-job", type=Path, action="append", default=[])
    parser.add_argument("--check-sources", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--publish-scores", action="store_true")
    args = parser.parse_args(argv)
    built = []
    for job in args.harbor_job:
        for trial in sorted(job.iterdir()):
            if not trial.is_dir() or not (trial / "result.json").exists():
                continue
            run, transcript, artifact = build_trial(trial)
            grade = select_grade(trial, args.grade_job)
            if grade != trial:
                if native_artifact(grade).read_bytes() != artifact.read_bytes():
                    raise ValueError("Native regrade changed the original artifact")
                run["usage"]["raw"]["harborGradeTrial"] = grade.name
                run["notes"] += (
                    " Scores come from a native Harbor regrade of the saved artifact; original generation timing, usage, and native completion status are retained."
                )
            if any(r[1]["id"] == run["id"] for r in built):
                raise ValueError("Multiple generations for one effort")
            destination = ROOT / run["id"]
            if (destination / "index.html").exists() and (
                destination / "index.html"
            ).read_bytes() != artifact.read_bytes():
                raise ValueError("Refusing to replace an immutable benchmark artifact")
            if args.check_sources:
                for name, expected in (
                    ("run.json", run),
                    ("transcript.json", transcript),
                ):
                    if json.loads((destination / name).read_text()) != expected:
                        raise ValueError(
                            f"{run['id']}/{name} differs from its native Harbor source"
                        )
            elif not args.dry_run:
                destination.mkdir(parents=True, exist_ok=True)
                if not (destination / "index.html").exists():
                    (destination / "index.html").write_bytes(artifact.read_bytes())
                for name, value in (("run.json", run), ("transcript.json", transcript)):
                    (destination / name).write_text(
                        json.dumps(value, indent=2, ensure_ascii=False) + "\n"
                    )
            built.append((grade, run, artifact))
            print(
                f"Verified {run['id']} against native Harbor agy and exact final writes"
            )
    if not built:
        raise ValueError("No completed native Harbor agy trials")
    if not args.check_sources and not args.dry_run:
        register([r for _, r, _ in built])
        if args.publish_scores:
            publish_grades(built, ROOT)
        if censor_transcripts(["--write", str(ROOT / "google")]):
            raise ValueError("Public agy censor failed")
    return 0
