from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass
class WeChatILinkCredentials:
    bot_token: str
    account_id: str
    base_url: str = "https://ilinkai.weixin.qq.com"
    sync_buf: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WeChatILinkCredentials:
        return cls(
            bot_token=str(data.get("bot_token") or ""),
            account_id=str(data.get("account_id") or data.get("ilink_bot_id") or ""),
            base_url=str(data.get("base_url") or "https://ilinkai.weixin.qq.com").rstrip("/"),
            sync_buf=str(data.get("sync_buf") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_wechat_ilink_credentials(path: Path) -> WeChatILinkCredentials | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    creds = WeChatILinkCredentials.from_dict(data)
    if not creds.bot_token or not creds.account_id:
        return None
    return creds


def save_wechat_ilink_credentials(path: Path, creds: WeChatILinkCredentials) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(creds.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def update_sync_buf(path: Path, sync_buf: str) -> None:
    creds = load_wechat_ilink_credentials(path)
    if creds is None:
        return
    save_wechat_ilink_credentials(path, WeChatILinkCredentials(
        bot_token=creds.bot_token,
        account_id=creds.account_id,
        base_url=creds.base_url,
        sync_buf=sync_buf,
    ))
