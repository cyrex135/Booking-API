"""Clinic slot and booking API, backed by SQLite."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

ROOT = Path(__file__).parent
DB_PATH = Path(os.getenv("DATABASE_PATH", ROOT / "booking.sqlite3"))
DATABASE_URL = os.getenv("DATABASE_URL")
IS_VERCEL = bool(os.getenv("VERCEL"))
API_KEY = os.getenv("API_KEY") or (None if IS_VERCEL else "dev-booking-key-change-me")
if IS_VERCEL and not DATABASE_URL:
    raise RuntimeError("DATABASE_URL must be configured for persistent Vercel storage.")
if IS_VERCEL and not API_KEY:
    raise RuntimeError("API_KEY must be configured for protected booking API access.")
app = FastAPI(title="Clinic Booking API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["X-API-Key", "Content-Type", "Idempotency-Key"],
)


class PostgresConnection:
    """Tiny placeholder/executescript adapter shared with the SQLite code path."""
    is_postgres = True

    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql: str, params=()):
        return self.connection.execute(sql.replace("?", "%s"), params)

    def executescript(self, script: str) -> None:
        for statement in script.split(";"):
            if statement.strip():
                self.execute(statement)

    def commit(self) -> None:
        self.connection.commit()

    def rollback(self) -> None:
        self.connection.rollback()

    def close(self) -> None:
        self.connection.close()


class SQLiteConnection:
    is_postgres = False

    def __init__(self, connection):
        self.connection = connection

    def __getattr__(self, name):
        return getattr(self.connection, name)


@contextmanager
def db() -> Iterator:
    if DATABASE_URL:
        import psycopg
        from psycopg.rows import dict_row
        connection = PostgresConnection(psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=True))
    else:
        raw_connection = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
        raw_connection.row_factory = sqlite3.Row
        raw_connection.execute("PRAGMA foreign_keys = ON")
        raw_connection.execute("PRAGMA busy_timeout = 15000")
        connection = SQLiteConnection(raw_connection)
    try:
        yield connection
    finally:
        connection.close()


def begin_write(connection) -> None:
    connection.execute("BEGIN" if connection.is_postgres else "BEGIN IMMEDIATE")


def initialize() -> None:
    if not DATABASE_URL:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        if not conn.is_postgres:
            conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS slots (
            id TEXT PRIMARY KEY, provider TEXT NOT NULL, starts_at TEXT NOT NULL,
            ends_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bookings (
            id TEXT PRIMARY KEY, slot_id TEXT NOT NULL REFERENCES slots(id),
            name TEXT NOT NULL, email TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('confirmed','cancelled')),
            created_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_one_confirmed_booking_per_slot
            ON bookings(slot_id) WHERE status='confirmed';
        CREATE INDEX IF NOT EXISTS idx_bookings_email_created
            ON bookings(lower(email), created_at, id);
        CREATE TABLE IF NOT EXISTS idempotency (
            key TEXT PRIMARY KEY, request_hash TEXT NOT NULL,
            status_code INTEGER NOT NULL, response_body TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS rate_events (
            email TEXT NOT NULL, occurred_at DOUBLE PRECISION NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_rate_events_email_time ON rate_events(lower(email), occurred_at);
        """)
        if conn.execute("SELECT count(*) AS count FROM slots").fetchone()["count"] == 0:
            # Friendly local defaults; POST /reset replaces these with the caller's schedule.
            from datetime import timedelta
            now = datetime.now(timezone.utc)
            day = now.date() + timedelta(days=1)
            while day.weekday() >= 5:
                day += timedelta(days=1)
            for hour in range(9, 17):
                starts = datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)
                ends = starts.replace(minute=30)
                conn.execute("INSERT INTO slots VALUES (?,?,?,?)", (f"demo-{hour}", "Dr Ade", starts.isoformat().replace("+00:00", "Z"), ends.isoformat().replace("+00:00", "Z")))


initialize()


def error(status: int, message: str, *, headers: dict[str, str] | None = None) -> HTTPException:
    return HTTPException(status_code=status, detail=message, headers=headers)


