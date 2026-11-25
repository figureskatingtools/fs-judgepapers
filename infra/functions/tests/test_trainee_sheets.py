"""trainee_sheets.stamp_trainee_sheet — the trainee's name over an official's
role line on a copy of their sheets.

The sheets are synthetic: reportlab draws an FSM-like header (two title lines,
the role line, a competitor line) at the positions and sizes the FSM exports
use. A judge prints `Judge No. 1` at x 25.5 and the name cell at x 168.4 on a
rule FSM draws as `w 2` under a 0.2834646 scale (0.567 pt), crossing the text's
descender box; a specialist prints role and name as one string, with the nation
at the right (`Technical Specialist:`, bold) or without it
(`Technical Specialist 1:`, regular in some exports). The result is read with
pdfminer: pypdf's layout extraction does not see Form XObjects, which is where
the stamp lives.
"""
import os

import pytest
from pdfminer.high_level import extract_pages
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, FloatObject, NameObject
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

import trainee_sheets as ts
from combine_judging_papers import merge_pdfs
from combined_schedule import _chars

PAGE_W, PAGE_H = A4
FSM_SCALE = 0.2834646
ROLE_X = 25.51
NAME_X = 168.38
ROLE_Y = 788.73                     # a judge's role line
SIZE = 8.71
RULE_X0, RULE_X1, RULE_DY = 165.54, 572.6, 1.44
TS_X, TS_Y, TS_SIZE = 26.08, 777.39, 10.79
TRAINEE = "Åsa Ärölä-ŠIMKUS"
JUDGE = "Mr. Jon JUDGE"
SPECIALIST = "Ms. Tia SPECIALIST"


# --- Fabricated FSM sheets -------------------------------------------------

def _rule(c, x0, x1, y):
    """A rule as FSM draws it: 2 units wide under its 0.2834646 scale."""
    c.saveState()
    c.transform(FSM_SCALE, 0, 0, FSM_SCALE, 0, 0)
    c.setLineWidth(2)
    c.line(x0 / FSM_SCALE, y / FSM_SCALE, x1 / FSM_SCALE, y / FSM_SCALE)
    c.restoreState()


def _titles(c, x=ROLE_X):
    c.setFont("Helvetica-Bold", 11.78)
    c.drawString(x, 816.6, "Kansallinen kilpailu 2026")
    c.drawString(x, 802.14, "SM-NOVIISIT Naiset, Short Program")


def _body(c):
    """Competitor line and a bit of the table under the header, with its grid."""
    _rule(c, 22.68, RULE_X1, 784.46)
    c.setFont("Helvetica-Bold", 11.78)
    c.drawString(41.64, 773.23, "1")
    c.drawString(113.39, 773.23, "Aino AAVIKKO")
    c.drawString(547.71, 773.23, "TL")
    _rule(c, 22.68, RULE_X1, 770.0)
    c.setLineWidth(0.567)
    c.rect(22.68, 452.52, 549.92, 299.91, stroke=1, fill=0)
    c.setFont("Helvetica-Bold", SIZE)
    c.drawString(37.05, 737.17, "Executed Elements")
    c.setFont("Helvetica", SIZE)
    c.drawString(155.91, 713.61, "3Lz+3T")


def judge_page(c, role="Judge No. 1", name=JUDGE, band=False):
    _titles(c)
    if band:                                    # a filled band FSM does not draw: must refuse
        c.setFillGray(0.85)
        c.rect(20, ROLE_Y - 3, 550, 12, stroke=0, fill=1)
        c.setFillGray(0)
    c.setFont("Helvetica-Bold", SIZE)
    c.drawString(ROLE_X, ROLE_Y, role)
    if name:
        c.drawString(NAME_X, ROLE_Y, name)
    _rule(c, RULE_X0, RULE_X1, ROLE_Y - RULE_DY)   # the name sits on it
    _body(c)


def referee_page(c):
    _titles(c)
    c.setFont("Helvetica-Bold", SIZE)
    c.drawString(ROLE_X, ROLE_Y, "Referee")
    c.drawString(NAME_X, ROLE_Y, "Mrs. Rea REFEREE")
    _rule(c, RULE_X0, RULE_X1, ROLE_Y - RULE_DY)
    _body(c)


def specialist_page(c, role="Technical Specialist:", bold=True, nation="FIN"):
    _titles(c, TS_X)
    c.setFont("Helvetica-Bold" if bold else "Helvetica", TS_SIZE)
    c.drawString(TS_X, TS_Y, f"{role}   {SPECIALIST}")
    if nation:
        c.drawString(519.02, TS_Y, nation)
    c.setFont("Helvetica-Bold", TS_SIZE)
    c.drawString(TS_X, 760.38, "1")
    c.drawString(54.14, 760.38, "Aino AAVIKKO")
    c.drawString(519.02, 760.38, "TL")


