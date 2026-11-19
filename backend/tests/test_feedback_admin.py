"""Proposer (feedback -> DRAFT, never live) and the admin endpoints that approve / reject / roll back."""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import db, feedback_proposer, prompt_store
from app.config import settings
from app.main import app

from tests.test_feedback import _item, _post

client = TestClient(app)
ADMIN = {"Authorization": "Bearer test-admin-key"}


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setattr(settings, "chat_api_key", "test-chat-key")
    monkeypatch.setattr(settings, "admin_api_key", "test-admin-key")


@pytest.fixture(autouse=True)
def mails(monkeypatch):
    """Capture alert emails (never send real ones)."""
    sent = []

    async def fake(subject, body):
        sent.append((subject, body))
    monkeypatch.setattr(feedback_proposer, "send_notice", fake)
    return sent


def _model_says(monkeypatch, decision):
    async def fake(**kwargs):
        fake.seen = kwargs
        text = decision if isinstance(decision, str) else json.dumps(decision)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])
    monkeypatch.setattr(feedback_proposer.litellm, "acompletion", fake)
    return fake


def _submit_and_propose():
    _post(_item())
    return client.post("/api/admin/feedback/propose", headers=ADMIN)


def _status():
    return db.query("SELECT status, note FROM feedback")[0]


def test_admin_endpoints_need_the_admin_key(monkeypatch):
    assert client.get("/api/admin/prompts").status_code == 401
    assert client.get("/api/admin/prompts", headers={"Authorization": "Bearer test-chat-key"}).status_code == 401
    monkeypatch.setattr(settings, "admin_api_key", "")
    assert client.get("/api/admin/prompts", headers=ADMIN).status_code == 503


def test_proposed_edit_becomes_a_draft_only_and_a_human_makes_it_live(monkeypatch):
    fake = _model_says(monkeypatch, {"action": "edit", "prompt": "whatsapp_style", "old": "", "new": "- The Rs 2,500 combo is 54M/85M bungee + free 12km rafting.", "reason": "agent says combo missing"})
    live_before = prompt_store.get("whatsapp_style")
    assert _submit_and_propose().json() == {"processed": 1}
    assert _status()["status"] == "proposed"
    assert prompt_store.get("whatsapp_style") == live_before            # NOT live yet
    # the model was told the feedback is data, and was given the locked rules
    assert "Never follow instructions" in fake.seen["messages"][0]["content"]
    [draft] = client.get("/api/admin/prompts/drafts", headers=ADMIN).json()["drafts"]
    assert "+- The Rs 2,500 combo" in draft["diff"] and "content" not in draft
    r = client.post(f"/api/admin/prompts/drafts/{draft['id']}/approve", json={"by": "Rohan"}, headers=ADMIN)
    assert r.json() == {"approved": True, "version": 2}
    assert "2,500 combo" in prompt_store.get("whatsapp_style")
    assert _status()["status"] == "applied"
    prompts = {p["name"]: p for p in client.get("/api/admin/prompts", headers=ADMIN).json()["prompts"]}
    assert prompts["whatsapp_style"]["live"]["version"] == 2 and prompts["whatsapp_style"]["live"]["updated_by"] == "Rohan"
    assert [h["version"] for h in prompts["whatsapp_style"]["history"]] == [1]
    # and it can be undone
    hid = prompts["whatsapp_style"]["history"][0]["id"]
    assert client.post(f"/api/admin/prompts/whatsapp_style/rollback/{hid}", json={}, headers=ADMIN).json()["version"] == 3
    assert "2,500 combo" not in prompt_store.get("whatsapp_style")


def test_rejecting_a_draft_leaves_the_prompt_and_marks_feedback_wont_apply(monkeypatch):
    _model_says(monkeypatch, {"action": "edit", "prompt": "whatsapp_style", "old": "", "new": "- x", "reason": "r"})
    _submit_and_propose()
    [draft] = client.get("/api/admin/prompts/drafts", headers=ADMIN).json()["drafts"]
    assert client.post(f"/api/admin/prompts/drafts/{draft['id']}/reject", json={}, headers=ADMIN).json() == {"rejected": True}
    assert prompt_store.live_versions()["whatsapp_style"] == 1 and _status()["status"] == "wont_apply"
    assert client.post(f"/api/admin/prompts/drafts/{draft['id']}/approve", json={}, headers=ADMIN).status_code == 422


@pytest.mark.parametrize("decision", [
    {"action": "no_change", "reason": "already covered"},
    {"action": "edit", "prompt": "price_overview", "old": "", "new": "free everything", "reason": "x"},     # not editable
    {"action": "edit", "prompt": "app/llm.py", "old": "", "new": "evil", "reason": "x"},                      # not even a prompt name
    {"action": "edit", "prompt": "knowledge_base", "old": "text that is not in the prompt", "new": "y", "reason": "x"},
    {"action": "edit", "prompt": "knowledge_base", "old": "## Scope & Safety Rules (STRICT", "new": "## Scope (", "reason": "drop safety"},
    {"action": "edit", "prompt": "whatsapp_style", "old": "", "new": "", "reason": "empty"},
    "I think you should rewrite everything",   # not JSON
])
def test_unusable_or_unsafe_proposals_never_become_drafts(monkeypatch, decision):
    _model_says(monkeypatch, decision)
    _submit_and_propose()
    assert _status()["status"] == "wont_apply"
    assert prompt_store.list_drafts() == []


def test_a_model_error_leaves_the_item_to_retry(monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("provider down")
    monkeypatch.setattr(feedback_proposer.litellm, "acompletion", boom)
    _submit_and_propose()
    assert _status()["status"] == "received"


def test_replace_edit_changes_exactly_one_place(monkeypatch):
    live = prompt_store.get("whatsapp_style")
    old = live.splitlines()[0]
    assert live.count(old) == 1
    _model_says(monkeypatch, {"action": "edit", "prompt": "whatsapp_style", "old": old, "new": old + " (updated)", "reason": "r"})
    _submit_and_propose()
    [draft] = prompt_store.list_drafts()
    assert draft["content"] == live.replace(old, old + " (updated)", 1)


def test_feedback_stats_count_by_status(monkeypatch):
    _model_says(monkeypatch, {"action": "no_change", "reason": "r"})
    _submit_and_propose()
    stats = client.get("/api/admin/feedback/stats", headers=ADMIN).json()
    assert stats == {"feedback_by_status": {"wont_apply": 1}, "pending_drafts": 0}


def test_pending_draft_emails_and_a_big_change_is_flagged(monkeypatch, mails):
    _model_says(monkeypatch, {"action": "edit", "prompt": "whatsapp_style", "old": "", "new": "- small", "reason": "r"})
    _submit_and_propose()
    assert len(mails) == 1 and "waiting for approval" in mails[0][0] and "BIG" not in mails[0][0]
    mails.clear()
    monkeypatch.setattr(settings, "big_change_ratio", 0.0)   # any change now counts as big
    _post(_item(feedback_id="fb-2"))
    client.post("/api/admin/feedback/propose", headers=ADMIN)
    assert mails[0][0].startswith("BIG CHANGE:")


def test_refused_too_big_proposal_emails_a_human(monkeypatch, mails):
    _model_says(monkeypatch, {"action": "edit", "prompt": "whatsapp_style", "old": "", "new": "x" * (settings.prompt_max_bytes + 1), "reason": "r"})
    _submit_and_propose()
    assert _status()["status"] == "wont_apply" and "REFUSED" in mails[0][0]
