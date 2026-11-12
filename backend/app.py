import io
import json
import os
import uuid
from contextvars import ContextVar
from datetime import date, timedelta

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from pathlib import Path

from auth import current_user, login, require_roles
from calendar_util import ics_for_container
from db import (
    add_audit,
    connect,
    get_container,
    holiday_dates,
    init_db,
    list_containers,
    now_iso,
)
from domain import (
    COST_OWNER_ACK,
    can_transition,
    extra_cost_if_gates,
    hours_until_lfd,
    idempotency_key,
    parse_iso_date,
    policy_window,
    projected_cost,
    should_auto_nudge,
)
from gemini import draft_nudge
from mailer import send_email
from pipeline import extract, reconcile_container, score_run

load_dotenv()
init_db()

request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")

app = FastAPI(title="LastFreeDay", version="2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    rid = request.headers.get("X-Request-Id") or str(uuid.uuid4())
    token = request_id_ctx.set(rid)
    try:
        response = await call_next(request)
        response.headers["X-Request-Id"] = rid
        return response
    finally:
        request_id_ctx.reset(token)


class LoginIn(BaseModel):
    username: str
    password: str


class ParseIn(BaseModel):
    text: str = Field(min_length=20)


class PersistIn(BaseModel):
    raw_source: str
    source_type: str = "paste"
    extract: dict
    reconciled: list[dict] = []


class PatchContainer(BaseModel):
    version: int
    status: str | None = None
    forwarder_email: str | None = None
    lfd_final: str | None = None


class TariffIn(BaseModel):
    line: str
    port: str
    free_days: int
    weekday_rate: float
    weekend_rate: float
    currency: str = "USD"


def gemini_live() -> bool:
    return bool(os.getenv("GEMINI_API_KEY", "").strip())


def pick_tariff(conn, port: str | None):
    rows = conn.execute("SELECT * FROM tariffs").fetchall()
    if not rows:
        return None
    if port:
        for row in rows:
            if row["port"].lower() in port.lower() or port.lower() in row["port"].lower():
                return dict(row)
    return dict(rows[0])


def bl_row(conn, bl_id: int) -> dict:
    row = conn.execute("SELECT * FROM bills_of_lading WHERE id = ?", (bl_id,)).fetchone()
    return dict(row) if row else {}


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "gemini": gemini_live(),
        "sendgrid": bool(os.getenv("SENDGRID_API_KEY", "").strip()),
        "calendar": "ics",
        "mail": "live" if os.getenv("SENDGRID_API_KEY", "").strip() else "dry_run",
    }


@app.post("/api/auth/login")
def auth_login(body: LoginIn):
    return login(body.username, body.password)


@app.get("/api/sample")
def sample_notice():
    path = Path(__file__).parent / "sample_notice.txt"
    return {"text": path.read_text()}


@app.post("/api/ingest/parse")
def ingest_parse(body: ParseIn, user: dict = Depends(require_roles("owner", "ops"))):
    try:
        extracted = extract(body.text, gemini_live())
    except Exception as exc:
        raise HTTPException(502, f"extract failed: {exc}") from exc
    conn = connect()
    holidays = holiday_dates(conn)
    tariff = pick_tariff(conn, extracted.get("port"))
    default_free = tariff["free_days"] if tariff else 7
    conn.close()
    reconciled = [reconcile_container(c, holidays, default_free) for c in extracted["containers"]]
    score, conflict = score_run(extracted, reconciled)
    extracted["confidence"] = score
    return {
        "extract": extracted,
        "reconciled": reconciled,
        "confidence": score,
        "conflict": conflict,
        "needs_review": score < 0.75 or conflict,
        "tariff": tariff,
        "prompt_version": extracted.get("prompt_version"),
    }


@app.post("/api/ingest/pdf")
async def ingest_pdf(file: UploadFile = File(...), user: dict = Depends(require_roles("owner", "ops"))):
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise HTTPException(500, "pypdf not installed") from exc
    data = await file.read()
    reader = PdfReader(io.BytesIO(data))
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    if len(text.strip()) < 20:
        raise HTTPException(400, "no text layer in PDF (photo OCR is design-only)")
    return ingest_parse(ParseIn(text=text), user)


