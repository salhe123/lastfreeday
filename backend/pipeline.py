from datetime import date

from domain import (
    compute_lfd,
    confidence_score,
    is_container_number,
    is_email,
    normalize_container,
    parse_iso_date,
)
from gemini import offline_parse, parse_notice


ALLOWED_EXTRACT_KEYS = {
    "vessel",
    "voyage",
    "bl_number",
    "port",
    "consignee",
    "forwarder_name",
    "forwarder_email",
    "cargo",
    "confidence",
    "warnings",
    "containers",
    "prompt_version",
    "gemini_run_id",
}


def normalize_text(raw: str) -> str:
    return "\n".join(line.strip() for line in raw.replace("\r\n", "\n").split("\n")).strip()


def extract(raw_text: str, gemini_live: bool) -> dict:
    text = normalize_text(raw_text)
    if gemini_live:
        data = parse_notice(text)
    else:
        data = offline_parse(text)
    extra = set(data) - ALLOWED_EXTRACT_KEYS
    for key in extra:
        data.pop(key, None)
    containers = data.get("containers") or []
    if not isinstance(containers, list) or not containers:
        raise ValueError("extract has no containers")
    cleaned = []
    seen = {}
    for item in containers:
        number = normalize_container(str(item.get("number") or ""))
        if not number:
            raise ValueError("container missing number")
        row = {
            "number": number,
            "type": item.get("type"),
            "discharge_or_arrival": item.get("discharge_or_arrival"),
            "free_days": item.get("free_days"),
            "lfd_text": item.get("lfd_text"),
        }
        if number in seen:
            existing = cleaned[seen[number]]
            for key in ("type", "discharge_or_arrival", "free_days", "lfd_text"):
                if not existing.get(key) and row.get(key):
                    existing[key] = row[key]
            continue
        seen[number] = len(cleaned)
        cleaned.append(row)
    data["containers"] = cleaned
    data["warnings"] = list(data.get("warnings") or [])
    return data


def reconcile_container(item: dict, holidays: list[date], default_free_days: int | None) -> dict:
    discharge = parse_iso_date(item.get("discharge_or_arrival"))
    free_days = item.get("free_days")
    if free_days is None:
        free_days = default_free_days
    try:
        free_days = int(free_days) if free_days is not None else None
    except (TypeError, ValueError):
        free_days = None
    extracted = parse_iso_date(item.get("lfd_text"))
    computed = None
    if discharge is not None and free_days is not None:
        computed = compute_lfd(discharge, free_days, holidays)
    conflict = bool(extracted and computed and extracted != computed)
    lfd_final = computed or extracted
    valid_number = is_container_number(item.get("number"))
    return {
        **item,
        "discharge_date": discharge.isoformat() if discharge else None,
        "free_days": free_days,
        "lfd_extracted": extracted.isoformat() if extracted else None,
        "lfd_computed": computed.isoformat() if computed else None,
        "lfd_final": lfd_final.isoformat() if lfd_final else None,
        "conflict": conflict,
        "valid_number": valid_number,
    }


def score_run(extracted: dict, reconciled: list[dict]) -> tuple[float, bool]:
    valid_containers = all(c["valid_number"] for c in reconciled) and len(reconciled) > 0
    valid_email = is_email(extracted.get("forwarder_email"))
    conflict = any(c["conflict"] for c in reconciled)
    score = confidence_score(extracted.get("confidence"), valid_containers, valid_email, conflict)
    return score, conflict
