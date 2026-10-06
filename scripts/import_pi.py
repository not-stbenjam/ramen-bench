#!/usr/bin/env python3
"""Import native Harbor Pi sessions without changing generated HTML."""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from backfill_session_metadata import (
    PRIVATE_TOOL_RE,
    ROOT,
    content_text,
    public_user_text,
    read_jsonl,
    replay_codex_command,
    result_stats,
    scrub,
    transcript_base,
    write_json,
)
from censor_transcripts import main as censor_transcripts
from import_harbor_codex import native_artifact, publish_grades, read_json


def normalize_session(rows: list[dict]) -> tuple[list[dict], int]:
    events = []
    published_calls = set()
    omitted_calls = set()
    for row in rows:
        if row.get("type") != "message":
            continue
        message = row["message"]
        first_event = len(events)
        role = message.get("role")
        timestamp = row.get("timestamp")
        if role in ("user", "assistant"):
            content = message.get("content", [])
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            text = content_text(
                [block for block in content if block.get("type") == "text"]
            )
            if role == "user":
                text = public_user_text(text, preserve_task=True)
            if text:
                events.append({"type": "message", "role": role, "content": scrub(text)})
            for block in content:
                if block.get("type") != "toolCall":
                    continue
                call_id, tool = block["id"], block["name"]
                arguments = block.get("arguments", {})
                if PRIVATE_TOOL_RE.search(tool) or PRIVATE_TOOL_RE.search(
                    str(arguments)
                ):
                    omitted_calls.add(call_id)
                    continue
                published_calls.add(call_id)
                events.append(
                    {
                        "type": "tool_call",
                        "id": call_id,
                        "tool": tool,
                        "input": scrub(arguments),
                    }
                )
        elif role == "toolResult" and message.get("toolCallId") in published_calls:
            events.append(
                {
                    "type": "tool_result",
                    "callId": message["toolCallId"],
                    "status": "error" if message.get("isError") else "success",
                    "output": scrub(message.get("content", [])),
                }
            )
        if timestamp:
            # All events emitted for this row have its actual recorded timestamp.
            for event in events[first_event:]:
                event["timestamp"] = timestamp
    if not any(event.get("role") == "user" for event in events):
        raise ValueError("Pi session contains no recorded public user message")
    return events, len(omitted_calls)


def verify_artifact(rows: list[dict], artifact: Path, run_id: str) -> str:
    operations = []
    for row in rows:
        message = row.get("message", {})
        if message.get("role") != "assistant":
            continue
        for block in message.get("content", []):
            if block.get("type") != "toolCall":
                continue
            name, arguments = block["name"], block.get("arguments", {})
            if name == "write" and arguments.get("path") in (
                "index.html",
                "/app/index.html",
            ):
                # Direct writes are recorded tool input, not reconstructed model text.
                operations.append({"direct_write": arguments["content"]})
            elif name == "bash":
                operations.append(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "exec_command",
                            "arguments": json.dumps({"cmd": arguments["command"]}),
                        },
                    }
                )
            elif name == "edit" and arguments.get("path") in (
                "index.html",
                "/app/index.html",
            ):
                operations.append({"direct_edit": arguments})
    content = None
    mutations = 0
    for operation in operations:
        if "direct_write" in operation:
            content = operation["direct_write"]
            mutations += 1
        elif "direct_edit" in operation:
            edit = operation["direct_edit"]
            if content is None or content.count(edit["oldText"]) != 1:
                raise ValueError(
                    "Pi edit does not match exactly one recorded source range"
                )
            content = content.replace(edit["oldText"], edit["newText"], 1)
            mutations += 1
        else:
            arguments = json.loads(operation["payload"]["arguments"])
            content, count = replay_codex_command(content, arguments["cmd"], run_id)
            mutations += count
    if content is None or content != artifact.read_text():
        raise ValueError("Pi artifact differs from its recorded final writes")
    return f"Recorded Pi file-operation replay ({mutations} operations)"


