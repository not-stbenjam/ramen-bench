"""Private verifier checkpoints and safe, bounded operation retries.

These helpers never publish a reward. A checkpoint is reusable only for an exact
request, and an ensemble is ready only when every requested judge is complete.
Checkpoint digests detect accidental corruption, not malicious local rewriting.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
from pathlib import Path

STAGES = {
    "config",
    "capture",
    "catalog",
    "completion",
    "parse",
    "checkpoint",
    "aggregate",
}
CODES = {
    "operation_failed",
    "timeout",
    "screenshot_timeout",
    "connection_error",
    "rate_limited",
    "provider_unavailable",
    "unauthorized",
    "forbidden",
    "unsupported_model",
    "invalid_config",
    "invalid_response",
    "checkpoint_integrity",
    "checkpoint_invalid",
    "checkpoint_write_failed",
    "checkpoint_read_failed",
    "incomplete_ensemble",
}
TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
PERMANENT_CODES = {"unauthorized", "forbidden", "unsupported_model", "invalid_config"}


class StageError(RuntimeError):
    """Only fixed codes and numeric metadata; never an exception's provider text."""

    def __init__(
        self,
        stage,
        code,
        *,
        retryable=False,
        status_code=None,
        sample=None,
        attempt=None,
    ):
        if stage not in STAGES or code not in CODES:
            raise ValueError("Unknown safe error stage or code")
        for name, value in (
            ("status_code", status_code),
            ("sample", sample),
            ("attempt", attempt),
        ):
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError(f"Invalid numeric error metadata: {name}")
        self.stage, self.code = stage, code
        self.retryable = (
            bool(retryable)
            and code not in PERMANENT_CODES
            and status_code not in {401, 403}
        )
        self.status_code, self.sample, self.attempt = status_code, sample, attempt
        super().__init__(f"{stage}: {code}")

    def metadata(self):
        result = {"stage": self.stage, "code": self.code, "retryable": self.retryable}
        for name in ("status_code", "sample", "attempt"):
            if (value := getattr(self, name)) is not None:
                result[name] = value
        return result


def safe_error(error, stage, *, sample=None, attempt=None):
    """Classify status/type only. Do not inspect or retain exception messages."""
    if isinstance(error, StageError):
        return StageError(
            error.stage,
            error.code,
            retryable=error.retryable,
            status_code=error.status_code,
            sample=sample if sample is not None else error.sample,
            attempt=attempt if attempt is not None else error.attempt,
        )
    status = getattr(error, "status_code", None)
    if type(status) is not int:
        status = getattr(getattr(error, "response", None), "status_code", None)
    if type(status) is not int or not 100 <= status <= 599:
        status = None
    name = type(error).__name__
    if status == 401:
        code = "unauthorized"
    elif status == 403:
        code = "forbidden"
    elif status == 429 or name == "RateLimitError":
        code = "rate_limited"
    elif status is not None and (status in TRANSIENT_STATUS or 500 <= status <= 599):
        code = "provider_unavailable"
    elif status is not None:
        code = "operation_failed"
    elif isinstance(error, TimeoutError) or name in {
        "TimeoutError",
        "APITimeoutError",
        "ReadTimeout",
        "ConnectTimeout",
        "TimeoutException",
    }:
        code = "timeout"
    elif isinstance(error, ConnectionError) or name in {
        "APIConnectionError",
        "ConnectError",
        "NetworkError",
    }:
        code = "connection_error"
    elif name in {"ServiceUnavailableError", "InternalServerError"}:
        code = "provider_unavailable"
    elif stage == "parse" and isinstance(error, (ValueError, TypeError)):
        code = "invalid_response"
    elif stage == "config":
        code = "invalid_config"
    else:
        code = "operation_failed"
    retryable = code in {
        "rate_limited",
        "provider_unavailable",
        "timeout",
        "connection_error",
        "invalid_response",
    }
    return StageError(
        stage,
        code,
        retryable=retryable,
        status_code=status,
        sample=sample,
        attempt=attempt,
    )


