#!/usr/bin/env python3
"""Censor public transcript JSON consistently across every harness importer.

Importers call :func:`censor_text` or :func:`censor_value` before writing public
events. Run this file directly as a final, idempotent audit or with ``--write``
to normalize existing JSON files.
"""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import re
import sys
import tempfile
from collections import Counter
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import quote


REPO_ROOT = Path(__file__).resolve().parents[1]
HOME_ROOT = Path.home()
CURRENT_USER = getpass.getuser() or HOME_ROOT.name

EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
PRIVATE_IPV6_RE = re.compile(
    r"(?i)(?<![0-9a-f:])(?:::1|fe80(?::[0-9a-f]{0,4}){2,}|"
    r"f[cd][0-9a-f]{2}(?::[0-9a-f]{0,4}){2,})(?![0-9a-f:])"
)
DATA_URI_BASE64_RE = re.compile(
    r"(?i)data:(?P<media>[a-z0-9.+-]+/[a-z0-9.+-]+)"
    r"(?:;[a-z0-9.+_-]+=[^;,\s\"'<>)]*)*;base64,"
    r"(?P<payload>[A-Za-z0-9+/]+={0,2})"
)
DATA_URI_IMAGE_RE = re.compile(
    r"(?i)data:(?P<media>image/[a-z0-9.+-]+)(?:;[^,\s\"'<>)]*)?,"
    r"(?P<payload>[^\s\"'<>)]{4096,})"
)
LARGE_BASE64_RE = re.compile(
    r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{4096,}={0,2}(?![A-Za-z0-9+/=])"
)
FERNET_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_-])gAAAAA[A-Za-z0-9_-]{250,}={0,2}(?![A-Za-z0-9_=-])"
)
TRUNCATED_BINARY_TAIL_RE = re.compile(
    r'(?P<marker><OMITTED_BINARY_PAYLOAD\s+[^>]+>)'
    r'(?P<truncation>(?:…|\.\.\.)\s*\d+\s+tokens?\s+truncated\s*(?:…|\.\.\.))'
    r'(?P<tail>[A-Za-z0-9+/=\r\n]{128,})',
    re.IGNORECASE,
)
SENSITIVE_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "auth_token",
    "authorization",
    "bearer",
    "capability",
    "client_secret",
    "credential",
    "password",
    "passwd",
    "private_key",
    "refresh_token",
    "secret",
}
HIDDEN_REASONING_KEYS = {
    "chain_of_thought",
    "encrypted_content",
    "internal_reasoning",
    "reasoning_content",
    "thinking_content",
    "thinking",
    "thought_signature",
}
SECRET_REPLACEMENTS = (
    re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\b(AIza)[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
)
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|authorization|"
    r"bearer|password|passwd|secret|client[_-]?secret|private[_-]?key)"
    r"[\"']?\s*[:=]\s*[\"']?)[^\s\"',;}]{8,}"
)
PRIVATE_KEY_RE = re.compile(
    r"(?is)-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?"
    r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
)


def _bump(
    stats: MutableMapping[str, int] | None, key: str, amount: int = 1
) -> None:
    if stats is not None and amount:
        stats[key] = stats.get(key, 0) + amount


def _replace_literal(
    value: str,
    source: str,
    replacement: str,
    stats: MutableMapping[str, int] | None,
    category: str,
) -> str:
    if not source:
        return value
    count = value.count(source)
    if count:
        _bump(stats, category, count)
        value = value.replace(source, replacement)
    return value


def _binary_marker(media_type: str, encoding: str, payload: str) -> str:
    return (
        f'<OMITTED_BINARY_PAYLOAD media-type="{media_type}" '
        f'encoding="{encoding}" original-characters="{len(payload)}">'
    )


def _opaque_marker(encoding: str, payload: str) -> str:
    return (
        f'<OMITTED_OPAQUE_PAYLOAD encoding="{encoding}" '
        f'original-characters="{len(payload)}">'
    )


