"""Editable prompts in SQLite: seeding, the 2-old-versions / 45-day history, rollback, and the safety checks."""
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import db, llm, prompt_store
from app.config import settings

DATA = Path(__file__).resolve().parents[1] / "data"


def _edit(name, marker):
    """A small, valid change: append a line."""
    return prompt_store.apply(name, prompt_store.get(name) + f"\n- {marker}\n", "manual", "tester")


def test_seed_matches_the_files_exactly_so_replies_do_not_change():
    assert llm._load_base_prompt() == (DATA / "knowledge_base.md").read_text(encoding="utf-8")
    persona = (DATA / "whatsapp_persona.md").read_text(encoding="utf-8")
    style = (DATA / "whatsapp_style.md").read_text(encoding="utf-8")
    expected = f"{persona}\n\n{style}".replace("{name}", settings.whatsapp_persona_name).replace("{desk_phone}", llm.BOOKING_DESK_PHONE)
    assert llm._whatsapp_style() == expected
    assert prompt_store.live_versions() == {"knowledge_base": 1, "whatsapp_persona": 1, "whatsapp_style": 1}
    assert prompt_store.history("knowledge_base") == []


def test_change_goes_live_and_old_version_moves_to_history():
    before = prompt_store.get("whatsapp_style")
    assert _edit("whatsapp_style", "new rule") == 2
    assert "new rule" in prompt_store.get("whatsapp_style")
    assert "new rule" in llm._whatsapp_style()   # llm sees it without a restart
    [old] = prompt_store.history("whatsapp_style")
    assert old["version"] == 1 and old["days_left"] == 45
    assert db.query("SELECT content FROM prompt_history WHERE id = ?", (old["id"],))[0]["content"] == before


def test_history_keeps_only_the_two_most_recent_old_versions():
    for i in range(3):
        _edit("whatsapp_style", f"change {i}")   # live is now v4; v1 was the oldest
    assert [h["version"] for h in prompt_store.history("whatsapp_style")] == [3, 2]
    assert prompt_store.live_versions()["whatsapp_style"] == 4


def test_history_older_than_45_days_is_purged_even_below_the_cap():
    _edit("whatsapp_style", "a")
    old = (datetime.now(timezone.utc) - timedelta(days=46)).isoformat(timespec="seconds")
    with db.tx() as conn:
        conn.execute("UPDATE prompt_history SET retired_at = ?", (old,))
    assert prompt_store.purge_expired()["history"] == 1
    assert prompt_store.history("whatsapp_style") == []


def test_expired_history_is_dropped_on_the_next_change_too():
    _edit("whatsapp_style", "a")
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat(timespec="seconds")
    with db.tx() as conn:
        conn.execute("UPDATE prompt_history SET retired_at = ?", (old,))
    _edit("whatsapp_style", "b")
    assert [h["version"] for h in prompt_store.history("whatsapp_style")] == [2]


def test_rollback_swaps_and_is_itself_undoable():
    original = prompt_store.get("whatsapp_style")
    _edit("whatsapp_style", "bad idea")
    bad = prompt_store.get("whatsapp_style")
    [h] = prompt_store.history("whatsapp_style")
    assert prompt_store.rollback("whatsapp_style", h["id"], "tester") == 3
    assert prompt_store.get("whatsapp_style") == original
    [h2] = prompt_store.history("whatsapp_style")   # the bad version is now the one in history
    assert db.query("SELECT content FROM prompt_history WHERE id = ?", (h2["id"],))[0]["content"] == bad
    with pytest.raises(prompt_store.PromptChangeError):
        prompt_store.rollback("whatsapp_style", 9999, "tester")


@pytest.mark.parametrize("name,new,why", [
    ("price_overview", "x" * 500, "not an editable prompt"),
    ("whatsapp_style", "   ", "empty"),
    ("whatsapp_style", "tiny {desk_phone}", "shrinks"),
    ("knowledge_base", "x" * 60_000, "shrinks|required"),
])
def test_unsafe_changes_are_refused_and_nothing_changes(name, new, why):
    with pytest.raises(prompt_store.PromptChangeError, match=why):
        prompt_store.apply(name, new, "manual", "tester")
    assert prompt_store.history("whatsapp_style") == []
    assert prompt_store.live_versions().get("whatsapp_style") == 1


def test_dropping_a_placeholder_or_required_text_is_refused():
    style = prompt_store.get("whatsapp_style")
    with pytest.raises(prompt_store.PromptChangeError, match="placeholder"):
        prompt_store.apply("whatsapp_style", style.replace("{desk_phone}", "+91 1"), "manual", "t")
    kb = prompt_store.get("knowledge_base")
    with pytest.raises(prompt_store.PromptChangeError, match="required text"):
        prompt_store.apply("knowledge_base", kb.replace("## Scope & Safety Rules", "## Scope"), "manual", "t")


def test_failure_inside_the_swap_changes_nothing(monkeypatch):
    prompt_store.seed()

    def boom(conn, live, now):
        raise sqlite3.OperationalError("disk full")
    with monkeypatch.context() as patch:   # scoped: a bare undo() would also undo the per-test DB path
        patch.setattr(prompt_store, "_retire", boom)
        with pytest.raises(sqlite3.OperationalError):
            _edit("whatsapp_style", "x")
    prompt_store.reload()
    assert prompt_store.live_versions()["whatsapp_style"] == 1 and prompt_store.history("whatsapp_style") == []


def test_database_down_falls_back_to_the_file(monkeypatch, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setattr(settings, "db_path", str(blocker / "sub" / "bucky.db"))   # parent is a file: cannot be opened
    prompt_store.reload()
    assert prompt_store.get("whatsapp_style") == (DATA / "whatsapp_style.md").read_text(encoding="utf-8")


def test_drafts_approve_reject_and_feedback_status():
    with db.tx() as conn:
        for fid in ("fb_1", "fb_2"):
            conn.execute("INSERT INTO feedback VALUES (?, 'now', 'tone', NULL, NULL, NULL, '{}', 'proposed', 'bf_x', NULL)", (fid,))
    live = prompt_store.get("whatsapp_style")
    d1 = prompt_store.create_draft("whatsapp_style", live + "\n- A\n", "because", "fb_1")
    d2 = prompt_store.create_draft("whatsapp_style", live + "\n- B\n", "because", "fb_2")
    assert prompt_store.get("whatsapp_style") == live   # drafts are NOT live
    assert prompt_store.approve_draft(d1, "boss") == 2
    prompt_store.reject_draft(d2, "boss")
    status = {r["feedback_id"]: r["status"] for r in db.query("SELECT feedback_id, status FROM feedback")}
    assert status == {"fb_1": "applied", "fb_2": "wont_apply"}
    with pytest.raises(prompt_store.PromptChangeError):
        prompt_store.approve_draft(d1, "boss")   # already decided
