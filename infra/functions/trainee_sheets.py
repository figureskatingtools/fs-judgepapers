"""
A trainee's copy of an official's sheets (issue #93): every page of an FSM
judges' sheet prints `Judge No. N` and the judge's name in its header, and a
Technical Specialist's sheet prints `Technical Specialist:   <name>`. The stamp
covers that line with white and prints `Judge Trainee` + the trainee's name
(two cells, as FSM prints a judge) or `Technical Trainee:   <name>` (one string,
as FSM prints a specialist) in its place, on every page.

The line is found from pdfminer character positions, so its size, baseline and
weight are the original's. The FSM page itself is never rewritten: pypdf's
merge_page re-serializes content streams and loses subset-font characters
(see combined_schedule._add_banner), so the overlay is a Form XObject whose
stream is reportlab's bytes, copied unparsed, drawn by a few bytes appended
around the page's own, untouched content streams.

The caller falls back to the unstamped file on any exception, so everything
unsafe raises and nothing is written until the result re-reads cleanly.
Exceptions and log lines never carry a name, the trainee's or the sheet's.
"""
import io
import os
import re
import tempfile
from typing import NamedTuple

from pdfminer.high_level import extract_pages
from pdfminer.layout import LTContainer, LTCurve, LTImage, LTLine
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, FloatObject, NameObject
from reportlab.pdfgen import canvas

try:
    from .combined_schedule import _chars
    from .create_cover_pages import TABLE_FONT, TABLE_FONT_BOLD, _fit
except ImportError:
    from combined_schedule import _chars
    from create_cover_pages import TABLE_FONT, TABLE_FONT_BOLD, _fit


HEADER_ROWS = 8        # text rows from the top that count as the FSM header block
ROW_TOL = 2.0          # pt; characters of one row
CELL_GAP = 4.0         # pt; a wider gap starts a new cell
LABEL_GAP = 4.0        # pt; the judge label stops this far short of the name cell
NAME_CELL_DX = 142.87  # pt from the role to the name cell, for a judges' sheet printed without names
ROLE_RE = {"judge": re.compile(r"^Judge\s+No\.?\s*\d+"),
           "technical": re.compile(r"^Technical Specialist(?:\s*\d)?\s*:")}
# A role line seen anywhere in the header, also where ROLE_RE could not place it
# (words drawn without space characters, an unexpected cell split): such a page
# must not pass through silently with the official's name on it.
LOOSE_RE = {"judge": re.compile(r"Judge\s*No"), "technical": re.compile(r"Technical\s*Specialist")}
LABEL = {"judge": "Judge Trainee", "technical": "Technical Trainee:"}
FORM_PREFIX = "/FstTrainee"


class UnsupportedLayout(Exception):
    """The role line is there but cannot be covered safely."""


class _Line(NamedTuple):
    """Where and how a page prints the role line; the same on every page of one official."""
    x: float                 # the role cell's left edge
    baseline: float
    size: float
    bold: bool
    name_x: float | None     # judges only: the name cell (FSM prints a specialist as one string)
    name_size: float | None
    name_bold: bool | None
    boxes: tuple             # ((x0, y0, x1, y1), ...) white boxes over the row's cells
    rules: tuple             # ((x0, x1, y, width, colour), ...) covered stretches to redraw


def _rows(layout):
    rows = []
    for ch in sorted(_chars(layout), key=lambda c: (-c.y0, c.x0)):
        if rows and abs(rows[-1][0].y0 - ch.y0) <= ROW_TOL:
            rows[-1].append(ch)
        else:
            rows.append([ch])
    return rows[:HEADER_ROWS]


def _cells(row):
    cells = []
    for ch in sorted(row, key=lambda c: c.x0):
        if cells and ch.x0 - cells[-1][-1].x1 <= CELL_GAP:
            cells[-1].append(ch)
        else:
            cells.append([ch])
    return cells


def _text(chars):
    return "".join(c.get_text() for c in chars)


def _shapes(obj):
    for o in obj:
        if isinstance(o, (LTCurve, LTImage)):      # LTLine and LTRect are LTCurves
            yield o
        elif isinstance(o, LTContainer):
            yield from _shapes(o)


def _colour(value):
    if isinstance(value, (int, float)):
        return (float(value),)
    if isinstance(value, (tuple, list)) and len(value) in (1, 3, 4) and all(
            isinstance(v, (int, float)) for v in value):
        return tuple(float(v) for v in value)
    return (0.0,)


def _upright(ch):
    m = ch.matrix
    return abs(m[1]) <= 1e-6 and abs(m[2]) <= 1e-6 and m[0] > 0 and m[3] > 0


def _is_bold(ch):
    return "bold" in ch.fontname.lower()


