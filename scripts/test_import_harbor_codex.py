"""Source integrity and native accounting checks for the Harbor Codex importer."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backfill_session_metadata import normalize_codex_events, verify_codex_result
from import_harbor_codex import (
    build_recovered_trial,
    main_rollout,
    native_artifact,
    native_usage,
    recovery_grade,
    register,
    trial_usage,
)


class HarborCodexImportTests(unittest.TestCase):
    def test_subagent_trajectory_uses_stdout_main_session_and_records_full_usage(self):
        result, trajectory, usage_rows = self.usage_fixture()
        trajectory["session_id"] = "review"
        main_rows = [
            {"type": "session_meta", "payload": {"id": "main", "source": "exec"}},
            *usage_rows,
        ]
        review_rows = [
            {
                "type": "session_meta",
                "payload": {"id": "review", "forked_from_id": "main"},
            },
            main_rows[0],  # Actual forks include inherited parent metadata.
            *usage_rows,
        ]
        with tempfile.TemporaryDirectory() as directory:
            trial = Path(directory)
            sessions = trial / "agent/sessions"
            sessions.mkdir(parents=True)
            for name, rows in (("main", main_rows), ("review", review_rows)):
                (sessions / f"rollout-{name}.jsonl").write_text(
                    "\n".join(json.dumps(row) for row in rows) + "\n"
                )
            (trial / "agent/codex.txt").write_text(
                json.dumps({"type": "thread.started", "thread_id": "main"}) + "\n"
            )
            source, rows = main_rollout(trial, trajectory)
            self.assertEqual(source.name, "rollout-main.jsonl")
            with self.assertRaisesRegex(ValueError, "Subagent-only Harbor cost"):
                trial_usage(trial, result, trajectory, rows)
            result["agent_result"]["cost_usd"] = None
            trajectory["final_metrics"]["total_cost_usd"] = None
            usage = trial_usage(trial, result, trajectory, rows)
            self.assertEqual(usage["tokens"]["total"], 240)
            self.assertEqual(usage["raw"]["harborAgentContext"], result["agent_result"])
            self.assertEqual(usage["raw"]["harborAccountingSessionId"], "review")
            self.assertIsNone(usage["cost"])
            # An unrelated session must never be added to run accounting.
            (sessions / "rollout-unrelated.jsonl").write_text(
                "\n".join(
                    json.dumps(row)
                    for row in [
                        {"type": "session_meta", "payload": {"id": "unrelated"}},
                        *usage_rows,
                    ]
                )
                + "\n"
            )
            with self.assertRaisesRegex(ValueError, "descendant"):
                trial_usage(trial, result, trajectory, rows)
            (trial / "agent/codex.txt").write_text(
                "\n".join(
                    json.dumps({"type": "thread.started", "thread_id": session_id})
                    for session_id in ("main", "review")
                )
            )
            with self.assertRaisesRegex(ValueError, "multiple main threads"):
                main_rollout(trial, trajectory)

    def usage_fixture(self):
        raw = {
            "input_tokens": 100,
            "cached_input_tokens": 80,
            "output_tokens": 20,
            "reasoning_output_tokens": 15,
            "total_tokens": 120,
        }
        context = {
            "n_input_tokens": 100,
            "n_cache_tokens": 80,
            "n_output_tokens": 20,
            "cost_usd": 0.0123,
        }
        trajectory = {
            "final_metrics": {
                "total_prompt_tokens": 100,
                "total_cached_tokens": 80,
                "total_completion_tokens": 20,
                "total_cost_usd": 0.0123,
                "extra": {"total_tokens": 120},
            }
        }
        rows = [
            {
                "type": "event_msg",
                "payload": {"type": "token_count", "info": {"total_token_usage": raw}},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Recorded task"}],
                },
            },
        ]
        return {"agent_result": context}, trajectory, rows

    def test_uses_native_cost_without_pricing_or_double_counting_cached_tokens(self):
        result, trajectory, rows = self.usage_fixture()
        usage = native_usage(result, trajectory, rows)
        self.assertEqual(
            usage["cost"], {"currency": "USD", "total": 0.0123, "estimated": True}
        )
        self.assertEqual(usage["tokens"]["input"], 100)
        self.assertEqual(usage["tokens"]["total"], 120)
        self.assertNotIn("cacheCreationInput", usage["tokens"])
        rows[0]["payload"]["info"]["cost_usd"] = 0.0123
        self.assertFalse(native_usage(result, trajectory, rows)["cost"]["estimated"])

    def test_unknown_cost_stays_unknown_and_inconsistent_accounting_is_rejected(self):
        result, trajectory, rows = self.usage_fixture()
        result["agent_result"]["cost_usd"] = None
        trajectory["final_metrics"]["total_cost_usd"] = None
        self.assertIsNone(native_usage(result, trajectory, rows)["cost"])
        result["agent_result"]["n_input_tokens"] = 180
        with self.assertRaisesRegex(ValueError, "disagrees"):
            native_usage(result, trajectory, rows)

    def test_recovery_uses_recorded_trajectory_without_inventing_agent_context(self):
        _, trajectory, rows = self.usage_fixture()
        usage = native_usage(None, trajectory, rows)
        self.assertEqual(usage["cost"]["total"], 0.0123)
        self.assertNotIn("harborAgentContext", usage["raw"])
        trajectory["final_metrics"]["total_prompt_tokens"] = 180
        with self.assertRaisesRegex(ValueError, "disagrees"):
            native_usage(None, trajectory, rows)

    def test_recovery_requires_recorded_completion_and_preserves_session_timing(self):
        _, trajectory, rows = self.usage_fixture()
        trajectory.update(
            session_id="fixture",
            agent={"name": "codex", "model_name": "gpt-6.1-sol", "version": "0.159.2"},
        )
        rows[:0] = [
            {
                "type": "session_meta",
                "payload": {
                    "id": "fixture",
                    "cwd": "/app",
                    "cli_version": "0.159.2",
                    "timestamp": "2026-09-30T12:00:00Z",
                },
            },
            {
                "type": "turn_context",
                "payload": {"model": "gpt-6.1-sol", "effort": "max"},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": json.dumps(
                        {
                            "cmd": "cat > /app/index.html <<'EOF'\n<div>recorded</div>\nEOF\n"
                        }
                    ),
                },
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            trial = Path(directory)
            (trial / "agent/sessions").mkdir(parents=True)
            (trial / "artifacts/app").mkdir(parents=True)
            (trial / "artifacts/app/index.html").write_text("<div>recorded</div>\n")
            (trial / "artifacts/manifest.json").write_text(
                json.dumps(
                    [
                        {
                            "source": "/app/index.html",
                            "destination": "artifacts/app/index.html",
                            "status": "ok",
                        }
                    ]
                )
            )
            (trial / "config.json").write_text(
                json.dumps(
                    {
                        "agent": {
                            "name": "codex",
                            "model_name": "openai/gpt-6.1-sol",
                            "kwargs": {
                                "version": "0.159.2",
                                "config": {"model_reasoning_effort": "max"},
                            },
                        }
                    }
                )
            )
            (trial / "lock.json").write_text(
                json.dumps({"task": {"name": "fixture", "digest": "recorded-fixture"}})
            )
            (trial / "agent/trajectory.json").write_text(json.dumps(trajectory))
            source = trial / "agent/sessions/rollout-fixture.jsonl"

            def write_source():
                source.write_text("\n".join(json.dumps(row) for row in rows) + "\n")

            write_source()
            with self.assertRaisesRegex(ValueError, "recorded task completion"):
                build_recovered_trial(trial)
            rows.append(
                {
                    "type": "event_msg",
                    "timestamp": "2026-09-30T12:02:00Z",
                    "payload": {"type": "task_complete"},
                }
            )
            write_source()
            with patch("import_harbor_codex.recovery_grade"):
                run, _, _ = build_recovered_trial(trial)
            self.assertEqual(run["timing"]["wallDurationMs"], 120000)
            self.assertTrue(run["usage"]["raw"]["harborTrialResultMissing"])
            self.assertFalse((trial / "result.json").exists())

    def test_recovery_rejects_reward_that_differs_from_saved_grade(self):
        with tempfile.TemporaryDirectory() as directory:
            trial = Path(directory)
            (trial / "verifier").mkdir()
            (trial / "verifier/grade.json").write_text(
                json.dumps({"reward": 0.8, "scores": {"standalone": 1}})
            )
            (trial / "verifier/reward.json").write_text(
                json.dumps({"reward": 0.7, "standalone": 1})
            )
            with (
                patch("ramen_harbor.validate_grade"),
                self.assertRaisesRegex(ValueError, "disagrees"),
            ):
                recovery_grade(trial, trial / "artifact")

    def test_replays_absolute_heredoc_and_recorded_path_repair_exactly(self):
        commands = [
            "cat > /app/index.html <<'HTML'\n<div>before</div>\nHTML\n",
            "python - <<'PY'\nfrom pathlib import Path\np=Path('/app/index.html')\ns=p.read_text().replace('before','after')\np.write_text(s)\nPY",
        ]
        rows = [
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": command}),
                },
            }
            for command in commands
        ]
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "index.html"
            artifact.write_text("<div>after</div>\n")
            self.assertIn(
                "2 operations", verify_codex_result(rows, "fixture", artifact)
            )
            artifact.write_text("<div>tampered</div>\n")
            with self.assertRaisesRegex(ValueError, "differs"):
                verify_codex_result(rows, "fixture", artifact)

    def test_path_repair_cannot_execute_arbitrary_python(self):
        command = "python - <<'PY'\nfrom pathlib import Path\np=Path('/app/index.html')\ns=__import__('os').getcwd()\np.write_text(s)\nPY"
        rows = [
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "arguments": json.dumps(
                        {"cmd": "cat > index.html <<'EOF'\nold\nEOF\n"}
                    ),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "arguments": json.dumps({"cmd": command}),
                },
            },
        ]
        with self.assertRaisesRegex(ValueError, "unsupported expression"):
            verify_codex_result(rows, "fixture", Path("unused"))

    def test_artifact_manifest_rejects_missing_collection_and_directory_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            trial = Path(directory)
            (trial / "artifacts").mkdir()
            manifest = trial / "artifacts/manifest.json"
            for destination, status in (
                ("artifacts/missing.html", "failed"),
                ("../index.html", "ok"),
            ):
                manifest.write_text(
                    json.dumps(
                        [
                            {
                                "source": "/app/index.html",
                                "destination": destination,
                                "status": status,
                            }
                        ]
                    )
                )
                with self.assertRaises(ValueError):
                    native_artifact(trial)

    def test_registration_orders_efforts_and_preserves_human_ratings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "harbor").mkdir()
            (root / "registry.json").write_text(
                json.dumps({"runs": ["openai/older/low/run.json"]})
            )
            human = {
                "weight": 0.8,
                "ratings": [
                    {
                        "runId": "openai/new/low",
                        "humanScore": 0.91,
                        "note": "Recorded opinion",
                    }
                ],
            }
            (root / "harbor/human-scores.json").write_text(json.dumps(human))
            runs = [
                {
                    "id": f"openai/new/{effort}",
                    "vendor": {"slug": "openai"},
                    "model": {"slug": "new"},
                }
                for effort in ("low", "ultra", "high")
            ]
            register(root, runs)
            first = (root / "registry.json").read_bytes()
            register(root, runs)
            self.assertEqual(first, (root / "registry.json").read_bytes())
            self.assertEqual(
                json.loads(first)["runs"],
                [
                    "openai/new/ultra/run.json",
                    "openai/new/high/run.json",
                    "openai/new/low/run.json",
                    "openai/older/low/run.json",
                ],
            )
            self.assertEqual(
                json.loads((root / "harbor/human-scores.json").read_text())["ratings"][
                    0
                ],
                human["ratings"][0],
            )

    def test_preserves_actual_task_and_excludes_injected_context(self):
        from backfill_session_metadata import PROMPT

        text = PROMPT + "\nWrite the final result to /app/index.html."
        rows = [
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": value}],
                },
            }
            for value in ("<environment_context>private</environment_context>", text)
        ]
        events, _ = normalize_codex_events(rows, preserve_task=True)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["content"], text)


if __name__ == "__main__":
    unittest.main()
