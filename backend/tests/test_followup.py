"""POST /api/v2/followup: text for a silent chat, with the CRM told whether/when to send."""
import pytest
from fastapi.testclient import TestClient

from app import llm as llm_module
from app import session_store
from app.config import settings
from app.main import app
from app.rate_limit import _hits

client = TestClient(app)
AUTH = {"Authorization": "Bearer k"}
BOT_LAST = [{"role": "user", "content": "bungee price?"}, {"role": "assistant", "content": "Splash 85m is ₹2,200. Which date?"}]


class _Resp:
    def __init__(self, text):
        self.choices = [type("C", (), {"message": type("M", (), {"content": text})()})()]


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    _hits.clear()
    monkeypatch.setattr(settings, "chat_api_key", "k")
    monkeypatch.setattr(session_store, "redis_client", None)
    yield
    _hits.clear()


def _model(monkeypatch, text="Shall I check slots for your date? 🙂"):
    seen = {}

    async def fake(*_a, **kw):
        seen["messages"] = kw["messages"]
        return _Resp(text)
    monkeypatch.setattr(llm_module.litellm, "acompletion", fake)
    monkeypatch.setattr("app.followup.litellm.acompletion", fake)
    return seen


def _post(messages, attempt, headers=AUTH):
    return client.post("/api/v2/followup", headers=headers, json={"messages": messages, "attempt": attempt, "session_id": "wa-f-1"})


def test_requires_the_key():
    assert _post(BOT_LAST, 1, headers={}).status_code == 401


def test_schedule_and_flags_per_attempt(monkeypatch):
    _model(monkeypatch)
    got = [_post(BOT_LAST, n).json() for n in (1, 2, 3, 4)]
    assert [g["next_in_minutes"] for g in got] == [45, 300, 1080, None]       # 15, 60, 360, 1440 minutes in total
    assert [g["template_required"] for g in got] == [False, False, False, True]  # the 1-day one is outside the 24 h window
    assert all(g["send"] and g["total"] == 4 for g in got)


def test_reply_is_text_without_emoji_and_the_guide_for_that_attempt_is_sent(monkeypatch):
    seen = _model(monkeypatch)
    r = _post(BOT_LAST, 4).json()
    assert r["reply"] == "Shall I check slots for your date?"
    system = str(seen["messages"][0]["content"])
    assert "the last message" in system and "NO emoji" in system
    assert seen["messages"][-1]["role"] == "user"        # never end on an assistant message (that is a prefill)


@pytest.mark.parametrize("messages,attempt,reason", [
    (BOT_LAST + [{"role": "user", "content": "yes"}], 1, "customer_replied"),
    (BOT_LAST[:1] + [{"role": "assistant", "content": "x"}] + [{"role": "user", "content": "stop messaging me"},
                                                              {"role": "assistant", "content": "ok"}], 1, "opted_out"),
    (BOT_LAST, 5, "sequence_finished"),
    ([{"role": "assistant", "content": "Hi!"}], 1, "no_customer_message"),
])
def test_skips_without_calling_the_model(monkeypatch, messages, attempt, reason):
    async def boom(*_a, **_k):
        raise AssertionError("the model must not be called")
    monkeypatch.setattr("app.followup.litellm.acompletion", boom)
    r = _post(messages, attempt).json()
    assert r["send"] is False and r["skip_reason"] == reason and r["reply"] == ""


def test_model_failure_is_reported_in_band(monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("down")
    monkeypatch.setattr("app.followup.litellm.acompletion", boom)
    r = _post(BOT_LAST, 1)
    assert r.status_code == 200 and r.json()["send"] is False and r.json()["error"] == "followup_failed"
