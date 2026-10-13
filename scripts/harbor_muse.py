"""Native Harbor Muse adapter for a pinned CLI, all efforts, and subscription auth."""

from pathlib import Path
from typing import Annotated, Literal

from harbor.agents.installed.muse_code import MuseCode, MuseCodeOptions
from harbor.agents.options import Cli
from pydantic import Field


class PinnedMuseOptions(MuseCodeOptions):
    reasoning_effort: Annotated[
        Literal["minimal", "low", "medium", "high", "xhigh", "max"] | None,
        Cli("--reasoning-effort", fallback="MUSE_CODE_REASONING_EFFORT"),
    ] = Field(default=None, description="Wonder's provider-advertised effort tiers.")
    binary_path: Path = Field(description="Local native Muse Code executable.")
    auth_path: Path | None = Field(
        default=None, description="Local Muse login for subscription access."
    )
    contributor_mode: Annotated[
        Literal["on", "off"] | None,
        Cli("--contributor-mode"),
    ] = Field(default=None, description="Explicit native per-run contributor setting.")


class PinnedMuseCode(MuseCode):
    options_model = PinnedMuseOptions

    async def install(self, environment):
        # The native installer cannot pin versions and its option schema omits
        # Max. Upload the already installed native executable instead; all run
        # commands, session collection, and isolation still come from Harbor.
        await self.ensure_system_dependencies(
            environment, ("bash", "ca_certificates", "coreutils")
        )
        await self.exec_as_agent(environment, command='mkdir -p "$HOME/.local/bin"')
        await self._upload_agent_owned_file(
            environment, self.options.binary_path, "/root/.local/bin/muse"
        )
        await self.exec_as_agent(environment, command="chmod 755 /root/.local/bin/muse")
        if self.options.auth_path is not None:
            await self.exec_as_agent(environment, command="mkdir -p /root/.config/muse")
            await self._upload_agent_owned_file(
                environment, self.options.auth_path, "/root/.config/muse/auth.json"
            )
            await self.exec_as_agent(
                environment, command="chmod 600 /root/.config/muse/auth.json"
            )
