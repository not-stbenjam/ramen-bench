"""Synthetic source fixtures for Grok's public importer (no private sessions)."""

import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from import_grok import (
    build_run,
    completed_prefix,
    normalize_updates,
    recorded_artifact,
)


def update(kind, **fields):
    return {"timestamp": 1000, "params": {"update": {"sessionUpdate": kind, **fields}}}


def call(tool, args, call_id="c1", status="completed"):
    return [
        update("tool_call", toolCallId=call_id, title=tool, rawInput=args),
        update(
            "tool_call_update",
            toolCallId=call_id,
            status=status,
            rawOutput={"ok": status == "completed"},
        ),
    ]


class ImportGrokTests(unittest.TestCase):
    def test_excludes_hidden_reasoning_and_retains_public_text(self):
        rows = [
            update("agent_thought_chunk", content={"type": "text", "text": "SECRET"}),
            update("agent_message_chunk", content={"type": "text", "text": "Visible"}),
        ]
        events = normalize_updates(rows)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["content"], "Visible")
        self.assertNotIn("SECRET", str(events))

    def test_replays_only_successful_writes_and_edits(self):
        rows = call("write", {"file_path": "index.html", "content": "one two"})
        rows += call(
            "write", {"file_path": "index.html", "content": "wrong"}, "c2", "failed"
        )
        rows += call(
            "search_replace",
            {
                "file_path": "/work/index.html",
                "old_string": "two",
                "new_string": "three",
            },
            "c3",
        )
        self.assertEqual(
            recorded_artifact(normalize_updates(rows), "/work"), b"one three"
        )

    def test_failed_tool_preserves_content_when_raw_output_is_null(self):
        event = update(
            "tool_call_update",
            toolCallId="c",
            status="failed",
            rawOutput=None,
            content=[{"type": "text", "text": "Denied"}],
        )
        self.assertEqual(
            normalize_updates([event])[0]["output"],
            event["params"]["update"]["content"],
        )

    def test_search_replace_empty_old_string_creates_file(self):
        rows = call(
            "search_replace",
            {
                "file_path": "/work/index.html",
                "old_string": "",
                "new_string": "<!DOCTYPE html>\n<p>ramen</p>\n",
            },
        )
        self.assertEqual(
            recorded_artifact(normalize_updates(rows), "/work"),
            b"<!DOCTYPE html>\n<p>ramen</p>\n",
        )
        rows += call(
            "search_replace",
            {
                "file_path": "/work/index.html",
                "old_string": "",
                "new_string": "unexpected overwrite",
            },
            "c2",
        )
        with self.assertRaisesRegex(ValueError, "creation follows an existing write"):
            recorded_artifact(normalize_updates(rows), "/work")

    def test_failed_write_cannot_verify_artifact(self):
        rows = call(
            "write", {"file_path": "index.html", "content": "bad"}, status="failed"
        )
        with self.assertRaisesRegex(ValueError, "No successful recorded"):
            recorded_artifact(normalize_updates(rows), "/work")

    def test_ambiguous_replacement_rejected(self):
        rows = call("write", {"file_path": "index.html", "content": "x x"})
        rows += call(
            "search_replace",
            {"file_path": "index.html", "old_string": "x", "new_string": "y"},
            "c2",
        )
        with self.assertRaisesRegex(ValueError, "multiple matches"):
            recorded_artifact(normalize_updates(rows), "/work")

    def test_static_image_embedding_reads_only_source_images(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary)
            (session / "images").mkdir()
            image = session / "images" / "1.jpg"
            image.write_bytes(b"image fixture")
            rows = call(
                "write", {"file_path": "index.html", "content": "<img src='IMAGE'>"}
            )
            source = f"""import base64
from pathlib import Path
img = Path({str(image)!r}).read_bytes()
b64 = base64.b64encode(img).decode('ascii')
p = Path('/work/index.html')
html = p.read_text()
html = html.replace('IMAGE', 'data:image/jpeg;base64,' + b64)
p.write_text(html)
print('ignored diagnostic')
"""
            rows += call(
                "run_terminal_command",
                {"command": "python3 - <<'PY'\n" + source + "PY"},
                "c2",
            )
            self.assertEqual(
                recorded_artifact(normalize_updates(rows), "/work", session),
                b"<img src='data:image/jpeg;base64,aW1hZ2UgZml4dHVyZQ=='>",
            )
            # Supplying an output artifact can never supply missing provenance.
            with self.assertRaisesRegex(ValueError, "Unrecorded file dependency"):
                recorded_artifact(normalize_updates(rows), "/work")

    def test_completed_prefix_requires_no_model_activity(self):
        telemetry = [
            {"type": "turn_started", "ts": "2026-01-01T00:00:00Z"},
            {
                "type": "turn_ended",
                "ts": "2026-01-01T00:00:01Z",
                "outcome": "completed",
            },
            {"type": "turn_started", "ts": "2026-01-01T00:00:02Z"},
            {"type": "loop_started"},
            {"type": "phase_changed", "phase": "waiting_for_model"},
        ]
        rows = [update("agent_message_chunk", content={"type": "text", "text": "done"})]
        self.assertEqual(completed_prefix(rows, telemetry), (rows, telemetry[:2]))
        telemetry.append({"type": "first_token"})
        with self.assertRaisesRegex(ValueError, "model/tool activity"):
            completed_prefix(rows, telemetry)

    def test_usage_cost_and_completion_are_recorded(self):
        summary = {
            "info": {"id": "s"},
            "current_model_id": "grok-4.7",
            "reasoning_effort": "low",
        }
        telemetry = [
            {
                "type": "turn_started",
                "ts": "2026-01-01T00:00:00.000Z",
                "model_id": "grok-4.7",
            },
            {
                "type": "turn_ended",
                "ts": "2026-01-01T00:00:01.500Z",
                "outcome": "success",
            },
        ]
        usage = {
            "sessionId": "s",
            "session": {
                "inputTokens": 100,
                "outputTokens": 20,
                "totalTokens": 120,
                "cachedReadTokens": 40,
                "reasoningTokens": 10,
                "modelCalls": 1,
                "turnCount": 1,
                "primaryModelId": "grok-4.7-build",
                "costUsdTicks": 123000000,
            },
        }
        run, _ = build_run(summary, [], telemetry, usage, "test")
        self.assertEqual(
            run["usage"]["tokens"],
            {
                "input": 100,
                "output": 20,
                "total": 120,
                "cachedInput": 40,
                "reasoning": 10,
            },
        )
        self.assertEqual(run["usage"]["cost"]["total"], 0.0123)
        self.assertEqual(run["timing"]["wallDurationMs"], 1500)
        unknown = copy.deepcopy(usage)
        del unknown["session"]["costUsdTicks"]
        self.assertIsNone(
            build_run(summary, [], telemetry, unknown, "test")[0]["usage"]["cost"]
        )
        partial = copy.deepcopy(usage)
        partial["session"]["costIsPartial"] = True
        self.assertIsNone(
            build_run(summary, [], telemetry, partial, "test")[0]["usage"]["cost"]
        )
        partial["session"]["usageIsIncomplete"] = True
        with self.assertRaisesRegex(ValueError, "ledger is incomplete"):
            build_run(summary, [], telemetry, partial, "test")
        telemetry[-1]["outcome"] = "cancelled"
        with self.assertRaisesRegex(ValueError, "did not complete"):
            build_run(summary, [], telemetry, usage, "test")


if __name__ == "__main__":
    unittest.main()
