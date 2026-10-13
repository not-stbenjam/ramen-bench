"""Privacy, exact-artifact and provider-accounting regressions for Muse imports."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from import_harbor_muse import normalize_session, register, sum_usage, verify_artifact
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
    def test_replays_recorded_pathlib_edit_with_spaced_python_heredoc(self):
        initial = '<svg xmlns="http://www.w3.org/2000/svg"></svg>\n'
        command = (
            "python3 - << 'PY'\n"
            "import pathlib\n"
            'p=pathlib.Path("/app/index.html"); s=p.read_text()\n'
            "s=s.replace(' xmlns=\"http://www.w3.org/2000/svg\"','')\n"
            "p.write_text(s)\n"
            "print('done')\n"
            "PY"
        )
        export = {
            "events": [
                record(
                    "assistant_tool_calls_committed",
                    tool_calls=[
                        call("write_file", path="/app/index.html", content=initial),
                        call("bash", command=command),
                    ],
                )
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "index.html"
            artifact.write_text('<svg></svg>\n')
            self.assertIn("2 operations", verify_artifact(export, artifact))

    def test_cancelled_auxiliary_without_usage_is_explicitly_allowed_only(self):
        export = {
            "sessions": [{"session_id": "fixture-session"}],
            "events": [
                record("timed_out"),
                record("terminal", terminal="cancelled"),
            ],
        }
        with self.assertRaisesRegex(ValueError, "no recorded provider usage"):
            sum_usage(export)
        totals, requests, duration = sum_usage(export, allow_cancelled_empty=True)
        self.assertTrue(all(value == 0 for value in totals.values()))
        self.assertEqual((requests, duration), (0, 0))
        for events in (
            [record("terminal", terminal="completed")],
            [record("terminal", terminal="cancelled")],
            [record("timed_out")],
        ):
            with (
                self.subTest(events=events),
                self.assertRaisesRegex(ValueError, "no recorded provider usage"),
            ):
                sum_usage({**export, "events": events}, allow_cancelled_empty=True)

    def test_full_has_no_regular_wonder_price_even_with_priced_auxiliary_requests(self):
        usage = {
            "input_tokens": 100,
            "cached_tokens": 80,
            "cache_read_tokens": 80,
            "cache_write_tokens": 0,
            "output_tokens": 20,
            "reasoning_tokens": 10,
        }
        full = {
            "events": [
                record("model_completed", model="muse-wonder-full-1.0", usage=usage)
            ]
        }
        regular = {
            "events": [record("model_completed", model="muse-wonder-1.0", usage=usage)]
        }
        self.assertIsNone(wonder_list_cost([full, regular]))

    def test_register_orders_full_and_preserves_prior_runs_and_human_ratings(self):
        regular = "meta/muse-wonder-1.0/max"
        spark = "meta/muse-spark-1.3/high"
        existing = [
            regular + "/run.json",
            spark + "/run.json",
            "xai/fixture/high/run.json",
        ]
        rating = {"runId": regular, "humanScore": 0.9}

        def run(model, effort, date):
            return {
                "id": f"meta/{model}/{effort}",
                "model": {"slug": model},
                "timing": {"completedAt": date},
            }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "harbor").mkdir()
            (root / "registry.json").write_text(
                json.dumps(
                    {
                        "runs": existing,
                        "modelAddedAt": {
                            "meta/muse-wonder-1.0": "2026-10-12T00:00:00Z",
                            "meta/muse-spark-1.3": "2026-09-01T00:00:00Z",
                        },
                    }
                )
            )
            (root / "harbor/human-scores.json").write_text(
                json.dumps({"ratings": [rating]})
            )
            with patch("import_harbor_muse.ROOT", root):
                register(
                    [
                        run("muse-wonder-full-1.0", "low", "2026-10-13T00:00:00Z"),
                        run("muse-wonder-full-1.0", "max", "2026-10-13T00:00:00Z"),
                    ]
                )
                register([run("muse-wonder-1.0", "max", "2026-10-12T00:00:00Z")])
            self.assertEqual(
                json.loads((root / "registry.json").read_text())["runs"],
                [
                    "meta/muse-wonder-full-1.0/max/run.json",
                    "meta/muse-wonder-full-1.0/low/run.json",
                    *existing,
                ],
            )
            self.assertEqual(
                json.loads((root / "harbor/human-scores.json").read_text())["ratings"][
                    0
                ],
                rating,
            )

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
