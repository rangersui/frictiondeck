"""pipeline/constants.py — Canonical string constants for FrictionDeck v4.

All magic strings that appear in more than one file live here.
Import from this module instead of repeating string literals.

Usage:
    from pipeline.constants import EventType, ArtifactState, TrustTier
"""


# ═══════════════════════════════════════════════════════════════════════════
# Audit event types
# ═══════════════════════════════════════════════════════════════════════════

class EventType:
    # Stage mutations
    ARTIFACT_DROPPED = "artifact_dropped"
    ARTIFACT_UPDATED = "artifact_updated"
    JUDGMENT_PROMOTED = "judgment_promoted"
    JUDGMENT_UPDATED = "judgment_updated"
    OVERLAY_CHANGED = "overlay_changed"

    # NLI
    NLI_VERIFIED = "nli_verified"

    # Commit lifecycle
    COMMIT_PROPOSED = "commit_proposed"
    COMMIT_APPROVED = "commit_approved"
    COMMIT_REJECTED = "commit_rejected"

    # Parameter operations
    PARAM_LOCKED = "param_locked"
    PARAM_UNLOCKED = "param_unlocked"
    PARAM_UPDATED = "param_updated"

    # Negative space
    NEGATIVE_SPACE_FLAGGED = "negative_space_flagged"
    NEGATIVE_SPACE_DISMISSED = "negative_space_dismissed"

    # Card operations (GUI only)
    CARD_MOVED = "card_moved"
    CARD_DELETED = "card_deleted"
    CARD_EDITED = "card_edited"
    CARDS_GROUPED = "cards_grouped"
    RELATION_ADDED = "relation_added"
    RELATION_REMOVED = "relation_removed"

    # Friction Gate
    GATE_CHALLENGE_ISSUED = "gate_challenge_issued"
    GATE_CHALLENGE_PASSED = "gate_challenge_passed"
    GATE_CHALLENGE_FAILED = "gate_challenge_failed"
    GATE_LOCKOUT = "gate_lockout"


# ═══════════════════════════════════════════════════════════════════════════
# Three-state model: fluid → viscous → solid
# ═══════════════════════════════════════════════════════════════════════════

class ArtifactState:
    FLUID = "fluid"         # AI dropped, grey, free to change/delete
    VISCOUS = "viscous"     # Promoted to judgment, constrained, tracked
    SOLID = "solid"         # Committed, HMAC sealed, irreversible


# ═══════════════════════════════════════════════════════════════════════════
# NLI verdicts (lowercase — as stored in result dicts)
# ═══════════════════════════════════════════════════════════════════════════

class Verdict:
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    NEUTRAL = "neutral"
    CONTRADICTION = "contradiction"

    ALL = {SUPPORTED, UNSUPPORTED, NEUTRAL, CONTRADICTION}
    POSITIVE = {SUPPORTED}


# ═══════════════════════════════════════════════════════════════════════════
# Trust tiers
# ═══════════════════════════════════════════════════════════════════════════

class TrustTier:
    T1_READ = "t1_read"            # Any agent can read
    T2_SPECULATE = "t2_speculate"  # Agent + logged (drop, verify, flag)
    T3_COMMIT = "t3_commit"        # Human only via Friction Gate


# ═══════════════════════════════════════════════════════════════════════════
# Source trust levels (provenance coloring)
# ═══════════════════════════════════════════════════════════════════════════

class SourceTrust:
    GREEN = "green"    # FrictionDeck has original document
    YELLOW = "yellow"  # External RAG provided metadata
    GREY = "grey"      # Pure AI assertion, zero provenance


# ═══════════════════════════════════════════════════════════════════════════
# Overlay types
# ═══════════════════════════════════════════════════════════════════════════

class OverlayType:
    LOCK = "lock"
    COMMIT = "commit"
    CONTRADICTION = "contradiction"
    NEGATIVE_SPACE = "negative_space"
    NLI_RESULT = "nli_result"
    GROUP = "group"


# ═══════════════════════════════════════════════════════════════════════════
# Broadcast event types
# ═══════════════════════════════════════════════════════════════════════════

class BroadcastEvent:
    ARTIFACT_ADDED = "artifact_added"
    ARTIFACT_UPDATED = "artifact_updated"
    JUDGMENT_PROMOTED = "judgment_promoted"
    OVERLAY_CHANGED = "overlay_changed"
    CARD_MOVED = "card_moved"
    CARD_DELETED = "card_deleted"
    CARD_EDITED = "card_edited"
    PARAM_UPDATED = "param_updated"
    PARAM_LOCKED = "param_locked"
    PARAM_UNLOCKED = "param_unlocked"
    NLI_COMPLETE = "nli_complete"
    NEGATIVE_SPACE = "negative_space"
    NEGATIVE_SPACE_DISMISSED = "negative_space_dismissed"
    COMMIT_PROPOSED = "commit_proposed"
    COMMIT_APPROVED = "commit_approved"
    COMMIT_REJECTED = "commit_rejected"
    RELATION_ADDED = "relation_added"
    RELATION_REMOVED = "relation_removed"
    CARDS_GROUPED = "cards_grouped"
    NOTE_ADDED = "note_added"
    CONDITION_SWITCHED = "condition_switched"
