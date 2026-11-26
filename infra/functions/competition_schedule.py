"""
Competition Schedule module - parses CompetitionSchedule PDF exports from
Figure Skating Manager.

The CompetitionSchedule PDF contains dates, start/finish times, entry counts,
and full event names (category + segment) for each segment in a competition.

Layout per schedule block:
    dd.mm.yyyy                                      (date line)
    start  finish  N  Category  Segment  durations  (segment lines, one per event)
    ...
    (next date line or end of page)

Times are printed as the exporting PC's locale has them: 14:00:00 or 16.00.00.
"""

import re
import logging
from pypdf import PdfReader

try:
    from .categories import _SPLIT_MARK_RE, _folded, _name_end
    from .combined_schedule import split_title
except ImportError:
    from categories import _SPLIT_MARK_RE, _folded, _name_end
    from combined_schedule import split_title


# A clock time as FSM prints it, seconds optional: 14:00:00, 16.00.00, 9.30
_TIME = r'\d{1,2}[.:]\d{2}(?:[.:]\d{2})?'
# A segment line, from its first character: start, finish, number of entries,
# then the event text. Matched at the start of the line only, so the duration
# columns further right are never taken for the start time.
_ROW_RE = re.compile(rf'({_TIME})\s+({_TIME})\s+(\d+)\s+(\S.*)')
# The columns after the segment (Resurfacing, Performance, Judging First /
# Last): durations, always with seconds.
_TRAILING_DURATIONS_RE = re.compile(r'(?:\s+\d{1,2}[.:]\d{2}[.:]\d{2})+\s*$')
_DATE_RE = re.compile(r'\b(\d{2})\.(\d{2})\.(\d{4})\b')


def parse_competition_schedule(pdf_path_or_stream, categories=None):
    """
    Parses a CompetitionSchedule PDF and returns a list of schedule entry dicts.

    Each entry:
        {
            "date": "YYYYMMDD",
            "date_display": "dd.mm.yyyy",
            "start_time": "HH:MM",
            "end_time": "HH:MM",
            "entries": int,
            "event_name": str,        # category + segment as printed, single spaces
            "category_name": str,     # as printed, or "" when the text can't be split
            "segment_name": str,      # as printed, or the whole event_name
        }

    Args:
        pdf_path_or_stream: file path (str) or file-like object
        categories: optional list of category dicts from load_categories()
                    (the last resort for splitting event_name)

    Returns [] when the PDF can't be read.
    """
    try:
        reader = PdfReader(pdf_path_or_stream)
    except Exception as e:
        logging.error(f"Error reading CompetitionSchedule: {type(e).__name__}")
        return []

    entries = []
    current_date = None          # YYYYMMDD
    current_date_display = None  # dd.mm.yyyy

    for page in reader.pages:
        try:
            text = page.extract_text(extraction_mode="layout")
        except Exception:
            text = page.extract_text()

        if not text:
            continue

        for line in text.split('\n'):
            stripped = line.strip()
            if not stripped:
                continue

            row = _ROW_RE.match(stripped)
            if not row:
                # A date line starts a day. The 'printed: dd.mm.yyyy hh.mm'
                # stamp is a date too, but not a competition day.
                date_match = _DATE_RE.search(stripped)
                if date_match and not stripped.casefold().startswith("printed"):
                    day, month, year = date_match.groups()
                    current_date = f"{year}{month}{day}"
                    current_date_display = f"{day}.{month}.{year}"
                continue

            if not current_date:
                continue

            start_raw, end_raw, count_str, event = row.groups()
            event = _TRAILING_DURATIONS_RE.sub('', event).strip()
            if not event:
                continue
            category_name, segment_name = _split_event(event, categories)
            entries.append({
                "date": current_date,
                "date_display": current_date_display,
                "start_time": _normalize_time(start_raw),
                "end_time": _normalize_time(end_raw),
                "entries": int(count_str),
                "event_name": _single_spaced(event),
                "category_name": category_name,
                "segment_name": segment_name,
            })

    logging.info(f"Parsed {len(entries)} schedule entries from CompetitionSchedule")
    return entries


def _single_spaced(text):
    return ' '.join(text.split())


def _split_event(event, categories):
    """
    (category, segment) of a schedule row's event text, both as printed. Layout
    extraction keeps the two columns 2+ spaces apart; a row that came out as
    one string is split before a known segment name (split_title), and only
    then against the categories table. ("", whole text) when nothing fits.
    """
    parts = re.split(r'\s{2,}', event, maxsplit=1)
    if len(parts) == 2:
        return _single_spaced(parts[0]), _single_spaced(parts[1])
    event = _single_spaced(event)
    category, segment = split_title(event)
    if segment:
        return category, segment
    return _split_event_name(event, categories)


def _normalize_time(time_str):
    """Convert HH:MM:SS, H.MM.SS or H:MM to HH:MM."""
    parts = re.split(r'[.:]', time_str)
    hh = parts[0].zfill(2)
    mm = parts[1] if len(parts) > 1 else '00'
    return f"{hh}:{mm}"


