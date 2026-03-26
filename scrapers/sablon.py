"""Scraper for Centre du Sablon pool schedule (static HTML table)."""
from datetime import date

import httpx
from bs4 import BeautifulSoup

URL = "https://centredusablon.ca/bain-libre/"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept-Language": "fr-CA,fr;q=0.9",
}

# Day name mapping (FR -> weekday index, Monday=0)
DAY_MAP = {
    "lundi": 0, "mardi": 1, "mercredi": 2, "jeudi": 3,
    "vendredi": 4, "samedi": 5, "dimanche": 6,
}


async def fetch_schedule(target_date: date) -> list[dict]:
    """Scrape Centre du Sablon schedule and return slots matching the target date."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(URL, headers=HEADERS)
        resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    target_weekday = target_date.weekday()  # Monday=0

    slots = []
    tables = soup.find_all("table")

    for table in tables:
        # Detect table header to find session type
        header = ""
        preceding = table.find_previous(["h2", "h3", "h4", "strong", "p"])
        if preceding:
            header = preceding.get_text(strip=True)

        rows = table.find_all("tr")
        for row in rows:
            cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
            if len(cells) < 2:
                continue

            day_cell = cells[0].lower()
            matched_day = None
            for day_name, day_idx in DAY_MAP.items():
                if day_name in day_cell:
                    matched_day = day_idx
                    break

            if matched_day is None or matched_day != target_weekday:
                continue

            # Parse time range from remaining cells
            time_info = " ".join(cells[1:])
            slots.append({
                "pool": "Centre du Sablon",
                "source": "sablon",
                "start": None,
                "end": None,
                "activity": header or "Bain libre",
                "note": time_info,
                "color": "",
                "type": _classify_sablon(header),
            })

    return slots


def _classify_sablon(header: str) -> str:
    header_lower = header.lower()
    if "longueur" in header_lower:
        return "public"
    if "familial" in header_lower:
        return "public"
    if "libre" in header_lower:
        return "public"
    return "public"
