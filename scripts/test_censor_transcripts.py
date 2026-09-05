#!/usr/bin/env python3
"""Regression tests for the shared transcript censor."""

from __future__ import annotations

import unittest
from pathlib import Path

from scripts.censor_transcripts import censor_text, censor_value


class TranscriptCensorTests(unittest.TestCase):
    def test_elides_fernet_payload_but_keeps_tool_call(self) -> None:
        token = "gAAAAA" + "Ab_9" * 80 + "=="
        event = {
            "type": "tool_call",
            "id": "call-1",
            "tool": "spawn_agent",
            "input": {"task_name": "browser_check", "message": token},
        }

        censored = censor_value(event)

        self.assertEqual(censored["tool"], "spawn_agent")
        self.assertEqual(censored["input"]["task_name"], "browser_check")
        self.assertEqual(
            censored["input"]["message"],
            (
                '<OMITTED_OPAQUE_PAYLOAD encoding="fernet/base64url" '
                f'original-characters="{len(token)}">'
            ),
        )

    def test_keeps_plain_text_reasoning_shared_as_message(self) -> None:
        transcript = {
            "events": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": "I compared both layouts and chose the simpler one.",
                }
            ]
        }

        self.assertEqual(censor_value(transcript), transcript)

    def test_removes_structured_hidden_reasoning(self) -> None:
        value = {
            "content": "Visible answer",
            "encrypted_content": "opaque",
            "internal_reasoning": "private",
        }

        self.assertEqual(censor_value(value), {"content": "Visible answer"})

    def test_redacts_private_paths_and_large_binary(self) -> None:
        payload = "A" * 5000
        value = {
            "path": "/home/private-user/project/index.html",
            "image": f"data:image/png;base64,{payload}",
        }

        censored = censor_value(
            value,
            workspace=Path("/home/private-user/project"),
            home=Path("/home/private-user"),
        )

        self.assertEqual(censored["path"], "<WORKSPACE>/index.html")
        self.assertEqual(
            censored["image"],
            (
                '<OMITTED_BINARY_PAYLOAD media-type="image/png" encoding="base64" '
                'original-characters="5000">'
            ),
        )

    def test_redacts_both_claude_roots_and_encoded_variants(self) -> None:
        home = Path("/home/private-user")
        value = " ".join(
            (
                "/home/private-user/.claude/projects/session.jsonl",
                "/home/private-user/.claude-personal/projects/session.jsonl",
                "%2Fhome%2Fprivate-user%2F.claude%2Fprojects",
                "-home-private-user-.claude-projects-session",
            )
        )

        censored = censor_text(value, home=home)

        self.assertNotIn("private-user", censored)
        self.assertNotIn(".claude", censored)
        self.assertEqual(censored.count("<CLAUDE_HOME>"), 4)


if __name__ == "__main__":
    unittest.main()
