"""POST /api/v2/followup: the text of a nudge for a WhatsApp chat where the customer went quiet.

The CRM owns the clock and the sending (it knows the 24-hour window, who has taken a chat over, and
whether the customer replied). It calls this when a timer fires: send the chat so far and which follow-up
it is (1 = first). The answer says whether to send, the text, and how long to wait for the next one.
Schedule: FOLLOWUP_SCHEDULE_MINUTES (default 15, 60, 360, 1440 minutes of silence after the bot's last
message). A follow-up due 23 hours or more after that message is outside WhatsApp's free-text window, so
`template_required` is true and the CRM must send an approved template (the reply is its text).
"""
import logging
import re

import litellm
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.cache_limiter import _enforce_cache_limits
from app.config import settings
from app.crm_chat import _require_key
from app.emoji_limit import strip_emojis
from app.llm import build_messages
from app.schemas import ChatMessage
from app.session_store import mask_session_id

logger = logging.getLogger(__name__)
router = APIRouter()

WINDOW_SAFE_MINUTES = 23 * 60   # past this, free text may fall outside WhatsApp's 24 h window

_OPT_OUT = re.compile(
    r"\b(?:stop|unsubscribe|not interested|no thanks|leave me alone|don'?t (?:message|text|contact)|"
    r"do not (?:message|text|contact)|nahi chahiye|mat bhejo|message mat|disturb mat|band karo)\b", re.I)

_ATTEMPT_GUIDE = {
    1: "A short while of silence. A light, warm nudge that picks up exactly where the chat stopped: ask the one "
       "open question again in an easier way (activity, date or how many people), or say you are here when they "
       "are free.",
    2: "About an hour of silence. Give one small, honest reason to reply (for example that you can check slots for "
       "their date), without pressure.",
    3: "Several hours of silence. A gentle check-in: ask if they still want to go ahead, and say they can ask "
       "anything or get a call from the team.",
    4: "A day of silence: the last message. A short, polite closing note: you will leave it here and they can "
       "message any time to continue. No question, no pressure.",
}

FOLLOWUP_PROMPT = (
    "## Follow-up message: the customer has gone quiet\n"
    "Write ONE short WhatsApp follow-up from you to this customer, continuing this chat. "
    "{guide}\n"
    "- One or two short sentences, under about 200 characters, in the customer's language and spelling, "
    "texting style as above.\n"
    "- NO emoji at all.\n"
    "- Never quote or invent a price, availability, discount or offer. Do not repeat an earlier message of "
    "yours word for word. Do not mention how long it has been, timers, or that this is automated. Avoid "
    "'just checking in'.\n"
    "- Output only the message text."
)


class FollowupRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=40)
    session_id: str | None = Field(default=None, max_length=128)
    attempt: int = Field(ge=1, le=20, description="1 = first follow-up")


class FollowupResponse(BaseModel):
    send: bool
    reply: str = ""
    attempt: int
    total: int
    template_required: bool = False
    next_in_minutes: int | None = None   # wait this long, then ask for the next follow-up (None = last)
    skip_reason: str | None = None       # customer_replied | opted_out | sequence_finished | no_customer_message
    error: str | None = None


def schedule() -> list[int]:
    return [int(x) for x in settings.followup_schedule_minutes.split(",") if x.strip().isdigit()]


def _skip(attempt: int, total: int, reason: str) -> FollowupResponse:
    return FollowupResponse(send=False, attempt=attempt, total=total, skip_reason=reason)


@router.post("/api/v2/followup", response_model=FollowupResponse)
async def followup(request: FollowupRequest, http_request: Request) -> FollowupResponse:
    _require_key(http_request)
    sched = schedule()
    total, attempt = len(sched), request.attempt
    if attempt > total:
        return _skip(attempt, total, "sequence_finished")
    user_texts = [m.content for m in request.messages if m.role == "user"]
    if not user_texts:
        return _skip(attempt, total, "no_customer_message")
    if request.messages[-1].role == "user":
        return _skip(attempt, total, "customer_replied")
    if _OPT_OUT.search(user_texts[-1]):
        return _skip(attempt, total, "opted_out")

    result = FollowupResponse(
        send=True, attempt=attempt, total=total,
        template_required=sched[attempt - 1] >= WINDOW_SAFE_MINUTES,
        next_in_minutes=sched[attempt] - sched[attempt - 1] if attempt < total else None,
    )
    try:
        built = await build_messages(request.messages, request.session_id, channel="whatsapp")
        built[0]["content"].append({"type": "text", "text": FOLLOWUP_PROMPT.format(guide=_ATTEMPT_GUIDE[min(attempt, 4)])})
        built.append({"role": "user", "content": "(The customer has not replied. Write the follow-up now.)"})
        response = await litellm.acompletion(
            model=settings.llm_model, messages=_enforce_cache_limits(built), max_tokens=200, num_retries=2, timeout=30)
        result.reply = strip_emojis((response.choices[0].message.content or "").strip())
    except Exception:  # pylint: disable=broad-except
        logger.exception("Follow-up %d failed for session %s", attempt, mask_session_id(request.session_id))
        result.error = "followup_failed"
    if not result.reply:
        result.send = False
        result.error = result.error or "empty_reply"
    return result
