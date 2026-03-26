"""Pool scheduler FastAPI app."""
import asyncio
import os
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, File, Query, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

from scrapers.splextech import fetch_all_pools
from scrapers.sablon import fetch_schedule as fetch_sablon

app = FastAPI(title="Laval Pool Scheduler")

# Simple in-memory cache: {date_str: (fetched_at_ts, data)}
_cache: dict[str, tuple[float, list]] = {}
CACHE_TTL_SECONDS = 3 * 60 * 60  # 3 hours

MONTREAL_TZ = ZoneInfo("America/Toronto")
DB_PATH = Path("uploads.db")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS uploaded_pools (
            pool_name TEXT PRIMARY KEY,
            source_label TEXT DEFAULT 'PDF Import'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS uploaded_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pool_name TEXT NOT NULL,
            day_of_week INTEGER NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            activity TEXT NOT NULL,
            activity_type TEXT DEFAULT 'public',
            description TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS uploaded_closures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pool_name TEXT NOT NULL,
            closure_date TEXT NOT NULL,
            reason TEXT
        )
    """)
    conn.commit()
    conn.close()


init_db()


def local_to_utc_iso(target_date: date, time_str: str) -> str:
    """Convert a local Montreal HH:MM time string to UTC ISO format."""
    h, m = map(int, time_str.split(":"))
    local_dt = datetime(
        target_date.year, target_date.month, target_date.day,
        h, m, tzinfo=MONTREAL_TZ
    )
    utc_dt = local_dt.astimezone(timezone.utc)
    return utc_dt.isoformat()


def fetch_uploaded_sessions(target_date: date) -> list[dict]:
    """Fetch sessions from uploaded schedules for the given date."""
    if not DB_PATH.exists():
        return []

    day_of_week = target_date.weekday()  # 0=Monday, 6=Sunday
    date_str = target_date.isoformat()

    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.execute(
            "SELECT pool_name FROM uploaded_closures WHERE closure_date = ?",
            (date_str,)
        )
        closed_pools = {row[0] for row in cursor.fetchall()}

        cursor = conn.execute("""
            SELECT pool_name, start_time, end_time, activity, activity_type, description
            FROM uploaded_sessions
            WHERE day_of_week = ?
        """, (day_of_week,))

        sessions = []
        for row in cursor.fetchall():
            pool_name, start_time, end_time, activity, activity_type, description = row

            if pool_name in closed_pools or "all" in closed_pools:
                continue

            start_iso = local_to_utc_iso(target_date, start_time)
            end_iso = local_to_utc_iso(target_date, end_time)

            sessions.append({
                "pool": pool_name,
                "start": start_iso,
                "end": end_iso,
                "activity": activity,
                "type": activity_type,
                "description": description or "",
                "note": None,
                "source": "upload",
            })

        return sessions
    finally:
        conn.close()


async def get_sessions(target_date: date) -> list[dict]:
    date_str = target_date.isoformat()
    now = datetime.now(timezone.utc).timestamp()

    if date_str in _cache:
        fetched_at, data = _cache[date_str]
        if now - fetched_at < CACHE_TTL_SECONDS:
            return data

    splextech_slots, sablon_slots = await asyncio.gather(
        fetch_all_pools(target_date),
        fetch_sablon(target_date),
        return_exceptions=True,
    )

    all_slots = []
    if not isinstance(splextech_slots, Exception):
        all_slots.extend(splextech_slots)
    if not isinstance(sablon_slots, Exception):
        all_slots.extend(sablon_slots)

    uploaded_slots = fetch_uploaded_sessions(target_date)
    all_slots.extend(uploaded_slots)

    all_slots.sort(key=lambda s: (s["pool"], s["start"] or ""))

    _cache[date_str] = (now, all_slots)
    return all_slots


@app.get("/api/sessions")
async def sessions(date: str = Query(default=None)):
    if date:
        try:
            target = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            return JSONResponse({"error": "Invalid date format, use YYYY-MM-DD"}, status_code=400)
    else:
        target = datetime.now(timezone.utc).date()

    data = await get_sessions(target)
    return data


@app.post("/api/upload-schedule")
async def upload_schedule(file: UploadFile = File(...)):
    """Accept a PDF or image of a pool schedule and parse it with open-source libraries."""
    from scrapers.pdf_parser import parse_schedule_file

    content = await file.read()
    media_type = (file.content_type or "").lower().split(";")[0].strip()

    if media_type in ("image/jpg",):
        media_type = "image/jpeg"

    supported = ("application/pdf", "image/jpeg", "image/png", "image/webp")
    if media_type not in supported:
        return JSONResponse(
            {"error": f"Format non supporté ({media_type}). Utilisez PDF, JPG ou PNG."},
            status_code=400,
        )

    try:
        schedule_data = parse_schedule_file(content, media_type)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": f"Échec de l'analyse : {e}"}, status_code=500)

    conn = sqlite3.connect(DB_PATH)
    try:
        total_sessions = 0
        pool_names = []

        for pool in schedule_data.get("pools", []):
            pool_name = pool["name"]
            pool_sessions = pool.get("sessions", [])

            conn.execute("DELETE FROM uploaded_sessions WHERE pool_name = ?", (pool_name,))
            conn.execute("DELETE FROM uploaded_pools WHERE pool_name = ?", (pool_name,))
            conn.execute("INSERT INTO uploaded_pools (pool_name) VALUES (?)", (pool_name,))

            for s in pool_sessions:
                conn.execute("""
                    INSERT INTO uploaded_sessions
                        (pool_name, day_of_week, start_time, end_time, activity, activity_type, description)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    pool_name,
                    int(s["day_of_week"]),
                    s["start_time"],
                    s["end_time"],
                    s.get("activity", "Activité"),
                    s.get("type", "public"),
                    s.get("description", ""),
                ))
                total_sessions += 1

            pool_names.append(pool_name)

        for closure in schedule_data.get("closures", []):
            conn.execute("""
                INSERT INTO uploaded_closures (pool_name, closure_date, reason)
                VALUES (?, ?, ?)
            """, (
                closure.get("pool_name", "all"),
                closure.get("date", ""),
                closure.get("reason", ""),
            ))

        conn.commit()

        # Invalidate cache so new pool appears immediately
        _cache.clear()

        return JSONResponse({
            "success": True,
            "pools": pool_names,
            "sessions_imported": total_sessions,
        })
    except Exception as e:
        conn.rollback()
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        conn.close()


@app.delete("/api/uploaded-pool")
async def delete_uploaded_pool(pool_name: str = Query(...)):
    """Remove an uploaded pool and all its sessions."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("DELETE FROM uploaded_sessions WHERE pool_name = ?", (pool_name,))
        conn.execute("DELETE FROM uploaded_closures WHERE pool_name = ?", (pool_name,))
        conn.execute("DELETE FROM uploaded_pools WHERE pool_name = ?", (pool_name,))
        conn.commit()
        _cache.clear()
        return JSONResponse({"success": True})
    finally:
        conn.close()


@app.get("/", response_class=HTMLResponse)
async def index():
    html = Path("templates/index.html").read_text()
    return HTMLResponse(html)
