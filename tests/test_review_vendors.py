"""Tests for the known-reviewer vendor registry."""

from __future__ import annotations

import review_vendors


def test_codex_vendor_is_known_by_its_graphql_login():
    vendor = review_vendors.vendor_for("chatgpt-codex-connector")

    assert vendor is not None
    assert vendor.display_name == "Codex"
    assert vendor.mention == "@codex"


def test_codex_rereview_phrase_is_the_exact_trigger_codex_accepts():
    vendor = review_vendors.vendor_for("chatgpt-codex-connector")

    assert vendor.rereview_phrase == "@codex review"


def test_bot_suffixed_login_resolves_to_the_same_vendor():
    assert review_vendors.vendor_for("chatgpt-codex-connector[bot]") is review_vendors.CODEX
    assert review_vendors.vendor_for("gemini-code-assist[bot]") is review_vendors.GEMINI


def test_gemini_keeps_its_sentence_shaped_rereview_phrase():
    vendor = review_vendors.vendor_for("gemini-code-assist")

    assert vendor.mention == "@gemini-code-assist"
    assert vendor.rereview_phrase == "@gemini-code-assist please review the latest changes."


def test_vendor_resolves_from_its_mention_too():
    assert review_vendors.vendor_for("@codex") is review_vendors.CODEX
    assert review_vendors.vendor_for("codex") is review_vendors.CODEX


def test_unknown_reviewer_login_has_no_vendor_record():
    assert review_vendors.vendor_for("coderabbitai") is None


def test_codex_does_not_review_until_it_is_pinged():
    assert review_vendors.CODEX.auto_reviews is False
    assert review_vendors.GEMINI.auto_reviews is True
