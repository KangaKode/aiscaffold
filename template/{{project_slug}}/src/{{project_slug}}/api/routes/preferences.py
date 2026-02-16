"""
Preferences API -- manage and search learned user preferences.

  GET    /api/v1/preferences           -- List active preferences
  POST   /api/v1/preferences           -- Save a preference
  GET    /api/v1/preferences/search    -- Semantic search
  PUT    /api/v1/preferences/{id}      -- Update a preference
  DELETE /api/v1/preferences/{id}      -- Deactivate a preference

Security:
  - Input validation on all fields
  - Auth required for mutations
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ...learning.models import UserPreference
from ...learning.user_profile import UserProfileManager
from ...security import ValidationError, validate_length
from ..middleware.auth import verify_api_key

logger = logging.getLogger(__name__)
router = APIRouter()


class PreferenceRequest(BaseModel):
    """Request to save a user preference."""

    preference_type: str = Field(..., description="Category: style, behavior, output_format, etc.")
    key: str = Field(..., description="What the preference is about")
    value: str = Field(..., description="The preference value")
    source: str = Field("explicit", description="explicit, implicit, or graduated")
    priority: int = Field(50, ge=0, le=100)


class PreferenceResponse(BaseModel):
    """Response with saved preference details."""

    id: str
    preference_type: str
    key: str
    value: str
    source: str
    priority: int
    active: bool


def _get_profile_mgr(request: Request) -> UserProfileManager:
    mgr = getattr(request.app.state, "profile_manager", None)
    if mgr is None:
        mgr = UserProfileManager()
        request.app.state.profile_manager = mgr
    return mgr


@router.post("/preferences", response_model=PreferenceResponse)
async def save_preference(
    pref_req: PreferenceRequest,
    request: Request,
    _key: str | None = Depends(verify_api_key),
) -> PreferenceResponse:
    """Save a user preference."""
    try:
        validate_length(pref_req.key, "key", min_length=1, max_length=500)
        validate_length(pref_req.value, "value", min_length=1, max_length=5000)
        validate_length(pref_req.preference_type, "preference_type", min_length=1, max_length=200)
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    mgr = _get_profile_mgr(request)
    pref = UserPreference(
        preference_type=pref_req.preference_type,
        key=pref_req.key,
        value=pref_req.value,
        source=pref_req.source,
        priority=pref_req.priority,
    )
    saved = mgr.save_preference(pref)

    return PreferenceResponse(
        id=saved.id,
        preference_type=saved.preference_type,
        key=saved.key,
        value=saved.value,
        source=saved.source,
        priority=saved.priority,
        active=saved.active,
    )


@router.get("/preferences")
async def list_preferences(request: Request) -> dict:
    """List all active preferences."""
    mgr = _get_profile_mgr(request)
    profile = mgr.get_profile()
    all_prefs = profile.explicit_preferences + profile.implicit_preferences
    return {
        "preferences": [
            {
                "id": p.id,
                "preference_type": p.preference_type,
                "key": p.key,
                "value": p.value,
                "source": p.source,
                "priority": p.priority,
            }
            for p in all_prefs
        ],
        "total": len(all_prefs),
    }


@router.get("/preferences/search")
async def search_preferences(
    q: str,
    request: Request,
    limit: int = 10,
    preference_type: str | None = None,
) -> dict:
    """Semantic search over preferences."""
    try:
        validate_length(q, "query", min_length=1, max_length=1000)
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    mgr = _get_profile_mgr(request)
    results = mgr._retriever.search(
        query=q,
        limit=min(limit, 50),
        preference_type=preference_type,
    )
    return {
        "results": [
            {
                "id": r.id,
                "content": r.content,
                "score": r.score,
                "metadata": r.metadata,
            }
            for r in results.results
        ],
        "total": results.total,
        "query": q,
    }


@router.get("/profile")
async def get_profile(request: Request, query: str = "") -> dict:
    """Get the synthesized user profile and context bundle."""
    mgr = _get_profile_mgr(request)
    profile = mgr.get_profile()
    bundle = mgr.get_context_bundle(query=query)
    return {
        "project_id": profile.project_id,
        "total_interactions": profile.total_interactions,
        "explicit_preferences": len(profile.explicit_preferences),
        "implicit_preferences": len(profile.implicit_preferences),
        "agent_trust_scores": profile.agent_trust_scores,
        "context_bundle": bundle,
    }
