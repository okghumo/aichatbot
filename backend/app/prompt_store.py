"""Editable prompts, stored in SQLite with a short undo history.

What lives here: ONLY the three prompt files a human may let the feedback loop tune
(EDITABLE). Everything else (code, tool schemas, handoff/safety prompts in llm.py, price_overview.md,
offers data) is NOT reachable through this module, whatever the model proposes.

Versioning rule (one transaction per change, see _apply):
  - `prompts` holds the LIVE prompt, one row per name.
  - Changing a prompt moves the old live row into `prompt_history`; history keeps at most
    PROMPT_HISTORY_KEEP rows per name (oldest deleted first) and PROMPT_HISTORY_DAYS days.
  - Rollback swaps a history row with the live one, so a rollback is itself undoable.
If the database cannot be read, get() falls back to the file in git: Bucky never runs without a prompt.
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import db
from app.config import settings

logger = logging.getLogger(__name__)

EDITABLE = ("knowledge_base", "whatsapp_persona", "whatsapp_style")
_PLACEHOLDERS = ("{name}", "{desk_phone}")

_cache: dict[str, str] = {}
_versions: dict[str, int] = {}


class PromptChangeError(ValueError):
    """A proposed/requested prompt change was refused (reason in the message)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cutoff() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=settings.prompt_history_days)).isoformat(timespec="seconds")


def _file(name: str) -> Path:
    base = Path(settings.system_prompt_file)
    return base if name == "knowledge_base" else base.parent / f"{name}.md"


def _file_text(name: str) -> str | None:
    path = _file(name)
    return path.read_text(encoding="utf-8") if path.exists() else None


def _must_keep(name: str) -> list[str]:
    path = Path(settings.system_prompt_file).parent / "locked" / "must_keep.json"
    try:
        return list(json.loads(path.read_text(encoding="utf-8")).get(name, []))
    except (OSError, ValueError):
        return []


def reload() -> None:
    """Forget the in-memory copies; the next get() re-reads the live rows."""
    _cache.clear()
    _versions.clear()


def seed() -> None:
    """Insert version 1 from the files in git for any prompt that has no live row yet. History starts empty."""
    for name in EDITABLE:
        text = _file_text(name)
        if text is None:
            continue
        with db.tx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO prompts (name, version, content, source, updated_by, updated_at) "
                "VALUES (?, 1, ?, 'seed', 'system', ?)", (name, text, _now()))


def get(name: str) -> str | None:
    """The live prompt text (cached in memory), or the git file if the database is unavailable. None = no such prompt."""
    if name in _cache:
        return _cache[name]
    try:
        rows = db.query("SELECT content, version FROM prompts WHERE name = ?", (name,))
        if not rows:
            seed()
            rows = db.query("SELECT content, version FROM prompts WHERE name = ?", (name,))
        if rows:
            _cache[name], _versions[name] = rows[0]["content"], rows[0]["version"]
            return _cache[name]
    except Exception:  # pylint: disable=broad-except
        logger.exception("Prompt store unreadable; using the file for %s", name)
        return _file_text(name)
    return _file_text(name)


def live_versions() -> dict[str, int]:
    """{name: version} for every editable prompt currently loaded (what recent replies were built from)."""
    for name in EDITABLE:
        get(name)
    return dict(_versions)


def check_change(name: str, old: str | None, new: str) -> None:
    """Refuse changes that could break Bucky. Raises PromptChangeError."""
    if name not in EDITABLE:
        raise PromptChangeError(f"'{name}' is not an editable prompt")
    if not new.strip():
        raise PromptChangeError("new prompt is empty")
    if len(new.encode("utf-8")) > settings.prompt_max_bytes:
        raise PromptChangeError(f"new prompt is larger than {settings.prompt_max_bytes} bytes")
    if old and len(new) < settings.prompt_min_ratio * len(old):
        raise PromptChangeError(f"new prompt shrinks the old one below {int(settings.prompt_min_ratio * 100)}% of its length")
    for token in _PLACEHOLDERS:
        if old and token in old and token not in new:
            raise PromptChangeError(f"new prompt dropped the placeholder {token}")
    for marker in _must_keep(name):
        if marker not in new:
            raise PromptChangeError(f"new prompt dropped required text: {marker!r}")


def _retire(conn, live, now: str) -> None:
    """Move the live row into history, first making room: drop expired rows, then the oldest ones past the cap."""
    conn.execute("DELETE FROM prompt_history WHERE name = ? AND retired_at < ?", (live["name"], _cutoff()))
    rows = conn.execute("SELECT id FROM prompt_history WHERE name = ? ORDER BY retired_at, id", (live["name"],)).fetchall()
    for row in rows[: max(0, len(rows) - settings.prompt_history_keep + 1)]:
        conn.execute("DELETE FROM prompt_history WHERE id = ?", (row["id"],))
    conn.execute(
        "INSERT INTO prompt_history (name, version, content, source, retired_at) VALUES (?, ?, ?, ?, ?)",
        (live["name"], live["version"], live["content"], live["source"], now))


