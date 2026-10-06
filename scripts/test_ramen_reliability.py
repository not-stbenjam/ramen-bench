"""Retry bounds and immutable evidence/sample checkpoint integrity."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "harbor/ramen/tests/reliability.py"
SPEC = importlib.util.spec_from_file_location("ramen_reliability", SOURCE)
reliability = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reliability)


class Clock:
    def __init__(self):
        self.now = 0
        self.delays = []

    def read(self):
        return self.now

    def sleep(self, delay):
        self.delays.append(delay)
        self.now += delay


class ProviderError(Exception):
    def __init__(self, status_code, usage=None):
        super().__init__("Bearer private-test-secret provider response body")
        self.status_code = status_code
        self.usage = usage


def validate_judgment(value):
    if set(value) != {"bowl"} or set(value["bowl"]) != {"score", "reason"}:
        raise ValueError("Invalid fixture judgment")
    entry = value["bowl"]
    if (
        type(entry["score"]) not in (float, int)
        or not 0 <= entry["score"] <= 1
        or not entry["reason"]
    ):
        raise ValueError("Invalid fixture judgment")


class RetryTests(unittest.TestCase):
    def test_transient_retries_keep_failed_usage_and_bound_delays(self):
        clock, failures, calls = Clock(), [], []
        usage = {
            "prompt_tokens": 32,
            "completion_tokens_details": {"reasoning_tokens": 4, "text_tokens": None},
            "is_byok": False,
        }

        def operation():
            calls.append(1)
            if len(calls) < 3:
                raise ProviderError(429, usage)
            return "actual response"

        actual = reliability.retry(
            operation,
            stage="completion",
            base_delay=2,
            max_delay=3,
            clock=clock.read,
            sleep=clock.sleep,
            on_failure=lambda error, recorded: failures.append(
                (error.metadata(), recorded)
            ),
        )
        self.assertEqual(actual, "actual response")
        self.assertEqual(clock.delays, [2, 3])
        self.assertEqual([error["attempt"] for error, _ in failures], [1, 2])
        self.assertEqual([recorded for _, recorded in failures], [usage, usage])
        self.assertNotIn("private-test-secret", json.dumps(failures))

    def test_permanent_errors_are_never_retried(self):
        for error in (
            ProviderError(401),
            ProviderError(403),
            ProviderError(400),
            ProviderError(404),
            reliability.StageError("config", "unsupported_model", retryable=True),
            reliability.StageError("config", "invalid_config", retryable=True),
        ):
            clock, operation = Clock(), unittest.mock.Mock(side_effect=error)
            with (
                self.subTest(error=type(error).__name__),
                self.assertRaises(reliability.StageError),
            ):
                reliability.retry(
                    operation, stage="completion", clock=clock.read, sleep=clock.sleep
                )
            self.assertEqual(operation.call_count, 1)
            self.assertEqual(clock.delays, [])

    def test_attempt_and_deadline_bounds(self):
        for deadline, expected_calls, expected_delays in (
            (None, 3, [1, 2]),
            (1.5, 2, [1]),
            (0, 1, []),
        ):
            clock, operation = (
                Clock(),
                unittest.mock.Mock(side_effect=TimeoutError("private-test-secret")),
            )
            with (
                self.subTest(deadline=deadline),
                self.assertRaises(reliability.StageError) as caught,
            ):
                reliability.retry(
                    operation,
                    stage="capture",
                    base_delay=1,
                    deadline_seconds=deadline,
                    clock=clock.read,
                    sleep=clock.sleep,
                )
            self.assertEqual(operation.call_count, expected_calls)
            self.assertEqual(clock.delays, expected_delays)
            self.assertEqual(caught.exception.attempt, expected_calls)
            self.assertEqual(caught.exception.code, "timeout")
            self.assertNotIn("private-test-secret", str(caught.exception))

    def test_unknown_errors_not_retried_and_interruptions_propagate(self):
        clock, operation = (
            Clock(),
            unittest.mock.Mock(side_effect=ValueError("private-test-secret")),
        )
        with self.assertRaises(reliability.StageError):
            reliability.retry(
                operation, stage="completion", clock=clock.read, sleep=clock.sleep
            )
        self.assertEqual(operation.call_count, 1)
        with self.assertRaises(KeyboardInterrupt):
            reliability.retry(
                unittest.mock.Mock(side_effect=KeyboardInterrupt), stage="capture"
            )

    def test_safe_stage_metadata_and_payload_filtering(self):
        error = reliability.safe_error(
            ProviderError(503), "completion", sample=2, attempt=3
        )
        self.assertEqual(
            error.metadata(),
            {
                "stage": "completion",
                "code": "provider_unavailable",
                "retryable": True,
                "status_code": 503,
                "sample": 2,
                "attempt": 3,
            },
        )
        self.assertEqual(
            reliability.numeric_usage(
                {
                    "prompt_tokens": 7,
                    "api_key": "private-test-secret",
                    "raw_payload": 4,
                    "response": "private-test-secret",
                    "cost": float("nan"),
                }
            ),
            {"prompt_tokens": 7},
        )


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "capture.json"
        self.artifact = b"<html>actual immutable artifact</html>"
        self.policy = {
            "renderer": "pinned-test-version",
            "seed": 42,
            "timings": [0, 1.5],
        }
        self.rendering = {
            "screenshots": [
                {"file": "first.png", "elapsed": 0.532019},
                {"file": "second.png", "elapsed": 2.802719},
            ],
            "measurements": {"desktop": {"rafMedianMs": 16.7}},
            "pageErrors": [],
            "violations": [],
        }
        (self.root / "first.png").write_bytes(b"actual frame one fixture")
        (self.root / "second.png").write_bytes(b"actual frame two fixture")
        self.judgments = [
            {"bowl": {"score": 0.4, "reason": "First authentic fixture judgment"}}
        ]
        self.usages = [
            {"sample": 1, "attempt": 1, "completion_tokens": 17},
            {"sample": 2, "attempt": 1, "completion_tokens": 23},
        ]
        self.key = reliability.judge_key(
            "capture-identity",
            "model/fixture",
            "low",
            "exact fixture prompt",
            {"max_tokens": 100},
        )

    def test_capture_resume_preserves_exact_metadata_and_detects_stale_config(self):
        saved = reliability.save_capture(
            self.path, self.artifact, self.policy, self.rendering, self.root
        )
        resumed = reliability.load_capture(
            self.path, self.artifact, self.policy, self.root
        )
        self.assertEqual(saved, resumed)
        self.assertEqual(resumed["rendering"], self.rendering)
        self.assertIsNone(
            reliability.load_capture(
                self.path, b"changed artifact", self.policy, self.root
            )
        )
        self.assertIsNone(
            reliability.load_capture(
                self.path, self.artifact, {**self.policy, "seed": 43}, self.root
            )
        )

    def test_frame_change_missing_frame_and_metadata_tampering_are_rejected(self):
        for mutation in ("frame", "missing", "metadata"):
            with self.subTest(mutation=mutation):
                (self.root / "first.png").write_bytes(b"actual frame one fixture")
                reliability.save_capture(
                    self.path, self.artifact, self.policy, self.rendering, self.root
                )
                if mutation == "frame":
                    (self.root / "first.png").write_bytes(b"different rendering")
                elif mutation == "missing":
                    (self.root / "first.png").unlink()
                else:
                    envelope = json.loads(self.path.read_text())
                    envelope["payload"]["rendering"]["screenshots"][0]["elapsed"] = 9
                    self.path.write_text(json.dumps(envelope))
                with self.assertRaises(reliability.StageError) as caught:
                    reliability.load_capture(
                        self.path, self.artifact, self.policy, self.root
                    )
                self.assertEqual(caught.exception.code, "checkpoint_integrity")

    def test_capture_and_request_identity_cover_all_evidence_and_options(self):
        first = reliability.save_capture(
            self.path, self.artifact, self.policy, self.rendering, self.root
        )
        (self.root / "second.png").write_bytes(b"replacement second frame")
        second = reliability.save_capture(
            self.path, self.artifact, self.policy, self.rendering, self.root
        )
        self.assertNotEqual(first["identity"], second["identity"])
        original = (
            first["identity"],
            "model/fixture",
            "low",
            "prompt",
            {"max_tokens": 100},
        )
        key = reliability.judge_key(*original)
        for index, replacement in enumerate(
            (
                second["identity"],
                "model/other",
                "high",
                "changed prompt",
                {"max_tokens": 200},
            )
        ):
            changed = list(original)
            changed[index] = replacement
            self.assertNotEqual(key, reliability.judge_key(*changed))

    def test_interrupted_atomic_write_keeps_previous_complete_checkpoint(self):
        reliability.save_judge(
            self.path,
            self.key,
            self.judgments,
            self.usages,
            3,
            validate_judgment=validate_judgment,
        )
        before = self.path.read_bytes()
        with (
            patch.object(
                reliability.os, "replace", side_effect=OSError("private-test-secret")
            ),
            self.assertRaises(reliability.StageError) as caught,
        ):
            reliability.save_judge(
                self.path,
                self.key,
                self.judgments * 2,
                self.usages,
                3,
                validate_judgment=validate_judgment,
            )
        self.assertEqual(caught.exception.code, "checkpoint_write_failed")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.root.glob(".capture.json.*")), [])
        self.assertEqual(
            reliability.load_judge(self.path, self.key, 3)["judgments"], self.judgments
        )

    def test_resume_prefix_preserves_failed_next_sample_usage(self):
        reliability.save_judge(
            self.path,
            self.key,
            self.judgments,
            self.usages,
            3,
            validate_judgment=validate_judgment,
        )
        resumed = reliability.load_judge(
            self.path, self.key, 3, validate_judgment=validate_judgment
        )
        self.assertEqual(resumed["judgments"], self.judgments)
        self.assertEqual(resumed["usages"], self.usages)
        self.assertIsNone(reliability.load_judge(self.path, "other-request", 3))
        self.assertIsNone(reliability.load_judge(self.path, self.key, 2))
        completed = resumed["judgments"] + [
            {"bowl": {"score": 0.8, "reason": "Second authentic fixture judgment"}}
        ]
        usages = resumed["usages"] + [
            {"sample": 2, "attempt": 2, "completion_tokens": 31}
        ]
        reliability.save_judge(
            self.path,
            self.key,
            completed,
            usages,
            3,
            validate_judgment=validate_judgment,
        )
        resumed = reliability.load_judge(
            self.path, self.key, 3, validate_judgment=validate_judgment
        )
        self.assertEqual(resumed["judgments"], completed)
        self.assertEqual(resumed["usages"], usages)

    def test_invalid_prefix_and_duplicate_usage_fail_validation(self):
        for judgments, usages in (
            (self.judgments, [{"sample": 3, "attempt": 1}]),
            (self.judgments, self.usages + [self.usages[0]]),
            ([{"bowl": {"score": 3, "reason": "Invalid"}}], []),
            (
                self.judgments,
                [{"sample": 1, "attempt": 1, "provider_text": "private-test-secret"}],
            ),
        ):
            with (
                self.subTest(judgments=judgments),
                self.assertRaises(reliability.StageError),
            ):
                reliability.save_judge(
                    self.path,
                    self.key,
                    judgments,
                    usages,
                    3,
                    validate_judgment=validate_judgment,
                )

    def test_partial_or_changed_ensemble_never_becomes_publishable(self):
        reliability.save_judge(
            self.path,
            self.key,
            self.judgments,
            self.usages,
            3,
            validate_judgment=validate_judgment,
        )
        prefix = reliability.load_judge(self.path, self.key, 3)
        expected = {"first": self.key, "second": "another-exact-request"}
        partial = {"first": {"key": self.key, "payload": prefix}}
        with self.assertRaises(reliability.StageError):
            reliability.ready_ensemble(partial, expected, 3)
        complete = {
            "samples": 3,
            "judgments": self.judgments * 3,
            "usages": self.usages,
        }
        ensemble = {
            **partial,
            "second": {"key": expected["second"], "payload": complete},
        }
        with self.assertRaises(reliability.StageError):
            reliability.ready_ensemble(ensemble, expected, 3)
        ensemble["first"]["payload"] = complete
        actual = reliability.ready_ensemble(
            ensemble, expected, 3, validate_judgment=validate_judgment
        )
        self.assertEqual(
            actual, {"first": complete["judgments"], "second": complete["judgments"]}
        )
        ensemble["first"]["key"] = "changed-request"
        with self.assertRaises(reliability.StageError):
            reliability.ready_ensemble(ensemble, expected, 3)

    def test_bad_json_and_unsafe_frame_paths_fail_closed(self):
        self.path.write_text('{"partial":')
        with self.assertRaises(reliability.StageError):
            reliability.load_judge(self.path, self.key, 3)
        rendering = {"screenshots": [{"file": "../outside.png"}]}
        with self.assertRaises(reliability.StageError):
            reliability.save_capture(
                self.path, self.artifact, self.policy, rendering, self.root
            )


if __name__ == "__main__":
    unittest.main()
