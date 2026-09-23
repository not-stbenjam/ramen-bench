#!/usr/bin/env python3
"""Backfill public benchmark metadata from local Claude Code and Codex records.

The generated transcripts include recorded user, assistant, and tool activity only.
System/developer messages, hidden reasoning, credentials, and private path details are
excluded; complete non-sensitive recorded tool payloads are retained.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shlex
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
    "gpt-6-sol": "GPT 6 Sol",
    "gpt-6-luna": "GPT 6 Luna",
    "gpt-6-astra": "GPT 6 Astra",
    "gpt-5.6-sol": "GPT 5.6 Sol",
    "gpt-5.6-terra": "GPT 5.6 Terra",
    "gpt-5.6-luna": "GPT 5.6 Luna",
    "gpt-5.5": "GPT 5.5",
    "fable-5.1": "Fable 5.1",
    "opus-5.5": "Opus 5.5",
    "opus-5": "Opus 5",
    "glm-5.3": "GLM-5.3",
}
VENDOR_DISPLAY_NAMES = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "z.ai": "Z.ai",
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
    "anthropic/opus-5.5/low": "ee5d151b-3aa4-4665-9edb-c8221098d7dd.jsonl",
    "anthropic/opus-5.5/medium": "f5588de3-8761-48e5-814c-0406b5a454d2.jsonl",
    "anthropic/opus-5.5/high": "e8585317-8097-4061-8f28-bab99dbf2011.jsonl",
    "anthropic/opus-5.5/xhigh": "a4f3c8e1-2b7d-4c9a-8e56-1f0d3b9c72aa.jsonl",
    "anthropic/opus-5.5/max": "72fad481-148f-4b58-b000-cf98db3d4ad4.jsonl",
    "anthropic/fable-5.1/max": "1aec6fb2-50f8-4922-a8be-fe1c7bd0c0f1.jsonl",
    "anthropic/fable-5.1/low": "7a98bbf8-3d05-488f-adcf-f5885320142e.jsonl",
    "anthropic/fable-5.1/medium": "8f4c534d-bc6b-4bad-b7cb-62035065f6b6.jsonl",
    "anthropic/fable-5.1/high": "146c3cb5-8250-43fd-8ad2-4fc433d907af.jsonl",
    "anthropic/fable-5.1/xhigh": "34bad787-8505-463b-a158-23c7142dc418.jsonl",
    "anthropic/opus-5/low": "53ffbdbc-fb75-4f0e-aa78-84867e0139b6.jsonl",
    "anthropic/opus-5/medium": "c5d5dbe1-70df-40aa-bbf3-f5716a922400.jsonl",
    "anthropic/opus-5/high": "4920bb88-dd70-4834-9840-7ddcd388c369.jsonl",
    "anthropic/opus-5/xhigh": "f7517ad6-13ef-4b03-aa98-afd1229c781d.jsonl",
    "anthropic/opus-5/max": "0cef914e-533c-4793-972f-95e098e24cd9.jsonl",
    "z.ai/glm-5.3/max": "85757c43-1df7-4d13-978a-6d135545dc86.jsonl",
    "z.ai/glm-5.3/high": "5817ebce-617e-481b-93b1-383fb4000df2.jsonl",
    # The supplied Low-directory session records claude-opus-5, so it cannot
    # truthfully serve as a GLM-5.3 source mapping.
}
CLAUDE_SOURCE_ROOTS = {
    "z.ai/glm-5.3/max": ".claude",
    "z.ai/glm-5.3/high": ".claude",
}
CLAUDE_REPLAY_VERIFICATION = {
    "anthropic/opus-5.5/low",
    "anthropic/opus-5.5/medium",
    "anthropic/opus-5.5/high",
    "anthropic/opus-5.5/xhigh",
    "anthropic/opus-5.5/max",
    "z.ai/glm-5.3/max",
    "z.ai/glm-5.3/high",
}
CLAUDE_COMPONENT_BUILDS = {
    "anthropic/opus-5.5/max": (
        "01-head.html",
        "02-util.js",
        "03-geom.js",
        "04-scene.js",
        "05-shaders.js",
        "06-gl.js",
        "07-main.js",
        "08-loop.js",
        "99-tail.html",
    )
}
CLAUDE_EXPECTED_PROVIDER_MODELS = {
    "anthropic/opus-5.5/low": "claude-opus-5-5",
    "anthropic/opus-5.5/medium": "claude-opus-5-5",
    "anthropic/opus-5.5/high": "claude-opus-5-5",
    "anthropic/opus-5.5/xhigh": "claude-opus-5-5",
    "anthropic/opus-5.5/max": "claude-opus-5-5",
    "anthropic/fable-5.1/max": "claude-fable-5-1",
    "z.ai/glm-5.3/max": "glm-5.3",
    "z.ai/glm-5.3/high": "glm-5.3",
}
CLAUDE_REQUIRE_FINAL_COST_STATE = {
    run_id for run_id in CLAUDE_SOURCES if run_id.startswith("anthropic/opus-5.5/")
}

CODEX_SESSION_SOURCES = {
    "openai/gpt-6-sol/low": (
        "2026/09/22",
        "rollout-2026-09-22T19-18-43-01a0cb6a-1a4a-7900-8e19-4c5d5d229bbb.jsonl",
    ),
    "openai/gpt-6-sol/medium": (
        "2026/09/22",
        "rollout-2026-09-22T19-18-46-01a0cb6a-2510-7c40-87b3-e2ca93cd0b99.jsonl",
    ),
    "openai/gpt-6-sol/high": (
        "2026/09/22",
        "rollout-2026-09-22T19-18-45-01a0cb6a-23a0-7eb3-bd85-a6bba00767c4.jsonl",
    ),
    "openai/gpt-6-sol/xhigh": (
        "2026/09/22",
        "rollout-2026-09-22T19-18-45-01a0cb6a-22f2-7fe3-877b-d8cc42f0619b.jsonl",
    ),
    "openai/gpt-6-sol/max": (
        "2026/09/22",
        "rollout-2026-09-22T19-18-45-01a0cb6a-2237-7db3-8cb8-996e6523dfe9.jsonl",
    ),
    "openai/gpt-6-sol/ultra": (
        "2026/09/22",
        "rollout-2026-09-22T19-18-45-01a0cb6a-2158-72b3-af4e-7fe0a3703565.jsonl",
    ),
    "openai/gpt-6-luna/low": (
        "2026/09/22",
        "rollout-2026-09-22T19-18-45-01a0cb6a-20a9-73a1-98be-8e7012ef0ac6.jsonl",
    ),
    "openai/gpt-6-luna/medium": (
        "2026/09/22",
        "rollout-2026-09-22T19-18-44-01a0cb6a-1feb-7943-87f9-e95b91063bc7.jsonl",
    ),
    "openai/gpt-6-luna/high": (
        "2026/09/22",
        "rollout-2026-09-22T19-18-44-01a0cb6a-1fd3-7822-97f2-47bef79f9f3e.jsonl",
    ),
    "openai/gpt-6-luna/xhigh": (
        "2026/09/22",
        "rollout-2026-09-22T19-18-44-01a0cb6a-1d8b-7933-8a53-9c8f8b6018aa.jsonl",
    ),
    "openai/gpt-6-luna/max": (
        "2026/09/22",
        "rollout-2026-09-22T19-18-43-01a0cb6a-1bff-73e1-b058-0ae32faa4d38.jsonl",
    ),
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
GPT_6_CODEX_RUNS = {
    run_id
    for run_id in CODEX_SESSION_SOURCES
    if run_id.startswith(("openai/gpt-6-sol/", "openai/gpt-6-luna/"))
}
CODEX_SOURCE_ROOTS: dict[str, Path] = {
    run_id: HOME / ".codex-personal2" for run_id in GPT_6_CODEX_RUNS
}
CODEX_EXPECTED_CWDS: dict[str, Path] = {
    run_id: Path("/tmp/ramen-bench-runs-20260922")
    / Path(run_id).relative_to("openai")
    for run_id in GPT_6_CODEX_RUNS
}
CODEX_REPLAY_VERIFICATION = GPT_6_CODEX_RUNS

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


def estimated_total_only_cost(
    pricing: dict[str, Any], *, total_tokens: int, effort: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Estimate a total-only Codex run using the same-effort Astra token mix."""
    reference_id = f"openai/gpt-6-astra/{effort}"
    reference_path = ROOT / reference_id / "run.json"
    reference = json.loads(reference_path.read_text())
    reference_tokens = reference["usage"]["tokens"]
    reference_total = reference_tokens["total"]
    reference_input = reference_tokens["input"]
    if reference_total <= 0 or reference_input <= 0:
        raise ValueError(f"{reference_id}: invalid token allocation reference")

    output_tokens = round(
        total_tokens * reference_tokens["output"] / reference_total
    )
    input_tokens = total_tokens - output_tokens
    cached_input_tokens = round(
        input_tokens * reference_tokens["cachedInput"] / reference_input
    )
    reasoning_tokens = round(
        output_tokens
        * reference_tokens.get("reasoning", 0)
        / max(reference_tokens["output"], 1)
    )
    allocation = {
        "input": input_tokens,
        "cachedInput": cached_input_tokens,
        "cacheCreationInput": 0,
        "reasoning": reasoning_tokens,
        "output": output_tokens,
        "total": total_tokens,
    }
    cost = estimated_cost(
        pricing,
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        cache_creation_input_tokens=0,
        output_tokens=output_tokens,
        input_includes_cached=True,
    )
    method = {
        "status": "estimated_from_total_tokens",
        "exactTotalTokens": total_tokens,
        "referenceRunId": reference_id,
        "referenceRationale": (
            "same Codex harness, task, date, and reasoning effort with recorded token "
            "categories"
        ),
        "estimatedTokenAllocation": allocation,
        "inputIncludesCachedInput": True,
    }
    return cost, method


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
            "displayName": VENDOR_DISPLAY_NAMES[vendor],
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


