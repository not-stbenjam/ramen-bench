"""Small native Harbor Muse adapter for a pinned local CLI and Max effort."""

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
