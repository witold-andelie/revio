"""Smoke tests for the REPL natural-language router (offline / keyword path).

These exercise the deterministic fallback classifier — no LLM, no API key —
which is what runs when the LLM router is unavailable AND is the place where
the routing rules are easiest to pin down. The LLM path uses the same intent
vocabulary (see `_INTENT_SYSTEM`).
"""

from revio.cli.repl import (
    _ALLOWED_SLASH_FROM_NL,
    _VALID_INTENTS,
    _keyword_classify,
    _keyword_config_slash,
)


# --- config: NL -> whitelisted slash ----------------------------------------


def test_set_model_maps_to_model_slash():
    c = _keyword_classify("switch the model to claude-opus-4-7")
    assert c["intent"] == "config"
    assert c["slash"] == "/model claude-opus-4-7"


def test_api_key_never_carries_a_value():
    c = _keyword_classify("set my api key")
    assert c["intent"] == "config"
    # SECURITY: the key change must always be the bare prompt-driven command.
    assert c["slash"] == "/key"


def test_budget_with_number_chinese():
    c = _keyword_classify("把预算调到 30")
    assert c["intent"] == "config"
    assert c["slash"] == "/budget 30"


def test_endpoint_url_extracted():
    c = _keyword_classify("用 deepseek 的接口 https://api.deepseek.com")
    assert c["intent"] == "config"
    assert c["slash"] == "/url https://api.deepseek.com"


def test_switch_mode_beats_dedup_keyword():
    # "dedup" appears, but the user wants to switch the default mode.
    c = _keyword_classify("switch to dedup mode")
    assert c["intent"] == "config"
    assert c["slash"] == "/mode dedup"


def test_cost_needs_no_verb():
    c = _keyword_classify("how much did this cost")
    assert c["intent"] == "config"
    assert c["slash"] == "/cost"


def test_show_config():
    c = _keyword_classify("show my config")
    assert c["intent"] == "config"
    assert c["slash"] == "/config"


def test_every_config_slash_is_whitelisted():
    for phrase in (
        "set my api key",
        "switch the model to gpt-4o-mini",
        "budget 50",
        "use the endpoint https://x.example.com",
        "show my config",
        "how much did this cost",
        "switch to audit mode",
        "set the profile to python",
    ):
        c = _keyword_classify(phrase)
        if c["intent"] == "config":
            cmd = c["slash"].split()[0]
            assert cmd in _ALLOWED_SLASH_FROM_NL, (phrase, c["slash"])


# --- dedup: the "clean vibe-coding junk" example ----------------------------


def test_clean_junk_code_is_dedup_en():
    c = _keyword_classify("clean up the duplicate junk code")
    assert c["intent"] == "dedup"


def test_clean_junk_code_is_dedup_zh():
    c = _keyword_classify("去掉重复的废物代码")
    assert c["intent"] == "dedup"


# --- review / audit unchanged -----------------------------------------------


def test_review_commit():
    c = _keyword_classify("review the last commit")
    assert c["intent"] == "review"


def test_audit_security():
    c = _keyword_classify("audit the repo for security vulnerabilities")
    assert c["intent"] == "audit"


# --- unknown / unrelated falls back to capability (never a silent run) -------


def test_unrelated_request_does_not_launch_review():
    c = _keyword_classify("what's the weather in Paris today")
    assert c["intent"] == "capability"
    assert c["intent"] in _VALID_INTENTS


def test_config_slash_returns_none_for_review():
    # A plain review request must not be mistaken for a settings change.
    assert _keyword_config_slash("review the auth module for bugs") is None