def claude_tool_call_count(rows: list[dict[str, Any]]) -> int:
    """Count unique recorded tool calls across streamed message fragments."""
    call_ids: set[str] = set()
    for row in rows:
        if row.get("type") != "assistant":
            continue
        message = row.get("message", {})
        message_id = str(message.get("id", ""))
        blocks = message.get("content", [])
        for index, block in enumerate(blocks if isinstance(blocks, list) else []):
            if isinstance(block, dict) and block.get("type") == "tool_use":
                call_ids.add(str(block.get("id") or f"{message_id}:{index}"))
    return len(call_ids)


def benchmark_claude_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Discard setup records before the benchmark prompt enters the session."""
    for index, row in enumerate(rows):
        if row.get("type") != "user":
            continue
        content = row.get("message", {}).get("content", "")
        if any(PROMPT in value for value in iter_strings(content)):
            return rows[index:]
    raise ValueError("Claude Code session does not contain the benchmark prompt")


def verify_claude_result(rows: list[dict[str, Any]], run_id: str) -> str:
    """Replay recorded file operations and require the final artifact to match."""
    if run_id in CLAUDE_COMPONENT_BUILDS:
        return verify_claude_component_result(rows, run_id)
    content: str | None = None
    operation_count = 0
    for row in rows:
        if row.get("type") != "assistant":
            continue
        blocks = row.get("message", {}).get("content", [])
        for block in blocks if isinstance(blocks, list) else []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool = block.get("name")
            tool_input = block.get("input", {})
            if not isinstance(tool_input, dict):
                continue
            file_path = str(tool_input.get("file_path", ""))
            if tool in {"Write", "Edit"} and Path(file_path).name != "index.html":
                continue
            if tool == "Write":
                recorded = tool_input.get("content")
                if not isinstance(recorded, str):
                    raise ValueError(f"{run_id}: recorded Write has no text content")
                content = recorded
                operation_count += 1
            elif tool == "Edit" and content is not None:
                old = tool_input.get("old_string")
                new = tool_input.get("new_string")
                if not isinstance(old, str) or not isinstance(new, str):
                    raise ValueError(f"{run_id}: recorded Edit is incomplete")
                occurrences = content.count(old)
                replace_all = bool(tool_input.get("replace_all"))
                if occurrences == 0 or (not replace_all and occurrences != 1):
                    raise ValueError(
                        f"{run_id}: cannot replay recorded Edit with "
                        f"{occurrences} matching regions"
                    )
                content = content.replace(old, new, -1 if replace_all else 1)
                operation_count += 1
            elif tool == "Bash" and content is not None:
                command = tool_input.get("command")
                if isinstance(command, str):
                    content, count = replay_recorded_shell_mutation(
                        content, command, run_id
                    )
                    operation_count += count
    if content is None:
        raise ValueError(f"{run_id}: no recorded index.html Write found")
    artifact = (ROOT / run_id / "index.html").read_text()
    if content != artifact:
        raise ValueError(f"{run_id}: index.html differs from the final recorded write")
    return f"recorded file-operation replay ({operation_count} operations)"


def normalize_claude_events(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    emitted_blocks: set[tuple[str, str, str]] = set()
    published_calls: set[str] = set()
    omitted_calls: set[str] = set()
    events: list[dict[str, Any]] = []
    user_message_emitted = False

    for row in rows:
        timestamp = row.get("timestamp")
        if row.get("type") == "assistant":
            message = row.get("message", {})
            message_id = str(message.get("id", ""))
            if not message_id:
                continue
            for block in message.get("content", []):
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text" and block.get("text"):
                    signature = (message_id, "text", str(block["text"]))
                    if signature in emitted_blocks:
                        continue
                    emitted_blocks.add(signature)
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
                    signature = (message_id, "tool_use", call_id)
                    if signature in emitted_blocks:
                        continue
                    emitted_blocks.add(signature)
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
            "toolCalls": claude_tool_call_count(rows),
            "providerReported": True,
            "raw": {
                "source": "Claude Code final cost-state",
                "requestCountBasis": "unique recorded assistant message ids",
                "costState": {
                    "totalCostUSD": state.get("totalCostUSD"),
                    "totalAPIDurationMs": state.get("totalAPIDuration"),
                    "totalDurationMs": state.get("totalDuration"),
                    "startTimeEpochMs": state.get("startTime"),
                    "hasUnknownModelCost": state.get("hasUnknownModelCost"),
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
        "toolCalls": claude_tool_call_count(rows),
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
    source_root = CLAUDE_SOURCE_ROOTS.get(run_id, ".claude-personal")
    source = HOME / source_root / "projects" / project_directory / filename
    rows = read_jsonl(source)
    if run_id in CLAUDE_REQUIRE_FINAL_COST_STATE and not any(
        row.get("type") == "cost-state" for row in rows
    ):
        raise ValueError(f"{run_id}: source session has no final cost-state")
    public_rows = benchmark_claude_rows(rows)
    events, omitted_calls = normalize_claude_events(public_rows)
    usage, provider_model_id, cost_kind = claude_usage(rows)
    expected_provider_model = CLAUDE_EXPECTED_PROVIDER_MODELS.get(run_id)
    if expected_provider_model and provider_model_id != expected_provider_model:
        raise ValueError(
            f"{run_id}: expected provider model {expected_provider_model}, "
            f"recorded {provider_model_id}"
        )
    started_at, completed_at = timestamp_bounds(public_rows)
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
        "runtime": (
            "Claude Code CLI with Z.ai provider"
            if run_id.startswith("z.ai/")
            else "Anthropic Claude Code CLI"
        ),
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
    if run_id in CLAUDE_REPLAY_VERIFICATION:
        verification = verify_claude_result(public_rows, run_id)
        notes += f" The result matches its {verification}."
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


def javascript_string_after(source: str, pattern: str) -> str | None:
    """Decode a JSON-compatible JavaScript string following ``pattern``."""
    match = re.search(pattern, source)
    if not match:
        return None
    remainder = source[match.end() :].lstrip()
    if not remainder.startswith('"'):
        return None
    value, _ = json.JSONDecoder().raw_decode(remainder)
    return value if isinstance(value, str) else None


def recorded_codex_patch(source: str) -> str | None:
    patch = javascript_string_after(source, r"const\s+patch\s*=\s*")
    if patch is not None:
        return patch
    return javascript_string_after(source, r"tools\.apply_patch\(\s*")


def recorded_codex_command(source: str) -> str | None:
    return javascript_string_after(source, r"\bcmd\s*:\s*")


def find_unique_line_region(
    content: list[str], old_lines: list[str], run_id: str
) -> int:
    matches = [
        index
        for index in range(len(content) - len(old_lines) + 1)
        if content[index : index + len(old_lines)] == old_lines
    ]
    if len(matches) != 1:
        raise ValueError(
            f"{run_id}: recorded patch region matched {len(matches)} locations"
        )
    return matches[0]


def replay_codex_patch(
    content: list[str] | None, patch: str, run_id: str
) -> tuple[list[str] | None, int]:
    """Replay index.html mutations from one recorded apply_patch payload."""
    lines = patch.splitlines()
    if not lines or lines[0] != "*** Begin Patch":
        raise ValueError(f"{run_id}: malformed recorded apply_patch payload")
    operation_count = 0
    index = 1
    while index < len(lines):
        line = lines[index]
        if line == "*** End Patch":
            return content, operation_count
        if line.startswith("*** Add File: "):
            target = line.split(": ", 1)[1]
            index += 1
            added: list[str] = []
            while index < len(lines) and not lines[index].startswith("*** "):
                if not lines[index].startswith("+"):
                    raise ValueError(f"{run_id}: malformed recorded Add File patch")
                added.append(lines[index][1:])
                index += 1
            if Path(target).name == "index.html":
                if content is not None:
                    raise ValueError(f"{run_id}: recorded index.html was added twice")
                content = added
                operation_count += 1
            continue
        if line.startswith("*** Update File: "):
            target = line.split(": ", 1)[1]
            index += 1
            is_index = Path(target).name == "index.html"
            if is_index and content is None:
                raise ValueError(f"{run_id}: recorded update precedes index.html creation")
            while index < len(lines) and not lines[index].startswith("*** "):
                if not lines[index].startswith("@@"):
                    raise ValueError(f"{run_id}: malformed recorded Update File patch")
                index += 1
                old_lines: list[str] = []
                new_lines: list[str] = []
                while (
                    index < len(lines)
                    and not lines[index].startswith("@@")
                    and not lines[index].startswith("*** ")
                ):
                    hunk_line = lines[index]
                    index += 1
                    if not hunk_line or hunk_line[0] not in " +-":
                        raise ValueError(f"{run_id}: malformed recorded patch hunk")
                    if hunk_line[0] in " -":
                        old_lines.append(hunk_line[1:])
                    if hunk_line[0] in " +":
                        new_lines.append(hunk_line[1:])
                if is_index:
                    assert content is not None
                    start = find_unique_line_region(content, old_lines, run_id)
                    content[start : start + len(old_lines)] = new_lines
                    operation_count += 1
            continue
        raise ValueError(f"{run_id}: unsupported recorded patch directive {line!r}")
    raise ValueError(f"{run_id}: recorded patch has no End Patch marker")


def replay_safe_python_write(content: str, script: str, run_id: str) -> str:
    """Execute a tightly validated string-only repair against an in-memory file."""
    tree = ast.parse(script)
    allowed_nodes = (
        ast.Module,
        ast.Import,
        ast.alias,
        ast.Assign,
        ast.FunctionDef,
        ast.arguments,
        ast.arg,
        ast.Global,
        ast.Assert,
        ast.Expr,
        ast.Call,
        ast.Name,
        ast.Load,
        ast.Store,
        ast.Constant,
        ast.Attribute,
        ast.Compare,
        ast.Eq,
        ast.GtE,
        ast.In,
        ast.IfExp,
        ast.List,
        ast.Tuple,
        ast.For,
        ast.Subscript,
        ast.Slice,
        ast.BinOp,
        ast.Add,
        ast.keyword,
    )
    allowed_methods = {"read", "write", "replace", "count", "index"}
    for node in ast.walk(tree):
        if not isinstance(node, allowed_nodes):
            raise ValueError(
                f"{run_id}: unsupported {type(node).__name__} in recorded repair"
            )
        if isinstance(node, ast.Import):
            if [alias.name for alias in node.names] != ["re"]:
                raise ValueError(f"{run_id}: unsupported import in recorded repair")
        elif isinstance(node, ast.FunctionDef):
            if node.name != "rep" or node.decorator_list:
                raise ValueError(f"{run_id}: unsupported helper in recorded repair")
        elif isinstance(node, ast.Attribute) and node.attr not in allowed_methods:
            raise ValueError(f"{run_id}: unsupported method in recorded repair")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id not in {"open", "rep"}:
                raise ValueError(f"{run_id}: unsupported call in recorded repair")
        elif isinstance(node, ast.BinOp) and not isinstance(node.op, ast.Add):
            raise ValueError(f"{run_id}: unsupported operation in recorded repair")
        elif isinstance(node, ast.Name) and node.id.startswith("__"):
            raise ValueError(f"{run_id}: private name in recorded repair")

    tree.body = [statement for statement in tree.body if not isinstance(statement, ast.Import)]
    file_content = content
    write_count = 0

    class MemoryFile:
        def read(self) -> str:
            return file_content

        def write(self, value: str) -> int:
            nonlocal file_content, write_count
            if not isinstance(value, str):
                raise ValueError(f"{run_id}: recorded write is not text")
            file_content = value
            write_count += 1
            return len(value)

    def safe_open(path: str, mode: str = "r") -> MemoryFile:
        if path != "index.html" or mode not in {"r", "w"}:
            raise ValueError(f"{run_id}: repair accesses an unexpected file")
        return MemoryFile()

    namespace = {"__builtins__": {"open": safe_open}}
    exec(compile(tree, "<recorded-repair>", "exec"), namespace)  # noqa: S102
    if write_count != 1:
        raise ValueError(f"{run_id}: recorded repair did not write index.html")
    return file_content


def replay_recorded_shell_mutation(
    content: str, command: str, run_id: str
) -> tuple[str, int]:
    """Replay narrowly supported shell mutations without executing recorded code."""
    for marker, terminator in (
        ("python3 - <<'PY'\n", "PY"),
        ("python3 - <<'EOF'\n", "EOF"),
    ):
        start = command.find(marker)
        if start >= 0:
            script_start = start + len(marker)
            match = re.search(
                rf"(?m)^{re.escape(terminator)}$", command[script_start:]
            )
            end = script_start + match.start() if match else -1
            if end >= 0 and "open(p,'w').write" in command[:end]:
                script = command[script_start:end]
                return replay_safe_python_write(content, script, run_id), 1

    first_command = command.split(" && ", 1)[0]
    if not first_command.startswith("sed -i "):
        return content, 0
    arguments = shlex.split(first_command)
    if (
        len(arguments) == 4
        and arguments[:2] == ["sed", "-i"]
        and arguments[3] == "index.html"
    ):
        if not re.fullmatch(r"s/(?:\\.|[^/])*/(?:\\.|[^/])*/g?", arguments[2]):
            raise ValueError(f"{run_id}: unsupported recorded sed expression")
        result = subprocess.run(
            ["sed", arguments[2]],
            input=content,
            text=True,
            capture_output=True,
            check=True,
        )
        return result.stdout, 1
    return content, 0


def component_file_key(
    path: str, cwd: Path, components: tuple[str, ...]
) -> str | None:
    """Return a known component name for a path inside the recorded build tree."""
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = cwd / candidate
    candidate = candidate.resolve()
    component_root = Path("/tmp/ramen/src")
    if candidate.parent != component_root or candidate.name not in components:
        return None
    return candidate.name


def replay_safe_component_python(
    files: dict[str, str],
    script: str,
    cwd: Path,
    components: tuple[str, ...],
    run_id: str,
) -> int:
    """Replay a validated string-only Python edit against in-memory components."""
    tree = ast.parse(script)
    allowed_nodes = (
        ast.Module,
        ast.Import,
        ast.alias,
        ast.Assign,
        ast.Assert,
        ast.Expr,
        ast.Call,
        ast.Name,
        ast.Load,
        ast.Store,
        ast.Constant,
        ast.Attribute,
        ast.Compare,
        ast.Eq,
        ast.GtE,
        ast.In,
        ast.NotIn,
        ast.IfExp,
        ast.List,
        ast.Tuple,
        ast.For,
        ast.Subscript,
        ast.Slice,
        ast.BinOp,
        ast.Add,
        ast.BoolOp,
        ast.And,
        ast.ListComp,
        ast.comprehension,
        ast.keyword,
    )
    allowed_methods = {
        "read",
        "write",
        "replace",
        "count",
        "index",
        "split",
        "startswith",
    }
    for node in ast.walk(tree):
        if not isinstance(node, allowed_nodes):
            raise ValueError(
                f"{run_id}: unsupported {type(node).__name__} in component repair"
            )
        if isinstance(node, ast.Import):
            if [alias.name for alias in node.names] != ["re"]:
                raise ValueError(f"{run_id}: unsupported import in component repair")
        elif isinstance(node, ast.Attribute) and node.attr not in allowed_methods:
            raise ValueError(f"{run_id}: unsupported method in component repair")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id not in {"open", "print"}:
                raise ValueError(f"{run_id}: unsupported call in component repair")
        elif isinstance(node, ast.BinOp) and not isinstance(node.op, ast.Add):
            raise ValueError(f"{run_id}: unsupported operation in component repair")
        elif isinstance(node, ast.Name) and node.id.startswith("__"):
            raise ValueError(f"{run_id}: private name in component repair")

    tree.body = [statement for statement in tree.body if not isinstance(statement, ast.Import)]
    write_count = 0

    class MemoryFile:
        def __init__(self, key: str) -> None:
            self.key = key

        def read(self) -> str:
            return files[self.key]

        def write(self, value: str) -> int:
            nonlocal write_count
            if not isinstance(value, str):
                raise ValueError(f"{run_id}: recorded component write is not text")
            files[self.key] = value
            write_count += 1
            return len(value)

    def safe_open(path: str, mode: str = "r") -> MemoryFile:
        if not isinstance(path, str) or mode not in {"r", "w"}:
            raise ValueError(f"{run_id}: component repair uses unsupported open")
        key = component_file_key(path, cwd, components)
        if key is None or key not in files:
            raise ValueError(f"{run_id}: component repair accesses an unexpected file")
        return MemoryFile(key)

    namespace = {
        "__builtins__": {
            "open": safe_open,
            "print": lambda *args, **kwargs: None,
        }
    }
    exec(compile(tree, "<recorded-component-repair>", "exec"), namespace)  # noqa: S102
    if write_count == 0:
        raise ValueError(f"{run_id}: recorded component repair wrote no files")
    return write_count


def replay_component_python_heredocs(
    files: dict[str, str],
    command: str,
    components: tuple[str, ...],
    run_id: str,
) -> int:
    """Find and replay Python heredocs that mutate known build components."""
    operation_count = 0
    pattern = re.compile(r"python3 - <<'([^']+)'\n")
    for match in pattern.finditer(command):
        terminator = match.group(1)
        script_start = match.end()
        end_match = re.search(
            rf"(?m)^{re.escape(terminator)}$", command[script_start:]
        )
        if not end_match:
            raise ValueError(f"{run_id}: malformed recorded Python heredoc")
        script = command[script_start : script_start + end_match.start()]
        constants = {
            Path(node.value).name
            for node in ast.walk(ast.parse(script))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        if not constants.intersection(components):
            continue
        prefix = command[: match.start()]
        cwd_matches = re.findall(
            r"(?:^|[;\n])\s*cd\s+([^\s;&]+)\s+&&", prefix
        )
        cwd = Path(cwd_matches[-1]) if cwd_matches else Path("/tmp/ramen")
        operation_count += replay_safe_component_python(
            files, script, cwd, components, run_id
        )
    return operation_count


def replay_component_sed(
    files: dict[str, str],
    command: str,
    components: tuple[str, ...],
    run_id: str,
) -> int:
    """Replay simple recorded sed substitutions against build components."""
    operation_count = 0
    pattern = re.compile(
        r"\bsed\s+-i\s+(?P<expr>'(?:\\.|[^'])*'|\"(?:\\.|[^\"])*\")"
        r"\s+(?P<path>[^\s;&]+)"
    )
    for match in pattern.finditer(command):
        arguments = shlex.split(
            f"sed -i {match.group('expr')} {match.group('path')}"
        )
        if len(arguments) != 4:
            raise ValueError(f"{run_id}: malformed recorded component sed")
        prefix = command[: match.start()]
        cwd_matches = re.findall(
            r"(?:^|[;\n])\s*cd\s+([^\s;&]+)\s+&&", prefix
        )
        cwd = Path(cwd_matches[-1]) if cwd_matches else Path("/tmp/ramen")
        key = component_file_key(arguments[3], cwd, components)
        if key is None:
            continue
        expression = arguments[2]
        if not re.fullmatch(r"s/(?:\\.|[^/])*/(?:\\.|[^/])*/g?", expression):
            raise ValueError(f"{run_id}: unsupported recorded component sed")
        result = subprocess.run(
            ["sed", expression],
            input=files[key],
            text=True,
            capture_output=True,
            check=True,
        )
        files[key] = result.stdout
        operation_count += 1
    return operation_count


def replay_component_tail(
    files: dict[str, str], command: str, run_id: str
) -> int:
    """Replay the recorded tail-component heredoc used by the modular build."""
    marker = "cat > src/99-tail.html <<'"
    start = command.find(marker)
    if start < 0:
        return 0
    delimiter_start = start + len(marker)
    delimiter_end = command.find("'\n", delimiter_start)
    if delimiter_end < 0:
        raise ValueError(f"{run_id}: malformed recorded tail heredoc")
    terminator = command[delimiter_start:delimiter_end]
    content_start = delimiter_end + 2
    end_match = re.search(
        rf"(?m)^{re.escape(terminator)}$", command[content_start:]
    )
    if not end_match:
        raise ValueError(f"{run_id}: unterminated recorded tail heredoc")
    files["99-tail.html"] = command[
        content_start : content_start + end_match.start()
    ]
    return 1


def verify_claude_component_result(
    rows: list[dict[str, Any]], run_id: str
) -> str:
    """Replay a recorded modular Claude build and require an exact artifact match."""
    components = CLAUDE_COMPONENT_BUILDS[run_id]
    files: dict[str, str] = {}
    content: str | None = None
    operation_count = 0
    for row in rows:
        if row.get("type") != "assistant":
            continue
        blocks = row.get("message", {}).get("content", [])
        for block in blocks if isinstance(blocks, list) else []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool = block.get("name")
            tool_input = block.get("input", {})
            if not isinstance(tool_input, dict):
                continue
            if tool == "Write":
                file_path = str(tool_input.get("file_path", ""))
                key = component_file_key(file_path, Path("/"), components)
                if key is None:
                    continue
                recorded = tool_input.get("content")
                if not isinstance(recorded, str):
                    raise ValueError(f"{run_id}: component Write has no text content")
                files[key] = recorded
                operation_count += 1
            elif tool == "Bash":
                command = tool_input.get("command")
                if not isinstance(command, str):
                    continue
                operation_count += replay_component_tail(files, command, run_id)
                operation_count += replay_component_python_heredocs(
                    files, command, components, run_id
                )
                operation_count += replay_component_sed(
                    files, command, components, run_id
                )
                if "./build.sh" in command:
                    missing = [name for name in components if name not in files]
                    if missing:
                        raise ValueError(
                            f"{run_id}: recorded build is missing components {missing}"
                        )
                    content = "".join(files[name] for name in components)
                    operation_count += 1
    if content is None:
        raise ValueError(f"{run_id}: no recorded component build found")
    artifact = (ROOT / run_id / "index.html").read_text()
    if content != artifact:
        raise ValueError(f"{run_id}: index.html differs from recorded component build")
    return f"recorded component-build replay ({operation_count} operations)"


def replay_codex_command(
    content: str | None, command: str, run_id: str
) -> tuple[str | None, int]:
    """Replay supported direct index.html writes from recorded shell commands."""
    heredoc_start = "cat > index.html <<'EOF'\n"
    if command.startswith(heredoc_start):
        delimiter = "\nEOF\n"
        end = command.find(delimiter, len(heredoc_start))
        if end < 0:
            raise ValueError(f"{run_id}: malformed recorded index.html heredoc")
        return command[len(heredoc_start) : end] + "\n", 1

    if content is not None:
        return replay_recorded_shell_mutation(content, command, run_id)
    return content, 0


def verify_codex_result(rows: list[dict[str, Any]], run_id: str) -> str:
    """Replay recorded Codex file mutations and require an exact artifact match."""
    content: str | None = None
    operation_count = 0
    for row in rows:
        payload = row.get("payload", {})
        if row.get("type") != "response_item" or payload.get(
            "type"
        ) not in {"custom_tool_call", "function_call"}:
            continue
        source = str(payload.get("input", payload.get("arguments", "")))
        patch = recorded_codex_patch(source)
        if patch is not None:
            line_content = content.splitlines() if content is not None else None
            line_content, count = replay_codex_patch(line_content, patch, run_id)
            content = (
                "\n".join(line_content) + "\n" if line_content is not None else None
            )
            operation_count += count
            continue
        command = recorded_codex_command(source)
        if command is not None:
            content, count = replay_codex_command(content, command, run_id)
            operation_count += count
    if content is None:
        raise ValueError(f"{run_id}: no recorded index.html creation found")
    artifact = (ROOT / run_id / "index.html").read_text()
    if content != artifact:
        raise ValueError(f"{run_id}: index.html differs from recorded file operations")
    return f"recorded file-operation replay ({operation_count} operations)"


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
    source_root = CODEX_SOURCE_ROOTS.get(run_id, HOME / ".codex-personal")
    source = source_root / "sessions" / date_dir / filename
    rows = read_jsonl(source)
    meta = next(row["payload"] for row in rows if row.get("type") == "session_meta")
    context = next(row["payload"] for row in rows if row.get("type") == "turn_context")
    expected_cwd = CODEX_EXPECTED_CWDS.get(run_id, ROOT / run_id)
    expected_model = run_id.split("/")[1]
    expected_effort = run_id.rsplit("/", 1)[1]
    if (
        meta.get("cwd") != str(expected_cwd)
        or context.get("model") != expected_model
        or context.get("effort") != expected_effort
    ):
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
    if run_id in CODEX_REPLAY_VERIFICATION:
        verification = verify_codex_result(rows, run_id)
        notes += f" The result matches its {verification}."
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
    cost, cost_calculation = estimated_total_only_cost(
        pricing, total_tokens=total_tokens, effort=effort
    )
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
        "cost": cost,
        "turns": 1,
        "toolCalls": tool_calls,
        "providerReported": True,
        "raw": {
            "source": "Codex terminal tokens used summary",
            "totalTokens": total_tokens,
            "tokenCategoryBreakdownAvailable": False,
            "pricing": pricing,
            "costCalculation": cost_calculation,
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
        f"LiteLLM {EXPECTED_LITELLM_VERSION} model_cost[{model_slug}]; "
        f"token allocation estimated from openai/gpt-6-astra/{effort}"
    )
    notes = (
        "The Codex terminal record reports an exact total token count without category "
        "breakdown. Cost was estimated with this model's LiteLLM rates after allocating "
        "that total using the same-effort GPT 6 Astra run's recorded input, cache, and "
        "output proportions; the exact total token field is unchanged. The public "
        "transcript contains recorded text and full recorded tool activity; hidden "
        "reasoning, credentials, and private local path prefixes are excluded."
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
    return run, transcript, "estimated-total-only"


def tracked_run_ids() -> list[str]:
    output = subprocess.check_output(
        [
            "git",
            "ls-files",
            "anthropic/**/run.json",
            "openai/**/run.json",
            "z.ai/**/run.json",
        ],
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
    parser.add_argument(
        "--run",
        action="append",
        dest="run_ids",
        help="regenerate one mapped run id (repeatable; permits onboarding an untracked run)",
    )
    args = parser.parse_args(argv)

    installed_version = package_version("litellm")
    if installed_version != EXPECTED_LITELLM_VERSION:
        raise RuntimeError(
            f"expected LiteLLM {EXPECTED_LITELLM_VERSION}, found {installed_version}"
        )

    expected = set(CLAUDE_SOURCES) | set(CODEX_SESSION_SOURCES) | set(LOG_STARTS) | {
        TERRA_LOW_ID
    }
    all_run_ids = tracked_run_ids()
    if not args.run_ids and set(all_run_ids) != expected:
        missing_sources = sorted(set(all_run_ids) - expected)
        missing_runs = sorted(expected - set(all_run_ids))
        raise ValueError(
            f"source map mismatch; missing sources={missing_sources}, missing runs={missing_runs}"
        )
    requested = list(dict.fromkeys(args.run_ids or all_run_ids))
    unknown = sorted(set(requested) - expected)
    if unknown:
        raise ValueError(f"no source mapping for requested runs: {unknown}")
    run_ids = []
    for run_id in requested:
        run_harness = "claude-code" if run_id in CLAUDE_SOURCES else "codex"
        if args.harness != "all" and args.harness != run_harness:
            raise ValueError(f"{run_id} is not a {args.harness} run")
        run_ids.append(run_id)

    coverage = {
        "claudeProviderCost": 0,
        "claudeLiteLLMCost": 0,
        "openaiLiteLLMCost": 0,
        "openaiTotalOnlyEstimatedCost": 0,
        "events": 0,
        "base64Omissions": 0,
        "base64CharactersOmitted": 0,
    }
    changed_paths = []
    for run_id in run_ids:
        if run_id in CLAUDE_SOURCES:
            run, transcript, cost_kind = build_claude(run_id)
            coverage[
                "claudeProviderCost"
                if cost_kind == "provider"
                else "claudeLiteLLMCost"
            ] += 1
        elif run_id in CODEX_SESSION_SOURCES:
            run, transcript, _ = build_codex_session(run_id)
            coverage["openaiLiteLLMCost"] += 1
        else:
            run, transcript, _ = build_codex_log(run_id)
            coverage["openaiTotalOnlyEstimatedCost"] += 1
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
        f"Claude Code provider cost={coverage['claudeProviderCost']}, "
        f"Claude Code LiteLLM cost={coverage['claudeLiteLLMCost']}, "
        f"OpenAI LiteLLM cost={coverage['openaiLiteLLMCost']}, "
        "OpenAI total-only estimated cost="
        f"{coverage['openaiTotalOnlyEstimatedCost']}."
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
