"""
UserProfileManager -- Aggregates preferences, trust, and check-in responses
into a context bundle suitable for injection into any agent prompt.

The profile is the bridge between the learning system and the LLM:
  - get_context_bundle() returns a dict that can be serialized into a
    CacheablePrompt's context field
  - Combines explicit preferences (user said), implicit preferences (inferred
    from feedback), and agent trust scores

Projects can subclass to add domain-specific synthesis.

Keep this file under 200 lines.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..security.prompt_guard import sanitize_for_prompt
from ..security.validators import validate_length
from .agent_trust import AgentTrustManager
from .feedback_tracker import FeedbackTracker
from .models import UserPreference
from .rag.preference_retriever import PreferenceRetriever
from .schema import DEFAULT_DB_PATH, dict_from_row, get_connection
from .trust_guard import effective_trust_scores

logger = logging.getLogger(__name__)


@dataclass
class UserProfile:
    """Synthesized user profile -- aggregation of all learning data."""

    project_id: str = "default"
    explicit_preferences: list[UserPreference] = field(default_factory=list)
    implicit_preferences: list[UserPreference] = field(default_factory=list)
    agent_trust_scores: dict[str, float] = field(default_factory=dict)
    total_interactions: int = 0
    top_context_types: list[str] = field(default_factory=list)


class UserProfileManager:
    """
    Aggregates learning data into a profile for prompt injection.

    Usage:
        profile_mgr = UserProfileManager(project_id="my_project")

        # Build the full profile
        profile = profile_mgr.get_profile()

        # Get a context bundle for CacheablePrompt
        bundle = profile_mgr.get_context_bundle()
        prompt = CacheablePrompt(system=..., context=bundle, user_message=...)
    """

    def __init__(
        self,
        project_id: str = "default",
        db_path: Path = DEFAULT_DB_PATH,
        feedback_tracker: FeedbackTracker | None = None,
        trust_manager: AgentTrustManager | None = None,
        preference_retriever: PreferenceRetriever | None = None,
    ):
        self._project_id = project_id
        self._db_path = db_path
        self._feedback = feedback_tracker or FeedbackTracker(db_path)
        self._trust = trust_manager or AgentTrustManager(db_path)
        self._retriever = preference_retriever or PreferenceRetriever(
            project_id=project_id, db_path=db_path
        )

    def get_profile(self) -> UserProfile:
        """Build the full user profile from all learning data."""
        explicit = self._get_preferences(source="explicit")
        implicit = self._get_preferences(source="implicit")
        # Same guarded read as routing (TRUST_* flags apply; exact
        # passthrough with flags unset) -- prompts and agent selection
        # must never disagree on trust within one request.
        trust_scores = effective_trust_scores(self._trust, self._project_id)
        total = self._feedback.get_total_count(self._project_id)

        return UserProfile(
            project_id=self._project_id,
            explicit_preferences=explicit,
            implicit_preferences=implicit,
            agent_trust_scores=trust_scores,
            total_interactions=total,
        )

    def get_context_bundle(self, query: str = "") -> str:
        """
        Build a context string for injection into a CacheablePrompt.

        If a query is provided, includes semantically relevant preferences.
        Otherwise returns all active preferences.
        """
        parts = []

        explicit = self._get_preferences(source="explicit")
        if explicit:
            rules = [f"- {p.key}: {p.value}" for p in explicit[:20]]
            parts.append("User preferences:\n" + "\n".join(rules))

        trust_scores = effective_trust_scores(self._trust, self._project_id)
        if trust_scores:
            ranked = sorted(trust_scores.items(), key=lambda x: x[1], reverse=True)
            trust_lines = [f"- {name}: {score:.0%}" for name, score in ranked[:10]]
            parts.append("Agent trust levels:\n" + "\n".join(trust_lines))

        if query:
            relevant = self._retriever.search(query, limit=5, min_priority=30)
            if relevant.results:
                rel_lines = [f"- {r.content}" for r in relevant.results]
                parts.append(
                    "Relevant learned preferences:\n" + "\n".join(rel_lines)
                )

        if not parts:
            return ""

        return "\n\n".join(parts)

    def save_preference(self, pref: UserPreference) -> UserPreference:
        """Save a preference to the database and index it."""
        pref.key = sanitize_for_prompt(pref.key, max_length=500)
        pref.value = sanitize_for_prompt(pref.value, max_length=5000)
        validate_length(pref.preference_type, "preference_type", max_length=200)

        conn = get_connection(self._db_path)
        try:
            conn.execute(
                """INSERT INTO user_preferences
                   (id, project_id, preference_type, key, value, source,
                    priority, active, metadata_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                    value = excluded.value,
                    priority = excluded.priority,
                    active = excluded.active,
                    updated_at = excluded.updated_at""",
                (
                    pref.id, pref.project_id, pref.preference_type,
                    pref.key, pref.value, pref.source, pref.priority,
                    1 if pref.active else 0,
                    json.dumps(pref.metadata, default=str),
                    pref.created_at, pref.updated_at,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        self._retriever.index_preference(pref)
        logger.debug(
            f"[UserProfile] Saved preference: {pref.key}={pref.value} "
            f"({pref.source}, priority={pref.priority})"
        )
        return pref

    def _get_preferences(
        self, source: str | None = None, active_only: bool = True
    ) -> list[UserPreference]:
        """Load preferences from the database."""
        query = "SELECT * FROM user_preferences WHERE project_id = ?"
        params: list[Any] = [self._project_id]

        if active_only:
            query += " AND active = 1"
        if source:
            query += " AND source = ?"
            params.append(source)

        query += " ORDER BY priority DESC"

        conn = get_connection(self._db_path)
        try:
            rows = conn.execute(query, params).fetchall()
            return [
                UserPreference(
                    id=dict_from_row(r)["id"],
                    project_id=dict_from_row(r).get("project_id", self._project_id),
                    preference_type=dict_from_row(r)["preference_type"],
                    key=dict_from_row(r)["key"],
                    value=dict_from_row(r)["value"],
                    source=dict_from_row(r).get("source", "implicit"),
                    priority=dict_from_row(r).get("priority", 50),
                    active=bool(dict_from_row(r).get("active", True)),
                    metadata=dict_from_row(r).get("metadata", {}),
                )
                for r in rows
            ]
        finally:
            conn.close()
