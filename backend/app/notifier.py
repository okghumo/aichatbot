import asyncio
import logging
import smtplib
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Dict

from app.config import settings

logger = logging.getLogger(__name__)

# Dictionary to store the last time an alert was sent for each specific category
_last_alert_times: Dict[str, float] = {}

# Default debounce times for different alert categories (in seconds)
ALERT_DEBOUNCE_CONFIG = {
    "llm_credits": 3600,      # 1 hour
    "llm_rate_limit": 1800,   # 30 minutes
    "llm_outage": 1800,       # 30 minutes
    "redis_down": 900,        # 15 minutes
    "weaviate_down": 900,     # 15 minutes
    "mcp_tool_error": 1800,   # 30 minutes
    "default": 3600           # 1 hour fallback
}

# Human-readable subject/description/causes per alert category, used to turn
# the raw exception into something actionable in the email body.
ALERT_INFO = {
    "llm_credits": {
        "subject": "URGENT: AI Chatbot Out of Credits (402/404)!",
        "issue": "The LLM provider rejected a request for billing/account reasons.",
        "causes": [
            "Anthropic/OpenAI account has run out of prepaid credits or hit a spend cap",
            "Payment method on the provider account failed or expired",
            "API key was revoked, disabled, or belongs to a suspended account",
        ],
    },
    "llm_rate_limit": {
        "subject": "WARNING: AI Chatbot Hitting API Rate Limits (429)",
        "issue": "The LLM provider is throttling requests — too many calls too fast.",
        "causes": [
            "Traffic spike exceeding your requests-per-minute or tokens-per-minute tier",
            "Provider-side quota is lower than expected for the current usage tier",
            "A retry loop or bug is hammering the API faster than normal",
        ],
    },
    "llm_outage": {
        "subject": "CRITICAL: Upstream LLM Provider Outage (500+/529)",
        "issue": "The LLM provider's servers are erroring or overloaded, not your account.",
        "causes": [
            "Provider-wide outage or degraded service (check their status page)",
            "Anthropic 529 'overloaded' — temporary capacity issue, usually self-resolves",
            "Transient network/DNS issue between this server and the provider",
        ],
    },
    "redis_down": {
        "subject": "CRITICAL: Redis Database Unreachable!",
        "issue": "The backend can't reach Redis — session memory and MCP caching are broken.",
        "causes": [
            "Redis container/process crashed or was OOM-killed",
            "REDIS_URL is misconfigured or points to the wrong host/port",
            "Network issue between the backend and Redis (e.g. Docker network down)",
        ],
    },
    "weaviate_down": {
        "subject": "CRITICAL: Weaviate Vector Database Unreachable!",
        "issue": "The backend can't reach Weaviate — RAG/knowledge-base retrieval is broken.",
        "causes": [
            "Weaviate container/process crashed or is still starting up",
            "WEAVIATE_URL or WEAVIATE_API_KEY is misconfigured",
            "Underlying disk/memory pressure on the host running Weaviate",
        ],
    },
    "mcp_tool_error": {
        "subject": "WARNING: External MCP Tool API Failing!",
        "issue": "A call to the external MCP server (catalog/booking tools) failed.",
        "causes": [
            "bucketlistt MCP server is down or slow to respond",
            "MCP_SERVER_URL is misconfigured or the server rejected the request",
            "The specific tool call had bad/unexpected arguments from the model",
        ],
    },
}


def _send_email_sync(subject: str, body: str) -> None:
    """Synchronous core for sending an email via SMTP."""
    if not all([settings.smtp_server, settings.smtp_user, settings.smtp_pass, settings.smtp_to]):
        logger.warning("SMTP configuration is incomplete. Skipping email alert for: %s", subject)
        return

    msg = MIMEMultipart()
    msg["From"] = settings.smtp_user
    msg["To"] = settings.smtp_to
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        server = smtplib.SMTP(settings.smtp_server, settings.smtp_port, timeout=10)
        server.starttls()
        server.login(settings.smtp_user, settings.smtp_pass)
        server.send_message(msg)
        server.quit()
        logger.info(f"Successfully sent alert email to {settings.smtp_to}")
    except Exception as e:
        logger.error(f"Failed to send alert email: {e}")


def send_critical_alert_sync(alert_type: str, error_message: str, context: str = "") -> None:
    """
    Debounce-check and email admins when a critical infrastructure piece fails.
    Fully synchronous (blocking SMTP call included) — safe to call from any thread,
    including plain worker threads with no running event loop (e.g. via
    asyncio.to_thread). Async callers should use send_critical_alert instead.
    """
    global _last_alert_times
    now = time.time()

    debounce_seconds = ALERT_DEBOUNCE_CONFIG.get(alert_type, ALERT_DEBOUNCE_CONFIG["default"])
    last_sent = _last_alert_times.get(alert_type, 0.0)

    if now - last_sent < debounce_seconds:
        logger.info("Alert '%s' throttled (already sent recently).", alert_type)
        return

    _last_alert_times[alert_type] = now

    info = ALERT_INFO.get(alert_type, {
        "subject": f"ALERT: Chatbot Error - {alert_type}",
        "issue": "An unrecognized error occurred.",
        "causes": ["No known causes mapped for this alert type — check the raw error below."],
    })

    causes = "\n".join(f"  - {c}" for c in info["causes"])
    body = (
        f"Hello,\n\n"
        f"Your AI Chatbot just encountered a critical infrastructure error.\n\n"
        f"WHAT'S WRONG\n"
        f"  {info['issue']}\n\n"
        f"PROBABLE CAUSES\n"
        f"{causes}\n\n"
        f"WHERE\n"
        f"  {context or alert_type}\n\n"
        f"RAW ERROR\n"
        f"  {error_message}\n\n"
        f"Next re-alert for this category is suppressed for "
        f"{debounce_seconds // 60} minutes.\n"
    )

    _send_email_sync(info["subject"], body)


async def send_critical_alert(alert_type: str, error_message: str, context: str = "") -> None:
    """Async wrapper for callers already running on the event loop — runs the
    (blocking) sync core in a worker thread so it never stalls the loop."""
    await asyncio.to_thread(send_critical_alert_sync, alert_type, error_message, context)


async def send_notice(subject: str, body: str) -> None:
    """Plain email to SMTP_TO (no debounce: each draft is its own event). Never raises; skipped if SMTP is unset."""
    await asyncio.to_thread(_send_email_sync, subject, body)
