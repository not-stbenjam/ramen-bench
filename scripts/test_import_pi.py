"""Integrity, privacy and accounting regressions for native Pi imports."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from import_pi import (
    generation_outcome,
    normalize_session,
    provider_cost,
    session_usage,
    verify_artifact,
)


class PiImportTests(unittest.TestCase):
    def test_final_api_failure_requires_successful_recorded_artifact_write(self):
        rows = [
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "stopReason": "toolUse",
                    "content": [
                        {
                            "type": "toolCall",
                            "id": "write-fixture",
                            "name": "write",
                            "arguments": {"path": "/app/index.html"},
                        }
                    ],
                },
            },
            {
                "type": "message",
                "message": {
                    "role": "toolResult",
                    "toolCallId": "write-fixture",
                    "isError": False,
                },
            },
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "stopReason": "error",
                    "errorMessage": "Upstream idle timeout exceeded",
                    "responseId": "gen-fixture",
                },
            },
        ]
        outcome, errors = generation_outcome(rows)
        self.assertEqual(outcome, "artifact-written-final-api-error")
        self.assertEqual(errors[0]["errorMessage"], "Upstream idle timeout exceeded")
        rows[1]["message"]["isError"] = True
        with self.assertRaisesRegex(ValueError, "successful artifact write"):
            generation_outcome(rows)

    def test_provider_charges_require_matching_session_model_and_complete_records(self):
        rows = [
            {
                "type": "message",
                "message": {"role": "assistant", "responseId": "gen-fixture"},
            }
        ]
        record = {
            "id": "gen-fixture",
            "session_id": "session-fixture",
            "model": "mistralai/mistral-large-4-0-20261006",
            "total_cost": 0.018,
            "created_at": "2026-10-06T12:00:00Z",
        }
        with tempfile.TemporaryDirectory() as directory:
            trial = Path(directory)
            self.assertIsNone(provider_cost(trial, rows, "session-fixture")[0])
            path = trial / "provider-generation-gen-fixture.json"
            path.write_text(json.dumps(record))
            cost, public_records = provider_cost(trial, rows, "session-fixture")
            self.assertEqual(
                cost, {"currency": "USD", "total": 0.018, "estimated": False}
            )
            self.assertNotIn("session_id", public_records[0])
            record["session_id"] = "another-session"
            path.write_text(json.dumps(record))
            with self.assertRaisesRegex(ValueError, "does not match"):
                provider_cost(trial, rows, "session-fixture")

    def test_replay_preserves_exact_write_and_edit_and_rejects_tampering(self):
        rows = [
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "name": "write",
                            "arguments": {
                                "path": "/app/index.html",
                                "content": "<svg>before</svg>\n",
                            },
                        },
                        {
                            "type": "toolCall",
                            "name": "edit",
                            "arguments": {
                                "path": "/app/index.html",
                                "oldText": "before",
                                "newText": "after",
                            },
                        },
                    ],
                },
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "index.html"
            artifact.write_text("<svg>after</svg>\n")
            self.assertIn("2 operations", verify_artifact(rows, artifact, "fixture"))
            artifact.write_text("<svg>tampered</svg>\n")
            with self.assertRaisesRegex(ValueError, "differs"):
                verify_artifact(rows, artifact, "fixture")

    def test_normalization_keeps_public_text_and_tools_without_hidden_reasoning(self):
        rows = [
            {
                "type": "message",
                "message": {"role": "system", "content": "private system"},
            },
            {
                "type": "message",
                "timestamp": "2026-10-06T12:00:00Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "Create a bowl"}],
                },
            },
            {
                "type": "message",
                "timestamp": "2026-10-06T12:00:01Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "private hidden reasoning"},
                        {"type": "text", "text": "I chose a round bowl."},
                        {
                            "type": "toolCall",
                            "id": "write",
                            "name": "write",
                            "arguments": {
                                "path": "/app/index.html",
                                "content": "<svg/>",
                            },
                        },
                    ],
                },
            },
            {
                "type": "message",
                "timestamp": "2026-10-06T12:00:02Z",
                "message": {
                    "role": "toolResult",
                    "toolCallId": "write",
                    "content": [{"type": "text", "text": "wrote file"}],
                },
            },
        ]
        events, omitted = normalize_session(rows)
        serialized = json.dumps(events)
        self.assertNotIn("private", serialized)
        self.assertIn("I chose a round bowl.", serialized)
        self.assertEqual(omitted, 0)
        self.assertEqual(events[-1]["type"], "tool_result")
        self.assertEqual(events[-1]["timestamp"], rows[-1]["timestamp"])

    def test_native_usage_counts_cache_once_and_rejects_disagreement(self):
        usage = {
            "input": 100,
            "cacheRead": 80,
            "cacheWrite": 0,
            "output": 20,
            "totalTokens": 200,
            "cost": {"total": 0.123},
        }
        message = {"role": "assistant", "usage": usage}
        rows = [{"type": "message", "message": message}]
        result = {
            "agent_result": {
                "n_input_tokens": 180,
                "n_cache_tokens": 80,
                "n_output_tokens": 20,
                "cost_usd": 0.123,
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            stdout = Path(directory) / "pi.txt"
            stdout.write_text(
                json.dumps({"type": "message_end", "message": message}) + "\n"
            )
            actual = session_usage(rows, result, stdout)
            self.assertEqual(actual["tokens"]["input"], 180)
            self.assertEqual(actual["tokens"]["total"], 200)
            self.assertEqual(actual["cost"]["total"], 0.123)
            self.assertTrue(actual["cost"]["estimated"])
            result["agent_result"]["n_input_tokens"] = 260
            with self.assertRaisesRegex(ValueError, "disagrees"):
                session_usage(rows, result, stdout)
            result["agent_result"]["n_input_tokens"] = 180
            stdout.write_text("")
            with self.assertRaisesRegex(ValueError, "stdout and session"):
                session_usage(rows, result, stdout)


if __name__ == "__main__":
    unittest.main()
