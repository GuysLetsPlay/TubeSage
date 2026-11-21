"""Error Doctor: signature extraction + auto Telegram/GitHub search.

Telegram search wraps the authorized MTProto reader in
C:\\Dev\\telegram-reader (see its AGENTS.md). GitHub search uses the public
REST API (no key needed).
"""
from __future__ import annotations

import json
import re
import subprocess
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

TG_PYTHON = Path(r"C:\Dev\telegram-reader\.venv\Scripts\python.exe")
TG_DIR = Path(r"C:\Dev\telegram-reader")
DEFAULT_GROUP = "YTDLNIS"
DEFAULT_TOPIC = "general"
UA = {"User-Agent": "TubeSage/1.0"}

# well-known yt-dlp error families -> good search terms
KNOWN_PATTERNS: list[tuple[str, str]] = [
    (r"sign in to confirm", "sign in to confirm you're not a bot cookies"),
    (r"http error 403", "HTTP Error 403"),
    (r"http error 429", "HTTP Error 429 rate limit"),
    (r"unable to extract", "Unable to extract"),
    (r"nsig extraction failed", "nsig extraction failed"),
    (r"requested format is not available", "Requested format is not available"),
    (r"player response", "player response"),
    (r"unsupported url", "Unsupported URL"),
    (r"failed to update", "failed to update"),
    (r"sponsorblock", "sponsorblock"),
    (r"live event will begin", "live event"),
    (r"this video is unavailable", "video unavailable"),
    (r"age.?restricted", "age restricted cookies"),
    (r"ffmpeg", "ffmpeg"),
    (r"certificate verify failed", "SSL certificate verify failed"),
    (r"no video formats", "no video formats"),
    (r"throttl", "throttling n-sig"),
]


@dataclass
class SearchResult:
    source: str            # "telegram" | "github"
    title: str
    body: str
    url: str = ""
    extra: str = ""


@dataclass
class DoctorReport:
    error_text: str = ""
    signature: str = ""
    queries: list[str] = field(default_factory=list)
    results: list[SearchResult] = field(default_factory=list)


# ------------------------------------------------------------ signature ----
def extract_signature(error_text: str) -> str:
    """The distinctive last ERROR line, stripped of volatile parts."""
    if not error_text:
        return ""
    lines = [l.strip() for l in error_text.splitlines() if l.strip()]
    err_lines = [l for l in lines if "ERROR" in l.upper()] or lines
    line = err_lines[-1] if err_lines else lines[-1]
    line = re.sub(r"https?://\S+", "<url>", line)
    line = re.sub(r"\b[\w-]{11}\b(?=\s|$)", "<id>", line)      # 11-char video ids
    line = re.sub(r"[A-Za-z]:\\[^\s]+", "<path>", line)        # windows paths
    line = re.sub(r"\d+", "<n>", line)
    return line


def generate_queries(error_text: str) -> list[str]:
    """1-4 short, high-signal search queries for the error."""
    queries: list[str] = []
    for pattern, term in KNOWN_PATTERNS:
        if re.search(pattern, error_text, re.IGNORECASE):
            queries.append(term)
            break
    sig = extract_signature(error_text)
    # keywords from the signature, drop yt-dlp boilerplate
    words = [w for w in re.findall(r"[A-Za-z_]{4,}", sig)
             if w.lower() not in {
                 "error", "download", "unable", "requested", "warning",
                 "ytdlp", "ffmpeg", "windows", "python", "player"}]
    if len(words) >= 2:
        queries.append(" ".join(words[:6]))
    # raw message after ERROR: is often verbatim-searchable
    m = re.search(r"ERROR:\s*(.{10,120})", error_text)
    if m:
        raw = re.sub(r"https?://\S+", " ", m.group(1))
        raw = re.sub(r"\s+", " ", raw).strip()
        if raw and raw not in queries:
            queries.append(raw[:100])
    return queries[:4] or [extract_signature(error_text)[:80] or "download error"]


