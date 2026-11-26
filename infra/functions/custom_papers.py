"""
Custom generate (issue #85): print a chosen part of the judging papers.

The flow is generate -> pick -> cut. A generate run writes, next to its
output, a *papers index*: per day the people of the day document, and for
each of them the parts of their packet with page counts (the cover, then one
part per task: the page before the segment + that role's papers). The site
draws its Day -> Person -> Category -> Segment tree from that index, and the
selection it sends back is cut out of the generated packets page by page.
Nothing is generated again, so a custom PDF holds exactly the pages of the
full papers: the cover lists the person's whole day, a referee's calculation
setup sits where the full run put it, withdrawn skaters and trainee stamps
are the full run's.

This module holds the index's shape and fingerprints, the validation of a
selection against an index, the cut and the contents page. It knows nothing
about storage: function_app moves the blobs.
"""
import hashlib
import io
import json
import re
import zipfile
from datetime import datetime
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

try:
    from .create_cover_pages import FOOTER_TEXT, TABLE_FONT, TABLE_FONT_BOLD
except ImportError:
    from create_cover_pages import FOOTER_TEXT, TABLE_FONT, TABLE_FONT_BOLD

# Bump when the index's shape changes: an index of another version is no index
INDEX_VERSION = 1
# Under the record's judgePapers/ folder. Its blobs are no generated files:
# never listed, linked or deletable by a client (_record_file_name allows
# judgePapers/<name> only), and they go with the competition's folder.
INTERNAL_FOLDER = "_internal"
INDEX_FILE = "papers-index.json"
SELECTION_FILE = "custom-selection.json"
COUNTER_FILE = "custom-counter.json"   # {"last": N}: the highest custom run number ever given
# A selection names every ticked part: a three-day competition is ~50 KB
MAX_REQUEST_BYTES = 512 * 1024

_DATE_RE = re.compile(r"\d{8}")
# The files an index names are opened by these names (blobs next to the index,
# members of the day ZIP): plain generated-file names only, never a path
_FILE_RE = re.compile(r"[A-Za-z0-9_-]+\.(?:pdf|zip)")
_HELSINKI = ZoneInfo("Europe/Helsinki")


class SelectionError(ValueError):
    """A selection that doesn't fit the index. The message names the rule,
    never a person, slug or file prefix."""


class PapersChanged(Exception):
    """The generated papers are not the ones the index describes."""


# --- The index ---------------------------------------------------------------

def build_index(days, segments, options):
    """
    The papers index of one generate run (processor.write_papers_index):

      {version, options, segments: {prefix: {date, start, end, category, segment}},
       days: [{date, title, file, pages, zip: {file, size, sha256},
               schedule: {file, pages, copies, size, sha256} | None,
               people: [{slug, name, file, pages, cover,
                         parts: [{prefix, role, label, pages, intro, flags}]}]}]}

    Days in date order, people in the day document's order, parts in packet
    order. `file` of a person is their packet's name inside the day ZIP;
    cover + the parts' pages = the packet's pages, and the packets + the
    schedule's copies = the day document's pages. `options` are the
    processor's effective_options, hashed into the fingerprint.
    function_app adds `generation`, `generatedUtc`, `inputs` and, to each
    zip and schedule, the `etag` its upload answered: the index describes
    exactly those blob versions (a hash for the bytes, an ETag for the blob).
    """
    return {"version": INDEX_VERSION, "options": options, "segments": segments, "days": days}


def canonical(value):
    """One JSON text per value: sorted keys, no whitespace."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    """sha256 hex of a text, or of a value's canonical JSON."""
    text = value if isinstance(value, str) else canonical(value)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def files_fingerprint(blobs):
    """
    One hash for the uploaded files a run reads. blobs: (record-relative
    name, etag) of each; any upload, re-import or delete changes it.
    """
    return digest("\n".join(f"{name}\t{etag}" for name, etag in sorted(blobs)))


def _is_count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_file_name(value):
    return isinstance(value, str) and _FILE_RE.fullmatch(value) is not None


def _is_text_or_none(value):
    return value is None or isinstance(value, str)


_SEGMENT_KEYS = ("date", "start", "end", "category", "segment")


