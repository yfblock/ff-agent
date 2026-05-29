from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from agent.channels.base import Channel, ChannelAttachment, ChannelMessage, ChannelSpec
from agent.channels.router import ChannelRouter
from agent.channels.utils import split_text
from agent.channels.wechat.credentials import (
    load_wechat_ilink_credentials,
    save_wechat_ilink_credentials,
    update_sync_buf,
    WeChatILinkCredentials,
)
from agent.channels.wechat.ilink_client import (
    ILinkClient,
    MESSAGE_TYPE_USER,
    TYPING_STATUS_OFF,
    TYPING_STATUS_ON,
    render_qrcode,
)
from agent.channels.wechat.ilink_media import (
    download_inbound_media,
    parse_media_tags,
    upload_and_send_media,
)
from agent.config import WeChatILinkConfig

logger = logging.getLogger(__name__)


class _TypingIndicator:
    """在 Agent 处理期间向微信发送「正在输入」状态。"""

    def __init__(
        self,
        client: ILinkClient,
        peer_id: str,
        context_token: str,
        *,
        refresh_seconds: float = 4.0,
    ) -> None:
        self.client = client
        self.peer_id = peer_id
        self.context_token = context_token
        self.refresh_seconds = refresh_seconds
        self._ticket = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> _TypingIndicator:
        try:
            self._ticket = self.client.get_typing_ticket(self.peer_id, self.context_token)
            if self._ticket:
                self.client.send_typing(self.peer_id, self._ticket, status=TYPING_STATUS_ON)
                self._thread = threading.Thread(target=self._refresh_loop, daemon=True)
                self._thread.start()
        except Exception as exc:
            logger.debug("开启正在输入状态失败: %s", exc)
        return self

    def __exit__(self, *args) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1)
        if not self._ticket:
            return
        try:
            self.client.send_typing(self.peer_id, self._ticket, status=TYPING_STATUS_OFF)
        except Exception as exc:
            logger.debug("关闭正在输入状态失败: %s", exc)

    def _refresh_loop(self) -> None:
        while not self._stop.wait(self.refresh_seconds):
            if not self._ticket:
                break
            try:
                self.client.send_typing(self.peer_id, self._ticket, status=TYPING_STATUS_ON)
            except Exception as exc:
                logger.debug("刷新正在输入状态失败: %s", exc)
                break


