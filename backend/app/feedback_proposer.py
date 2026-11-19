"""Turns approved feedback into DRAFT prompt edits. It never changes a live prompt: a human approves drafts
(app/admin_prompts.py). Rules the model is given live in data/locked/proposer_rules.md (git only, not editable
through the prompt store). Code-level checks (prompt_store.check_change) apply on top of whatever the model says.
"""
import asyncio
import json
import logging
import re
from pathlib import Path

import litellm

from app import db, prompt_store
from app.config import settings
from app.notifier import send_notice

logger = logging.getLogger(__name__)

MAX_TRANSCRIPT_MESSAGES = 30
MAX_MESSAGE_CHARS = 600


def _rules() -> str:
    return (Path(settings.system_prompt_file).parent / "locked" / "proposer_rules.md").read_text(encoding="utf-8")


def _user_message(payload: dict) -> str:
    chat = [{"role": m.get("role"), "text": str(m.get("text", ""))[:MAX_MESSAGE_CHARS]}
            for m in payload.get("transcript", [])[-MAX_TRANSCRIPT_MESSAGES:]]
    item = {
        "category": payload["feedback"]["category"],
        "flagged_reply": payload["flagged"].get("text", ""),
        "what_it_should_have_said": payload["feedback"]["expected_reply"],
        "agent_note": payload["feedback"].get("note"),
        "ad_the_chat_came_from": (payload.get("conversation") or {}).get("source"),
        "recent_chat": chat,
    }
    prompts = "\n\n".join(f"=== {name} ===\n{prompt_store.get(name) or ''}" for name in prompt_store.EDITABLE)
    return f"<feedback_data>\n{json.dumps(item, ensure_ascii=False)}\n</feedback_data>\n\nCurrent prompts:\n{prompts}"


def _parse(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("no JSON object in the model reply")
    return json.loads(match.group(0))


def build_content(decision: dict) -> tuple[str, str, str]:
    """(prompt name, new full text, reason) from a model decision. Raises PromptChangeError / ValueError if unusable."""
    name = decision.get("prompt")
    if name not in prompt_store.EDITABLE:
        raise prompt_store.PromptChangeError(f"'{name}' is not an editable prompt")
    live = prompt_store.get(name) or ""
    old, new = decision.get("old") or "", decision.get("new")
    if not isinstance(new, str) or not new.strip():
        raise prompt_store.PromptChangeError("the proposed text is empty")
    if old:
        if live.count(old) != 1:
            raise prompt_store.PromptChangeError("the text to replace was not found exactly once in the live prompt")
        content = live.replace(old, new, 1)
    else:
        content = live.rstrip() + "\n\n" + new.strip() + "\n"
    return name, content, str(decision.get("reason") or "")[:500]


def _set_status(feedback_id: str, status: str, note: str | None = None) -> None:
    with db.tx() as conn:
        conn.execute("UPDATE feedback SET status=?, note=? WHERE feedback_id=?", (status, note, feedback_id))


async def propose_one(feedback_id: str, payload: dict) -> str:
    """Ask the model about one item. Returns the new feedback status."""
    reply = await litellm.acompletion(
        model=settings.proposer_model or settings.llm_model, temperature=0, max_tokens=1500,
        messages=[{"role": "system", "content": _rules()}, {"role": "user", "content": _user_message(payload)}])
    try:
        decision = _parse(reply.choices[0].message.content or "")
        if decision.get("action") != "edit":
            status, note = "wont_apply", str(decision.get("reason") or "no change needed")[:500]
        else:
            name, content, reason = build_content(decision)
            live = prompt_store.get(name) or ""
            draft_id = await asyncio.to_thread(prompt_store.create_draft, name, content, reason, feedback_id)
            status, note = "proposed", f"draft {draft_id} for {name}"
            change = abs(len(content) - len(live)) / max(len(live), 1)
            big = change >= settings.big_change_ratio
            await send_notice(
                f"{'BIG CHANGE: ' if big else ''}Bucky prompt draft #{draft_id} waiting for approval",
                f"A feedback item produced a draft edit to the '{name}' prompt. It is NOT live until approved.\n\n"
                f"Draft id: {draft_id}\nFeedback id: {feedback_id}\n"
                f"Size change: {change:.0%} of the live prompt{' (BIG)' if big else ''}\nReason: {reason}\n\n"
                f"Review: GET /api/admin/prompts/drafts\nApprove: POST /api/admin/prompts/drafts/{draft_id}/approve\n"
                f"Reject: POST /api/admin/prompts/drafts/{draft_id}/reject\n")
    except (ValueError, prompt_store.PromptChangeError) as exc:   # JSONDecodeError is a ValueError
        status, note = "wont_apply", f"proposal refused: {exc}"[:500]
        if isinstance(exc, prompt_store.PromptChangeError):   # e.g. too big / too much removed: a human should know
            await send_notice(
                "Bucky prompt change REFUSED (too big or unsafe)",
                f"Feedback id: {feedback_id}\nThe proposed edit was blocked by the safety checks and was not saved as a draft.\n"
                f"Why: {exc}\n")
    await asyncio.to_thread(_set_status, feedback_id, status, note)
    return status


async def propose_pending(limit: int = 5) -> int:
    """Handle up to `limit` newly received items. A model/network error leaves the item 'received' to retry later."""
    rows = await asyncio.to_thread(
        db.query, "SELECT feedback_id, payload_json FROM feedback WHERE status = 'received' ORDER BY received_at LIMIT ?", (limit,))
    done = 0
    for row in rows:
        try:
            await propose_one(row["feedback_id"], json.loads(row["payload_json"]))
            done += 1
        except Exception:  # pylint: disable=broad-except
            logger.exception("Proposer failed for a feedback item; will retry")
    return done


async def proposer_loop() -> None:
    while True:
        try:
            await propose_pending()
        except Exception:  # pylint: disable=broad-except
            logger.exception("Proposer pass failed")
        await asyncio.sleep(settings.proposer_interval_seconds)