def _is_segment(value):
    return isinstance(value, dict) and all(_is_text_or_none(value.get(key)) for key in _SEGMENT_KEYS)


def _is_part(part, segments):
    return (isinstance(part, dict)
            and isinstance(part.get("prefix"), str) and part["prefix"] in segments
            and isinstance(part.get("role"), str) and isinstance(part.get("label"), str)
            and _is_count(part.get("pages")) and _is_count(part.get("intro"))
            and isinstance(part.get("flags"), list) and all(isinstance(flag, str) for flag in part["flags"]))


def _is_person(person, segments):
    return (isinstance(person, dict) and isinstance(person.get("slug"), str)
            and isinstance(person.get("name"), str) and _is_file_name(person.get("file"))
            and _is_count(person.get("pages")) and _is_count(person.get("cover"))
            and isinstance(person.get("parts"), list)
            and all(_is_part(part, segments) for part in person["parts"])
            # the cover and the parts are the whole packet
            and person["cover"] + sum(part["pages"] for part in person["parts"]) == person["pages"])


def _is_stored_file(value, *counts):
    """A zip / schedule entry: a safe file name, its sha256 and the given counts."""
    return (isinstance(value, dict) and _is_file_name(value.get("file")) and isinstance(value.get("sha256"), str)
            and all(_is_count(value.get(key)) for key in counts))


def parse_index(raw):
    """
    The index from its blob's bytes, or None when it is not an index of this
    version in the expected shape (then Custom generate simply generates
    again). Checks everything a reader of the index relies on (the freshness
    check, the site's tree, the selection, the cut and the contents page), so
    none of them meets a value of another type: every segment is an object of
    texts, every day, person and part has its fields in their types, days and
    a day's people are named once, a packet's cover and parts add up to its
    pages and a day's packets and schedule copies to the day document's.
    """
    try:
        index = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(index, dict) or index.get("version") != INDEX_VERSION:
        return None
    segments, days = index.get("segments"), index.get("days")
    if not isinstance(segments, dict) or not isinstance(days, list):
        return None
    if not all(_is_segment(segment) for segment in segments.values()):
        return None
    dates = set()
    for day in days:
        if not isinstance(day, dict) or not isinstance(day.get("date"), str) or not _DATE_RE.fullmatch(day["date"]):
            return None
        if day["date"] in dates or not _is_text_or_none(day.get("title")):
            return None
        dates.add(day["date"])
        schedule, people = day.get("schedule"), day.get("people")
        if not _is_stored_file(day.get("zip"), "size"):
            return None
        if schedule is not None and not _is_stored_file(schedule, "pages", "copies"):
            return None
        if not isinstance(people, list) or not _is_count(day.get("pages")):
            return None
        if not all(_is_person(person, segments) for person in people):
            return None
        if len({person["slug"] for person in people}) != len(people):
            return None
        papers = sum(person["pages"] for person in people)
        if schedule is not None:
            papers += schedule["pages"] * schedule["copies"]
        if papers != day["pages"]:
            return None
    return index


def client_index(index):
    """What the site gets of an index: the tree's data, no file names or hashes."""
    return {
        "generation": index.get("generation"),
        "generatedUtc": index.get("generatedUtc"),
        "segments": {prefix: {key: segment.get(key) for key in ("date", "start", "end", "category", "segment")}
                     for prefix, segment in index["segments"].items()},
        "days": [{
            "date": day["date"],
            "pages": day["pages"],
            "schedule": ({"pages": day["schedule"]["pages"], "copies": day["schedule"]["copies"]}
                         if day.get("schedule") else None),
            "people": [{
                "slug": person["slug"],
                "name": person["name"],
                "pages": person["pages"],
                "cover": person["cover"],
                "parts": [{
                    "prefix": part["prefix"],
                    "role": part["role"],
                    "label": part.get("label") or "",
                    "pages": part["pages"],
                    "intro": part.get("intro") or 0,
                    "flags": part.get("flags") or [],
                } for part in person["parts"]],
            } for person in day["people"]],
        } for day in index["days"]],
    }


# --- The selection -------------------------------------------------------------

def _only_keys(value, allowed, what):
    if not isinstance(value, dict) or any(key not in allowed for key in value):
        raise SelectionError(f"{what} takes only {' and '.join(allowed)}")


