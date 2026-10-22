from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent_bridge.config import AgentConfig, EnvConfig
from agent_bridge.models import Session, Task, TurnResult
from agent_bridge.worker_env import enforce_env_deny

# asyncio's default StreamReader limit is 64KB. Workers emit one JSON event
# per line, and a single edit event can embed a whole file, so long lines are
# routine. readline() past the limit raises ValueError and drops the buffer.
STDIO_LIMIT = 16 * 1024 * 1024


class Adapter(ABC):
    """Worker process adapter.

    ``resident`` is True when the worker process stays alive between turns
    (ACP). One-shot exec adapters set it False so a finished turn is not
    reported as an active ``ready`` session.
    """

    resident: bool = True

    def __init__(self, agent: AgentConfig, home: Path, env_config: EnvConfig | None = None) -> None:
        self.agent = agent
        self.home = home
        self.env_config = env_config or EnvConfig()

    def can_revive(self) -> bool:
        return self.agent.revivable

    async def list_models(self, cwd: str) -> dict[str, Any]:
        """Return the worker's advertised model catalog."""
        raise NotImplementedError(f"model discovery is not implemented for {self.agent.name}")

    def _enforce_spawn_env(self, env: Mapping[str, str]) -> dict[str, str]:
        """Last ``env.deny`` gate on the exact env a worker spawn receives.

        Agent rewrites after build_worker_env (gateway keys, DSH refills)
        can recreate a denied key; run this on the final env, not earlier.
        """
        return enforce_env_deny(
            env,
            self.env_config,
            set(self.env_config.set) | set(self.agent.env),
        )

    @abstractmethod
    async def ensure_session(self, session: Session) -> None: ...

    @abstractmethod
    async def run_turn(self, session: Session, task: Task) -> TurnResult: ...

    @abstractmethod
    async def cancel(self, session: Session) -> None: ...

    @abstractmethod
    async def shutdown(self, session: Session) -> None: ...
