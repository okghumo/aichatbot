"""WhatsApp (POST /api/v2/chat) must never log in or touch the cart, even if the model tries: the tools are
blocked where they would run (the web chat keeps them)."""
import json

import pytest
from fastapi.testclient import TestClient

from app import llm as llm_module
from app import mcp_client, session_store, token_store
from app.config import settings
from app.main import app
from app.rate_limit import _hits

client = TestClient(app)
AUTH = {"Authorization": "Bearer k"}


class _Stream:
    def __init__(self, content=None, tool=None):
        delta = type("D", (), {
            "content": content,
            "tool_calls": [type("T", (), {"index": 0, "id": "c", "function": type(
                "F", (), {"name": tool[0], "arguments": json.dumps(tool[1])})()})()] if tool else None,
        })()
        self._chunks = [type("C", (), {"choices": [type("Ch", (), {"delta": delta})()]})()]

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


def _install(monkeypatch, script):
    """script: list of ('tool', name, args) | ('text', str), consumed one per model call."""
    seen = {"system": [], "mcp_calls": []}

    async def no_tools():
        return []

    async def fake_session():
        class _Stack:
            async def aclose(self):
                pass
        return _Stack(), object()

    async def fake_mcp(call, session=None):
        args = json.loads(call.function.arguments)
        seen["mcp_calls"].append((call.function.name, args))
        if call.function.name == "verify_otp":
            return {"result": json.dumps({"authToken": "tok-123"})}
        return {"result": json.dumps({"success": True})}

    async def fake_acompletion(*_a, **kw):
        seen["system"].append(json.dumps(kw["messages"][0]["content"]))
        kind = script.pop(0)
        return _Stream(tool=(kind[1], kind[2])) if kind[0] == "tool" else _Stream(content=kind[1])

    monkeypatch.setattr(llm_module, "load_catalog_tools", no_tools)
    monkeypatch.setattr(mcp_client, "_fresh_session", fake_session)
    monkeypatch.setattr(llm_module, "call_catalog_tool", fake_mcp)
    monkeypatch.setattr(llm_module.litellm, "acompletion", fake_acompletion)
    return seen


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    _hits.clear()
    token_store._store.clear()
    token_store._pending_phone.clear()
    monkeypatch.setattr(settings, "chat_api_key", "k")
    monkeypatch.setattr(session_store, "redis_client", None)
    yield
    _hits.clear()
    token_store._store.clear()
    token_store._pending_phone.clear()


def _say(text, sid="wa-otp-1"):
    r = client.post("/api/v2/chat", headers=AUTH, json={
        "session_id": sid, "messages": [{"role": "user", "content": text}]})
    assert r.status_code == 200 and r.json()["error"] is None, r.text
    return r.json()


def test_whatsapp_cannot_log_in_or_use_the_cart(monkeypatch):
    seen = _install(monkeypatch, [
        ("tool", "send_otp", {"phone": "+919876543210"}), ("text", "Website or a call?"),
        ("tool", "add_to_cart", {"activityId": 1}), ("text", "Website or a call?"),
    ])
    assert "Website" in _say("book rafting, my number is +919876543210")["reply"]
    assert "Website" in _say("add it to my cart")["reply"]
    assert seen["mcp_calls"] == []            # refused before reaching the MCP server
    assert token_store.get_token("wa-otp-1") is None
