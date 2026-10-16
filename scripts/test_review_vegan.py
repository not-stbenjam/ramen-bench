"""Evidence integrity and conservative consensus for trajectory-aware judging."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import review_vegan as workflow

policy = workflow.policy


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.run = {"id": "fixture/model/high"}
        self.artifact = (
            b"// Soy-marinated vegan egg with plant-curd white and pumpkin yolk\n"
        )
        self.transcript = json.dumps(
            {
                "runId": self.run["id"],
                "events": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": "USER-GOAL vegan eggs",
                    },
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": "Created vegan ajitama with pumpkin yolk.",
                        "thinking": "HIDDEN-REASONING",
                    },
                    {
                        "type": "tool_call",
                        "tool": "write_to_file",
                        "input": {"CodeContent": self.artifact.decode()},
                    },
                    {"type": "tool_result", "output": "Wrote the file."},
                ],
            }
        ).encode()
        self.judge = {
            "models": ["a", "b", "c"],
            "efforts": {m: "default" for m in "abc"},
            "samples": 3,
        }
        self.score = {
            "runId": self.run["id"],
            "artifactSha256": policy.digest(self.artifact),
            "reward": 0,
            "scores": {"standalone": 1, "procedural": 1},
            "diagnostics": {"veganFailed": True},
            "judgeResults": [
                {
                    "model": m,
                    "judgments": [{"vegan": {"reason": "Egg-shaped topping."}}],
                }
                for m in "abc"
            ],
        }
        self.bundle = policy.build_input(
            self.run, self.artifact, self.transcript, self.score, self.judge
        )

    def sample(self, decision="plant_based_substitutes"):
        evidence = self.bundle["evidence"][0]
        return {
            "judgment": {
                "decision": decision,
                "reason": "Recorded ingredient declaration.",
                "evidence": [{"id": evidence["id"]}],
            },
            "usage": None,
            "requestSha256": "a" * 64,
            "invalidAttempts": [],
        }

    def results(self):
        return [
            {
                "model": m,
                "effort": "default",
                "judgments": [self.sample() for _ in range(3)],
            }
            for m in "abc"
        ]

    def test_input_is_deterministic_verbatim_and_excludes_user_goals_and_hidden_data(
        self,
    ):
        rebuilt = policy.build_input(
            self.run, self.artifact, self.transcript, self.score, self.judge
        )
        self.assertEqual(self.bundle, rebuilt)
        payload = policy.canonical(self.bundle).decode()
        self.assertNotIn("USER-GOAL", payload)
        self.assertNotIn("HIDDEN-REASONING", payload)
        self.assertIn("Created vegan ajitama with pumpkin yolk.", payload)
        self.assertTrue(any(e["source"] == "artifact" for e in self.bundle["evidence"]))
        self.assertEqual(
            self.bundle["transcriptSha256"], policy.digest(self.transcript)
        )

    def test_unrecorded_citations_and_evidence_free_restorations_fail(self):
        sample = self.sample()["judgment"]
        self.assertEqual(policy.parse_judgment(json.dumps(sample), self.bundle), sample)
        for replacement in (
            {"id": "invented"},
            {"id": [sample["evidence"][0]["id"]]},
        ):
            with self.assertRaisesRegex(ValueError, "recorded evidence"):
                policy.parse_judgment(
                    json.dumps({**sample, "evidence": [replacement]}), self.bundle
                )
        with self.assertRaisesRegex(ValueError, "requires cited"):
            policy.parse_judgment(json.dumps({**sample, "evidence": []}), self.bundle)

    def test_every_judge_and_sample_is_required_and_unknown_votes_do_not_clear_gate(
        self,
    ):
        results = self.results()
        self.assertTrue(policy.summarize(results, self.judge, self.bundle))
        for item in results[:2]:
            item["judgments"] = [self.sample("insufficient_evidence") for _ in range(3)]
        self.assertFalse(policy.summarize(results, self.judge, self.bundle))
        with self.assertRaisesRegex(ValueError, "every distinct"):
            policy.summarize(results[:-1], self.judge, self.bundle)
        results[0]["judgments"].pop()
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            policy.summarize(results, self.judge, self.bundle)

    def test_wrong_artifact_and_other_compliance_failures_cannot_be_reviewed(self):
        with self.assertRaisesRegex(ValueError, "sources do not match"):
            policy.build_input(
                self.run, b"different", self.transcript, self.score, self.judge
            )
        for field in ("standalone", "procedural"):
            score = copy.deepcopy(self.score)
            score["scores"][field] = 0
            with self.assertRaisesRegex(ValueError, "other compliance"):
                policy.build_input(
                    self.run, self.artifact, self.transcript, score, self.judge
                )

    def test_requests_keep_candidate_text_untrusted_and_exclude_provider_reasoning(
        self,
    ):
        request = policy.request(self.bundle, "openrouter/model", "low")
        self.assertIn("untrusted", request["messages"][0]["content"])
        self.assertIn("supplied evidence IDs", request["messages"][0]["content"])
        self.assertEqual(
            request["extra_body"]["reasoning"], {"exclude": True, "effort": "low"}
        )
        self.assertNotIn("tools", request)

    def test_failed_review_never_leaves_reward(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "reward.json").write_text('{"restore_vegan_gate": 1}')
            with (
                patch.object(
                    policy, "judge_model", side_effect=ValueError("invalid response")
                ),
                self.assertRaisesRegex(ValueError, "invalid response"),
            ):
                policy.judge(self.bundle, output)
            self.assertFalse((output / "reward.json").exists())

    def test_only_invalid_formats_retry_and_private_responses_are_reused(self):
        valid = self.sample("insufficient_evidence")["judgment"]
        self.bundle["judge"]["samples"] = 1
        responses = ["not JSON", json.dumps(valid)]

        def completion(**kwargs):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(message=SimpleNamespace(content=responses.pop(0)))
                ],
                usage=None,
            )

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                "sys.modules", {"litellm": SimpleNamespace(completion=completion)}
            ),
        ):
            output = Path(directory)
            result = policy.judge_model(self.bundle, output, "a")
            sample = result["judgments"][0]
            self.assertEqual(sample["judgment"], valid)
            self.assertEqual(len(sample["invalidAttempts"]), 1)
            self.assertFalse(responses)
            self.assertEqual(len(list(output.glob("response-*.json"))), 2)
            for checkpoint in output.glob("review-*.json"):
                checkpoint.unlink()
            self.assertEqual(policy.judge_model(self.bundle, output, "a"), result)

    def test_three_invalid_citations_fail_without_a_completed_sample(self):
        self.bundle["judge"]["samples"] = 1
        invalid = self.sample()["judgment"]
        invalid["evidence"][0]["id"] = "invented"
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content=json.dumps(invalid)))
            ],
            usage=None,
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                "sys.modules",
                {"litellm": SimpleNamespace(completion=lambda **kwargs: response)},
            ),
        ):
            output = Path(directory)
            with self.assertRaisesRegex(ValueError, "Three invalid"):
                policy.judge_model(self.bundle, output, "a")
            self.assertEqual(len(list(output.glob("response-*.json"))), 3)
            self.assertFalse(list(output.glob("review-*.json")))

    def test_export_builds_its_own_verifier_and_preserves_policy_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with patch.object(workflow, "bundle_for", return_value=self.bundle):
                workflow.export(output, [self.run["id"]])
            task = output / "vegan--fixture--model--high"
            self.assertNotIn("docker_image", (task / "task.toml").read_text())
            self.assertIn("vegan_review.py", (task / "tests/test.sh").read_text())
            for name in policy.FILES:
                self.assertEqual(
                    (task / "tests" / name).read_bytes(),
                    (policy.HERE / name).read_bytes(),
                )

    def test_source_or_consensus_changes_invalidate_a_published_review(self):
        results = self.results()
        for judge in results:
            key = policy.digest(
                policy.canonical(
                    policy.request(self.bundle, judge["model"], judge["effort"])
                )
            )
            for sample in judge["judgments"]:
                sample["requestSha256"] = key
        review = {
            key: self.bundle[key]
            for key in (
                "schemaVersion",
                "runId",
                "artifactSha256",
                "transcriptSha256",
                "sourceScoreSha256",
                "sourceReward",
                "policySha256",
                "policyVersion",
                "judge",
                "omittedEvidenceChunks",
            )
        }
        review.update(
            inputSha256=policy.digest(policy.canonical(self.bundle)),
            reviewedAt="2026-10-16T00:00:00Z",
            judgeResults=results,
            restoreVeganGate=True,
            evidenceSources=policy.citation_sources(self.bundle, results),
        )
        workflow.validate_review(review, self.bundle)
        with self.assertRaisesRegex(ValueError, "consensus mismatch"):
            workflow.validate_review({**review, "restoreVeganGate": False}, self.bundle)
        with self.assertRaisesRegex(ValueError, "Stale"):
            workflow.validate_review(
                {**review, "transcriptSha256": "b" * 64}, self.bundle
            )


if __name__ == "__main__":
    unittest.main()
