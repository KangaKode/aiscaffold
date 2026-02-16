"""
Webhook receiver for async external agents.

External agents in async mode push results here when their work is done
instead of the RemoteAgent adapter waiting for a synchronous HTTP response.

  POST /api/v1/webhooks/agents/{agent_id} -- Receive agent result

Security:
  - HMAC signature verification (X-Webhook-Signature header)
  - Payload size validation
  - Bounded pending results cache with TTL
  - Registered agent verification
"""

import hashlib
import hmac
import logging
import os
import time
from collections import OrderedDict
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request

from ...security import ValidationError, validate_dict_size
from ..middleware.auth import AuthContext, verify_api_key
from ..models.requests import WebhookPayload

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_PENDING_RESULTS = 500
MAX_PAYLOAD_BYTES = 5_000_000
RESULT_TTL_SECONDS = 3600

_pending_results: OrderedDict[str, dict] = OrderedDict()


def _get_webhook_secret() -> str | None:
    """Load webhook signing secret from environment."""
    return os.environ.get("WEBHOOK_SECRET", "").strip() or None


def _verify_signature(payload_bytes: bytes, signature: str, secret: str) -> bool:
    """Verify HMAC-SHA256 signature of webhook payload."""
    expected = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)


def _evict_expired() -> None:
    """Remove expired entries from pending results."""
    now = time.time()
    expired_keys = [
        k for k, v in _pending_results.items()
        if now - v.get("_received_at_ts", 0) > RESULT_TTL_SECONDS
    ]
    for k in expired_keys:
        del _pending_results[k]


def _store_result(task_key: str, data: dict) -> None:
    """Store result with LRU eviction and TTL."""
    _evict_expired()
    data["_received_at_ts"] = time.time()
    _pending_results[task_key] = data
    while len(_pending_results) > MAX_PENDING_RESULTS:
        _pending_results.popitem(last=False)


@router.post("/webhooks/agents/{agent_id}")
async def receive_webhook(
    agent_id: str,
    payload: WebhookPayload,
    request: Request,
    auth: AuthContext = Depends(verify_api_key),
) -> dict:
    """
    Receive a result from an async external agent.

    Security:
      - If WEBHOOK_SECRET is set, the X-Webhook-Signature header must contain
        a valid HMAC-SHA256 signature of the request body.
      - Payload size is limited to 5MB.
      - Only registered agents can submit results.
    """
    registry = request.app.state.registry
    if registry.get(agent_id) is None:
        raise HTTPException(
            status_code=404, detail=f"Agent '{agent_id}' is not registered"
        )

    if payload.phase not in ("analyze", "challenge", "vote"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid phase: '{payload.phase}'. Must be analyze, challenge, or vote.",
        )

    webhook_secret = _get_webhook_secret()
    if webhook_secret:
        signature = request.headers.get("X-Webhook-Signature", "")
        body = await request.body()
        if not _verify_signature(body, signature, webhook_secret):
            logger.warning(
                f"[Webhook] Invalid signature from agent {agent_id} "
                f"for task {payload.task_id}"
            )
            raise HTTPException(status_code=403, detail="Invalid webhook signature")

    try:
        validate_dict_size(payload.result, "result", max_size_bytes=MAX_PAYLOAD_BYTES)
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    task_key = f"{payload.task_id}:{agent_id}:{payload.phase}"
    _store_result(task_key, {
        "agent_name": payload.agent_name,
        "phase": payload.phase,
        "result": payload.result,
        "received_at": datetime.now().isoformat(),
    })

    logger.info(
        f"[Webhook] Received {payload.phase} result from {agent_id} "
        f"for task {payload.task_id}"
    )
    return {"status": "received", "task_key": task_key}


def get_pending_result(task_id: str, agent_id: str, phase: str) -> dict | None:
    """Retrieve a pending webhook result (used by the orchestrator)."""
    _evict_expired()
    task_key = f"{task_id}:{agent_id}:{phase}"
    return _pending_results.pop(task_key, None)
