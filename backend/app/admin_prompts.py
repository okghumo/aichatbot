"""Admin endpoints for the feedback loop: review drafts, roll prompts back, see what is live.

Guarded by ADMIN_API_KEY (its own key: not the CRM's chat key, not the dashboard key). Blank key = 503.
"""
import asyncio
import difflib
import hmac
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app import db, prompt_store
from app.config import settings
from app.feedback_proposer import propose_pending

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin")


def _auth(http_request: Request) -> None:
    if not settings.admin_api_key:
        raise HTTPException(503, "Admin API not configured (set ADMIN_API_KEY)")
    supplied = http_request.headers.get("authorization", "")
    if not hmac.compare_digest(supplied.encode(), f"Bearer {settings.admin_api_key}".encode()):
        raise HTTPException(401, "Unauthorized")


class Decision(BaseModel):
    by: str = Field(default="admin", min_length=1, max_length=100)   # who is deciding, kept in the audit trail


def _refuse(exc: prompt_store.PromptChangeError) -> HTTPException:
    return HTTPException(422, str(exc))


@router.get("/prompts")
async def list_prompts(http_request: Request) -> dict:
    """Each editable prompt: live version, who/when, and its old versions (at most PROMPT_HISTORY_KEEP) with days left."""
    _auth(http_request)

    def load() -> list[dict]:
        out = []
        for name in prompt_store.EDITABLE:
            rows = db.query("SELECT version, source, updated_by, updated_at FROM prompts WHERE name = ?", (name,))
            out.append({"name": name, "live": dict(rows[0]) if rows else None, "history": prompt_store.history(name)})
        return out
    await asyncio.to_thread(prompt_store.seed)
    return {"prompts": await asyncio.to_thread(load)}


@router.get("/prompts/drafts")
async def list_drafts(http_request: Request, status: str = "pending") -> dict:
    _auth(http_request)
    drafts = await asyncio.to_thread(prompt_store.list_drafts, status)
    versions = await asyncio.to_thread(prompt_store.live_versions)
    for draft in drafts:
        live = prompt_store.get(draft["name"]) or ""
        draft["diff"] = "".join(difflib.unified_diff(
            live.splitlines(keepends=True), draft["content"].splitlines(keepends=True), "live", "draft", n=2))
        draft["stale"] = draft["base_version"] is not None and draft["base_version"] != versions.get(draft["name"])
        del draft["content"]
    return {"drafts": drafts}


@router.post("/prompts/drafts/{draft_id}/approve")
async def approve(draft_id: int, decision: Decision, http_request: Request) -> dict:
    _auth(http_request)
    try:
        version = await asyncio.to_thread(prompt_store.approve_draft, draft_id, decision.by)
    except prompt_store.PromptChangeError as exc:
        raise _refuse(exc) from exc
    logger.info("Prompt draft %d approved by %s -> v%d", draft_id, decision.by, version)
    return {"approved": True, "version": version}


@router.post("/prompts/drafts/{draft_id}/reject")
async def reject(draft_id: int, decision: Decision, http_request: Request) -> dict:
    _auth(http_request)
    try:
        await asyncio.to_thread(prompt_store.reject_draft, draft_id, decision.by)
    except prompt_store.PromptChangeError as exc:
        raise _refuse(exc) from exc
    return {"rejected": True}


@router.post("/prompts/{name}/rollback/{history_id}")
async def rollback(name: str, history_id: int, decision: Decision, http_request: Request) -> dict:
    _auth(http_request)
    try:
        version = await asyncio.to_thread(prompt_store.rollback, name, history_id, decision.by)
    except prompt_store.PromptChangeError as exc:
        raise _refuse(exc) from exc
    logger.info("Prompt %s rolled back by %s -> v%d", name, decision.by, version)
    return {"rolled_back": True, "version": version}


@router.post("/feedback/propose")
async def propose_now(http_request: Request) -> dict:
    """Run the proposer once on newly received feedback (also runs on a timer when PROPOSER_ENABLED)."""
    _auth(http_request)
    return {"processed": await propose_pending()}


@router.get("/feedback/stats")
async def feedback_stats(http_request: Request) -> dict:
    _auth(http_request)
    rows = await asyncio.to_thread(db.query, "SELECT status, COUNT(*) AS n FROM feedback GROUP BY status")
    return {"feedback_by_status": {r["status"]: r["n"] for r in rows},
            "pending_drafts": len(await asyncio.to_thread(prompt_store.list_drafts, "pending"))}
