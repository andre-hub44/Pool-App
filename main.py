"""Pool scheduler FastAPI app."""
import asyncio
import base64
import json
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
    """Accept a PDF or image of a pool schedule and parse it with Claude Vision."""
    try:
        import anthropic as anthropic_sdk
    except ImportError:
        return JSONResponse({"error": "Package 'anthropic' not installed."}, status_code=500)

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return JSONResponse({"error": "ANTHROPIC_API_KEY not configured on the server."}, status_code=500)

    content = await file.read()
    media_type = (file.content_type or "").lower()

    # Normalize jpg
    if media_type in ("image/jpg",):
        media_type = "image/jpeg"

    if media_type == "application/pdf":
        content_block = {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": base64.standard_b64encode(content).decode("utf-8"),
            },
        }
    elif media_type in ("image/jpeg", "image/png", "image/webp", "image/gif"):
        content_block = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.standard_b64encode(content).decode("utf-8"),
            },
        }
    else:
        return JSONResponse(
            {"error": f"Format non supporté ({media_type}). Utilisez PDF, JPG ou PNG."},
            status_code=400
        )

    prompt = """Analyze this pool schedule document and extract the complete weekly schedule as JSON.

Return ONLY valid JSON with no markdown formatting, in this exact structure:
{
  "pools": [
    {
      "name": "Full pool name exactly as shown in document",
      "sessions": [
        {
          "day_of_week": 0,
          "start_time": "HH:MM",
          "end_time": "HH:MM",
          "activity": "Activity name in French",
          "type": "public",
          "description": "Optional extra details e.g. number of lanes"
        }
      ]
    }
  ],
  "closures": [
    {
      "pool_name": "pool name or all",
      "date": "YYYY-MM-DD",
      "reason": "reason for closure"
    }
  ]
}

Rules:
- day_of_week: 0=Monday, 1=Tuesday, 2=Wednesday, 3=Thursday, 4=Friday, 5=Saturday, 6=Sunday
- type: use "public" for bain libre and couloirs de nage; "club" for club activities; "other" for anything else
- Include ALL time slots shown in the document for every day
- If multiple pools appear in the document (e.g. Piscine 25m and Bassin récréatif), include each as a separate entry in the pools array
- Times must be in HH:MM 24h format, local Montreal time
- Extract all planned closures (Fermetures prévues) into the closures array with YYYY-MM-DD dates"""

    client = anthropic_sdk.Anthropic(api_key=api_key)
    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=4096,
        messages=[
            {
                "role": "user",
                "content": [
                    content_block,
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    )

    response_text = message.content[0].text.strip()

    # Strip markdown code fences if present
    if response_text.startswith("```"):
        lines = response_text.split("\n")
        end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
        response_text = "\n".join(lines[1:end])

    try:
        schedule_data = json.loads(response_text)
    except json.JSONDecodeError as e:
        return JSONResponse({"error": f"Impossible de parser la réponse: {e}"}, status_code=500)

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
