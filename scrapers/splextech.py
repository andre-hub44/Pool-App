"""Splextech API client for Laval indoor pools."""
import asyncio
import json
from datetime import date, timedelta
from typing import Optional
from urllib.parse import quote

import httpx

CONFIGURATION_ID = "65905251-d399-4fb3-9b2f-828cbde008dd"

# Any surface ID works to bootstrap the session cookies
BOOTSTRAP_SURFACE_ID = "c128c29a-0da5-44ba-893b-08e7aa68571b"
CALENDAR_URL = (
    f"https://calendar.splextech.com/?locale=FR"
    f"&configurationId={CONFIGURATION_ID}"
    f"&surfaceId={BOOTSTRAP_SURFACE_ID}&initialDate="
)

POOLS = {
    "Piscine Vanier": "c128c29a-0da5-44ba-893b-08e7aa68571b",
    "Piscine Poly-Jeunesse": "9977e45d-688a-4999-8c8b-dc451cce3e51",
    "Piscine Honoré-Mercier": "e2e6fdd6-d75f-4dec-ae5d-0dec8e2f6669",
    "Centre sportif Josée-Faucher": "6ded38c4-7232-424d-9393-b83cdb908a40",
    "Complexe Aquatique – Bassin Récréatif": "8ecfbd2b-c8b0-4765-95f2-d399e51e5b95",
    "Complexe Aquatique – Bassin Sportif": "f30bd23f-e45f-4a23-b80e-d9db3f100b21",
    "Complexe Aquatique – Bassin Plongeon": "2cf53adb-c2ff-4f4c-a126-6546b814f208",
}

BASE_URL = "https://app.splextech.com/orm/VenuesPublicOnlineCalendarConfigurationTimeSlotDetails"

API_HEADERS = {
    "Origin": "https://calendar.splextech.com",
    "Referer": "https://calendar.splextech.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fr-CA,fr;q=0.9,en;q=0.8",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}

# Shared client with persistent cookie jar; bootstrapped once per process
_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()


async def _get_client() -> httpx.AsyncClient:
    global _client
    async with _client_lock:
        if _client is None or _client.is_closed:
            client = httpx.AsyncClient(
                timeout=15,
                follow_redirects=True,
                headers={
                    "User-Agent": API_HEADERS["User-Agent"],
                    "Accept-Language": API_HEADERS["Accept-Language"],
                },
            )
            # Visit the calendar page to pick up any session cookies
            await client.get(CALENDAR_URL)
            _client = client
    return _client


def _classify_session(slot: dict) -> str:
    note = (slot.get("notePublic") or "").lower()
    if "fermée" in note or "fermee" in note or "ferme" in note:
        return "closed"

    activity = slot.get("activityName")
    if isinstance(activity, dict):
        activity = activity.get("FR") or activity.get("EN") or ""
    activity = (activity or "").lower()

    closed_kw = ["fermé", "ferme", "entretien", "maintenance"]
    public_kw = ["bain libre", "bain longueur", "bain familial", "natation publique", "public"]
    club_kw = ["club", "école", "ecole", "compétition", "competition", "entraînement", "entrainement", "triathlon"]

    if any(k in note for k in closed_kw):
        return "closed"
    if any(k in activity for k in public_kw) or any(k in note for k in public_kw):
        return "public"
    if any(k in activity for k in club_kw):
        return "club"
    if activity:
        return "club"
    return "other"


async def fetch_slots(pool_name: str, surface_id: str, target_date: date) -> list[dict]:
    date_str = target_date.isoformat()
    next_day = (target_date + timedelta(days=1)).isoformat()

    query = {
        "where": {
            "rootSurfaceId": surface_id,
            "configurationId": CONFIGURATION_ID,
            "startDate": {"$gte": date_str, "$lt": next_day},
        }
    }
    url = f"{BASE_URL}?q={quote(json.dumps(query))}"

    client = await _get_client()
    resp = await client.get(url, headers=API_HEADERS)
    resp.raise_for_status()
    slots = resp.json()

    results = []
    for slot in slots:
        activity = slot.get("activityName")
        if isinstance(activity, dict):
            activity = activity.get("FR") or activity.get("EN") or ""

        results.append({
            "pool": pool_name,
            "source": "splextech",
            "start": slot.get("startDate", ""),
            "end": slot.get("endDate", ""),
            "activity": activity or "",
            "note": slot.get("notePublic") or "",
            "color": slot.get("activityColor") or "",
            "type": _classify_session(slot),
        })

    return results


async def fetch_all_pools(target_date: date) -> list[dict]:
    tasks = [
        fetch_slots(name, sid, target_date)
        for name, sid in POOLS.items()
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    slots = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            print(f"Error fetching {list(POOLS)[i]}: {result}")
        else:
            slots.extend(result)

    return slots
