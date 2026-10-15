"""
Combined time schedule of one competition day: every segment's
StartListwithTimes on a single (multi-page) list, with the segment parameters
under each block and the competition banner on top. Optional add-on page placed
right after the personal cover page of each packet (`globalCombinedSchedule`).

The StartLists are read with pdfminer.six rather than pypdf: pypdf's text
extraction merges a whole row into one string and, on the Free Skating lists,
orders the CurrentRank/Points columns before the name. pdfminer gives every
character's position, so cells are rebuilt from x/y and the columns are
matched against the header row ('Nation', 'Current Rank', 'Points').
"""
import os
import re
from datetime import datetime
from io import BytesIO

from pdfminer.high_level import extract_pages
from pdfminer.layout import LTChar, LTContainer
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (ArrayObject, ContentStream, DecodedStreamObject, DictionaryObject,
                           IndirectObject, NameObject)
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

FONTS_DIR = os.path.join(os.path.dirname(__file__), "assets", "fonts")


def _register_fonts():
    # Liberation Sans: Arial metrics like the FSM sheets, lining figures and
    # Latin Extended for skater names (Raleway has neither). Best-effort, like
    # create_cover_pages: fall back to Helvetica rather than fail the page.
    try:
        pdfmetrics.registerFont(TTFont("LiberationSans", os.path.join(FONTS_DIR, "LiberationSans-Regular.ttf")))
        pdfmetrics.registerFont(TTFont("LiberationSans-Bold", os.path.join(FONTS_DIR, "LiberationSans-Bold.ttf")))
        return "LiberationSans", "LiberationSans-Bold"
    except Exception as e:
        print(f"Could not register Liberation Sans fonts, falling back to Helvetica: {e}")
        return "Helvetica", "Helvetica-Bold"


FONT_REGULAR, FONT_BOLD = _register_fonts()

# --- Parsing ---------------------------------------------------------------

PARAM_KEYS = ("Resurfacing", "Warm-Up", "Duration of Skating",
              "Introduction", "Judging First", "Judging Last")
_PARAM_RE = re.compile(
    r"(" + "|".join(re.escape(k) for k in PARAM_KEYS) + r"):\s*(.*?)"
    r"(?=\s+(?:" + "|".join(re.escape(k) for k in PARAM_KEYS) + r"):|$)")
_TIME_RE = re.compile(r"^\d{1,2}[.:]\d{2}[.:]\d{2}$")
_DATE_RE = re.compile(
    r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})",
    re.IGNORECASE)
_PRINTED_RE = re.compile(r"printed:\s*(\d{1,2}\.\d{1,2}\.\d{4})\s+(\d{1,2})[.:](\d{2})", re.IGNORECASE)
_RANK_HEADERS = {"current", "rank", "points", "currentrank", "current rank"}

ROW_Y_TOLERANCE = 3    # pt; chars of one table row sit within this of each other
CELL_GAP = 4           # pt; a wider horizontal gap starts a new cell
CLUB_SLACK = 15        # pt; club cells may start a little left of the 'Nation' header


def _chars(obj):
    if isinstance(obj, LTChar):
        yield obj
    elif isinstance(obj, LTContainer):
        for child in obj:
            yield from _chars(child)


def _page_rows(layout_page):
    """
    Rows of cells on a page, top to bottom: [(y, [(x0, text, bold), ...]), ...].
    Characters are clustered into rows by y and into cells by horizontal gaps.
    """
    chars = sorted(_chars(layout_page), key=lambda c: (-c.y0, c.x0))
    clusters = []
    for ch in chars:
        if clusters and abs(clusters[-1][0] - ch.y0) <= ROW_Y_TOLERANCE:
            clusters[-1][1].append(ch)
        else:
            clusters.append((ch.y0, [ch]))

    rows = []
    for y, row_chars in clusters:
        cells = []
        for ch in sorted(row_chars, key=lambda c: c.x0):
            if cells and ch.x0 - cells[-1]["x1"] <= CELL_GAP:
                cells[-1]["text"] += ch.get_text()
                cells[-1]["x1"] = ch.x1
            else:
                cells.append({"x0": ch.x0, "x1": ch.x1, "text": ch.get_text(),
                              "bold": "bold" in ch.fontname.lower()})
        cells = [(c["x0"], c["text"].strip(), c["bold"]) for c in cells if c["text"].strip()]
        if cells:
            rows.append((y, cells))
    return rows


