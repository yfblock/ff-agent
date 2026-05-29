from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from agent.channels.utils import guess_mime
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

logger = logging.getLogger(__name__)

DEFAULT_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
UPLOAD_MEDIA_TYPE_IMAGE = 1
UPLOAD_MEDIA_TYPE_VIDEO = 2
UPLOAD_MEDIA_TYPE_FILE = 3
UPLOAD_MAX_RETRIES = 3
MEDIA_MAX_BYTES = 100 * 1024 * 1024


@dataclass(frozen=True)
class UploadedMedia:
    filekey: str
    download_encrypted_query_param: str
    aeskey_hex: str
    file_size: int
    file_size_ciphertext: int


def aes_ecb_padded_size(plaintext_size: int) -> int:
    return ((plaintext_size + 1 + 15) // 16) * 16


def encrypt_aes_ecb(plaintext: bytes, key: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    encryptor = cipher.encryptor()
    pad_len = 16 - (len(plaintext) % 16)
    padded = plaintext + bytes([pad_len]) * pad_len
    return encryptor.update(padded) + encryptor.finalize()


def decrypt_aes_ecb(ciphertext: bytes, key: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    pad_len = padded[-1]
    if pad_len < 1 or pad_len > 16:
        raise ValueError("AES 解密 padding 无效")
    return padded[:-pad_len]


def parse_aes_key(aes_key_base64: str, *, label: str = "media") -> bytes:
    decoded = base64.b64decode(aes_key_base64)
    if len(decoded) == 16:
        return decoded
    text = decoded.decode("ascii", errors="strict")
    if len(text) == 32 and all(ch in "0123456789abcdefABCDEF" for ch in text):
        return bytes.fromhex(text)
    raise ValueError(f"{label}: aes_key 格式无效")


def build_cdn_download_url(encrypted_query_param: str, cdn_base_url: str) -> str:
    query = urllib.parse.urlencode({"encrypted_query_param": encrypted_query_param})
    return f"{cdn_base_url.rstrip('/')}/download?{query}"


def build_cdn_upload_url(cdn_base_url: str, upload_param: str, filekey: str) -> str:
    query = urllib.parse.urlencode(
        {"encrypted_query_param": upload_param, "filekey": filekey}
    )
    return f"{cdn_base_url.rstrip('/')}/upload?{query}"


def _http_request(
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 60,
) -> tuple[int, dict[str, str], bytes]:
    req_headers = dict(headers or {})
    if data is not None and "Content-Type" not in req_headers:
        req_headers["Content-Type"] = "application/octet-stream"
    request = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            response_headers = {k.lower(): v for k, v in response.headers.items()}
            return response.status, response_headers, body
    except urllib.error.HTTPError as exc:
        detail = exc.read()
        err_msg = exc.headers.get("x-error-message") or detail.decode("utf-8", errors="replace")
        raise RuntimeError(f"CDN HTTP {exc.code}: {err_msg}") from exc


def upload_buffer_to_cdn(
    *,
    plaintext: bytes,
    upload_full_url: str | None,
    upload_param: str | None,
    filekey: str,
    cdn_base_url: str,
    aeskey: bytes,
    label: str,
) -> str:
    ciphertext = encrypt_aes_ecb(plaintext, aeskey)
    trimmed_full = (upload_full_url or "").strip()
    if trimmed_full:
        cdn_url = trimmed_full
    elif upload_param:
        cdn_url = build_cdn_upload_url(cdn_base_url, upload_param, filekey)
    else:
        raise RuntimeError(f"{label}: 缺少 CDN 上传 URL")

    last_error: Exception | None = None
    for attempt in range(1, UPLOAD_MAX_RETRIES + 1):
        try:
            status, headers, _ = _http_request(
                cdn_url,
                method="POST",
                data=ciphertext,
                timeout=120,
            )
            if 400 <= status < 500:
                raise RuntimeError(f"{label}: CDN 客户端错误 {status}")
            if status != 200:
                raise RuntimeError(f"{label}: CDN 服务端错误 {status}")
            download_param = headers.get("x-encrypted-param")
            if not download_param:
                raise RuntimeError(f"{label}: CDN 响应缺少 x-encrypted-param")
            return download_param
        except Exception as exc:
            last_error = exc
            if "客户端错误" in str(exc):
                raise
            logger.warning("%s: 上传失败 attempt=%s err=%s", label, attempt, exc)
    raise RuntimeError(f"{label}: CDN 上传失败") from last_error


def download_and_decrypt_buffer(
    *,
    encrypted_query_param: str,
    aes_key_base64: str,
    cdn_base_url: str,
    label: str,
    full_url: str | None = None,
) -> bytes:
    key = parse_aes_key(aes_key_base64, label=label)
    url = full_url or build_cdn_download_url(encrypted_query_param, cdn_base_url)
    status, _, encrypted = _http_request(url, timeout=120)
    if status != 200:
        raise RuntimeError(f"{label}: CDN 下载失败 status={status}")
    return decrypt_aes_ecb(encrypted, key)


def upload_media_file(
    *,
    file_path: Path | str,
    to_user_id: str,
    media_type: int,
    get_upload_url,
    cdn_base_url: str,
    label: str,
) -> UploadedMedia:
    path = Path(file_path)
    plaintext = path.read_bytes()
    if len(plaintext) > MEDIA_MAX_BYTES:
        raise ValueError(f"文件过大（>{MEDIA_MAX_BYTES} 字节）: {path}")

    rawsize = len(plaintext)
    rawfilemd5 = hashlib.md5(plaintext).hexdigest()
    filesize = aes_ecb_padded_size(rawsize)
    filekey = secrets.token_hex(16)
    aeskey = secrets.token_bytes(16)

    upload_resp = get_upload_url(
        filekey=filekey,
        media_type=media_type,
        to_user_id=to_user_id,
        rawsize=rawsize,
        rawfilemd5=rawfilemd5,
        filesize=filesize,
        no_need_thumb=True,
        aeskey=aeskey.hex(),
    )

    upload_full_url = str(upload_resp.get("upload_full_url") or "").strip()
    upload_param = str(upload_resp.get("upload_param") or "")
    if not upload_full_url and not upload_param:
        raise RuntimeError(f"{label}: getUploadUrl 未返回上传地址: {upload_resp}")

    download_param = upload_buffer_to_cdn(
        plaintext=plaintext,
        upload_full_url=upload_full_url or None,
        upload_param=upload_param or None,
        filekey=filekey,
        cdn_base_url=cdn_base_url,
        aeskey=aeskey,
        label=label,
    )
    return UploadedMedia(
        filekey=filekey,
        download_encrypted_query_param=download_param,
        aeskey_hex=aeskey.hex(),
        file_size=rawsize,
        file_size_ciphertext=filesize,
    )


def aes_key_for_outbound_media(aeskey_hex: str) -> str:
    return base64.b64encode(aeskey_hex.encode("ascii")).decode("ascii")


def save_inbound_media(data: bytes, media_dir: Path, suffix: str) -> Path:
    media_dir.mkdir(parents=True, exist_ok=True)
    name = f"{secrets.token_hex(8)}{suffix}"
    path = media_dir / name
    path.write_bytes(data)
    return path
