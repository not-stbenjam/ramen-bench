"""Verifier accounting, immutable export, failure handling and public provenance checks."""

import copy
import importlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from scripts import ramen_harbor as workflow

verifier = importlib.import_module("grade")


class GradingTests(unittest.TestCase):
    @staticmethod
    def capture_fixture(source, output):
        (output / "fixture.png").write_bytes(b"Deterministic captured frame fixture")
        return {
            "screenshots": [{"file": "fixture.png"}],
            "violations": [],
            "pageErrors": [],
            "measurements": {},
        }

    def test_dependency_gate(self):
        cases = [
            '<script src="app.js"></script>',
            '<link rel="stylesheet" href="https://example.com/style.css">',
            '<img srcset="soup.png 1x, soup-2.png 2x">',
            '<svg><image href="../soup.svg"/></svg>',
            '<style>@import "style.css";</style>',
            "<style>body { background: url(soup.png) }</style>",
            '<script type="module">import x from "./x.js";</script>',
            '<script>import("https://example.com/code.js")</script>',
            '<script type="module">export {x} from "./x.js";</script>',
        ]
        for html in cases:
            with self.subTest(html=html):
                self.assertTrue(verifier.standalone_violations(html))

    def test_inline_assets_and_inert_import_mentions(self):
        html = """<style>/* @import "ignore.css"; */ circle {filter:url(#steam)}</style>
          <img src="data:image/svg+xml;base64,PHN2Zy8+">
          <svg><use href="#bowl"/></svg>
          <script>// import x from "ignore.js";
            const description = 'import("ignore.js")';
          </script>"""
        self.assertEqual(verifier.standalone_violations(html), [])

    def test_strict_judge_response(self):
        valid = {
            key: {"score": 0.5, "reason": "Test fixture evidence."}
            for key in verifier.JUDGMENT_KEYS
        }
        self.assertEqual(verifier.parse_judgment(json.dumps(valid)), valid)
        for bad_score in [True, -1, 1.01, float("nan"), float("inf"), "0.5"]:
            invalid = copy.deepcopy(valid)
            invalid["vegan"]["score"] = bad_score
            with self.subTest(score=bad_score), self.assertRaises(ValueError):
                verifier.parse_judgment(json.dumps(invalid))
        valid["bonus"] = {"score": 1, "reason": "Unexpected criterion"}
        with self.assertRaises(ValueError):
            verifier.parse_judgment(json.dumps(valid))

    def test_composite_and_sample_aggregation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / "index.html"
            artifact.write_text("<!doctype html><html><body>fixture</body></html>")
            first = {
                key: {"score": 0.25, "reason": "First test sample"}
                for key in verifier.JUDGMENT_KEYS
            }
            second = {
                key: {"score": 0.75, "reason": "Second test sample"}
                for key in verifier.JUDGMENT_KEYS
            }
            with (
                patch.object(verifier, "capture", side_effect=self.capture_fixture),
                patch.object(
                    verifier, "judge", return_value=([first, second], [{}, {}])
                ),
            ):
                result = verifier.grade(artifact, root / "out", "test/fixture", 2)
            self.assertEqual(result["reward"], 0.5)
            self.assertIsNone(result["rewardStdDev"])
            self.assertAlmostEqual(
                result["judgeResults"][0]["sampleStdDev"], 0.3535533905932738
            )
            workflow.validate_grade(result, artifact)
            self.assertEqual(
                json.loads((root / "out" / "reward.json").read_text())["reward"], 0.5
            )
            result["scores"]["vegan"] = 1
            with self.assertRaises(ValueError):
                workflow.validate_grade(result, artifact)

    def test_gate_never_calls_judge_or_invents_scores(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / "index.html"
            artifact.write_text('<script src="external.js"></script>')
            with (
                patch.object(verifier, "judge") as judge,
                patch.object(verifier, "capture") as capture,
            ):
                result = verifier.grade(artifact, root / "out", "test/fixture", 3)
            judge.assert_not_called()
            capture.assert_not_called()
            self.assertEqual(result["scores"], {"standalone": 0, "procedural": 1})
            self.assertEqual(result["reward"], 0)
            workflow.validate_grade(result, artifact)
            workflow.publish(
                [({"id": "test/fixture/low"}, result)], root / "scores.json"
            )
            workflow.audit(
                root / "scores.json", [({"id": "test/fixture/low"}, artifact)]
            )

    def test_failed_judge_removes_stale_reward(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / "index.html"
            artifact.write_text("<html>test fixture</html>")
            (root / "reward.json").write_text('{"reward": 1}')
            with (
                patch.object(verifier, "capture", side_effect=self.capture_fixture),
                patch.object(
                    verifier,
                    "judge",
                    side_effect=ValueError("Test fixture provider failure"),
                ),
                self.assertRaises(ValueError),
            ):
                verifier.grade(artifact, root, "test/fixture", 1)
            self.assertFalse((root / "reward.json").exists())
            self.assertEqual(
                json.loads((root / "grade.json").read_text())["status"], "error"
            )

    def test_stale_artifact_and_verifier_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / "index.html"
            artifact.write_text('<img src="external.png">')
            result = verifier.grade(artifact, root / "out", "test/fixture", 1)
            stale = {**result, "verifierSha256": "0" * 64}
            with self.assertRaises(ValueError):
                workflow.validate_grade(stale, artifact)
            artifact.write_text("<html>changed fixture</html>")
            with self.assertRaises(ValueError):
                workflow.validate_grade(result, artifact)

    def test_export_is_lossless_and_deterministic(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "replay"
            runs = workflow.registered_runs()
            workflow.export(destination, runs)
            mapping = (destination / "replay-map.json").read_bytes()
            snapshot = {
                path.relative_to(destination): path.read_bytes()
                for path in destination.rglob("*")
                if path.is_file()
            }
            for run, artifact in runs:
                exported = (
                    destination
                    / workflow.task_name(run["id"])
                    / "solution"
                    / "index.html"
                )
                self.assertEqual(exported.read_bytes(), artifact.read_bytes())
            workflow.export(destination, runs)
            self.assertEqual(mapping, (destination / "replay-map.json").read_bytes())
            self.assertEqual(
                snapshot,
                {
                    path.relative_to(destination): path.read_bytes()
                    for path in destination.rglob("*")
                    if path.is_file()
                },
            )

    def test_mixed_judges_rejected(self):
        with tempfile.TemporaryDirectory() as temporary, self.assertRaises(ValueError):
            workflow.publish(
                [
                    ({}, {"judge": {"models": ["test/a"], "samples": 1}}),
                    ({}, {"judge": {"models": ["test/b"], "samples": 1}}),
                ],
                Path(temporary) / "scores.json",
            )

    def test_ensemble_weights_judges_equally(self):
        def sample(score):
            return {
                key: {"score": score, "reason": "Test fixture evidence"}
                for key in verifier.JUDGMENT_KEYS
            }

        first = verifier.summarize_judge("test/a", [sample(0)] * 3)
        second = verifier.summarize_judge("test/b", [sample(1)])
        combined = verifier.summarize_ensemble([first, second])
        self.assertEqual(combined["reward"], 0.5)
        self.assertAlmostEqual(combined["rewardStdDev"], 0.7071067811865476)

    def test_ensemble_failure_is_not_silently_dropped(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / "index.html"
            artifact.write_text("<html>Test fixture</html>")
            samples = [
                {
                    key: {"score": 0.5, "reason": "Test fixture evidence"}
                    for key in verifier.JUDGMENT_KEYS
                }
            ]

            def fake_judge(rendering, output, model, count, effort):
                if model == "test/b":
                    raise ValueError("Test provider failed")
                return samples, [{}]

            with (
                patch.object(verifier, "capture", side_effect=self.capture_fixture),
                patch.object(verifier, "judge", side_effect=fake_judge),
                self.assertRaises(ValueError),
            ):
                verifier.grade(artifact, root / "out", "test/a,test/b", 1)
            self.assertFalse((root / "out" / "reward.json").exists())

    def test_raster_artwork_is_disqualified_without_paid_judging(self):
        artifact = workflow.ROOT / "openai/gpt-6-astra/xhigh/index.html"
        before = artifact.read_bytes()
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(verifier, "judge") as judge,
        ):
            result = verifier.grade(artifact, Path(temporary), "test/fixture", 1)
        judge.assert_not_called()
        self.assertEqual(result["scores"], {"standalone": 1, "procedural": 0})
        self.assertEqual(result["reward"], 0)
        self.assertEqual(before, artifact.read_bytes())
        self.assertEqual(
            verifier.procedural_violations(
                '<svg><circle r="5"/></svg><canvas></canvas>'
            ),
            [],
        )

    def test_visual_weights_and_majority_vegan_failure(self):
        self.assertAlmostEqual(sum(verifier.WEIGHTS.values()), 1)
        scores = dict.fromkeys(verifier.CRITERIA, 0)
        scores["toppings"] = 1
        self.assertEqual(verifier.composite(scores, True), 0.25)

        def sample(vegan):
            return {
                key: {
                    "score": vegan if key == "vegan" else 0.8,
                    "reason": "Visible test evidence",
                }
                for key in verifier.JUDGMENT_KEYS
            }

        judges = [
            verifier.summarize_judge(name, [sample(0)]) for name in ("test/a", "test/b")
        ]
        judges.append(verifier.summarize_judge("test/c", [sample(1)]))
        combined = verifier.summarize_ensemble(judges)
        self.assertEqual(combined["reward"], 0)
        self.assertTrue(combined["diagnostics"]["veganFailed"])

    def test_effort_is_sent_and_invalid_sample_is_retried(self):
        valid = {
            key: {"score": 0.8, "reason": "Concrete visual evidence"}
            for key in verifier.JUDGMENT_KEYS
        }

        def response(content):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
                usage=None,
            )

        model = "openrouter/test/vision"
        catalog = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "data": [
                    {
                        "id": "test/vision",
                        "architecture": {"input_modalities": ["image"]},
                        "reasoning": {"supported_efforts": ["low", "high"]},
                    }
                ]
            },
        )
        with (
            patch.dict(
                "sys.modules",
                {
                    "litellm": SimpleNamespace(completion=None),
                    "httpx": SimpleNamespace(get=lambda *args, **kwargs: catalog),
                },
            ),
            patch(
                "litellm.completion",
                side_effect=[response("[]"), response(json.dumps(valid))],
            ) as completion,
            tempfile.TemporaryDirectory() as temporary,
        ):
            rendering = {"screenshots": [], "pageErrors": [], "measurements": {}}
            judgments, usage = verifier.judge(
                rendering, Path(temporary), model, 1, "low"
            )
            self.assertEqual(judgments, [valid])
            self.assertEqual(len(usage), 2)
            kwargs = completion.call_args.kwargs
            self.assertEqual(
                kwargs["extra_body"]["reasoning"], {"effort": "low", "exclude": True}
            )
            self.assertEqual(kwargs["response_format"]["type"], "json_schema")
            with self.assertRaises(verifier.StageError):
                verifier.judge(rendering, Path(temporary), model, 1, "max")
            self.assertEqual(completion.call_count, 2)

    def test_capture_retry_preserves_metadata_order_on_resume(self):
        from reliability import StageError, retry

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / "index.html"
            artifact.write_text("<html>Test rendering</html>")
            calls = []

            def render(source, output):
                calls.append(source)
                if len(calls) == 1:
                    raise StageError("capture", "screenshot_timeout", retryable=True)
                fixture = self.capture_fixture(source, output)
                fixture["measurements"] = {"zFirst": 42, "aSecond": 16}
                return fixture

            with (
                patch.object(verifier, "capture", side_effect=render) as capture,
                patch.object(
                    verifier,
                    "retry",
                    side_effect=lambda op, **kw: retry(
                        op, sleep=lambda seconds: None, **kw
                    ),
                ),
            ):
                first = verifier.grade(
                    artifact, root / "out", "test/fixture", 1, capture_only=True
                )
                second = verifier.grade(
                    artifact, root / "out", "test/fixture", 1, capture_only=True
                )
            self.assertEqual(capture.call_count, 2)
            self.assertEqual(first["rendering"], second["rendering"])
            self.assertEqual(
                list(second["rendering"]["measurements"]), ["zFirst", "aSecond"]
            )

    def test_browser_operations_allow_complex_rendering_time(self):
        manager = MagicMock()
        browser = manager.__enter__.return_value.chromium.launch.return_value
        page = browser.new_context.return_value.new_page.return_value
        page.evaluate.side_effect = lambda expression: (
            [16] if "new Promise" in expression else 100
        )
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(
                "sys.modules",
                {
                    "playwright.sync_api": SimpleNamespace(
                        sync_playwright=lambda: manager
                    )
                },
            ),
        ):
            rendering = verifier.capture("<html>Fixture</html>", Path(temporary))
        self.assertEqual(len(rendering["screenshots"]), 8)
        self.assertEqual(verifier.CAPTURE_TIMEOUT_MS, 120_000)
        for call in page.goto.call_args_list + page.screenshot.call_args_list:
            self.assertEqual(call.kwargs["timeout"], 120_000)
        browser.close.assert_called_once()

    def test_judge_resumes_valid_samples_after_permanent_provider_failure(self):
        from reliability import StageError

        valid = {
            key: {"score": 0.8, "reason": "Recorded visual evidence"}
            for key in verifier.JUDGMENT_KEYS
        }
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content=json.dumps(valid)))
            ],
            usage=SimpleNamespace(model_dump=lambda: {"total_tokens": 123}),
        )
        rendering = {"screenshots": [], "pageErrors": [], "measurements": {}}
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict("sys.modules", {"litellm": SimpleNamespace(completion=None)}),
            patch(
                "litellm.completion",
                side_effect=[response, StageError("completion", "unauthorized")],
            ) as completion,
        ):
            output = Path(temporary)
            with self.assertRaises(StageError) as failure:
                verifier.judge(rendering, output, "test/fixture", 2)
            self.assertEqual(failure.exception.code, "unauthorized")
            self.assertEqual(completion.call_count, 2)
            completion.reset_mock(side_effect=True)
            completion.return_value = response
            judgments, usages = verifier.judge(rendering, output, "test/fixture", 2)
            self.assertEqual(completion.call_count, 1)
            self.assertEqual(judgments, [valid, valid])
            self.assertEqual([u["total_tokens"] for u in usages], [123, 123])
            completion.reset_mock()
            self.assertEqual(
                verifier.judge(rendering, output, "test/fixture", 2),
                (judgments, usages),
            )
            completion.assert_not_called()


if __name__ == "__main__":
    unittest.main()