def _apply(conn, name: str, content: str, source: str, by: str, *, validate: bool = True) -> int:
    live = conn.execute("SELECT * FROM prompts WHERE name = ?", (name,)).fetchone()
    if validate:
        check_change(name, live["content"] if live else None, content)
    now = _now()
    if live is None:
        conn.execute("INSERT INTO prompts VALUES (?, 1, ?, ?, ?, ?)", (name, content, source, by, now))
        return 1
    _retire(conn, live, now)
    version = live["version"] + 1
    conn.execute("UPDATE prompts SET version=?, content=?, source=?, updated_by=?, updated_at=? WHERE name=?",
                 (version, content, source, by, now, name))
    return version


def apply(name: str, content: str, source: str, by: str) -> int:
    """Make `content` the live prompt; the old live prompt goes to history. Returns the new version."""
    seed()   # so the change is checked against (and retires) the real current prompt, never inserted as a bare v1
    with db.tx() as conn:
        version = _apply(conn, name, content, source, by)
    reload()
    return version


def rollback(name: str, history_id: int, by: str) -> int:
    """Swap: the chosen history prompt becomes live (as a new version number), the live one goes to history."""
    with db.tx() as conn:
        old = conn.execute("SELECT * FROM prompt_history WHERE id = ? AND name = ?", (history_id, name)).fetchone()
        if old is None:
            raise PromptChangeError("no such history entry for this prompt")
        conn.execute("DELETE FROM prompt_history WHERE id = ?", (history_id,))
        version = _apply(conn, name, old["content"], f"rollback:v{old['version']}", by, validate=False)
    reload()
    return version


def history(name: str) -> list[dict]:
    rows = db.query("SELECT id, version, source, retired_at FROM prompt_history WHERE name = ? ORDER BY retired_at DESC, id DESC", (name,))
    out = []
    for row in rows:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["retired_at"])).days
        out.append({**dict(row), "days_left": max(0, settings.prompt_history_days - age)})
    return out


def create_draft(name: str, content: str, reason: str, feedback_id: str | None) -> int:
    """Store a proposed change (NOT live). Refused if it would fail check_change against the live prompt."""
    check_change(name, get(name), content)
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO prompt_drafts (name, content, reason, feedback_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (name, content, reason, feedback_id, _now()))
        return cur.lastrowid


def list_drafts(status: str = "pending") -> list[dict]:
    return [dict(r) for r in db.query("SELECT * FROM prompt_drafts WHERE status = ? ORDER BY id", (status,))]


def approve_draft(draft_id: int, by: str) -> int:
    """Draft -> live, old live -> history, feedback -> applied: all in one transaction."""
    with db.tx() as conn:
        draft = conn.execute("SELECT * FROM prompt_drafts WHERE id = ? AND status = 'pending'", (draft_id,)).fetchone()
        if draft is None:
            raise PromptChangeError("no pending draft with that id")
        source = f"feedback:{draft['feedback_id']}" if draft["feedback_id"] else "manual"
        version = _apply(conn, draft["name"], draft["content"], source, by)
        conn.execute("UPDATE prompt_drafts SET status='approved', decided_by=?, decided_at=? WHERE id=?", (by, _now(), draft_id))
        if draft["feedback_id"]:
            conn.execute("UPDATE feedback SET status='applied', note=? WHERE feedback_id=?",
                         (f"{draft['name']} v{version}", draft["feedback_id"]))
    reload()
    return version


def reject_draft(draft_id: int, by: str) -> None:
    with db.tx() as conn:
        draft = conn.execute("SELECT * FROM prompt_drafts WHERE id = ? AND status = 'pending'", (draft_id,)).fetchone()
        if draft is None:
            raise PromptChangeError("no pending draft with that id")
        conn.execute("UPDATE prompt_drafts SET status='rejected', decided_by=?, decided_at=? WHERE id=?", (by, _now(), draft_id))
        if draft["feedback_id"]:
            conn.execute("UPDATE feedback SET status='wont_apply' WHERE feedback_id=?", (draft["feedback_id"],))


def purge_expired() -> dict:
    """Delete history older than PROMPT_HISTORY_DAYS and decided drafts older than the same."""
    cutoff = _cutoff()
    with db.tx() as conn:
        h = conn.execute("DELETE FROM prompt_history WHERE retired_at < ?", (cutoff,)).rowcount
        d = conn.execute("DELETE FROM prompt_drafts WHERE status != 'pending' AND decided_at < ?", (cutoff,)).rowcount
    return {"history": h, "drafts": d}


def log_response(response_id: str, session_id: str, request_id: str | None) -> None:
    """Remember which prompt versions produced a reply, so a flagged reply can be traced. Never raises."""
    try:
        versions = json.dumps(live_versions())   # before the transaction: it may itself seed (write) on a cold start
        with db.tx() as conn:
            conn.execute("INSERT OR IGNORE INTO response_log VALUES (?, ?, ?, ?, ?)",
                         (response_id, session_id, request_id, _now(), versions))
    except Exception:  # pylint: disable=broad-except
        logger.exception("Could not log response %s", response_id)


async def maintenance_loop() -> None:
    """Daily: seed on first run, then purge history/drafts past their 45 days."""
    import asyncio   # local: the rest of this module is synchronous
    while True:
        try:
            await asyncio.to_thread(seed)
            await asyncio.to_thread(purge_expired)
        except Exception:  # pylint: disable=broad-except
            logger.exception("Prompt maintenance failed")
        await asyncio.sleep(86_400)
