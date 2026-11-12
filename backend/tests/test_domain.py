from datetime import date, datetime, timezone

from domain import (
    CONTAINER_RE,
    can_transition,
    compute_lfd,
    confidence_score,
    extra_cost_if_gates,
    hours_until_lfd,
    idempotency_key,
    is_container_number,
    policy_window,
    projected_cost,
    should_auto_nudge,
)


def test_container_regex():
    assert is_container_number("MSKU7834210")
    assert is_container_number("tclu2291883")  # normalized to upper
    assert CONTAINER_RE.match("TCLU2291883")
    assert not is_container_number("BOX-1")
    assert not is_container_number("")


def test_lfd_skips_holidays():
    discharge = date(2026, 11, 7)
    holidays = {date(2026, 11, 10)}
    # 7 free days, 10 Nov does not count
    got = compute_lfd(discharge, 7, holidays)
    plain = compute_lfd(discharge, 7, [])
    assert got == date(2026, 11, 15)
    assert plain == date(2026, 11, 14)
    assert got > plain


def test_cost_engine_weekday_weekend():
    lfd = date(2026, 11, 13)  # Friday
    # Sat 14 weekend 200, Sun 15 weekend 200, Mon 16 weekday 100
    as_of = date(2026, 11, 16)
    total = projected_cost(lfd, as_of, 100, 200, [])
    assert total == 500
    assert projected_cost(lfd, lfd, 100, 200, []) == 0


def test_what_if_extra_cost():
    lfd = date(2026, 11, 12)
    extra = extra_cost_if_gates(lfd, date(2026, 11, 13), date(2026, 11, 16), 100, 100, [])
    assert extra == 300


def test_illegal_state_jump():
    assert can_transition("watching", "nudging")
    assert can_transition("watching", "review")
    assert not can_transition("cleared", "watching")
    assert not can_transition("ingested", "cleared")


def test_policy_window_and_idempotency():
    now = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
    # LFD end of 2026-11-13 → a bit over 24h, under 48h
    assert policy_window(hours_until_lfd(date(2026, 11, 13), now)) == 48
    assert policy_window(-3) == 0
    assert policy_window(10) == 24
    assert policy_window(30) == 48
    assert policy_window(60) == 72
    assert policy_window(100) is None
    a = idempotency_key(1, 48, date(2026, 11, 12))
    b = idempotency_key(1, 48, date(2026, 11, 12))
    c = idempotency_key(1, 24, date(2026, 11, 12))
    assert a == b
    assert a != c


def test_auto_nudge_blocked_on_conflict_or_low_confidence():
    assert should_auto_nudge(0.9, False, "watching")
    assert not should_auto_nudge(0.9, True, "watching")
    assert not should_auto_nudge(0.5, False, "watching")
    assert not should_auto_nudge(0.99, False, "review")
    assert should_auto_nudge(0.5, True, "nudging")
    score = confidence_score(0.95, True, True, True)
    assert score <= 0.55
