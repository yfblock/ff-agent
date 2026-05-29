from __future__ import annotations

import threading

from agent.config import Config
from agent.core import Agent


class SessionHub:
    """统一管理本地 TUI 与消息渠道共用的 Agent 会话。"""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._local_agent: Agent | None = None
        self._channel_agents: dict[str, Agent] = {}

    @property
    def local_agent(self) -> Agent:
        with self._lock:
            if self._local_agent is None:
                self._local_agent = Agent(self.config, session_key="local")
            return self._local_agent

    def get_channel_agent(
        self,
        channel_id: str,
        peer_id: str,
        account_id: str = "default",
    ) -> Agent:
        if self.config.shared_chat_session:
            return self.local_agent
        key = f"{channel_id}:{account_id}:{peer_id}"
        with self._lock:
            agent = self._channel_agents.get(key)
            if agent is None:
                agent = Agent(self.config, session_key=key)
                self._channel_agents[key] = agent
            return agent
