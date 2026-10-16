"""Regression coverage for public native Harbor agy imports."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agy_list_prices import argon_list_cost
from import_agy import (
    build_trial,
    normalize,
    recovered_completion,
    replay_artifact,
    stream_usage,
    transcript_rows,
)


def row(index, kind, *, source="MODEL", status="DONE", **fields):
    return {
        "step_index": index,
        "type": kind,
        "source": source,
        "status": status,
        "created_at": "2026-10-15T23:00:00Z",
        **fields,
    }


class AgyImportTests(unittest.TestCase):
    def test_published_rates_discount_cache_and_count_thinking_once(self):
        usage = {
            "input_tokens": 1_000_000,
            "cache_read_tokens": 1_000_000,
            "output_tokens": 1_000_000,
            "thinking_tokens": 500_000,
        }
        cost, source = argon_list_cost(usage, "2026-10-15T23:00:00Z")
        self.assertEqual(cost["total"], 12.1)
        self.assertTrue(cost["estimated"])
        self.assertTrue(source["outputIncludesThinking"])
        later, _ = argon_list_cost(usage, "2026-12-01T00:00:00Z")
        self.assertEqual(later["total"], 24.2)
        with self.assertRaisesRegex(ValueError, "predates"):
            argon_list_cost(usage, "2026-10-01T00:00:00Z")

    def test_error_status_requires_completed_post_timeout_write_and_final_response(
        self,
    ):
        final = {"status": "ERROR", "response": "Created the file.\n"}
        rows = [
            row(1, "ERROR_MESSAGE", source="SYSTEM", error="API error: idle timeout"),
            row(
                2,
                "PLANNER_RESPONSE",
                tool_calls=[
                    {
                        "name": "write_to_file",
                        "args": {
                            "TargetFile": "/app/index.html",
                            "CodeContent": "ramen",
                        },
                    }
                ],
            ),
            row(3, "GENERIC", content="Written"),
            row(4, "PLANNER_RESPONSE", content="Created the file."),
        ]
        self.assertTrue(recovered_completion(rows, final))
        self.assertFalse(
            recovered_completion(
                rows, {**final, "response": "Failed to create the file."}
            )
        )
        self.assertFalse(recovered_completion([rows[0], rows[-1]], final))
        self.assertFalse(
            recovered_completion([*rows[:-1], {**rows[-1], "status": "ACTIVE"}], final)
        )
        self.assertFalse(
            recovered_completion(
                [
                    *rows,
                    row(
                        5,
                        "ERROR_MESSAGE",
                        source="SYSTEM",
                        error="API error: still failed",
                    ),
                ],
                final,
            )
        )
        self.assertFalse(
            recovered_completion(
                [rows[0], rows[1], {**rows[2], "status": "ERROR"}, rows[3]], final
            )
        )

    def test_direct_agy_output_is_rejected_even_with_matching_model(self):
        with tempfile.TemporaryDirectory() as directory:
            trial = Path(directory)
            (trial / "config.json").write_text(
                json.dumps(
                    {
                        "agent": {
                            "name": "direct-agy",
                            "model_name": "google/gemini-4-argon-low",
                            "kwargs": {"reasoning_effort": "low"},
                        }
                    }
                )
            )
            (trial / "result.json").write_text(
                json.dumps(
                    {
                        "exception_info": None,
                        "agent_info": {
                            "name": "antigravity-cli",
                            "model_info": {
                                "name": "gemini-4-argon-low",
                                "provider": "google",
                            },
                        },
                    }
                )
            )
            with self.assertRaisesRegex(ValueError, "native Harbor"):
                build_trial(trial)

    def test_usage_deduplicates_terminal_updates_and_counts_reasoning_once(self):
        usage = {
            "input_tokens": 100,
            "cache_read_tokens": 300,
            "output_tokens": 50,
            "thinking_tokens": 20,
            "total_tokens": 150,
        }
        step = {
            "event": "step_update",
            "step_update": {"step_index": 1, "state": "DONE", "usage": usage},
        }
        final = {"usage": usage, "status": "SUCCESS", "num_turns": 1}
        active = {
            "event": "step_update",
            "step_update": {
                "step_index": 1,
                "state": "ACTIVE",
                "usage": {**usage, "input_tokens": 1},
            },
        }
        stream = [active, step, step, {"event": "result", "result": final}]
        totals, requests, _ = stream_usage(stream)
        self.assertEqual(totals, usage)
        self.assertEqual(requests, 1)
        self.assertEqual(
            totals["input_tokens"]
            + totals["cache_read_tokens"]
            + totals["output_tokens"],
            450,
        )
        with self.assertRaisesRegex(ValueError, "differs"):
            stream_usage(
                [
                    step,
                    {
                        "event": "result",
                        "result": {
                            **final,
                            "usage": {
                                **usage,
                                "input_tokens": 101,
                                "total_tokens": 151,
                            },
                        },
                    },
                ]
            )
        with self.assertRaisesRegex(ValueError, "one native"):
            stream_usage([step])
        with self.assertRaisesRegex(ValueError, "single turn"):
            stream_usage(
                [step, {"event": "result", "result": {**final, "status": "ERROR"}}]
            )

    def test_replay_excludes_failed_edits_and_requires_exact_artifact(self):
        write = {
            "name": "write_to_file",
            "args": {
                "TargetFile": "/app/index.html",
                "CodeContent": "<svg>ramen</svg>\n",
            },
        }
        edit = {
            "name": "replace_file_content",
            "args": {
                "TargetFile": "/app/index.html",
                "TargetContent": "ramen",
                "ReplacementContent": "changed",
            },
        }
        rows = [
            row(1, "PLANNER_RESPONSE", tool_calls=[write]),
            row(2, "GENERIC", content="written"),
            row(3, "PLANNER_RESPONSE", tool_calls=[edit]),
            row(4, "GENERIC", status="ERROR", content="failed"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "index.html"
            artifact.write_text("<svg>ramen</svg>\n")
            self.assertIn("1 operations", replay_artifact(rows, artifact, "/app"))
            artifact.write_text("<svg>changed</svg>\n")
            with self.assertRaisesRegex(ValueError, "differs"):
                replay_artifact(rows, artifact, "/app")

    def test_normalization_keeps_visible_content_and_omits_private_activity(self):
        rows = [
            row(
                0,
                "USER_INPUT",
                source="USER_EXPLICIT",
                content="<USER_REQUEST>\nCreate ramen\n</USER_REQUEST>",
            ),
            row(
                1,
                "PLANNER_RESPONSE",
                content="Visible explanation",
                thinking="hidden reasoning",
                tool_calls=[
                    {"name": "manage_inbox", "args": {"message": "private agent mail"}},
                    {"name": "view_file", "args": {"AbsolutePath": "/app/index.html"}},
                ],
            ),
            row(2, "GENERIC", content="private mail result"),
            row(3, "GENERIC", content="Readable source"),
            row(
                4,
                "ERROR_MESSAGE",
                source="SYSTEM",
                error="API error: stream idle timeout",
            ),
            row(5, "CHECKPOINT", source="SYSTEM", content="system prompt"),
        ]
        events, omitted = normalize(rows)
        encoded = json.dumps(events)
        self.assertEqual(omitted, 1)
        for text in ("Visible explanation", "Readable source", "Create ramen"):
            self.assertIn(text, encoded)
        for text in (
            "hidden reasoning",
            "private agent mail",
            "private mail result",
            "system prompt",
        ):
            self.assertNotIn(text, encoded)
        self.assertEqual(
            [e["type"] for e in events],
            ["message", "message", "tool_call", "tool_result", "error"],
        )
        self.assertEqual(events[-1]["message"], "API error: stream idle timeout")

    def test_transcript_snapshots_do_not_duplicate_model_calls(self):
        active = row(1, "PLANNER_RESPONSE", status="ACTIVE", content="partial")
        done = row(1, "PLANNER_RESPONSE", content="complete", tool_calls=[])
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "transcript.jsonl"
            p.write_text("\n".join(json.dumps(r) for r in (active, done, done)))
            self.assertEqual(transcript_rows(p), [done])


if __name__ == "__main__":
    unittest.main()