@app.post("/api/bls")
def persist_bl(body: PersistIn, user: dict = Depends(require_roles("owner", "ops"))):
    extracted = body.extract
    conn = connect()
    holidays = holiday_dates(conn)
    tariff = pick_tariff(conn, extracted.get("port"))
    default_free = tariff["free_days"] if tariff else 7
    reconciled = body.reconciled or [
        reconcile_container(c, holidays, default_free) for c in (extracted.get("containers") or [])
    ]
    score, conflict = score_run(extracted, reconciled)
    status = "review" if (score < 0.75 or conflict) else "watching"
    cur = conn.execute(
        """
        INSERT INTO bills_of_lading (
            bl_number, vessel, voyage, port, consignee, forwarder_name, forwarder_email,
            cargo, raw_source, source_type, gemini_run_id, prompt_version, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            extracted.get("bl_number"),
            extracted.get("vessel"),
            extracted.get("voyage"),
            extracted.get("port"),
            extracted.get("consignee"),
            extracted.get("forwarder_name"),
            extracted.get("forwarder_email") or os.getenv("FORWARDER_EMAIL"),
            extracted.get("cargo"),
            body.raw_source,
            body.source_type,
            extracted.get("gemini_run_id"),
            extracted.get("prompt_version"),
            now_iso(),
        ),
    )
    bl_id = cur.lastrowid
    created = []
    for item in reconciled:
        cost = 0.0
        if tariff and item.get("lfd_final"):
            cost = projected_cost(
                parse_iso_date(item["lfd_final"]),
                date.today(),
                tariff["weekday_rate"],
                tariff["weekend_rate"],
                holidays,
            )
        cur = conn.execute(
            """
            INSERT INTO containers (
                bl_id, number, type, discharge_date, free_days, lfd_extracted, lfd_computed,
                lfd_final, status, projected_cost, confidence, conflict, version, tariff_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                bl_id,
                item["number"],
                item.get("type"),
                item.get("discharge_date"),
                item.get("free_days"),
                item.get("lfd_extracted"),
                item.get("lfd_computed"),
                item.get("lfd_final"),
                status,
                cost,
                score,
                1 if item.get("conflict") else 0,
                tariff["id"] if tariff else None,
                now_iso(),
            ),
        )
        created.append(cur.lastrowid)
        add_audit(
            conn,
            actor=user["username"],
            event_type="parsed",
            payload={"status": status, "confidence": score, "conflict": item.get("conflict")},
            request_id=request_id_ctx.get(),
            container_id=cur.lastrowid,
            bl_id=bl_id,
        )
    conn.commit()
    items = [get_container(conn, cid) for cid in created]
    conn.close()
    return {"bl_id": bl_id, "status": status, "confidence": score, "containers": items}


@app.get("/api/board")
def board(user: dict = Depends(current_user)):
    conn = connect()
    items = list_containers(conn)
    conn.close()
    open_items = [c for c in items if c["status"] not in {"cleared"}]
    exposure = sum(c.get("projected_cost") or 0 for c in open_items if c["status"] in {"watching", "nudging", "review", "missed"})
    bands = {"ok": 0, "watch": 0, "critical": 0, "burning": 0}
    for c in open_items:
        if c["status"] == "review":
            continue
        bands[c["risk_band"]] = bands.get(c["risk_band"], 0) + 1
    return {
        "exposure": round(exposure, 2),
        "currency": next((c.get("currency") for c in open_items if c.get("currency")), "USD"),
        "bands": bands,
        "review": sum(1 for c in open_items if c["status"] == "review"),
        "containers": items,
    }


@app.get("/api/containers")
def containers(status: str | None = None, user: dict = Depends(current_user)):
    conn = connect()
    items = list_containers(conn, status)
    conn.close()
    return items


