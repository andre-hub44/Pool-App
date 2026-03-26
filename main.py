"""Pool scheduler FastAPI app."""
import asyncio
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from scrapers.splextech import fetch_all_pools
from scrapers.sablon import fetch_schedule as fetch_sablon

app = FastAPI(title="Laval Pool Scheduler")

# Simple in-memory cache: {date_str: (fetched_at_ts, data)}
_cache: dict[str, tuple[float, list]] = {}
CACHE_TTL_SECONDS = 3 * 60 * 60  # 3 hours


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

    # Sort by pool name then start time
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


@app.get("/", response_class=HTMLResponse)
async def index():
    html = Path("templates/index.html").read_text()
    return HTMLResponse(html)
