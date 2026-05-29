from __future__ import annotations

import logging
import re
from pathlib import Path

from agent.channels.wechat.ilink_cdn import (
    DEFAULT_CDN_BASE_URL,
    UPLOAD_MEDIA_TYPE_FILE,
    UPLOAD_MEDIA_TYPE_IMAGE,
    UPLOAD_MEDIA_TYPE_VIDEO,
    download_and_decrypt_buffer,
    guess_mime,
    save_inbound_media,
    upload_media_file,
)
from agent.channels.wechat.ilink_client import (
    MESSAGE_ITEM_FILE,
    MESSAGE_ITEM_IMAGE,
    MESSAGE_ITEM_VIDEO,
    MESSAGE_ITEM_VOICE,
    ILinkClient,
)

logger = logging.getLogger(__name__)

MEDIA_TAG_RE = re.compile(r"\[\[media:([^\]]+)\]\]", re.IGNORECASE)


def download_inbound_media(
    raw_msg: dict,
    *,
    media_dir: Path,
    cdn_base_url: str = DEFAULT_CDN_BASE_URL,
) -> list[dict[str, str]]:
    """从入站消息下载图片/文件/视频，返回 attachment 描述列表。"""
    attachments: list[dict[str, str]] = []

    for item in raw_msg.get("item_list") or []:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        label = f"inbound-{item_type}"

        try:
            if item_type == MESSAGE_ITEM_IMAGE:
                attachment = _download_image_item(item, media_dir=media_dir, cdn_base_url=cdn_base_url, label=label)
            elif item_type == MESSAGE_ITEM_FILE:
                attachment = _download_file_item(item, media_dir=media_dir, cdn_base_url=cdn_base_url, label=label)
            elif item_type == MESSAGE_ITEM_VIDEO:
                attachment = _download_video_item(item, media_dir=media_dir, cdn_base_url=cdn_base_url, label=label)
            elif item_type == MESSAGE_ITEM_VOICE:
                attachment = _download_voice_item(item, media_dir=media_dir, cdn_base_url=cdn_base_url, label=label)
            else:
                attachment = None
        except Exception as exc:
            logger.exception("下载入站媒体失败: %s", exc)
            attachment = None

        if attachment:
            attachments.append(attachment)

    return attachments


def base64_hex_key(aeskey_hex: str) -> str:
    import base64

    return base64.b64encode(aeskey_hex.encode("ascii")).decode("ascii")


def _download_image_item(
    item: dict,
    *,
    media_dir: Path,
    cdn_base_url: str,
    label: str,
) -> dict[str, str] | None:
    image_item = item.get("image_item") or {}
    media = image_item.get("media") or {}
    encrypt_param = str(media.get("encrypt_query_param") or "")
    full_url = str(media.get("full_url") or "") or None
    aes_key = str(media.get("aes_key") or "")
    if image_item.get("aeskey") and not aes_key:
        aes_key = base64_hex_key(str(image_item["aeskey"]))
    if not encrypt_param and not full_url:
        return None

    data = download_and_decrypt_buffer(
        encrypted_query_param=encrypt_param,
        aes_key_base64=aes_key,
        cdn_base_url=cdn_base_url,
        label=f"{label}-image",
        full_url=full_url,
    )
    path = save_inbound_media(data, media_dir, ".jpg")
    return {"kind": "image", "path": str(path), "mime": guess_mime(path), "file_name": path.name}


def _download_file_item(
    item: dict,
    *,
    media_dir: Path,
    cdn_base_url: str,
    label: str,
) -> dict[str, str] | None:
    file_item = item.get("file_item") or {}
    media = file_item.get("media") or {}
    encrypt_param = str(media.get("encrypt_query_param") or "")
    full_url = str(media.get("full_url") or "") or None
    aes_key = str(media.get("aes_key") or "")
    file_name = str(file_item.get("file_name") or "file.bin")
    if not ((encrypt_param or full_url) and aes_key):
        return None

    data = download_and_decrypt_buffer(
        encrypted_query_param=encrypt_param,
        aes_key_base64=aes_key,
        cdn_base_url=cdn_base_url,
        label=f"{label}-file",
        full_url=full_url,
    )
    suffix = Path(file_name).suffix or ".bin"
    path = save_inbound_media(data, media_dir, suffix)
    return {
        "kind": "file",
        "path": str(path),
        "mime": guess_mime(file_name),
        "file_name": file_name,
    }