@app.get("/api/containers/{container_id}")
def container_detail(container_id: int, user: dict = Depends(current_user)):
    conn = connect()
    item = get_container(conn, container_id)
    if not item:
        conn.close()
        raise HTTPException(404, "container not found")
    bl = bl_row(conn, item["bl_id"])
    holidays = holiday_dates(conn)
    tariff = item.get("tariff")
    lfd = parse_iso_date(item.get("lfd_final"))
    today = date.today()
    what_if = None
    if tariff and lfd:
        tomorrow = today + timedelta(days=1)
        in_three = today + timedelta(days=3)
        what_if = {
            "if_gates_tomorrow": projected_cost(lfd, tomorrow, tariff["weekday_rate"], tariff["weekend_rate"], holidays),
            "if_gates_in_3_days": projected_cost(lfd, in_three, tariff["weekday_rate"], tariff["weekend_rate"], holidays),
            "extra_if_waits_3_days": extra_cost_if_gates(
                lfd, tomorrow, in_three, tariff["weekday_rate"], tariff["weekend_rate"], holidays
            ),
            "currency": tariff["currency"],
        }
    events = [dict(r) for r in conn.execute(
        "SELECT * FROM audit_events WHERE container_id = ? ORDER BY id DESC",
        (container_id,),
    ).fetchall()]
    for ev in events:
        try:
            ev["payload"] = json.loads(ev["payload"] or "{}")
        except json.JSONDecodeError:
            pass
    outbox = [dict(r) for r in conn.execute(
        "SELECT * FROM outbox WHERE container_id = ? ORDER BY id DESC",
        (container_id,),
    ).fetchall()]
    conn.close()
    item["bl"] = bl
    item["what_if"] = what_if
    item["events"] = events
    item["outbox"] = outbox
    item["holidays"] = [d.isoformat() for d in holidays]
    return item


@app.patch("/api/containers/{container_id}")
def patch_container(container_id: int, body: PatchContainer, user: dict = Depends(require_roles("owner", "ops"))):
    conn = connect()
    item = get_container(conn, container_id)
    if not item:
        conn.close()
        raise HTTPException(404, "container not found")
    if body.version != item["version"]:
        conn.close()
        raise HTTPException(409, "stale version — reload")
    if body.status:
        if user["role"] != "owner" and body.status in {"cleared", "gated"} and item["status"] == "review":
            conn.close()
            raise HTTPException(403, "owner must clear review items")
        if not can_transition(item["status"], body.status):
            conn.close()
            raise HTTPException(400, f"illegal jump {item['status']} → {body.status}")
        conn.execute("UPDATE containers SET status = ? WHERE id = ?", (body.status, container_id))
    if body.lfd_final is not None:
        conn.execute("UPDATE containers SET lfd_final = ? WHERE id = ?", (body.lfd_final, container_id))
    if body.forwarder_email is not None:
        conn.execute(
            "UPDATE bills_of_lading SET forwarder_email = ? WHERE id = ?",
            (body.forwarder_email, item["bl_id"]),
        )
    conn.execute("UPDATE containers SET version = version + 1 WHERE id = ?", (container_id,))
    add_audit(
        conn,
        actor=user["username"],
        event_type="override",
        payload=body.model_dump(),
        request_id=request_id_ctx.get(),
        container_id=container_id,
        bl_id=item["bl_id"],
    )
    conn.commit()
    updated = get_container(conn, container_id)
    conn.close()
    return updated


@app.post("/api/containers/{container_id}/approve-nudge")
def approve_nudge(container_id: int, user: dict = Depends(require_roles("owner"))):
    conn = connect()
    item = get_container(conn, container_id)
    if not item:
        conn.close()
        raise HTTPException(404, "container not found")
    if item["status"] not in {"review", "watching"}:
        conn.close()
        raise HTTPException(400, "nothing to approve")
    if not can_transition(item["status"], "watching"):
        conn.close()
        raise HTTPException(400, "illegal jump")
    conn.execute("UPDATE containers SET status = 'watching', version = version + 1 WHERE id = ?", (container_id,))
    queued = _enqueue_window(conn, get_container(conn, container_id), force=True)
    add_audit(
        conn,
        actor=user["username"],
        event_type="approve_nudge",
        payload={"outbox": queued},
        request_id=request_id_ctx.get(),
        container_id=container_id,
        bl_id=item["bl_id"],
    )
    conn.commit()
    updated = get_container(conn, container_id)
    conn.close()
    return {"ok": True, "container": updated, "queued": queued}


@app.get("/api/review")
def review_queue(user: dict = Depends(current_user)):
    conn = connect()
    items = list_containers(conn, "review")
    conn.close()
    return items


@app.get("/api/tariffs")
def get_tariffs(user: dict = Depends(current_user)):
    conn = connect()
    rows = [dict(r) for r in conn.execute("SELECT * FROM tariffs ORDER BY line, port").fetchall()]
    conn.close()
    return rows


