"""pipeline/constants.py — Canonical string constants for FrictionDeck v4.

Import from this module instead of repeating string literals.
"""


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

    # Proxy
    PROXY_FORWARDED = "proxy_forwarded"

    # CSP
    CSP_DOMAIN_ADDED = "csp_domain_added"

    # Plugins
    PLUGIN_PROPOSED = "plugin_proposed"
    PLUGIN_APPROVED = "plugin_approved"
    PLUGIN_REJECTED = "plugin_rejected"

    # Negative space
    NEGATIVE_SPACE_FLAGGED = "negative_space_flagged"


class JudgmentState:
    VISCOUS = "viscous"
    SOLID = "solid"