def find_role_line(layout, kind):
    """
    The header line that prints the official's role and name: None when the
    page has none, otherwise a _Line. UnsupportedLayout when the text is
    rotated or something other than a horizontal rule lies under the line.
    """
    for row in _rows(layout):
        cells = _cells(row)
        if not ROLE_RE[kind].match(_text(cells[0]).strip()):
            continue
        first = cells[0][0]
        if not all(_upright(ch) for ch in row):
            raise UnsupportedLayout("rotated text on the role line")
        size, baseline = first.size, first.matrix[5]
        # pdfminer's box is baseline - 0.21 em .. + 0.79 em; rings and carons of capitals reach 0.90 em
        boxes = tuple((round(c[0].x0 - 1, 3), round(min(ch.y0 for ch in c) - 0.2, 3),
                       round(c[-1].x1 + 1, 3), round(max(ch.matrix[5] + 0.95 * ch.size for ch in c), 3))
                      for c in cells)
        rules = set()
        for s in _shapes(layout):
            hw = (getattr(s, "linewidth", 0) or 0) / 2
            for x0, y0, x1, y1 in boxes:
                if s.x1 + hw <= x0 or s.x0 - hw >= x1 or s.y1 + hw <= y0 or s.y0 - hw >= y1:
                    continue
                if not isinstance(s, LTLine) or not s.stroke or s.fill or abs(s.y1 - s.y0) > 0.01:
                    raise UnsupportedLayout(type(s).__name__ + " under the role line")
                # pdfminer scales the line width to page units but not a dash
                # array (FSM draws under a 0.2834646 scale): it could not be redrawn alike
                if s.dashing_style and s.dashing_style[0]:
                    raise UnsupportedLayout("dashed rule under the role line")
                # only the covered stretch (+0.5 pt, so the ends leave no seam), never past the rule's own ends
                rules.add((round(max(s.x0, x0 - 0.5), 3), round(min(s.x1, x1 + 0.5), 3), round(s.y0, 3),
                           round(s.linewidth, 4), _colour(s.stroking_color)))
        name_x = name_size = name_bold = None
        if kind == "judge":   # role and name are two cells; a panel without names prints the role alone
            if len(cells) > 1:
                name = cells[1][0]
                name_x, name_size, name_bold = round(name.x0, 3), round(name.size, 3), _is_bold(name)
            else:
                name_x, name_size, name_bold = round(first.x0 + NAME_CELL_DX, 3), round(size, 3), _is_bold(first)
        return _Line(round(first.x0, 3), round(baseline, 3), round(size, 3), _is_bold(first),
                     name_x, name_size, name_bold, boxes, tuple(sorted(rules)))
    return None


def _font(bold):
    return TABLE_FONT_BOLD if bold else TABLE_FONT