def _download_video_item(
    item: dict,
    *,
    media_dir: Path,
    cdn_base_url: str,
    label: str,
) -> dict[str, str] | None:
    video_item = item.get("video_item") or {}
    media = video_item.get("media") or {}
    encrypt_param = str(media.get("encrypt_query_param") or "")
    full_url = str(media.get("full_url") or "") or None
    aes_key = str(media.get("aes_key") or "")
    if not ((encrypt_param or full_url) and aes_key):
        return None

    data = download_and_decrypt_buffer(
        encrypted_query_param=encrypt_param,
        aes_key_base64=aes_key,
        cdn_base_url=cdn_base_url,
        label=f"{label}-video",
        full_url=full_url,
    )
    path = save_inbound_media(data, media_dir, ".mp4")
    return {"kind": "video", "path": str(path), "mime": "video/mp4", "file_name": path.name}


def _download_voice_item(
    item: dict,
    *,
    media_dir: Path,
    cdn_base_url: str,
    label: str,
) -> dict[str, str] | None:
    voice_item = item.get("voice_item") or {}
    media = voice_item.get("media") or {}
    encrypt_param = str(media.get("encrypt_query_param") or "")
    full_url = str(media.get("full_url") or "") or None
    aes_key = str(media.get("aes_key") or "")
    if not ((encrypt_param or full_url) and aes_key):
        return None

    data = download_and_decrypt_buffer(
        encrypted_query_param=encrypt_param,
        aes_key_base64=aes_key,
        cdn_base_url=cdn_base_url,
        label=f"{label}-voice",
        full_url=full_url,
    )
    path = save_inbound_media(data, media_dir, ".silk")
    return {"kind": "voice", "path": str(path), "mime": "audio/silk", "file_name": path.name}


def upload_and_send_media(
    client: ILinkClient,
    *,
    to_user_id: str,
    context_token: str,
    file_path: Path | str,
    cdn_base_url: str = DEFAULT_CDN_BASE_URL,
    caption: str = "",
) -> None:
    path = Path(file_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"文件不存在: {path}")

    mime = guess_mime(path)
    if mime.startswith("video/"):
        media_type = UPLOAD_MEDIA_TYPE_VIDEO
    elif mime.startswith("image/"):
        media_type = UPLOAD_MEDIA_TYPE_IMAGE
    else:
        media_type = UPLOAD_MEDIA_TYPE_FILE

    uploaded = upload_media_file(
        file_path=path,
        to_user_id=to_user_id,
        media_type=media_type,
        get_upload_url=lambda **kwargs: client.get_upload_url(**kwargs),
        cdn_base_url=cdn_base_url,
        label=f"upload-{path.name}",
    )

    if caption:
        client.send_text(to_user_id, caption, context_token)

    if media_type == UPLOAD_MEDIA_TYPE_IMAGE:
        client.send_image(
            to_user_id,
            context_token=context_token,
            encrypt_query_param=uploaded.download_encrypted_query_param,
            aeskey_hex=uploaded.aeskey_hex,
            ciphertext_size=uploaded.file_size_ciphertext,
        )
    elif media_type == UPLOAD_MEDIA_TYPE_FILE:
        client.send_file(
            to_user_id,
            context_token=context_token,
            file_name=path.name,
            encrypt_query_param=uploaded.download_encrypted_query_param,
            aeskey_hex=uploaded.aeskey_hex,
            file_size=uploaded.file_size,
        )
    else:
        client.send_message_item(
            to_user_id,
            {
                "type": MESSAGE_ITEM_VIDEO,
                "video_item": {
                    "media": {
                        "encrypt_query_param": uploaded.download_encrypted_query_param,
                        "aes_key": base64_hex_key(uploaded.aeskey_hex),
                        "encrypt_type": 1,
                    },
                    "video_size": uploaded.file_size_ciphertext,
                },
            },
            context_token,
        )


def parse_media_tags(text: str, *, workspace_dir: Path) -> tuple[str, list[Path]]:
    paths: list[Path] = []

    def repl(match: re.Match[str]) -> str:
        raw = match.group(1).strip().strip("\"'")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = (workspace_dir / path).resolve()
        else:
            path = path.resolve()
        paths.append(path)
        return ""

    cleaned = MEDIA_TAG_RE.sub(repl, text).strip()
    return cleaned, paths
