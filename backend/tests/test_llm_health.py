"""Hourly LLM health check: silent when the model answers, emails an alert when it doesn't."""
import pytest

from app import llm_health


@pytest.mark.asyncio
async def test_failure_sends_alert_and_success_does_not(monkeypatch):
    alerts = []

    async def alert(kind, msg, ctx=""):
        alerts.append((kind, msg))

    async def ok(**kw):
        return object()

    async def down(**kw):
        raise RuntimeError("overloaded")
    monkeypatch.setattr(llm_health, "send_critical_alert", alert)
    monkeypatch.setattr(llm_health.litellm, "acompletion", ok)
    assert await llm_health.check_llm() is True and not alerts
    monkeypatch.setattr(llm_health.litellm, "acompletion", down)
    assert await llm_health.check_llm() is False and alerts == [("llm_outage", "overloaded")]