def _overlay_form(writer, line, label, name, page_w, page_h):
    """The overlay as a Form XObject of `writer`. reportlab's content bytes are copied, never parsed."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(page_w, page_h), initialFontName=TABLE_FONT)  # no stray Helvetica resource
    c.setFillColorRGB(1, 1, 1)                      # as FSM fills its own tables: '1 1 1 rg'
    for x0, y0, x1, y1 in line.boxes:
        c.rect(x0, y0, x1 - x0, y1 - y0, stroke=0, fill=1)
    for x0, x1, y, width, colour in line.rules:
        if len(colour) == 1:
            c.setStrokeGray(*colour)
        elif len(colour) == 3:
            c.setStrokeColorRGB(*colour)
        else:
            c.setStrokeColorCMYK(*colour)
        c.setLineWidth(width)
        c.setLineCap(0)
        c.line(x0, y, x1, y)
    c.setFillColorRGB(0, 0, 0)
    right = page_w - line.x                         # the header's right margin mirrors the left one
    if line.name_x is None:                         # a specialist: role and name as one string
        parts = [(line.x, f"{label}   {name}", _font(line.bold), line.size, right)]
    else:
        parts = [(line.x, label, _font(line.bold), line.size, line.name_x - LABEL_GAP),
                 (line.name_x, name, _font(line.name_bold), line.name_size, right)]
    for px, text, font, size, limit in parts:
        text, fitted = _fit(c, text, font, size, limit - px)
        c.setFont(font, fitted)
        c.drawString(px, line.baseline, text)
    c.showPage()
    c.save()

    src = PdfReader(buf).pages[0]
    form = DecodedStreamObject()
    form.set_data(src.raw_get("/Contents").get_object().get_data())
    form[NameObject("/Type")] = NameObject("/XObject")
    form[NameObject("/Subtype")] = NameObject("/Form")
    form[NameObject("/BBox")] = ArrayObject(FloatObject(v) for v in (0, 0, page_w, page_h))
    form[NameObject("/Resources")] = src["/Resources"].clone(writer)
    return writer._add_object(form.flate_encode())


def _q_depth(page):
    """(q minus Q at the end of the page's content, lowest level reached). Parsed only, never rewritten."""
    depth = low = 0
    for _, op in page.get_contents().operations:
        if op == b"q":
            depth += 1
        elif op == b"Q":
            depth -= 1
            low = min(low, depth)
    return depth, low


def _attach(page, name, form_ref, head_ref, tail_ref):
    """A private /Resources with the form, and the content wrapped: q <FSM's own streams> Q q /name Do Q."""
    old = page["/Resources"].get_object() if "/Resources" in page else DictionaryObject()
    resources = DictionaryObject()
    for key in old:
        resources[NameObject(key)] = old.raw_get(key)
    xobjects = DictionaryObject()
    if "/XObject" in old:
        own = old["/XObject"].get_object()
        for key in own:
            xobjects[NameObject(key)] = own.raw_get(key)
    xobjects[NameObject(name)] = form_ref
    resources[NameObject("/XObject")] = xobjects
    page[NameObject("/Resources")] = resources      # never the shared or inherited dictionary itself

    raw = page.raw_get("/Contents")
    contents = raw.get_object()
    existing = list(contents) if isinstance(contents, ArrayObject) else [raw]
    page[NameObject("/Contents")] = ArrayObject([head_ref, *existing, tail_ref])


def _stamp(pdf_bytes, kind, trainee_name):
    """
    (pdf bytes, pages stamped, pages without a role line, pages whose role
    line was seen but could not be placed). Raises on anything unsafe.
    """
    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()
    pages = [writer.add_page(p) for p in reader.pages]
    taken = set()
    for page in pages:
        if "/Resources" in page and "/XObject" in page["/Resources"]:
            taken |= {str(k) for k in page["/Resources"]["/XObject"].get_object().keys()}
    forms, streams, stamped, plain, missed = {}, {}, 0, 0, 0

    def stream(data):                               # one object per distinct wrapper, shared by the pages
        if data not in streams:
            s = DecodedStreamObject()
            s.set_data(data)
            streams[data] = writer._add_object(s)
        return streams[data]

    for page, layout in zip(pages, extract_pages(io.BytesIO(pdf_bytes)), strict=True):
        box = page.mediabox
        # pdfminer reports positions relative to the MediaBox corner, and a
        # rotated page turns them: both would misplace the overlay
        upright = not page.get("/Rotate") and float(box.left) == 0 and float(box.bottom) == 0
        line = find_role_line(layout, kind) if upright and "/Contents" in page else None
        if line is None:
            # on a turned or shifted page the header rows are not rows: look through all of its text
            seen = (any(LOOSE_RE[kind].search(_text(row)) for row in _rows(layout)) if upright
                    else LOOSE_RE[kind].search(_text(_chars(layout))))
            if seen:
                missed += 1
            else:
                plain += 1
            continue
        key = (line, float(box.width), float(box.height))
        if key not in forms:
            n = len(forms)
            while f"{FORM_PREFIX}{n}" in taken:
                n += 1
            taken.add(f"{FORM_PREFIX}{n}")
            forms[key] = (f"{FORM_PREFIX}{n}", _overlay_form(writer, line, LABEL[kind], trainee_name,
                                                             float(box.width), float(box.height)))
        name, form_ref = forms[key]
        depth, low = _q_depth(page)
        # enough q's that the page's own stray Q's never reach below ours, then as many Q's
        # as are open at its end: the overlay is drawn in the untouched default state.
        # The leading newline keeps the tail from joining a last token of FSM's stream.
        _attach(page, name, form_ref, stream(b"q\n" * (1 - low)),
                stream(b"\n" + b"Q\n" * (1 - low + depth) + f"q {name} Do Q\n".encode("ascii")))
        stamped += 1
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue(), stamped, plain, missed


def stamp_trainee_sheet(src_pdf, dst_pdf, kind, name):
    """
    Write to dst_pdf a copy of the official's sheets src_pdf with the header's
    role line replaced by the trainee's. kind: "judge" | "technical".

    Returns (stamped, plain, missed): pages whose role line was replaced, pages
    without one (copied unchanged), and pages whose role line could not be
    placed, always 0 on return. Raises, writing nothing, when the source cannot
    be read, no page could be stamped, a role line could not be placed
    (UnsupportedLayout), or the result does not re-read with the source's page
    count.
    """
    if kind not in LABEL:
        raise ValueError("unknown trainee kind")
    name = " ".join(str(name or "").split())
    if not name:
        raise ValueError("empty trainee name")

    with open(src_pdf, "rb") as f:
        data = f.read()
    source_pages = len(PdfReader(io.BytesIO(data)).pages)
    out, stamped, plain, missed = _stamp(data, kind, name)
    if missed:
        raise UnsupportedLayout(f"{missed} role line(s) could not be placed")
    if not stamped:
        raise UnsupportedLayout("no role line found")
    # merge_pdfs swallows read errors: a broken file would silently drop the sheets
    result_pages = len(PdfReader(io.BytesIO(out)).pages)
    if result_pages != source_pages:
        raise ValueError(f"the stamped copy has {result_pages} pages, the source {source_pages}")

    # write next to the target, then rename: a failed write never leaves a partial dst_pdf
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(dst_pdf)), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(out)
        os.replace(tmp, dst_pdf)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return stamped, plain, 0