def _flag(value, key, what):
    flag = value.get(key, False)
    if not isinstance(flag, bool):
        raise SelectionError(f"{what} {key} must be true or false")
    return flag


def validate_selection(index, selection):
    """
    A client's selection checked against the index and normalized:

      {"<YYYYMMDD>": {"schedule": bool,
                      "people": {"<slug>": {"cover": bool, "parts": [[prefix, role], ...]}}}}

    Every day, person and part must be in the index; a day's schedule only
    where the day has one. People and days with nothing ticked are dropped,
    and parts become (prefix, role) tuples. Raises SelectionError (also when
    nothing is left).
    """
    if not isinstance(selection, dict):
        raise SelectionError("the selection must be an object of days")
    days = {day["date"]: day for day in index["days"]}
    chosen = {}
    for date, picked in selection.items():
        day = days.get(date)
        if day is None:
            raise SelectionError("unknown day")
        _only_keys(picked, ("schedule", "people"), "a day")
        schedule = _flag(picked, "schedule", "a day's")
        if schedule and not day.get("schedule"):
            raise SelectionError("that day has no combined schedule")
        wanted = picked.get("people", {})
        if not isinstance(wanted, dict):
            raise SelectionError("a day's people must be an object")
        by_slug = {person["slug"]: person for person in day["people"]}
        people = {}
        for slug, want in wanted.items():
            person = by_slug.get(slug)
            if person is None:
                raise SelectionError("unknown person")
            _only_keys(want, ("cover", "parts"), "a person")
            cover = _flag(want, "cover", "a person's")
            listed = want.get("parts", [])
            if not isinstance(listed, list):
                raise SelectionError("a person's parts must be a list")
            known = {(part["prefix"], part["role"]) for part in person["parts"]}
            parts = []
            for item in listed:
                if not (isinstance(item, list) and len(item) == 2 and all(isinstance(x, str) for x in item)):
                    raise SelectionError("a part is [prefix, role]")
                key = (item[0], item[1])
                if key not in known:
                    raise SelectionError("unknown part")
                if key in parts:
                    raise SelectionError("a part is listed twice")
                parts.append(key)
            if cover or parts:
                people[slug] = {"cover": cover, "parts": parts}
        if schedule or people:
            chosen[date] = {"schedule": schedule, "people": people}
    if not chosen:
        raise SelectionError("nothing is selected")
    return chosen


def selection_json(chosen):
    """A validated selection in the request's shape again (for storing it)."""
    return {date: {"schedule": day["schedule"],
                   "people": {slug: {"cover": want["cover"], "parts": [list(key) for key in want["parts"]]}
                              for slug, want in day["people"].items()}}
            for date, day in chosen.items()}


def plan_cut(index, chosen):
    """
    What to cut for a validated selection: one plan per day with pages, in
    date order:

      {date, title, zip: {file, sha256, …}, pages, segments,
       schedule: {file, pages, copies, sha256, …} | None,
       people: [{name, file, packet_pages, pages, ranges: [(first, stop)],
                 labels: [role label], segments: [prefix]}]}

    People in the day document's order; `ranges` are 0-based page ranges of
    the person's packet (stop excluded), in packet order, neighbours merged.
    `pages` counts the papers, without a contents page. Parts and covers
    without pages are left out, so are people and days left with none.
    """
    plans = []
    for day in index["days"]:
        picked = chosen.get(day["date"])
        if not picked:
            continue
        people, prefixes = [], set()
        for person in day["people"]:
            want = picked["people"].get(person["slug"])
            if not want:
                continue
            ranges, labels, segments = [], [], []
            if want["cover"] and person["cover"]:
                ranges.append([0, person["cover"]])
            wanted = set(want["parts"])
            offset = person["cover"]
            for part in person["parts"]:
                first, offset = offset, offset + part["pages"]
                if (part["prefix"], part["role"]) not in wanted or not part["pages"]:
                    continue
                if ranges and ranges[-1][1] == first:
                    ranges[-1][1] = offset
                else:
                    ranges.append([first, offset])
                label = part.get("label") or ""
                if label and label not in labels:
                    labels.append(label)
                if part["prefix"] not in segments:
                    segments.append(part["prefix"])
            pages = sum(stop - first for first, stop in ranges)
            if not pages:
                continue
            prefixes.update(segments)
            people.append({
                "name": person["name"],
                "file": person["file"],
                "packet_pages": person["pages"],
                "pages": pages,
                "ranges": [(first, stop) for first, stop in ranges],
                "labels": labels,
                "segments": segments,
            })
        schedule = day.get("schedule") if picked["schedule"] else None
        if schedule and not (schedule["pages"] and schedule["copies"]):
            schedule = None
        pages = sum(person["pages"] for person in people)
        if schedule:
            pages += schedule["pages"] * schedule["copies"]
        if pages:
            plans.append({
                "date": day["date"],
                "title": day.get("title") or "",
                "zip": day["zip"],
                "pages": pages,
                "segments": len(prefixes),
                "schedule": schedule,
                "people": people,
            })
    return plans