def omit_binary_payloads(
    value: str, stats: MutableMapping[str, int] | None = None
) -> str:
    """Replace large embedded media/base64 while preserving factual size metadata."""

    def data_base64(match: re.Match[str]) -> str:
        payload = match.group("payload")
        _bump(stats, "binary_payloads")
        _bump(stats, "binary_characters", len(payload))
        return _binary_marker(match.group("media").lower(), "base64", payload)

    def image_data(match: re.Match[str]) -> str:
        payload = match.group("payload")
        _bump(stats, "binary_payloads")
        _bump(stats, "binary_characters", len(payload))
        return _binary_marker(match.group("media").lower(), "data-uri", payload)

    def plain_base64(match: re.Match[str]) -> str:
        payload = match.group(0)
        _bump(stats, "binary_payloads")
        _bump(stats, "binary_characters", len(payload))
        return _binary_marker("unknown", "base64", payload)

    def fernet_token(match: re.Match[str]) -> str:
        payload = match.group(0)
        _bump(stats, "opaque_payloads")
        _bump(stats, "opaque_characters", len(payload))
        return _opaque_marker("fernet/base64url", payload)

    value = FERNET_TOKEN_RE.sub(fernet_token, value)
    value = DATA_URI_BASE64_RE.sub(data_base64, value)
    value = DATA_URI_IMAGE_RE.sub(image_data, value)
    value = LARGE_BASE64_RE.sub(plain_base64, value)

    def truncated_tail(match: re.Match[str]) -> str:
        tail = match.group("tail")
        _bump(stats, "binary_payloads")
        _bump(stats, "binary_characters", len(tail))
        return match.group("marker") + "[source-truncated binary remainder omitted]"

    return TRUNCATED_BINARY_TAIL_RE.sub(truncated_tail, value)


def censor_text(
    value: str,
    *,
    workspace: Path | str = REPO_ROOT,
    home: Path | str = HOME_ROOT,
    session_roots: Mapping[Path | str, str] | None = None,
    stats: MutableMapping[str, int] | None = None,
) -> str:
    """Return public-safe text without changing ordinary recorded content."""

    value = omit_binary_payloads(value, stats)
    workspace_text = str(Path(workspace).resolve())
    home_text = str(Path(home).resolve())
    roots: dict[str, str] = {
        f"{home_text}/.codex-personal": "<CODEX_HOME>",
        f"{home_text}/.claude-personal": "<CLAUDE_HOME>",
    }
    for path, placeholder in (session_roots or {}).items():
        roots[str(Path(path).resolve())] = placeholder
    for private_path, placeholder in sorted(
        roots.items(), key=lambda item: len(item[0]), reverse=True
    ):
        value = _replace_literal(
            value, private_path, placeholder, stats, "private_paths"
        )

    value = _replace_literal(
        value, workspace_text, "<WORKSPACE>", stats, "private_paths"
    )
    value = _replace_literal(value, home_text, "<HOME>", stats, "private_paths")
    value = _replace_literal(
        value, quote(workspace_text, safe=""), "<WORKSPACE>", stats, "private_paths"
    )
    value = _replace_literal(
        value, quote(home_text, safe=""), "<HOME>", stats, "private_paths"
    )
    value = _replace_literal(
        value, tempfile.gettempdir(), "<TMP>", stats, "private_paths"
    )
    for private_path, placeholder in (
        ("<HOME>/.codex-personal", "<CODEX_HOME>"),
        ("<HOME>/.claude-personal", "<CLAUDE_HOME>"),
        ("~/.codex-personal", "<CODEX_HOME>"),
        ("~/.claude-personal", "<CLAUDE_HOME>"),
        ("${HOME}", "<HOME>"),
        ("$HOME", "<HOME>"),
        ("${TMPDIR}", "<TMP>"),
        ("$TMPDIR", "<TMP>"),
    ):
        value = _replace_literal(
            value, private_path, placeholder, stats, "private_paths"
        )

    value, count = re.subn(
        r"(?:(?:file://)?/(?:home|Users|var/home)/[^/\s\"'<>]+)",
        "<HOME>",
        value,
    )
    _bump(stats, "private_paths", count)
    value, count = re.subn(r"(?<!\w)~(?=/)", "<HOME>", value)
    _bump(stats, "private_paths", count)

    value, count = EMAIL_RE.subn("<EMAIL>", value)
    _bump(stats, "emails", count)

    def replace_ipv4(match: re.Match[str]) -> str:
        try:
            address = ipaddress.ip_address(match.group(0))
        except ValueError:
            return match.group(0)
        if address.is_private or address.is_loopback or address.is_link_local:
            _bump(stats, "lan_addresses")
            return "<LAN_IP>"
        return match.group(0)

    value = IPV4_RE.sub(replace_ipv4, value)
    value, count = PRIVATE_IPV6_RE.subn("<LAN_IP>", value)
    _bump(stats, "lan_addresses", count)

    value, count = PRIVATE_KEY_RE.subn("<REDACTED_PRIVATE_KEY>", value)
    _bump(stats, "secrets", count)
    for pattern in SECRET_REPLACEMENTS:
        value, count = pattern.subn(
            lambda match: f"{match.group(1) if match.lastindex else ''}<REDACTED>",
            value,
        )
        _bump(stats, "secrets", count)
    value, count = SECRET_ASSIGNMENT_RE.subn(r"\1<REDACTED>", value)
    _bump(stats, "secrets", count)

    if CURRENT_USER:
        value, count = re.subn(
            rf"(?i)(?<!not-)\b{re.escape(CURRENT_USER)}\b", "<USER>", value
        )
        _bump(stats, "usernames", count)
    return value


