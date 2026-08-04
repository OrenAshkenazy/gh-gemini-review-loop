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


GEMINI = ReviewVendor(
    login="gemini-code-assist",
    display_name="Gemini Code Assist",
    mention="@gemini-code-assist",
    rereview_phrase="@gemini-code-assist please review the latest changes.",
    auto_reviews=True,
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
