"""Reviewer discovery and persisted reviewer record helpers."""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from typing import Any


KNOWN_TRIGGERS = {
    "gemini-code-assist": "@gemini-code-assist",
}

KNOWN_DISPLAY_NAMES = {
    "gemini-code-assist": "Gemini Code Assist",
}


@dataclass(frozen=True)
class Candidate:
    login: str
    display_name: str
    review_trigger: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "login": self.login,
            "display_name": self.display_name,
            "review_trigger": self.review_trigger,
        }


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def display_name_for(login: str) -> str:
    known = KNOWN_DISPLAY_NAMES.get(login)
    if known:
        return known
    words = re.split(r"[-_\s]+", login.replace("[bot]", " bot"))
    return " ".join(word.capitalize() for word in words if word) or login


def trigger_for(login: str) -> str | None:
    return KNOWN_TRIGGERS.get(login)


def make_reviewer_record(
    login: str,
    *,
    display_name: str | None = None,
    review_trigger: str | None = None,
    source: str,
    selected_at: str | None = None,
) -> dict[str, Any]:
    return {
        "login": login,
        "display_name": display_name or display_name_for(login),
        "review_trigger": review_trigger if review_trigger is not None else trigger_for(login),
        "source": source,
        "selected_at": selected_at or now_iso(),
    }


def _iter_comments(thread: dict[str, Any]) -> list[dict[str, Any]]:
    comments = thread.get("comments")
    if isinstance(comments, list):
        return [comment for comment in comments if isinstance(comment, dict)]
    if isinstance(comments, dict):
        nodes = comments.get("nodes")
        if isinstance(nodes, list):
            return [comment for comment in nodes if isinstance(comment, dict)]
    return []


def _is_bot_author(author: dict[str, Any]) -> bool:
    typename = author.get("__typename")
    login = author.get("login")
    if typename == "Bot":
        return True
    if isinstance(login, str):
        return login.endswith("[bot]")
    return False


def discover_candidates(
    pull_request: dict[str, Any],
    *,
    self_login: str | None,
) -> list[Candidate]:
    first_seen: dict[str, int] = {}
    threads = (pull_request.get("reviewThreads") or {}).get("nodes") or []
    for thread_index, thread in enumerate(threads):
        if not isinstance(thread, dict):
            continue
        for comment in _iter_comments(thread):
            author = comment.get("author")
            if not isinstance(author, dict):
                continue
            login = author.get("login")
            if not isinstance(login, str) or not login:
                continue
            if self_login and login == self_login:
                continue
            if login in first_seen or not _is_bot_author(author):
                continue
            first_seen[login] = thread_index
    return [
        Candidate(
            login=login,
            display_name=display_name_for(login),
            review_trigger=trigger_for(login),
        )
        for login, _thread_index in sorted(
            first_seen.items(), key=lambda item: (item[1], item[0])
        )
    ]
