"""Import completed native Harbor Muse Wonder sessions and unchanged artifacts."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from backfill_session_metadata import (
    ROOT,
    replay_codex_command,
    replay_codex_patch,
    result_stats,
    scrub,
    write_json,
)
from censor_transcripts import censor_value
from censor_transcripts import main as censor_transcripts
from import_harbor_claude import select_grade
from import_harbor_codex import native_artifact, publish_grades, read_json
from muse_list_prices import AS_OF as PRICE_DATE
from muse_list_prices import SOURCE as PRICE_SOURCE
from muse_list_prices import wonder_list_cost

MODEL = "muse-wonder-1.0"
MODEL_NAMES = {MODEL: "Muse Wonder", "muse-wonder-full-1.0": "Muse Wonder Full"}
EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")
PRIVATE_TOOLS = re.compile(
    r"agent[_-]?mail|session[_-]?message|dawnmark", re.IGNORECASE
)
TOKEN_KEYS = (
    "input_tokens",
    "cached_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "output_tokens",
    "reasoning_tokens",
)


def envelope(record: dict) -> dict:
    return record.get("envelope", {})


def event(record: dict) -> dict:
    return envelope(record).get("payload", {}).get("event", {})


def timestamp(record: dict) -> str:
    micros = envelope(record).get("recorded_at")
    if isinstance(micros, bool) or not isinstance(micros, int):
        raise TypeError("Muse event lacks a recorded timestamp")
    return datetime.fromtimestamp(micros / 1_000_000, UTC).isoformat()


def arguments(call: dict):
    raw = call.get("args")
    return json.loads(raw) if isinstance(raw, str) else raw


def recorded_calls(export: dict):
    for record in export["events"]:
        if event(record).get("kind") == "assistant_tool_calls_committed":
            yield from event(record)["tool_calls"]


def normalize_session(export: dict) -> tuple[list[dict], int]:
    events, pending = [], []
    counter = omitted = 0
    for record in export["events"]:
        env, current = envelope(record), event(record)
        payload = env.get("payload", {})
        first = len(events)
        if env.get("payload_type") == "runtime.user_intent.accepted":
            for block in payload.get("refill_blocks", []):
                if block.get("kind") == "text" and isinstance(block.get("text"), str):
                    events.append(
                        {
                            "type": "message",
                            "role": "user",
                            "content": scrub(block["text"]),
                        }
                    )
        elif current.get("kind") == "assistant_message_committed":
            events.append(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": scrub(current["text"]),
                }
            )
        elif current.get("kind") == "assistant_tool_calls_committed":
            batch = []
            for call in current["tool_calls"]:
                counter += 1
                args, name = arguments(call), call["name"]
                if PRIVATE_TOOLS.search(name) or PRIVATE_TOOLS.search(str(args)):
                    batch.append(None)
                    omitted += 1
                    continue
                call_id = f"muse-tool-{counter:03d}"
                batch.append(call_id)
                events.append(
                    {
                        "type": "tool_call",
                        "id": call_id,
                        "tool": name,
                        "input": scrub(args),
                    }
                )
            pending.append(batch)
        elif current.get("kind") == "tool_result_batch_committed":
            if not pending:
                raise ValueError("Muse results lack their recorded tool-call batch")
            batch = pending.pop(0)
            indexes = set()
            for index, result in enumerate(current["results"]):
                index = result.get("tool_call_index", index)
                if (
                    not isinstance(index, int)
                    or not 0 <= index < len(batch)
                    or index in indexes
                ):
                    raise ValueError("Muse tool result has an invalid batch index")
                indexes.add(index)
                if batch[index] is None:
                    continue
                output = result.get("text")
                if isinstance(output, str):
                    try:
                        output = json.loads(output)
                    except json.JSONDecodeError:
                        pass
                failed = (
                    isinstance(output, dict)
                    and (
                        output.get("terminal_status") == "failed"
                        or output.get("exit_code", 0) != 0
                    )
                ) or (
                    isinstance(output, str)
                    and output.lstrip().lower().startswith("error")
                )
                events.append(
                    {
                        "type": "tool_result",
                        "callId": batch[index],
                        "status": "error" if failed else "success",
                        "output": scrub(output),
                    }
                )
            if indexes != set(range(len(batch))):
                raise ValueError("Muse tool result batch is incomplete")
        for public in events[first:]:
            public["timestamp"] = timestamp(record)
    if pending:
        raise ValueError("Muse session has unfinished tool-call batches")
    diagnostics = export.get("diagnostics", {})
    if diagnostics.get("unparseable_lines") or diagnostics.get("unknown_payload_kinds"):
        raise ValueError("Muse exporter could not parse the retained session")
    if diagnostics.get("gaps") or diagnostics.get("omitted_live_only"):
        events.append(
            {
                "type": "note",
                "content": f"Muse's native export reports {diagnostics.get('gaps', 0)} history gaps and {diagnostics.get('omitted_live_only', 0)} omitted live-only records. Missing content has not been reconstructed.",
            }
        )
    return events, omitted


def verify_artifact(export: dict, artifact: Path, model: str = MODEL) -> str:
    content, mutations = None, 0
    for call in recorded_calls(export):
        name, args = call["name"], arguments(call)
        if name == "write_file" and args.get("path") in (
            "index.html",
            "/app/index.html",
        ):
            content = args["content"]
            mutations += 1
        elif name == "apply_patch":
            patch = args.get("patch", args.get("input"))
            lines, count = replay_codex_patch(
                content.splitlines() if content is not None else None, patch, model
            )
            content = "\n".join(lines) + "\n" if lines is not None else None
            mutations += count
        elif name == "bash":
            content, count = replay_codex_command(content, args["command"], model)
            mutations += count
        elif name == "edit_file" and args.get("path") in (
            "index.html",
            "/app/index.html",
        ):
            if content is None or content.count(args["find"]) != 1:
                raise ValueError(
                    "Muse edit must match exactly one recorded source range"
                )
            content = content.replace(args["find"], args["replace"], 1)
            mutations += 1
        elif name == "replace_in_file" and args.get("path") in (
            "index.html",
            "/app/index.html",
        ):
            raise ValueError("Extend the Muse replay for this recorded edit tool")
    if content is None or content.encode() != artifact.read_bytes():
        raise ValueError("Muse artifact differs from its recorded final writes")
    return f"Recorded Muse file-operation replay ({mutations} operations)"


def sum_usage(
    export: dict, *, allow_cancelled_empty: bool = False
) -> tuple[dict, int, int]:
    session_id = export["sessions"][0]["session_id"]
    totals = dict.fromkeys(TOKEN_KEYS, 0)
    seen, requests, duration = set(), 0, 0
    for record in export["events"]:
        current, env = event(record), envelope(record)
        if current.get("kind") != "model_completed":
            continue
        if env.get("stream", {}).get("id") != session_id:
            raise ValueError("Muse accounting includes a foreign or copied session")
        if env["id"] in seen:
            raise ValueError("Duplicate recorded Muse request")
        seen.add(env["id"])
        usage = current["usage"]
        for key in TOKEN_KEYS:
            value = usage.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(
                    f"Missing or invalid native Muse token category: {key}"
                )
            totals[key] += value
        if (
            usage["input_tokens"] < usage["cache_read_tokens"]
            or usage["cached_tokens"] != usage["cache_read_tokens"]
        ):
            raise ValueError("Inconsistent native Muse cache accounting")
        if usage["cache_write_tokens"] != 0:
            raise ValueError(
                "Muse cache-write semantics require verification before import"
            )
        if usage["reasoning_tokens"] > usage["output_tokens"]:
            raise ValueError("Muse reasoning exceeds its inclusive output total")
        millis = current.get("duration_ms")
        if isinstance(millis, bool) or not isinstance(millis, int) or millis < 0:
            raise ValueError("Invalid native Muse request duration")
        duration += millis
        requests += 1
    if not requests:
        terminals = [
            event(r) for r in export["events"] if event(r).get("kind") == "terminal"
        ]
        if not (
            allow_cancelled_empty
            and len(terminals) == 1
            and terminals[0].get("terminal") == "cancelled"
            and any(event(r).get("kind") == "timed_out" for r in export["events"])
        ):
            raise ValueError("Muse export has no recorded provider usage")
    return totals, requests, duration


def export_session(source: Path, binary: str) -> dict:
    destination = source.parent / "benchmark-export.json"
    subprocess.run(
        [binary, "export", "--session", str(source), "--out", str(destination)],
        check=True,
        capture_output=True,
        text=True,
    )
    data = read_json(destination)
    if data.get("export_schema_version") != 1 or len(data.get("sessions", [])) != 1:
        raise ValueError("Unsupported or ambiguous native Muse export")
    if data["sessions"][0]["session_id"] != source.parent.name:
        raise ValueError(
            "Muse export session identity differs from its retained source"
        )
    return data


def build_trial(trial: Path, binary: str) -> tuple[dict, dict, Path]:
    result, config = (
        read_json(trial / "result.json"),
        read_json(trial / "config.json")["agent"],
    )
    effort = config.get("kwargs", {}).get("reasoning_effort")
    model = config["model_name"].removeprefix("meta/")
    if (
        result.get("exception_info")
        or model not in MODEL_NAMES
        or config["model_name"] != f"meta/{model}"
        or effort not in EFFORTS
    ):
        raise ValueError("Unsuccessful or unsupported native Muse trial")
    if result["agent_info"]["name"] != "muse-code" or result["agent_info"][
        "model_info"
    ] != {"name": model, "provider": "meta"}:
        raise ValueError("Harbor recorded a different generation model")
    sources = sorted((trial / "agent/sessions").rglob("session.jsonl"))
    main = [
        p
        for p in sources
        if "subagent" not in p.relative_to(trial / "agent/sessions").parts
    ]
    if len(main) != 1:
        raise ValueError("Expected exactly one retained main Muse session")
    export = export_session(main[0], binary)
    metadata = [
        envelope(r)["payload"]["record"]
        for r in export["events"]
        if envelope(r).get("payload_type") == "runtime.session.metadata"
    ]
    if (
        len(metadata) != 1
        or metadata[0].get("workspace_root") != "/app"
        or metadata[0].get("model_id") != model
        or metadata[0].get("provider_id") != "meta"
    ):
        raise ValueError(
            "Muse session metadata differs from the benchmark configuration"
        )
    if (
        export.get("session_terminated_abnormally")
        or export["sessions"][0]["turn_count"] != 1
    ):
        raise ValueError("Muse generation did not complete a single native turn")
    terminals = [r for r in export["events"] if event(r).get("kind") == "terminal"]
    if len(terminals) != 1 or event(terminals[0]).get("terminal") != "completed":
        raise ValueError("Muse generation has no successful native completion")
    completed = [
        event(r) for r in export["events"] if event(r).get("kind") == "model_completed"
    ]
    if not completed or any(r.get("model") != model for r in completed):
        raise ValueError("Muse responses came from a different model")
    log = (trial / "trial.log").read_text()
    if f"--model {model} --reasoning-effort {effort} " not in log:
        raise ValueError("Recorded Muse invocation differs from the requested effort")
    events, omitted = normalize_session(export)
    users = [e["content"].strip() for e in events if e.get("role") == "user"]
    if users != [(ROOT / "harbor/ramen/instruction.md").read_text().strip()]:
        raise ValueError(
            "Muse session does not contain the exact benchmark instruction"
        )
    artifact = native_artifact(trial)
    verification = verify_artifact(export, artifact, model)
    totals, requests, api_duration = sum_usage(export)
    exports = [export]
    main_totals, auxiliary, incomplete_auxiliary = totals.copy(), [], []
    for source in sources:
        if source == main[0]:
            continue
        child = export_session(source, binary)
        exports.append(child)
        child_totals, child_requests, child_duration = sum_usage(
            child, allow_cancelled_empty=True
        )
        child_terminal = [
            event(r) for r in child["events"] if event(r).get("kind") == "terminal"
        ]
        incomplete = any(t.get("terminal") == "cancelled" for t in child_terminal)
        if incomplete:
            incomplete_auxiliary.append(child["sessions"][0]["session_id"])
        auxiliary.append(
            {
                "sessionId": child["sessions"][0]["session_id"],
                "modelCompletedTotals": child_totals if child_requests else None,
                "requests": child_requests,
                **(
                    {"accountingIncomplete": True, "terminal": child_terminal[0]}
                    if incomplete
                    else {}
                ),
            }
        )
        for key in totals:
            totals[key] += child_totals[key]
        requests += child_requests
        api_duration += child_duration
    execution = result["agent_execution"]
    start, finish = (
        datetime.fromisoformat(execution[k]) for k in ("started_at", "finished_at")
    )
    if start.tzinfo is None or finish.tzinfo is None or finish < start:
        raise ValueError("Invalid native Harbor generation timestamps")
    run_id = f"meta/{model}/{effort}"
    cached, output, reasoning = (
        totals["cache_read_tokens"],
        totals["output_tokens"],
        totals["reasoning_tokens"],
    )
    version = export["session_build"]["semver"]
    if result["agent_info"]["version"] != version:
        raise ValueError("Harbor and Muse's recorded build versions disagree")
    priced = wonder_list_cost(exports) if not incomplete_auxiliary else None
    run = {
        "$schema": "../../../schemas/run.schema.json",
        "schemaVersion": 1,
        "id": run_id,
        "vendor": {"slug": "meta", "displayName": "Meta"},
        "model": {
            "slug": model,
            "displayName": MODEL_NAMES[model],
            "providerModelId": model,
        },
        "variation": {
            "slug": effort,
            "displayName": "XHigh" if effort == "xhigh" else effort.title(),
            "reasoningEffort": effort,
            "parameters": {
                "effortSource": "Native Harbor kwargs and recorded Muse CLI invocation"
            },
        },
        "harness": {
            "name": "Muse Code",
            "version": version,
            "runtime": "Harbor",
            "invocation": f"Native Harbor Muse Code with pinned executable; {model} @ {effort}",
        },
        "artifacts": {"result": "index.html", "transcript": "transcript.json"},
        "timing": {
            "startedAt": execution["started_at"],
            "completedAt": execution["finished_at"],
            "wallDurationMs": round((finish - start).total_seconds() * 1000),
            **({"apiDurationMs": api_duration} if not incomplete_auxiliary else {}),
        },
        "usage": {
            "tokens": {
                "input": totals["input_tokens"] - cached,
                "cachedInput": cached,
                "cacheCreationInput": 0,
                "output": output - reasoning,
                "reasoning": reasoning,
                "total": totals["input_tokens"] + output,
            },
            "cost": priced[0] if priced else None,
            **({"requests": requests} if not incomplete_auxiliary else {}),
            "turns": 1,
            "toolCalls": sum(1 for _ in recorded_calls(export)),
            "providerReported": True,
            "raw": {
                "mainSessionTokens": main_totals,
                "auxiliarySessions": auxiliary,
                "modelCompletedTotals": totals,
                "terminal": event(terminals[0]),
                "harborTrial": trial.name,
                "harborTaskChecksum": result["task_checksum"],
                "artifactVerification": verification,
                "nativeExportDiagnostics": export["diagnostics"],
                **(
                    {
                        "accountingIncomplete": True,
                        "unaccountedAuxiliarySessions": incomplete_auxiliary,
                        "recordedCompletedRequests": requests,
                        "recordedApiDurationMs": api_duration,
                    }
                    if incomplete_auxiliary
                    else {}
                ),
                **({"pricing": priced[1]} if priced else {}),
            },
        },
        "resultStats": result_stats(run_id, artifact),
        "provenance": {
            "pricingSource": f"Meta Standard published list prices, pinned {PRICE_DATE}; {PRICE_SOURCE}"
        }
        if priced
        else {},
        "notes": "HTML is unchanged and verified against recorded Muse writes. Generation timing excludes setup and grading. Tokens sum native model_completed events once per retained main and auxiliary session. Input excludes cache hits; reasoning is a subset of native output, split out of usage.tokens.output and counted once in the total. Auxiliary session content, hidden reasoning, system/developer prompts and private activity are excluded from the public transcript. "
        + (
            "Cost is an API-equivalent Standard list-price estimate at Meta's published rates, including auxiliary sessions and reasoning once; it is not a recorded charge. "
            if priced
            else "Wonder Full is subscription-only and has no published per-token rate; cost is unknown. "
            if model == "muse-wonder-full-1.0"
            else "Cost remains unknown when recorded usage or model/tool pricing is insufficient. "
        )
        + "Effort is the explicitly requested setting recorded in Harbor and its Muse invocation."
        + (
            " Auxiliary model work was cancelled with no complete usage accounting; token totals include only recorded completed requests and are a lower bound. Full request counts and API duration are unknown; recorded subtotals remain in usage.raw."
            if incomplete_auxiliary
            else ""
        ),
    }
    transcript = {
        "$schema": "../../../schemas/transcript.schema.json",
        "schemaVersion": 1,
        "runId": run_id,
        "sessionId": export["sessions"][0]["session_id"],
        "sourceFormat": "Native Muse Code export_schema_version 1, filtered by shared public censor",
        "events": events,
        "metadata": {
            "providerModelId": model,
            "reasoningEffort": effort,
            "harnessVersion": export["session_build"]["display"],
            "omittedPrivateToolCalls": omitted,
        },
    }
    return censor_value(run), censor_value(transcript), artifact


def register(runs: list[dict]) -> None:
    path = ROOT / "registry.json"
    registry = read_json(path)
    models = {r["model"]["slug"] for r in runs}
    if len(models) != 1:
        raise ValueError("Import one native Muse model per job")
    model = next(iter(models))
    added = registry.setdefault("modelAddedAt", {})
    added.setdefault(f"meta/{model}", max(r["timing"]["completedAt"] for r in runs))
    fresh = {f"{r['id']}/run.json" for r in runs}
    all_paths = set(registry["runs"]) | fresh
    fresh = sorted(
        (p for p in all_paths if p.startswith(f"meta/{model}/")),
        key=lambda p: -EFFORTS.index(p.split("/")[2]),
    )
    other = [p for p in registry["runs"] if p not in fresh]
    meta_indexes = [i for i, p in enumerate(other) if p.startswith("meta/")]
    index = next(
        (
            i
            for i in meta_indexes
            if added.get("/".join(other[i].split("/")[:2]), "")
            <= added[f"meta/{model}"]
        ),
        meta_indexes[-1] + 1 if meta_indexes else len(other),
    )
    registry["runs"] = other[:index] + fresh + other[index:]
    path.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n")
    path = ROOT / "harbor/human-scores.json"
    human = read_json(path)
    ids = {r["runId"] for r in human["ratings"]}
    for run in runs:
        if run["id"] not in ids:
            human["ratings"].append({"runId": run["id"], "humanScore": None})
    path.write_text(json.dumps(human, indent=2, ensure_ascii=False) + "\n")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harbor-job", type=Path, required=True)
    parser.add_argument("--grade-job", type=Path, action="append", default=[])
    parser.add_argument("--effort", choices=EFFORTS, action="append")
    parser.add_argument("--muse-binary", default="muse")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--check-sources", action="store_true")
    parser.add_argument("--publish-scores", action="store_true")
    args = parser.parse_args(argv)
    built = []
    for trial in sorted(args.harbor_job.iterdir()):
        if not trial.is_dir() or not (trial / "result.json").exists():
            continue
        if read_json(trial / "result.json").get("exception_info"):
            continue
        effort = read_json(trial / "config.json")["agent"]["kwargs"]["reasoning_effort"]
        if args.effort and effort not in args.effort:
            continue
        run, transcript, artifact = build_trial(trial, args.muse_binary)
        if any(item[1]["id"] == run["id"] for item in built):
            raise ValueError("Multiple successful generations for one effort")
        grade = select_grade(trial, args.grade_job)
        if grade != trial:
            if native_artifact(grade).read_bytes() != artifact.read_bytes():
                raise ValueError("Native regrade changed the original artifact")
            run["usage"]["raw"]["harborGradeTrial"] = grade.name
            run["notes"] += (
                " Scores come from a native Harbor regrade of the saved artifact; generation was not repeated."
            )
        destination = ROOT / run["id"]
        if (destination / "index.html").exists() and (
            destination / "index.html"
        ).read_bytes() != artifact.read_bytes():
            raise ValueError("Refusing to replace an immutable benchmark artifact")
        if args.check_sources:
            for name, expected in (("run.json", run), ("transcript.json", transcript)):
                if read_json(destination / name) != expected:
                    raise ValueError(
                        f"{run['id']}/{name} differs from its native source"
                    )
        elif not args.dry_run:
            destination.mkdir(parents=True, exist_ok=True)
            if not (destination / "index.html").exists():
                (destination / "index.html").write_bytes(artifact.read_bytes())
            write_json(destination / "run.json", run, False)
            write_json(destination / "transcript.json", transcript, False)
        built.append((grade, run, artifact))
        print(
            f"Verified {run['id']} against native Muse session and exact final writes"
        )
    if not built:
        raise ValueError("No completed native Muse trials found")
    if not args.dry_run and not args.check_sources:
        register([r for _, r, _ in built])
        if args.publish_scores:
            publish_grades(built, ROOT)
        if censor_transcripts(["--write", str(ROOT / "meta")]):
            raise ValueError("Public Muse censor failed")
    return 0
