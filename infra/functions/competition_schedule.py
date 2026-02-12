"""
Competition Schedule module - parses CompetitionSchedule PDF exports from
Figure Skating Manager.

The CompetitionSchedule PDF contains dates, start/finish times, entry counts,
and full event names (category + segment) for each segment in a competition.

Layout per schedule block:
    dd.mm.yyyy                          (date line)
    HH:MM:SS  HH:MM:SS  N  EventName   (segment lines, one per event)
    ...
    (next date line or end of page)
"""

import re
import logging
from pypdf import PdfReader


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
            "event_name": str,        # full event text (category + segment)
            "category_name": str,     # matched category display name or ""
            "segment_name": str,      # remainder after stripping category, or full event_name
        }

    Args:
        pdf_path_or_stream: file path (str) or file-like object
        categories: optional list of category dicts from load_categories()
                    (used to split event_name into category + segment)
    """
    try:
        reader = PdfReader(pdf_path_or_stream)
    except Exception as e:
        logging.error(f"Error reading CompetitionSchedule: {e}")
        return []

    entries = []
    current_date = None          # YYYYMMDD
    current_date_display = None  # dd.mm.yyyy

    # Patterns
    # Date line: dd.mm.yyyy (possibly with surrounding text/whitespace)
    date_pattern = re.compile(r'\b(\d{2})\.(\d{2})\.(\d{4})\b')
    # Segment line: start_time  end_time  count  category_name  segment_name
    # The category and segment are in separate columns in the PDF.
    # With layout extraction, they are separated by 2+ spaces.
    # We first try the two-column pattern (category + segment separated by
    # multiple spaces), then fall back to a single event_name blob.
    segment_pattern_two_col = re.compile(
        r'(\d{1,2}:\d{2}(?::\d{2})?)\s+'     # start time
        r'(\d{1,2}:\d{2}(?::\d{2})?)\s+'     # end time
        r'(\d+)\s{2,}'                         # number of entries + wide gap
        r'(.+?)\s{2,}'                         # category name (up to wide gap)
        r'(\S.+)'                              # segment name (rest of line)
    )
    segment_pattern = re.compile(
        r'(\d{1,2}:\d{2}(?::\d{2})?)\s+'   # start time
        r'(\d{1,2}:\d{2}(?::\d{2})?)\s+'   # end time
        r'(\d+)\s+'                          # number of entries
        r'(.+)'                              # event name (rest of line)
    )

    for page in reader.pages:
        try:
            text = page.extract_text(extraction_mode="layout")
        except Exception:
            text = page.extract_text()

        if not text:
            continue

        lines = text.split('\n')
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue

            # Try to match a date line
            date_match = date_pattern.search(stripped)
            # A date line is one that is predominantly a date (short line)
            # or starts with a day-of-week name, or is just the date.
            # We detect by checking if the line contains a date AND does NOT
            # also match a segment pattern (to avoid false positives).
            seg_match = segment_pattern.search(stripped) or segment_pattern_two_col.search(stripped)

            if date_match and not seg_match:
                day, month, year = date_match.groups()
                current_date = f"{year}{month}{day}"
                current_date_display = f"{day}.{month}.{year}"
                continue

            if seg_match and current_date:
                # Try two-column pattern first (Category and Segment in separate columns)
                two_col = segment_pattern_two_col.search(stripped)
                if two_col:
                    start_raw, end_raw, count_str, cat_name_raw, seg_name_raw = two_col.groups()
                    start_time = _normalize_time(start_raw)
                    end_time = _normalize_time(end_raw)
                    cat_name_raw = cat_name_raw.strip()
                    seg_name_raw = seg_name_raw.strip()
                    event_name = f"{cat_name_raw} {seg_name_raw}"
                    entries.append({
                        "date": current_date,
                        "date_display": current_date_display,
                        "start_time": start_time,
                        "end_time": end_time,
                        "entries": int(count_str),
                        "event_name": event_name,
                        "category_name": cat_name_raw,
                        "segment_name": seg_name_raw,
                    })
                else:
                    start_raw, end_raw, count_str, event_name = seg_match.groups()
                    start_time = _normalize_time(start_raw)
                    end_time = _normalize_time(end_raw)
                    event_name = event_name.strip()
                    cat_name, seg_name = _split_event_name(event_name, categories)
                    entries.append({
                        "date": current_date,
                        "date_display": current_date_display,
                        "start_time": start_time,
                        "end_time": end_time,
                        "entries": int(count_str),
                        "event_name": event_name,
                        "category_name": cat_name,
                        "segment_name": seg_name,
                    })

    logging.info(f"Parsed {len(entries)} schedule entries from CompetitionSchedule")
    return entries


def _normalize_time(time_str):
    """Convert HH:MM:SS or H:MM to HH:MM."""
    parts = time_str.split(':')
    hh = parts[0].zfill(2)
    mm = parts[1] if len(parts) > 1 else '00'
    return f"{hh}:{mm}"


def _split_event_name(event_name, categories):
    """
    Split a full event name like "Advanced Novice Ice Dance PDK1 (Starlight Waltz)"
    into category_name="Advanced Novice Ice Dance" and segment_name="PDK1 (Starlight Waltz)".

    Uses the categories table displayName values for longest-match.
    Falls back to returning ("", event_name) if no match found.
    """
    if not categories:
        return "", event_name

    # Build a list of (displayName, length) sorted by length descending
    # for longest-match-first strategy
    display_names = []
    for cat in categories:
        dn = cat.get("displayName", "")
        if dn:
            display_names.append(dn)

    # Sort by length descending for longest match
    display_names.sort(key=len, reverse=True)

    event_upper = event_name.upper()
    for dn in display_names:
        dn_upper = dn.upper()
        if event_upper.startswith(dn_upper):
            remainder = event_name[len(dn):].strip()
            return dn, remainder if remainder else event_name
        # Also try: displayName may be a partial match mid-string
        # e.g. "A-Silmut, Tytöt" vs schedule text "A-Silmut Tytöt"
        # Try a normalized comparison (remove commas, extra spaces)
        dn_norm = _normalize_for_match(dn_upper)
        ev_norm = _normalize_for_match(event_upper)
        if ev_norm.startswith(dn_norm) and len(dn_norm) > 3:
            remainder = event_name[len(dn):].strip()
            # Try to find the actual split point by scanning past the matched category chars
            # Account for possible punctuation differences
            split_pos = _find_split_position(event_name, dn)
            if split_pos > 0:
                remainder = event_name[split_pos:].strip()
            return dn, remainder if remainder else event_name

    return "", event_name


def _normalize_for_match(text):
    """Normalize text for fuzzy matching: lowercase, remove punctuation, collapse spaces."""
    text = text.upper()
    text = re.sub(r'[,.\-/]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _find_split_position(event_name, display_name):
    """
    Find where in event_name the display_name portion ends,
    accounting for minor punctuation differences.
    """
    en_norm = _normalize_for_match(event_name)
    dn_norm = _normalize_for_match(display_name)

    if not en_norm.startswith(dn_norm):
        return 0

    # Map normalized position back to original string
    # Walk through original string counting "significant" characters
    norm_idx = 0
    for i, ch in enumerate(event_name):
        norm_ch = ch.upper()
        if norm_ch in ',./-':
            continue
        if ch == ' ' and norm_idx > 0:
            # Check if we consumed all of dn_norm
            pass
        if norm_idx >= len(dn_norm):
            return i
        norm_idx += 1

    return len(event_name)


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
