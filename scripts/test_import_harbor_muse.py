"""Privacy, exact-artifact and provider-accounting regressions for Muse imports."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from import_harbor_muse import normalize_session, sum_usage, verify_artifact
from muse_list_prices import wonder_list_cost


def record(kind, **values):
    return {
        "envelope": {
            "id": "event-fixture",
            "stream": {"id": "session-fixture"},
            "recorded_at": 1791820800000000,
            "payload_type": "runtime.session",
            "payload": {"event": {"kind": kind, **values}},
        }
    }


def call(name, **args):
    return {"name": name, "args": json.dumps(args)}


class MuseImportTests(unittest.TestCase):
    def test_list_price_counts_cache_discount_reasoning_once_and_auxiliary_requests(
        self,
    ):
        usage = {
            "input_tokens": 1_200_000,
            "cached_tokens": 200_000,
            "cache_read_tokens": 200_000,
            "cache_write_tokens": 0,
            "output_tokens": 100_000,
            "reasoning_tokens": 60_000,
        }
        main = {
            "events": [record("model_completed", model="muse-wonder-1.0", usage=usage)]
        }
        cost, evidence = wonder_list_cost([main])
        # One million uncached tokens ($4), 200k cache hits ($.04),
        # and 100k inclusive output tokens ($2), with no extra reasoning charge.
        self.assertEqual(cost["total"], 6.04)
        self.assertEqual(cost["output"], 2)
        self.assertTrue(cost["estimated"])
        self.assertEqual(evidence["asOf"], "2026-10-12")
        child_usage = {
            **usage,
            "input_tokens": 50_000,
            "cached_tokens": 20_000,
            "cache_read_tokens": 20_000,
            "output_tokens": 1_000,
            "reasoning_tokens": 500,
        }
        child = {
            "events": [
                record("model_completed", model="muse-wonder-1.0", usage=child_usage)
            ]
        }
        cost, evidence = wonder_list_cost([main, child])
        self.assertEqual(cost["total"], 6.184)
        self.assertEqual(evidence["pricedRequests"], 2)

    def test_pricing_stays_unknown_for_missing_categories_models_or_tool_charges(self):
        usage = {
            "input_tokens": 100,
            "cached_tokens": 80,
            "cache_read_tokens": 80,
            "cache_write_tokens": 0,
            "output_tokens": 20,
            "reasoning_tokens": 10,
        }
        request = record("model_completed", model="muse-wonder-1.0", usage=usage)
        export = {"events": [request]}
        request["envelope"]["payload"]["event"]["model"] = "unpriced-model"
        self.assertIsNone(wonder_list_cost([export]))
        request["envelope"]["payload"]["event"]["model"] = "muse-wonder-1.0"
        del usage["cache_read_tokens"]
        self.assertIsNone(wonder_list_cost([export]))
        usage["cache_read_tokens"] = 80
        export["events"].append(
            record(
                "assistant_tool_calls_committed",
                tool_calls=[call("web_search", query="fixture")],
            )
        )
        self.assertIsNone(wonder_list_cost([export]))

    def test_exact_write_and_edit_replay_rejects_tampered_artifact(self):
        export = {
            "events": [
                record(
                    "assistant_tool_calls_committed",
                    tool_calls=[
                        call(
                            "write_file",
                            path="/app/index.html",
                            content="<svg>before</svg>\n",
                        ),
                        call(
                            "edit_file",
                            path="/app/index.html",
                            find="before",
                            replace="after",
                        ),
                    ],
                )
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "index.html"
            artifact.write_text("<svg>after</svg>\n")
            self.assertIn("2 operations", verify_artifact(export, artifact))
            artifact.write_text("<svg>tampered</svg>\n")
            with self.assertRaisesRegex(ValueError, "differs"):
                verify_artifact(export, artifact)
            export["events"][0]["envelope"]["payload"]["event"]["tool_calls"][1] = call(
                "edit_file", path="/app/index.html", find="missing", replace="after"
            )
            with self.assertRaisesRegex(ValueError, "exactly one"):
                verify_artifact(export, artifact)

    def test_public_messages_and_tools_survive_without_hidden_or_private_content(self):
        user = {
            "envelope": {
                "recorded_at": 1791820800000000000 // 1000,
                "payload_type": "runtime.user_intent.accepted",
                "payload": {
                    "refill_blocks": [{"kind": "text", "text": "Create a bowl"}]
                },
            }
        }
        export = {
            "events": [
                user,
                record(
                    "reasoning_committed",
                    text="hidden reasoning",
                    encrypted_content="private ciphertext",
                ),
                record(
                    "model_request_configured",
                    base_instructions="private system prompt",
                ),
                record("assistant_message_committed", text="I chose a round bowl."),
                record(
                    "assistant_tool_calls_committed",
                    tool_calls=[
                        call("session_message", text="private agent mail"),
                        call("write_file", path="/app/index.html", content="<svg/>"),
                    ],
                ),
                record(
                    "tool_result_batch_committed",
                    results=[
                        {"tool_call_index": 0, "text": "private reply"},
                        {"tool_call_index": 1, "text": "wrote file"},
                    ],
                ),
            ],
            "diagnostics": {"gaps": 2, "omitted_live_only": 2},
        }
        events, omitted = normalize_session(export)
        text = json.dumps(events)
        self.assertNotIn("private", text)
        self.assertNotIn("hidden reasoning", text)
        self.assertIn("I chose a round bowl.", text)
        self.assertIn("<svg/>", text)
        self.assertEqual(omitted, 1)
        self.assertEqual(events[-2]["callId"], events[-3]["id"])
        self.assertIn("2 history gaps", events[-1]["content"])
        self.assertIn("not been reconstructed", events[-1]["content"])

    def test_incomplete_tool_result_batch_is_rejected(self):
        export = {
            "events": [
                record(
                    "assistant_tool_calls_committed",
                    tool_calls=[call("bash", command="pwd")],
                ),
                record("tool_result_batch_committed", results=[]),
            ]
        }
        with self.assertRaisesRegex(ValueError, "incomplete"):
            normalize_session(export)

    def test_native_token_counts_require_categories_and_no_duplicate_or_foreign_requests(
        self,
    ):
        usage = {
            "input_tokens": 100,
            "cached_tokens": 80,
            "cache_read_tokens": 80,
            "cache_write_tokens": 0,
            "output_tokens": 20,
            "reasoning_tokens": 10,
        }
        request = record("model_completed", usage=usage, duration_ms=1200)
        export = {"sessions": [{"session_id": "session-fixture"}], "events": [request]}
        totals, count, duration = sum_usage(export)
        self.assertEqual(
            (
                totals["input_tokens"] - totals["cache_read_tokens"],
                totals["output_tokens"],
                totals["reasoning_tokens"],
            ),
            (20, 20, 10),
        )
        self.assertEqual((count, duration), (1, 1200))
        export["events"].append(request)
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            sum_usage(export)
        export["events"].pop()
        request["envelope"]["stream"]["id"] = "foreign-session"
        with self.assertRaisesRegex(ValueError, "foreign"):
            sum_usage(export)
        request["envelope"]["stream"]["id"] = "session-fixture"
        del usage["cache_read_tokens"]
        with self.assertRaisesRegex(ValueError, "token category"):
            sum_usage(export)


if __name__ == "__main__":
    unittest.main()
