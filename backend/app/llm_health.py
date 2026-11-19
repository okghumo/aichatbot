"""Hourly LLM liveness check: one 1-token call; if it fails, email the SMTP_TO admins (debounced by notifier)."""
import asyncio
import logging

import litellm

from app.config import settings
from app.notifier import send_critical_alert

logger = logging.getLogger(__name__)


async def check_llm() -> bool:
    """True if the model answered. On failure, sends an 'llm_outage' alert and returns False."""
    try:
        await litellm.acompletion(model=settings.llm_model, max_tokens=1, timeout=30,
                                  messages=[{"role": "user", "content": "ping"}])
        return True
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("LLM health check failed: %s", exc)
        await send_critical_alert("llm_outage", str(exc)[:500], f"hourly health check, model={settings.llm_model}")
        return False


async def llm_health_loop() -> None:
    while True:
        await check_llm()
        await asyncio.sleep(settings.llm_health_interval_seconds)