def _row_text(cells):
    return " ".join(text for _, text, _ in cells)


def _parse_date(text):
    m = _DATE_RE.search(text)
    if not m:
        return None
    try:
        return datetime.strptime(" ".join(m.groups()), "%d %B %Y")
    except ValueError:
        return None


def _parse_table_row(cells, nation_x, rank_x):
    """One table row -> {kind, time, number, name, club, bold}."""
    cells = list(cells)
    times = []
    while cells and (_TIME_RE.match(cells[0][1]) or cells[0][1] in ("-", "–")
                     or re.fullmatch(r"\d{1,2}[.:]\d{2}[.:]\d{2}\s*[-–]\s*\d{1,2}[.:]\d{2}[.:]\d{2}", cells[0][1])):
        times.extend(t for t in re.split(r"\s*[-–]\s*", cells.pop(0)[1]) if t)
    time = " - ".join(times)

    number = ""
    if cells and re.fullmatch(r"\d+|WD", cells[0][1]) and (nation_x is None or cells[0][0] < nation_x - CLUB_SLACK):
        number = cells.pop(0)[1]

    name_parts, club_parts = [], []
    bold = False
    for x0, text, is_bold in cells:
        if rank_x is not None and x0 >= rank_x:
            continue  # CurrentRank / Points columns of the Free Skating lists
        if nation_x is not None and x0 >= nation_x - CLUB_SLACK:
            club_parts.append(text)
        else:
            name_parts.append(text)
            bold = bold or is_bold
    name = " ".join(name_parts)

    if number == "WD":
        kind = "withdrawn"
    elif number:
        kind = "skater"
    elif re.search(r"warm[- ]?up", name, re.IGNORECASE):
        kind = "warmup"
    elif re.search(r"resurfac", name, re.IGNORECASE):
        kind = "resurfacing"
    else:
        kind = "other"
    return {"kind": kind, "time": time, "number": number, "name": name,
            "club": " ".join(club_parts), "bold": bold}


def parse_start_list(pdf_path):
    """
    Parse an FSM StartListwithTimes PDF. Returns None if it can't be read:
      {title_line, date, rows, params, printed, first_time}
    rows:   [{kind: skater|withdrawn|warmup|resurfacing|other, time, number, name, club}]
    params: {key: value} for the PARAM_KEYS present
    printed: datetime of the 'printed:' footer stamp, or None
    first_time: first row's start time 'H.MM.SS' as printed, or None
    """
    try:
        pages = list(extract_pages(pdf_path))
    except Exception as e:
        print(f"  Could not read a StartList for the combined schedule: {type(e).__name__}")
        return None

    result = {"title_line": None, "date": None, "rows": [], "params": {},
              "printed": None, "first_time": None}

    for layout_page in pages:
        rows = _page_rows(layout_page)

        # Header block: 'TIME SCHEDULE', the category/segment line, the date
        # line with the column headers. Repeated on every page of the list.
        header_end = None
        nation_x = rank_x = None
        for i, (y, cells) in enumerate(rows):
            text = _row_text(cells)
            if result["title_line"] is None and i > 0 and _row_text(rows[i - 1][1]).upper() == "TIME SCHEDULE":
                result["title_line"] = text
            for x0, cell, _ in cells:
                if cell.lower() == "nation":
                    nation_x = x0
                elif cell.lower() in _RANK_HEADERS:
                    rank_x = x0 if rank_x is None else min(rank_x, x0)
            date = _parse_date(text)
            if date:
                result["date"] = result["date"] or date
                header_end = i
        if header_end is None:
            continue
        # Column headers may sit a few points below the date line ('Rank' under 'Current').
        while header_end + 1 < len(rows) and all(
                c.lower() in _RANK_HEADERS | {"nation"} for _, c, _ in rows[header_end + 1][1]):
            header_end += 1
        if rank_x is not None:
            rank_x -= CLUB_SLACK  # the values are right-aligned under the header

        for y, cells in rows[header_end + 1:]:
            text = _row_text(cells)
            printed = _PRINTED_RE.search(text)
            if printed:
                try:
                    result["printed"] = datetime.strptime(
                        f"{printed.group(1)} {printed.group(2)}:{printed.group(3)}", "%d.%m.%Y %H:%M")
                except ValueError:
                    pass
                continue
            params = dict((k, v.strip()) for k, v in _PARAM_RE.findall(text))
            if params and _PARAM_RE.match(text):
                result["params"].update(params)
                continue
            if text.startswith("Legend") or re.fullmatch(r"WD\s+Withdrawn", text) or re.match(r"Page \d+ / \d+$", text):
                continue
            row = _parse_table_row(cells, nation_x, rank_x)
            if not (row["time"] or row["number"] or row["name"]):
                continue
            result["rows"].append(row)

    if result["title_line"] is None and result["date"] is None:
        return None
    for row in result["rows"]:
        if row["time"]:
            result["first_time"] = row["time"].split(" - ")[0]
            break
    return result