# ------------------------------------------------------------- telegram ----
def search_telegram(query: str, limit: int = 20,
                    group: str = DEFAULT_GROUP,
                    topic: str = DEFAULT_TOPIC) -> list[SearchResult]:
    """Search the YTDLNIS group via the authorized MTProto reader."""
    if not TG_PYTHON.exists():
        return [SearchResult("telegram", "telegram-reader not found",
                             f"Expected at {TG_PYTHON}")]
    script = TG_DIR / "search.py"
    cmd = [str(TG_PYTHON), str(script), group, query, str(limit)]
    if topic:
        cmd.append(topic)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120, cwd=str(TG_DIR),
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception as exc:
        return [SearchResult("telegram", "search failed", str(exc))]
    out = proc.stdout or ""
    results: list[SearchResult] = []
    pat = re.compile(
        r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\] id=(\d+) ([^:]+): (.*)")
    for line in out.splitlines():
        m = pat.match(line.strip())
        if m:
            date, mid, who, text = m.groups()
            results.append(SearchResult(
                "telegram", f"{who} · {date}", text, extra=mid))
    if proc.returncode != 0 and not results:
        tail = (proc.stderr or out).strip().splitlines()
        msg = tail[-1] if tail else f"exit code {proc.returncode}"
        results.append(SearchResult("telegram", "no results / error", msg))
    return results



def read_recent_topic(limit: int = 40, group: str = DEFAULT_GROUP,
                      topic: str = DEFAULT_TOPIC) -> list[SearchResult]:
    """Read the newest messages of a group topic (context for the doctor)."""
    if not TG_PYTHON.exists():
        return []
    script = TG_DIR / "read_topic.py"
    try:
        proc = subprocess.run(
            [str(TG_PYTHON), str(script), group, topic, str(limit)],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120, cwd=str(TG_DIR),
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        return []
    results: list[SearchResult] = []
    pat = re.compile(
        r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\] id=(\d+) ([^:]+): (.*)")
    for line in (proc.stdout or "").splitlines():
        m = pat.match(line.strip())
        if m:
            date, mid, who, text = m.groups()
            results.append(SearchResult("telegram", f"{who} · {date}", text, extra=mid))
    return results


# --------------------------------------------------------------- github ----
def search_github(query: str, per_page: int = 10) -> list[SearchResult]:
    """Search yt-dlp issues via the public GitHub search API."""
    q = f"repo:yt-dlp/yt-dlp {query}"
    url = ("https://api.github.com/search/issues?q="
           + urllib.parse.quote(q)
           + f"&per_page={per_page}&sort=relevance")
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:
        return [SearchResult("github", "GitHub search failed", str(exc))]
    results: list[SearchResult] = []
    for item in data.get("items", []):
        body = (item.get("body") or "").replace("\r", "")
        body = re.sub(r"\s+", " ", body)[:400]
        label = "PR" if "pull_request" in item else "issue"
        state = item.get("state", "?")
        results.append(SearchResult(
            "github",
            f"[{label} #{item.get('number')}] {item.get('title', '')}",
            body,
            url=item.get("html_url", ""),
            extra=f"{state} · comments: {item.get('comments', 0)}"))
    return results


# ------------------------------------------------------------- one-shot ----
def run_auto_search(error_text: str, progress_cb=None) -> DoctorReport:
    """Auto search: Telegram + GitHub for all generated queries."""
    report = DoctorReport(error_text=error_text,
                          signature=extract_signature(error_text))
    report.queries = generate_queries(error_text)
    seen: set[tuple] = set()
    for q in report.queries:
        if progress_cb:
            progress_cb(f"Searching Telegram: {q}")
        for r in search_telegram(q):
            key = ("tg", r.extra, r.title)
            if key not in seen:
                seen.add(key)
                report.results.append(r)
        if progress_cb:
            progress_cb(f"Searching GitHub: {q}")
        for r in search_github(q):
            key = ("gh", r.url)
            if key not in seen:
                seen.add(key)
                report.results.append(r)
    return report

