"""
Open-source PDF/image schedule parser.

- PDFs  → pdfplumber  (table extraction, no OCR needed)
- Images → pytesseract (OCR)

Both return a common structure:
  {
    "pools": [{"name": str, "sessions": [...]}],
    "closures": [{"pool_name": str, "date": str, "reason": str}]
  }
"""

import io
import re
from typing import Optional

# ── Constants ────────────────────────────────────────────────────────────────

DAYS_FR = {
    "lundi": 0,
    "mardi": 1,
    "mercredi": 2,
    "jeudi": 3,
    "vendredi": 4,
    "samedi": 5,
    "dimanche": 6,
}

# Matches "6h à 9h", "7h35 à 8h40", "9h05 à 9h55", "06:00 - 11:45"
TIME_PATTERN = re.compile(
    r'(\d{1,2})[h:](\d{0,2})\s*[àa\-–]\s*(\d{1,2})[h:](\d{0,2})',
    re.IGNORECASE,
)

# Pool section headers commonly found in Laval PDFs
POOL_SECTION_PATTERNS = [
    re.compile(r'piscine[s]?\s*[|]?\s*(\d+\s*m[èe]tres?)', re.IGNORECASE),
    re.compile(r'bassin\s+r[eé]cr[eé]atif', re.IGNORECASE),
    re.compile(r'bassin\s+sportif', re.IGNORECASE),
    re.compile(r'bassin\s+plongeon', re.IGNORECASE),
]

ACTIVITY_KEYWORDS = {
    "couloirs": ("Couloirs de nage", "public"),
    "nage en continue": ("Couloirs de nage", "public"),
    "bain libre": ("Bain libre", "public"),
    "bain familial": ("Bain familial", "public"),
    "bain public": ("Bain public", "public"),
    "club": ("Club de natation", "club"),
    "programme": ("Programme", "club"),
    "cours": ("Cours", "club"),
}

CLOSURE_PATTERN = re.compile(
    r'(\d{1,2})\s*(janvier|f[eé]vrier|mars|avril|mai|juin|juillet|ao[uû]t|septembre|octobre|novembre|d[eé]cembre)',
    re.IGNORECASE,
)

MONTHS_FR = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4,
    "mai": 5, "juin": 6, "juillet": 7, "août": 8, "aout": 8,
    "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def fmt_time(h: str, m: str) -> str:
    return f"{int(h):02d}:{int(m) if m.strip() else 0:02d}"


def extract_time_slots(text: str) -> list[tuple[str, str]]:
    """Return list of (start_HH:MM, end_HH:MM) from a block of text."""
    slots = []
    for match in TIME_PATTERN.finditer(text or ""):
        start = fmt_time(match.group(1), match.group(2))
        end   = fmt_time(match.group(3), match.group(4))
        slots.append((start, end))
    return slots


def find_day_columns(row: list) -> dict[int, int]:
    """Return {col_index: day_of_week} from a header row."""
    mapping = {}
    for i, cell in enumerate(row or []):
        if not cell:
            continue
        text = str(cell).strip().lower()
        for name, num in DAYS_FR.items():
            if name in text:
                mapping[i] = num
                break
    return mapping


def guess_activity(section_text: str) -> tuple[str, str]:
    """Return (activity_label, type) based on context text."""
    lower = section_text.lower()
    for keyword, result in ACTIVITY_KEYWORDS.items():
        if keyword in lower:
            return result
    return ("Activité", "public")


