#!/usr/bin/env python3
"""Build public Muse benchmark metadata from redacted Muse Code exports."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from typing import Any

import litellm
from jsonschema import Draft202012Validator, FormatChecker


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from censor_transcripts import censor_text, censor_value  # noqa: E402

MODEL_KEY = "meta/muse-spark-1.3-contributor"
PROVIDER_MODEL_ID = "muse-spark-1.3-contributor"
SESSIONS = {
    "low": "01a06f27-d350-7d90-8b0f-6a71f5f8108a",
    "medium": "01a06f27-fb5e-7533-9139-e3b452948ff5",
    "high": "01a06f27-86d4-70b2-b7e5-c210cd0bd54d",
    "xhigh": "01a06f28-1f9e-7873-80bd-e9a2bc553b82",
}
EFFORT_NAMES = {
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "XHigh",
}
WORKSPACE_ROOT = str(Path(__file__).resolve().parents[2])
HOME_ROOT = str(Path.home())


def iso_from_micros(value: int) -> str:
    return datetime.fromtimestamp(value / 1_000_000, UTC).isoformat().replace(
        "+00:00", "Z"
    )


def public_string(value: str) -> str:
    return censor_text(
        value,
        workspace=WORKSPACE_ROOT,
        home=HOME_ROOT,
    )


def normalize_value(value: Any, key: str | None = None) -> Any:
    return censor_value(
        value,
        workspace=WORKSPACE_ROOT,
        home=HOME_ROOT,
        key=key,
    )


def normalize_tool_input(raw_args: Any) -> Any:
    if isinstance(raw_args, str):
        try:
            value = json.loads(raw_args)
        except json.JSONDecodeError:
            value = raw_args
    else:
        value = raw_args

    return normalize_value(value)


def normalize_tool_output(raw_text: Any) -> tuple[Any, str]:
    if not isinstance(raw_text, str):
        return normalize_value(raw_text), "success"

    try:
        value = json.loads(raw_text)
    except json.JSONDecodeError:
        value = public_string(raw_text)
        status = "error" if raw_text.lstrip().lower().startswith("error") else "success"
        return value, status

    value = normalize_value(value)
    if isinstance(value, dict):
        failed = value.get("terminal_status") == "failed" or (
            isinstance(value.get("exit_code"), int) and value["exit_code"] != 0
        )
        return value, "error" if failed else "success"
    return value, "success"


def event_payload(record: dict[str, Any]) -> dict[str, Any]:
    return record.get("envelope", {}).get("payload", {}).get("event", {})


def event_timestamp(record: dict[str, Any]) -> str:
    micros = record.get("envelope", {}).get("recorded_at") or record.get("recorded_at")
    if not isinstance(micros, int):
        raise ValueError("public event is missing its recorded timestamp")
    return iso_from_micros(micros)


def build_transcript(export: dict[str, Any], run_id: str, effort: str) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    pending_batches: list[list[str]] = []
    tool_number = 0

    for record in export["events"]:
        envelope = record.get("envelope", {})
        payload = envelope.get("payload", {})
        payload_type = envelope.get("payload_type")
        event = payload.get("event", {})
        kind = event.get("kind")

        if payload_type == "runtime.user_intent.accepted":
            for block in payload.get("refill_blocks", []):
                if block.get("kind") == "text" and isinstance(block.get("text"), str):
                    events.append(
                        {
                            "type": "message",
                            "role": "user",
                            "content": public_string(block["text"]),
                            "timestamp": event_timestamp(record),
                        }
                    )
            continue

        if kind == "assistant_message_committed" and isinstance(event.get("text"), str):
            events.append(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": public_string(event["text"]),
                    "timestamp": event_timestamp(record),
                }
            )
            continue

        if kind == "assistant_tool_calls_committed":
            batch: list[str] = []
            for call in event.get("tool_calls", []):
                tool_number += 1
                public_id = f"muse-tool-{tool_number:03d}"
                batch.append(public_id)
                tool = call.get("name", "unknown")
                events.append(
                    {
                        "type": "tool_call",
                        "id": public_id,
                        "tool": tool,
                        "input": normalize_tool_input(call.get("args")),
                        "timestamp": event_timestamp(record),
                    }
                )
            pending_batches.append(batch)
            continue

        if kind == "tool_result_batch_committed":
            batch = pending_batches.pop(0) if pending_batches else []
            for index, result in enumerate(event.get("results", [])):
                call_index = result.get("tool_call_index", index)
                if isinstance(call_index, int) and call_index < len(batch):
                    call_id = batch[call_index]
                else:
                    call_id = f"muse-tool-unmatched-{index + 1:03d}"
                output, status = normalize_tool_output(result.get("text"))
                events.append(
                    {
                        "type": "tool_result",
                        "callId": call_id,
                        "status": status,
                        "output": output,
                        "timestamp": event_timestamp(record),
                    }
                )

    session = export["sessions"][0]
    return {
        "$schema": "../../../schemas/transcript.schema.json",
        "schemaVersion": 1,
        "runId": run_id,
        "sessionId": session["session_id"],
        "sourceFormat": "Muse Code redacted export_schema_version 1",
        "events": events,
        "metadata": {
            "providerModelId": PROVIDER_MODEL_ID,
            "reasoningEffort": effort,
            "harnessVersion": export["session_build"]["display"],
            "redaction": export["redaction"],
            "sourceToolCallIds": "redacted by Muse; public IDs assigned in recorded order",
        },
    }


def sum_usage(export: dict[str, Any]) -> tuple[dict[str, int], int, int]:
    totals = {
        "input_tokens": 0,
        "cached_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
    }
    requests = 0
    api_duration_ms = 0
    for record in export["events"]:
        event = event_payload(record)
        if event.get("kind") != "model_completed":
            continue
        requests += 1
        api_duration_ms += event.get("duration_ms", 0)
        usage = event.get("usage", {})
        for key in totals:
            totals[key] += usage.get(key, 0)
    return totals, requests, api_duration_ms


def terminal_event(export: dict[str, Any]) -> tuple[dict[str, Any], int]:
    terminals = [
        record
        for record in export["events"]
        if event_payload(record).get("kind") == "terminal"
    ]
    if not terminals:
        raise ValueError("export has no completed terminal event")
    record = terminals[-1]
    event = event_payload(record)
    if event.get("terminal") != "completed":
        raise ValueError(f"session terminal state is {event.get('terminal')!r}")
    return event, record["envelope"]["recorded_at"]


def git_head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def build_run(
    export: dict[str, Any], run_id: str, effort: str, result_path: Path, repo: Path
) -> dict[str, Any]:
    totals, requests, api_duration_ms = sum_usage(export)
    terminal, completed_micros = terminal_event(export)
    wall_duration_ms = terminal["turn_duration_ms"]
    completed = datetime.fromtimestamp(completed_micros / 1_000_000, UTC)
    started = completed - timedelta(milliseconds=wall_duration_ms)

    cached = totals["cache_read_tokens"]
    uncached = totals["input_tokens"] - cached
    output = totals["output_tokens"]
    reasoning = totals["reasoning_tokens"]
    if uncached < 0:
        raise ValueError("cached input exceeds provider-reported input")

    pricing = litellm.model_cost[MODEL_KEY]
    input_rate = pricing["input_cost_per_token"]
    cached_rate = pricing["cache_read_input_token_cost"]
    output_rate = pricing["output_cost_per_token"]
    litellm_input, litellm_output = litellm.cost_per_token(
        model=PROVIDER_MODEL_ID,
        custom_llm_provider="meta",
        prompt_tokens=totals["input_tokens"],
        cache_read_input_tokens=cached,
        completion_tokens=output + reasoning,
    )
    input_cost = uncached * input_rate
    cached_cost = cached * cached_rate
    output_cost = (output + reasoning) * output_rate
    if abs(litellm_input - (input_cost + cached_cost)) > 1e-12:
        raise ValueError("LiteLLM input cost disagrees with its published rates")
    if abs(litellm_output - output_cost) > 1e-12:
        raise ValueError("LiteLLM output cost disagrees with its published rate")

    result_text = result_path.read_text()
    session = export["sessions"][0]
    tool_calls = sum(
        len(event_payload(record).get("tool_calls", []))
        for record in export["events"]
        if event_payload(record).get("kind") == "assistant_tool_calls_committed"
    )
    generated_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    return {
        "$schema": "../../../schemas/run.schema.json",
        "schemaVersion": 1,
        "id": run_id,
        "vendor": {"slug": "meta", "displayName": "Meta"},
        "model": {
            "slug": "muse-spark-1.3",
            "displayName": "Muse Spark 1.3",
            "providerModelId": PROVIDER_MODEL_ID,
        },
        "variation": {
            "slug": effort,
            "displayName": EFFORT_NAMES[effort],
            "reasoningEffort": effort,
        },
        "harness": {
            "name": "Muse Code",
            "version": export["session_build"]["display"].removeprefix("Muse Code "),
        },
        "artifacts": {"result": "index.html", "transcript": "transcript.json"},
        "timing": {
            "startedAt": started.isoformat().replace("+00:00", "Z"),
            "completedAt": completed.isoformat().replace("+00:00", "Z"),
            "wallDurationMs": wall_duration_ms,
            "apiDurationMs": api_duration_ms,
        },
        "usage": {
            "tokens": {
                "input": uncached,
                "cachedInput": cached,
                "cacheCreationInput": totals["cache_write_tokens"],
                "reasoning": reasoning,
                "output": output,
                "total": uncached + cached + totals["cache_write_tokens"] + reasoning + output,
            },
            "cost": {
                "currency": "USD",
                "input": round(input_cost, 12),
                "cachedInput": round(cached_cost, 12),
                "cacheCreationInput": 0,
                "output": round(output_cost, 12),
                "other": 0,
                "total": round(litellm_input + litellm_output, 12),
                "estimated": True,
            },
            "requests": requests,
            "turns": session["turn_count"],
            "toolCalls": tool_calls,
            "providerReported": True,
            "raw": {
                "modelCompletedTotals": totals,
                "terminal": terminal,
                "pricing": {
                    "library": "LiteLLM",
                    "version": version("litellm"),
                    "modelKey": MODEL_KEY,
                    "inputCostPerToken": input_rate,
                    "cachedInputCostPerToken": cached_rate,
                    "outputCostPerToken": output_rate,
                    "reasoningCostPerToken": output_rate,
                },
            },
        },
        "resultStats": {
            "bytes": result_path.stat().st_size,
            "lines": len(result_text.splitlines()),
        },
        "provenance": {
            "generatedAt": generated_at,
            "sourceCommit": git_head(repo),
            "pricingSource": f"LiteLLM {version('litellm')} model_cost[{MODEL_KEY!r}]",
        },
        "notes": (
            "Token counts and durations are exact sums of Muse Code model_completed "
            "events and the completed terminal event. Provider input includes cache hits; "
            "usage.tokens.input is the uncached remainder. Estimated cost uses LiteLLM's "
            "recorded contributor rates, with reasoning tokens charged at the output rate."
        ),
    }


def validate(instance: dict[str, Any], schema_path: Path) -> None:
    schema = json.loads(schema_path.read_text())
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(instance), key=lambda error: list(error.path))
    if errors:
        details = "\n".join(f"- {list(error.path)}: {error.message}" for error in errors)
        raise ValueError(f"{schema_path.name} validation failed:\n{details}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "exports",
        type=Path,
        help="directory containing ramen-<session-id>-redacted.json exports",
    )
    args = parser.parse_args()

    model_dir = Path(__file__).resolve().parent
    repo = model_dir.parents[1]
    run_schema = repo / "schemas" / "run.schema.json"
    transcript_schema = repo / "schemas" / "transcript.schema.json"

    for effort, session_id in SESSIONS.items():
        export_path = args.exports / f"ramen-{session_id}-redacted.json"
        export = json.loads(export_path.read_text())
        if export.get("redaction") != "redacted":
            raise ValueError(f"refusing non-redacted export: {export_path}")
        if export["sessions"][0]["session_id"] != session_id:
            raise ValueError(f"session mismatch in {export_path}")

        effort_dir = model_dir / effort
        run_id = f"meta/muse-spark-1.3/{effort}"
        transcript = build_transcript(export, run_id, effort)
        run = build_run(export, run_id, effort, effort_dir / "index.html", repo)
        validate(transcript, transcript_schema)
        validate(run, run_schema)
        (effort_dir / "transcript.json").write_text(
            json.dumps(transcript, indent=2, ensure_ascii=False) + "\n"
        )
        (effort_dir / "run.json").write_text(
            json.dumps(run, indent=2, ensure_ascii=False) + "\n"
        )


if __name__ == "__main__":
    main()
