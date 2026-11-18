"""POST /api/v2/feedback: auth, validation, idempotency, privacy, and response_id from /api/v2/chat."""
import copy
import json

import pytest
from fastapi.testclient import TestClient

from app import db
from app.config import settings
from app.main import app

client = TestClient(app)
AUTH = {"Authorization": "Bearer test-chat-key"}


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setattr(settings, "chat_api_key", "test-chat-key")


def _item(**over):
    item = {
        "feedback_id": "fb_1", "submitted_at": "2026-11-15T10:42:11.000Z", "approved_at": "2026-11-15T11:05:40.000Z",
        "flagged": {"message_id": "m3", "response_id": "resp_abc", "text": "I can't find that offer", "delivered": False, "source": "conversation"},
        "feedback": {"category": "wrong_information", "expected_reply": "The 2,500 combo is 54M or 85M bungee + rafting.",
                     "note": "from our Instagram ad", "submitted_by": {"id": "a", "name": "Annu"}, "approved_by": {"id": "b", "name": "Rohan", "via": "whatsapp"}},
        "bucky": {"session_id": "wa-1", "request_id": "r1"},
        "conversation": {"id": "c1", "channel": "whatsapp", "customer": {"name": "Manish", "phone": "918860270303"}, "tags": ["Interested"]},
        "transcript": [{"id": "m1", "at": "t", "role": "customer", "type": "text", "text": "What is the combo?"},
                       {"id": "m3", "at": "t", "role": "bucky", "type": "text", "text": "I can't find that offer", "flagged": True}],
    }
    item.update(over)
    return item


def _post(item, headers=AUTH):
    return client.post("/api/v2/feedback", content=json.dumps(item), headers={**headers, "Content-Type": "application/json"})


def test_requires_the_crm_key(monkeypatch):
    assert _post(_item(), headers={}).status_code == 401
    assert _post(_item(), headers={"Authorization": "Bearer nope"}).status_code == 401
    monkeypatch.setattr(settings, "chat_api_key", "")
    assert _post(_item()).status_code == 503


def test_stores_and_acknowledges():
    r = _post(_item())
    assert r.status_code == 200
    body = r.json()
    assert body["received"] is True and body["ref"].startswith("bf_") and "duplicate" not in body
    [row] = db.query("SELECT * FROM feedback")
    assert (row["feedback_id"], row["category"], row["status"], row["response_id"], row["session_id"]) == \
           ("fb_1", "wrong_information", "received", "resp_abc", "wa-1")


def test_retry_is_a_duplicate_with_the_same_ref_and_stores_once():
    first = _post(_item()).json()
    again = _post(_item()).json()
    assert again == {"received": True, "duplicate": True, "ref": first["ref"]}
    assert len(db.query("SELECT 1 FROM feedback")) == 1


def test_customer_name_is_dropped_and_phone_masked_before_storage():
    _post(_item())
    stored = json.loads(db.query("SELECT payload_json FROM feedback")[0]["payload_json"])
    assert stored["conversation"]["customer"] == {"phone": "91886*****03"}
    assert "Manish" not in json.dumps(stored) and "918860270303" not in json.dumps(stored)


def test_an_already_masked_phone_is_left_alone():
    item = _item()
    item["conversation"]["customer"] = {"phone": "91886******03"}
    _post(item)
    stored = json.loads(db.query("SELECT payload_json FROM feedback")[0]["payload_json"])
    assert stored["conversation"]["customer"]["phone"] == "91886******03"


@pytest.mark.parametrize("mutate,expect", [
    (lambda i: i["feedback"].update(category="nonsense"), "category"),
    (lambda i: i["feedback"].update(expected_reply=""), "expected_reply"),
    (lambda i: i.pop("transcript"), "transcript"),
    (lambda i: i["flagged"].update(source="elsewhere"), "source"),
    (lambda i: i.update(feedback_id=""), "feedback_id"),
])
def test_invalid_bodies_get_422_with_a_reason_that_does_not_echo_customer_text(mutate, expect):
    item = copy.deepcopy(_item())
    mutate(item)
    r = _post(item)
    assert r.status_code == 422 and expect in r.json()["error"]
    assert "Instagram" not in r.json()["error"] and "combo" not in r.json()["error"]
    assert db.query("SELECT 1 FROM feedback") == []


def test_not_json_is_422():
    r = client.post("/api/v2/feedback", content=b"{oops", headers={**AUTH, "Content-Type": "application/json"})
    assert r.status_code == 422


def test_too_long_or_too_big_is_422(monkeypatch):
    monkeypatch.setattr(settings, "feedback_max_messages", 1)
    assert _post(_item()).status_code == 422
    monkeypatch.setattr(settings, "feedback_max_messages", 500)
    monkeypatch.setattr(settings, "feedback_max_bytes", 200)
    assert _post(_item()).status_code == 422


def test_database_failure_is_a_5xx_so_the_crm_retries(monkeypatch, tmp_path):
    blocker = tmp_path / "f"
    blocker.write_text("x")
    monkeypatch.setattr(settings, "db_path", str(blocker / "x" / "b.db"))   # cannot be opened
    assert _post(_item()).status_code == 503