def time_sort_key(time_str):
    """'9.30.00' / '09:30' -> seconds since midnight; None -> None."""
    if not time_str:
        return None
    parts = [int(p) for p in re.split(r"[.:]", time_str) if p.isdigit()]
    if len(parts) < 2:
        return None
    return parts[0] * 3600 + parts[1] * 60 + (parts[2] if len(parts) > 2 else 0)


# Segment names FSM prints after the category on line 2 of its sheets; used
# when the processor has no segment name for the prefix.
_SEGMENT_SUFFIX_RE = re.compile(
    r"\s(SHORT PROGRAM|FREE SKATING|FREE PROGRAM|RHYTHM DANCE|FREE DANCE|PATTERN DANCE.*|PD\d.*)$",
    re.IGNORECASE)


def split_title(title_line, segment_hint=None):
    """
    'JUNIORI NAISET  SHORT PROGRAM' -> ('JUNIORI NAISET', 'SHORT PROGRAM'),
    as printed. segment_hint is the segment name the processor took from
    JudgesSheetAll; falls back to known segment names, then to a 2+ space gap.
    Returns (title_line, None) when no split is found.
    """
    line = (title_line or "").strip()
    hint = (segment_hint or "").strip()
    if hint and len(line) > len(hint) and line.casefold().endswith(hint.casefold()):
        return line[:-len(hint)].strip(), line[-len(hint):].strip()
    m = _SEGMENT_SUFFIX_RE.search(line)
    if m and m.start() > 0:
        return line[:m.start()].strip(), m.group(1).strip()
    parts = re.split(r"\s{2,}", line, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return line, None


# --- Banner ----------------------------------------------------------------

def _mul(a, b):
    """PDF matrix product a x b (6-element affine matrices)."""
    return [a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3],
            a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
            a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5]]


def _bbox(matrix, box=(0, 0, 1, 1)):
    x0, y0, x1, y1 = box
    pts = [(x * matrix[0] + y * matrix[2] + matrix[4], x * matrix[1] + y * matrix[3] + matrix[5])
           for x, y in ((x0, y0), (x1, y0), (x0, y1), (x1, y1))]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def find_banner(pdf_path):
    """
    The banner FSM draws above the 'TIME SCHEDULE' heading: every image or form
    XObject on page 1 that lies entirely above the first text. Returns
    {path, draws: [(matrix, name)], bottom, size} or None. The organiser's banner text
    and logos are baked into that image, so copying it carries them over.
    """
    try:
        layout = next(iter(extract_pages(pdf_path, page_numbers=[0])))
        text_top = max((c.y1 for c in _chars(layout)), default=None)
        if text_top is None:
            return None

        reader = PdfReader(pdf_path)
        page = reader.pages[0]
        xobjects = page.get("/Resources", {}).get("/XObject", {})
        xobjects = xobjects.get_object() if xobjects else {}
        ops = ContentStream(page.get_contents(), reader).operations

        ctm, stack, draws = [1, 0, 0, 1, 0, 0], [], []
        for operands, op in ops:
            if op == b"q":
                stack.append(ctm)
            elif op == b"Q":
                ctm = stack.pop() if stack else [1, 0, 0, 1, 0, 0]
            elif op == b"cm":
                ctm = _mul([float(v) for v in operands], ctm)
            elif op == b"Do":
                name = operands[0]
                xobj = xobjects.get(name)
                if xobj is None:
                    continue
                xobj = xobj.get_object()
                subtype = xobj.get("/Subtype")
                if subtype == "/Image":
                    box = _bbox(ctm)
                elif subtype == "/Form":
                    form_matrix = [float(v) for v in xobj.get("/Matrix", [1, 0, 0, 1, 0, 0])]
                    box = _bbox(_mul(form_matrix, ctm), [float(v) for v in xobj["/BBox"]])
                else:
                    continue
                if box[1] >= text_top - 1:
                    draws.append((list(ctm), str(name), box))
        if not draws:
            return None
        return {"path": pdf_path, "draws": [(m, n) for m, n, _ in draws],
                "bottom": min(b[1] for _, _, b in draws),
                "size": (float(page.mediabox.width), float(page.mediabox.height))}
    except Exception as e:
        print(f"  Could not read the banner from a StartList: {type(e).__name__}")
        return None


