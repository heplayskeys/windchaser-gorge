#!/usr/bin/env python3
"""
Gorge wind conditions scraper — v2.

Adds on top of v1:
  - normalize_case(): turns VICTOR'S ALL-CAPS TEXT into readable sentence case,
    while preserving known acronyms (MPH, NW, PDX, NWS...) and place names
    (Hood River, Stevenson, Viento...).
  - extract_zones(): pulls "NN-NNmph from/at/between ZONE" mentions out of the
    prose and turns them into a structured [{zone, low, high}] list, ordered
    geographically west -> east, so the dashboard can render a bar chart
    instead of asking someone to read a paragraph.
  - extract_flags(): looks for advisories/smoke/fire-danger callouts so the
    dashboard can show colored condition badges.

Verified against several years of thegorgeismygym.com's archive: the
"NN-NNmph from/at/between ZONE" phrasing has been consistent since at least
2017, so this structured extraction should keep working without babysitting.
Victor's site has no archive (single page, overwritten daily) so its
structure is assumed stable but worth spot-checking occasionally.
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

# The Gorge, and both source sites, all operate on Pacific time — "today"
# needs to mean Pacific's today, not UTC's. UTC is far enough ahead that a
# run any time after ~5pm Pacific would otherwise see "tomorrow" in UTC
# while it's still today in the Gorge, silently skipping the current day.
PACIFIC = ZoneInfo("America/Los_Angeles")
from bs4 import BeautifulSoup

try:
    from io import BytesIO
    from PIL import Image
    import pytesseract
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; GorgeDashboardBot/1.0; personal use)"
}

VTI_URL = "https://victortheinflictor.com"
GORGE_GYM_URL = "https://thegorgeismygym.com/forecast"

# Geographic order, west to east, used to sort the zone chart the same way
# the wind actually moves through the corridor.
ZONE_ORDER = [
    "coast", "astoria", "rooster rock", "corridor", "cascade locks", "stevenson", "wyeth",
    "viento", "hood river", "the hatch", "hatch", "hatchery", "mosier",
    "rowena", "the dalles", "doug's", "dougs", "lyle", "the wall", "wall",
    "swell city", "swell", "celilo", "rufus", "arlington", "roosevelt", "pasco", "desert",
]

# Multi-letter acronyms/abbreviations to keep uppercase after normalizing.
ACRONYMS = [
    "MPH", "NW", "SW", "SE", "NE", "KT", "PDX", "NWS", "VTI", "GFS",
    "TATAS", "USACE", "AST", "HR", "TD", "PST", "PDT", "SR-14", "I-84",
    "AM", "PM",
]

# Known place names to title-case properly (regex handles apostrophes/case).
PLACE_NAMES = [
    "hood river", "stevenson", "viento", "mosier", "rowena", "the dalles",
    "doug's", "dougs", "lyle", "rufus", "arlington", "celilo", "pasco",
    "cascade locks", "the hatch", "hatchery", "wyeth", "the wall",
    "white salmon", "goldendale", "trout lake", "parkdale", "odell",
    "husum", "willard", "boardman", "sauvie island", "jones beach",
    "astoria", "portland", "glenwood",
]


def light_normalize(snippet: str) -> str:
    """
    Simple cleanup for short context snippets: lowercase, capitalize just
    the first letter, restore acronyms/place names. Skips normalize_case's
    per-sentence splitting, which isn't meaningful on a short fragment and
    was mis-capitalizing mid-word after an ellipsis.
    """
    lowered = snippet.lower()
    for i, ch in enumerate(lowered):
        if ch.isalpha() and (i == 0 or not lowered[i - 1].isalnum()):
            lowered = lowered[:i] + ch.upper() + lowered[i + 1:]
            break
    for acr in ACRONYMS:
        lowered = re.sub(rf"\b{re.escape(acr.lower())}\b", acr, lowered, flags=re.I)
    for place in PLACE_NAMES:
        title = nice_case(place)
        lowered = re.sub(rf"\b{re.escape(place)}\b", title, lowered, flags=re.I)
    return lowered


def normalize_case(text: str) -> str:
    """
    Turn ALL-CAPS or inconsistently-cased forecast text into readable
    sentence case, without mangling acronyms or place names.
    """
    if not text:
        return text

    lowered = text.lower()

    # Split into sentences on ./!/? or the ellipsis-heavy style both sites use
    sentences = re.split(r"(?<=[.!?])\s+|\.\.\.\s*", lowered)
    rebuilt = []
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        # capitalize first alphabetic character
        for i, ch in enumerate(s):
            if ch.isalpha():
                s = s[:i] + ch.upper() + s[i + 1:]
                break
        rebuilt.append(s)
    result = ". ".join(rebuilt)
    if result and not result.endswith((".", "!", "?")):
        result += "."

    # restore acronyms (word-boundary, case-insensitive)
    for acr in ACRONYMS:
        result = re.sub(rf"\b{re.escape(acr.lower())}\b", acr, result, flags=re.I)

    # restore place names in title case
    for place in PLACE_NAMES:
        title = " ".join(w.capitalize() if w != "the" else w for w in place.split())
        title = title[0].upper() + title[1:]  # capitalize even if starts with "the"
        result = re.sub(rf"\b{re.escape(place)}\b", title, result, flags=re.I)

    # standalone "i" -> "I"
    result = re.sub(r"\bi\b", "I", result)

    # cosmetic cleanup: collapse doubled punctuation left over from source
    result = re.sub(r"\.{2,}", ".", result)
    result = re.sub(r"\s+([.,])", r"\1", result)

    return result.strip()


def extract_zones(text: str):
    """
    Pull {zone, low, high} entries out of prose like:
    "21-24mph from Stevenson to Doug's" or "17-20mph at the Hatch".
    Aggregates min/max per zone if a zone is mentioned multiple times
    (e.g. once for morning, once for afternoon), keeping the FULL range —
    the dashboard's tally chart color-codes low-to-high across that range,
    which is what surfaces "it gets light in the morning, strong by
    afternoon" patterns without discarding any of the data.
    """
    pattern = re.compile(
        r"(\d{1,2})-(\d{1,2})\s?mph\s+(?:from|at|between|near)\s+"
        r"([A-Za-z][A-Za-z'\s]{2,25}?)"
        r"(?:\s+to\s+([A-Za-z][A-Za-z'\s]{2,25}?))?"
        r"(?=[.,]| with | and |$)",
        re.I,
    )

    zones = {}
    for m in pattern.finditer(text):
        low, high = int(m.group(1)), int(m.group(2))
        for zname in filter(None, [m.group(3), m.group(4)]):
            key = zname.strip().lower().rstrip(".")
            # only accept it if it's actually a known Gorge place name —
            # otherwise stray phrases like "at best" get mistaken for a zone
            if not any(vocab == key or vocab in key or key in vocab for vocab in MENTION_VOCAB):
                continue
            if key not in zones:
                zones[key] = [low, high]
            else:
                zones[key][0] = min(zones[key][0], low)
                zones[key][1] = max(zones[key][1], high)

    def sort_key(item):
        key = item[0]
        for i, z in enumerate(ZONE_ORDER):
            if z in key:
                return i
        return len(ZONE_ORDER)  # unknown zones sort last

    ordered = sorted(zones.items(), key=sort_key)
    return [
        {"zone": apply_zone_alias(normalize_case(z).rstrip(".")), "low": v[0], "high": v[1]}
        for z, v in ordered
    ]


# Union vocabulary used for "does this source mention this spot at all" —
# broader than extract_zones(), which requires a number tied to the name.
MENTION_VOCAB = sorted(set(ZONE_ORDER) | set(PLACE_NAMES), key=len, reverse=True)


def nice_case(term: str) -> str:
    words = term.split()
    return " ".join(w if w == "the" else w.capitalize() for w in words)


# Display-only rename — matching against the source text still uses "Doug's"
# (that's what the sites actually say), this just relabels it on output.
ZONE_DISPLAY_ALIASES = {"doug's": "Lyle", "dougs": "Lyle"}


def apply_zone_alias(name: str) -> str:
    key = name.strip().lower().rstrip(".")
    return ZONE_DISPLAY_ALIASES.get(key, name)


def extract_zone_mentions(text: str):
    """
    Which known Gorge locations does this source name at all, with or
    without an attached wind number? Used to detect when both forecasters
    are talking about the same spot today.
    """
    t = text.lower()
    raw_found = [term for term in MENTION_VOCAB if re.search(rf"\b{re.escape(term)}\b", t)]
    # drop shorter matches subsumed by a longer one (e.g. "hatch" inside "the hatch")
    raw_found.sort(key=len, reverse=True)
    kept = []
    for term in raw_found:
        if not any(term != other and term in other for other in kept):
            kept.append(term)
    return {apply_zone_alias(nice_case(term)) for term in kept}


def extract_mph_mentions(text: str, limit=6, window=55):
    """
    Any 'NN-NNmph' mention anywhere, each with a short snippet of
    surrounding text — used to power tappable chips that reveal what the
    number was actually talking about.
    """
    out, seen = [], set()
    for m in re.finditer(r"\d{1,2}-\d{1,2}\s?mph", text, re.I):
        norm = m.group(0).replace(" ", "").lower()
        if norm in seen:
            continue
        seen.add(norm)
        start = max(0, m.start() - window)
        end = min(len(text), m.end() + window)
        snippet = text[start:end].strip()
        if start > 0:
            snippet = "…" + snippet
        if end < len(text):
            snippet = snippet + "…"
        where = sorted(extract_zone_mentions(snippet))
        out.append({
            "range": m.group(0).replace(" ", ""),
            "where": where[0] if len(where) == 1 else (", ".join(where) if where else None),
            "context": light_normalize(snippet),
        })
        if len(out) >= limit:
            break
    return out


# Static credibility lines pulled from each forecaster's own bio — stable
# facts about the person, not something that needs re-scraping daily.
CREDIBILITY = {
    "Victor the Inflictor": "40+ years reading Gorge wind",
    "The Gorge Is My Gym (Temira)": "Hyperlocal Gorge forecaster since 2006",
}


DAY_NAMES = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]
DAY_ABBR = {d: d[:3].capitalize() for d in DAY_NAMES}

# Ordered so the more specific phrase is checked before a broader one that
# would otherwise mis-fire on a substring (e.g. "late morning" vs "morning").
TIME_SEGMENT_KEYWORDS = [
    ("dawn", ("dawn patrol", "dawn")),
    ("early", ("early wind", "early")),
    ("morning", ("late morning", "morning")),
    ("afternoon", ("afternoon",)),
    ("evening", ("evening", "late day", "by evening")),
    ("night", ("overnight", "tonight", "night")),
]


def extract_time_segments(text: str):
    """
    Same sentence-walking approach as the day-tracker below, but for
    time-of-day instead of day-of-week: tags each mph range with whichever
    period (dawn/early/morning/afternoon/evening) was most recently
    mentioned. This is an additional, optional breakdown — it doesn't
    replace or trim the full low/high range used elsewhere.
    """
    sentences = re.split(r"(?<=[.!?])\s+", text)
    segments = {}
    current = None

    for sentence in sentences:
        low = sentence.lower()
        for label, phrases in TIME_SEGMENT_KEYWORDS:
            if any(p in low for p in phrases):
                current = label
                break
        if not current:
            continue
        for lo, hi in re.findall(r"(\d{1,2})-(\d{1,2})\s?mph", sentence, re.I):
            lo, hi = int(lo), int(hi)
            rec = segments.setdefault(current, {"low": None, "high": None})
            rec["low"] = lo if rec["low"] is None else min(rec["low"], lo)
            rec["high"] = hi if rec["high"] is None else max(rec["high"], hi)

    order = ["dawn", "early", "morning", "afternoon", "evening", "night"]
    return [{"period": p.capitalize(), "low": segments[p]["low"], "high": segments[p]["high"]}
            for p in order if p in segments and segments[p]["low"] is not None]


ZONE_PATTERN = re.compile(
    r"(\d{1,2})-(\d{1,2})\s?mph\s+(?:from|at|between|near)\s+"
    r"([A-Za-z][A-Za-z'\s]{2,25}?)"
    r"(?:\s+to\s+([A-Za-z][A-Za-z'\s]{2,25}?))?"
    r"(?=[.,]| with | and |$)",
    re.I,
)


def extract_zone_periods(text: str):
    """
    Combines extract_zones' zone-name matching with extract_time_segments'
    time-of-day tracking, sentence by sentence, so each zone mention is
    tagged with the period it was said in — e.g. Stevenson: Early 6-10mph,
    Afternoon 17-21mph, rather than one flattened 6-21mph blob. Feeds the
    Wind by Zone period selector.
    """
    sentences = re.split(r"(?<=[.!?])\s+", text)
    current = None
    results = {}

    for sentence in sentences:
        low_s = sentence.lower()
        for label, phrases in TIME_SEGMENT_KEYWORDS:
            if any(p in low_s for p in phrases):
                current = label
                break
        if not current:
            continue
        for m in ZONE_PATTERN.finditer(sentence):
            lo, hi = int(m.group(1)), int(m.group(2))
            for zname in filter(None, [m.group(3), m.group(4)]):
                key = zname.strip().lower().rstrip(".")
                if not any(vocab == key or vocab in key or key in vocab for vocab in MENTION_VOCAB):
                    continue
                rkey = (key, current)
                if rkey not in results:
                    results[rkey] = [lo, hi]
                else:
                    results[rkey][0] = min(results[rkey][0], lo)
                    results[rkey][1] = max(results[rkey][1], hi)

    return [
        {"zone": apply_zone_alias(normalize_case(zkey).rstrip(".")), "period": period.capitalize(), "low": lo, "high": hi}
        for (zkey, period), (lo, hi) in results.items()
    ]


def merge_zone_periods(all_lists):
    """Combine per-zone-per-period data across both sources."""
    merged = {}
    for entries in all_lists:
        for e in entries:
            key = (e["zone"].lower(), e["period"])
            if key not in merged:
                merged[key] = dict(e)
            else:
                merged[key]["low"] = min(merged[key]["low"], e["low"])
                merged[key]["high"] = max(merged[key]["high"], e["high"])
    return list(merged.values())



def extract_direction(text: str):
    """
    Overall prevailing wind direction, converted to the *-erly term
    (westerly = wind FROM the west). Picks the first cardinal direction
    mentioned near the word 'wind'.
    """
    m = re.search(r"\b(west|east|north|south)(?:erly)?\s+wind", text, re.I)
    if m:
        return m.group(1).lower() + "erly"
    return None


def parse_daily_mentions(text: str):
    """
    Walks the longer-term forecast prose sentence by sentence, tracking
    which day is currently being discussed, and pulls out any mph ranges
    and direction mentioned while that day is "in focus". This mirrors how
    Temira actually writes the outlook (a running narrative, day by day),
    so it's a reasonable structured approximation rather than a guarantee.
    Returns a raw {day_name: {low, high, direction}} dict.
    """
    sentences = re.split(r"(?<=[.!?])\s+", text)
    days = {}
    current_day = None

    for sentence in sentences:
        low = sentence.lower()
        for d in DAY_NAMES:
            if d in low:
                current_day = d
                break
        if not current_day:
            continue

        ranges = re.findall(r"(\d{1,2})-(\d{1,2})\s?mph", sentence, re.I)
        if ranges:
            days.setdefault(current_day, {"low": None, "high": None, "direction": None})
            for lo, hi in ranges:
                lo, hi = int(lo), int(hi)
                rec = days[current_day]
                rec["low"] = lo if rec["low"] is None else min(rec["low"], lo)
                rec["high"] = hi if rec["high"] is None else max(rec["high"], hi)

        dir_match = re.search(r"\b(west|east|north|south)(?:erly)?", sentence, re.I)
        if dir_match and current_day in days:
            days[current_day]["direction"] = dir_match.group(1).lower() + "erly"

    return days


def build_weekly_outlook(daily, today_low, today_high, today_direction):
    """
    Always returns exactly 7 slots, starting with today, e.g. Thu-Wed if
    today is Thursday. Today's numbers come from the already-parsed zone
    data (the same numbers driving the Zones section); the rest come from
    Temira's longer-term prose. Days without a clean number get a
    placeholder (low/high = None) rather than being dropped, so the grid
    always represents the full week.
    """
    python_to_day = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    today_idx = datetime.now(PACIFIC).weekday()  # Mon=0..Sun=6 — Pacific, not UTC (see PACIFIC comment above)
    ordered_names = (python_to_day[today_idx:] + python_to_day[:today_idx])[:7]

    outlook = []
    for i, d in enumerate(ordered_names):
        if i == 0:
            outlook.append({"day": DAY_ABBR[d], "low": today_low, "high": today_high, "direction": today_direction})
            continue
        rec = daily.get(d)
        if rec and rec["low"] is not None:
            outlook.append({"day": DAY_ABBR[d], "low": rec["low"], "high": rec["high"], "direction": rec["direction"]})
        else:
            outlook.append({"day": DAY_ABBR[d], "low": None, "high": None, "direction": None})
    return outlook


def _flag_context(text: str, keyword_pattern: str, window=60):
    """Grab the sentence-ish snippet around a keyword match, plus any
    known zone/place names mentioned in it (None if the mention reads as
    general/gorge-wide rather than tied to a specific spot)."""
    m = re.search(keyword_pattern, text, re.I)
    if not m:
        return None, None
    start = max(0, m.start() - window)
    end = min(len(text), m.end() + window)
    snippet = text[start:end].strip()
    where_set = extract_zone_mentions(snippet)
    where = ", ".join(sorted(where_set)) if where_set else None
    return light_normalize(("…" if start > 0 else "") + snippet + ("…" if end < len(text) else "")), where


def extract_flags(text: str):
    """Detect notable conditions worth a badge on the dashboard, each with
    a context snippet and, if identifiable, which spot it applies to.
    Covers the advisory types that actually show up on these sites across
    a full year, not just peak summer (fire/smoke) or peak winter (ice)."""
    flags = []

    checks = [
        ("Small Craft Advisory", "warning", r"small craft advisory"),
        ("Fire Danger", "warning", r"red flag warning|high fire danger"),
        ("Smoke / Haze", "caution", r"smoke|hazy|haze"),
        ("Heat Advisory", "warning", r"heat advisory|excessive heat warning|extreme heat"),
        ("Ice Storm", "warning", r"ice storm"),
        ("Winter Storm", "warning", r"winter storm warning|winter weather advisory"),
        ("Freeze Warning", "caution", r"freeze warning|frost advisory"),
        ("Air Quality Alert", "caution", r"air quality alert|unhealthy air|unhealthy for sensitive"),
        ("Avalanche Danger", "warning", r"avalanche warning|avalanche advisory|avalanche danger"),
        ("Flood Warning", "warning", r"flood warning|flash flood"),
        ("High Wind Warning", "warning", r"high wind warning"),
        ("Wind Advisory", "caution", r"\bwind advisory\b"),
        ("Dense Fog Advisory", "caution", r"dense fog advisory"),
        ("Wind Chill Advisory", "caution", r"wind chill advisory|wind chill warning"),
    ]
    for label, level, pattern in checks:
        if re.search(pattern, text, re.I):
            context, where = _flag_context(text, pattern)
            flags.append({"label": label, "level": level, "context": context, "where": where})
    return flags


# Sentences containing any of these are dropped outright — donation asks,
# site-maintenance boilerplate, and tangents that have nothing to do with
# today's wind. This is the biggest single quality win: it's what was
# letting "please Venmo me" and cache-refresh instructions into the summary.
OFFTOPIC_MARKERS = (
    "venmo", "paypal", "donat", "subscribe", "contribut", " tip ", "tip temira",
    "hard refresh", "incognito", "flushing your cache", "still seeing yesterday",
    "trail", "closure", "work party", "humidity", "partly cloudy", "sunny,",
    "around the world", "safe travels", "have a great week", "see you on the",
    "fingers crossed", "hold off on any solid planning",
    "ikite", "iwind", "sensor", "reads low", "reads high", "think like a local",
)

# Sentences containing any of these are considered genuinely wind-relevant
# and kept (after off-topic sentences are already dropped). A sentence with
# none of these markers and no actual wind number is dropped too — it's
# almost always scene-setting chit-chat ("Friday started off with clear
# sky...") rather than something worth showing in a short summary.
WIND_RELEVANT_MARKERS = (
    "mph", "wind", "gust", "kt ", "westerly", "easterly", "northerly", "southerly",
    "advisory", "warning", "smoke", "haze", "fire danger",
)


def summarize_forecast(text: str, max_sentences: int = 3) -> str:
    """
    Zero-cost, no-API extractive summary, targeted at TODAY's wind
    specifically: split into sentences, drop off-topic ones outright,
    drop sentences that are about a different day ("returning Monday",
    "Tuesday may be windier") unless they also mention "today", then
    prefer whatever's left that has an actual wind number over sentences
    that just mention "wind" as a word. This is a filter, not true
    language understanding — it won't paraphrase or compress a sentence,
    just choose which ones to keep — but it reliably removes the
    donation asks / boilerplate / other-day chatter that were making the
    raw paragraph dump read as nonsense.
    """
    if not text:
        return text
    sentences = re.split(r"(?<=[.!?])\s+", text)
    with_numbers, without_numbers = [], []
    for s in sentences:
        low = s.lower()
        if any(m in low for m in OFFTOPIC_MARKERS):
            continue
        if not any(m in low for m in WIND_RELEVANT_MARKERS):
            continue
        if any(day in low for day in DAY_NAMES) and "today" not in low and "tonight" not in low:
            continue  # about a different day, not today
        s = s.strip()
        if re.search(r"\d{1,2}-\d{1,2}\s?mph|\d{1,2}\s?mph|\d{1,2}\s?kt", low):
            with_numbers.append(s)
        else:
            without_numbers.append(s)

    kept = (with_numbers + without_numbers)[:max_sentences]
    return " ".join(kept) if kept else text  # fall back to raw text rather than showing nothing


# Free-tier Google Gemini API, used only for a single supplementary
# one-sentence "AI summary" per source — NOT the source of truth for any
# numbers on the dashboard (the regex-based zone extraction still owns
# that). Uses the "latest" alias rather than a pinned version so this
# doesn't break every time Google cycles model names.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-flash-lite-latest"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"


def ai_summarize(raw_text: str):
    """
    One-sentence AI summary of today's forecast. Returns None on ANY
    failure — no key configured, network error, rate limit, unexpected
    response shape — so a bad day for the API never breaks the scrape.
    The dashboard shows a plain "No summary available" fallback in that case.
    """
    if not GEMINI_API_KEY or not raw_text:
        return None
    prompt = (
        "Summarize this kiteboarding wind forecast in ONE short sentence "
        "(under 30 words), focused only on today's conditions. No preamble, "
        "no caveats, just the sentence:\n\n" + raw_text
    )
    try:
        resp = requests.post(
            GEMINI_URL,
            headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        return text or None
    except Exception as e:
        print(f"WARNING: AI summary failed: {e}", file=sys.stderr)
        return None


def clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text.replace("…", "...")


def fetch(url: str) -> BeautifulSoup:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


# Unambiguous markers of Victor's Coast/marine section — never relevant to
# Gorge kiting, and his page mixes it in right alongside the Today section.
COAST_MARKERS = ("nws forecast", "small craft", "seas ", "coastal waters", "wave detail", "pistol river")


def scrape_victor():
    soup = fetch(VTI_URL)
    candidates = []
    for tag in soup.find_all(["p", "div", "span"]):
        text = clean(tag.get_text(" "))
        if len(text) < 80:
            continue
        if any(marker in text.lower() for marker in COAST_MARKERS):
            continue  # Coast/marine section — not applicable to Gorge kiting
        # Require an actual wind NUMBER, not just the bare word "wind" —
        # his page repeats "WIND PREDICTOR" branding in the header/nav,
        # which was satisfying a looser check and getting picked over his
        # real forecast text.
        if re.search(r"\d{1,2}-\d{1,2}\s?mph|\d{1,2}\s?mph|\d{1,2}-\d{1,2}\s?kt", text, re.I):
            candidates.append(text)

    seen, unique = set(), []
    for c in candidates:
        key = c[:60]
        if key not in seen:
            seen.add(key)
            unique.append(c)

    raw_text = unique[0] if unique else ""

    return {
        "source": "Victor the Inflictor",
        "url": VTI_URL,
        "credibility": CREDIBILITY["Victor the Inflictor"],
        "ai_summary": ai_summarize(raw_text),
        "zones": extract_zones(raw_text),
        "flags": extract_flags(raw_text),
        "mph_mentions": extract_mph_mentions(raw_text),
        "time_segments": extract_time_segments(raw_text),
        "zone_periods": extract_zone_periods(raw_text),
        "_mentions": extract_zone_mentions(raw_text),  # used for cross-source agreement, stripped before writing
    }


# Row labels as they appear in Temira's wind table, mapped to our own zone
# vocabulary. Calibrated from one real sample image (Aug 2026) — her row
# order/colors have been stable across the samples checked, but this is
# the part most likely to need adjustment if her table design changes.
WINDTABLE_LABEL_ALIASES = {
    "rooster rock": "Rooster Rock", "iwash": "Rooster Rock",
    "stevenson": "Stevenson",
    "viento": "Viento",
    "swell-hood river": "Swell City", "hood river": "Hood River",
    "lyle-doug's": "Lyle", "lyle": "Lyle", "doug's": "Lyle",
    "rufus": "Rufus",
    "roosevelt": "Roosevelt", "arlington": "Arlington",
}
# Skip rows that aren't wind-zone data at all (river flow / temperature rows)
WINDTABLE_SKIP_LABELS = ("river flow", "temps", "temp")

WINDTABLE_PERIODS = ["Dawn", "Morning", "Afternoon", "Evening"]  # Dawn Patrol / morning / afternoon / later


def _is_true_border_color(c):
    r, g, b = c[:3]
    if not (abs(r - g) < 10 and abs(g - b) < 10 and abs(r - b) < 10):
        return False
    return 100 < r < 250


def _is_near_white(c):
    r, g, b = c[:3]
    return r > 245 and g > 245 and b > 245


def _detect_row_bands(img):
    """Find each colored row's (y_start, y_end) by scanning down the label
    column and grouping contiguous non-border/non-white runs."""
    w, h = img.size
    x_sample = int(w * 0.10)
    bands, in_band, band_start = [], False, None
    for y in range(h):
        c = img.getpixel((x_sample, y))
        is_sep = _is_true_border_color(c) or _is_near_white(c)
        if not is_sep and not in_band:
            in_band, band_start = True, y
        elif is_sep and in_band:
            in_band = False
            if y - band_start > 12:
                bands.append((band_start, y))
    if in_band and h - band_start > 12:
        bands.append((band_start, h))
    return bands


def _ocr_crop(img, box, psm=7):
    crop = img.crop(box)
    big = crop.resize((crop.width * 4, crop.height * 4), Image.LANCZOS)
    return pytesseract.image_to_string(big, config=f"--psm {psm}").strip()


def _match_windtable_label(label_text):
    low = label_text.lower()
    if any(skip in low for skip in WINDTABLE_SKIP_LABELS):
        return None
    for key, zone in WINDTABLE_LABEL_ALIASES.items():
        if key in low:
            return zone
    return None


def extract_windtable(image_bytes):
    """
    Full pipeline: detect each row's color band, OCR its label column and
    each of the 4 period columns SEPARATELY (isolating each region gave
    near-perfect accuracy in testing, and — importantly — OCR'ing columns
    independently instead of parsing one merged string keeps positional
    integrity when a cell says something non-numeric like "BUILDING",
    which would otherwise silently shift every later column out of place).
    Any failure here degrades gracefully — this is an enrichment on top
    of the text-based extraction, not a replacement.
    """
    if not OCR_AVAILABLE:
        return []
    try:
        img = Image.open(BytesIO(image_bytes)).convert("RGB")
    except Exception:
        return []

    w, h = img.size
    bands = _detect_row_bands(img)
    results = []

    data_x0, data_x1 = int(w * 0.28), w
    col_w = (data_x1 - data_x0) / 4

    for (y0, y1) in bands:
        label_text = _ocr_crop(img, (0, max(0, y0 - 2), int(w * 0.27), y1 + 2))
        zone = _match_windtable_label(label_text)
        if not zone:
            continue  # unrecognized or non-wind row (river flow, temps, header)

        for i, period in enumerate(WINDTABLE_PERIODS):
            cx0 = int(data_x0 + i * col_w)
            cx1 = int(data_x0 + (i + 1) * col_w)
            cell_text = _ocr_crop(img, (cx0, max(0, y0 - 2), cx1, y1 + 2))
            m = re.search(r"(\d{1,2})-(\d{1,2})", cell_text)
            if not m:
                continue  # non-numeric cell (e.g. "BUILDING", "clearing") — skip, don't shift
            lo, hi = int(m.group(1)), int(m.group(2))
            results.append({"zone": zone, "period": period, "low": lo, "high": hi})

    return results


def fetch_windtable_image_url(soup):
    """Find the wind table image — it sits between the main heading and
    the SHORT-TERM subheading, named windtable-N.png (N changes daily,
    so we find it by position/filename pattern, not a guessed URL)."""
    img_tag = soup.find("img", src=re.compile(r"windtable", re.I))
    if img_tag and img_tag.get("src"):
        return img_tag["src"]
    return None


def scrape_gorge_gym():
    soup = fetch(GORGE_GYM_URL)

    # Wind table image (Dawn Patrol / morning / afternoon / later by zone) —
    # try to OCR it for a richer per-period breakdown than the prose alone
    # gives us. Fully optional: any failure here (network, OCR unavailable,
    # image layout changed) just yields an empty list, no impact on the
    # rest of the scrape.
    windtable_entries = []
    try:
        img_url = fetch_windtable_image_url(soup)
        if img_url and OCR_AVAILABLE:
            resp = requests.get(img_url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            windtable_entries = extract_windtable(resp.content)
    except Exception as e:
        print(f"WARNING: windtable OCR failed: {e}", file=sys.stderr)

    # Anchor to the SHORT-TERM subheading specifically, not the top-level
    # "GORGE WIND FORECAST" heading — a sensor-calibration disclaimer
    # paragraph ("30mph at Rufus is about 23-24mph at Swell...") sits
    # between the two, and was eating slots meant for the real narrative.
    heading = soup.find(
        lambda tag: tag.name and re.match(r"h[1-6]$", tag.name)
        and "SHORT-TERM" in tag.get_text().upper()
    )
    if not heading:  # fallback if she ever drops the SHORT-TERM subheading
        heading = soup.find(
            lambda tag: tag.name in ("h1", "h2", "h3", "h4")
            and "GORGE WIND FORECAST" in tag.get_text().upper()
        )

    paragraphs = []
    if heading:
        for sib in heading.find_all_next():
            if sib.name in ("h1", "h2", "h3", "h4"):
                break
            if sib.name == "p":
                text = clean(sib.get_text(" "))
                if text:
                    paragraphs.append(text)

    raw_text = " ".join(paragraphs[:3])

    # Longer-term outlook lives under its own heading further down the page.
    # Parsing it separately (rather than blending with the short-term text)
    # matters because the short-term section sometimes mentions upcoming
    # day names in passing ("easterlies forecast Saturday and Sunday") —
    # blending them in was corrupting the day-tracker in extract_daily_forecast.
    longer_heading = soup.find(
        lambda tag: tag.name and re.match(r"h[1-6]$", tag.name)
        and "LONGER-TERM" in tag.get_text().upper()
    )
    outlook_paragraphs = []
    if longer_heading:
        for sib in longer_heading.find_all_next():
            if sib.name and re.match(r"h[1-6]$", sib.name):
                break
            if sib.name == "p":
                text = clean(sib.get_text(" "))
                if text:
                    outlook_paragraphs.append(text)
    outlook_text = " ".join(outlook_paragraphs)

    prose_zones = extract_zones(raw_text)
    prose_zone_periods = extract_zone_periods(raw_text)

    # Combine windtable OCR data with prose-based extraction: for zone_periods,
    # merge the two lists (dedupes/aggregates any zone+period overlap). For
    # the All-Day zones list, aggregate each windtable zone's low/high across
    # its 4 periods, then merge with whatever the prose extraction found.
    combined_zone_periods = merge_zone_periods([prose_zone_periods, windtable_entries])

    windtable_alldae = {}
    for e in windtable_entries:
        z = e["zone"]
        if z not in windtable_alldae:
            windtable_alldae[z] = {"zone": z, "low": e["low"], "high": e["high"]}
        else:
            windtable_alldae[z]["low"] = min(windtable_alldae[z]["low"], e["low"])
            windtable_alldae[z]["high"] = max(windtable_alldae[z]["high"], e["high"])
    combined_zones = merge_zones([prose_zones, list(windtable_alldae.values())])

    return {
        "source": "The Gorge Is My Gym (Temira)",
        "url": GORGE_GYM_URL,
        "credibility": CREDIBILITY["The Gorge Is My Gym (Temira)"],
        "ai_summary": ai_summarize(raw_text),
        "zones": combined_zones,
        "flags": extract_flags(raw_text),
        "mph_mentions": extract_mph_mentions(raw_text),
        "time_segments": extract_time_segments(raw_text),
        "zone_periods": combined_zone_periods,
        "_mentions": extract_zone_mentions(raw_text),
        "_outlook_text": outlook_text,  # used only for the weekly outlook, stripped before writing
    }


def merge_zones(all_zone_lists):
    """Combine zone data across both sources into one chart-ready list."""
    merged = {}
    for zones in all_zone_lists:
        for z in zones:
            key = z["zone"].lower()
            if key not in merged:
                merged[key] = dict(z)
            else:
                merged[key]["low"] = min(merged[key]["low"], z["low"])
                merged[key]["high"] = max(merged[key]["high"], z["high"])

    def sort_key(item):
        key = item[0]
        for i, z in enumerate(ZONE_ORDER):
            if z in key:
                return i
        return len(ZONE_ORDER)

    return [v for _, v in sorted(merged.items(), key=sort_key)]


PERIOD_ORDER = ["Dawn", "Early", "Morning", "Afternoon", "Evening"]


def merge_time_segments(all_segment_lists):
    """
    Combine each source's time-of-day breakdown into one gorge-wide view
    per period. This is intentionally not tied to a specific zone — it's
    a "how strong is the wind right now, generally" signal that
    complements (not replaces) the per-zone Wind by Zone data.
    """
    merged = {}
    for segments in all_segment_lists:
        for s in segments:
            key = s["period"]
            if key not in merged:
                merged[key] = {"period": key, "low": s["low"], "high": s["high"]}
            else:
                merged[key]["low"] = min(merged[key]["low"], s["low"])
                merged[key]["high"] = max(merged[key]["high"], s["high"])

    return [merged[p] for p in PERIOD_ORDER if p in merged]


# Same 8 locations the dashboard's Live Model Data section already knows
# about — kept in sync manually with the LIVE_LOCATIONS array in index.html.
# Coordinates target the Columbia River waterway itself (the actual wind
# corridor), not town centers. Swell City is anchored against a verified
# coordinate for the Hood River Event Site (45.7153, -121.5178 — Wikipedia)
# shifted ~3mi west per its known description as "a few miles west of Hood
# River." The rest are best-effort adjustments toward the river channel
# based on known geography, not individually verified per-spot. Kept in
# sync with the matching array in index.html.
LIVE_LOCATIONS = [
    {"name": "Swell City, OR", "lat": 45.7123, "lon": -121.5799},
    {"name": "Stevenson, WA", "lat": 45.6940, "lon": -121.8934},
    {"name": "Viento, OR", "lat": 45.7020, "lon": -121.6660},
    {"name": "Mosier, OR", "lat": 45.6880, "lon": -121.3971},
    {"name": "The Dalles, OR", "lat": 45.6120, "lon": -121.1800},
    {"name": "Lyle, WA", "lat": 45.6940, "lon": -121.2860},
    {"name": "Rufus, OR", "lat": 45.6750, "lon": -120.7260},
    {"name": "Arlington, OR", "lat": 45.7170, "lon": -120.2110},
]


def fetch_weekly_location_forecast():
    """
    Fetches 7 days of hourly wind speed for every known location from the
    free, no-key Open-Meteo API — used to build the 7-Day Outlook's future
    days. Victor/Temira's prose only ever gives one blended range per day
    with no zone attribution for anything beyond today, so this gives real
    per-spot numbers instead. Moved server-side (once/day) rather than
    fetched fresh by every browser session — the same 8-location fetch was
    previously happening client-side on every page load.
    """
    location_data = {}
    for loc in LIVE_LOCATIONS:
        try:
            url = (
                f"https://api.open-meteo.com/v1/gfs?latitude={loc['lat']}&longitude={loc['lon']}"
                f"&hourly=wind_speed_10m&wind_speed_unit=kn&timezone=auto&forecast_days=7"
            )
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
            location_data[loc["name"]] = resp.json().get("hourly")
        except Exception as e:
            print(f"WARNING: weekly forecast fetch failed for {loc['name']}: {e}", file=sys.stderr)
            location_data[loc["name"]] = None
    return location_data


def aggregate_weekly_forecast(location_data):
    """
    Collapses per-location hourly data into one entry per calendar date:
    each location's own low/high for that day, a weighted "typical" value
    (average of each location's own average, then averaged across
    locations — not just the raw span's high, which one outlier location
    could skew), and the overall low/high span across all locations.
    """
    by_date = {}
    for loc_name, hourly in location_data.items():
        if not hourly or "time" not in hourly:
            continue
        for t, speed in zip(hourly["time"], hourly.get("wind_speed_10m", [])):
            if speed is None:
                continue
            date_str = t[:10]
            by_date.setdefault(date_str, {})
            if loc_name not in by_date[date_str]:
                by_date[date_str][loc_name] = {"low": speed, "high": speed}
            else:
                by_date[date_str][loc_name]["low"] = min(by_date[date_str][loc_name]["low"], speed)
                by_date[date_str][loc_name]["high"] = max(by_date[date_str][loc_name]["high"], speed)

    result = {}
    for date_str, per_loc in by_date.items():
        zones = [
            {"zone": name.split(",")[0], "low": round(vals["low"]), "high": round(vals["high"])}
            for name, vals in per_loc.items()
        ]
        if not zones:
            continue
        loc_avgs = [(z["low"] + z["high"]) / 2 for z in zones]
        result[date_str] = {
            "low": min(z["low"] for z in zones),
            "high": max(z["high"] for z in zones),
            "avgKn": round(sum(loc_avgs) / len(loc_avgs), 1),
            "zones": zones,
        }
    return result


def main():
    result = {"generated_at": datetime.now(timezone.utc).isoformat(), "sources": []}

    for scraper_fn, name in [(scrape_victor, "Victor"), (scrape_gorge_gym, "Gorge Is My Gym")]:
        try:
            result["sources"].append(scraper_fn())
        except Exception as e:
            print(f"WARNING: failed to scrape {name}: {e}", file=sys.stderr)
            result["sources"].append({"source": name, "error": str(e)})

    result["zones"] = merge_zones([s.get("zones", []) for s in result["sources"] if "zones" in s])
    result["zone_periods"] = merge_zone_periods([s.get("zone_periods", []) for s in result["sources"] if "zone_periods" in s])
    result["time_segments"] = merge_time_segments([s.get("time_segments", []) for s in result["sources"] if "time_segments" in s])
    result["flags"] = list({f["label"]: f for s in result["sources"] for f in s.get("flags", [])}.values())

    # Cross-source agreement: which named spots do BOTH forecasters mention
    # today, with or without an attached number?
    mention_sets = [s.get("_mentions", set()) for s in result["sources"] if "_mentions" in s]
    agreement = sorted(set.intersection(*mention_sets)) if len(mention_sets) >= 2 else []
    result["agreement_zones"] = agreement

    agreement_lower = {a.lower() for a in agreement}
    for z in result["zones"]:
        z["confirmed_by_both"] = z["zone"].lower() in agreement_lower

    # Overall prevailing direction, checked across both sources
    combined_text = " ".join(s.get("forecast_text", "") for s in result["sources"])
    result["direction"] = extract_direction(combined_text) or "westerly"  # Gorge defaults westerly most of the season

    # Weekly outlook, parsed from Temira's longer-term section (Victor's site
    # has no equivalent multi-day breakdown to draw from)
    gym_outlook_text = next((s.get("_outlook_text", "") for s in result["sources"] if "_outlook_text" in s), "")
    daily = parse_daily_mentions(gym_outlook_text)
    today_lows = [z["low"] for z in result["zones"]]
    today_highs = [z["high"] for z in result["zones"]]
    today_low = min(today_lows) if today_lows else None
    today_high = max(today_highs) if today_highs else None
    result["weekly_outlook"] = build_weekly_outlook(daily, today_low, today_high, result["direction"])

    # 7-Day Outlook's future days: real per-location Open-Meteo data. Today's
    # own zone breakdown already comes from the scrape above (result["zones"]);
    # this only fills in days 1-6, which prose alone can't give per-spot detail
    # for. Wrapped defensively — any failure just yields an empty array, and
    # the dashboard already handles missing future-day data gracefully.
    try:
        location_data = fetch_weekly_location_forecast()
        agg_by_date = aggregate_weekly_forecast(location_data)
        today_date = datetime.now(PACIFIC).date()
        result["weekly_location_forecast"] = [
            agg_by_date.get((today_date + timedelta(days=i)).isoformat())
            for i in range(7)
        ]
    except Exception as e:
        print(f"WARNING: weekly location forecast failed: {e}", file=sys.stderr)
        result["weekly_location_forecast"] = []

    # strip internal-only fields before writing
    for s in result["sources"]:
        s.pop("_mentions", None)
        s.pop("_outlook_text", None)

    with open("summary.json", "w") as f:
        json.dump(result, f, indent=2)
    print("Wrote summary.json")


if __name__ == "__main__":
    main()
