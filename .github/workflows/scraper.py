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
import re
import sys
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; GorgeDashboardBot/1.0; personal use)"
}

VTI_URL = "https://victortheinflictor.com"
GORGE_GYM_URL = "https://thegorgeismygym.com/forecast"

# Geographic order, west to east, used to sort the zone chart the same way
# the wind actually moves through the corridor.
ZONE_ORDER = [
    "coast", "astoria", "corridor", "cascade locks", "stevenson", "wyeth",
    "viento", "hood river", "the hatch", "hatch", "hatchery", "mosier",
    "rowena", "the dalles", "doug's", "dougs", "lyle", "the wall", "wall",
    "swell", "celilo", "rufus", "arlington", "roosevelt", "pasco", "desert",
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
    today_idx = datetime.now(timezone.utc).weekday()  # Mon=0..Sun=6
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
    normalized = normalize_case(raw_text) if raw_text else "Forecast text not found — site layout may have changed."
    # Headline: use the real forecast's own first sentence rather than the
    # static <title> tag (which is just his site name every day, not
    # today's actual headline, and isn't reliably locatable in the DOM).
    headline = normalized.split(".")[0].strip() if raw_text else ""

    return {
        "source": "Victor the Inflictor",
        "url": VTI_URL,
        "headline": headline,
        "forecast_text": normalized,
        "credibility": CREDIBILITY["Victor the Inflictor"],
        "zones": extract_zones(raw_text),
        "flags": extract_flags(raw_text),
        "mph_mentions": extract_mph_mentions(raw_text),
        "time_segments": extract_time_segments(raw_text),
        "zone_periods": extract_zone_periods(raw_text),
        "_mentions": extract_zone_mentions(raw_text),  # used for cross-source agreement, stripped before writing
    }


def scrape_gorge_gym():
    soup = fetch(GORGE_GYM_URL)

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

    # Headline: the actual post title. Her theme uses <h1> for BOTH the
    # site logo ("The Gorge Is My Gym") and the real post title, so we
    # skip any short h1 that's just the site name and take the first
    # substantial one.
    headline_raw = ""
    for h in soup.find_all("h1"):
        txt = clean(h.get_text(" "))
        if len(txt) > 20 and "the gorge is my gym" not in txt.lower():
            headline_raw = txt
            break

    return {
        "source": "The Gorge Is My Gym (Temira)",
        "url": GORGE_GYM_URL,
        "headline": headline_raw,
        "forecast_text": raw_text or "Forecast text not found — site layout may have changed.",
        "credibility": CREDIBILITY["The Gorge Is My Gym (Temira)"],
        "zones": extract_zones(raw_text),
        "flags": extract_flags(raw_text),
        "mph_mentions": extract_mph_mentions(raw_text),
        "time_segments": extract_time_segments(raw_text),
        "zone_periods": extract_zone_periods(raw_text),
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

    # strip internal-only fields before writing
    for s in result["sources"]:
        s.pop("_mentions", None)
        s.pop("_outlook_text", None)

    with open("summary.json", "w") as f:
        json.dump(result, f, indent=2)
    print("Wrote summary.json")


if __name__ == "__main__":
    main()
