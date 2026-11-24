"""Public-client Twitch OAuth and Helix lookup helpers for the Windows app."""
from __future__ import annotations

import base64
import ctypes
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from ctypes import wintypes
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from app_paths import DATA_DIR


DEFAULT_CLIENT_ID = "95litvelyd4mlshypb9pakhhzm0vvh"
DEVICE_URL = "https://id.twitch.tv/oauth2/device"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
VIDEOS_URL = "https://api.twitch.tv/helix/videos"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
TOKEN_FILE = DATA_DIR / "twitch_refresh_token.dpapi"


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(raw: bytes):
    buffer = ctypes.create_string_buffer(raw)
    blob = _DataBlob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    return buffer, blob


def protect_token(token: str) -> str:
    """Encrypt a refresh token for this Windows user with DPAPI."""
    if not hasattr(ctypes, "windll"):
        raise OSError("Twitch token encryption is available only on Windows.")
    raw = token.encode("utf-8")
    buffer, source = _blob(raw)
    target = _DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptProtectData.argtypes = [ctypes.POINTER(_DataBlob), wintypes.LPCWSTR,
                                         ctypes.POINTER(_DataBlob), ctypes.c_void_p,
                                         ctypes.c_void_p, wintypes.DWORD,
                                         ctypes.POINTER(_DataBlob)]
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    ok = crypt32.CryptProtectData(
        ctypes.byref(source), "TubeSage Twitch refresh token", None, None, None, 1,
        ctypes.byref(target))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return base64.b64encode(ctypes.string_at(target.pbData, target.cbData)).decode("ascii")
    finally:
        kernel32.LocalFree(target.pbData)


def unprotect_token(protected: str) -> str:
    if not hasattr(ctypes, "windll"):
        raise OSError("Twitch token decryption is available only on Windows.")
    raw = base64.b64decode(protected)
    buffer, source = _blob(raw)
    target = _DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(_DataBlob),
                                           ctypes.POINTER(wintypes.LPWSTR),
                                           ctypes.POINTER(_DataBlob), ctypes.c_void_p,
                                           ctypes.c_void_p, wintypes.DWORD,
                                           ctypes.POINTER(_DataBlob)]
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(target.pbData, target.cbData).decode("utf-8")
    finally:
        kernel32.LocalFree(target.pbData)


def save_refresh_token(protected: str) -> None:
    """Atomically persist the DPAPI-protected, rotating token outside settings."""
    temporary = TOKEN_FILE.with_suffix(".tmp")
    temporary.write_text(protected, encoding="ascii")
    temporary.replace(TOKEN_FILE)


def load_refresh_token() -> str:
    try:
        return TOKEN_FILE.read_text(encoding="ascii").strip()
    except OSError:
        return ""


def _post_form(url: str, data: dict, timeout: int = 20) -> dict:
    body = urllib.parse.urlencode(data).encode("ascii")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded",
                                 "User-Agent": "TubeSage/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8", "replace"))
            message = payload.get("message") or payload.get("error_description") or payload.get("error")
        except (ValueError, AttributeError):
            message = str(exc)
        raise RuntimeError(str(message)) from exc


def _get_json(url: str, client_id: str, access_token: str) -> dict:
    request = urllib.request.Request(url, headers={
        "Client-ID": client_id,
        "Authorization": f"Bearer {access_token}",
        "User-Agent": "TubeSage/1.0",
    })
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8", "replace"))
            message = payload.get("message") or payload.get("error")
        except (ValueError, AttributeError):
            message = str(exc)
        raise RuntimeError(str(message)) from exc


def fetch_stream_ids(client_id: str, encrypted_refresh_token: str,
                     vod_ids: list[str], on_refresh=None) -> tuple[dict[str, str], str]:
    """Look up canonical Twitch broadcast IDs for Twitch VOD IDs."""
    refresh_token = unprotect_token(encrypted_refresh_token)
    token = _post_form(TOKEN_URL, {
        "client_id": client_id,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    })
    access_token = token["access_token"]
    rotated = protect_token(token.get("refresh_token") or refresh_token)
    if on_refresh:
        on_refresh(rotated)
    numeric_ids = list(dict.fromkeys(str(value).removeprefix("v") for value in vod_ids))
    output: dict[str, str] = {}
    for offset in range(0, len(numeric_ids), 100):
        batch = numeric_ids[offset:offset + 100]
        query = urllib.parse.urlencode([("id", value) for value in batch])
        response = _get_json(f"{VIDEOS_URL}?{query}", client_id, access_token)
        for video in response.get("data", []):
            if video.get("id") and video.get("stream_id"):
                output[f"v{video['id']}"] = str(video["stream_id"])
    return output, rotated


class DeviceAuthorizationWorker(QThread):
    status = Signal(str)
    authorization_ready = Signal(str, str)  # verification URL, user code
    connected = Signal(str)  # refresh token; the UI protects it before saving
    failed = Signal(str)

    def __init__(self, client_id: str, parent=None):
        super().__init__(parent)
        self.client_id = client_id.strip()
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            device = _post_form(DEVICE_URL, {"client_id": self.client_id, "scopes": ""})
            self.authorization_ready.emit(device["verification_uri"], device["user_code"])
            deadline = time.monotonic() + int(device.get("expires_in", 1800))
            interval = max(5, int(device.get("interval", 5)))
            self.status.emit("Waiting for Twitch authorization in your browser.")
            while not self._cancel and time.monotonic() < deadline:
                time.sleep(interval)
                if self._cancel:
                    return
                try:
                    token = _post_form(TOKEN_URL, {
                        "client_id": self.client_id,
                        "device_code": device["device_code"],
                        "grant_type": DEVICE_GRANT,
                        "scopes": "",
                    })
                    self.connected.emit(token["refresh_token"])
                    return
                except RuntimeError as exc:
                    message = str(exc).lower()
                    if "authorization_pending" in message:
                        continue
                    if "slow_down" in message:
                        interval += 5
                        continue
                    if "authorization_declined" in message or "access_denied" in message:
                        raise RuntimeError("Twitch authorization was declined.") from exc
                    if "expired_token" in message or "invalid device code" in message:
                        raise RuntimeError("The Twitch device code expired. Try connecting again.") from exc
                    raise
            if not self._cancel:
                raise RuntimeError("The Twitch device code expired. Try connecting again.")
        except Exception as exc:
            if not self._cancel:
                self.failed.emit(str(exc))


class VideoMetadataWorker(QThread):
    found = Signal(dict)
    failed = Signal(str)

    def __init__(self, client_id: str, encrypted_refresh_token: str,
                 vod_ids: list[str], parent=None):
        super().__init__(parent)
        self.client_id = client_id
        self.encrypted_refresh_token = encrypted_refresh_token
        self.vod_ids = list(vod_ids)

    def run(self):
        try:
            stream_ids, _rotated = fetch_stream_ids(
                self.client_id, self.encrypted_refresh_token, self.vod_ids,
                on_refresh=save_refresh_token)
            self.found.emit(stream_ids)
        except Exception as exc:
            self.failed.emit(str(exc))
