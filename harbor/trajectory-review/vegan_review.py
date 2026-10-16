"""Native trajectory-aware vegan review; original visual judgments stay immutable."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
FILES = ("vegan_review.py", "policy.json", "Dockerfile", "requirements.txt", "test.sh")
POLICY = json.loads((HERE / "policy.json").read_text())
DECISIONS = ("plant_based_substitutes", "animal_ingredients", "insufficient_evidence")
INGREDIENT = re.compile(
    r"\b(?:vegan|plant[- ]based|plant[- ]curd|egg|eggs|yolk|ajitama|tamago|albumen|"
    r"tofu|mushroom|chashu|pork|meat|beef|chicken|fish|shrimp|seafood|bonito|bacon|"
    r"dairy|cheese|milk|seitan|aquafaba|pumpkin|saffron)\b",
    re.IGNORECASE,
)


def canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def fingerprint() -> str:
    return digest(
        b"".join(name.encode() + b"\0" + (HERE / name).read_bytes() for name in FILES)
    )


def contexts(text: str):
    """Return exact character spans around ingredient terms, without summaries."""
    spans = []
    for match in INGREDIENT.finditer(text):
        start, end = max(0, match.start() - 200), min(len(text), match.end() + 200)
        if spans and start <= spans[-1][1]:
            spans[-1][1] = end
        else:
            spans.append([start, end])
    for start, end in spans:
        # Keep individual contexts bounded even for dense minified source.
        for offset in range(start, end, 4000):
            yield (
                offset,
                min(offset + 4000, end),
                text[offset : min(offset + 4000, end)],
            )


def strings(value: object, path: str):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from strings(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from strings(item, f"{path}.{index}")


def build_input(
    run: dict, artifact: bytes, transcript: bytes, score: dict, judge: dict
) -> dict:
    public = json.loads(transcript)
    if public["runId"] != run["id"] or digest(artifact) != score["artifactSha256"]:
        raise ValueError("Review sources do not match the scored run")
    if not score.get("diagnostics", {}).get("veganFailed"):
        raise ValueError("Review requires an existing vegan-compliance failure")
    if score["scores"].get("standalone") != 1 or score["scores"].get("procedural") != 1:
        raise ValueError("Trajectory review cannot clear other compliance failures")
    candidates = []
    # Visible assistant messages take priority; user instructions are not evidence
    # that the generator actually intended plant-based substitutes.
    for index, event in enumerate(public["events"]):
        if event.get("role") == "assistant" and event.get("type") == "message":
            for start in range(0, len(event["content"]), 8000):
                candidates.append(
                    {
                        "id": f"trajectory.events.{index}.content:{start}",
                        "source": "trajectory",
                        "eventIndex": index,
                        "text": event["content"][start : start + 8000],
                    }
                )
    for start, end, text in contexts(artifact.decode()):
        candidates.append(
            {"id": f"artifact:{start}-{end}", "source": "artifact", "text": text}
        )
    for index, event in enumerate(public["events"]):
        field = (
            "input"
            if event["type"] == "tool_call"
            else "output"
            if event["type"] == "tool_result"
            else None
        )
        if field is None:
            continue
        for path, value in strings(event.get(field), field):
            for start, end, text in contexts(value):
                candidates.append(
                    {
                        "id": f"trajectory.events.{index}.{path}:{start}-{end}",
                        "source": "trajectory",
                        "eventIndex": index,
                        "text": text,
                    }
                )
    evidence, seen, size, omitted = [], set(), 0, 0
    for item in candidates:
        key = digest(item["text"].encode())
        if key in seen:
            continue
        seen.add(key)
        if size + len(item["text"]) > 100_000:
            omitted += 1
            continue
        evidence.append(
            {**item, "location": item["id"], "id": f"E{len(evidence) + 1:04d}"}
        )
        size += len(item["text"])
    return {
        "schemaVersion": 1,
        "runId": run["id"],
        "artifactSha256": digest(artifact),
        "transcriptSha256": digest(transcript),
        "sourceScoreSha256": digest(canonical(score)),
        "sourceReward": score["reward"],
        "policySha256": fingerprint(),
        "policyVersion": POLICY["version"],
        "judge": {key: judge[key] for key in ("models", "efforts", "samples")},
        "visualVeganReasons": [
            {
                "model": item["model"],
                "reasons": [s["vegan"]["reason"] for s in item["judgments"]],
            }
            for item in score["judgeResults"]
        ],
        "evidence": evidence,
        "omittedEvidenceChunks": omitted,
    }


def parse_judgment(value: str, bundle: dict) -> dict:
    result = json.loads(value)
    if not isinstance(result, dict) or set(result) != {
        "decision",
        "reason",
        "evidence",
    }:
        raise ValueError("Invalid trajectory-review response fields")
    if (
        result["decision"] not in DECISIONS
        or not isinstance(result["reason"], str)
        or not result["reason"].strip()
    ):
        raise ValueError("Invalid trajectory-review decision or reason")
    if not isinstance(result["evidence"], list) or len(result["evidence"]) > 8:
        raise ValueError("Invalid trajectory-review evidence")
    known = {item["id"]: item["text"] for item in bundle["evidence"]}
    for citation in result["evidence"]:
        if (
            not isinstance(citation, dict)
            or set(citation) != {"id"}
            or not isinstance(citation["id"], str)
            or citation["id"] not in known
        ):
            raise ValueError("Trajectory-review citation is not recorded evidence")
    if result["decision"] == "plant_based_substitutes" and not result["evidence"]:
        raise ValueError("Clearing the vegan gate requires cited generator evidence")
    return result


def summarize(results: list[dict], judge: dict, bundle: dict) -> bool:
    if (
        not judge["models"]
        or len(set(judge["models"])) != len(judge["models"])
        or {r["model"] for r in results} != set(judge["models"])
        or len(results) != len(judge["models"])
    ):
        raise ValueError("Review requires every distinct configured judge")
    votes = []
    for result in results:
        if (
            result["effort"] != judge["efforts"][result["model"]]
            or len(result["judgments"]) != judge["samples"]
        ):
            raise ValueError("Incomplete trajectory-review ensemble")
        for sample in result["judgments"]:
            parse_judgment(json.dumps(sample["judgment"]), bundle)
        votes.append(
            sum(
                s["judgment"]["decision"] == "plant_based_substitutes"
                for s in result["judgments"]
            )
            > judge["samples"] / 2
        )
    return sum(votes) > len(votes) / 2


def citation_sources(bundle: dict, results: list[dict]) -> dict:
    cited = {
        citation["id"]
        for result in results
        for sample in result["judgments"]
        for citation in sample["judgment"]["evidence"]
    }
    return {
        item["id"]: {
            key: item[key]
            for key in ("source", "location", "eventIndex", "text")
            if key in item
        }
        for item in bundle["evidence"]
        if item["id"] in cited
    }


def request(bundle: dict, model: str, effort: str) -> dict:
    options = {}
    if model.startswith("openrouter/"):
        reasoning = {"exclude": True}
        if effort != "default":
            reasoning["effort"] = effort
        options["extra_body"] = {
            "provider": {"require_parameters": True},
            "reasoning": reasoning,
        }
    elif effort != "default":
        options["reasoning_effort"] = effort
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["decision", "reason", "evidence"],
        "properties": {
            "decision": {"type": "string", "enum": list(DECISIONS)},
            "reason": {"type": "string", "minLength": 1},
            "evidence": {
                "type": "array",
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["id"],
                    "properties": {
                        "id": {
                            "type": "string",
                            "enum": [item["id"] for item in bundle["evidence"]],
                        },
                    },
                },
            },
        },
    }
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": POLICY["prompt"]},
            {"role": "user", "content": canonical(bundle).decode()},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "vegan_review", "strict": True, "schema": schema},
        },
        "max_tokens": 8192,
        "timeout": 240,
        "num_retries": 0,
        **options,
    }


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    )
    temporary.replace(path)


def judge_model(bundle: dict, output: Path, model: str) -> dict:
    from litellm import completion

    config = bundle["judge"]
    effort = config["efforts"][model]
    kwargs = request(bundle, model, effort)
    key = digest(canonical(kwargs))
    samples = []
    for index in range(config["samples"]):
        checkpoint = output / f"review-{key[:20]}-{index}.json"
        if checkpoint.exists():
            sample = json.loads(checkpoint.read_text())
            if sample["requestSha256"] != key:
                raise ValueError("Review checkpoint request changed")
            parse_judgment(json.dumps(sample["judgment"]), bundle)
        else:
            invalid = []
            for attempt in range(3):
                response_path = output / f"response-{key[:20]}-{index}-{attempt}.json"
                if response_path.exists():
                    recorded = json.loads(response_path.read_text())
                    if recorded["requestSha256"] != key:
                        raise ValueError("Saved review response request changed")
                else:
                    response = completion(**kwargs)
                    recorded = {
                        "requestSha256": key,
                        "content": response.choices[0].message.content,
                        "usage": response.usage.model_dump(exclude_none=True)
                        if response.usage
                        else None,
                    }
                    write_json(response_path, recorded)
                try:
                    judgment = parse_judgment(recorded["content"], bundle)
                except (ValueError, TypeError):
                    invalid.append(
                        {
                            "requestSha256": key,
                            "error": "invalid_format_or_citation",
                            "usage": recorded["usage"],
                        }
                    )
                    if attempt == 2:
                        raise ValueError(
                            "Three invalid review formats or citations; no review published"
                        ) from None
                    continue
                sample = {
                    "requestSha256": key,
                    "judgment": judgment,
                    "usage": recorded["usage"],
                    "invalidAttempts": invalid,
                }
                break
            write_json(checkpoint, sample)
        samples.append(sample)
    return {"model": model, "effort": effort, "judgments": samples}


def judge(bundle: dict, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    (output / "reward.json").unlink(missing_ok=True)
    if bundle["policySha256"] != fingerprint():
        raise ValueError("Trajectory-review policy or implementation changed")
    with ThreadPoolExecutor(max_workers=len(bundle["judge"]["models"])) as pool:
        futures = [
            pool.submit(judge_model, bundle, output, model)
            for model in bundle["judge"]["models"]
        ]
        results = [future.result() for future in futures]
    restored = summarize(results, bundle["judge"], bundle)
    review = {
        key: bundle[key]
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
        inputSha256=digest(canonical(bundle)),
        reviewedAt=datetime.now(UTC).isoformat(),
        judgeResults=results,
        restoreVeganGate=restored,
        evidenceSources=citation_sources(bundle, results),
    )
    write_json(output / "review.json", review)
    write_json(output / "reward.json", {"restore_vegan_gate": float(restored)})
    return review


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("/app/input.json"))
    parser.add_argument("--output", type=Path, default=Path("/logs/verifier"))
    args = parser.parse_args()
    completed = judge(json.loads(args.input.read_text()), args.output)
    print(
        json.dumps(
            {
                "runId": completed["runId"],
                "restoreVeganGate": completed["restoreVeganGate"],
            }
        )
    )
