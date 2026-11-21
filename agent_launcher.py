"""Launch a visible CLI agent (codex / cline / opencode) for deep error search.

The agent receives a one-line instruction pointing at a fully detailed
prompt file (rendered from AGENT_PROMPT.md) and writes its solution to
knowledge/pending/, which the app imports automatically.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from app_paths import DATA_DIR, RESOURCE_DIR

BASE_DIR = DATA_DIR
PENDING_DIR = DATA_DIR / "knowledge" / "pending"
PROMPT_TEMPLATE = RESOURCE_DIR / "AGENT_PROMPT.md"

CLI_CANDIDATES = ["codex", "cline", "opencode"]

# how each CLI is invoked with an initial instruction (one line, quoted)
_CLI_LAUNCH = {
    "codex":    ['{cli}', '{one liner}'],
    "cline":    ['{cli}', '{one liner}'],
    "opencode": ['{cli}', '{one liner}'],
}


def detect_cli(preferred: str | None = None) -> str | None:
    if preferred and shutil.which(preferred):
        return preferred
    for name in CLI_CANDIDATES:
        if shutil.which(name):
            return name
    return None


def render_prompt(error_text: str, yt_command: str, signature: str,
                  pending_path: Path, cli: str) -> str:
    template = PROMPT_TEMPLATE.read_text(encoding="utf-8") \
        if PROMPT_TEMPLATE.exists() else "Investigate this yt-dlp error: __ERROR_TEXT__"
    return (template
            .replace("__ERROR_TEXT__", error_text.strip() or "(no stderr captured)")
            .replace("__YT_COMMAND__", yt_command.strip() or "yt-dlp <url>")
            .replace("__SIGNATURE__", signature or "(unknown)")
            .replace("__PENDING_PATH__", str(pending_path))
            .replace("__CLI__", cli)
            .replace("__DATE__", time.strftime("%Y-%m-%d")))


def launch_deep_search(error_text: str, yt_command: str, signature: str,
                       cli: str | None = None) -> tuple[bool, str]:
    """Open a visible terminal window running the CLI agent.

    Returns (success, message). The agent writes its result to
    knowledge/pending/<id>.md; the app polls that folder.
    """
    cli = detect_cli(cli)
    if not cli:
        return False, ("No CLI agent found. Install one of: "
                       + ", ".join(CLI_CANDIDATES))

    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    pending_path = PENDING_DIR / f"{stamp}-solution.md"
    prompt_path = PENDING_DIR / f"{stamp}-PROMPT.md"
    prompt = render_prompt(error_text, yt_command, signature, pending_path, cli)
    prompt_path.write_text(prompt, encoding="utf-8")

    # NOTE: no double quotes here — the whole line is passed inside a
    # quoted cmd argument; paths in C:\Dev have no spaces.
    one_liner = (
        f'Read the file {prompt_path} and follow it exactly. '
        f'It is a yt-dlp error investigation task: search the YTDLNIS Telegram '
        f'group and GitHub issues as described, then write the solution to '
        f'{pending_path}. Work autonomously until that file exists.'
    )

    inner = _CLI_LAUNCH.get(cli, ['{cli}', '{one liner}'])
    args = [a.replace("{cli}", cli).replace("{one liner}", one_liner) for a in inner]
    # cmd-safe quoting: wrap each arg that contains spaces
    inner_cmd = " ".join(
        f'"{a}"' if (" " in a and not a.startswith('"')) else a for a in args)
    cmd_line = f"cmd /k {inner_cmd}"

    try:
        if shutil.which("wt"):
            subprocess.Popen(
                ["wt.exe", "new-tab", "--title", "TubeSage Error Agent",
                 "cmd", "/k", *args],
                cwd=str(BASE_DIR), env=os.environ.copy())
        else:
            subprocess.Popen(
                cmd_line, shell=True, cwd=str(BASE_DIR),
                creationflags=subprocess.CREATE_NEW_CONSOLE,
                env=os.environ.copy())
    except Exception as exc:
        return False, f"Failed to open terminal: {exc}"
    return True, (
        f"Opened a terminal with {cli}. It will search the YTDLNIS Telegram "
        f"group + GitHub and write its fix to:\n{pending_path}\n"
        f"The app imports it automatically when it appears.")
