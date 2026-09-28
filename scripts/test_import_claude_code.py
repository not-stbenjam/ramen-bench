"""Synthetic fixtures for Claude session import edge cases."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backfill_session_metadata import (
    BENCHMARK_PROMPTS,
    benchmark_claude_rows,
    claude_usage,
    public_user_text,
)


class ImportClaudeCodeTests(unittest.TestCase):
    def test_preserves_both_recorded_prompt_versions(self):
        for prompt in BENCHMARK_PROMPTS:
            with self.subTest(prompt=prompt):
                user = {"type": "user", "message": {"content": prompt}}
                self.assertEqual(benchmark_claude_rows([{"type": "system"}, user]), [user])
                self.assertEqual(public_user_text(prompt), prompt)

    def test_unknown_model_pricing_does_not_publish_fallback_price(self):
        for unknown in (False, True):
            with self.subTest(unknown=unknown):
                rows = [
                    {
                        "type": "assistant",
                        "message": {"id": "fixture", "model": "claude-sonnet-5-5"},
                    },
                    {
                        "type": "cost-state",
                        "totalCostUSD": 1.25,
                        "hasUnknownModelCost": unknown,
                        "modelUsage": {
                            "claude-sonnet-5-5": {
                                "inputTokens": 10,
                                "outputTokens": 20,
                                "thinkingTokens": 5,
                                "cacheReadInputTokens": 30,
                                "cacheCreationInputTokens": 40,
                            },
                            "auxiliary-model": {"inputTokens": 2, "outputTokens": 3},
                        },
                    },
                ]
                usage, model, cost_kind = claude_usage(rows)
                self.assertEqual(model, "claude-sonnet-5-5")
                self.assertEqual(cost_kind, "unknown" if unknown else "provider")
                self.assertEqual(usage["cost"]["total"], None if unknown else 1.25)
                self.assertEqual(usage["tokens"]["total"], 105)
                self.assertEqual(usage["tokens"]["reasoning"], 5)
                self.assertEqual(usage["raw"]["costState"]["totalCostUSD"], 1.25)


if __name__ == "__main__":
    unittest.main()
