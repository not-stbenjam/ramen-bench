"""Estimate Wonder usage at pinned Meta Standard list prices."""

from decimal import Decimal

SOURCE = "https://dev.meta.ai/docs/pricing-rate-limits"
REASONING_SOURCE = "https://dev.meta.ai/docs/reasoning"
AS_OF = "2026-10-12"
MODEL = "muse-wonder-1.0"
RATES = {"input": 4, "cachedInput": 0.2, "output": 20}


def wonder_list_cost(exports: list[dict]) -> tuple[dict, dict] | None:
    totals = {"input_tokens": 0, "cache_read_tokens": 0, "output_tokens": 0}
    requests = 0
    for export in exports:
        for record in export["events"]:
            event = record.get("envelope", {}).get("payload", {}).get("event", {})
            # Hosted search/image charges cannot be inferred from token totals.
            if event.get("kind") == "assistant_tool_calls_committed" and any(
                "search" in call["name"].lower() or "image" in call["name"].lower()
                for call in event["tool_calls"]
            ):
                return None
            if event.get("kind") != "model_completed":
                continue
            if event.get("model") != MODEL:
                return None
            usage = event["usage"]
            keys = (*totals, "cached_tokens", "cache_write_tokens", "reasoning_tokens")
            if any(
                isinstance(usage.get(key), bool)
                or not isinstance(usage.get(key), int)
                or usage[key] < 0
                for key in keys
            ):
                return None
            if (
                usage["input_tokens"] < usage["cache_read_tokens"]
                or usage["cached_tokens"] != usage["cache_read_tokens"]
                or usage["cache_write_tokens"] != 0
                or usage["reasoning_tokens"] > usage["output_tokens"]
            ):
                return None
            for key in totals:
                totals[key] += usage[key]
            requests += 1
    if not requests:
        return None
    million = Decimal(1_000_000)
    charges = {
        "input": Decimal(totals["input_tokens"] - totals["cache_read_tokens"])
        * Decimal(str(RATES["input"]))
        / million,
        "cachedInput": Decimal(totals["cache_read_tokens"])
        * Decimal(str(RATES["cachedInput"]))
        / million,
        # Muse's native output total already includes reasoning: charge it once.
        "output": Decimal(totals["output_tokens"])
        * Decimal(str(RATES["output"]))
        / million,
    }
    cost = {
        "currency": "USD",
        **{key: float(value) for key, value in charges.items()},
        "total": float(sum(charges.values())),
        "estimated": True,
    }
    evidence = {
        "source": SOURCE,
        "reasoningSource": REASONING_SOURCE,
        "asOf": AS_OF,
        "model": MODEL,
        "mode": "Standard",
        "usdPerMillionTokens": RATES,
        "reasoningIncludedInOutput": True,
        "includesAuxiliarySessions": True,
        "pricedRequests": requests,
    }
    return cost, evidence
