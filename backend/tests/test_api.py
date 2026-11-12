import os
import tempfile
from pathlib import Path

os.environ["LASTDAY_DB"] = str(Path(tempfile.mkdtemp()) / "test.sqlite")

from fastapi.testclient import TestClient

from app import app


client = TestClient(app)


def test_health_and_login():
    h = client.get("/api/health")
    assert h.status_code == 200
    assert h.json()["ok"] is True
    bad = client.post("/api/auth/login", json={"username": "ops", "password": "nope"})
    assert bad.status_code == 401
    ok = client.post("/api/auth/login", json={"username": "ops", "password": "ops"})
    assert ok.status_code == 200
    assert ok.json()["role"] == "ops"


def test_sample_parse_conflict_goes_to_review():
    token = client.post("/api/auth/login", json={"username": "ops", "password": "ops"}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    text = (Path(__file__).parent.parent / "sample_notice.txt").read_text()
    parsed = client.post("/api/ingest/parse", json={"text": text}, headers=headers)
    assert parsed.status_code == 200
    data = parsed.json()
    assert data["conflict"] is True
    assert data["needs_review"] is True
    numbers = [c["number"] for c in data["reconciled"]]
    assert numbers == ["MSKU7834210", "TCLU2291883"]
    tclu = next(c for c in data["reconciled"] if c["number"] == "TCLU2291883")
    assert tclu["conflict"] is True
    saved = client.post(
        "/api/bls",
        json={"raw_source": text, "source_type": "paste", "extract": data["extract"], "reconciled": data["reconciled"]},
        headers=headers,
    )
    assert saved.status_code == 200
    assert saved.json()["status"] == "review"
    review = client.get("/api/review", headers=headers)
    assert any(c["number"] == "TCLU2291883" for c in review.json())
    scan = client.post("/api/jobs/scan-deadlines", headers={"X-Scan-Secret": "lastday-dev"})
    body = scan.json()
    assert body["queued"] == []
    assert len(body["blocked"]) == 2
    assert all(row["reason"] == "review" for row in body["blocked"])
