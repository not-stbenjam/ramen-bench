#!/usr/bin/env python3
"""Backfill public benchmark metadata from local Claude Code and Codex records.

The generated transcripts include recorded user, assistant, and tool activity only.
System/developer messages, hidden reasoning, credentials, and private path details are
excluded; complete non-sensitive recorded tool payloads are retained.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any, Iterable

import litellm
from jsonschema import Draft202012Validator, FormatChecker

from censor_transcripts import censor_text, censor_value


ROOT = Path(__file__).resolve().parents[1]
HOME = Path.home()
RUN_SCHEMA_URL = "https://not-stbenjam.github.io/ramen-bench/schemas/run.schema.json"
TRANSCRIPT_SCHEMA_URL = (
    "https://not-stbenjam.github.io/ramen-bench/schemas/transcript.schema.json"
)
EXPECTED_LITELLM_VERSION = "1.83.7"
PROMPT = (
    "Create a single-file HTML page (index.html) with all CSS and JavaScript inline "
    "and zero external assets. Render a dark, atmospheric hero scene featuring a "
    "single, steaming bowl of vegan ramen as the sole visual focus. Make the dish feel "
    "alive and appetizing — with natural motion, ambient warmth, and interactive "
    "response to the mouse. Keep the scene pure (no UI, text, or menus), responsive "
    "on all screens, and respectful of prefers-reduced-motion."
)

DISPLAY_NAMES = {
    "gpt-6-astra": "GPT 6 Astra",
    "gpt-5.6-sol": "GPT 5.6 Sol",
    "gpt-5.6-terra": "GPT 5.6 Terra",
    "gpt-5.6-luna": "GPT 5.6 Luna",
    "gpt-5.5": "GPT 5.5",
    "fable-5.1": "Fable 5.1",
    "opus-5": "Opus 5",
}
EFFORT_NAMES = {
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "XHigh",
    "max": "Max",
    "ultra": "Ultra",
}

CLAUDE_SOURCES = {
    "anthropic/fable-5.1/low": "7a98bbf8-3d05-488f-adcf-f5885320142e.jsonl",
    "anthropic/fable-5.1/medium": "8f4c534d-bc6b-4bad-b7cb-62035065f6b6.jsonl",
    "anthropic/fable-5.1/high": "146c3cb5-8250-43fd-8ad2-4fc433d907af.jsonl",
    "anthropic/fable-5.1/xhigh": "34bad787-8505-463b-a158-23c7142dc418.jsonl",
    "anthropic/opus-5/low": "53ffbdbc-fb75-4f0e-aa78-84867e0139b6.jsonl",
    "anthropic/opus-5/medium": "c5d5dbe1-70df-40aa-bbf3-f5716a922400.jsonl",
    "anthropic/opus-5/high": "4920bb88-dd70-4834-9840-7ddcd388c369.jsonl",
    "anthropic/opus-5/xhigh": "f7517ad6-13ef-4b03-aa98-afd1229c781d.jsonl",
    "anthropic/opus-5/max": "0cef914e-533c-4793-972f-95e098e24cd9.jsonl",
}

CODEX_SESSION_SOURCES = {
    "openai/gpt-5.6-luna/max": (
        "2026/09/02",
        "rollout-2026-09-02T17-56-12-01a0641f-5e5a-7280-82cc-af4372cb1a35.jsonl",
    ),
    "openai/gpt-6-astra/low": (
        "2026/09/04",
        "rollout-2026-09-04T17-46-36-01a06e63-4a74-74e2-b345-cc38bc601a87.jsonl",
    ),
    "openai/gpt-6-astra/medium": (
        "2026/09/04",
        "rollout-2026-09-04T17-47-13-01a06e63-dd0f-7af3-ae3a-faffc288e806.jsonl",
    ),
    "openai/gpt-6-astra/high": (
        "2026/09/04",
        "rollout-2026-09-04T17-47-02-01a06e63-b049-7de1-83e1-b76527726f86.jsonl",
    ),
    "openai/gpt-6-astra/xhigh": (
        "2026/09/04",
        "rollout-2026-09-04T17-45-15-01a06e62-0eb4-7cc2-a135-006d65b23d48.jsonl",
    ),
    "openai/gpt-6-astra/max": (
        "2026/09/04",
        "rollout-2026-09-04T17-47-26-01a06e64-0e07-7793-9a85-7e983f733c88.jsonl",
    ),
    # This retry performed the final write at 22:10:12.013Z, matching index.html's
    # 22:10:12.091Z mtime. The two other concurrent retries are not combined with it.
    "openai/gpt-6-astra/ultra": (
        "2026/09/04",
        "rollout-2026-09-04T17-47-38-01a06e64-3cc2-7683-9899-b23ee13ada47.jsonl",
    ),
}

LOG_STARTS = {
    "openai/gpt-5.6-luna/low": "2026-09-04T22:12:22.504Z",
    "openai/gpt-5.6-luna/medium": "2026-09-04T22:12:22.504Z",
    "openai/gpt-5.6-luna/high": "2026-09-04T22:12:22.504Z",
    "openai/gpt-5.6-luna/xhigh": "2026-09-04T22:12:22.504Z",
    "openai/gpt-5.6-terra/medium": "2026-09-04T22:12:22.504Z",
    "openai/gpt-5.6-terra/high": "2026-09-04T22:17:00.762Z",
    "openai/gpt-5.6-terra/xhigh": "2026-09-04T22:17:00.762Z",
    "openai/gpt-5.6-terra/max": "2026-09-04T22:17:00.762Z",
    "openai/gpt-5.6-terra/ultra": "2026-09-04T22:17:00.762Z",
    "openai/gpt-5.6-sol/low": "2026-09-04T22:17:00.762Z",
    "openai/gpt-5.6-sol/medium": "2026-09-04T22:23:38.976Z",
    "openai/gpt-5.6-sol/high": "2026-09-04T22:23:38.976Z",
    "openai/gpt-5.6-sol/xhigh": "2026-09-04T22:23:38.976Z",
    "openai/gpt-5.6-sol/max": "2026-09-04T22:23:38.976Z",
    "openai/gpt-5.6-sol/ultra": "2026-09-04T22:23:38.976Z",
    "openai/gpt-5.5/low": "2026-09-04T22:33:45.905Z",
    "openai/gpt-5.5/medium": "2026-09-04T22:33:45.905Z",
    "openai/gpt-5.5/high": "2026-09-04T22:33:45.905Z",
    "openai/gpt-5.5/xhigh": "2026-09-04T22:33:45.905Z",
}

LOG_NAMES = {
    "gpt-5.6-luna": "luna",
    "gpt-5.6-terra": "terra",
    "gpt-5.6-sol": "sol",
    "gpt-5.5": "gpt55",
}

TERRA_LOW_ID = "openai/gpt-5.6-terra/low"
TERRA_LOW_SESSION_ID = "01a06e79-141b-7dd3-b98d-e97af3ba8d18"
TERRA_LOW_PARENT = (
    HOME
    / ".codex-personal/sessions/2026/09/04/"
    "rollout-2026-09-04T18-06-57-01a06e75-ef17-7e22-9c1c-c0a1227f3d4c.jsonl"
)
TERRA_LOW_STARTED = "2026-09-04T22:09:59.409Z"
TERRA_LOW_COMPLETED = "2026-09-04T22:11:35.604Z"

PRIVATE_TOOL_RE = re.compile(r"(?:agent[_-]?mail|dawnmark)", re.I)
BASE64_MARKER_RE = re.compile(
    r'OMITTED_BINARY_PAYLOAD[^>]+original-characters=\\?"(\d+)\\?"'
)
def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    rows = []
    for line_number, line in enumerate(path.read_text(errors="strict").splitlines(), 1):
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: {exc}") from exc
    return rows


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def timestamp_bounds(rows: Iterable[dict[str, Any]]) -> tuple[str, str]:
    values = [
        row["timestamp"]
        for row in rows
        if isinstance(row.get("timestamp"), str) and row["timestamp"]
    ]
    if not values:
        raise ValueError("source has no timestamps")
    values.sort(key=parse_timestamp)
    return values[0], values[-1]


def wall_duration_ms(started_at: str, completed_at: str) -> int:
    return round(
        (parse_timestamp(completed_at) - parse_timestamp(started_at)).total_seconds()
        * 1000
    )


def scrub_text(value: str) -> str:
    return censor_text(
        value,
        workspace=ROOT,
        home=HOME,
    )


def scrub(value: Any) -> Any:
    return censor_value(
        value,
        workspace=ROOT,
        home=HOME,
    )


def content_text(content: Any) -> str:
    if isinstance(content, str):
        return scrub_text(content)
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") in {
                "text",
                "input_text",
                "output_text",
            }:
                parts.append(item.get("text", ""))
        return scrub_text("\n".join(part for part in parts if part))
    if isinstance(content, dict):
        return scrub_text(json.dumps(scrub(content), ensure_ascii=False))
    return scrub_text(str(content))


def public_user_text(value: str) -> str | None:
    """Return recorded user text after excluding harness-injected context blocks."""
    value = value.strip()
    if not value:
        return None
    private_prefixes = (
        "<environment_context>",
        "<system-reminder>",
        "<command-message>",
        "<local-command",
        "<ide_",
    )
    if value.startswith(private_prefixes):
        return None
    if PROMPT in value:
        return PROMPT
    return scrub_text(value)


def iter_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from iter_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_strings(item)


def source_record(path: Path) -> str:
    return path.name


def litellm_rates(model_key: str) -> dict[str, Any]:
    if model_key not in litellm.model_cost:
        raise KeyError(f"LiteLLM has no pricing entry for {model_key}")
    entry = litellm.model_cost[model_key]
    rates = {
        "input": entry.get("input_cost_per_token"),
        "cachedInput": entry.get("cache_read_input_token_cost"),
        "cacheCreationInput": entry.get("cache_creation_input_token_cost"),
        "output": entry.get("output_cost_per_token"),
    }
    if rates["input"] is None or rates["output"] is None:
        raise ValueError(f"LiteLLM pricing entry is incomplete for {model_key}")
    return {
        "library": "litellm",
        "version": EXPECTED_LITELLM_VERSION,
        "modelKey": model_key,
        "ratesUsdPerToken": rates,
    }


def estimated_cost(
    pricing: dict[str, Any],
    *,
    input_tokens: int,
    cached_input_tokens: int,
    cache_creation_input_tokens: int,
    output_tokens: int,
    input_includes_cached: bool,
) -> dict[str, Any]:
    rates = pricing["ratesUsdPerToken"]
    billable_input = (
        input_tokens - cached_input_tokens if input_includes_cached else input_tokens
    )
    if billable_input < 0:
        raise ValueError("cached input exceeds total input")
    components = {
        "input": billable_input * rates["input"],
        "cachedInput": cached_input_tokens * (rates["cachedInput"] or rates["input"]),
        "cacheCreationInput": cache_creation_input_tokens
        * (rates["cacheCreationInput"] or rates["input"]),
        "output": output_tokens * rates["output"],
    }
    components = {key: round(value, 12) for key, value in components.items()}
    return {
        "currency": "USD",
        **components,
        "total": round(sum(components.values()), 12),
        "estimated": True,
    }


def result_stats(run_id: str) -> dict[str, int]:
    data = (ROOT / run_id / "index.html").read_bytes()
    return {"bytes": len(data), "lines": data.count(b"\n")}


def run_base(
    run_id: str,
    provider_model_id: str,
    harness: dict[str, str],
    timing: dict[str, Any],
    usage: dict[str, Any],
    notes: str,
    pricing_source: str,
) -> dict[str, Any]:
    vendor, model_slug, effort = run_id.split("/")
    parameters: dict[str, Any] = {}
    return {
        "$schema": RUN_SCHEMA_URL,
        "schemaVersion": 1,
        "id": run_id,
        "vendor": {
            "slug": vendor,
            "displayName": "Anthropic" if vendor == "anthropic" else "OpenAI",
        },
        "model": {
            "slug": model_slug,
            "displayName": DISPLAY_NAMES[model_slug],
            "providerModelId": provider_model_id,
        },
        "variation": {
            "slug": effort,
            "displayName": EFFORT_NAMES[effort],
            "reasoningEffort": effort,
            "parameters": parameters,
        },
        "harness": harness,
        "artifacts": {"result": "index.html", "transcript": "transcript.json"},
        "timing": timing,
        "usage": usage,
        "resultStats": result_stats(run_id),
        "provenance": {"pricingSource": pricing_source},
        "notes": notes,
    }


def transcript_base(
    run_id: str,
    session_id: str,
    source_format: str,
    source: Path,
    events: list[dict[str, Any]],
    omitted_tool_calls: int,
) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for event in events:
        counts[event["type"]] = counts.get(event["type"], 0) + 1
    return {
        "$schema": TRANSCRIPT_SCHEMA_URL,
        "schemaVersion": 1,
        "runId": run_id,
        "sessionId": session_id,
        "sourceFormat": source_format,
        "events": events,
        "metadata": {
            "sourceRecord": source_record(source),
            "normalization": {
                "excluded": [
                    "system and developer prompts",
                    "hidden thinking and reasoning",
                    "credentials and private agent-mail calls",
                    "large base64 and embedded image data (event retained with size marker)",
                ],
                "omittedPrivateToolCalls": omitted_tool_calls,
            },
            "eventCounts": counts,
        },
    }


def latest_claude_messages(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest = {}
    for row in rows:
        if row.get("type") == "assistant" and row.get("message", {}).get("id"):
            latest[row["message"]["id"]] = row
    return latest


def normalize_claude_events(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    latest = latest_claude_messages(rows)
    emitted_messages: set[str] = set()
    published_calls: set[str] = set()
    omitted_calls: set[str] = set()
    events: list[dict[str, Any]] = []
    user_message_emitted = False

    for row in rows:
        timestamp = row.get("timestamp")
        if row.get("type") == "assistant":
            message = row.get("message", {})
            message_id = message.get("id")
            if not message_id or latest.get(message_id) is not row or message_id in emitted_messages:
                continue
            emitted_messages.add(message_id)
            for block in message.get("content", []):
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text" and block.get("text"):
                    event = {
                        "type": "message",
                        "role": "assistant",
                        "content": scrub_text(block["text"]),
                    }
                    if timestamp:
                        event["timestamp"] = timestamp
                    events.append(event)
                elif block_type == "tool_use":
                    call_id = str(block.get("id", message_id))
                    tool = str(block.get("name", "tool"))
                    raw_input = block.get("input", {})
                    if PRIVATE_TOOL_RE.search(tool) or PRIVATE_TOOL_RE.search(
                        json.dumps(raw_input, ensure_ascii=False)
                    ):
                        omitted_calls.add(call_id)
                        continue
                    published_calls.add(call_id)
                    event = {
                        "type": "tool_call",
                        "id": call_id,
                        "tool": tool,
                        "input": scrub(raw_input),
                    }
                    if timestamp:
                        event["timestamp"] = timestamp
                    events.append(event)
                # Deliberately ignore thinking and redacted_thinking blocks.
        elif row.get("type") == "user":
            content = row.get("message", {}).get("content", [])
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text = public_user_text(block.get("text", ""))
                    if text:
                        event = {"type": "message", "role": "user", "content": text}
                        if timestamp:
                            event["timestamp"] = timestamp
                        events.append(event)
                        user_message_emitted = True
                elif block.get("type") == "tool_result":
                    call_id = str(block.get("tool_use_id", ""))
                    if not call_id or call_id in omitted_calls or call_id not in published_calls:
                        continue
                    event = {
                        "type": "tool_result",
                        "callId": call_id,
                        "status": "error" if block.get("is_error") else "success",
                        "output": scrub(block.get("content", "")),
                    }
                    if timestamp:
                        event["timestamp"] = timestamp
                    events.append(event)
    if not user_message_emitted:
        raise ValueError("Claude Code transcript does not contain a public user message")
    return events, len(omitted_calls)


def claude_usage(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], str, str]:
    latest_messages = list(latest_claude_messages(rows).values())
    model_ids = sorted(
        {
            row.get("message", {}).get("model")
            for row in latest_messages
            if row.get("message", {}).get("model")
        }
    )
    if len(model_ids) != 1:
        raise ValueError(f"expected one Claude model, found {model_ids}")
    provider_model_id = model_ids[0]
    cost_states = [row for row in rows if row.get("type") == "cost-state"]

    if cost_states:
        state = cost_states[-1]
        model_usage = state.get("modelUsage", {})
        token_values = {
            "input": sum(item.get("inputTokens", 0) for item in model_usage.values()),
            "cachedInput": sum(
                item.get("cacheReadInputTokens", 0) for item in model_usage.values()
            ),
            "cacheCreationInput": sum(
                item.get("cacheCreationInputTokens", 0) for item in model_usage.values()
            ),
            "reasoning": sum(
                item.get("thinkingTokens", 0) for item in model_usage.values()
            ),
            "output": sum(item.get("outputTokens", 0) for item in model_usage.values()),
        }
        token_values["total"] = (
            token_values["input"]
            + token_values["cachedInput"]
            + token_values["cacheCreationInput"]
            + token_values["output"]
        )
        usage = {
            "tokens": token_values,
            "cost": {
                "currency": "USD",
                "total": state["totalCostUSD"],
                "estimated": False,
            },
            "requests": len(latest_messages),
            "turns": 1,
            "toolCalls": sum(
                1
                for row in latest_messages
                for block in row.get("message", {}).get("content", [])
                if isinstance(block, dict) and block.get("type") == "tool_use"
            ),
            "providerReported": True,
            "raw": {
                "source": "Claude Code final cost-state",
                "requestCountBasis": "unique recorded assistant message ids",
                "costState": {
                    "totalCostUSD": state.get("totalCostUSD"),
                    "totalAPIDurationMs": state.get("totalAPIDuration"),
                    "totalDurationMs": state.get("totalDuration"),
                    "startTimeEpochMs": state.get("startTime"),
                    "modelUsage": model_usage,
                },
            },
        }
        return usage, provider_model_id, "provider"

    totals = {
        "input": 0,
        "cachedInput": 0,
        "cacheCreationInput": 0,
        "reasoning": 0,
        "output": 0,
    }
    for row in latest_messages:
        item = row.get("message", {}).get("usage", {})
        totals["input"] += item.get("input_tokens", 0)
        totals["cachedInput"] += item.get("cache_read_input_tokens", 0)
        totals["cacheCreationInput"] += item.get("cache_creation_input_tokens", 0)
        totals["reasoning"] += item.get("output_tokens_details", {}).get(
            "thinking_tokens", 0
        )
        totals["output"] += item.get("output_tokens", 0)
    totals["total"] = (
        totals["input"]
        + totals["cachedInput"]
        + totals["cacheCreationInput"]
        + totals["output"]
    )
    pricing = litellm_rates(provider_model_id)
    cost = estimated_cost(
        pricing,
        input_tokens=totals["input"],
        cached_input_tokens=totals["cachedInput"],
        cache_creation_input_tokens=totals["cacheCreationInput"],
        output_tokens=totals["output"],
        input_includes_cached=False,
    )
    usage = {
        "tokens": totals,
        "cost": cost,
        "requests": len(latest_messages),
        "turns": 1,
        "toolCalls": sum(
            1
            for row in latest_messages
            for block in row.get("message", {}).get("content", [])
            if isinstance(block, dict) and block.get("type") == "tool_use"
        ),
        "providerReported": True,
        "raw": {
            "source": "sum of final snapshots for unique Claude message ids",
            "requestCountBasis": "unique recorded assistant message ids",
            "pricing": pricing,
        },
    }
    return usage, provider_model_id, "litellm"


def build_claude(run_id: str) -> tuple[dict[str, Any], dict[str, Any], str]:
    filename = CLAUDE_SOURCES[run_id]
    project_directory = str(ROOT / run_id).replace(os.sep, "-").replace(".", "-")
    source = HOME / ".claude-personal/projects" / project_directory / filename
    rows = read_jsonl(source)
    events, omitted_calls = normalize_claude_events(rows)
    usage, provider_model_id, cost_kind = claude_usage(rows)
    started_at, completed_at = timestamp_bounds(rows)
    timing: dict[str, Any] = {
        "startedAt": started_at,
        "completedAt": completed_at,
        "wallDurationMs": wall_duration_ms(started_at, completed_at),
    }
    cost_states = [row for row in rows if row.get("type") == "cost-state"]
    if cost_states and cost_states[-1].get("totalAPIDuration") is not None:
        timing["apiDurationMs"] = cost_states[-1]["totalAPIDuration"]
    versions = {row.get("version") for row in rows if row.get("version")}
    if len(versions) != 1:
        raise ValueError(f"{run_id}: expected one Claude Code version, found {versions}")
    cli_version = versions.pop()
    harness = {
        "name": "Claude Code",
        "version": cli_version,
        "runtime": "Anthropic Claude Code CLI",
        "invocation": f"Claude Code session using {provider_model_id}; effort {run_id.rsplit('/', 1)[1]}",
    }
    if cost_kind == "provider":
        pricing_source = f"Claude Code {cli_version} final cost-state"
        notes = (
            "Token and cost totals are the exact final Claude Code cost-state totals. "
            "The public transcript omits hidden thinking, private agent-mail activity, "
            "credentials, and private local path prefixes."
        )
    else:
        pricing_source = (
            f"LiteLLM {EXPECTED_LITELLM_VERSION} model_cost[{provider_model_id}]"
        )
        notes = (
            "Claude Code recorded exact token categories but no final cost-state. Cost "
            "was calculated from LiteLLM rates; the public transcript omits hidden "
            "thinking, private agent-mail activity, credentials, and private local path "
            "prefixes."
        )
    run = run_base(
        run_id,
        provider_model_id,
        harness,
        timing,
        usage,
        notes,
        pricing_source,
    )
    session_id = next(
        (row.get("sessionId") for row in rows if row.get("sessionId")),
        source.stem,
    )
    transcript = transcript_base(
        run_id,
        session_id,
        "claude-code-jsonl",
        source,
        events,
        omitted_calls,
    )
    return run, transcript, cost_kind


def decode_tool_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
    return value


def normalize_codex_events(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    events: list[dict[str, Any]] = []
    published_calls: set[str] = set()
    omitted_calls: set[str] = set()
    user_message_emitted = False

    for row in rows:
        if row.get("type") != "response_item":
            continue
        payload = row.get("payload", {})
        item_type = payload.get("type")
        timestamp = row.get("timestamp")
        if item_type == "message":
            role = payload.get("role")
            if role not in {"user", "assistant"}:
                continue
            text = content_text(payload.get("content", []))
            if role == "user":
                text = public_user_text(text)
                if not text:
                    continue
                user_message_emitted = True
            if not text:
                continue
            event = {"type": "message", "role": role, "content": text}
            if timestamp:
                event["timestamp"] = timestamp
            events.append(event)
        elif item_type in {"custom_tool_call", "function_call"}:
            call_id = str(payload.get("call_id") or payload.get("id"))
            tool = str(payload.get("name", "tool"))
            raw_input = payload.get("input", payload.get("arguments", {}))
            if PRIVATE_TOOL_RE.search(tool) or PRIVATE_TOOL_RE.search(str(raw_input)):
                omitted_calls.add(call_id)
                continue
            published_calls.add(call_id)
            event = {
                "type": "tool_call",
                "id": call_id,
                "tool": tool,
                "input": scrub(decode_tool_value(raw_input)),
            }
            if timestamp:
                event["timestamp"] = timestamp
            events.append(event)
        elif item_type in {"custom_tool_call_output", "function_call_output"}:
            call_id = str(payload.get("call_id", ""))
            if call_id not in published_calls or call_id in omitted_calls:
                continue
            output = scrub(payload.get("output", ""))
            output_text = content_text(output)
            event = {
                "type": "tool_result",
                "callId": call_id,
                "status": "error" if re.search(r"\b(?:failed|error)\b", output_text, re.I) else "success",
                "output": output,
            }
            if timestamp:
                event["timestamp"] = timestamp
            events.append(event)
        # Deliberately ignore response_item reasoning records.
    if not user_message_emitted:
        raise ValueError("Codex transcript does not contain a public user message")
    return events, len(omitted_calls)


def final_codex_usage(rows: list[dict[str, Any]]) -> dict[str, int]:
    records = [
        row.get("payload", {}).get("thread_token_usage")
        for row in rows
        if row.get("type") == "token_usage_record"
        and row.get("payload", {}).get("thread_token_usage")
    ]
    if not records:
        records = [
            row.get("payload", {}).get("info", {}).get("total_token_usage")
            for row in rows
            if row.get("type") == "event_msg"
            and row.get("payload", {}).get("type") == "token_count"
            and row.get("payload", {}).get("info", {}).get("total_token_usage")
        ]
    if not records:
        raise ValueError("Codex session has no cumulative token usage")
    return records[-1]


def build_codex_session(run_id: str) -> tuple[dict[str, Any], dict[str, Any], str]:
    date_dir, filename = CODEX_SESSION_SOURCES[run_id]
    source = HOME / ".codex-personal/sessions" / date_dir / filename
    rows = read_jsonl(source)
    meta = next(row["payload"] for row in rows if row.get("type") == "session_meta")
    context = next(row["payload"] for row in rows if row.get("type") == "turn_context")
    expected_cwd = str(ROOT / run_id)
    if meta.get("cwd") != expected_cwd or context.get("model") != run_id.split("/")[1]:
        raise ValueError(f"{run_id}: source session does not match run")
    provider_model_id = context["model"]
    raw_tokens = final_codex_usage(rows)
    tokens = {
        "input": raw_tokens["input_tokens"],
        "cachedInput": raw_tokens.get("cached_input_tokens", 0),
        "cacheCreationInput": raw_tokens.get("cache_write_input_tokens", 0),
        "reasoning": raw_tokens.get("reasoning_output_tokens", 0),
        "output": raw_tokens["output_tokens"],
        "total": raw_tokens["total_tokens"],
    }
    pricing = litellm_rates(provider_model_id)
    cost = estimated_cost(
        pricing,
        input_tokens=tokens["input"],
        cached_input_tokens=tokens["cachedInput"],
        cache_creation_input_tokens=tokens["cacheCreationInput"],
        output_tokens=tokens["output"],
        input_includes_cached=True,
    )
    tool_calls = sum(
        1
        for row in rows
        if row.get("type") == "response_item"
        and row.get("payload", {}).get("type") in {"custom_tool_call", "function_call"}
    )
    requests = sum(1 for row in rows if row.get("type") == "token_usage_record")
    usage = {
        "tokens": tokens,
        "cost": cost,
        "requests": requests,
        "turns": 1,
        "toolCalls": tool_calls,
        "providerReported": True,
        "raw": {
            "source": "Codex final cumulative thread_token_usage",
            "codexThreadTokenUsage": raw_tokens,
            "pricing": pricing,
            "costCalculation": {
                "uncachedInputTokens": tokens["input"] - tokens["cachedInput"],
                "cachedInputIsSubsetOfInput": True,
            },
        },
    }
    started_at, completed_at = timestamp_bounds(rows)
    timing = {
        "startedAt": started_at,
        "completedAt": completed_at,
        "wallDurationMs": wall_duration_ms(started_at, completed_at),
    }
    cli_version = meta["cli_version"]
    effort = context.get("effort") or run_id.rsplit("/", 1)[1]
    harness = {
        "name": "Codex CLI",
        "version": cli_version,
        "runtime": "OpenAI Codex CLI",
        "invocation": f"Codex CLI session using {provider_model_id}; reasoning effort {effort}",
    }
    pricing_source = f"LiteLLM {EXPECTED_LITELLM_VERSION} model_cost[{provider_model_id}]"
    notes = (
        "Codex recorded exact cumulative token categories. Cost was calculated from "
        "LiteLLM rates; cached input is a subset of Codex's input total. The public "
        "transcript omits hidden reasoning, private agent-mail activity, credentials, "
        "and private local path prefixes."
    )
    events, omitted_calls = normalize_codex_events(rows)
    run = run_base(
        run_id,
        provider_model_id,
        harness,
        timing,
        usage,
        notes,
        pricing_source,
    )
    transcript = transcript_base(
        run_id,
        meta["id"],
        "codex-rollout-jsonl",
        source,
        events,
        omitted_calls,
    )
    return run, transcript, "litellm"


LOG_MARKERS = {"user", "codex", "exec", "apply patch", "tokens used"}


def normalize_log_events(
    text: str, started_at: str, completed_at: str
) -> tuple[list[dict[str, Any]], int]:
    text = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", text).replace("\r\n", "\n")
    lines = text.splitlines()
    events: list[dict[str, Any]] = []
    tool_number = 0
    index = next((i for i, line in enumerate(lines) if line == "user"), -1)
    if index < 0:
        raise ValueError("Codex terminal log has no user marker")

    def collect(start: int) -> tuple[list[str], int]:
        end = start
        while end < len(lines) and lines[end] not in LOG_MARKERS:
            end += 1
        return lines[start:end], end

    chunk, index = collect(index + 1)
    prompt_text = "\n".join(chunk).strip()
    if PROMPT not in prompt_text:
        raise ValueError("Codex terminal log does not contain the benchmark prompt")
    events.append(
        {
            "type": "message",
            "role": "user",
            "content": PROMPT,
            "timestamp": started_at,
        }
    )

    while index < len(lines):
        marker = lines[index]
        chunk, index = collect(index + 1)
        body = "\n".join(chunk).strip()
        if marker == "codex" and body:
            events.append(
                {"type": "message", "role": "assistant", "content": scrub_text(body)}
            )
        elif marker in {"exec", "apply patch"}:
            tool_number += 1
            call_id = f"terminal-tool-{tool_number}"
            if marker == "exec":
                first_line, _, remainder = body.partition("\n")
                command, separator, cwd = first_line.rpartition(" in ")
                tool_input = {
                    "command": scrub_text(command if separator else first_line),
                }
                if separator:
                    tool_input["cwd"] = scrub_text(cwd)
                output = scrub_text(remainder)
            else:
                # The plain terminal format records the patch result and full diff, but
                # does not expose the tool's input as a distinct payload.
                tool_input = None
                output = scrub_text(body)
            events.append(
                {
                    "type": "tool_call",
                    "id": call_id,
                    "tool": marker.replace(" ", "_"),
                    "input": tool_input,
                }
            )
            events.append(
                {
                    "type": "tool_result",
                    "callId": call_id,
                    "status": "error"
                    if re.search(r"\b(?:exited [1-9]|failed|error)\b", output, re.I)
                    else "success",
                    "output": output,
                }
            )
        elif marker == "tokens used":
            if not chunk:
                raise ValueError("tokens used marker has no value")
            final_text = "\n".join(chunk[1:]).strip()
            if final_text:
                events.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": scrub_text(final_text),
                        "timestamp": completed_at,
                    }
                )
    return events, 0


def parse_log_header(text: str) -> dict[str, str]:
    patterns = {
        "version": r"^OpenAI Codex v([^\n]+)$",
        "model": r"^model: ([^\n]+)$",
        "effort": r"^reasoning effort: ([^\n]+)$",
        "sessionId": r"^session id: ([^\n]+)$",
    }
    result = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, text, re.MULTILINE)
        if not match:
            raise ValueError(f"Codex terminal log is missing {key}")
        result[key] = match.group(1).strip()
    return result


def log_total_tokens(text: str) -> int:
    matches = re.findall(r"^tokens used\r?\n([\d,]+)$", text, re.MULTILINE)
    if not matches:
        raise ValueError("Codex terminal log has no total token count")
    return int(matches[-1].replace(",", ""))


def terra_low_log() -> str:
    rows = read_jsonl(TERRA_LOW_PARENT)
    candidates = []
    for row in rows:
        if row.get("timestamp") != TERRA_LOW_COMPLETED:
            continue
        for value in iter_strings(row.get("payload", {})):
            if (
                value.startswith("OpenAI Codex v")
                and TERRA_LOW_SESSION_ID in value
                and "tokens used\n16,079" in value
            ):
                candidates.append(value)
    if not candidates:
        raise ValueError("could not recover Terra low terminal record from parent session")
    return max(candidates, key=len)


def build_codex_log(run_id: str) -> tuple[dict[str, Any], dict[str, Any], str]:
    _, model_slug, effort = run_id.split("/")
    if run_id == TERRA_LOW_ID:
        source = TERRA_LOW_PARENT
        text = terra_low_log()
        started_at = TERRA_LOW_STARTED
        completed_at = TERRA_LOW_COMPLETED
        source_format = "codex-terminal-captured-in-parent-jsonl"
    else:
        log_name = f"ramen-bench-{LOG_NAMES[model_slug]}-{effort}.log"
        source = Path("/tmp") / log_name
        if not source.is_file():
            raise FileNotFoundError(source)
        text = source.read_text(errors="strict")
        started_at = LOG_STARTS[run_id]
        completed_at = format_timestamp(
            datetime.fromtimestamp(source.stat().st_mtime, timezone.utc)
        )
        source_format = "codex-exec-terminal-log"
    header = parse_log_header(text)
    if header["model"] != model_slug or header["effort"] != effort:
        raise ValueError(f"{run_id}: terminal header does not match run")
    total_tokens = log_total_tokens(text)
    pricing = litellm_rates(model_slug)
    events, omitted_calls = normalize_log_events(text, started_at, completed_at)
    tool_calls = sum(event["type"] == "tool_call" for event in events)
    tokens = {
        "input": 0,
        "cachedInput": 0,
        "cacheCreationInput": 0,
        "reasoning": 0,
        "output": 0,
        "total": total_tokens,
    }
    usage = {
        "tokens": tokens,
        "cost": None,
        "turns": 1,
        "toolCalls": tool_calls,
        "providerReported": True,
        "raw": {
            "source": "Codex terminal tokens used summary",
            "totalTokens": total_tokens,
            "tokenCategoryBreakdownAvailable": False,
            "pricing": pricing,
            "costCalculation": {
                "status": "not_calculated_missing_token_categories",
                "reason": (
                    "The terminal record reports total tokens only; assigning input, "
                    "cached input, reasoning, or output categories would require guessing."
                ),
            },
        },
    }
    timing = {
        "startedAt": started_at,
        "completedAt": completed_at,
        "wallDurationMs": wall_duration_ms(started_at, completed_at),
    }
    harness = {
        "name": "Codex CLI",
        "version": header["version"],
        "runtime": "OpenAI Codex CLI",
        "invocation": (
            f"codex exec --ephemeral --model {model_slug} --config "
            f"model_reasoning_effort={effort} PROMPT.md"
        ),
    }
    pricing_source = (
        f"LiteLLM {EXPECTED_LITELLM_VERSION} model_cost[{model_slug}] "
        "(rates retained; cost not calculated because token categories are unavailable)"
    )
    notes = (
        "The Codex terminal record reports an exact total token count without category "
        "breakdown. Input, cache, reasoning, and output fields remain zero and cost is "
        "unavailable rather than guessed. The public transcript contains recorded text "
        "and full recorded tool activity; hidden reasoning, credentials, and private "
        "local path prefixes are excluded."
    )
    run = run_base(
        run_id,
        model_slug,
        harness,
        timing,
        usage,
        notes,
        pricing_source,
    )
    transcript = transcript_base(
        run_id,
        header["sessionId"],
        source_format,
        source,
        events,
        omitted_calls,
    )
    return run, transcript, "unavailable"


def tracked_run_ids() -> list[str]:
    output = subprocess.check_output(
        ["git", "ls-files", "anthropic/**/run.json", "openai/**/run.json"],
        cwd=ROOT,
        text=True,
    )
    run_ids = []
    for relative in output.splitlines():
        if relative.startswith("openai/gpt-5.3-codex-spark/"):
            continue
        path = ROOT / relative
        if path.is_file():
            run_ids.append(str(Path(relative).parent))
    return sorted(run_ids)


def validate_document(document: dict[str, Any], schema_name: str) -> None:
    schema = json.loads((ROOT / "schemas" / schema_name).read_text())
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(document), key=lambda error: list(error.path))
    if errors:
        details = "; ".join(
            f"{'/'.join(map(str, error.path)) or '<root>'}: {error.message}"
            for error in errors
        )
        raise ValueError(f"{schema_name} validation failed: {details}")


def write_json(path: Path, document: dict[str, Any], dry_run: bool) -> None:
    document = censor_value(document, workspace=ROOT, home=HOME)
    validate_document(
        document,
        "run.schema.json" if path.name == "run.json" else "transcript.schema.json",
    )
    serialized = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    if str(HOME) in serialized:
        raise ValueError(f"private home path leaked into {path}")
    if re.search(r"\bsk-[A-Za-z0-9_-]{16,}\b", serialized):
        raise ValueError(f"credential-like value leaked into {path}")
    if not dry_run:
        path.write_text(serialized)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="validate without writing")
    parser.add_argument(
        "--harness",
        choices=("all", "claude-code", "codex"),
        default="all",
        help="limit regeneration to one harness",
    )
    args = parser.parse_args(argv)

    installed_version = package_version("litellm")
    if installed_version != EXPECTED_LITELLM_VERSION:
        raise RuntimeError(
            f"expected LiteLLM {EXPECTED_LITELLM_VERSION}, found {installed_version}"
        )

    all_run_ids = tracked_run_ids()
    expected = set(CLAUDE_SOURCES) | set(CODEX_SESSION_SOURCES) | set(LOG_STARTS) | {
        TERRA_LOW_ID
    }
    if set(all_run_ids) != expected:
        missing_sources = sorted(set(all_run_ids) - expected)
        missing_runs = sorted(expected - set(all_run_ids))
        raise ValueError(
            f"source map mismatch; missing sources={missing_sources}, missing runs={missing_runs}"
        )
    run_ids = [
        run_id
        for run_id in all_run_ids
        if args.harness == "all"
        or (args.harness == "claude-code" and run_id.startswith("anthropic/"))
        or (args.harness == "codex" and run_id.startswith("openai/"))
    ]

    coverage = {
        "anthropicProviderCost": 0,
        "anthropicLiteLLMCost": 0,
        "openaiLiteLLMCost": 0,
        "openaiCostUnavailable": 0,
        "events": 0,
        "base64Omissions": 0,
        "base64CharactersOmitted": 0,
    }
    changed_paths = []
    for run_id in run_ids:
        if run_id in CLAUDE_SOURCES:
            run, transcript, cost_kind = build_claude(run_id)
            coverage[
                "anthropicProviderCost"
                if cost_kind == "provider"
                else "anthropicLiteLLMCost"
            ] += 1
        elif run_id in CODEX_SESSION_SOURCES:
            run, transcript, _ = build_codex_session(run_id)
            coverage["openaiLiteLLMCost"] += 1
        else:
            run, transcript, _ = build_codex_log(run_id)
            coverage["openaiCostUnavailable"] += 1
        coverage["events"] += len(transcript["events"])
        omission_sizes = [
            int(size) for size in BASE64_MARKER_RE.findall(json.dumps(transcript))
        ]
        coverage["base64Omissions"] += len(omission_sizes)
        coverage["base64CharactersOmitted"] += sum(omission_sizes)
        run_path = ROOT / run_id / "run.json"
        transcript_path = ROOT / run_id / "transcript.json"
        write_json(run_path, run, args.dry_run)
        write_json(transcript_path, transcript, args.dry_run)
        changed_paths.extend((run_path.relative_to(ROOT), transcript_path.relative_to(ROOT)))

    action = "Validated" if args.dry_run else "Backfilled"
    print(f"{action} {len(run_ids)} runs and {len(changed_paths)} metadata files.")
    print(
        "Coverage: "
        f"Anthropic provider cost={coverage['anthropicProviderCost']}, "
        f"Anthropic LiteLLM cost={coverage['anthropicLiteLLMCost']}, "
        f"OpenAI LiteLLM cost={coverage['openaiLiteLLMCost']}, "
        f"OpenAI cost unavailable (total tokens only)={coverage['openaiCostUnavailable']}."
    )
    print(f"Normalized transcript events: {coverage['events']}.")
    print(
        f"Base64/image omissions: {coverage['base64Omissions']} markers, "
        f"{coverage['base64CharactersOmitted']} source characters."
    )
    for path in changed_paths:
        print(path)


if __name__ == "__main__":
    main()
