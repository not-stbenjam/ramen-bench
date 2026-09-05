#!/usr/bin/env python3
"""Build Google run metadata and public transcripts from Antigravity records.

Antigravity stores the readable event stream as JSONL and exact per-request
usage in protobuf blobs inside each conversation SQLite database. This importer
publishes complete user-visible messages and tool activity, including generated
source bodies. It drops planner thinking and checkpoint contents, redacts
secret-like values and personal identifiers, and replaces private local paths.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
import shutil
import sqlite3
import struct
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import litellm


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from censor_transcripts import censor_text, censor_value  # noqa: E402

ANTIGRAVITY_ROOT = Path.home() / ".gemini" / "antigravity-cli"
RUN_SCHEMA = "https://not-stbenjam.github.io/ramen-bench/schemas/run.schema.json"
TRANSCRIPT_SCHEMA = (
    "https://not-stbenjam.github.io/ramen-bench/schemas/transcript.schema.json"
)

RUNS = (
    {
        "model": "gemini-3.7-flash",
        "model_name": "Gemini 3.7 Flash",
        "variation": "low",
        "variation_name": "Low",
        "source_workspace": "google/gemini-3.7-flash",
    },
    {
        "model": "gemini-3.7-flash",
        "model_name": "Gemini 3.7 Flash",
        "variation": "medium",
        "variation_name": "Medium",
    },
    {
        "model": "gemini-3.8-flash",
        "model_name": "Gemini 3.8 Flash",
        "variation": "low",
        "variation_name": "Low",
    },
    {
        "model": "gemini-3.8-flash",
        "model_name": "Gemini 3.8 Flash",
        "variation": "medium",
        "variation_name": "Medium",
    },
    {
        "model": "gemini-3.8-flash",
        "model_name": "Gemini 3.8 Flash",
        "variation": "high",
        "variation_name": "High",
    },
)

BENCHMARK_PROMPT_PREFIX = "Create a single-file HTML page (index.html)"


def read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, offset
        shift += 7
        if shift > 70:
            raise ValueError("invalid protobuf varint")
    raise ValueError("truncated protobuf varint")


def parse_protobuf(data: bytes) -> dict[int, list[tuple[int, Any]]]:
    """Decode protobuf wire fields without relying on private descriptors."""
    fields: dict[int, list[tuple[int, Any]]] = defaultdict(list)
    offset = 0
    while offset < len(data):
        key, offset = read_varint(data, offset)
        field_number, wire_type = key >> 3, key & 7
        if field_number == 0:
            raise ValueError("invalid protobuf field number")
        if wire_type == 0:
            value, offset = read_varint(data, offset)
        elif wire_type == 1:
            if offset + 8 > len(data):
                raise ValueError("truncated fixed64")
            value = struct.unpack_from("<Q", data, offset)[0]
            offset += 8
        elif wire_type == 2:
            size, offset = read_varint(data, offset)
            if offset + size > len(data):
                raise ValueError("truncated length-delimited field")
            value = data[offset : offset + size]
            offset += size
        elif wire_type == 5:
            if offset + 4 > len(data):
                raise ValueError("truncated fixed32")
            value = struct.unpack_from("<I", data, offset)[0]
            offset += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire_type}")
        fields[field_number].append((wire_type, value))
    return fields


def first_bytes(fields: dict[int, list[tuple[int, Any]]], number: int) -> bytes | None:
    for wire_type, value in fields.get(number, []):
        if wire_type == 2:
            return value
    return None


def first_int(fields: dict[int, list[tuple[int, Any]]], number: int) -> int:
    for wire_type, value in fields.get(number, []):
        if wire_type == 0:
            return int(value)
    return 0


def decode_timestamp(data: bytes | None) -> datetime | None:
    if not data:
        return None
    fields = parse_protobuf(data)
    seconds = first_int(fields, 1)
    nanos = first_int(fields, 2)
    if seconds < 1_500_000_000 or seconds > 2_500_000_000 or nanos >= 1_000_000_000:
        return None
    return datetime.fromtimestamp(seconds + nanos / 1_000_000_000, UTC)


def isoformat(timestamp: datetime) -> str:
    return timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def snapshot_database(source: Path, destination: Path) -> Path:
    """Copy the database and an optional live WAL into a writable directory."""
    copied = destination / source.name
    shutil.copy2(source, copied)
    for suffix in ("-wal", "-shm"):
        companion = Path(f"{source}{suffix}")
        if companion.exists():
            shutil.copy2(companion, Path(f"{copied}{suffix}"))
    return copied


def usage_from_stats(data: bytes) -> dict[str, int]:
    """Decode Antigravity ModelUsageStats fields embedded in step metadata.

    The wire mapping comes from the ModelUsageStats protobuf metadata embedded
    in Antigravity CLI 1.1.26:
      2 input_tokens, 3 output_tokens, 5 cache_read_tokens,
      9 thinking_output_tokens, 10 response_output_tokens,
      23 tool_call_output_tokens.
    """
    fields = parse_protobuf(data)
    return {
        "inputTokens": first_int(fields, 2),
        "outputTokens": first_int(fields, 3),
        "cacheReadTokens": first_int(fields, 5),
        "thinkingOutputTokens": first_int(fields, 9),
        "responseOutputTokens": first_int(fields, 10),
        "toolCallOutputTokens": first_int(fields, 23),
    }


def extract_usage_and_timing(database: Path) -> tuple[dict[str, int], dict[str, Any]]:
    totals = defaultdict(int)
    timestamps: list[datetime] = []
    api_duration_ms = 0
    requests = 0

    with tempfile.TemporaryDirectory(prefix="ramen-antigravity-") as temp_dir:
        snapshot = snapshot_database(database, Path(temp_dir))
        connection = sqlite3.connect(snapshot)
        rows = connection.execute(
            "SELECT idx, step_type, status, metadata FROM steps ORDER BY idx"
        ).fetchall()
        connection.close()

    for _index, step_type, _status, metadata in rows:
        fields = parse_protobuf(metadata)
        for number in (1, 6, 7, 8, 32):
            timestamp = decode_timestamp(first_bytes(fields, number))
            if timestamp:
                timestamps.append(timestamp)

        stats_blob = None
        started = decode_timestamp(first_bytes(fields, 6))
        completed = decode_timestamp(first_bytes(fields, 7))
        if step_type == 15:
            stats_blob = first_bytes(fields, 9)
        elif step_type == 23:
            checkpoint_blob = first_bytes(fields, 28)
            if checkpoint_blob:
                checkpoint_fields = parse_protobuf(checkpoint_blob)
                stats_blob = first_bytes(checkpoint_fields, 2)
            started = decode_timestamp(first_bytes(fields, 1))
            completed = decode_timestamp(first_bytes(fields, 8))

        if not stats_blob:
            continue
        request_usage = usage_from_stats(stats_blob)
        if not any(request_usage.values()):
            continue
        requests += 1
        for key, value in request_usage.items():
            totals[key] += value
        if started and completed and completed >= started:
            api_duration_ms += round((completed - started).total_seconds() * 1000)

    if not timestamps:
        raise ValueError(f"no timestamps found in {database}")
    started_at, completed_at = min(timestamps), max(timestamps)
    timing = {
        "startedAt": isoformat(started_at),
        "completedAt": isoformat(completed_at),
        "wallDurationMs": round((completed_at - started_at).total_seconds() * 1000),
        "apiDurationMs": api_duration_ms,
        "requests": requests,
    }
    return dict(totals), timing


def discover_sources(
    config: dict[str, str], run_id: str
) -> tuple[str, Path, Path, Path]:
    """Find the latest matching local session without publishing its identifiers."""
    source_workspace = REPO_ROOT / config.get("source_workspace", run_id)
    history_path = ANTIGRAVITY_ROOT / "history.jsonl"
    candidates: list[tuple[int, str]] = []
    for line in history_path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("workspace") != str(source_workspace):
            continue
        if not str(row.get("display", "")).startswith(BENCHMARK_PROMPT_PREFIX):
            continue
        if row.get("conversationId"):
            candidates.append((int(row.get("timestamp", 0)), row["conversationId"]))
    if not candidates:
        raise ValueError(f"no Antigravity session found for {run_id}")
    _timestamp, session_id = max(candidates)

    database = ANTIGRAVITY_ROOT / "conversations" / f"{session_id}.db"
    transcript = (
        ANTIGRAVITY_ROOT
        / "brain"
        / session_id
        / ".system_generated"
        / "logs"
        / "transcript_full.jsonl"
    )
    selection = f'{config["model_name"]} ({config["variation_name"]})'
    cli_log = None
    for candidate in sorted(
        (ANTIGRAVITY_ROOT / "log").glob("cli-*.log"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    ):
        contents = candidate.read_text(errors="replace")
        if (
            session_id in contents
            and str(source_workspace) in contents
            and f'label="{selection}"' in contents
        ):
            cli_log = candidate
            break
    if cli_log is None:
        raise ValueError(f"no matching Antigravity CLI log found for {run_id}")
    return session_id, database, transcript, cli_log


def scrub_text(
    value: str, redactions: dict[str, int], omissions: dict[str, int]
) -> str:
    stats: Counter[str] = Counter()
    cleaned = censor_text(
        value,
        workspace=REPO_ROOT,
        home=Path.home(),
        session_roots={ANTIGRAVITY_ROOT: "<SESSION_STORE>"},
        stats=stats,
    )
    for key, amount in stats.items():
        if key == "binary_payloads":
            omissions["binaryPayloads"] += amount
        elif key == "binary_characters":
            omissions["binaryPayloadCharacters"] += amount
        else:
            output_key = {
                "private_paths": "privateAbsolutePaths",
                "usernames": "personalNames",
                "emails": "emailAddresses",
                "lan_addresses": "lanAddresses",
                "secrets": "secretLikeValues",
                "sensitive_fields": "secretLikeValues",
            }.get(key, key)
            redactions[output_key] = redactions.get(output_key, 0) + amount
    return cleaned


def scrub_value(
    value: Any, redactions: dict[str, int], omissions: dict[str, int]
) -> Any:
    stats: Counter[str] = Counter()
    cleaned = censor_value(
        value,
        workspace=REPO_ROOT,
        home=Path.home(),
        session_roots={ANTIGRAVITY_ROOT: "<SESSION_STORE>"},
        stats=stats,
    )
    for key, amount in stats.items():
        if key == "binary_payloads":
            omissions["binaryPayloads"] += amount
        elif key == "binary_characters":
            omissions["binaryPayloadCharacters"] += amount
        else:
            output_key = {
                "private_paths": "privateAbsolutePaths",
                "usernames": "personalNames",
                "emails": "emailAddresses",
                "lan_addresses": "lanAddresses",
                "secrets": "secretLikeValues",
                "sensitive_fields": "secretLikeValues",
            }.get(key, key)
            redactions[output_key] = redactions.get(output_key, 0) + amount
    return cleaned


def public_tool_input(
    arguments: dict[str, Any],
    redactions: dict[str, int],
    omissions: dict[str, int],
) -> dict[str, Any]:
    return scrub_value(arguments, redactions, omissions)


def extract_user_request(content: str) -> str:
    match = re.search(r"<USER_REQUEST>\n(.*?)\n</USER_REQUEST>", content, re.DOTALL)
    if not match:
        raise ValueError("Antigravity transcript does not contain USER_REQUEST markers")
    return match.group(1)


def load_transcript(path: Path, run_id: str) -> tuple[dict[str, Any], int]:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    events: list[dict[str, Any]] = []
    pending_calls: list[tuple[str, str]] = []
    tool_call_count = 0
    omitted_thinking_steps = 0
    omitted_checkpoint_steps = 0
    redactions = {
        "privateAbsolutePaths": 0,
        "personalNames": 0,
        "emailAddresses": 0,
        "lanAddresses": 0,
        "secretLikeValues": 0,
    }
    binary_omissions = {
        "binaryPayloads": 0,
        "binaryPayloadCharacters": 0,
    }

    for row in rows:
        timestamp = row.get("created_at")
        if row.get("thinking"):
            omitted_thinking_steps += 1
        if row.get("source") == "SYSTEM":
            omitted_checkpoint_steps += 1
            continue

        if row.get("type") == "USER_INPUT" and row.get("source") == "USER_EXPLICIT":
            events.append(
                {
                    "type": "message",
                    "role": "user",
                    "content": scrub_text(
                        extract_user_request(row.get("content", "")),
                        redactions,
                        binary_omissions,
                    ),
                    "timestamp": timestamp,
                }
            )
            continue

        if row.get("type") == "PLANNER_RESPONSE" and row.get("source") == "MODEL":
            content = row.get("content", "")
            if content:
                events.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": scrub_text(content, redactions, binary_omissions),
                        "timestamp": timestamp,
                    }
                )
            for call_index, call in enumerate(row.get("tool_calls", [])):
                call_id = f"step-{row['step_index']}-call-{call_index}"
                tool = call.get("name", "unknown")
                events.append(
                    {
                        "type": "tool_call",
                        "id": call_id,
                        "tool": tool,
                        "input": public_tool_input(
                            call.get("args") or {}, redactions, binary_omissions
                        ),
                        "timestamp": timestamp,
                    }
                )
                pending_calls.append((call_id, tool))
                tool_call_count += 1
            continue

        if row.get("type") == "GENERIC" and row.get("source") == "MODEL" and pending_calls:
            call_id, _tool = pending_calls.pop(0)
            content = row.get("content", "")
            events.append(
                {
                    "type": "tool_result",
                    "callId": call_id,
                    "status": "success" if row.get("status") == "DONE" else "error",
                    "output": scrub_text(content, redactions, binary_omissions),
                    "timestamp": timestamp,
                }
            )

    transcript = {
        "$schema": TRANSCRIPT_SCHEMA,
        "schemaVersion": 1,
        "runId": run_id,
        "sourceFormat": "antigravity-cli-transcript-full-jsonl+sqlite-protobuf",
        "events": events,
        "metadata": {
            "source": "Antigravity CLI full public transcript export",
            "omissions": {
                "hiddenPlannerThinkingSteps": omitted_thinking_steps,
                "systemCheckpointSteps": omitted_checkpoint_steps,
                **binary_omissions,
            },
            "redactions": redactions,
            "toolPayloads": "Complete recorded tool inputs and outputs are retained.",
            "sourceIntegrity": {
                "sourceRows": len(rows),
                "publishedEvents": len(events),
                "toolCallsWithoutRecordedResult": len(pending_calls),
            },
            "viewer": f"../../../transcript.html#{quote(run_id, safe='')}",
        },
    }
    return transcript, tool_call_count


def read_cli_version(
    log_path: Path, session_id: str, model_name: str, variation_name: str
) -> str:
    contents = log_path.read_text(errors="replace")
    if session_id not in contents:
        raise ValueError(f"session {session_id} is not present in {log_path}")
    selection = f"{model_name} ({variation_name})"
    if f'label="{selection}"' not in contents:
        raise ValueError(f"model selection {selection} is not present in {log_path}")
    match = re.search(r"Language server version: ([^\s]+)", contents)
    if not match:
        raise ValueError(f"language server version is missing from {log_path}")
    return match.group(1)


def calculate_cost(model: str, usage: dict[str, int]) -> tuple[dict[str, Any], dict[str, Any]]:
    model_key = f"vertex_ai/{model}"
    pricing = litellm.model_cost[model_key]
    input_rate = pricing["input_cost_per_token"]
    cache_rate = pricing["cache_read_input_token_cost"]
    output_rate = pricing["output_cost_per_token"]
    input_cost = usage["inputTokens"] * input_rate
    cached_cost = usage["cacheReadTokens"] * cache_rate
    output_cost = usage["outputTokens"] * output_rate
    cost = {
        "currency": "USD",
        "input": round(input_cost, 12),
        "cachedInput": round(cached_cost, 12),
        "cacheCreationInput": 0,
        "output": round(output_cost, 12),
        "other": 0,
        "total": round(input_cost + cached_cost + output_cost, 12),
        "estimated": True,
    }
    rate_record = {
        "catalog": "LiteLLM",
        "catalogVersion": importlib.metadata.version("litellm"),
        "modelKey": model_key,
        "inputCostPerToken": input_rate,
        "cacheReadInputCostPerToken": cache_rate,
        "outputCostPerToken": output_rate,
        "source": pricing["source"],
        "endpointRegion": "global",
    }
    return cost, rate_record


def line_count(path: Path) -> int:
    with path.open("rb") as stream:
        return sum(1 for _line in stream)


def build_run(config: dict[str, str]) -> tuple[Path, Path]:
    model = config["model"]
    variation = config["variation"]
    run_id = f"google/{model}/{variation}"
    run_dir = REPO_ROOT / run_id
    result_path = run_dir / "index.html"
    session_id, database_path, transcript_source, cli_log = discover_sources(
        config, run_id
    )
    for required in (result_path, database_path, transcript_source, cli_log):
        if not required.exists():
            raise FileNotFoundError(required)

    cli_version = read_cli_version(
        cli_log, session_id, config["model_name"], config["variation_name"]
    )
    provider_usage, timing = extract_usage_and_timing(database_path)
    transcript, tool_calls = load_transcript(transcript_source, run_id)
    cost, rates = calculate_cost(model, provider_usage)
    tokens = {
        "input": provider_usage["inputTokens"],
        "cachedInput": provider_usage["cacheReadTokens"],
        "cacheCreationInput": 0,
        "reasoning": provider_usage["thinkingOutputTokens"],
        "output": provider_usage["outputTokens"],
        "total": (
            provider_usage["inputTokens"]
            + provider_usage["cacheReadTokens"]
            + provider_usage["outputTokens"]
        ),
    }
    run = {
        "$schema": RUN_SCHEMA,
        "schemaVersion": 1,
        "id": run_id,
        "vendor": {"slug": "google", "displayName": "Google"},
        "model": {
            "slug": model,
            "displayName": config["model_name"],
            "providerModelId": model,
        },
        "variation": {
            "slug": variation,
            "displayName": config["variation_name"],
            "reasoningEffort": variation,
            "parameters": {"modelSelection": f"{config['model_name']} ({config['variation_name']})"},
        },
        "harness": {
            "name": "Antigravity CLI",
            "version": cli_version,
            "runtime": "Google Antigravity",
            "invocation": "agy --dangerously-skip-permissions (interactive model selection)",
        },
        "artifacts": {"result": "index.html", "transcript": "transcript.json"},
        "timing": {
            "startedAt": timing["startedAt"],
            "completedAt": timing["completedAt"],
            "wallDurationMs": timing["wallDurationMs"],
            "apiDurationMs": timing["apiDurationMs"],
        },
        "usage": {
            "tokens": tokens,
            "cost": cost,
            "requests": timing["requests"],
            "turns": 1,
            "toolCalls": tool_calls,
            "providerReported": True,
            "raw": {
                "antigravityModelUsageStats": provider_usage,
                "pricing": rates,
            },
        },
        "resultStats": {"bytes": result_path.stat().st_size, "lines": line_count(result_path)},
        "provenance": {
            "generatedAt": timing["completedAt"],
            "pricingSource": (
                f"LiteLLM {rates['catalogVersion']} {rates['modelKey']} rate catalog"
            ),
        },
        "notes": (
            "Tokens are exact provider usage reconstructed from Antigravity CLI 1.1.26 "
            "assistant/checkpoint step protobuf metadata. inputTokens excludes separately "
            "reported cacheReadTokens; outputTokens includes thinkingOutputTokens and "
            "responseOutputTokens. API duration sums recorded model request intervals. "
            "USD cost is estimated from the retained LiteLLM Vertex AI global-endpoint rates; "
            "Antigravity did not record provider cost. The public transcript retains complete "
            "recorded visible messages and tool inputs/outputs, including generated source. "
            "Only hidden planner thinking and system checkpoint bodies are omitted; secret-like "
            "values, personal identifiers, and private paths are replaced with neutral "
            "placeholders."
        ),
    }

    run_path = run_dir / "run.json"
    transcript_path = run_dir / "transcript.json"
    run_path.write_text(json.dumps(run, indent=2, ensure_ascii=False) + "\n")
    transcript_path.write_text(json.dumps(transcript, indent=2, ensure_ascii=False) + "\n")
    return run_path, transcript_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-sources",
        action="store_true",
        help="verify and decode sources without writing run files",
    )
    arguments = parser.parse_args()

    outputs: list[Path] = []
    for config in RUNS:
        run_id = f"google/{config['model']}/{config['variation']}"
        if arguments.check_sources:
            session_id, database, _transcript, log = discover_sources(config, run_id)
            read_cli_version(
                log,
                session_id,
                config["model_name"],
                config["variation_name"],
            )
            usage, timing = extract_usage_and_timing(database)
            print(
                f"{run_id}: "
                f"{timing['requests']} requests, "
                f"{sum((usage.get('inputTokens', 0), usage.get('cacheReadTokens', 0), usage.get('outputTokens', 0)))} tokens"
            )
            continue
        outputs.extend(build_run(config))

    for output in outputs:
        print(output.relative_to(REPO_ROOT))


if __name__ == "__main__":
    main()