def numeric_usage(value):
    """Retain recorded numeric usage, including nested token detail objects.

    Text and arbitrary payloads are never copied from a failed provider response.
    No missing counts or costs are estimated or filled in.
    """
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, item in value.items():
        if not isinstance(key, str) or not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_]{0,63}", key
        ):
            continue
        if any(
            word in key.lower()
            for word in ("key", "secret", "credential", "authorization", "payload")
        ):
            continue
        if (
            item is None
            or type(item) is bool
            or (type(item) in (int, float) and math.isfinite(item) and item >= 0)
        ):
            result[key] = item
        elif isinstance(item, dict) and (nested := numeric_usage(item)):
            result[key] = nested
    return result


def retry(
    operation,
    *,
    stage,
    max_attempts=3,
    base_delay=0.5,
    max_delay=5.0,
    deadline_seconds=None,
    sample=None,
    clock=time.monotonic,
    sleep=time.sleep,
    on_failure=None,
):
    """Retry known transient errors; callback receives safe error and numeric usage.

    The deadline bounds scheduling retries, not the duration of an operation;
    each network/browser operation must also set its own timeout. The callback
    can durably record failed-attempt usage before a retry or terminal failure.
    """
    if (
        stage not in STAGES
        or type(max_attempts) is not int
        or not 1 <= max_attempts <= 20
    ):
        raise StageError("config", "invalid_config")
    for value in (base_delay, max_delay, deadline_seconds):
        if value is not None and (
            type(value) not in (int, float) or not math.isfinite(value) or value < 0
        ):
            raise StageError("config", "invalid_config")
    started = clock()
    for attempt in range(1, max_attempts + 1):
        try:
            return operation()
        except Exception as original:  # noqa: BLE001 - sanitize arbitrary provider exceptions at this boundary.
            error = safe_error(original, stage, sample=sample, attempt=attempt)
            usage = getattr(original, "usage", None)
            if usage is None:
                usage = getattr(getattr(original, "response", None), "usage", None)
            if hasattr(usage, "model_dump"):
                usage = usage.model_dump()
            if on_failure is not None:
                on_failure(error, numeric_usage(usage))
            delay = min(max_delay, base_delay * 2 ** (attempt - 1))
            if (
                not error.retryable
                or attempt == max_attempts
                or (
                    deadline_seconds is not None
                    and clock() - started + delay > deadline_seconds
                )
            ):
                raise error from None
            sleep(delay)


