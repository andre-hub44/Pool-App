"""Splextech API client for Laval indoor pools."""
import json
from datetime import date, timedelta
from urllib.parse import quote

import httpx

CONFIGURATION_ID = "65905251-d399-4fb3-9b2f-828cbde008dd"

# All pool surfaces on the Splextech system
POOLS = {
    "Piscine Vanier": "c128c29a-0da5-44ba-893b-08e7aa68571b",
    "Piscine Poly-Jeunesse": "9977e45d-688a-4999-8c8b-dc451cce3e51",
    "Piscine Honoré-Mercier": "e2e6fdd6-d75f-4dec-ae5d-0dec8e2f6669",
    "Centre sportif Josée-Faucher": "6ded38c4-7232-424d-9393-b83cdb908a40",
    "Complexe Aquatique – Bassin Récréatif": "8ecfbd2b-c8b0-4765-95f2-d399e51e5b95",
    "Complexe Aquatique – Bassin Sportif": "f30bd23f-e45f-4a23-b80e-d9db3f100b21",
    "Complexe Aquatique – Bassin Plongeon": "2cf53adb-c2ff-4f4c-a126-6546b814f208",
}

HEADERS = {
    "Origin": "https://calendar.splextech.com",
    "Referer": "https://calendar.splextech.com/",
    "Accept": "application/json",
}

BASE_URL = "https://app.splextech.com/orm/VenuesPublicOnlineCalendarConfigurationTimeSlotDetails"


def _classify_session(slot: dict) -> str:
    """Classify a time slot as 'public', 'club', 'closed', or 'other'."""
    note = (slot.get("notePublic") or "").lower()
    if "fermée" in note or "fermee" in note or "ferme" in note:
        return "closed"

    activity = slot.get("activityName")
    if isinstance(activity, dict):
        activity = activity.get("FR") or activity.get("EN") or ""
    activity = (activity or "").lower()

    public_keywords = ["bain libre", "bain longueur", "bain familial", "natation publique", "public"]
    closed_keywords = ["fermé", "ferme", "entretien", "maintenance"]
    club_keywords = ["club", "école", "ecole", "compétition", "competition", "entraînement", "entrainement", "triathlon"]

    if any(k in note for k in closed_keywords):
        return "closed"
    if any(k in activity for k in public_keywords) or any(k in note for k in public_keywords):
        return "public"
    if any(k in activity for k in club_keywords):
        return "club"
    if activity:
        return "club"
    return "other"


async def fetch_slots(pool_name: str, surface_id: str, target_date: date) -> list[dict]:
    """Fetch time slots for one pool on a given date."""
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

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, headers=HEADERS)
        resp.raise_for_status()
        slots = resp.json()

    results = []
    for slot in slots:
        start = slot.get("startDate", "")
        end = slot.get("endDate", "")

        activity = slot.get("activityName")
        if isinstance(activity, dict):
            activity = activity.get("FR") or activity.get("EN") or ""

        note = slot.get("notePublic") or ""
        color = slot.get("activityColor") or ""

        results.append({
            "pool": pool_name,
            "source": "splextech",
            "start": start,
            "end": end,
            "activity": activity,
            "note": note,
            "color": color,
            "type": _classify_session(slot),
        })

    return results


async def fetch_all_pools(target_date: date) -> list[dict]:
    """Fetch slots for all Splextech pools on a given date."""
    import asyncio

    tasks = [
        fetch_slots(name, surface_id, target_date)
        for name, surface_id in POOLS.items()
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    slots = []
    for i, result in enumerate(results):
        pool_name = list(POOLS.keys())[i]
        if isinstance(result, Exception):
            print(f"Error fetching {pool_name}: {result}")
        else:
            slots.extend(result)

    return slots
