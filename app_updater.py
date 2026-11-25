"""Fetch TubeSage releases and launch the per-user installer after exit."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import base64
import urllib.error
import urllib.request
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from app_paths import DATA_DIR

APP_VERSION = "1.2.3"
RELEASE_API = "https://api.github.com/repos/GuysLetsPlay/TubeSage/releases/latest"
ASSET_NAME = "TubeSage-Setup.exe"


def _version_tuple(value: str) -> tuple[int, ...]:
    value = value.strip().lstrip("vV").split("-", 1)[0]
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError:
        return ()


class UpdateWorker(QThread):
    progress = Signal(str)
    up_to_date = Signal(str)
    downloaded = Signal(str, str)  # installer path, version
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)

    def run(self):
        try:
            self.progress.emit("Checking GitHub for TubeSage updates…")
            request = urllib.request.Request(RELEASE_API, headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"TubeSage/{APP_VERSION}",
            })
            with urllib.request.urlopen(request, timeout=30) as response:
                release = json.loads(response.read().decode("utf-8"))
            latest = str(release.get("tag_name", "")).lstrip("v")
            if not latest or not _version_tuple(latest):
                raise RuntimeError("GitHub returned an invalid release version.")
            if _version_tuple(latest) <= _version_tuple(APP_VERSION):
                self.up_to_date.emit(APP_VERSION)
                return
            asset = next((item for item in release.get("assets", [])
                          if item.get("name") == ASSET_NAME), None)
            if not asset or not asset.get("browser_download_url"):
                raise RuntimeError(f"Release {latest} has no {ASSET_NAME} installer.")

            update_dir = DATA_DIR / "updates"
            update_dir.mkdir(parents=True, exist_ok=True)
            temporary = update_dir / f"{ASSET_NAME}.download"
            installer = update_dir / ASSET_NAME
            request = urllib.request.Request(asset["browser_download_url"], headers={
                "User-Agent": f"TubeSage/{APP_VERSION}",
            })
            digest = hashlib.sha256()
            total = int(asset.get("size") or 0)
            received = 0
            with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
                    if total:
                        self.progress.emit(f"Downloading TubeSage {latest}: {received * 100 // total}%")
            if total and received != total:
                temporary.unlink(missing_ok=True)
                raise RuntimeError("The installer download was incomplete. Try again.")
            expected = str(asset.get("digest", ""))
            if not expected.startswith("sha256:"):
                temporary.unlink(missing_ok=True)
                raise RuntimeError("GitHub did not provide the installer checksum; update canceled.")
            if digest.hexdigest().lower() != expected[7:].lower():
                temporary.unlink(missing_ok=True)
                raise RuntimeError("The installer failed GitHub's SHA-256 check. Try again.")
            temporary.replace(installer)
            self.downloaded.emit(str(installer), latest)
        except urllib.error.HTTPError as exc:
            self.failed.emit(f"GitHub update check failed (HTTP {exc.code}).")
        except (OSError, ValueError, RuntimeError) as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(f"Update failed: {exc}")


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def launch_installer_after_exit(installer_path: str, app_path: str) -> None:
    """Wait for this app to exit, run a non-elevated per-user setup, relaunch."""
    if os.name != "nt":
        raise OSError("The TubeSage Windows installer can only run on Windows.")
    pid = os.getpid()
    installer = _ps_quote(str(Path(installer_path).resolve()))
    app = _ps_quote(str(Path(app_path).resolve()))
    command = (
        f"$p=Get-Process -Id {pid} -ErrorAction SilentlyContinue; if($p){{$p.WaitForExit()}}; "
        f"$i=Start-Process -FilePath {installer} -ArgumentList @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART') -Wait -PassThru; "
        f"if($i.ExitCode -eq 0 -and (Test-Path {app})){{Start-Process -FilePath {app}}}"
    )
    encoded = base64.b64encode(command.encode("utf-16le")).decode("ascii")
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-EncodedCommand", encoded],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        startupinfo=startup, creationflags=subprocess.CREATE_NO_WINDOW,
        close_fds=True)
