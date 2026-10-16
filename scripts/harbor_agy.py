"""Pinned native Harbor agy adapter using an existing consumer OAuth login."""

from pathlib import Path
from typing import Literal

from harbor.agents.installed.antigravity_cli import (
    AntigravityCli,
    AntigravityCliOptions,
)
from pydantic import Field


class SubscriptionAgyOptions(AntigravityCliOptions):
    reasoning_effort: Literal["low", "medium", "high"] | None = Field(default=None)
    binary_path: Path = Field(description="Pinned native agy executable.")
    auth_path: Path = Field(description="Existing agy consumer OAuth login file.")


class SubscriptionAgy(AntigravityCli):
    options_model = SubscriptionAgyOptions

    @staticmethod
    def _validate_reasoning_effort(reasoning_effort, model_name):
        if reasoning_effort not in ("low", "medium", "high") or model_name != (
            f"google/gemini-4-argon-{reasoning_effort}"
        ):
            raise ValueError("Use an exact catalog Argon model and its matching effort")

    def _use_adc_auth(self):
        return False

    def _find_api_key(self):
        return None

    def _resolve_api_key(self):
        # Native run() leaves modelProvider unset without an API key. agy then
        # reads the existing consumer login installed below, inside the container.
        return ""

    async def install(self, environment):
        await self.ensure_system_dependencies(
            environment, ("bash", "ca_certificates", "coreutils")
        )
        await self.exec_as_agent(
            environment,
            command="mkdir -p /root/.local/bin /root/.gemini/antigravity-cli",
        )
        await self._upload_agent_owned_file(
            environment, self.options.binary_path, "/root/.local/bin/agy"
        )
        await self.exec_as_agent(environment, command="chmod 755 /root/.local/bin/agy")
        await self._upload_agent_owned_file(
            environment,
            self.options.auth_path,
            "/root/.gemini/antigravity-cli/antigravity-oauth-token",
        )
        await self.exec_as_agent(
            environment,
            command="chmod 700 /root/.gemini/antigravity-cli && chmod 600 /root/.gemini/antigravity-cli/antigravity-oauth-token",
        )
        await self.exec_as_agent(environment, command="/root/.local/bin/agy --version")
