"""Pure domain: LFD, cost, state machine, policy, validators. No I/O."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

CONTAINER_RE = re.compile(r"^[A-Z]{4}\d{7}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

STATUSES = (
    "ingested",
    "watching",
    "review",
    "nudging",
    "gated",
    "cleared",
    "missed",
)

ALLOWED_TRANSITIONS = {
    "ingested": {"watching", "review"},
    "watching": {"nudging", "review", "gated", "cleared", "missed"},
    "review": {"watching", "nudging", "gated", "cleared"},
    "nudging": {"gated", "cleared", "missed", "review"},
    "gated": {"cleared"},
    "cleared": set(),
    "missed": {"cleared", "gated"},
}

POLICY_WINDOWS = (72, 48, 24, 0)
CONFIDENCE_AUTO_SEND = 0.75
COST_OWNER_ACK = 500  # currency units; owner must ack if projected > this at T-24


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    text = str(value).strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def is_container_number(value: str | None) -> bool:
    if not value:
        return False
    return bool(CONTAINER_RE.match(value.strip().upper()))


def normalize_container(value: str) -> str:
    return value.strip().upper()


def is_email(value: str | None) -> bool:
    if not value:
        return False
    return bool(EMAIL_RE.match(value.strip()))


def compute_lfd(discharge: date, free_days: int, holidays: Iterable[date]) -> date:
    """Add free_days, skipping holidays so they do not consume free time."""
    holiday_set = set(holidays)
    remaining = max(int(free_days), 0)
    cursor = discharge
    if remaining == 0:
        return discharge
    while remaining > 0:
        cursor += timedelta(days=1)
        if cursor not in holiday_set:
            remaining -= 1
    return cursor


def days_left(lfd: date | None, today: date | None = None) -> int | None:
    if lfd is None:
        return None
    today = today or date.today()
    return (lfd - today).days


def hours_until_lfd(lfd: date, now: datetime | None = None) -> float:
    now = now or utc_now()
    end = datetime(lfd.year, lfd.month, lfd.day, 23, 59, 59, tzinfo=timezone.utc)
    return (end - now).total_seconds() / 3600.0


def risk_band(days: int | None, status: str) -> str:
    if status == "missed" or (days is not None and days < 0):
        return "burning"
    if days is None:
        return "watch"
    if days <= 0:
        return "critical"
    if days <= 3:
        return "watch"
    return "ok"


def rate_for_day(day: date, weekday_rate: float, weekend_rate: float, holidays: Iterable[date]) -> float:
    holiday_set = set(holidays)
    if day.weekday() >= 5 or day in holiday_set:
        return float(weekend_rate)
    return float(weekday_rate)


def projected_cost(
    lfd_final: date | None,
    as_of: date,
    weekday_rate: float,
    weekend_rate: float,
    holidays: Iterable[date],
) -> float:
    """Sum tariff for each calendar day after LFD through as_of inclusive."""
    if lfd_final is None or as_of <= lfd_final:
        return 0.0
    total = 0.0
    cursor = lfd_final + timedelta(days=1)
    while cursor <= as_of:
        total += rate_for_day(cursor, weekday_rate, weekend_rate, holidays)
        cursor += timedelta(days=1)
    return round(total, 2)


def extra_cost_if_gates(
    lfd_final: date | None,
    gate_a: date,
    gate_b: date,
    weekday_rate: float,
    weekend_rate: float,
    holidays: Iterable[date],
) -> float:
    a = projected_cost(lfd_final, gate_a, weekday_rate, weekend_rate, holidays)
    b = projected_cost(lfd_final, gate_b, weekday_rate, weekend_rate, holidays)
    return round(b - a, 2)


def can_transition(current: str, target: str) -> bool:
    if current == target:
        return True
    return target in ALLOWED_TRANSITIONS.get(current, set())


def policy_window(hours_left: float) -> int | None:
    if hours_left <= 0:
        return 0
    if hours_left <= 24:
        return 24
    if hours_left <= 48:
        return 48
    if hours_left <= 72:
        return 72
    return None


def idempotency_key(container_id: int, window: int, on_date: date) -> str:
    return f"{container_id}:{window}:{on_date.isoformat()}"


def confidence_score(
    model_score: float | None,
    valid_containers: bool,
    valid_email: bool,
    conflict: bool,
) -> float:
    model = 0.5 if model_score is None else max(0.0, min(1.0, float(model_score)))
    validator = 1.0
    if not valid_containers:
        validator *= 0.4
    if not valid_email:
        validator *= 0.7
    score = min(model, validator)
    if conflict:
        score = min(score, 0.55)
    return round(score, 3)


def should_auto_nudge(confidence: float, conflict: bool, status: str) -> bool:
    if status in {"review", "gated", "cleared", "missed"}:
        return False
    if status == "nudging":
        return True
    if conflict:
        return False
    return confidence >= CONFIDENCE_AUTO_SEND