def _bytes(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def capture_key(artifact_bytes, policy):
    """Policy must include renderer version, layouts, seed, and timing policy."""
    return _digest(
        _bytes({"artifactSha256": _digest(artifact_bytes), "policy": policy})
    )


def judge_key(capture_identity, model, effort, prompt, options):
    """Options include exact request schema, token limit, temperature, routing, etc."""
    return _digest(
        _bytes(
            {
                "capture": capture_identity,
                "model": model,
                "effort": effort,
                "prompt": prompt,
                "options": options,
            }
        )
    )


def _write(path, kind, key, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"schemaVersion": 1, "kind": kind, "key": key, "payload": payload}
    envelope = {**body, "sha256": _digest(_bytes(body))}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as stream:
            temporary = Path(stream.name)
            # Preserve capture object order: its JSON text is part of the exact
            # judge message. Only checksum computation canonicalizes key order.
            stream.write(
                json.dumps(
                    envelope,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode()
                + b"\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        raise StageError("checkpoint", "checkpoint_write_failed") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _read(path, kind, key):
    path = Path(path)
    if not path.exists():
        return None
    try:
        envelope = json.loads(path.read_bytes())
        if not isinstance(envelope, dict) or set(envelope) != {
            "schemaVersion",
            "kind",
            "key",
            "payload",
            "sha256",
        }:
            raise ValueError
        body = {
            name: envelope[name] for name in ("schemaVersion", "kind", "key", "payload")
        }
        if (
            envelope["sha256"] != _digest(_bytes(body))
            or body["schemaVersion"] != 1
            or body["kind"] != kind
        ):
            raise ValueError
        return body["payload"] if body["key"] == key else None
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise StageError("checkpoint", "checkpoint_integrity") from None
    except OSError:
        raise StageError("checkpoint", "checkpoint_read_failed") from None


def _frames(rendering, frame_root):
    result = []
    files = set()
    for frame in rendering["screenshots"]:
        name = frame["file"]
        if (
            not isinstance(name, str)
            or not name
            or Path(name).name != name
            or name in {".", ".."}
            or name in files
        ):
            raise ValueError
        path = Path(frame_root) / name
        if path.is_symlink():
            raise ValueError
        result.append({"file": name, "sha256": _digest(path.read_bytes())})
        files.add(name)
    if not result:
        raise ValueError
    return result


def save_capture(path, artifact_bytes, policy, rendering, frame_root):
    """Store exact capture metadata and hashes; existing PNG bytes stay in place."""
    try:
        payload = {"rendering": rendering, "frames": _frames(rendering, frame_root)}
        payload["identity"] = _digest(
            _bytes({"captureKey": capture_key(artifact_bytes, policy), **payload})
        )
        _write(path, "capture", capture_key(artifact_bytes, policy), payload)
        return payload
    except (ValueError, TypeError, KeyError, OSError):
        raise StageError("checkpoint", "checkpoint_invalid") from None


def load_capture(path, artifact_bytes, policy, frame_root):
    key = capture_key(artifact_bytes, policy)
    payload = _read(path, "capture", key)
    if payload is None:
        return None
    try:
        if not isinstance(payload, dict) or set(payload) != {
            "rendering",
            "frames",
            "identity",
        }:
            raise ValueError
        frames = _frames(payload["rendering"], frame_root)
        identity = _digest(
            _bytes(
                {"captureKey": key, "rendering": payload["rendering"], "frames": frames}
            )
        )
        if frames != payload["frames"] or identity != payload["identity"]:
            raise ValueError
    except (ValueError, TypeError, KeyError, OSError):
        raise StageError("checkpoint", "checkpoint_integrity") from None
    return payload


def _validate_judge(payload, samples, validate_judgment):
    if (
        type(samples) is not int
        or samples < 1
        or not isinstance(payload, dict)
        or set(payload) != {"samples", "judgments", "usages"}
    ):
        raise ValueError
    if payload["samples"] != samples:
        raise ValueError
    judgments, usages = payload["judgments"], payload["usages"]
    if (
        not isinstance(judgments, list)
        or len(judgments) > samples
        or not isinstance(usages, list)
    ):
        raise ValueError
    for judgment in judgments:
        if not isinstance(judgment, dict):
            raise TypeError
        if validate_judgment is not None:
            validate_judgment(judgment)
    seen = set()
    for usage in usages:
        if not isinstance(usage, dict) or numeric_usage(usage) != usage:
            raise ValueError
        sample, attempt = usage.get("sample"), usage.get("attempt")
        if (
            type(sample) is not int
            or not 1 <= sample <= min(samples, len(judgments) + 1)
            or type(attempt) is not int
            or attempt < 1
        ):
            raise ValueError
        if (sample, attempt) in seen:
            raise ValueError
        seen.add((sample, attempt))


def save_judge(path, key, judgments, usages, samples, *, validate_judgment=None):
    """Persist the completed sample prefix plus real usage from all attempts.

    A failed next sample can have usage even though it has no valid judgment.
    No usage record is synthesized when the provider omitted one.
    """
    payload = {"samples": samples, "judgments": judgments, "usages": usages}
    try:
        _validate_judge(payload, samples, validate_judgment)
        _write(path, "judge", key, payload)
    except (ValueError, TypeError, KeyError):
        raise StageError("checkpoint", "checkpoint_invalid") from None


def load_judge(path, key, samples, *, validate_judgment=None):
    payload = _read(path, "judge", key)
    if payload is None:
        return None
    if isinstance(payload, dict) and payload.get("samples") != samples:
        return None
    try:
        _validate_judge(payload, samples, validate_judgment)
    except (ValueError, TypeError, KeyError):
        raise StageError("checkpoint", "checkpoint_integrity") from None
    return payload


def ready_ensemble(checkpoints, expected_keys, samples, *, validate_judgment=None):
    """Return authentic judgments only for an exact, fully completed judge set."""
    if not expected_keys or set(checkpoints) != set(expected_keys):
        raise StageError("aggregate", "incomplete_ensemble")
    for model, key in expected_keys.items():
        checkpoint = checkpoints[model]
        if not isinstance(checkpoint, dict) or checkpoint.get("key") != key:
            raise StageError("aggregate", "incomplete_ensemble")
        payload = checkpoint.get("payload")
        try:
            _validate_judge(payload, samples, validate_judgment)
            if len(payload["judgments"]) != samples:
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise StageError("aggregate", "incomplete_ensemble") from None
    return {
        model: checkpoint["payload"]["judgments"]
        for model, checkpoint in checkpoints.items()
    }