# --- The contents page ---------------------------------------------------------

def segment_label(index, prefix):
    """'Category, Segment' of a segment as the papers print them."""
    segment = index["segments"].get(prefix) or {}
    return ", ".join(name for name in (segment.get("category"), segment.get("segment")) if name) or prefix


def contents_rows(index, plan, first_page):
    """
    The rows of a day plan's contents page: (first page, last page, person,
    roles, [segment labels]), numbered from first_page (1-based, the page
    after the contents pages) in the order the PDF holds them.
    """
    rows, page = [], first_page
    for person in plan["people"]:
        rows.append((page, page + person["pages"] - 1, person["name"],
                     ", ".join(person["labels"]) or "Cover page",
                     [segment_label(index, prefix) for prefix in person["segments"]]))
        page += person["pages"]
    if plan["schedule"]:
        pages = plan["schedule"]["pages"] * plan["schedule"]["copies"]
        copies = plan["schedule"]["copies"]
        rows.append((page, page + pages - 1, "Combined time schedule",
                     f"{copies} {'copy' if copies == 1 else 'copies'}", []))
    return rows


def _count(number, one, many):
    return f"{number} {one if number == 1 else many}"


def _contents_story(index, plan, first_page, made):
    date = datetime.strptime(plan["date"], "%Y%m%d")
    title = ParagraphStyle("title", fontName=TABLE_FONT_BOLD, fontSize=14, leading=18)
    line = ParagraphStyle("line", fontName=TABLE_FONT, fontSize=9.5, leading=13)
    cell = ParagraphStyle("cell", fontName=TABLE_FONT, fontSize=9, leading=11.5)
    head = ParagraphStyle("head", parent=cell, fontName=TABLE_FONT_BOLD)
    pages = ParagraphStyle("pages", parent=cell, alignment=TA_RIGHT)
    pages_head = ParagraphStyle("pages-head", parent=head, alignment=TA_RIGHT)

    story = [Paragraph(escape(f"CUSTOM PRINT · {date.strftime('%A')} {date.strftime('%d.%m.%Y')}"), title)]
    if plan["title"]:
        story.append(Paragraph(escape(plan["title"]), line))
    story.append(Paragraph(escape(
        f"Made {made.strftime('%d.%m.%Y %H:%M')} · {_count(plan['segments'], 'segment', 'segments')} · "
        f"{_count(len(plan['people']), 'person', 'people')} · {_count(plan['pages'], 'page', 'pages')} of papers"),
        line))
    story.append(Spacer(1, 5 * mm))

    data = [[Paragraph("Pages", pages_head), Paragraph("Person", head), Paragraph("Role", head),
             Paragraph("Segments", head)]]
    for first, last, name, roles, segments in contents_rows(index, plan, first_page):
        span = str(first) if first == last else f"{first} – {last}"
        data.append([Paragraph(span, pages), Paragraph(escape(name), cell), Paragraph(escape(roles), cell),
                     Paragraph("<br/>".join(escape(text) for text in segments), cell)])
    # splitInRow: a person with more segments than fit on a page continues on
    # the next one (a table otherwise splits between rows only, and fails)
    table = Table(data, colWidths=[24 * mm, 48 * mm, 42 * mm, 66 * mm], repeatRows=1, splitInRow=1)
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.9, colors.black),
        ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.Color(0.7, 0.7, 0.7)),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
    ]))
    story.append(table)
    return story


