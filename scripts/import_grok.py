#!/usr/bin/env python3
"""Import a completed Grok Build session without modifying its HTML artifact.

Example:
  python3 scripts/import_grok.py --session ~/.grok/sessions/.../SESSION_ID \
      --artifact /tmp/ramen/index.html --usage-file /tmp/usage.json \
      --harness-version 1.0.41

Capture usage with ``grok usage SESSION_ID > /tmp/usage.json``. Omit
--usage-file to query the installed harness directly. Re-run with
--check-sources to check both the recorded write and deterministic public JSON.
Image-embedding runs also require the source session's images/ directory; recorded
Python embedding expressions are evaluated as data without executing model code.
--completed-turns-only explicitly imports a completed prefix when the harness
started a trailing turn that never reached model or tool activity.
"""

from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import json
import re
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from censor_transcripts import censor_value
from censor_transcripts import main as censor_main

ROOT = Path(__file__).resolve().parents[1]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def iso_time(milliseconds: int) -> str:
    return (
        datetime.fromtimestamp(milliseconds / 1000, timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def normalize_updates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Allowlist public ACP messages/tools; never copy hidden thoughts or metadata."""
    events = []
    for row in rows:
        params = row["params"]
        update = params["update"]
        kind = update["sessionUpdate"]
        timestamp = iso_time(
            params.get("_meta", {}).get("agentTimestampMs", row["timestamp"] * 1000)
        )
        if kind in {"user_message_chunk", "agent_message_chunk"}:
            content = update.get("content", {})
            if content.get("type") != "text":
                continue
            events.append(
                {
                    "type": "message",
                    "role": "user" if kind.startswith("user_") else "assistant",
                    "content": content["text"],
                    "timestamp": timestamp,
                }
            )
        elif kind == "tool_call":
            tool = (
                update.get("_meta", {})
                .get("x.ai/tool", {})
                .get("name", update.get("title", "unknown"))
            )
            events.append(
                {
                    "type": "tool_call",
                    "id": update["toolCallId"],
                    "tool": tool,
                    "input": update.get("rawInput", {}),
                    "timestamp": timestamp,
                }
            )
        elif kind == "tool_call_update" and update.get("status") in {
            "completed",
            "failed",
        }:
            events.append(
                {
                    "type": "tool_result",
                    "callId": update["toolCallId"],
                    "status": "success" if update["status"] == "completed" else "error",
                    "output": update["rawOutput"]
                    if update.get("rawOutput") is not None
                    else update.get("content", []),
                    "timestamp": timestamp,
                }
            )
    return events


class StaticFiles:
    """Evaluate only recorded file/template expressions, never run model code."""

    def __init__(self, cwd: str, session: Path | None):
        self.cwd = Path(cwd)
        self.session = session
        self.files = {}
        self.variables = {}

    def path(self, value):
        return self.cwd / value

    def read(self, path):
        path = self.path(path)
        if path in self.files:
            value = self.files[path]
            return value.encode() if isinstance(value, str) else value
        # Only source-session generated images may be read from disk. In
        # particular, never use the output artifact itself as reconstruction input.
        if self.session and path.resolve().is_relative_to(
            (self.session / "images").resolve()
        ):
            return path.read_bytes()
        raise ValueError(f"Unrecorded file dependency: {path}")

    def expression(self, node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return self.variables[node.id]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return self.expression(node.left) + self.expression(node.right)
        if isinstance(node, ast.Call):
            args = [self.expression(arg) for arg in node.args]
            if isinstance(node.func, ast.Name) and node.func.id == "Path":
                return self.path(args[0])
            if isinstance(node.func, ast.Name) and node.func.id == "open":
                return (self.path(args[0]), args[1])
            if isinstance(node.func, ast.Attribute):
                attr = node.func.attr
                if (
                    isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "base64"
                    and attr == "b64encode"
                ):
                    return base64.b64encode(*args)
                value = self.expression(node.func.value)
                if isinstance(value, Path):
                    if attr == "read_bytes":
                        return self.read(value)
                    if attr == "read_text":
                        return self.read(value).decode()
                    if attr == "write_text":
                        self.files[value] = args[0]
                        return len(args[0])
                if isinstance(value, tuple) and len(value) == 2:
                    if attr == "read":
                        data = self.read(value[0])
                        return data if "b" in value[1] else data.decode()
                    if attr == "write" and value[1] == "w":
                        self.files[value[0]] = args[0]
                        return len(args[0])
                if isinstance(value, (str, bytes)) and attr in {
                    "replace",
                    "strip",
                    "decode",
                }:
                    return getattr(value, attr)(*args)
        raise ValueError(f"Unsupported recorded expression: {ast.dump(node)[:100]}")

    def python(self, source):
        self.variables = {}
        for node in ast.parse(source).body:
            if isinstance(node, (ast.Import, ast.ImportFrom, ast.Assert)):
                continue
            if isinstance(node, ast.If):
                # Observed guard only raises when an embedding placeholder is absent.
                if (
                    all(isinstance(item, ast.Raise) for item in node.body)
                    and not node.orelse
                ):
                    continue
                raise ValueError("Unsupported conditional in recorded write script")
            if isinstance(node, ast.With):
                if len(node.items) != 1 or not isinstance(
                    node.items[0].optional_vars, ast.Name
                ):
                    raise ValueError("Unsupported recorded file context")
                self.variables[node.items[0].optional_vars.id] = self.expression(
                    node.items[0].context_expr
                )
                for child in node.body:
                    self.assignment(child)
                continue
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                func = node.value.func
                if isinstance(func, ast.Name) and func.id == "print":
                    continue
                self.expression(node.value)
                continue
            # Statements after the final write often only inspect output for logs.
            self.assignment(node)

    def assignment(self, node):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            self.variables[node.targets[0].id] = self.expression(node.value)
            return
        raise ValueError("Unsupported statement in recorded write script")

    def shell(self, command):
        # Replay the observed copy-to-scratch operation in memory, not on disk.
        if command.startswith("cp "):
            tokens = shlex.split(command.split("&&", 1)[0])
            if len(tokens) == 3:
                self.files[self.path(tokens[2])] = self.read(self.path(tokens[1]))
        sources = re.findall(r"python3 - <<'PY'\n(.*?)\nPY(?:\n|$)", command, re.DOTALL)
        for match in re.finditer(r'python3 -c "((?:[^"\\]|\\.)*)"', command, re.DOTALL):
            sources.append(shlex.split(match.group(0))[2])
        for source in sources:
            if ".write_text(" not in source and not (
                "base64.b64encode" in source and ".write(" in source
            ):
                continue
            # Only replay through the last write; remaining statements are recorded
            # diagnostics and cannot affect the output. No Python is executed.
            tree = ast.parse(source)
            writes = [
                i
                for i, node in enumerate(tree.body)
                if any(
                    isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr in {"write_text", "write"}
                    for n in ast.walk(node)
                )
            ]
            if writes:
                tree.body = tree.body[: writes[-1] + 1]
                self.python(ast.unparse(tree))


def recorded_artifact(
    events: list[dict[str, Any]], cwd: str, session: Path | None = None
) -> bytes:
    """Replay successful writes, edits, and supported static image embedding."""
    successful = {
        e["callId"]
        for e in events
        if e["type"] == "tool_result" and e["status"] == "success"
    }
    state = StaticFiles(cwd, session)
    target = Path(cwd) / "index.html"
    for event in events:
        if event["type"] != "tool_call" or event["id"] not in successful:
            continue
        args = event["input"]
        if not isinstance(args, dict):
            continue
        name = event["tool"]
        if name == "run_terminal_command":
            state.shell(args["command"])
            continue
        if name not in {"write", "search_replace"}:
            continue
        path = state.path(args["file_path"])
        if name == "write":
            state.files[path] = args["content"]
            continue
        old = args["old_string"]
        if old == "":
            if path in state.files:
                raise ValueError("Recorded file creation follows an existing write")
            state.files[path] = args["new_string"]
            continue
        if path not in state.files:
            raise ValueError("Replacement before recorded initial write")
        text = state.files[path]
        if old not in text:
            raise ValueError("Recorded replacement cannot be replayed unambiguously")
        if not args.get("replace_all", False) and text.count(old) != 1:
            raise ValueError("Recorded replacement has multiple matches")
        state.files[path] = text.replace(
            old, args["new_string"], -1 if args.get("replace_all", False) else 1
        )
    if target not in state.files:
        raise ValueError("No successful recorded index.html write found")
    return state.read(target)


def serialize(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--usage-file", type=Path)
    parser.add_argument("--output-root", type=Path, default=ROOT)
    parser.add_argument("--harness-version", required=True)
    parser.add_argument("--check-sources", action="store_true")
    parser.add_argument(
        "--completed-turns-only",
        action="store_true",
        help="Import the completed prefix, excluding a trailing unfinished turn",
    )
    args = parser.parse_args()
    summary = json.loads((args.session / "summary.json").read_text())
    updates = read_jsonl(args.session / "updates.jsonl")
    telemetry = read_jsonl(args.session / "events.jsonl")
    if args.completed_turns_only:
        updates, telemetry = completed_prefix(updates, telemetry)
    session_id = summary["info"]["id"]
    if args.usage_file:
        usage = json.loads(args.usage_file.read_text())
    else:
        usage = json.loads(
            subprocess.check_output(["grok", "usage", session_id], text=True)
        )
    run, transcript = build_run(
        summary, updates, telemetry, usage, args.harness_version
    )
    if args.completed_turns_only:
        run["notes"] += (
            " Imported completed turns only; a trailing unfinished turn is excluded."
        )
        transcript["metadata"]["completedTurnsOnly"] = True
    content = args.artifact.read_bytes()
    recorded = recorded_artifact(
        normalize_updates(updates), summary["info"]["cwd"], args.session
    )
    if content != recorded:
        raise ValueError("Artifact differs from the final recorded write")
    run["resultStats"] = {"bytes": len(content), "lines": len(content.splitlines())}
    transcript["metadata"]["artifactSha256"] = hashlib.sha256(content).hexdigest()
    folder = args.output_root / run["id"]
    roots = {summary["info"]["cwd"]: "<WORKSPACE>"}
    output = folder / "index.html"
    if output.exists() and output.read_bytes() != content:
        raise ValueError("Refusing to overwrite an existing benchmark artifact")
    documents = {"run.json": run, "transcript.json": transcript}
    for name, document in documents.items():
        data = serialize(censor_value(document, session_roots=roots))
        if args.check_sources:
            if (folder / name).read_bytes() != data:
                raise ValueError(f"Regeneration differs: {folder / name}")
        else:
            folder.mkdir(parents=True, exist_ok=True)
            (folder / name).write_bytes(data)
    if not args.check_sources and not output.exists():
        output.write_bytes(content)
    if args.check_sources and output.read_bytes() != recorded:
        raise ValueError("Published artifact differs from recorded write")
    if not args.check_sources:
        censor_main(["--write", str(folder)])
    if censor_main([str(folder)]):
        return 1
    print(f"Verified {run['id']} against recorded writes and usage")
    return 0


def completed_prefix(updates, telemetry):
    ends = [i for i, event in enumerate(telemetry) if event["type"] == "turn_ended"]
    if not ends:
        raise ValueError("No completed turn to import")
    end = ends[-1]
    tail = telemetry[end + 1 :]
    if not tail or tail[0]["type"] != "turn_started":
        raise ValueError("No trailing unfinished turn to exclude")
    if any(
        event["type"] not in {"turn_started", "loop_started", "phase_changed"}
        or event.get("phase", "waiting_for_model") != "waiting_for_model"
        for event in tail
    ):
        raise ValueError("Trailing turn has model/tool activity; cannot exclude it")
    cutoff = datetime.fromisoformat(tail[0]["ts"]).timestamp() * 1000
    prefix = [
        row
        for row in updates
        if row["params"]
        .get("_meta", {})
        .get("agentTimestampMs", row["timestamp"] * 1000)
        < cutoff
    ]
    return prefix, telemetry[: end + 1]


def build_run(summary, updates, telemetry, usage, version):
    """Use persisted accounting; inputTokens includes the cached subset."""
    session_id = summary["info"]["id"]
    if usage["sessionId"] != session_id:
        raise ValueError("Usage belongs to another session")
    starts = [e for e in telemetry if e["type"] == "turn_started"]
    ends = [e for e in telemetry if e["type"] == "turn_ended"]
    if not starts or len(starts) != len(ends):
        raise ValueError("Session has an unfinished turn")
    if ends[-1]["outcome"] not in {"success", "completed"}:
        raise ValueError("Last turn did not complete successfully")
    aggregate = usage["session"]
    if aggregate.get("turnCount") != len(ends):
        raise ValueError("Usage ledger does not cover all recorded turns")
    if aggregate.get("usageIsIncomplete"):
        raise ValueError("Usage ledger is incomplete")
    model = summary["current_model_id"]
    effort = summary["reasoning_effort"]
    if any(e["model_id"] != model for e in starts):
        raise ValueError("Session contains multiple configured models")
    run_id = f"xai/{model}/{effort}"
    events = normalize_updates(updates)
    started = starts[0]["ts"]
    completed = ends[-1]["ts"]
    elapsed = datetime.fromisoformat(completed) - datetime.fromisoformat(started)
    tokens = {
        "input": aggregate["inputTokens"],
        "output": aggregate["outputTokens"],
        "total": aggregate["totalTokens"],
    }
    for source, target in [
        ("cachedReadTokens", "cachedInput"),
        ("cacheCreationTokens", "cacheCreationInput"),
        ("reasoningTokens", "reasoning"),
    ]:
        if source in aggregate:
            tokens[target] = aggregate[source]
    cost = None
    if aggregate.get("costUsdTicks") is not None and not aggregate.get("costIsPartial"):
        cost = {
            "currency": "USD",
            "total": aggregate["costUsdTicks"] / 1e10,
            "estimated": False,
        }
    run = {
        "$schema": "../../../schemas/run.schema.json",
        "schemaVersion": 1,
        "id": run_id,
        "vendor": {"slug": "xai", "displayName": "xAI"},
        "model": {
            "slug": model,
            "displayName": model.replace("grok-", "Grok "),
            "providerModelId": aggregate["primaryModelId"],
        },
        "variation": {
            "slug": effort,
            "displayName": "XHigh" if effort == "xhigh" else effort.title(),
            "reasoningEffort": effort,
        },
        "harness": {"name": "Grok Build", "version": version},
        "artifacts": {"result": "index.html", "transcript": "transcript.json"},
        "timing": {
            "startedAt": started,
            "completedAt": completed,
            "wallDurationMs": round(elapsed.total_seconds() * 1000),
        },
        "usage": {
            "tokens": tokens,
            "cost": cost,
            "providerReported": True,
            "requests": aggregate["modelCalls"],
            "turns": len(ends),
            "toolCalls": sum(e["type"] == "tool_call" for e in events),
        },
        "notes": "Usage and cost are from Grok Build's persisted session ledger. "
        "Input tokens include cached input; reasoning tokens are a subset "
        "of output tokens. Timing spans the first turn start through the "
        "last turn end. Structured hidden reasoning is excluded.",
    }
    if aggregate.get("apiDurationMs") is not None:
        run["timing"]["apiDurationMs"] = aggregate["apiDurationMs"]
    transcript = {
        "$schema": "../../../schemas/transcript.schema.json",
        "schemaVersion": 1,
        "runId": run_id,
        "sessionId": session_id,
        "sourceFormat": "Grok Build ACP updates.jsonl",
        "events": events,
        "metadata": {
            "sourceFiles": ["updates.jsonl", "events.jsonl", "summary.json"],
            "usageSource": "grok usage SESSION_ID",
            "hiddenReasoningOmitted": True,
        },
    }
    return run, transcript


if __name__ == "__main__":
    raise SystemExit(main())