def _add_banner(writer, page, banner_xobjects, draws):
    """
    Put the banner images under `page`'s own content. The page's content
    stream is left byte-for-byte as reportlab wrote it: pypdf's merge_page
    re-serializes it and loses the subset-font characters (Ä, Ö, …).
    """
    resources = page["/Resources"].get_object()
    if "/XObject" not in resources:
        resources[NameObject("/XObject")] = DictionaryObject()
    page_xobjects = resources["/XObject"].get_object()

    ops = []
    for i, (matrix, ref) in enumerate(zip((m for m, _ in draws), banner_xobjects)):
        name = f"/FstBanner{i}"
        page_xobjects[NameObject(name)] = ref
        ops.append("q {} cm {} Do Q".format(" ".join(f"{v:.6f}" for v in matrix), name))
    stream = DecodedStreamObject()
    stream.set_data(("\n".join(ops) + "\n").encode("ascii"))
    stream_ref = writer._add_object(stream)

    contents = page.raw_get("/Contents")
    contents = contents.get_object() if isinstance(contents, IndirectObject) and isinstance(
        contents.get_object(), ArrayObject) else contents
    existing = list(contents) if isinstance(contents, ArrayObject) else [contents]
    page[NameObject("/Contents")] = ArrayObject([stream_ref] + existing)


# --- Rendering -------------------------------------------------------------

PAGE_W, PAGE_H = A4
LEFT, RIGHT = 34, PAGE_W - 34
TOP_NO_BANNER = PAGE_H - 34
FOOTER_Y = 28
CONTENT_BOTTOM = 48

COL_TIME = LEFT + 6
COL_NO_RIGHT = LEFT + 150
COL_NAME = LEFT + 160
COL_CLUB_RIGHT = RIGHT - 6

BAR_H = 20
BAR_GAP = 3
ROW_H = 13.5
PARAM_GAP = 5
PARAM_LEAD = 10
BLOCK_GAP = 14

BAR_FILL = (0.894, 0.918, 0.945)
RULE = (0.835, 0.851, 0.871)
MUTED = (0.42, 0.42, 0.42)
# Row bands that divide a segment into its warm-up groups.
ROW_FILLS = {
    "warmup": (0.867, 0.937, 0.894),       # pale green
    "resurfacing": (0.992, 0.925, 0.796),  # pale amber
}

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _normalize_name(name):
    return " ".join((name or "").split()).casefold()


def is_struck(row, withdrawn_names):
    """A WD row of the StartList, or a skater the judges' sheets mark withdrawn."""
    if row["kind"] == "withdrawn":
        return True
    return row["kind"] == "skater" and _normalize_name(row["name"]) in withdrawn_names


def _param_lines(params):
    first = [k for k in ("Resurfacing", "Warm-Up", "Duration of Skating") if params.get(k)]
    second = [k for k in ("Introduction", "Judging First", "Judging Last") if params.get(k)]
    return ["   ".join(f"{k}: {params[k]}" for k in keys) for keys in (first, second) if keys]


def _printed_note(stamps):
    stamps = sorted(s for s in stamps if s)
    if not stamps:
        return "Times as per the source documents."
    lo, hi = stamps[0], stamps[-1]
    if lo == hi:
        span = f"{lo:%d.%m.%Y} {lo.hour}.{lo:%M}"
    elif lo.date() == hi.date():
        span = f"{lo:%d.%m.%Y} {lo.hour}.{lo:%M}–{hi.hour}.{hi:%M}"
    else:
        span = f"{lo:%d.%m.%Y} {lo.hour}.{lo:%M} – {hi:%d.%m.%Y} {hi.hour}.{hi:%M}"
    return f"Times as per the source documents. Sources printed {span}."


