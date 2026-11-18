"""POST /api/v2/feedback — approved CRM feedback on a Bucky reply.

The CRM only sends items a super admin approved. We validate, mask the customer's phone / drop the name,
store durably in SQLite and acknowledge. No LLM work happens here (the proposer runs separately, see
app/feedback_proposer.py). Idempotent on feedback_id: a retry gets the same ref back with duplicate=true.
Contract for the CRM team: FEEDBACK_API.md.
"""
import asyncio
import json
import logging
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app import db
from app.config import settings
from app.crm_chat import _require_key
from app.session_store import mask_session_id

logger = logging.getLogger(__name__)
router = APIRouter()

Category = Literal["wrong_information", "wrong_price", "missed_intent", "should_have_handed_off",
                   "should_not_have_handed_off", "tone", "formatting", "too_long", "other"]


class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")   # the CRM may add fields later; keep them, don't reject


class Flagged(_Open):
    message_id: str = Field(min_length=1, max_length=200)
    response_id: str | None = None
    text: str = Field(default="", max_length=20_000)
    sent_at: str | None = None
    delivered: bool | None = None
    source: Literal["conversation", "team_cover", "follow_up"] | None = None


class FeedbackBody(_Open):
    category: Category
    expected_reply: str = Field(min_length=1, max_length=10_000)
    note: str | None = Field(default=None, max_length=10_000)
    submitted_by: dict | None = None
    approved_by: dict | None = None


class BuckyRef(_Open):
    session_id: str | None = None
    request_id: str | None = None


class FeedbackIn(_Open):
    feedback_id: str = Field(min_length=1, max_length=200)
    submitted_at: str | None = None
    approved_at: str | None = None
    flagged: Flagged
    feedback: FeedbackBody
    bucky: BuckyRef = BuckyRef()
    conversation: dict | None = None
    transcript: list[dict]
    truncated: bool = False


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _mask_phone(phone: str) -> str:
    """91886xxxxxx03 -> 91886******03. Already-masked or very short values are left as they are."""
    digits = re.sub(r"\D", "", phone)
    if "*" in phone or len(digits) < 8:
        return phone
    return digits[:5] + "*" * (len(digits) - 7) + digits[-2:]


def _scrub(data: dict) -> dict:
    """Customer privacy: keep a masked phone, never the name (this is training data, not a contact list)."""
    customer = (data.get("conversation") or {}).get("customer")
    if isinstance(customer, dict):
        customer.pop("name", None)
        if isinstance(customer.get("phone"), str):
            customer["phone"] = _mask_phone(customer["phone"])
    return data


def _validation_message(exc: ValidationError) -> str:
    """First few problems as 'field: reason', never echoing the submitted values (they may hold customer text)."""
    return "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:5])


def _store(item: FeedbackIn) -> tuple[str, bool]:
    """Insert once. Returns (ref, duplicate). The primary key is the dedupe, so concurrent retries are safe."""
    data = _scrub(item.model_dump(mode="json"))
    ref = f"bf_{uuid.uuid4().hex[:20]}"
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO feedback (feedback_id, received_at, category, session_id, request_id, response_id, "
            "payload_json, ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (item.feedback_id, datetime.now(timezone.utc).isoformat(timespec="seconds"), item.feedback.category,
             item.bucky.session_id, item.bucky.request_id, item.flagged.response_id, json.dumps(data), ref))
        if cur.rowcount == 1:
            return ref, False
        return conn.execute("SELECT ref FROM feedback WHERE feedback_id = ?", (item.feedback_id,)).fetchone()["ref"], True


@router.post("/api/v2/feedback")
async def receive_feedback(http_request: Request) -> JSONResponse:
    _require_key(http_request)
    declared = http_request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > settings.feedback_max_bytes:
        return _error(422, f"request larger than {settings.feedback_max_bytes} bytes")
    raw = await http_request.body()
    if len(raw) > settings.feedback_max_bytes:
        return _error(422, f"request larger than {settings.feedback_max_bytes} bytes")
    try:
        item = FeedbackIn.model_validate_json(raw)
    except ValidationError as exc:
        return _error(422, _validation_message(exc))
    if len(item.transcript) > settings.feedback_max_messages:
        return _error(422, f"transcript has more than {settings.feedback_max_messages} messages")
    try:
        ref, duplicate = await asyncio.to_thread(_store, item)
    except (sqlite3.Error, OSError):   # unopenable/full database or disk
        logger.exception("Could not store feedback %s", mask_session_id(item.feedback_id))
        return _error(503, "could not store feedback, please retry")   # the CRM retries 5xx
    logger.info("POST /api/v2/feedback — category=%s duplicate=%s session=%s", item.feedback.category, duplicate,
                mask_session_id(item.bucky.session_id or ""))
    body: dict = {"received": True, "ref": ref}
    if duplicate:
        body["duplicate"] = True
    return JSONResponse(body)