@app.put("/api/tariffs")
def put_tariff(body: TariffIn, user: dict = Depends(require_roles("owner"))):
    conn = connect()
    conn.execute(
        """
        INSERT INTO tariffs (line, port, free_days, weekday_rate, weekend_rate, currency)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(line, port) DO UPDATE SET
            free_days=excluded.free_days,
            weekday_rate=excluded.weekday_rate,
            weekend_rate=excluded.weekend_rate,
            currency=excluded.currency
        """,
        (body.line, body.port, body.free_days, body.weekday_rate, body.weekend_rate, body.currency),
    )
    conn.commit()
    rows = [dict(r) for r in conn.execute("SELECT * FROM tariffs ORDER BY line, port").fetchall()]
    conn.close()
    return rows


@app.post("/api/containers/{container_id}/calendar")
def attach_calendar(container_id: int, user: dict = Depends(require_roles("owner", "ops"))):
    conn = connect()
    item = get_container(conn, container_id)
    if not item:
        conn.close()
        raise HTTPException(404, "container not found")
    bl = bl_row(conn, item["bl_id"])
    ics = ics_for_container(item, bl)
    add_audit(
        conn,
        actor=user["username"],
        event_type="calendar",
        payload={"ics": True},
        request_id=request_id_ctx.get(),
        container_id=container_id,
        bl_id=item["bl_id"],
    )
    conn.commit()
    conn.close()
    return {"ok": True, "mode": "ics", "ics": ics}


@app.get("/api/containers/{container_id}/calendar.ics")
def download_ics(container_id: int, user: dict = Depends(current_user)):
    conn = connect()
    item = get_container(conn, container_id)
    if not item:
        conn.close()
        raise HTTPException(404, "container not found")
    bl = bl_row(conn, item["bl_id"])
    conn.close()
    return PlainTextResponse(ics_for_container(item, bl), media_type="text/calendar")


def _template(window: int, item: dict, bl: dict) -> tuple[str, str]:
    number = item["number"]
    lfd = item.get("lfd_final")
    cost = item.get("projected_cost")
    if window == 72:
        subject = f"Pickup window opening — {number} LFD {lfd}"
        body = f"Pickup window is opening for {number} at {bl.get('port')}. Last free day {lfd}. Please arrange haulage."
    elif window == 48:
        subject = f"Still in port — {number} LFD {lfd}"
        body = f"{number} is still in port. Last free day {lfd}. Copying owner. Projected exposure {cost}."
    elif window == 24:
        subject = f"URGENT pickup — {number} LFD {lfd}"
        body = f"Urgent: {number} last free day {lfd}. Projected demurrage {cost}. Owner acknowledgement required if cost is high."
    else:
        subject = f"Last free day today/past — {number}"
        body = f"{number} is at or past last free day ({lfd}). Do not use the T-48 copy. Arrange gate-out now. Exposure {cost}."
    return subject, body


def _enqueue_window(conn, item: dict, force: bool = False) -> dict | None:
    if item["status"] in {"gated", "cleared"}:
        return None
    lfd = parse_iso_date(item.get("lfd_final"))
    if not lfd:
        return None
    window = policy_window(hours_until_lfd(lfd))
    if window is None:
        return None
    if not force and not should_auto_nudge(item.get("confidence") or 0, item.get("conflict"), item["status"]):
        if item["status"] != "review":
            conn.execute("UPDATE containers SET status = 'review' WHERE id = ?", (item["id"],))
        return {"blocked": True, "reason": "review"}
    if window == 24 and (item.get("projected_cost") or 0) > COST_OWNER_ACK and not force:
        conn.execute("UPDATE containers SET status = 'review' WHERE id = ?", (item["id"],))
        return {"blocked": True, "reason": "high_cost"}
    key = idempotency_key(item["id"], window, date.today())
    existing = conn.execute("SELECT id, status FROM outbox WHERE idempotency_key = ?", (key,)).fetchone()
    if existing:
        return {"idempotent": True, "outbox_id": existing["id"], "status": existing["status"]}
    bl = bl_row(conn, item["bl_id"])
    to_addr = bl.get("forwarder_email") or os.getenv("FORWARDER_EMAIL")
    if not to_addr:
        return {"blocked": True, "reason": "no_email"}
    cc = os.getenv("OWNER_EMAIL") if window in {48, 24, 0} else None
    facts = {
        "container": item["number"],
        "lfd": item.get("lfd_final"),
        "window": window,
        "port": bl.get("port"),
        "vessel": bl.get("vessel"),
        "cost": item.get("projected_cost"),
    }
    try:
        draft = draft_nudge(facts) if gemini_live() else {"subject": None, "body": None}
    except Exception:
        draft = {"subject": None, "body": None}
    subject, body = _template(window, item, bl)
    subject = draft.get("subject") or subject
    body = draft.get("body") or body
    conn.execute(
        """
        INSERT INTO outbox (
            container_id, channel, window, idempotency_key, to_addr, cc_addr, subject, body, status, created_at
        ) VALUES (?, 'email', ?, ?, ?, ?, ?, ?, 'pending', ?)
        """,
        (item["id"], window, key, to_addr, cc, subject, body, now_iso()),
    )
    if item["status"] in {"watching", "ingested"}:
        conn.execute("UPDATE containers SET status = 'nudging' WHERE id = ?", (item["id"],))
    add_audit(
        conn,
        actor="system",
        event_type="outbox_enqueued",
        payload={"window": window, "key": key},
        request_id=request_id_ctx.get(),
        container_id=item["id"],
        bl_id=item["bl_id"],
    )
    return {"queued": True, "window": window, "key": key}


