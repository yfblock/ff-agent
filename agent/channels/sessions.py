from __future__ import annotations

import threading

from agent.config import Config
from agent.core import Agent
from agent.session_hub import SessionHub


class ChannelSessionManager:
    """按 channel + peer 隔离 Agent 会话；共享模式下与 TUI 使用同一会话。"""

    def __init__(
        self,
        config: Config,
        hub: SessionHub | None = None,
    ) -> None:
        self.config = config
        self.hub = hub or SessionHub(config)
        self._agents: dict[str, Agent] = {}
        self._lock = threading.Lock()

    def session_key(self, channel_id: str, peer_id: str, account_id: str = "default") -> str:
        if self.config.shared_chat_session:
            return "local"
        return f"{channel_id}:{account_id}:{peer_id}"

    def get_agent(self, channel_id: str, peer_id: str, account_id: str = "default") -> Agent:
        if self.config.shared_chat_session:
            agent = self.hub.local_agent
            return agent

        key = self.session_key(channel_id, peer_id, account_id)
        with self._lock:
            agent = self._agents.get(key)
            if agent is None:
                agent = Agent(self.config, session_key=key)
                self._agents[key] = agent
            if agent.channel_id != channel_id:
                agent.set_channel(channel_id)
            return agent

    def reset_session(self, channel_id: str, peer_id: str, account_id: str = "default") -> None:
        if self.config.shared_chat_session:
            self.hub.local_agent.reset()
            return

        key = self.session_key(channel_id, peer_id, account_id)
        with self._lock:
            agent = self._agents.get(key)
            if agent is not None:
                agent.reset()

    def active_sessions(self) -> list[str]:
        if self.config.shared_chat_session:
            return ["local"]
        with self._lock:
            return list(self._agents.keys())
