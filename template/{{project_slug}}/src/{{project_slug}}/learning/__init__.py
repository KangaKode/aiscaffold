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
from .knowledge_context import build_knowledge_context  # noqa: F401
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
from .erasure import ErasureCapExceededError, erase_correction  # noqa: F401
from .override_detector import OverrideDetector  # noqa: F401
from .content_policy import ContentPolicy  # noqa: F401
from .collusion import CollusionDetector, analyze_correction_drift  # noqa: F401
from .activity import ActivityTracker, AgentBaselineTracker  # noqa: F401
from .reflector import Reflection, ReflectionType, reflect  # noqa: F401
from .error_schemata import (  # noqa: F401
    ErrorSchema,
    extract_error_schemas,
    get_schemas_for_context,
)
from .contradiction import ContradictionFinding, scan_corrections  # noqa: F401

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
    "build_knowledge_context",
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
    "ErasureCapExceededError",
    "erase_correction",
    "OverrideDetector",
    "ContentPolicy",
    "CollusionDetector",
    "analyze_correction_drift",
    "ActivityTracker",
    "AgentBaselineTracker",
    "Reflection",
    "ReflectionType",
    "reflect",
    "ErrorSchema",
    "extract_error_schemas",
    "get_schemas_for_context",
    "ContradictionFinding",
    "scan_corrections",
]