def _contents_footer(canvas, doc):
    canvas.saveState()
    canvas.setFont(TABLE_FONT, 7)
    canvas.setFillGray(0.4)
    canvas.drawRightString(A4[0] - 15 * mm, 24, FOOTER_TEXT)
    canvas.restoreState()


def create_contents_pdf(output_path, index, plan, made=None):
    """
    Write the contents page(s) of a day plan: every person in print order with
    their roles, segments and the pages they hold in the custom PDF, so the
    printout is easy to split and to check. Page numbers count the contents
    pages themselves, which go in front. Returns the number of pages written.
    """
    made = made or datetime.now(_HELSINKI)

    def build(first_page):
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                                topMargin=16 * mm, bottomMargin=16 * mm, title="Custom print")
        doc.build(_contents_story(index, plan, first_page, made),
                  onFirstPage=_contents_footer, onLaterPages=_contents_footer)
        return buffer.getvalue(), doc.page

    # The page numbers depend on how many pages the list itself takes. Row
    # heights don't depend on the numbers (one line, fixed column), so the
    # second pass has the same page count as the first.
    data, own_pages = build(2)
    if own_pages != 1:
        data, own_pages = build(own_pages + 1)
    with open(output_path, "wb") as f:
        f.write(data)
    return own_pages


# --- The cut -------------------------------------------------------------------

def file_sha256(path):
    """sha256 hex of a file, read in chunks."""
    digest_ = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest_.update(chunk)
    return digest_.hexdigest()


def cut_day(zip_path, schedule_path, plan, output_path, contents_path=None):
    """
    Write a day plan's custom PDF: the contents page(s) when given, each
    person's selected pages from their packet in the day ZIP, then the
    combined schedule's copies. Returns the page count.

    The files must be the ones the index describes, byte for byte: the day
    ZIP and the schedule are checked against the plan's sha256 before a page
    is read, so papers replaced meanwhile (another generate run), even by
    ones with the same page counts, are never mixed into a custom PDF. Raises
    PapersChanged for that, and for a packet that is missing or has another
    page count: never a PDF cut by wrong numbers.

    Every packet repeats the fonts and the banner of its sheets, so the
    writer merges identical objects before writing; when that fails the PDF
    is written as it is.
    """
    if file_sha256(zip_path) != plan["zip"]["sha256"]:
        raise PapersChanged("the day ZIP is not the one the index describes")
    schedule = None
    if plan["schedule"]:
        with open(schedule_path, "rb") as f:
            schedule = f.read()
        if hashlib.sha256(schedule).hexdigest() != plan["schedule"]["sha256"]:
            raise PapersChanged("the combined schedule is not the one the index describes")

    def assemble(compress):
        writer = PdfWriter()
        if contents_path:
            for page in PdfReader(contents_path).pages:
                writer.add_page(page)
        with zipfile.ZipFile(zip_path) as archive:
            members = set(archive.namelist())
            for person in plan["people"]:
                if person["file"] not in members:
                    raise PapersChanged("a packet is not in the ZIP")
                reader = PdfReader(io.BytesIO(archive.read(person["file"])))
                if len(reader.pages) != person["packet_pages"]:
                    raise PapersChanged("a packet has another page count")
                for first, stop in person["ranges"]:
                    for number in range(first, stop):
                        writer.add_page(reader.pages[number])
        if schedule is not None:
            # the bytes whose sha256 was just checked against the index, never
            # the file a second time: a schedule of another run can't get in
            reader = PdfReader(io.BytesIO(schedule))
            if len(reader.pages) != plan["schedule"]["pages"]:
                raise PapersChanged("the combined schedule has another page count")
            for _ in range(plan["schedule"]["copies"]):
                for page in reader.pages:
                    writer.add_page(page)
        if compress:
            writer.compress_identical_objects()
        with open(output_path, "wb") as f:
            writer.write(f)
        return len(writer.pages)

    try:
        return assemble(True)
    except PapersChanged:
        raise
    except Exception as e:
        print(f"  Warning: a custom PDF was written without merging identical objects ({type(e).__name__})")
        return assemble(False)
