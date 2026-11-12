import json
import os
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from domain import days_left, projected_cost, risk_band

DB_PATH = Path(os.getenv("LASTDAY_DB") or Path(__file__).parent / "lastday.sqlite")

ETHIOPIA_HOLIDAYS_2026 = [
    "2026-01-07",
    "2026-01-19",
    "2026-03-02",
    "2026-05-01",
    "2026-05-05",
    "2026-05-28",
    "2026-09-11",
    "2026-09-27",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _open():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def connect():
    conn = _open()
    try:
        conn.execute("SELECT 1 FROM schema_meta LIMIT 1")
    except sqlite3.OperationalError:
        conn.close()
        init_db()
        conn = _open()
    return conn


def init_db():
    conn = _open()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS parties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT,
            phone TEXT,
            kind TEXT NOT NULL DEFAULT 'forwarder'
        );
        CREATE TABLE IF NOT EXISTS holidays (
            day TEXT PRIMARY KEY,
            market TEXT NOT NULL DEFAULT 'ET'
        );
        CREATE TABLE IF NOT EXISTS tariffs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            line TEXT NOT NULL,
            port TEXT NOT NULL,
            free_days INTEGER NOT NULL,
            weekday_rate REAL NOT NULL,
            weekend_rate REAL NOT NULL,
            currency TEXT NOT NULL,
            UNIQUE(line, port)
        );
        CREATE TABLE IF NOT EXISTS bills_of_lading (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bl_number TEXT,
            vessel TEXT,
            voyage TEXT,
            port TEXT,
            consignee TEXT,
            forwarder_name TEXT,
            forwarder_email TEXT,
            cargo TEXT,
            raw_source TEXT NOT NULL,
            source_type TEXT NOT NULL DEFAULT 'paste',
            gemini_run_id TEXT,
            prompt_version TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS containers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bl_id INTEGER NOT NULL,
            number TEXT NOT NULL,
            type TEXT,
            discharge_date TEXT,
            free_days INTEGER,
            lfd_extracted TEXT,
            lfd_computed TEXT,
            lfd_final TEXT,
            status TEXT NOT NULL DEFAULT 'ingested',
            projected_cost REAL NOT NULL DEFAULT 0,
            confidence REAL,
            conflict INTEGER NOT NULL DEFAULT 0,
            version INTEGER NOT NULL DEFAULT 1,
            tariff_id INTEGER,
            created_at TEXT NOT NULL,
            FOREIGN KEY (bl_id) REFERENCES bills_of_lading(id),
            FOREIGN KEY (tariff_id) REFERENCES tariffs(id)
        );
        CREATE TABLE IF NOT EXISTS outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            container_id INTEGER NOT NULL,
            channel TEXT NOT NULL DEFAULT 'email',
            window INTEGER,
            idempotency_key TEXT UNIQUE NOT NULL,
            to_addr TEXT,
            cc_addr TEXT,
            subject TEXT,
            body TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            sent_at TEXT,
            FOREIGN KEY (container_id) REFERENCES containers(id)
        );
        CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor TEXT,
            type TEXT NOT NULL,
            container_id INTEGER,
            bl_id INTEGER,
            payload TEXT,
            request_id TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None:
        conn.executemany(
            "INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
            [
                ("owner", "owner", "owner"),
                ("ops", "ops", "ops"),
                ("viewer", "viewer", "viewer"),
            ],
        )
    if conn.execute("SELECT 1 FROM tariffs LIMIT 1").fetchone() is None:
        conn.executemany(
            """
            INSERT INTO tariffs (line, port, free_days, weekday_rate, weekend_rate, currency)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                ("Maersk", "Djibouti — Doraleh", 7, 150, 220, "USD"),
                ("MSC", "Djibouti — Doraleh", 7, 140, 200, "USD"),
                ("Maersk", "Mombasa", 5, 8500, 12000, "ETB"),
                ("PIL", "Djibouti — Doraleh", 7, 130, 190, "USD"),
            ],
        )
    for day in ETHIOPIA_HOLIDAYS_2026:
        conn.execute(
            "INSERT OR IGNORE INTO holidays (day, market) VALUES (?, 'ET')",
            (day,),
        )
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('version', '2')"
    )
    conn.commit()
    conn.close()


def holiday_dates(conn) -> list[date]:
    rows = conn.execute("SELECT day FROM holidays").fetchall()
    out = []
    for row in rows:
        try:
            out.append(date.fromisoformat(row["day"]))
        except ValueError:
            continue
    return out


def add_audit(conn, *, actor, event_type, payload, request_id=None, container_id=None, bl_id=None):
    conn.execute(
        """
        INSERT INTO audit_events (actor, type, container_id, bl_id, payload, request_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            actor,
            event_type,
            container_id,
            bl_id,
            json.dumps(payload, default=str),
            request_id,
            now_iso(),
        ),
    )


def _enrich_container(conn, row, holidays=None) -> dict:
    if row is None:
        return None
    item = dict(row)
    lfd = None
    if item.get("lfd_final"):
        try:
            lfd = date.fromisoformat(item["lfd_final"])
        except ValueError:
            lfd = None
    today = date.today()
    left = days_left(lfd, today)
    item["days_left"] = left
    item["conflict"] = bool(item.get("conflict"))
    item["risk_band"] = risk_band(left, item["status"])
    tariff = None
    if item.get("tariff_id"):
        trow = conn.execute("SELECT * FROM tariffs WHERE id = ?", (item["tariff_id"],)).fetchone()
        if trow:
            tariff = dict(trow)
    if tariff:
        holidays = holidays if holidays is not None else holiday_dates(conn)
        item["projected_cost"] = projected_cost(
            lfd, today, tariff["weekday_rate"], tariff["weekend_rate"], holidays
        )
        item["currency"] = tariff["currency"]
        item["tariff"] = tariff
    else:
        item["currency"] = None
        item["tariff"] = None
    return item


def get_container(conn, container_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM containers WHERE id = ?", (container_id,)).fetchone()
    return _enrich_container(conn, row)


def list_containers(conn, status: str | None = None) -> list[dict]:
    holidays = holiday_dates(conn)
    sql = "SELECT * FROM containers"
    args: list = []
    if status:
        sql += " WHERE status = ?"
        args.append(status)
    sql += " ORDER BY lfd_final IS NULL, lfd_final ASC"
    return [_enrich_container(conn, r, holidays) for r in conn.execute(sql, args)]
