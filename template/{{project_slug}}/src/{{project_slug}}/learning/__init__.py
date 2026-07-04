"""
Vanilla Learning System -- teaches your AI agent project to learn from user interactions.

All terminology is vanilla -- "user", "agent", "preference", "feedback", "session".
Domain-specific vocabulary is added by the project, not the scaffold.
"""

from .models import (  # noqa: F401
    FeedbackSignal,
    UserPreference,
    AgentTrustScore,
    CheckIn,
    SignalType,
    CheckInStatus,
)
from .feedback_tracker import FeedbackTracker  # noqa: F401
from .agent_trust import AgentTrustManager  # noqa: F401
from .checkin_manager import CheckInManager  # noqa: F401
from .user_profile import UserProfileManager  # noqa: F401
from .global_profile import GlobalProfileManager  # noqa: F401
from .graduation import GraduationEngine, GraduationRule, GraduationCandidate  # noqa: F401
from .store import (  # noqa: F401
    LearningStore,
    SqliteLearningStore,
    PostgresLearningStore,
    get_learning_store,
    TABLE_COLUMNS,
)
from .corrections import (  # noqa: F401
    Correction,
    CorrectionsManager,
    STATUS_PROPOSED,
    STATUS_APPROVED,
    STATUS_REJECTED,
    STATUS_RETIRED,
)
from .override_detector import OverrideDetector  # noqa: F401
from .content_policy import ContentPolicy  # noqa: F401
from .collusion import CollusionDetector, analyze_correction_drift  # noqa: F401
from .activity import ActivityTracker, AgentBaselineTracker  # noqa: F401

__all__ = [
    "FeedbackSignal",
    "UserPreference",
    "AgentTrustScore",
    "CheckIn",
    "SignalType",
    "CheckInStatus",
    "FeedbackTracker",
    "AgentTrustManager",
    "CheckInManager",
    "UserProfileManager",
    "GlobalProfileManager",
    "GraduationEngine",
    "GraduationRule",
    "GraduationCandidate",
    "LearningStore",
    "SqliteLearningStore",
    "PostgresLearningStore",
    "get_learning_store",
    "TABLE_COLUMNS",
    "Correction",
    "CorrectionsManager",
    "STATUS_PROPOSED",
    "STATUS_APPROVED",
    "STATUS_REJECTED",
    "STATUS_RETIRED",
    "OverrideDetector",
    "ContentPolicy",
    "CollusionDetector",
    "analyze_correction_drift",
    "ActivityTracker",
    "AgentBaselineTracker",
]