def make_pdf(path, *pages):
    """pages: callables drawing one page each on the canvas."""
    c = canvas.Canvas(str(path), pagesize=A4)
    for draw in pages:
        draw(c)
        c.showPage()
    c.save()
    return path


def stamp(tmp_path, src, kind="judge", name=TRAINEE):
    dst = tmp_path / "stamped.pdf"
    return ts.stamp_trainee_sheet(str(src), str(dst), kind, name), dst


# --- Reading the result ----------------------------------------------------

def stamp_cells(path):
    """Per page: the stamp's text cells (Liberation Sans) as [(text, x0, baseline, size, fontname)]."""
    pages = []
    for layout in extract_pages(str(path)):
        chars = sorted((c for c in _chars(layout) if "LiberationSans" in c.fontname),
                       key=lambda c: (-round(c.matrix[5], 1), c.x0))
        cells = []
        for ch in chars:
            last = cells[-1] if cells else None
            if last and abs(last[-1].matrix[5] - ch.matrix[5]) < 0.5 and ch.x0 - last[-1].x1 <= 4:
                last.append(ch)
            else:
                cells.append([ch])
        pages.append([("".join(c.get_text() for c in cell), cell[0].x0, cell[0].matrix[5], cell[0].size,
                       cell[0].fontname) for cell in cells])
    return pages


def collapse(text):
    return " ".join(text.split())


def stream_data(page):
    """The decoded data of each of a page's content streams, in order."""
    contents = page.raw_get("/Contents").get_object()
    streams = list(contents) if isinstance(contents, ArrayObject) else [contents]
    return [s.get_object().get_data() for s in streams]


def q_balance(page):
    depth = low = 0
    for _, op in page.get_contents().operations:
        depth += {b"q": 1, b"Q": -1}.get(op, 0)
        low = min(low, depth)
    return depth, low


# --- Judges ------------------------------------------------------------------

def test_judge_sheet_gets_the_trainee_on_every_page(tmp_path):
    src = make_pdf(tmp_path / "judge.pdf", judge_page, judge_page, judge_page)

    result, dst = stamp(tmp_path, src)

    assert result == (3, 0, 0)
    assert len(PdfReader(str(dst)).pages) == 3
    for cells in stamp_cells(dst):
        assert len(cells) == 2
        (label, lx, ly, lsize, lfont), (name, nx, ny, nsize, nfont) = cells
        assert label == "Judge Trainee" and name == TRAINEE
        assert lx == pytest.approx(ROLE_X, abs=0.05) and ly == pytest.approx(ROLE_Y, abs=0.05)
        assert nx == pytest.approx(NAME_X, abs=0.05) and ny == pytest.approx(ROLE_Y, abs=0.05)
        assert lsize == pytest.approx(SIZE, abs=0.05) and nsize == pytest.approx(SIZE, abs=0.05)
        assert lfont.endswith("LiberationSans-Bold") and nfont.endswith("LiberationSans-Bold")


def test_the_fsm_content_streams_stay_untouched(tmp_path):
    src = make_pdf(tmp_path / "judge.pdf", judge_page, judge_page)

    _, dst = stamp(tmp_path, src)

    before = PdfReader(str(src)).pages
    after = PdfReader(str(dst)).pages
    for old, new in zip(before, after, strict=True):
        data = stream_data(new)
        assert data[1:-1] == stream_data(old)          # FSM's own streams, between the wrappers
        assert data[0].strip(b"q\n") == b""
        assert data[-1].rstrip().endswith(b"q /FstTrainee0 Do Q")
        assert q_balance(new) == (0, 0)


def test_one_form_per_geometry_shared_by_the_pages(tmp_path):
    src = make_pdf(tmp_path / "judge.pdf", judge_page, judge_page, judge_page)

    _, dst = stamp(tmp_path, src)

    forms = {page["/Resources"]["/XObject"].raw_get("/FstTrainee0").idnum for page in PdfReader(str(dst)).pages}
    assert len(forms) == 1