def _fit(c, text, font, size, max_width, min_size=6.5):
    """Shrink, then truncate with an ellipsis, so text fits max_width."""
    while size > min_size and c.stringWidth(text, font, size) > max_width:
        size -= 0.5
    if c.stringWidth(text, font, size) > max_width:
        while text and c.stringWidth(text + "…", font, size) > max_width:
            text = text[:-1]
        text += "…"
    return text, size


def _layout(segments, content_top):
    """
    Split the segment blocks over pages. A block moves to a new page when it
    doesn't fit what is left; a block taller than a whole page is cut and
    continues under a '(cont.)' header bar. Returns pages of block pieces:
    [[{segment, rows, cont, params}, ...], ...]
    """
    full = content_top - CONTENT_BOTTOM
    pages, current, used = [], [], 0

    def height(n_rows, n_params):
        return BAR_H + BAR_GAP + max(n_rows, 1) * ROW_H + (
            PARAM_GAP + n_params * PARAM_LEAD if n_params else 0) + BLOCK_GAP

    for seg in segments:
        rows = list(seg["rows"])
        n_params = len(_param_lines(seg["params"]))
        cont = False
        while True:
            need = height(len(rows), n_params)
            if used + need <= full:
                current.append({"segment": seg, "rows": rows, "cont": cont, "params": True})
                used += need
                break
            if current and height(len(rows), n_params) <= full:
                pages.append(current)
                current, used = [], 0
                continue
            # Doesn't fit on any page: take as many rows as fit here.
            room = int((full - used - height(0, 0) + ROW_H) // ROW_H)
            if room < 3 and current:
                pages.append(current)
                current, used = [], 0
                continue
            room = max(room, 1)
            current.append({"segment": seg, "rows": rows[:room], "cont": cont, "params": False})
            pages.append(current)
            current, used = [], 0
            rows, cont = rows[room:], True
    if current or not pages:
        pages.append(current)
    return pages


def _draw_block(c, y, piece, withdrawn):
    seg = piece["segment"]
    category, segment = split_title(seg["title_line"], seg.get("segment_hint"))
    header = f"{category} | {segment}" if segment else category
    if piece["cont"]:
        header += " (cont.)"

    c.setFillColorRGB(*BAR_FILL)
    c.rect(LEFT, y - BAR_H, RIGHT - LEFT, BAR_H, stroke=0, fill=1)
    c.setFillColorRGB(0, 0, 0)
    text, size = _fit(c, header, FONT_BOLD, 9.5, RIGHT - LEFT - 12)
    c.setFont(FONT_BOLD, size)
    c.drawString(LEFT + 6, y - BAR_H + 6.5, text)
    y -= BAR_H + BAR_GAP

    if not seg["rows"]:
        c.setFont(FONT_REGULAR, 8.5)
        c.drawString(COL_TIME, y - ROW_H + 4, "No skaters or times in the source file.")
        y -= ROW_H

    for row in piece["rows"]:
        base = y - ROW_H + 4
        fill = ROW_FILLS.get(row["kind"])
        if fill:
            c.setFillColorRGB(*fill)
            c.rect(LEFT, y - ROW_H, RIGHT - LEFT, ROW_H, stroke=0, fill=1)
        font = FONT_BOLD if fill else FONT_REGULAR
        struck = is_struck(row, withdrawn)
        c.setFillColorRGB(*(MUTED if struck else (0, 0, 0)))
        c.setFont(font, 8.5)
        if row["time"]:
            c.drawString(COL_TIME, base, row["time"])
        if row["number"]:
            c.drawRightString(COL_NO_RIGHT, base, row["number"])
        club_w = 0
        if row["club"]:
            club, club_size = _fit(c, row["club"], FONT_REGULAR, 8.5, 90)
            c.setFont(FONT_REGULAR, club_size)
            c.drawRightString(COL_CLUB_RIGHT, base, club)
            club_w = c.stringWidth(club, FONT_REGULAR, club_size)
        if row["name"]:
            name, size = _fit(c, row["name"], font, 8.5, COL_CLUB_RIGHT - club_w - 10 - COL_NAME)
            c.setFont(font, size)
            c.drawString(COL_NAME, base, name)
        if struck:
            c.setStrokeColorRGB(*MUTED)
            c.setLineWidth(0.7)
            start = COL_TIME if row["time"] else COL_NO_RIGHT - 14
            c.line(start, base + 3, COL_CLUB_RIGHT, base + 3)
        c.setStrokeColorRGB(*RULE)
        c.setLineWidth(0.4)
        c.line(LEFT, y - ROW_H, RIGHT, y - ROW_H)
        y -= ROW_H

    c.setFillColorRGB(*MUTED)
    if piece["params"]:
        lines = _param_lines(seg["params"])
        if lines:
            y -= PARAM_GAP
            for line in lines:
                text, size = _fit(c, line, FONT_REGULAR, 7.5, RIGHT - LEFT - 12)
                c.setFont(FONT_REGULAR, size)
                c.drawString(COL_TIME, y - PARAM_LEAD + 3, text)
                y -= PARAM_LEAD
    c.setFillColorRGB(0, 0, 0)
    return y - BLOCK_GAP


def create_combined_schedule_pdf(output_path, date_obj, segments, banner=None, withdrawn_by_prefix=None):
    """
    Write the combined schedule of one day.

    segments: [{prefix, title_line, segment_hint, rows, params, printed}] in
              running order (parse_start_list output plus prefix/segment_hint)
    banner:   find_banner() of the day's first StartList, or None
    withdrawn_by_prefix: {prefix: [names]} withdrawn per the judges' sheets,
              struck through on top of the StartList's own WD rows
    Returns the number of pages written.
    """
    withdrawn_by_prefix = withdrawn_by_prefix or {}
    if banner and not (abs(banner["size"][0] - PAGE_W) < 2 and abs(banner["size"][1] - PAGE_H) < 2):
        banner = None  # not an A4 portrait source; positions wouldn't carry over

    top = (banner["bottom"] - 8) if banner else TOP_NO_BANNER
    heading_y = top - 14
    columns_y = heading_y - 20
    content_top = columns_y - 8

    pages = _layout(segments, content_top)
    weekday = WEEKDAYS[date_obj.weekday()]
    heading = f"Time schedule and starting order | {weekday} {date_obj:%d.%m.%Y}"
    note = _printed_note(s.get("printed") for s in segments)

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for page_no, pieces in enumerate(pages, start=1):
        c.setFillColorRGB(0, 0, 0)
        c.setFont(FONT_BOLD, 12)
        c.drawString(LEFT, heading_y, heading)

        c.setFont(FONT_BOLD, 7.5)
        c.drawString(COL_TIME, columns_y, "TIME")
        c.drawRightString(COL_NO_RIGHT, columns_y, "NO.")
        c.drawString(COL_NAME, columns_y, "SKATER / COUPLE / TEAM")
        c.drawRightString(COL_CLUB_RIGHT, columns_y, "CLUB")

        y = content_top
        for piece in pieces:
            withdrawn = {_normalize_name(n) for n in withdrawn_by_prefix.get(piece["segment"].get("prefix"), [])}
            y = _draw_block(c, y, piece, withdrawn)

        c.setFillColorRGB(*MUTED)
        c.setFont(FONT_REGULAR, 7)
        c.drawString(LEFT, FOOTER_Y, note)
        c.drawRightString(RIGHT, FOOTER_Y, f"{page_no} / {len(pages)}")
        c.showPage()
    c.save()
    buf.seek(0)

    overlay = PdfReader(buf)
    writer = PdfWriter()
    banner_xobjects = None
    if banner:
        # Image streams must stay indirect objects: copy each one once and
        # share it between the pages.
        src_xobjects = PdfReader(banner["path"]).pages[0]["/Resources"]["/XObject"].get_object()
        banner_xobjects = [src_xobjects[name].clone(writer).indirect_reference
                           for _, name in banner["draws"]]
    for overlay_page in overlay.pages:
        page = writer.add_page(overlay_page)
        if banner_xobjects:
            _add_banner(writer, page, banner_xobjects, banner["draws"])
    with open(output_path, "wb") as f:
        writer.write(f)
    return len(pages)
