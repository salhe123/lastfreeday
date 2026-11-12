import json
import os
import re
import uuid

import httpx

PROMPT_VERSION = "extract_v2"

EXTRACT_PROMPT = f"""You extract container free-day facts from an arrival notice or bill of lading excerpt.
prompt_version={PROMPT_VERSION}
Return ONLY JSON with this shape and no extra keys:
{{
  "vessel": string or null,
  "voyage": string or null,
  "bl_number": string or null,
  "port": string or null,
  "consignee": string or null,
  "forwarder_name": string or null,
  "forwarder_email": string or null,
  "cargo": string or null,
  "confidence": number between 0 and 1,
  "warnings": string[],
  "containers": [
    {{
      "number": string,
      "type": string or null,
      "discharge_or_arrival": "YYYY-MM-DD" or null,
      "free_days": number or null,
      "lfd_text": "YYYY-MM-DD" or null
    }}
  ]
}}
Rules:
- Dates must be ISO YYYY-MM-DD.
- Split every container onto its own object. Do not collapse a BL into one row.
- Container numbers look like MSKU7834210 (4 letters + 7 digits).
- Never invent last free day if the notice has no date to work from.
- confidence is how sure you are of the container numbers and LFDs.
Notice:
"""

DRAFT_PROMPT = """Draft a short professional email from an Ethiopian importer to their freight forwarder.
Return ONLY JSON: {"subject": string, "body": string}
Window meaning: 72=pickup window opening, 48=still in port, 24=urgent, 0=last free day today or past — do not reuse the 48-hour body for window 0.
Facts:
"""


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    return json.loads(text)


def _generate(prompt: str) -> dict:
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    with httpx.Client(timeout=40) as client:
        response = client.post(url, params={"key": key}, json=payload)
        response.raise_for_status()
        data = response.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return _extract_json(text)


def parse_notice(raw_text: str) -> dict:
    extracted = _generate(EXTRACT_PROMPT + raw_text)
    extracted["prompt_version"] = PROMPT_VERSION
    extracted["gemini_run_id"] = str(uuid.uuid4())
    return extracted


def draft_nudge(facts: dict) -> dict:
    return _generate(DRAFT_PROMPT + json.dumps(facts, default=str))


def offline_parse(raw_text: str) -> dict:
    """Deterministic extract for tests / no API key. Marked so the UI does not pretend Gemini ran."""
    def grab(label: str) -> str | None:
        match = re.search(rf"{label}\s*[:\-]\s*(.+)", raw_text, re.I)
        return match.group(1).strip() if match else None

    containers = []
    for match in re.finditer(r"\b([A-Z]{4}\d{7})\b(?:\s+(\d{2}HC|\d{2}OT|\d{2}GP))?", raw_text):
        containers.append(
            {
                "number": match.group(1),
                "type": match.group(2),
                "discharge_or_arrival": None,
                "free_days": None,
                "lfd_text": None,
            }
        )
    eta = None
    eta_line = grab(r"ETA\s*/\s*discharged") or grab("ETA") or grab("discharged")
    if eta_line:
        date_match = re.search(r"\d{4}-\d{2}-\d{2}", eta_line)
        if date_match:
            eta = date_match.group(0)
    free = grab("Free time") or grab("Free days")
    free_days = None
    if free:
        num = re.search(r"\d+", free)
        if num:
            free_days = int(num.group(0))
    lfd_match = re.search(r"Last free day[^\n]*?(\d{4}-\d{2}-\d{2})", raw_text, re.I)
    lfd_global = lfd_match.group(1) if lfd_match else None
    per = {m.group(1): m.group(2) for m in re.finditer(
        r"\b([A-Z]{4}\d{7})\b[^\n]*?LFD\s*[:\-]\s*(\d{4}-\d{2}-\d{2})", raw_text, re.I
    )}
    for c in containers:
        c["discharge_or_arrival"] = eta
        c["free_days"] = free_days
        c["lfd_text"] = per.get(c["number"], lfd_global)
    email = None
    em = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", raw_text)
    if em:
        email = em.group(0)
    return {
        "vessel": (grab("Vessel") or "").split("\n")[0] or None,
        "voyage": (grab("Voyage") or "").split("\n")[0] or None,
        "bl_number": grab("BL") or grab("B/L") or grab("Bill of lading"),
        "port": grab("Port"),
        "consignee": grab("Consignee"),
        "forwarder_name": grab("From"),
        "forwarder_email": email,
        "cargo": grab("Cargo"),
        "confidence": 0.62,
        "warnings": ["offline_parse: GEMINI_API_KEY missing; queued for review"],
        "containers": containers,
        "prompt_version": "offline_v2",
        "gemini_run_id": "offline",
    }