def test_the_rule_under_the_name_is_redrawn_only_where_covered(tmp_path):
    src = make_pdf(tmp_path / "judge.pdf", judge_page)

    line = ts.find_role_line(next(iter(extract_pages(str(src)))), "judge")

    # the rule under the header (784.46) lies clear of the boxes and is left alone
    [(x0, x1, y, width, colour)] = line.rules
    name_box = line.boxes[1]
    assert x0 == pytest.approx(name_box[0] - 0.5) and x1 == pytest.approx(name_box[2] + 0.5)
    assert RULE_X0 < x0 and x1 < RULE_X1
    assert y == pytest.approx(ROLE_Y - RULE_DY, abs=0.01)
    assert width == pytest.approx(2 * FSM_SCALE, abs=1e-4)     # pdfminer reports it in page units
    assert colour == (0.0,)
    assert line.boxes[0][3] == pytest.approx(ROLE_Y + 0.95 * SIZE, abs=0.01)


def test_judge_sheet_without_names_puts_the_name_in_the_name_column(tmp_path):
    src = make_pdf(tmp_path / "judge.pdf", lambda c: judge_page(c, name=None))

    result, dst = stamp(tmp_path, src)

    assert result == (1, 0, 0)
    [[(label, lx, _, _, _), (name, nx, _, _, _)]] = stamp_cells(dst)
    assert label == "Judge Trainee" and name == TRAINEE
    assert nx == pytest.approx(ROLE_X + ts.NAME_CELL_DX, abs=0.05)


def test_pages_without_a_role_line_pass_through(tmp_path):
    src = make_pdf(tmp_path / "sheets.pdf", referee_page, referee_page, judge_page, judge_page)

    result, dst = stamp(tmp_path, src)

    assert result == (2, 2, 0)
    pages = stamp_cells(dst)
    assert pages[0] == [] and pages[1] == []
    assert [collapse(t) for t, *_ in pages[2]] == ["Judge Trainee", TRAINEE]
    reader = PdfReader(str(dst))
    for old, new in zip(PdfReader(str(src)).pages[:2], reader.pages[:2]):
        assert stream_data(new) == stream_data(old)
        assert "/XObject" not in new["/Resources"]


LONG_NAME = ("ÅSA-MARJATTA ÄRÖLÄ-ŠIMKUS WOLFGANG " * 3)[:80]


@pytest.mark.parametrize("kind, draw, x, size", [
    ("judge", judge_page, ROLE_X, SIZE),
    ("technical", specialist_page, TS_X, TS_SIZE),
])
def test_a_long_name_fits(tmp_path, kind, draw, x, size):
    src = make_pdf(tmp_path / "sheet.pdf", draw)
    assert len(LONG_NAME) == 80

    result, dst = stamp(tmp_path, src, kind=kind, name=LONG_NAME)

    assert result == (1, 0, 0)
    [cells] = stamp_cells(dst)
    text, _, _, fitted, _ = cells[-1]
    layout = next(iter(extract_pages(str(dst))))
    right = max(c.x1 for c in _chars(layout) if "LiberationSans" in c.fontname)
    assert right <= PAGE_W - x + 0.5
    assert fitted < size - 0.1 or text.endswith("…")


# --- Technical Specialists ---------------------------------------------------

@pytest.mark.parametrize("role, bold, nation, font", [
    ("Technical Specialist:", True, "FIN", "LiberationSans-Bold"),
    ("Technical Specialist 1:", False, "", "LiberationSans"),
    ("Technical Specialist 2:", True, "", "LiberationSans-Bold"),
])
def test_specialist_sheet_is_one_string(tmp_path, role, bold, nation, font):
    src = make_pdf(tmp_path / "ts.pdf", *[lambda c: specialist_page(c, role, bold, nation)] * 2)

    result, dst = stamp(tmp_path, src, kind="technical")

    assert result == (2, 0, 0)
    for cells in stamp_cells(dst):
        [(text, x, y, size, fontname)] = cells
        assert collapse(text) == f"Technical Trainee: {TRAINEE}"
        assert x == pytest.approx(TS_X, abs=0.05) and y == pytest.approx(TS_Y, abs=0.05)
        assert size == pytest.approx(TS_SIZE, abs=0.05)
        assert fontname.split("+")[-1] == font


def test_a_judge_line_is_not_a_specialist_line(tmp_path):
    src = make_pdf(tmp_path / "judge.pdf", judge_page)
    dst = tmp_path / "stamped.pdf"

    with pytest.raises(ts.UnsupportedLayout):
        ts.stamp_trainee_sheet(str(src), str(dst), "technical", TRAINEE)
    assert not dst.exists()


# --- Refusals ----------------------------------------------------------------