def session_usage(rows: list[dict], result: dict, stdout: Path) -> dict:
    messages = [
        row["message"]
        for row in rows
        if row.get("type") == "message" and row["message"].get("role") == "assistant"
    ]
    categories = {
        "input": "input",
        "output": "output",
        "cachedInput": "cacheRead",
        "cacheCreationInput": "cacheWrite",
    }
    tokens = {}
    for message in messages:
        usage = message.get("usage", {})
        if usage.get("totalTokens") != sum(
            usage.get(key, 0) for key in categories.values()
        ):
            raise ValueError("Recorded Pi token total is inconsistent")
    for name, key in categories.items():
        values = [message.get("usage", {}).get(key) for message in messages]
        if not values or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in values
        ):
            raise ValueError(f"Missing recorded Pi token category: {key}")
        tokens[name] = sum(values)
    # Pi input excludes cache hits; Harbor input includes them.
    tokens["input"] += tokens["cachedInput"]
    tokens["total"] = tokens["input"] + tokens["cacheCreationInput"] + tokens["output"]
    if all("reasoning" in message.get("usage", {}) for message in messages):
        reasoning = [message["usage"]["reasoning"] for message in messages]
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or value > message["usage"]["output"]
            for value, message in zip(reasoning, messages, strict=True)
        ):
            raise ValueError("Invalid recorded Pi reasoning token category")
        tokens["reasoning"] = sum(reasoning)
    context = result["agent_result"]
    for key, name in (
        ("n_input_tokens", "input"),
        ("n_cache_tokens", "cachedInput"),
        ("n_output_tokens", "output"),
    ):
        if context.get(key) != tokens[name]:
            raise ValueError(f"Pi session usage disagrees with Harbor: {key}")
    # Require Harbor's actual stdout usage events to agree with the session.
    reported = []
    for line in stdout.read_text().splitlines():
        if not line.startswith("{"):
            continue
        event = json.loads(line)
        if (
            event.get("type") == "message_end"
            and event.get("message", {}).get("role") == "assistant"
        ):
            reported.append(event["message"])
    if [message.get("usage") for message in reported] != [
        message.get("usage") for message in messages
    ]:
        raise ValueError("Pi stdout and session usage disagree")
    cost = context.get("cost_usd")
    recorded_cost = sum(
        message.get("usage", {}).get("cost", {}).get("total", 0) for message in messages
    )
    if cost is not None and (
        isinstance(cost, bool)
        or not math.isfinite(cost)
        or cost < 0
        or not math.isclose(cost, recorded_cost, abs_tol=1e-12)
    ):
        raise ValueError("Pi cost disagrees with native Harbor accounting")
    return {
        "tokens": tokens,
        "cost": None
        if cost is None
        else {"currency": "USD", "total": cost, "estimated": True},
        "providerReported": True,
        "raw": {
            "harborAgentContext": context,
            "piUsage": [message["usage"] for message in messages],
            "inputIncludesCachedInput": True,
        },
    }


def refresh_provider_costs(trial: Path, env_file: Path) -> None:
    """Save original provider responses privately; subsequent imports are offline."""
    from dotenv import dotenv_values

    key = dotenv_values(env_file).get("OPENROUTER_API_KEY")
    if not key:
        raise ValueError("Missing OpenRouter credential for provider charge lookup")
    sources = list((trial / "agent/pi/sessions").rglob("*.jsonl"))
    if len(sources) != 1:
        raise ValueError("Expected one Pi session for charge lookup")
    ids = {
        row["message"]["responseId"]
        for row in read_jsonl(sources[0])
        if row.get("type") == "message"
        and row["message"].get("role") == "assistant"
        and row["message"].get("responseId")
    }
    for generation_id in sorted(ids):
        if not generation_id.startswith("gen-") or any(
            char
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"
            for char in generation_id
        ):
            raise ValueError("Invalid recorded OpenRouter generation identifier")
        destination = trial / f"provider-generation-{generation_id}.json"
        if destination.exists():
            continue
        request = Request(
            "https://openrouter.ai/api/v1/generation?"
            + urlencode({"id": generation_id}),
            headers={"Authorization": f"Bearer {key}"},
        )
        with urlopen(request, timeout=30) as response:
            data = json.load(response)["data"]
        if data.get("id") != generation_id:
            raise ValueError("OpenRouter returned an unexpected generation record")
        destination.write_text(json.dumps(data, indent=2) + "\n")
        destination.chmod(0o600)


def provider_cost(
    trial: Path, rows: list[dict], session_id: str
) -> tuple[dict | None, list[dict]]:
    messages = [
        row["message"]
        for row in rows
        if row.get("type") == "message" and row["message"].get("role") == "assistant"
    ]
    ids = [message.get("responseId") for message in messages]
    if any(not generation_id for generation_id in ids) or len(set(ids)) != len(ids):
        return None, []
    records = []
    for generation_id in ids:
        if not generation_id.startswith("gen-") or any(
            char
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"
            for char in generation_id
        ):
            raise ValueError("Invalid recorded OpenRouter generation identifier")
        path = trial / f"provider-generation-{generation_id}.json"
        if not path.is_file():
            return None, []
        data = read_json(path)
        if (
            data.get("id") != generation_id
            or data.get("session_id") != session_id
            or data.get("model") != "mistralai/mistral-large-4-0-20261006"
        ):
            raise ValueError("OpenRouter charge does not match this Pi session/model")
        cost = data.get("total_cost")
        if (
            isinstance(cost, bool)
            or not isinstance(cost, (int, float))
            or not math.isfinite(cost)
            or cost < 0
        ):
            raise ValueError("Invalid recorded OpenRouter generation charge")
        records.append(
            {
                key: data[key]
                for key in (
                    "id",
                    "model",
                    "created_at",
                    "total_cost",
                    "native_tokens_prompt",
                    "native_tokens_completion",
                    "native_tokens_cached",
                    "finish_reason",
                )
                if key in data
            }
        )
    return {
        "currency": "USD",
        "total": sum(record["total_cost"] for record in records),
        "estimated": False,
    }, records