@app.post("/api/jobs/scan-deadlines")
def scan_deadlines(x_scan_secret: str | None = Header(default=None)):
    expected = os.getenv("SCAN_SECRET", "lastday-dev")
    if x_scan_secret != expected:
        raise HTTPException(401, "bad scan secret")
    conn = connect()
    items = list_containers(conn)
    queued = []
    blocked = []
    missed = []
    for item in items:
        if item["status"] in {"cleared", "gated"}:
            continue
        if item.get("days_left") is not None and item["days_left"] < 0 and item["status"] in {"watching", "nudging"}:
            conn.execute("UPDATE containers SET status = 'missed' WHERE id = ?", (item["id"],))
            missed.append(item["id"])
            continue
        result = _enqueue_window(conn, item)
        if not result:
            continue
        row = {"id": item["id"], **result}
        if result.get("blocked"):
            blocked.append(row)
        else:
            queued.append(row)
    conn.commit()
    conn.close()
    return {"checked": len(items), "queued": queued, "blocked": blocked, "missed": missed}


@app.post("/api/jobs/drain-outbox")
def drain_outbox(x_scan_secret: str | None = Header(default=None)):
    expected = os.getenv("SCAN_SECRET", "lastday-dev")
    if x_scan_secret != expected:
        raise HTTPException(401, "bad scan secret")
    conn = connect()
    rows = conn.execute("SELECT * FROM outbox WHERE status = 'pending' ORDER BY id").fetchall()
    sent = []
    for row in rows:
        try:
            result = send_email(row["to_addr"], row["subject"], row["body"] or "")
        except Exception as exc:
            conn.execute("UPDATE outbox SET status = 'failed' WHERE id = ?", (row["id"],))
            add_audit(
                conn,
                actor="system",
                event_type="outbox_failed",
                payload={"error": str(exc), "outbox_id": row["id"]},
                request_id=request_id_ctx.get(),
                container_id=row["container_id"],
            )
            continue
        status = "dry_run" if result.get("dry_run") else "sent"
        conn.execute(
            "UPDATE outbox SET status = ?, sent_at = ? WHERE id = ?",
            (status, now_iso(), row["id"]),
        )
        add_audit(
            conn,
            actor="system",
            event_type="email",
            payload=result,
            request_id=request_id_ctx.get(),
            container_id=row["container_id"],
        )
        sent.append({"id": row["id"], "status": status, "to": row["to_addr"]})
    conn.commit()
    conn.close()
    return {"drained": sent}


@app.get("/api/audit")
def audit(container_id: int | None = None, request_id: str | None = None, user: dict = Depends(current_user)):
    conn = connect()
    sql = "SELECT * FROM audit_events WHERE 1=1"
    args: list = []
    if container_id:
        sql += " AND container_id = ?"
        args.append(container_id)
    if request_id:
        sql += " AND request_id = ?"
        args.append(request_id)
    sql += " ORDER BY id DESC LIMIT 200"
    rows = []
    for r in conn.execute(sql, args):
        item = dict(r)
        try:
            item["payload"] = json.loads(item["payload"] or "{}")
        except json.JSONDecodeError:
            pass
        rows.append(item)
    conn.close()
    return rows


@app.exception_handler(HTTPException)
def http_exc(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail, "request_id": request_id_ctx.get()})
