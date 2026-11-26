"""
Schedule check (issue #84): which segments of the uploaded CompetitionSchedule
have papers among the uploads.

FSM prints a segment's category and segment the same way on the schedule, as
the title of its StartListwithTimes and on line 2 of its JudgesSheetAll; only
case and spacing differ ('SM-JUNIORI Miehet  Short Program' on the schedule,
'SM-JUNIORI MIEHET SHORT PROGRAM' on the start list). A schedule row is
therefore matched to an uploaded segment by that text alone, never by the
categories table or by a guess.

Pure functions: no storage, no PDF reading. get_competition_details feeds
them what it has already read.
"""

import re

try:
    from .categories import _folded
except ImportError:
    from categories import _folded


# A split group's mark at the end of a category name ('SM-Noviisit #2'); the
# details structure adds one that FSM did not print.
_TRAILING_SPLIT_MARK_RE = re.compile(r"\s*#\s*\d+$")


def start_list_title(page_text):
    """
    The category + segment line of a StartListwithTimes page, as printed: the
    line after the 'TIME SCHEDULE' heading. None when the page has no heading.
    """
    lines = [line.strip() for line in (page_text or "").splitlines() if line.strip()]
    for i, line in enumerate(lines[:-1]):
        if _folded(line) == "timeschedule":
            return lines[i + 1]
    return None


def _iso_date(yyyymmdd):
    text = yyyymmdd or ""
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}" if len(text) == 8 and text.isdigit() else None


def check_schedule(rows, segments, category_keys=()):
    """
    One result per schedule row, in the schedule's order.

    rows: entries of competition_schedule.parse_competition_schedule.
    segments: the uploaded segments that carry a printed title, one per FSM
        file prefix: {"prefix", "categoryKey", "titles": [str, ...], "date"}.
        titles = the StartListwithTimes title and/or line 2 of the
        JudgesSheetAll; date = the start list's date 'YYYYMMDD' or None;
        categoryKey = the key of the segment's category in the details
        `structure`.
    category_keys: every category key of that structure.

    Result: {date: 'YYYY-MM-DD', startTime: 'HH:MM', entries, name, category,
    segment, status, prefix, categoryKey}.

    status: 'found' when an uploaded segment has the row's title (compared as
    letters and digits only, casefolded), else 'notNeeded' for a row with no
    entries (FSM exports nothing worth printing for it), else 'missing'. One
    segment covers one row; among rows with the same title, a segment goes to
    the row of its own date first.

    categoryKey: the category card the row belongs to. For a found row it is
    the matched segment's; for any other row, the card a found row of the same
    printed category has, else a structure key with the same name (with or
    without its '#N'), else None (no card: nothing of the category is
    uploaded under a name the schedule knows).
    """
    by_title = {}
    for seg in segments:
        for key in {_folded(title) for title in seg.get("titles") or () if title}:
            if key:
                by_title.setdefault(key, []).append(seg)

    matched = [None] * len(rows)
    used = set()

    def claim(index, same_date_only):
        row = rows[index]
        for seg in by_title.get(_folded(row.get("event_name")), ()):
            if seg["prefix"] in used:
                continue
            if same_date_only and seg.get("date") != row.get("date"):
                continue
            used.add(seg["prefix"])
            matched[index] = seg
            return

    # Same-day matches first, so a segment uploaded for day 2 is never spent
    # on day 1's row of the same title.
    for same_date_only in (True, False):
        for index in range(len(rows)):
            if matched[index] is None:
                claim(index, same_date_only)

    cards = {}
    for row, seg in zip(rows, matched):
        category = _folded(row.get("category_name"))
        if seg and seg.get("categoryKey") and category:
            cards.setdefault(category, seg["categoryKey"])
    # A card's own name first. A split group's bare name ('JUNIORIT' for
    # 'JUNIORIT #1') stands in only where no card is called exactly that, and
    # then for the lowest group, whatever order the cards come in.
    keys_by_name = {}
    for key in category_keys:
        if _folded(key):
            keys_by_name.setdefault(_folded(key), key)
    for key in sorted(k for k in category_keys if k):
        bare = _folded(_TRAILING_SPLIT_MARK_RE.sub("", key))
        if bare:
            keys_by_name.setdefault(bare, key)

    result = []
    for row, seg in zip(rows, matched):
        category = _folded(row.get("category_name"))
        if seg:
            status = "found"
            card = seg.get("categoryKey")
        else:
            status = "missing" if row.get("entries") else "notNeeded"
            card = None
        if card is None and category:
            card = cards.get(category) or keys_by_name.get(category)
        result.append({
            "date": _iso_date(row.get("date")),
            "startTime": row.get("start_time"),
            "entries": row.get("entries", 0),
            "name": row.get("event_name", ""),
            "category": row.get("category_name", ""),
            "segment": row.get("segment_name", ""),
            "status": status,
            "prefix": seg["prefix"] if seg else None,
            "categoryKey": card,
        })
    return result