def generation_outcome(rows: list[dict]) -> tuple[str, list[dict]]:
    messages = [row["message"] for row in rows if row.get("type") == "message"]
    assistants = [message for message in messages if message.get("role") == "assistant"]
    if not assistants:
        raise ValueError("Pi session contains no assistant generation")
    failures = [
        {
            key: message[key]
            for key in ("stopReason", "responseId", "errorMessage")
            if key in message
        }
        for message in assistants
        if message.get("stopReason") == "error"
    ]
    if assistants[-1].get("stopReason") == "stop":
        return "completed", failures
    if assistants[-1].get("stopReason") != "error":
        raise ValueError("Pi session has no recorded final outcome")
    file_calls = {
        block["id"]
        for message in assistants
        for block in message.get("content", [])
        if block.get("type") == "toolCall"
        and block.get("name") in ("write", "edit")
        and block.get("arguments", {}).get("path") in ("index.html", "/app/index.html")
    }
    if not any(
        message.get("role") == "toolResult"
        and message.get("toolCallId") in file_calls
        and message.get("isError") is False
        for message in messages
    ):
        raise ValueError(
            "Cannot retain an API-failed run without a recorded successful artifact write"
        )
    return "artifact-written-final-api-error", failures


def build_trial(trial: Path) -> tuple[dict, dict, Path]:
    result = read_json(trial / "result.json")
    if result.get("exception_info") or not result.get("agent_result"):
        raise ValueError("Cannot import an unsuccessful Pi trial")
    config = read_json(trial / "config.json")["agent"]
    if (
        config["name"] != "pi"
        or config["model_name"] != "openrouter/mistralai/mistral-large-4-0"
    ):
        raise ValueError("Unsupported Pi model or harness")
    info = result["agent_info"]
    if info["name"] != "pi" or info["version"] != config["kwargs"]["version"]:
        raise ValueError(
            "Recorded Pi harness or version differs from the requested agent"
        )
    if config.get("kwargs", {}).get("thinking") is not None:
        raise ValueError(
            "Importing this recorded default run requires no effort override"
        )
    sources = sorted((trial / "agent/pi/sessions").rglob("*.jsonl"))
    if len(sources) != 1:
        raise ValueError("Expected exactly one recorded Pi session")
    source = sources[0]
    rows = read_jsonl(source)
    header = rows[0]
    if (
        header.get("type") != "session"
        or header.get("version") != 3
        or header.get("cwd") != "/app"
    ):
        raise ValueError("Pi session has an unexpected working directory or header")
    model_changes = [row for row in rows if row.get("type") == "model_change"]
    if not model_changes or any(
        row.get("provider") != "openrouter"
        or row.get("modelId") != "mistralai/mistral-large-4-0"
        for row in model_changes
    ):
        raise ValueError("Recorded Pi model differs from the requested model")
    assistants = [
        row["message"]
        for row in rows
        if row.get("type") == "message" and row["message"].get("role") == "assistant"
    ]
    outcome, failures = generation_outcome(rows)
    if any(
        message.get("model") != "mistralai/mistral-large-4-0"
        or message.get("provider") != "openrouter"
        for message in assistants
    ):
        raise ValueError("Assistant responses came from an unexpected Pi model")
    thinking = [
        row["thinkingLevel"]
        for row in rows
        if row.get("type") == "thinking_level_change"
    ]
    run_id = "mistralai/mistral-large-4-0/default"
    artifact = native_artifact(trial)
    verification = verify_artifact(rows, artifact, run_id)
    events, omitted = normalize_session(rows)
    usage = session_usage(rows, result, trial / "agent/pi.txt")
    native_cost = usage["cost"]
    usage["cost"], charges = provider_cost(trial, rows, header["id"])
    usage["raw"].update(
        harborPiCatalogCost=native_cost,
        openrouterGenerations=charges,
        generationOutcome=outcome,
        recordedApiFailures=failures,
        costNote="Pi's catalog did not recognize this new model. Its fallback estimate is retained only as native accounting; the displayed cost is the sum of OpenRouter's recorded generation charges, or unknown when those records are incomplete.",
    )
    usage["toolCalls"] = sum(event["type"] == "tool_call" for event in events)
    usage["raw"].update(
        harborTrial=trial.name,
        harborTaskChecksum=result["task_checksum"],
        artifactVerification=verification,
    )
    execution = result["agent_execution"]
    start, finish = (
        datetime.fromisoformat(execution[key]) for key in ("started_at", "finished_at")
    )
    if start.tzinfo is None or finish.tzinfo is None or finish < start:
        raise ValueError("Invalid native Pi generation timestamps")
    run = {
        "$schema": "../../../schemas/run.schema.json",
        "schemaVersion": 1,
        "id": run_id,
        "vendor": {"slug": "mistralai", "displayName": "Mistral"},
        "model": {
            "slug": "mistral-large-4-0",
            "displayName": "Mistral Large 4",
            "providerModelId": config["model_name"],
        },
        "variation": {
            "slug": "default",
            "displayName": "Default",
            "reasoningEffort": "other",
            "parameters": {
                "requestedThinking": None,
                "recordedPiThinkingLevels": thinking,
            },
        },
        "harness": {
            "name": "Pi",
            "version": result["agent_info"]["version"],
            "runtime": "Harbor",
            "invocation": "Native Harbor Pi agent; OpenRouter mistralai/mistral-large-4-0; no reasoning-effort override",
        },
        "artifacts": {"result": "index.html", "transcript": "transcript.json"},
        "timing": {
            "startedAt": execution["started_at"],
            "completedAt": execution["finished_at"],
            "wallDurationMs": round((finish - start).total_seconds() * 1000),
        },
        "usage": usage,
        "resultStats": result_stats(run_id, artifact),
        "provenance": {
            "pricingSource": "OpenRouter generation records total_cost, matched to recorded Pi responseId and session ID; no independent pricing calculation"
        },
        "notes": "Generated by Pi through native Harbor and OpenRouter. Artifact verified against recorded final writes. Tokens and duration retain recorded session and Harbor accounting. Cost uses OpenRouter's recorded charges; the unsupported Pi catalog estimate is retained separately. No reasoning-effort control was advertised by the OpenRouter endpoint at generation time. Mistral's native API supports none/high; this run requested neither. Pi's internal thinking setting is recorded separately from the requested provider configuration."
        + (
            " The artifact write succeeded, but Pi's final follow-up API requests failed after retries. Those recorded failures are retained; no final assistant completion was invented."
            if outcome != "completed"
            else ""
        ),
    }
    transcript = transcript_base(
        run_id, header["id"], "harbor-pi-jsonl", source, events, omitted
    )
    return run, transcript, artifact