class WeChatILinkChannel(Channel):
    """微信 iLink 扫码登录 Channel（与 OpenClaw openclaw-weixin 同类方案）。"""

    def __init__(self, settings: WeChatILinkConfig, router: ChannelRouter) -> None:
        self.settings = settings
        self.router = router
        self._client: ILinkClient | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def spec(self) -> ChannelSpec:
        return ChannelSpec(
            id="wechat",
            title="微信（扫码登录）",
            description="扫码登录个人/Bot 微信，通过 iLink 长轮询收消息（类似 OpenClaw）。",
        )

    def start(self) -> None:
        creds = load_wechat_ilink_credentials(self.settings.credentials_path)
        if creds is None:
            raise ValueError(
                "尚未登录微信。请先运行: python main.py --channel-login wechat"
            )

        self.settings.media_dir.mkdir(parents=True, exist_ok=True)
        self._client = ILinkClient(
            bot_token=creds.bot_token,
            account_id=creds.account_id,
            base_url=creds.base_url,
            bot_agent=self.settings.bot_agent,
        )
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll_loop, name="wechat-ilink", daemon=True)
        self._thread.start()
        logger.info(
            "微信 iLink channel 已启动，账号: %s",
            creds.account_id,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None
        self._client = None

    def can_handle(self, method: str, path: str) -> bool:
        return False

    def handle_http(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]],
        headers: dict[str, str],
        body: bytes,
    ) -> tuple[int, dict[str, str], bytes]:
        return 404, {"Content-Type": "text/plain; charset=utf-8"}, b"not supported"

    def _poll_loop(self) -> None:
        creds = load_wechat_ilink_credentials(self.settings.credentials_path)
        if creds is None or self._client is None:
            return

        sync_buf = creds.sync_buf
        client = self._client

        while not self._stop.is_set():
            try:
                response = client.get_updates(sync_buf, timeout=40)
            except Exception as exc:
                logger.exception("微信长轮询失败: %s", exc)
                time.sleep(3)
                continue

            if ILinkClient.is_session_expired(response):
                logger.error("微信登录已过期，请重新运行 --channel-login wechat")
                self._stop.set()
                break

            if response.get("ret") not in (0, None):
                logger.warning("getUpdates 返回异常: %s", response)
                time.sleep(2)
                continue

            new_buf = str(response.get("get_updates_buf") or sync_buf)
            if new_buf != sync_buf:
                sync_buf = new_buf
                update_sync_buf(self.settings.credentials_path, sync_buf)

            for raw_msg in response.get("msgs") or []:
                if not isinstance(raw_msg, dict):
                    continue
                self._handle_message(raw_msg)

            timeout_ms = response.get("longpolling_timeout_ms")
            if timeout_ms and not response.get("msgs"):
                time.sleep(min(float(timeout_ms) / 1000, 1))

    def _handle_message(self, raw_msg: dict) -> None:
        if raw_msg.get("message_type") != MESSAGE_TYPE_USER:
            return

        from_user = str(raw_msg.get("from_user_id") or "")
        if not from_user:
            return
        if self.settings.allowed_user_ids and from_user not in self.settings.allowed_user_ids:
            logger.info("忽略未授权用户: %s", from_user)
            return

        text = ILinkClient.extract_text(raw_msg)
        attachments_raw = download_inbound_media(
            raw_msg,
            media_dir=self.settings.media_dir,
            cdn_base_url=self.settings.cdn_base_url,
        )
        attachments = tuple(
            ChannelAttachment(
                kind=item["kind"],  # type: ignore[arg-type]
                path=item["path"],
                mime=item.get("mime", "application/octet-stream"),
                file_name=item.get("file_name", ""),
            )
            for item in attachments_raw
        )

        if not text and not attachments:
            return
        if text in {"/ping", "ping"}:
            self._send_reply(from_user, str(raw_msg.get("context_token") or ""), "pong")
            return

        message = ChannelMessage(
            channel_id="wechat",
            peer_id=from_user,
            text=text,
            message_id=str(raw_msg.get("message_id") or ""),
            account_id=self._client.account_id if self._client else "default",
            metadata={"context_token": raw_msg.get("context_token")},
            attachments=attachments,
        )

        threading.Thread(
            target=self._process_message,
            args=(message, str(raw_msg.get("context_token") or "")),
            daemon=True,
        ).start()

    def _process_message(self, message: ChannelMessage, context_token: str) -> None:
        client = self._client

        def send_attachment_now(path: str) -> None:
            if client is None or not context_token:
                raise RuntimeError("微信客户端未就绪，无法发送附件")
            target = Path(path)
            if not target.is_file():
                raise FileNotFoundError(f"文件不存在: {path}")
            upload_and_send_media(
                client,
                to_user_id=message.peer_id,
                context_token=context_token,
                file_path=target,
                cdn_base_url=self.settings.cdn_base_url,
            )
            logger.info("微信附件已发送: %s", target.name)

        try:
            if client and context_token:
                with _TypingIndicator(client, message.peer_id, context_token):
                    reply = self.router.handle(
                        message,
                        send_attachment=send_attachment_now,
                    )
            else:
                reply = self.router.handle(message)
            self._send_reply(message.peer_id, context_token, reply.text, reply.attachments)
        except Exception as exc:
            logger.exception("处理微信消息失败")
            self._send_reply(message.peer_id, context_token, f"处理失败: {exc}")

    def _send_reply(
        self,
        to_user_id: str,
        context_token: str,
        text: str,
        attachments: tuple[ChannelAttachment, ...] = (),
    ) -> None:
        client = self._client
        if client is None or not context_token:
            return

        workspace = self.router.config.workspace_dir
        cleaned_text, tagged_paths = parse_media_tags(text, workspace_dir=workspace)
        outbound_paths = [Path(p) for p in tagged_paths]
        for item in attachments:
            outbound_paths.append(Path(item.path))

        chunks = split_text(cleaned_text)
        for index, chunk in enumerate(chunks, start=1):
            payload = f"[{index}/{len(chunks)}] {chunk}" if len(chunks) > 1 else chunk
            if payload:
                try:
                    client.send_text(to_user_id, payload, context_token)
                except Exception as exc:
                    logger.exception("发送微信文本失败: %s", exc)
                    return

        for path in outbound_paths:
            try:
                upload_and_send_media(
                    client,
                    to_user_id=to_user_id,
                    context_token=context_token,
                    file_path=path,
                    cdn_base_url=self.settings.cdn_base_url,
                )
            except Exception as exc:
                logger.exception("发送微信媒体失败: %s", exc)
                try:
                    client.send_text(
                        to_user_id,
                        f"媒体发送失败 ({path.name}): {exc}",
                        context_token,
                    )
                except Exception:
                    pass


def login_wechat_ilink(settings: WeChatILinkConfig) -> WeChatILinkCredentials:
    client = ILinkClient(bot_agent=settings.bot_agent)

    def on_qrcode(url: str) -> None:
        print("\n请用微信扫描下方二维码并确认登录:\n")
        render_qrcode(url)

    def on_status(status: str) -> None:
        labels = {
            "wait": "等待扫码…",
            "scaned": "已扫码，请在手机上确认…",
            "confirmed": "登录成功",
            "expired": "二维码已过期，正在刷新…",
        }
        print(f"[微信登录] {labels.get(status, status)}")

    result = client.login(on_qrcode=on_qrcode, on_status=on_status)
    creds = WeChatILinkCredentials(
        bot_token=result.bot_token,
        account_id=result.account_id,
        base_url=result.base_url,
    )
    save_wechat_ilink_credentials(settings.credentials_path, creds)
    print(f"\n登录凭证已保存: {settings.credentials_path}")
    print("接下来运行: python main.py  （TUI 会自动接入微信）")
    return creds
