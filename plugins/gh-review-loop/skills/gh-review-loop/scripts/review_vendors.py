"""Known-reviewer vendor records shared by discovery, fetch, and re-review.

Reviewer discovery works for any bot, but the loop can only drive a reviewer
end-to-end when it knows how to ping it. This registry holds that per-vendor
knowledge: the display name, the mention, the exact re-review phrase the vendor
accepts, and whether it reviews a new PR on its own.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReviewVendor:
    login: str
    display_name: str
    mention: str
    rereview_phrase: str
    auto_reviews: bool
    # Set when a vendor's *free or individual* tier no longer performs reviews
    # while a paid or enterprise tier still does. Such a vendor stays fully
    # selectable and fully supported; it is only disqualified from being the
    # default, because a default applies to every user and most users are on
    # the tier that no longer works.
    consumer_tier_retired: bool = False
    tier_note: str = ""


# Google shut down the *consumer* Gemini Code Assist GitHub app on 2026-07-17;
# new consumer installs were blocked from 2026-06-18. The enterprise app is
# unaffected and still posts reviews, so Gemini remains a fully supported
# reviewer here. It is only disqualified as the *default*, since a new user on
# the consumer tier can no longer install the app at all.
# https://developers.google.com/gemini-code-assist/docs/deprecations/consumer-code-review
GEMINI = ReviewVendor(
    login="gemini-code-assist",
    display_name="Gemini Code Assist",
    mention="@gemini-code-assist",
    rereview_phrase="@gemini-code-assist please review the latest changes.",
    auto_reviews=True,
    consumer_tier_retired=True,
    tier_note=(
        "Gemini Code Assist reviews on GitHub now require the enterprise app. "
        "The consumer app was shut down on 2026-07-17. If you are on the "
        "enterprise app this works normally; if not, pick another reviewer."
    ),
)

# Codex answers to ``@codex``; its GraphQL author login is the connector app.
# It only reviews when pinged, so the loop must post the trigger itself for
# cycle 0 instead of waiting for a review that never arrives.
CODEX = ReviewVendor(
    login="chatgpt-codex-connector",
    display_name="Codex",
    mention="@codex",
    rereview_phrase="@codex review",
    auto_reviews=False,
)

KNOWN_VENDORS = {vendor.login: vendor for vendor in (GEMINI, CODEX)}

# The reviewer the loop assumes when the user has not chosen one. Must be a
# vendor that is alive and that the loop can trigger itself: an auto-reviewing
# default turns "no reviewer here" into a silent wait, while a ping-first
# default turns it into a working cycle 0.
DEFAULT_VENDOR = CODEX


def is_consumer_tier_retired(name: str | None) -> bool:
    """True when the vendor still reviews, but only on a paid or enterprise tier.

    This is a defaulting signal, not a support signal. Such a vendor works
    exactly as well as any other for the users who have it.
    """
    vendor = vendor_for(name)
    return bool(vendor and vendor.consumer_tier_retired)


def tier_note_for(name: str | None) -> str:
    """Return the vendor's tier caveat, or "" when it has none."""
    vendor = vendor_for(name)
    return vendor.tier_note if vendor else ""

# Callers hold either the account login (`chatgpt-codex-connector`) or the
# handle it answers to (`@codex`); both name the same vendor.
_ALIASES = {vendor.mention.lstrip("@").lower(): vendor for vendor in KNOWN_VENDORS.values()}
_ALIASES.update({login.lower(): vendor for login, vendor in KNOWN_VENDORS.items()})


def vendor_for(name: str | None) -> ReviewVendor | None:
    """Return the vendor record for a reviewer login or mention, else None.

    GitHub renders bot logins with a ``[bot]`` suffix in some APIs and without
    it in GraphQL's ``author.login``; both spellings resolve to one vendor.
    """
    if not isinstance(name, str):
        return None
    name = name.strip().lstrip("@")
    if name.endswith("[bot]"):
        name = name[: -len("[bot]")]
    return _ALIASES.get(name.lower())