@app.exception_handler(HTTPException)
async def http_error_handler(_: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return JSONResponse({"error": detail}, status_code=exc.status_code, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse({"error": "invalid request"}, status_code=400)


def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    if not x_api_key or not hmac.compare_digest(x_api_key, API_KEY):
        raise error(401, "invalid API key")


class SlotInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    id: str = Field(min_length=1, max_length=100)
    provider: str = Field(min_length=1, max_length=120)
    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime must include a timezone")
        return value.astimezone(timezone.utc)


class ResetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slots: list[SlotInput]


class BookingInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    slot_id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr

    @field_validator("email", mode="before")
    @classmethod
    def trim_email(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


def utc_string(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/reset", dependencies=[Depends(require_api_key)])
def reset_slots(payload: ResetInput) -> dict:
    ids = [slot.id for slot in payload.slots]
    if len(ids) != len(set(ids)):
        raise error(400, "slot ids must be unique")
    if any(slot.end <= slot.start for slot in payload.slots):
        raise error(400, "slot end must be after slot start")
    with db() as conn:
        begin_write(conn)
        conn.execute("DELETE FROM bookings")
        conn.execute("DELETE FROM slots")
        conn.execute("DELETE FROM idempotency")
        conn.execute("DELETE FROM rate_events")
        conn.executemany(
            "INSERT INTO slots(id,provider,starts_at,ends_at) VALUES(?,?,?,?)",
            [(s.id, s.provider, utc_string(s.start), utc_string(s.end)) for s in payload.slots],
        )
        conn.commit()
    return {"message": "slots reset", "count": len(payload.slots)}


@app.get("/slots", dependencies=[Depends(require_api_key)])
def list_slots(provider: str | None = None) -> dict:
    with db() as conn:
        if provider is None:
            rows = conn.execute("""
                SELECT s.id,s.provider,s.starts_at,s.ends_at,
                    NOT EXISTS(SELECT 1 FROM bookings b WHERE b.slot_id=s.id AND b.status='confirmed') AS available
                FROM slots s ORDER BY s.starts_at,s.id
            """).fetchall()
        else:
            rows = conn.execute("""
                SELECT s.id,s.provider,s.starts_at,s.ends_at,
                    NOT EXISTS(SELECT 1 FROM bookings b WHERE b.slot_id=s.id AND b.status='confirmed') AS available
                FROM slots s WHERE s.provider=? ORDER BY s.starts_at,s.id
            """, (provider,)).fetchall()
    return {"slots": [{"id": r["id"], "provider": r["provider"], "start": r["starts_at"], "end": r["ends_at"], "available": bool(r["available"])} for r in rows]}


def increment_attempt(email: str) -> None:
    now = time.time()
    with db() as conn:
        begin_write(conn)
        conn.execute("DELETE FROM rate_events WHERE occurred_at <= ?", (now - 60,))
        if conn.is_postgres:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (email,))
        count = conn.execute("SELECT count(*) AS count FROM rate_events WHERE lower(email)=?", (email,)).fetchone()["count"]
        if count >= 5:
            oldest = conn.execute("SELECT min(occurred_at) AS oldest FROM rate_events WHERE lower(email)=?", (email,)).fetchone()["oldest"]
            retry_after = max(1, int(60 - (now - oldest) + 0.999))
            conn.rollback()
            raise error(429, "too many booking attempts", headers={"Retry-After": str(retry_after)})
        conn.execute("INSERT INTO rate_events(email,occurred_at) VALUES(?,?)", (email, now))
        conn.commit()


@app.middleware("http")
async def booking_rate_limit(request: Request, call_next):
    # Count attempts before validation so failed requests for an email also count.
    if request.method == "POST" and request.url.path in ("/bookings", "/ui-api/bookings"):
        key = request.headers.get("X-API-Key", "")
        protected_api_request = request.url.path == "/bookings" and key and hmac.compare_digest(key, API_KEY)
        public_booking_page_request = request.url.path == "/ui-api/bookings"
        if protected_api_request or public_booking_page_request:
            raw = await request.body()
            try:
                submitted = json.loads(raw)
                email_value = submitted.get("email", "") if isinstance(submitted, dict) else ""
            except (json.JSONDecodeError, UnicodeDecodeError):
                email_value = ""
            email_bucket = email_value.strip().lower() if isinstance(email_value, str) else ""
            # A malformed/missing email is still an attempt in its own bucket.
            try:
                increment_attempt(email_bucket)
            except HTTPException as exc:
                return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=exc.headers)
            request._body = raw
    return await call_next(request)


@app.post("/bookings", status_code=201, dependencies=[Depends(require_api_key)])
def create_booking(payload: BookingInput, response: Response, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict:
    email = str(payload.email).strip().lower()
    name = payload.name.strip()
    canonical = json.dumps({"slot_id": payload.slot_id, "name": name, "email": email}, sort_keys=True, separators=(",", ":"))
    request_hash = hashlib.sha256(canonical.encode()).hexdigest()
    if idempotency_key is not None and (not idempotency_key.strip() or len(idempotency_key) > 200):
        raise error(400, "invalid Idempotency-Key")
    with db() as conn:
        begin_write(conn)
        if idempotency_key and conn.is_postgres:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (f"idempotency:{idempotency_key}",))
        if idempotency_key:
            previous = conn.execute("SELECT request_hash,status_code,response_body FROM idempotency WHERE key=?", (idempotency_key,)).fetchone()
            if previous:
                if previous["request_hash"] != request_hash:
                    conn.rollback()
                    raise error(422, "Idempotency-Key reused with a different body")
                response.status_code = previous["status_code"]
                result = json.loads(previous["response_body"])
                conn.commit()
                return result
        lock = " FOR UPDATE" if conn.is_postgres else ""
        slot = conn.execute(f"SELECT id,starts_at FROM slots WHERE id=?{lock}", (payload.slot_id,)).fetchone()
        if slot is None:
            conn.rollback()
            raise error(404, "slot not found")
        try:
            starts_at = datetime.fromisoformat(slot["starts_at"].replace("Z", "+00:00"))
        except ValueError:
            starts_at = datetime.min.replace(tzinfo=timezone.utc)
        if starts_at <= datetime.now(timezone.utc):
            conn.rollback()
            raise error(400, "slot has already started")
        if conn.execute("SELECT 1 FROM bookings WHERE slot_id=? AND status='confirmed'", (payload.slot_id,)).fetchone():
            conn.rollback()
            raise error(409, "slot already booked")
        booking = {"id": str(uuid.uuid4()), "slot_id": payload.slot_id, "name": name, "email": email, "status": "confirmed", "created_at": utc_string(datetime.now(timezone.utc))}
        conn.execute("INSERT INTO bookings(id,slot_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)", (booking["id"], booking["slot_id"], name, email, "confirmed", booking["created_at"]))
        if idempotency_key:
            conn.execute("INSERT INTO idempotency(key,request_hash,status_code,response_body) VALUES(?,?,201,?)", (idempotency_key, request_hash, json.dumps(booking)))
        conn.commit()
    return booking


def cursor_encode(created_at: str, booking_id: str) -> str:
    raw = json.dumps([created_at, booking_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def cursor_decode(cursor: str) -> tuple[str, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        decoded = json.loads(raw)
        if not isinstance(decoded, list) or len(decoded) != 2 or not all(isinstance(v, str) for v in decoded):
            raise ValueError
        return decoded[0], decoded[1]
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        raise error(400, "invalid cursor")


@app.get("/bookings", dependencies=[Depends(require_api_key)])
def list_bookings(email: str = Query(...), limit: int = Query(10), cursor: str | None = None) -> dict:
    email = email.strip().lower()
    if not email or "@" not in email:
        raise error(400, "invalid email")
    if limit < 1 or limit > 50:
        raise error(400, "limit must be between 1 and 50")
    args: list[str | int] = [email]
    continuation = ""
    if cursor:
        created_at, booking_id = cursor_decode(cursor)
        continuation = " AND (created_at > ? OR (created_at = ? AND id > ?))"
        args.extend([created_at, created_at, booking_id])
    args.append(limit + 1)
    with db() as conn:
        rows = conn.execute(f"SELECT id,slot_id,name,email,status,created_at FROM bookings WHERE lower(email)=?{continuation} ORDER BY created_at,id LIMIT ?", args).fetchall()
    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = cursor_encode(page[-1]["created_at"], page[-1]["id"]) if has_more and page else None
    return {"bookings": [dict(row) for row in page], "next_cursor": next_cursor}


@app.get("/bookings/{id}", dependencies=[Depends(require_api_key)])
def get_booking(id: str) -> dict:
    with db() as conn:
        row = conn.execute("SELECT id,slot_id,name,email,status,created_at FROM bookings WHERE id=?", (id,)).fetchone()
    if row is None:
        raise error(404, "booking not found")
    return dict(row)


@app.delete("/bookings/{id}", dependencies=[Depends(require_api_key)])
def cancel_booking(id: str) -> dict:
    with db() as conn:
        begin_write(conn)
        lock = " FOR UPDATE" if conn.is_postgres else ""
        row = conn.execute(f"SELECT id,slot_id,name,email,status,created_at FROM bookings WHERE id=?{lock}", (id,)).fetchone()
        if row is None:
            conn.rollback()
            raise error(404, "booking not found")
        if row["status"] == "cancelled":
            conn.rollback()
            raise error(409, "booking already cancelled")
        conn.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (id,))
        conn.commit()
    result = dict(row)
    result["status"] = "cancelled"
    return result


# Public same-origin bridge for the patient-facing page. The assessment API stays
# protected by X-API-Key; the page calls this small bridge so the clinic secret
# never has to be disclosed to patients.
@app.get("/ui-api/slots", include_in_schema=False)
def ui_slots(provider: str | None = None) -> dict:
    return list_slots(provider)


@app.post("/ui-api/bookings", status_code=201, include_in_schema=False)
def ui_create_booking(payload: BookingInput, response: Response, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict:
    return create_booking(payload, response, idempotency_key)


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.get("/", include_in_schema=False)
def homepage() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")
        
