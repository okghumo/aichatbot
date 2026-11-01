"""WhatsApp emojis are rare: at most one per reply, none right after a bot reply that had one."""
import json

import pytest
from fastapi.testclient import TestClient

from app import session_store
from app.config import settings
from app.emoji_limit import limit_emojis, strip_emojis
from app.main import app
from app.price_overview import price_overview_text
from app.rate_limit import _hits

client = TestClient(app)
AUTH = {"Authorization": "Bearer k"}


def test_one_emoji_kept_extra_ones_cut():
    assert limit_emojis("Great 🙌 see you 🪂 soon 🙂", []) == "Great 🙌 see you soon"
    assert limit_emojis("Perfect 🙂", []) == "Perfect 🙂"
    assert limit_emojis("No emoji here", []) == "No emoji here"


def test_none_after_a_recent_emoji():
    assert limit_emojis("Great 🙌 and more 🪂", ["Hi", "Hello 🙂"]) == "Great and more"
    assert limit_emojis("Great 🙌", ["Hello 🙂", "Hi"]) == "Great 🙌"   # only the previous bot reply counts


def test_rupee_and_text_symbols_are_kept():
    assert strip_emojis("Price ₹2,000 → 85m 🪂") == "Price ₹2,000 → 85m"


@pytest.fixture
def _setup(monkeypatch):
    _hits.clear()
    monkeypatch.setattr(settings, "chat_api_key", "k")
    monkeypatch.setattr(session_store, "redis_client", None)
    yield
    _hits.clear()


def _chat(monkeypatch, reply, history):
    async def _s(_m, _sid, channel=None, handoff_state=None):
        yield f"data: {json.dumps({'delta': reply, 'done': False})}\n\n"
        yield f"data: {json.dumps({'delta': '', 'done': True})}\n\n"
    monkeypatch.setattr("app.crm_chat.stream_chat_response", _s)
    msgs = history + [{"role": "user", "content": "ok"}]
    return client.post("/api/v2/chat", headers=AUTH, json={"messages": msgs, "session_id": "wa-e-1"}).json()["reply"]


def test_v2_reply_has_no_emoji_after_one_and_the_fixed_message_is_untouched(monkeypatch, _setup):
    prior = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "Hello 🙂"}]
    assert _chat(monkeypatch, "Sure 🙌 which date?", prior) == "Sure which date?"
    assert _chat(monkeypatch, "Sure 🙌 which date?", prior[:1]) == "Sure 🙌 which date?"
    assert _chat(monkeypatch, price_overview_text(), prior) == price_overview_text()