def test_no_role_line_at_all_raises(tmp_path):
    src = make_pdf(tmp_path / "referee.pdf", referee_page, referee_page)
    dst = tmp_path / "stamped.pdf"

    with pytest.raises(ts.UnsupportedLayout):
        ts.stamp_trainee_sheet(str(src), str(dst), "judge", TRAINEE)
    assert not dst.exists()


def test_a_band_under_the_role_line_raises(tmp_path):
    src = make_pdf(tmp_path / "judge.pdf", judge_page, lambda c: judge_page(c, band=True))
    dst = tmp_path / "stamped.pdf"

    with pytest.raises(ts.UnsupportedLayout):
        ts.stamp_trainee_sheet(str(src), str(dst), "judge", TRAINEE)
    assert not dst.exists()


def test_a_dashed_rule_under_the_role_line_raises(tmp_path):
    def page(c):
        judge_page(c)
        c.setDash([3, 2])
        c.setLineWidth(0.5)
        c.line(NAME_X, ROLE_Y - 0.5, 400, ROLE_Y - 0.5)

    src = make_pdf(tmp_path / "judge.pdf", page)
    dst = tmp_path / "stamped.pdf"

    with pytest.raises(ts.UnsupportedLayout):
        ts.stamp_trainee_sheet(str(src), str(dst), "judge", TRAINEE)
    assert not dst.exists()


def test_a_role_line_that_cannot_be_placed_raises(tmp_path):
    """A rotated page prints the role line but pdfminer's positions are turned: never half-stamp."""
    src = make_pdf(tmp_path / "judge.pdf", judge_page, judge_page)
    writer = PdfWriter(clone_from=str(src))
    writer.pages[1].rotate(90)
    rotated = tmp_path / "rotated.pdf"
    writer.write(str(rotated))
    dst = tmp_path / "stamped.pdf"

    with pytest.raises(ts.UnsupportedLayout):
        ts.stamp_trainee_sheet(str(rotated), str(dst), "judge", TRAINEE)
    assert not dst.exists()


def test_an_unreadable_source_raises(tmp_path):
    src = tmp_path / "broken.pdf"
    src.write_bytes(b"not a pdf")
    dst = tmp_path / "stamped.pdf"

    with pytest.raises(Exception):
        ts.stamp_trainee_sheet(str(src), str(dst), "judge", TRAINEE)
    assert not dst.exists()


def test_errors_carry_no_names(tmp_path):
    src = make_pdf(tmp_path / "judge.pdf", lambda c: judge_page(c, band=True))

    with pytest.raises(ts.UnsupportedLayout) as err:
        ts.stamp_trainee_sheet(str(src), str(tmp_path / "out.pdf"), "judge", TRAINEE)
    assert TRAINEE not in str(err.value) and "JUDGE" not in str(err.value)


@pytest.mark.parametrize("kind, name", [("referee", TRAINEE), ("judge", "   "), ("judge", None)])
def test_bad_arguments_raise(tmp_path, kind, name):
    src = make_pdf(tmp_path / "judge.pdf", judge_page)

    with pytest.raises(ValueError):
        ts.stamp_trainee_sheet(str(src), str(tmp_path / "out.pdf"), kind, name)
    assert not (tmp_path / "out.pdf").exists()


# --- Page structure ----------------------------------------------------------

def _source_role_line(path):
    """Where pdfminer sees the source's role line: the stamp must land exactly there."""
    line = ts.find_role_line(next(iter(extract_pages(str(path)))), "judge")
    return line.x, line.baseline


@pytest.mark.parametrize("prefix, suffix", [
    ("", "q 1 0 0 1 60 -40 cm 0.5 g"),     # a q left open at the end, with a shift and a fill colour
    ("Q Q", ""),                           # more Q's than q's
    ("1 0 0 1 10 -20 cm", ""),             # a top-level cm that stays in force
])
def test_the_stamp_is_drawn_in_the_default_state(tmp_path, prefix, suffix):
    def page(c):
        if prefix:
            c._code.append(prefix)
        judge_page(c)
        if suffix:
            c._code.append(suffix)

    src = make_pdf(tmp_path / "judge.pdf", page)
    x, baseline = _source_role_line(src)

    result, dst = stamp(tmp_path, src)

    assert result == (1, 0, 0)
    [[(label, lx, ly, _, _), (name, _, ny, _, _)]] = stamp_cells(dst)
    assert label == "Judge Trainee" and name == TRAINEE
    assert lx == pytest.approx(x, abs=0.05) and ly == pytest.approx(baseline, abs=0.05)
    [new] = PdfReader(str(dst)).pages
    depth, low = q_balance(new)
    assert depth == 0 and low >= 0
    assert stream_data(new)[1:-1] == stream_data(PdfReader(str(src)).pages[0])


