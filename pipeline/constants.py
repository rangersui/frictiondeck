"""pipeline/constants.py — Canonical string constants for FrictionDeck v4.

All magic strings that appear in more than one file live here.
Import from this module instead of repeating string literals.

Usage:
    from pipeline.constants import EventType, JudgmentState, TrustTier
"""


# ═══════════════════════════════════════════════════════════════════════════
# Audit event types
# ═══════════════════════════════════════════════════════════════════════════

class EventType:
    # Stage DOM mutations (AI via MCP)
    STAGE_MUTATED = "stage_mutated"
    STAGE_APPENDED = "stage_appended"

    # Judgment lifecycle
    JUDGMENT_PROMOTED = "judgment_promoted"

    # Commit lifecycle
    COMMIT_PROPOSED = "commit_proposed"
    COMMIT_APPROVED = "commit_approved"
    COMMIT_REJECTED = "commit_rejected"

    # Parameter operations
    PARAM_LOCKED = "param_locked"
    PARAM_UNLOCKED = "param_unlocked"

    # Proxy
    PROXY_FORWARDED = "proxy_forwarded"

    # CSP
    CSP_DOMAIN_ADDED = "csp_domain_added"

    # Negative space
    NEGATIVE_SPACE_FLAGGED = "negative_space_flagged"



# ═══════════════════════════════════════════════════════════════════════════
# Two-state model: viscous → solid
# ═══════════════════════════════════════════════════════════════════════════

class JudgmentState:
    VISCOUS = "viscous"     # Promoted judgment, constrained, tracked
    SOLID = "solid"         # Committed, HMAC sealed, irreversible


# ═══════════════════════════════════════════════════════════════════════════
# Trust tiers
# ═══════════════════════════════════════════════════════════════════════════

class TrustTier:
    T1_READ = "t1_read"            # Any agent can read
    T2_SPECULATE = "t2_speculate"  # Agent + logged (mutate, promote, flag)
    T3_COMMIT = "t3_commit"        # Human only


# ═══════════════════════════════════════════════════════════════════════════
# Broadcast event types
# ═══════════════════════════════════════════════════════════════════════════

class BroadcastEvent:
    STAGE_UPDATED = "stage_updated"
    JUDGMENT_PROMOTED = "judgment_promoted"
    PARAM_LOCKED = "param_locked"
    PARAM_UNLOCKED = "param_unlocked"
    NEGATIVE_SPACE = "negative_space"
    COMMIT_PROPOSED = "commit_proposed"
    COMMIT_APPROVED = "commit_approved"
    COMMIT_REJECTED = "commit_rejected"