def extract_pool_name(text: str, fallback: str = "Piscine importée") -> str:
    """Try to find the pool name from the top of the document text."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for line in lines[:10]:
        lower = line.lower()
        if any(k in lower for k in ("piscine", "bassin", "complexe", "centre")):
            return line
    return fallback


def parse_closure_dates(text: str, pool_name: str) -> list[dict]:
    """Extract planned closure dates from free text."""
    closures = []
    import datetime

    current_year = datetime.date.today().year

    # Look for lines containing "fermeture" or "fermé"
    for line in text.splitlines():
        if not any(k in line.lower() for k in ("ferm", "fermeture")):
            continue
        for match in CLOSURE_PATTERN.finditer(line):
            day = int(match.group(1))
            month = MONTHS_FR.get(match.group(2).lower().replace("é", "e").replace("û", "u").replace("ô", "o"))
            if not month:
                continue
            year = current_year if month >= datetime.date.today().month else current_year + 1
            try:
                d = datetime.date(year, month, day)
                closures.append({
                    "pool_name": pool_name,
                    "date": d.isoformat(),
                    "reason": line.strip(),
                })
            except ValueError:
                pass

    return closures


# ── PDF parser (pdfplumber) ──────────────────────────────────────────────────

def parse_pdf(content: bytes) -> dict:
    """Parse a PDF schedule using pdfplumber table extraction."""
    try:
        import pdfplumber
    except ImportError:
        raise RuntimeError("pdfplumber not installed. Run: pip install pdfplumber")

    pools: dict[str, list] = {}
    all_closures: list[dict] = []
    full_text = ""

    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            full_text += page_text + "\n"

            # Current pool name for this page (try title, fall back to doc)
            current_pool = extract_pool_name(page_text)

            # Try to find section boundaries in the text
            section_pools = _split_sections(page_text, current_pool)

            tables = page.extract_tables()
            if not tables:
                continue

            for table in tables:
                if not table:
                    continue

                # Find the header row with day names
                day_cols: dict[int, int] = {}
                header_idx = 0
                for i, row in enumerate(table):
                    day_cols = find_day_columns(row)
                    if day_cols:
                        header_idx = i
                        break

                if not day_cols:
                    continue

                # Determine activity from rows above the header or page context
                context = " ".join(str(c) for row in table[:header_idx] for c in (row or []) if c)
                activity, atype = guess_activity(context or page_text)

                # Walk rows below header
                current_section_pool = current_pool
                for row in table[header_idx + 1:]:
                    if not row:
                        continue

                    # Check if this row is a new section header (e.g. "Bassin récréatif")
                    row_text = " ".join(str(c) for c in row if c)
                    new_section = _detect_section(row_text)
                    if new_section:
                        current_section_pool = new_section
                        activity, atype = guess_activity(row_text)
                        continue

                    # Check for activity label rows (no times, but activity name)
                    if not any(TIME_PATTERN.search(str(c) or "") for c in row):
                        row_activity, row_atype = guess_activity(row_text)
                        if row_text.strip():
                            activity, atype = row_activity, row_atype
                        continue

                    pool_key = current_section_pool
                    if pool_key not in pools:
                        pools[pool_key] = []

                    for col_idx, dow in day_cols.items():
                        cell = str(row[col_idx]) if col_idx < len(row) and row[col_idx] else ""
                        for start, end in extract_time_slots(cell):
                            # description: corridor count like "(8)"
                            desc_match = re.search(r'\((\d+)\)', cell)
                            desc = f"{desc_match.group(1)} corridors" if desc_match else ""
                            pools[pool_key].append({
                                "day_of_week": dow,
                                "start_time": start,
                                "end_time": end,
                                "activity": activity,
                                "type": atype,
                                "description": desc,
                            })

    # Parse closures from full document text
    for pool_name in pools:
        all_closures.extend(parse_closure_dates(full_text, pool_name))

    return {
        "pools": [{"name": name, "sessions": sessions} for name, sessions in pools.items()],
        "closures": all_closures,
    }


def _split_sections(text: str, default_name: str) -> dict[str, str]:
    """Return {section_name: section_text} from a page."""
    # Simple: return one section for now
    return {default_name: text}


def _detect_section(row_text: str) -> Optional[str]:
    """If a row looks like a pool section header, return its name."""
    lower = row_text.lower().strip()
    for pattern in POOL_SECTION_PATTERNS:
        if pattern.search(lower):
            return row_text.strip().title()
    return None


# ── Image parser (pytesseract OCR) ───────────────────────────────────────────

def parse_image(content: bytes, media_type: str = "image/jpeg") -> dict:
    """Parse a JPG/PNG schedule image using pytesseract OCR + regex."""
    try:
        import pytesseract
        from PIL import Image as PILImage
    except ImportError:
        raise RuntimeError(
            "pytesseract and Pillow not installed. "
            "Run: pip install pytesseract pillow  (also install Tesseract system package)"
        )

    img = PILImage.open(io.BytesIO(content))

    # OCR with French language for better accent handling
    try:
        text = pytesseract.image_to_string(img, lang="fra+eng")
    except Exception:
        text = pytesseract.image_to_string(img)

    return _parse_text_schedule(text)


def _parse_text_schedule(text: str) -> dict:
    """
    Parse free-form OCR text into a schedule dict.
    Works best when the document follows a day-column structure.
    """
    lines = [l.strip() for l in text.splitlines()]
    pool_name = extract_pool_name(text)

    # Find the line containing day headers
    day_header_line_idx = None
    day_order: list[int] = []  # ordered list of day_of_week values

    for i, line in enumerate(lines):
        lower = line.lower()
        found = [(pos, dow) for name, dow in DAYS_FR.items()
                 if (pos := lower.find(name)) != -1]
        if len(found) >= 3:  # at least 3 days on a line = header row
            day_header_line_idx = i
            day_order = [dow for _, dow in sorted(found)]
            break

    if day_header_line_idx is None or not day_order:
        # Fallback: just collect all time slots found anywhere
        sessions = []
        for start, end in extract_time_slots(text):
            sessions.append({
                "day_of_week": 0,
                "start_time": start,
                "end_time": end,
                "activity": "Activité importée",
                "type": "public",
                "description": "",
            })
        return {
            "pools": [{"name": pool_name, "sessions": sessions}],
            "closures": [],
        }

    # Parse rows after the header
    sessions = []
    activity = "Activité"
    atype = "public"

    for line in lines[day_header_line_idx + 1:]:
        if not line:
            continue

        # Activity label line (no times)
        if not TIME_PATTERN.search(line):
            a, t = guess_activity(line)
            if a != "Activité":
                activity, atype = a, t
            continue

        # Time slots line — split by whitespace chunks and match to day columns
        # Each "chunk" with a time corresponds to the next day in order
        slots = extract_time_slots(line)
        for i, (start, end) in enumerate(slots):
            if i < len(day_order):
                sessions.append({
                    "day_of_week": day_order[i],
                    "start_time": start,
                    "end_time": end,
                    "activity": activity,
                    "type": atype,
                    "description": "",
                })

    closures = parse_closure_dates(text, pool_name)

    return {
        "pools": [{"name": pool_name, "sessions": sessions}],
        "closures": closures,
    }


# ── Main entry point ─────────────────────────────────────────────────────────

def parse_schedule_file(content: bytes, media_type: str) -> dict:
    """Route to the right parser based on media type."""
    if media_type == "application/pdf":
        return parse_pdf(content)
    elif media_type in ("image/jpeg", "image/jpg", "image/png", "image/webp"):
        return parse_image(content, media_type)
    else:
        raise ValueError(f"Unsupported media type: {media_type}")