def _two_pages_one_resources(tmp_path, inherit):
    """A judge page and a referee page sharing one indirect /Resources, on the pages or on /Pages."""
    src = make_pdf(tmp_path / "plain.pdf", judge_page, referee_page)
    writer = PdfWriter(clone_from=str(src))
    shared = DictionaryObject()
    for page in writer.pages:
        resources = page["/Resources"].get_object()
        for key in resources:
            if key == "/Font":
                fonts = shared.setdefault(NameObject("/Font"), DictionaryObject())
                for font in resources["/Font"]:
                    fonts[NameObject(font)] = resources["/Font"].raw_get(font)
            else:
                shared[NameObject(key)] = resources.raw_get(key)
    ref = writer._add_object(shared)
    for page in writer.pages:
        if inherit:
            del page[NameObject("/Resources")]
        else:
            page[NameObject("/Resources")] = ref
    if inherit:
        writer._root_object["/Pages"][NameObject("/Resources")] = ref
    path = tmp_path / "shared.pdf"
    writer.write(str(path))
    return path


@pytest.mark.parametrize("inherit", [False, True], ids=["shared", "inherited"])
def test_shared_or_inherited_resources_are_not_changed(tmp_path, inherit):
    src = _two_pages_one_resources(tmp_path, inherit)
    reader = PdfReader(str(src))
    first_kid = reader.trailer["/Root"]["/Pages"]["/Kids"][0].get_object()
    assert ("/Resources" in first_kid) is not inherit

    result, dst = stamp(tmp_path, src)

    assert result == (1, 1, 0)
    out = PdfReader(str(dst))
    stamped, plain = out.pages
    assert "/FstTrainee0" in stamped["/Resources"]["/XObject"]
    assert "/XObject" not in plain["/Resources"]
    pages = stamp_cells(dst)
    assert [t for t, *_ in pages[0]] == ["Judge Trainee", TRAINEE] and pages[1] == []


def test_an_existing_xobject_name_is_not_reused(tmp_path):
    src = make_pdf(tmp_path / "judge.pdf", judge_page)
    writer = PdfWriter(clone_from=str(src))
    own = DecodedStreamObject()
    own.set_data(b"% FSM's own form\n")
    own[NameObject("/Type")] = NameObject("/XObject")
    own[NameObject("/Subtype")] = NameObject("/Form")
    own[NameObject("/BBox")] = ArrayObject(FloatObject(v) for v in (0, 0, 1, 1))
    page = writer.pages[0]
    page["/Resources"].get_object()[NameObject("/XObject")] = DictionaryObject(
        {NameObject("/FstTrainee0"): writer._add_object(own)})
    taken = tmp_path / "taken.pdf"
    writer.write(str(taken))

    result, dst = stamp(tmp_path, taken)

    assert result == (1, 0, 0)
    [new] = PdfReader(str(dst)).pages
    xobjects = new["/Resources"]["/XObject"]
    assert xobjects["/FstTrainee0"].get_data() == b"% FSM's own form\n"
    assert "/FstTrainee1" in xobjects
    assert stream_data(new)[-1].rstrip().endswith(b"q /FstTrainee1 Do Q")
    assert [t for t, *_ in stamp_cells(dst)[0]] == ["Judge Trainee", TRAINEE]


# --- In the packet -----------------------------------------------------------

def test_the_stamp_survives_the_packet_merge(tmp_path):
    src = make_pdf(tmp_path / "judge.pdf", referee_page, judge_page, judge_page)
    cover = make_pdf(tmp_path / "cover.pdf", lambda c: c.drawString(100, 100, "Cover"))
    _, dst = stamp(tmp_path, src)
    packet = tmp_path / "packet.pdf"

    assert merge_pdfs(str(packet), [str(cover), str(dst), str(dst)]) == 7

    pages = stamp_cells(packet)
    labels = [[t for t, *_ in cells] for cells in pages]
    assert labels == [[], [], ["Judge Trainee", TRAINEE], ["Judge Trainee", TRAINEE],
                      [], ["Judge Trainee", TRAINEE], ["Judge Trainee", TRAINEE]]


def test_the_temporary_file_does_not_stay_behind(tmp_path):
    src = make_pdf(tmp_path / "judge.pdf", judge_page)
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    ts.stamp_trainee_sheet(str(src), str(out_dir / "stamped.pdf"), "judge", TRAINEE)

    assert os.listdir(out_dir) == ["stamped.pdf"]