def _split_event_name(event_name, categories):
    """
    Last resort for a row that came out as one string and does not end in a
    known segment name: split it after the longest categories-table name it
    starts with. Both of a row's names count (`displayName`, `displayNameFi`),
    since the schedule prints the competition's own language, and they are
    compared like line 2 of the judges' sheets (categories._name_end: case,
    spacing and punctuation ignored, on a word boundary). The table only
    locates the split: both parts are returned as the schedule prints them, a
    split group's `#N` included. ("", event_name) when no name fits or
    nothing follows it.
    """
    names = {name for cat in categories or []
             for name in (cat.get("displayName"), cat.get("displayNameFi")) if name}
    for name in sorted(names, key=lambda n: (-len(_folded(n)), n)):
        end = _name_end(event_name, name)
        if end is None:
            continue
        mark = _SPLIT_MARK_RE.match(event_name, end)
        if mark:
            end = mark.end()
        category = event_name[:end].strip(" ,-/")
        segment = event_name[end:].strip(" ,-/")
        if category and segment:
            return category, segment
    return "", event_name


def _normalize_for_match(text):
    """Normalize text for fuzzy matching: lowercase, remove punctuation, collapse spaces."""
    text = text.upper()
    text = re.sub(r'[,.\-/]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def get_schedule_start_time(schedule_entries, category_name, segment_name, date_str=None):
    """
    Look up the start time for a given category + segment combination.

    Args:
        schedule_entries: list from parse_competition_schedule()
        category_name: category display name (from categories table)
        segment_name: segment name (from JudgesSheetAll line 2 or schedule)
        date_str: optional YYYYMMDD date to narrow the search

    Returns:
        start_time string "HH:MM" or None if not found
    """
    if not schedule_entries or not segment_name:
        return None

    seg_norm = _normalize_for_match(segment_name)
    # Also strip parenthesised content for looser matching
    seg_base = re.sub(r'\(.*?\)', '', segment_name).strip()
    seg_base_norm = _normalize_for_match(seg_base) if seg_base else seg_norm

    for entry in schedule_entries:
        if date_str and entry["date"] != date_str:
            continue

        entry_seg_norm = _normalize_for_match(entry["segment_name"])
        entry_event_norm = _normalize_for_match(entry["event_name"])

        # Direct segment name match
        if seg_norm and entry_seg_norm and seg_norm == entry_seg_norm:
            return entry["start_time"]

        # Segment name contained in event name
        if seg_norm and seg_norm in entry_event_norm:
            return entry["start_time"]

        # Match base segment name (without parentheses) against schedule segment
        # e.g. "PDK1" matches schedule "PDK1" even if JudgesSheet says "PDK1 (STARLIGHT WALTZ)"
        entry_seg_base = re.sub(r'\(.*?\)', '', entry.get("segment_name", "")).strip()
        entry_seg_base_norm = _normalize_for_match(entry_seg_base) if entry_seg_base else ""
        if seg_base_norm and entry_seg_base_norm and seg_base_norm == entry_seg_base_norm:
            # If base names match, also verify the category matches (to avoid
            # PDK1 of Intermediate matching PDK1 of Advanced)
            if category_name:
                cat_norm = _normalize_for_match(category_name)
                # Check if category appears in the event name
                if cat_norm in entry_event_norm or entry_event_norm in cat_norm:
                    return entry["start_time"]
                # Also try: schedule category_name field (from _split_event_name)
                sched_cat_norm = _normalize_for_match(entry.get("category_name", ""))
                if sched_cat_norm and (cat_norm in sched_cat_norm or sched_cat_norm in cat_norm):
                    return entry["start_time"]
            else:
                return entry["start_time"]

    # Broader fallback: try matching category + segment against full event_name
    if category_name:
        combined_norm = _normalize_for_match(f"{category_name} {segment_name}")
        combined_base_norm = _normalize_for_match(f"{category_name} {seg_base}")
        for entry in schedule_entries:
            if date_str and entry["date"] != date_str:
                continue
            entry_event_norm = _normalize_for_match(entry["event_name"])
            if combined_norm == entry_event_norm or combined_base_norm == entry_event_norm:
                return entry["start_time"]

    return None


def get_schedule_date(schedule_entries, category_name, segment_name):
    """
    Look up the date (YYYYMMDD) for a given category + segment.
    Useful as a fallback when StartListwithTimes is not available.

    Returns date string "YYYYMMDD" or None.
    """
    if not schedule_entries or not segment_name:
        return None

    seg_norm = _normalize_for_match(segment_name)

    for entry in schedule_entries:
        entry_seg_norm = _normalize_for_match(entry["segment_name"])
        entry_event_norm = _normalize_for_match(entry["event_name"])

        if seg_norm and entry_seg_norm and seg_norm == entry_seg_norm:
            return entry["date"]

        if seg_norm and seg_norm in entry_event_norm:
            return entry["date"]

    return None
