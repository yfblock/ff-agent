from __future__ import annotations

import base64
import json
import logging
import secrets
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
MESSAGE_TYPE_USER = 1
MESSAGE_TYPE_BOT = 2
MESSAGE_STATE_FINISH = 2
SESSION_EXPIRED_ERRCODE = -14
TYPING_STATUS_ON = 1
TYPING_STATUS_OFF = 2

MESSAGE_ITEM_TEXT = 1
MESSAGE_ITEM_IMAGE = 2
MESSAGE_ITEM_VOICE = 3
MESSAGE_ITEM_FILE = 4
MESSAGE_ITEM_VIDEO = 5

UPLOAD_MEDIA_TYPE_IMAGE = 1
UPLOAD_MEDIA_TYPE_VIDEO = 2
UPLOAD_MEDIA_TYPE_FILE = 3


@dataclass
class ILinkLoginResult:
    bot_token: str
    account_id: str
    base_url: str


@dataclass(frozen=True)
class InboundMedia:
    kind: str
    path: str
    mime: str
    file_name: str = ""


class ILinkClient:
    """腾讯 iLink Bot API 客户端（与 OpenClaw openclaw-weixin 相同协议）。"""

    def __init__(
        self,
        *,
        bot_token: str = "",
        account_id: str = "",
        base_url: str = DEFAULT_BASE_URL,
        bot_agent: str = "ff-agent/0.1.0",
        channel_version: str = "0.1.0",
    ) -> None:
        self.bot_token = bot_token
        self.account_id = account_id
        self.base_url = base_url.rstrip("/") or DEFAULT_BASE_URL
        self.bot_agent = bot_agent
        self.channel_version = channel_version

    @staticmethod
    def _random_uin() -> str:
        value = secrets.randbits(32)
        return base64.b64encode(struct.pack(">I", value)).decode("ascii")

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        timeout: float = 30,
        auth: bool = True,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"

        headers = {"Content-Type": "application/json"}
        if extra_headers:
            headers.update(extra_headers)
        if auth:
            if not self.bot_token:
                raise RuntimeError("尚未登录，缺少 bot_token")
            headers["AuthorizationType"] = "ilink_bot_token"
            headers["Authorization"] = f"Bearer {self.bot_token}"
            headers["X-WECHAT-UIN"] = self._random_uin()

        data = None
        if body is not None:
            payload = dict(body)
            payload.setdefault("base_info", {"channel_version": self.channel_version})
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"iLink HTTP {exc.code}: {detail}") from exc

        if not raw.strip():
            return {}
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise RuntimeError(f"iLink 响应格式异常: {parsed!r}")
        return parsed

    def get_bot_qrcode(self, bot_type: str = "3") -> tuple[str, str]:
        data = self._request_json(
            "GET",
            "ilink/bot/get_bot_qrcode",
            query={"bot_type": bot_type},
            auth=False,
        )
        token = str(data.get("qrcode") or "")
        img_url = str(
            data.get("qrcode_img_content")
            or data.get("qrcode_url")
            or data.get("url")
            or ""
        )
        if not token:
            raise RuntimeError(f"获取二维码失败: {data}")
        return token, img_url or token

    def poll_qrcode_status(self, qrcode: str, timeout: float = 35) -> dict[str, Any]:
        return self._request_json(
            "GET",
            "ilink/bot/get_qrcode_status",
            query={"qrcode": qrcode},
            timeout=timeout,
            auth=False,
            extra_headers={"iLink-App-ClientVersion": "1"},
        )

    def login(
        self,
        *,
        on_qrcode: Callable[[str], None] | None = None,
        on_status: Callable[[str], None] | None = None,
        timeout_ms: int = 480_000,
        max_refreshes: int = 3,
    ) -> ILinkLoginResult:
        deadline = time.time() + timeout_ms / 1000
        refreshes = 0

        while time.time() < deadline:
            qrcode_token, qrcode_url = self.get_bot_qrcode()
            if on_qrcode:
                on_qrcode(qrcode_url)
            if on_status:
                on_status("wait")

            while time.time() < deadline:
                status_data = self.poll_qrcode_status(qrcode_token, timeout=35)
                status = str(status_data.get("status") or "wait")
                if on_status:
                    on_status(status)

                if status == "confirmed":
                    token = str(status_data.get("bot_token") or "")
                    account_id = str(
                        status_data.get("ilink_bot_id")
                        or status_data.get("account_id")
                        or ""
                    )
                    base_url = str(status_data.get("baseurl") or DEFAULT_BASE_URL).rstrip("/")
                    if not token or not account_id:
                        raise RuntimeError(f"登录成功但凭证不完整: {status_data}")
                    self.bot_token = token
                    self.account_id = account_id
                    self.base_url = base_url or DEFAULT_BASE_URL
                    return ILinkLoginResult(token, account_id, self.base_url)

                if status == "expired":
                    refreshes += 1
                    if refreshes > max_refreshes:
                        raise RuntimeError("二维码多次过期，请重试登录")
                    break

                if status == "scaned" and on_status:
                    on_status("scaned")

        raise TimeoutError("扫码登录超时，请重试")

    def get_updates(self, sync_buf: str = "", timeout: float = 40) -> dict[str, Any]:
        return self._request_json(
            "POST",
            "ilink/bot/getupdates",
            body={"get_updates_buf": sync_buf},
            timeout=timeout,
        )

    def get_upload_url(self, **fields: Any) -> dict[str, Any]:
        data = self._request_json(
            "POST",
            "ilink/bot/getuploadurl",
            body=fields,
            timeout=60,
        )
        if data.get("ret") not in (0, None):
            logger.error("getUploadUrl 失败: %s", data)
            raise RuntimeError(f"getUploadUrl 失败: {data.get('errmsg') or data}")
        return data

    def send_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        payload = dict(msg)
        payload.setdefault("from_user_id", self.account_id)
        payload.setdefault("client_id", str(uuid.uuid4()))
        payload.setdefault("message_type", MESSAGE_TYPE_BOT)
        payload.setdefault("message_state", MESSAGE_STATE_FINISH)
        return self._request_json(
            "POST",
            "ilink/bot/sendmessage",
            body={"msg": payload},
            timeout=60,
        )

    def send_text(self, to_user_id: str, text: str, context_token: str) -> dict[str, Any]:
        return self.send_message(
            {
                "to_user_id": to_user_id,
                "context_token": context_token,
                "item_list": [{"type": MESSAGE_ITEM_TEXT, "text_item": {"text": text}}],
            }
        )

    def send_message_item(
        self,
        to_user_id: str,
        item: dict[str, Any],
        context_token: str,
    ) -> dict[str, Any]:
        return self.send_message(
            {
                "to_user_id": to_user_id,
                "context_token": context_token,
                "item_list": [item],
            }
        )

    def send_image(
        self,
        to_user_id: str,
        *,
        context_token: str,
        encrypt_query_param: str,
        aeskey_hex: str,
        ciphertext_size: int,
    ) -> dict[str, Any]:
        from agent.channels.wechat.ilink_cdn import aes_key_for_outbound_media

        return self.send_message_item(
            to_user_id,
            {
                "type": MESSAGE_ITEM_IMAGE,
                "image_item": {
                    "media": {
                        "encrypt_query_param": encrypt_query_param,
                        "aes_key": aes_key_for_outbound_media(aeskey_hex),
                        "encrypt_type": 1,
                    },
                    "mid_size": ciphertext_size,
                },
            },
            context_token,
        )

    def send_file(
        self,
        to_user_id: str,
        *,
        context_token: str,
        file_name: str,
        encrypt_query_param: str,
        aeskey_hex: str,
        file_size: int,
    ) -> dict[str, Any]:
        from agent.channels.wechat.ilink_cdn import aes_key_for_outbound_media

        return self.send_message_item(
            to_user_id,
            {
                "type": MESSAGE_ITEM_FILE,
                "file_item": {
                    "media": {
                        "encrypt_query_param": encrypt_query_param,
                        "aes_key": aes_key_for_outbound_media(aeskey_hex),
                        "encrypt_type": 1,
                    },
                    "file_name": file_name,
                    "len": str(file_size),
                },
            },
            context_token,
        )

    def get_typing_ticket(self, ilink_user_id: str, context_token: str = "") -> str:
        body: dict[str, Any] = {"ilink_user_id": ilink_user_id}
        if context_token:
            body["context_token"] = context_token
        data = self._request_json("POST", "ilink/bot/getconfig", body=body)
        if data.get("ret") not in (0, None):
            logger.warning("getconfig 失败: %s", data)
            return ""
        return str(data.get("typing_ticket") or "")

    def send_typing(
        self,
        ilink_user_id: str,
        typing_ticket: str,
        *,
        status: int = TYPING_STATUS_ON,
    ) -> dict[str, Any]:
        if not typing_ticket:
            return {}
        return self._request_json(
            "POST",
            "ilink/bot/sendtyping",
            body={
                "ilink_user_id": ilink_user_id,
                "typing_ticket": typing_ticket,
                "status": status,
            },
        )

    @staticmethod
    def extract_text(message: dict[str, Any]) -> str:
        parts: list[str] = []
        for item in message.get("item_list") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == MESSAGE_ITEM_TEXT:
                text_item = item.get("text_item") or {}
                if isinstance(text_item, dict) and text_item.get("text"):
                    parts.append(str(text_item["text"]))
            elif item.get("type") == MESSAGE_ITEM_VOICE:
                voice_item = item.get("voice_item") or {}
                if isinstance(voice_item, dict) and voice_item.get("text"):
                    parts.append(str(voice_item["text"]))
        return "\n".join(parts).strip()

    @staticmethod
    def is_session_expired(response: dict[str, Any]) -> bool:
        if response.get("ret") == 0:
            return False
        return response.get("errcode") == SESSION_EXPIRED_ERRCODE


def render_qrcode(url: str) -> None:
    try:
        import qrcode

        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.print_ascii(invert=True)
    except Exception:
        print(f"\n请用微信扫描以下链接对应的二维码:\n{url}\n")
