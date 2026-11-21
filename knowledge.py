"""Local knowledge base of error solutions for TubeSage.

Store: knowledge/solutions.json
Pending agent output: knowledge/pending/*.md  (imported + archived)
"""
from __future__ import annotations

import difflib
import json
import re
import time
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
KB_DIR = BASE_DIR / "knowledge"
PENDING_DIR = KB_DIR / "pending"
PROCESSED_DIR = KB_DIR / "processed"
STORE = KB_DIR / "solutions.json"

for _d in (KB_DIR, PENDING_DIR, PROCESSED_DIR):
    _d.mkdir(parents=True, exist_ok=True)

_P = {"store": None}  # cache


def _load() -> list[dict]:
    if _P["store"] is None:
        if STORE.exists():
            try:
                _P["store"] = json.loads(STORE.read_text(encoding="utf-8"))
            except Exception:
                _P["store"] = []
        else:
            _P["store"] = []
    return _P["store"]


def _save(entries: list[dict]) -> None:
    STORE.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
    _P["store"] = entries


def add_solution(signature: str, error_text: str, solution_md: str,
                 source: str = "agent") -> dict:
    """Add a solution, or update the existing entry with the same signature."""
    entries = _load()
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    sig_key = normalize_signature(signature)
    for e in entries:
        if normalize_signature(e["signature"]) == sig_key:
            e["solution_md"] = solution_md
            e["error_text"] = error_text or e["error_text"]
            e["source"] = source
            e["updated"] = now
            _save(entries)
            return e
    entry = {
        "id": uuid.uuid4().hex[:12],
        "signature": signature.strip(),
        "error_text": error_text.strip(),
        "solution_md": solution_md,
        "source": source,
        "status": "new",          # new | confirmed | rejected
        "worked": 0,
        "failed": 0,
        "created": now,
        "updated": now,
    }
    entries.append(entry)
    _save(entries)
    return entry


def mark_worked(entry_id: str) -> None:
    for e in _load():
        if e["id"] == entry_id:
            e["worked"] += 1
            e["status"] = "confirmed"
            e["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _save(_load())


def mark_failed(entry_id: str) -> None:
    for e in _load():
        if e["id"] == entry_id:
            e["failed"] += 1
            if e["failed"] >= 2 and e["status"] != "confirmed":
                e["status"] = "rejected"
            e["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _save(_load())


def normalize_signature(sig: str) -> str:
    s = sig.lower()
    s = re.sub(r"https?://\S+", " ", s)
    s = re.sub(r"\d+", "#", s)
    s = re.sub(r"[^a-z# ]+", " ", s)
    return " ".join(s.split())


def _score(entry: dict, error_text: str, signature: str) -> float:
    """0..1 similarity between a stored entry and the current error."""
    sig_norm = normalize_signature(signature)
    ent_norm = normalize_signature(entry["signature"])
    if not sig_norm or not ent_norm:
        return 0.0
    ratio = difflib.SequenceMatcher(None, ent_norm, sig_norm).ratio()
    # keyword overlap on raw error text
    words = {w for w in re.findall(r"[a-z_]{5,}", error_text.lower())}
    ent_words = set(re.findall(r"[a-z_]{5,}", entry["error_text"].lower()))
    overlap = (len(words & ent_words) / max(1, len(words | ent_words))) if words else 0
    score = max(ratio, 0.7 * ratio + 0.3 * overlap)
    # confirmed solutions get a boost in ranking (not in score itself)
    return score


def find_matches(error_text: str, signature: str,
                 threshold: float = 0.45) -> list[dict]:
    """Best-first list of KB entries similar to this error."""
    scored = []
    for e in _load():
        s = _score(e, error_text, signature)
        if s >= threshold:
            rank = (e["status"] == "confirmed", -e["failed"], s)
            scored.append((rank, s, e))
    scored.sort(key=lambda t: t[0], reverse=True)
    out = []
    for _, s, e in scored:
        item = dict(e)
        item["score"] = round(s, 3)
        out.append(item)
    return out


# ---------------------------------------------------------------- pending ---
PENDING_HEADER = re.compile(r"^<!--\s*signature:\s*(.+?)\s*-->", re.IGNORECASE)


def import_pending() -> list[dict]:
    """Import solution markdown files written by the CLI agent."""
    imported = []
    for md in list(PENDING_DIR.glob("*.md")):
        text = md.read_text(encoding="utf-8", errors="replace")
        m = PENDING_HEADER.search(text)
        signature = m.group(1) if m else md.stem
        error_text = ""
        em = re.search(r"<!--\s*error:\s*(.+?)\s*-->", text, re.IGNORECASE)
        if em:
            error_text = em.group(1)
        entry = add_solution(signature, error_text, text, source="agent")
        imported.append(entry)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        md.rename(PROCESSED_DIR / f"{stamp}-{md.name}")
    return imported
