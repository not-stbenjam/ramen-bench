"""Price legacy Sonnet 5.5 accounting at pinned official list rates."""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

SOURCE = "https://platform.claude.com/docs/en/models/sonnet-5-5/overview"
AS_OF = "2026-09-30"
RATES = {"input": 2, "output": 10, "cacheRead": 0.2, "cacheWrite5m": 2.5, "cacheWrite1h": 4}


def sonnet_list_cost(messages: list[dict], state: dict) -> tuple[dict, dict] | None:
    models = state.get("modelUsage", {})
    main = models.get("claude-sonnet-5-5")
    if not main:
        return None
    # Auxiliary Haiku accounting is already recorded by the harness. Do not
    # treat a globally unknown-model flag as proof of another model's price.
    other = Decimal(0)
    for model, usage in models.items():
        if model == "claude-sonnet-5-5":
            continue
        value = usage.get("costUSD")
        if model != "claude-haiku-4-5-20251001" or not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
            return None
        other += Decimal(str(value))
    for key in ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens"):
        value = main.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
    if main.get("webSearchRequests", 0):
        return None
    five = hour = 0
    for row in messages:
        message = row.get("message", {})
        if message.get("model") != "claude-sonnet-5-5":
            continue
        usage = message.get("usage", {})
        split = usage.get("cache_creation")
        if not isinstance(split, dict):
            continue
        values = [split.get("ephemeral_5m_input_tokens"), split.get("ephemeral_1h_input_tokens")]
        if any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in values):
            return None
        if sum(values) != usage.get("cache_creation_input_tokens"):
            raise ValueError("Recorded cache duration categories do not match cache writes")
        five += values[0]
        hour += values[1]
    unclassified = main["cacheCreationInputTokens"] - five - hour
    if unclassified < 0:
        raise ValueError("Recorded cache writes exceed cumulative harness accounting")
    # Some auxiliary/retried requests have cumulative usage but no public
    # response snapshot. Use the upper tariff bound for those cache writes,
    # disclose the range, and never invent a duration for the missing requests.
    million = Decimal(1_000_000)
    costs = {
        "input": Decimal(main["inputTokens"]) * Decimal(str(RATES["input"])) / million,
        "output": Decimal(main["outputTokens"]) * Decimal(str(RATES["output"])) / million,
        "cachedInput": Decimal(main["cacheReadInputTokens"]) * Decimal(str(RATES["cacheRead"])) / million,
        "cacheCreationInput": (Decimal(five) * Decimal(str(RATES["cacheWrite5m"])) + Decimal(hour + unclassified) * Decimal(str(RATES["cacheWrite1h"]))) / million,
        "other": other,
    }
    total = sum(costs.values())
    cache_range = Decimal(unclassified) * (Decimal(str(RATES["cacheWrite1h"])) - Decimal(str(RATES["cacheWrite5m"]))) / million
    cost = {"currency": "USD", **{k: float(v) for k, v in costs.items()}, "total": float(total), "estimated": True}
    evidence: dict[str, Any] = {
        "source": SOURCE, "asOf": AS_OF, "usdPerMillionTokens": RATES,
        "cacheWrite5mTokens": five, "cacheWrite1hTokens": hour,
        "cacheWriteUnclassifiedTokens": unclassified,
        "unclassifiedCacheWriteTreatment": "upper tariff bound; duration unknown",
        "totalRangeUsd": [float(total - cache_range), float(total)],
        "auxiliaryCostSource": "recorded Claude Code Haiku costUSD",
        "thinkingIncludedInOutput": True,
    }
    return cost, evidence
