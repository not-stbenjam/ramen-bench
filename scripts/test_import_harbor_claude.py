"""Evidence and accounting checks for native Harbor Claude imports."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from import_harbor_claude import ROOT, build_trial, native_usage, select_grade


class HarborClaudeTests(unittest.TestCase):
    def fixture(self, trial):
        artifact = b"<!doctype html>\r\n<html>ramen</html>\r\n"
        rows = [
            {
                "type": "user",
                "message": {
                    "content": (ROOT / "harbor/ramen/instruction.md").read_text()
                },
            },
            {
                "type": "assistant",
                "message": {
                    "id": "fixture-message",
                    "model": "claude-haiku-5-5",
                    "content": [
                        {"type": "thinking", "thinking": "private fixture reasoning"},
                        {
                            "type": "tool_use",
                            "id": "fixture-write",
                            "name": "Write",
                            "input": {
                                "file_path": "/app/index.html",
                                "content": artifact.decode(),
                            },
                        },
                    ],
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 20,
                        "cache_read_input_tokens": 30,
                        "cache_creation_input_tokens": 40,
                        "output_tokens_details": {"thinking_tokens": 5},
                    },
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "fixture-write",
                            "content": "Created index.html",
                        }
                    ]
                },
            },
        ]
        result = {
            "id": "fixture-trial",
            "exception_info": None,
            "task_checksum": "fixture-task",
            "agent_info": {"name": "claude-code", "version": "2.1.295"},
            "agent_execution": {
                "started_at": "2026-10-08T10:00:00Z",
                "finished_at": "2026-10-08T10:00:01Z",
            },
            "agent_result": {
                "n_input_tokens": 80,
                "n_cache_tokens": 30,
                "n_output_tokens": 20,
                "cost_usd": 0.25,
            },
        }
        completion = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": "fixture-session",
            "total_cost_usd": 0.25,
            "modelUsage": {"claude-haiku-5-5": {"costUSD": 0.25, "costBasis": "list"}},
        }
        init = {
            "type": "system",
            "subtype": "init",
            "model": "claude-haiku-5-5",
            "claude_code_version": "2.1.295",
            "session_id": "fixture-session",
        }
        documents = {
            "config.json": {
                "agent": {
                    "name": "claude-code",
                    "model_name": "anthropic/claude-haiku-5-5",
                    "kwargs": {"reasoning_effort": "max"},
                }
            },
            "result.json": result,
            "artifacts/manifest.json": [
                {
                    "source": "/app/index.html",
                    "destination": "artifacts/app/index.html",
                    "status": "ok",
                }
            ],
        }
        for name, value in documents.items():
            path = trial / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value))
        path = trial / "agent/sessions/projects/-app/fixture-session.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        (trial / "agent/claude-code.txt").write_text(
            "".join(json.dumps(event) + "\n" for event in (init, completion))
        )
        path = trial / "artifacts/app/index.html"
        path.parent.mkdir(parents=True)
        path.write_bytes(artifact)
        return rows, result, completion

    def test_import_preserves_bytes_and_excludes_hidden_reasoning(self):
        with tempfile.TemporaryDirectory() as directory:
            trial = Path(directory)
            self.fixture(trial)
            run, transcript, artifact = build_trial(trial)
            self.assertEqual(run["id"], "anthropic/haiku-5.5/max")
            self.assertEqual(run["usage"]["tokens"]["total"], 100)
            self.assertEqual(run["usage"]["tokens"]["reasoning"], 5)
            self.assertTrue(run["usage"]["cost"]["estimated"])
            self.assertEqual(run["timing"]["wallDurationMs"], 1000)
            self.assertEqual(run["resultStats"]["bytes"], len(artifact.read_bytes()))
            self.assertNotIn("private fixture reasoning", json.dumps(transcript))
            self.assertEqual(
                transcript["events"][0]["content"],
                (ROOT / "harbor/ramen/instruction.md").read_text().strip(),
            )

    def test_rejects_changed_artifact_even_with_equivalent_line_endings(self):
        with tempfile.TemporaryDirectory() as directory:
            trial = Path(directory)
            self.fixture(trial)
            path = trial / "artifacts/app/index.html"
            path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n"))
            with self.assertRaisesRegex(ValueError, "final recorded write"):
                build_trial(trial)

    def test_rejects_failed_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            trial = Path(directory)
            _, _, completion = self.fixture(trial)
            completion["is_error"] = True
            path = trial / "agent/claude-code.txt"
            path.write_text(
                path.read_text().splitlines()[0] + "\n" + json.dumps(completion) + "\n"
            )
            with self.assertRaisesRegex(ValueError, "did not complete"):
                build_trial(trial)

    def test_unknown_cost_is_not_promoted_and_usage_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            rows, result, completion = self.fixture(Path(directory))
            self.assertEqual(
                native_usage(rows, result, completion)["cost"]["total"], 0.25
            )
            completion["hasUnknownModelCost"] = True
            self.assertIsNone(native_usage(rows, result, completion)["cost"])
            result["agent_result"]["n_input_tokens"] += 1
            with self.assertRaisesRegex(ValueError, "usage disagrees"):
                native_usage(rows, result, completion)

    def test_verifier_failure_retains_generation_but_agent_failure_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            trial = Path(directory)
            _, result, _ = self.fixture(trial)
            result["exception_info"] = {"exception_type": "RewardFileNotFoundError"}
            (trial / "result.json").write_text(json.dumps(result))
            run, _, _ = build_trial(trial)
            self.assertEqual(
                run["usage"]["raw"]["harborVerifierFailure"], "RewardFileNotFoundError"
            )
            result["exception_info"]["exception_type"] = "AgentTimeoutError"
            (trial / "result.json").write_text(json.dumps(result))
            with self.assertRaisesRegex(ValueError, "unsuccessful"):
                build_trial(trial)

    def test_regrade_must_match_original_trial_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trial = root / "original"
            self.fixture(trial)
            regrade = root / "regrades" / "retry"
            regrade.mkdir(parents=True)
            (regrade / "result.json").write_text(json.dumps({"exception_info": None}))
            config = {
                "source_trial": {
                    "action": "regrade",
                    "type": "local",
                    "path": str(trial),
                    "trial_id": "other-trial",
                }
            }
            (regrade / "config.json").write_text(json.dumps(config))
            self.assertEqual(select_grade(trial, [regrade.parent]), trial)
            config["source_trial"]["trial_id"] = "fixture-trial"
            (regrade / "config.json").write_text(json.dumps(config))
            self.assertEqual(select_grade(trial, [regrade.parent]), regrade)


if __name__ == "__main__":
    unittest.main()