def censor_value(
    value: Any,
    *,
    workspace: Path | str = REPO_ROOT,
    home: Path | str = HOME_ROOT,
    session_roots: Mapping[Path | str, str] | None = None,
    stats: MutableMapping[str, int] | None = None,
    key: str | None = None,
) -> Any:
    """Recursively censor a JSON-compatible transcript value."""

    if key is not None and key.lower().replace("-", "_") in SENSITIVE_KEYS:
        _bump(stats, "sensitive_fields")
        return "<REDACTED>"
    if isinstance(value, str):
        return censor_text(
            value,
            workspace=workspace,
            home=home,
            session_roots=session_roots,
            stats=stats,
        )
    if isinstance(value, list):
        return [
            censor_value(
                item,
                workspace=workspace,
                home=home,
                session_roots=session_roots,
                stats=stats,
            )
            for item in value
        ]
    if isinstance(value, dict):
        cleaned: dict[Any, Any] = {}
        for item_key, item in value.items():
            normalized_key = str(item_key).lower().replace("-", "_")
            if normalized_key in HIDDEN_REASONING_KEYS:
                _bump(stats, "hidden_reasoning_fields")
                continue
            if item_key == "events" and isinstance(item, list):
                public_events = [
                    event
                    for event in item
                    if not (
                        isinstance(event, dict)
                        and event.get("type") == "message"
                        and event.get("role") in {"system", "developer"}
                    )
                ]
                _bump(stats, "hidden_prompt_events", len(item) - len(public_events))
                item = public_events
            cleaned[item_key] = censor_value(
                item,
                workspace=workspace,
                home=home,
                session_roots=session_roots,
                stats=stats,
                key=str(item_key),
            )
        return cleaned
    return value


def iter_json_files(paths: Iterable[Path]) -> Iterable[Path]:
    seen: set[Path] = set()
    for path in paths:
        candidates = path.rglob("*.json") if path.is_dir() else (path,)
        for candidate in candidates:
            if candidate.is_file() and candidate not in seen:
                seen.add(candidate)
                yield candidate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument(
        "--write", action="store_true", help="write censored JSON in place"
    )
    args = parser.parse_args(argv)
    paths = args.paths or [
        REPO_ROOT / name for name in ("anthropic", "openai", "google", "meta")
    ]
    paths = [path if path.is_absolute() else REPO_ROOT / path for path in paths]

    changed = 0
    checked = 0
    totals: Counter[str] = Counter()
    for path in iter_json_files(paths):
        checked += 1
        original = json.loads(path.read_text())
        stats: Counter[str] = Counter()
        censored = censor_value(original, stats=stats)
        totals.update(stats)
        if censored == original:
            continue
        changed += 1
        summary = ", ".join(f"{key}={value}" for key, value in sorted(stats.items()))
        action = "censored" if args.write else "needs censoring"
        try:
            display_path = path.relative_to(REPO_ROOT)
        except ValueError:
            display_path = path
        print(f"{display_path}: {action} ({summary})")
        if args.write:
            path.write_text(json.dumps(censored, indent=2, ensure_ascii=False) + "\n")

    print(
        f"Checked {checked} JSON files; {changed} changed; "
        + ", ".join(f"{key}={value}" for key, value in sorted(totals.items()))
    )
    return 0 if args.write or changed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
