"""Pinned Google Standard API-equivalent rates for recorded Argon usage."""

from datetime import date
from decimal import Decimal

SOURCE = "https://ai.google.dev/gemini-api/docs/pricing#gemini-4-argon"
AS_OF = "2026-10-15"


def argon_list_cost(usage: dict, completed_at: str) -> tuple[dict, dict]:
    """Price recorded categories once; callers retain unknown failed-request usage."""
    day = date.fromisoformat(completed_at[:10])
    if day < date(2026, 10, 15):
        raise ValueError("Argon run predates this pinned rate snapshot")
    introductory = day <= date(2026, 11, 30)
    rates = ("2", "0.10", "10") if introductory else ("4", "0.20", "20")
    amounts = {}
    for field, category, rate in zip(
        ("input", "cachedInput", "output"),
        ("input_tokens", "cache_read_tokens", "output_tokens"),
        rates,
        strict=True,
    ):
        count = usage[category]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("Invalid recorded Argon token count")
        amounts[field] = Decimal(count) * Decimal(rate) / 1_000_000
    cost = {
        "currency": "USD",
        **{key: float(value) for key, value in amounts.items()},
        "cacheCreationInput": 0,
        "other": 0,
        "total": float(sum(amounts.values())),
        "estimated": True,
    }
    provenance = {
        "source": SOURCE,
        "pinnedAt": AS_OF,
        "model": "gemini-4-argon",
        "tier": "Standard",
        "basis": "API-equivalent published list prices; not a subscription charge",
        "introductory": introductory,
        "effectiveThrough": "2026-11-30" if introductory else None,
        "inputUsdPerMillion": float(rates[0]),
        "cachedInputUsdPerMillion": float(rates[1]),
        "outputUsdPerMillion": float(rates[2]),
        "outputIncludesThinking": True,
        "cacheType": "implicit",
    }
    return cost, provenance
