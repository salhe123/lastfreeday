import os

import httpx


def send_email(to_addr: str, subject: str, body: str) -> dict:
    key = os.getenv("SENDGRID_API_KEY", "").strip()
    from_addr = os.getenv("SENDGRID_FROM", "ops@example.com")
    if not key:
        return {"sent": False, "dry_run": True, "to": to_addr, "subject": subject, "body": body}
    payload = {
        "personalizations": [{"to": [{"email": to_addr}]}],
        "from": {"email": from_addr},
        "subject": subject,
        "content": [{"type": "text/plain", "value": body}],
    }
    with httpx.Client(timeout=20) as client:
        response = client.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
        )
        if response.status_code >= 300:
            raise RuntimeError(f"SendGrid {response.status_code}: {response.text}")
    return {"sent": True, "dry_run": False, "to": to_addr, "subject": subject}