def register_run(root: Path, run: dict) -> None:
    path = root / "registry.json"
    registry = read_json(path)
    entry = f"{run['id']}/run.json"
    if entry not in registry["runs"]:
        registry["runs"].append(entry)
    registry.setdefault("modelAddedAt", {}).setdefault(
        "mistralai/mistral-large-4-0",
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )
    path.write_text(json.dumps(registry, indent=2) + "\n")
    path = root / "harbor/human-scores.json"
    panel = read_json(path)
    if not any(row["runId"] == run["id"] for row in panel["ratings"]):
        panel["ratings"].append({"runId": run["id"], "humanScore": None})
    path.write_text(json.dumps(panel, indent=2, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harbor-job", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--publish-scores", action="store_true")
    parser.add_argument("--refresh-provider-costs", action="store_true")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".harbor/judge.env")
    args = parser.parse_args()
    trials = [path.parent for path in args.harbor_job.glob("*/result.json")]
    if args.refresh_provider_costs:
        for trial in trials:
            refresh_provider_costs(trial, args.env_file)
    built = [build_trial(trial) for trial in trials]
    if len(built) != 1:
        raise ValueError("Expected one completed Pi generation")
    for trial, (run, transcript, artifact) in zip(trials, built, strict=True):
        destination = ROOT / run["id"]
        target = destination / "index.html"
        if target.exists() and target.read_bytes() != artifact.read_bytes():
            raise ValueError("Refusing to overwrite an immutable model artifact")
        if not args.dry_run:
            destination.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_bytes(artifact.read_bytes())
        write_json(destination / "run.json", run, args.dry_run)
        write_json(destination / "transcript.json", transcript, args.dry_run)
        if not args.dry_run:
            register_run(ROOT, run)
            censor_transcripts(["--write", str(destination)])
            if args.publish_scores:
                publish_grades([(trial, run, artifact)], ROOT)
        print(f"Verified {run['id']} against its recorded Pi session")
    return censor_transcripts([str(ROOT / "mistralai")]) if not args.dry_run else 0


if __name__ == "__main__":
    raise SystemExit(main())
