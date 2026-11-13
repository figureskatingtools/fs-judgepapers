import os
import re
import sys
from datetime import datetime
from io import BytesIO
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# --- Helper Functions ---

FOOTER_TEXT = "Created with Figureskatingtools.com - Supporting the Figure Skating Community"
LOGO_PATH = os.path.join(os.path.dirname(__file__), "assets", "logo.png")
FONTS_DIR = os.path.join(os.path.dirname(__file__), "assets", "fonts")


def _register_fonts():
    # Best-effort: fall back to Helvetica rather than fail cover generation
    try:
        pdfmetrics.registerFont(TTFont("Raleway", os.path.join(FONTS_DIR, "Raleway-Regular.ttf")))
        pdfmetrics.registerFont(TTFont("Raleway-Bold", os.path.join(FONTS_DIR, "Raleway-Bold.ttf")))
        return "Raleway", "Raleway-Bold"
    except Exception as e:
        print(f"Could not register Raleway fonts, falling back to Helvetica: {e}")
        return "Helvetica", "Helvetica-Bold"


FONT_REGULAR, FONT_BOLD = _register_fonts()


def _register_table_fonts():
    # Liberation Sans: lining figures (times line up) and Latin Extended for
    # names; Raleway has neither. Also used by combined_schedule. Best-effort.
    try:
        pdfmetrics.registerFont(TTFont("LiberationSans", os.path.join(FONTS_DIR, "LiberationSans-Regular.ttf")))
        pdfmetrics.registerFont(TTFont("LiberationSans-Bold", os.path.join(FONTS_DIR, "LiberationSans-Bold.ttf")))
        return "LiberationSans", "LiberationSans-Bold"
    except Exception as e:
        print(f"Could not register Liberation Sans fonts, falling back to Helvetica: {e}")
        return "Helvetica", "Helvetica-Bold"


TABLE_FONT, TABLE_FONT_BOLD = _register_table_fonts()


def _fit(c, text, font, size, max_width, min_size=6.5):
    """Shrink, then truncate with an ellipsis, so text fits max_width."""
    while size > min_size and c.stringWidth(text, font, size) > max_width:
        size -= 0.5
    if c.stringWidth(text, font, size) > max_width:
        while text and c.stringWidth(text + "…", font, size) > max_width:
            text = text[:-1]
        text += "…"
    return text, size


def _font_for(text, font, fallback):
    """font, or fallback when font lacks a glyph of text (Raleway has no Latin Extended: Č, Ł, …)."""
    try:
        glyphs = pdfmetrics.getFont(font).face.charToGlyph
    except AttributeError:
        return font  # a standard font (Helvetica) after a failed registration
    return font if all(ord(ch) in glyphs for ch in text if not ch.isspace()) else fallback

def slugify(text):
    text = text.lower()
    replacements = {
        'ä': 'a', 'ö': 'o', 'å': 'a',
        'ü': 'u', 'é': 'e', 'è': 'e',
        ' ': '_'
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    text = re.sub(r'[^a-z0-9_]', '', text)
    return text

# --- Extraction Logic ---

def extract_segment_name_from_pdf(pdf_path):
    # Extracts the second line from the PDF which is usually the segment name
    try:
        reader = PdfReader(pdf_path)
        if len(reader.pages) > 0:
            text = reader.pages[0].extract_text(extraction_mode="layout")
            lines = text.split('\n')
            
            # Filter distinct non-empty lines
            non_empty_lines = [line.strip() for line in lines if line.strip()]
            
            if len(non_empty_lines) >= 2:
                return non_empty_lines[1]
    except Exception as e:
        print(f"Error extracting segment name from {pdf_path}: {e}")
    return None

def extract_judges_from_pdf(pdf_path):
    # Logic from extract_judges.py
    try:
        reader = PdfReader(pdf_path)
        judges = []
        
        for page in reader.pages:
            text = page.extract_text(extraction_mode="layout")
            lines = text.split('\n')
            
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                
                parts = re.split(r'\s{2,}', line)
                
                if len(parts) >= 2:
                    role = parts[0].strip()
                    name_part = parts[1].strip()
                    
                    valid_role_prefixes = ["Judge", "Referee", "Technical", "Data", "Replay"]
                    if not any(role.startswith(prefix) for prefix in valid_role_prefixes):
                        continue
                    
                    # Clean Name
                    clean_name = re.sub(r'^(Ms\.|Mr\.|Dr\.)\s*', '', name_part).strip()
                    
                    # We only need the name for the cover page, but we return role too just in case
                    judges.append((role, clean_name))
        return judges
    except Exception as e:
        print(f"Error extracting judges from {pdf_path}: {e}")
        return []

def extract_date_from_pdf(pdf_path):
    # Logic from extract_date_segment.py
    try:
        reader = PdfReader(pdf_path)
        text = ""
        if len(reader.pages) > 0:
            text = reader.pages[0].extract_text(extraction_mode="layout")
        
        lines = text.split('\n')
        for line in lines:
            match = re.search(r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})', line, re.IGNORECASE)
            if match:
                day, month, year = match.groups()
                date_str = f"{day} {month} {year}"
                try:
                    dt = datetime.strptime(date_str, "%d %B %Y")
                    return dt
                except ValueError:
                    pass
    except Exception as e:
        print(f"Error extracting date from {pdf_path}: {e}")
    return None

def extract_title_from_pdf(pdf_path):
    # Logic from extract_competition_title.py
    try:
        reader = PdfReader(pdf_path)
        if len(reader.pages) > 0:
            text = reader.pages[0].extract_text(extraction_mode="layout")
            lines = text.split('\n')
            for line in lines:
                if line.strip():
                    return line.strip()
    except Exception as e:
        print(f"Error extracting title from {pdf_path}: {e}")
    return None

# --- PDF Generation ---

def _draw_footer(c, x_start, x_end, footer_y_line=25 * mm, footer_y_text=20 * mm):
    c.setLineWidth(0.5)
    c.line(x_start, footer_y_line, x_end, footer_y_line)

    # Shrink font from 8pt until the text fits the available width
    size = 8
    while size > 5 and c.stringWidth(FOOTER_TEXT, FONT_REGULAR, size) > (x_end - x_start):
        size -= 0.5
    c.setFont(FONT_REGULAR, size)
    c.drawRightString(x_end, footer_y_text, FOOTER_TEXT)

# Role groups of the day-document check block: key -> (singular, plural)
ROLE_GROUP_NAMES = {
    "judge": ("judge", "judges"),
    "referee": ("referee", "referees"),
    "technical_controller": ("technical controller", "technical controllers"),
    "technical_specialist": ("technical specialist", "technical specialists"),
    "data_operator": ("data operator", "data operators"),
    "replay_operator": ("replay operator", "replay operators"),
}

COVER_TEXT_GREY = 0.35
COVER_RULE_GREY = 0.82
SUMMARY_GREY = 0.4


def create_cover_pdf(output_path, competition_name, date_obj, person_name, segments=None, day_summary=None):
    """
    The personal cover: landscape A4 folded down the middle, the right half
    being the front. segments = [(time 'HH:MM' or None, 'Category, Segment',
    role label)] in running order, listed under the name. day_summary (see
    _draw_day_summary) goes to the bottom-left corner, the back cover once
    folded: only the first cover of the day document gets one.
    """
    c = canvas.Canvas(output_path, pagesize=landscape(A4))
    width, height = landscape(A4)

    # Layout settings
    # "second column should have some space after the center of page"
    # Center X is width/2. Let's start text at width/2 + 10mm
    x_pos = width / 2 + 10 * mm
    x_end = width - 10 * mm

    # Logo header at the top of the page (best-effort; never fail cover generation over the asset)
    logo_w = 50 * mm
    logo_h = logo_w * 412 / 655  # preserve the 655x412 px aspect ratio
    try:
        c.drawImage(LOGO_PATH, x_pos, height - 10 * mm - logo_h, width=logo_w, height=logo_h,
                    preserveAspectRatio=True, anchor='sw', mask='auto')
    except Exception as e:
        print(f"Could not draw logo on cover page: {e}")

    date_str = date_obj.strftime("%d.%m.%Y")

    title_font = _font_for(competition_name, FONT_REGULAR, TABLE_FONT)
    if segments:
        # Name block right under the logo, the segments at the bottom of the
        # space below it
        c.setFont(title_font, 12)
        c.drawString(x_pos, 155 * mm, competition_name)
        c.setFont(FONT_REGULAR, 12)
        c.drawString(x_pos, 147 * mm, date_str)
        name_font = _font_for(person_name, FONT_BOLD, TABLE_FONT_BOLD)
        name, size = _fit(c, person_name, name_font, 16, x_end - x_pos, min_size=11)
        c.setFont(name_font, size)
        c.drawString(x_pos, 136 * mm, name)
        _draw_segment_table(c, segments, x_pos, x_end, top=124 * mm, bottom=35 * mm)
    else:
        # Vertical position: Middle of page
        y_start = height / 2 + 20 * mm
        line_height = 10 * mm # Space between lines
        c.setFont(title_font, 12)
        c.drawString(x_pos, y_start, competition_name)
        c.setFont(FONT_REGULAR, 12)
        c.drawString(x_pos, y_start - line_height, date_str)
        c.setFont(_font_for(person_name, FONT_REGULAR, TABLE_FONT), 12)
        c.drawString(x_pos, y_start - 2 * line_height, person_name)

    if day_summary:
        _draw_day_summary(c, day_summary, 10 * mm, width / 2 - 10 * mm)

    # Footer
    _draw_footer(c, x_pos, x_end)

    c.save()


def _draw_segment_table(c, segments, x_start, x_end, top, bottom):
    """
    One row per segment: start time (bold), 'Category, Segment', the role
    right-aligned in grey, hairlines between the rows. The table sits on
    `bottom` and grows up to `top`: rows shrink from 8 mm to 5.5 mm to fit,
    past that the last row says how many were left out.
    """
    available = top - bottom
    row_h = max(5.5 * mm, min(8 * mm, available / len(segments)))
    # From the minimum height: available // row_h can round a row that was
    # sized to fit exactly (available / n) down to n - 1
    max_rows = int(available // (5.5 * mm))
    rows = list(segments)
    hidden = 0
    if len(rows) > max_rows:
        hidden = len(rows) - (max_rows - 1)
        rows = rows[:max_rows - 1]

    top = bottom + (len(rows) + (1 if hidden else 0)) * row_h
    c.setLineWidth(0.5)
    c.line(x_start, top, x_end, top)

    size = min(10, row_h * 0.45)
    time_w = c.stringWidth("00:00", TABLE_FONT_BOLD, size) + 4 * mm
    role_w = max(c.stringWidth(role or "", TABLE_FONT, size) for _, _, role in rows) if rows else 0
    label_x = x_start + time_w
    label_max = x_end - label_x - role_w - 4 * mm

    y = top
    for i, (time, label, role) in enumerate(rows):
        baseline = y - row_h / 2 - size * 0.35
        c.setFillGray(0)
        c.setFont(TABLE_FONT_BOLD, size)
        c.drawString(x_start, baseline, time or "–")
        text, label_size = _fit(c, label or "", TABLE_FONT, size, label_max)
        c.setFont(TABLE_FONT, label_size)
        c.drawString(label_x, baseline, text)
        if role:
            c.setFillGray(COVER_TEXT_GREY)
            c.setFont(TABLE_FONT, size)
            c.drawRightString(x_end, baseline, role)
        y -= row_h
        if i < len(rows) - 1 or hidden:
            c.setStrokeGray(COVER_RULE_GREY)
            c.setLineWidth(0.3)
            c.line(x_start, y, x_end, y)
    if hidden:
        c.setFillGray(COVER_TEXT_GREY)
        c.setFont(TABLE_FONT, size)
        c.drawString(label_x, y - row_h / 2 - size * 0.35, f"+ {hidden} more")
    c.setFillGray(0)
    c.setStrokeGray(0)


def _format_role_groups(roles):
    parts = []
    for key, count in roles:
        singular, plural = ROLE_GROUP_NAMES.get(key, (key, key))
        parts.append(f"{count} {singular if count == 1 else plural}")
    return " · ".join(parts)


def _draw_day_summary(c, summary, x_start, x_end):
    """
    Small grey check block for the Results Service folding the printed day
    document, at the bottom-left (the back cover once folded). summary:
    {date (datetime), packets, roles [(ROLE_GROUP_NAMES key, people)],
     categories, segments, start, end ('HH:MM' or None), pages, file_name}
    """
    date = summary["date"]
    start, end = summary.get("start"), summary.get("end")
    schedule = f"{start or '?'}–{end or '?'}" if (start or end) else "–"
    lines = [
        (TABLE_FONT_BOLD, f"RESULTS SERVICE CHECK · {date.strftime('%A')} {date.strftime('%d.%m.%Y')}"),
        (TABLE_FONT, f"Packets {summary['packets']}"
                     + (f": {_format_role_groups(summary['roles'])}" if summary.get("roles") else "")),
        (TABLE_FONT, f"Categories {summary['categories']} · Segments {summary['segments']} · Schedule {schedule}"),
        (TABLE_FONT, f"Pages {summary['pages']} · {summary['file_name']}"),
    ]
    size, leading = 7, 9
    y = 12 * mm + leading * (len(lines) - 1)
    c.setFillGray(SUMMARY_GREY)
    for font, text in lines:
        text, line_size = _fit(c, text, font, size, x_end - x_start, min_size=5.5)
        c.setFont(font, line_size)
        c.drawString(x_start, y, text)
        y -= leading
    c.setFillGray(0)


def create_segment_cover_pdf(output_path, competition_name, date_obj, segment_name, category_name=None, withdrawn_competitors=None):
    # Vertical A4 for Segment Cover
    c = canvas.Canvas(output_path, pagesize=A4)
    width, height = A4
    
    # Center X
    center_x = width / 2
    
    # Layout (Top-down)
    y_pos = height - 100 * mm
    
    c.setFont(FONT_BOLD, 16)
    c.drawCentredString(center_x, y_pos, competition_name)
    
    y_pos -= 20 * mm
    c.setFont(FONT_REGULAR, 14)
    date_str = date_obj.strftime("%d.%m.%Y")
    c.drawCentredString(center_x, y_pos, date_str)
    
    # Category name (if provided)
    if category_name:
        y_pos -= 30 * mm
        c.setFont(FONT_BOLD, 18)
        c.drawCentredString(center_x, y_pos, category_name)
    
    y_pos -= 25 * mm
    c.setFont(FONT_BOLD, 20)
    c.drawCentredString(center_x, y_pos, segment_name)
    
    # Withdrawn competitors list (only if there are any)
    if withdrawn_competitors:
        y_pos -= 20 * mm
        c.setFont(FONT_BOLD, 14)
        c.drawCentredString(center_x, y_pos, "Withdrawn:")
        
        c.setFont(FONT_REGULAR, 12)
        for name in withdrawn_competitors:
            y_pos -= 8 * mm
            c.drawCentredString(center_x, y_pos, name)
    
    # Footer
    _draw_footer(c, 10 * mm, width - 10 * mm)

    c.save()


def create_start_list_with_strikethrough(input_pdf_path, output_pdf_path, withdrawn_names):
    """
    Create a modified copy of a StartListwithTimes PDF with strikethrough lines
    drawn over rows that contain withdrawn competitors.
    
    Args:
        input_pdf_path: path to the original StartListwithTimes PDF
        output_pdf_path: path to write the modified PDF
        withdrawn_names: list of competitor names to strike through
    """
    if not withdrawn_names or not os.path.exists(input_pdf_path):
        return False
    
    try:
        reader = PdfReader(input_pdf_path)
        writer = PdfWriter()
        
        # Normalize withdrawn names for matching (uppercase, no extra spaces)
        wd_names_upper = [n.upper().strip() for n in withdrawn_names]
        
        for page_num, page in enumerate(reader.pages):
            page_width = float(page.mediabox.width)
            page_height = float(page.mediabox.height)
            
            # Collect text positions using visitor_text
            text_positions = []  # list of (text, x, y) from visitor callbacks
            
            def visitor_callback(text, cm, tm, font_dict, font_size):
                if text and text.strip():
                    # tm[4] = x position, tm[5] = y position
                    text_positions.append((text.strip(), tm[4], tm[5]))
            
            page.extract_text(visitor_text=visitor_callback)
            
            # Find Y coordinates of lines containing withdrawn competitor names
            strikethrough_y_positions = []
            
            for wd_name in wd_names_upper:
                # Split the withdrawn name into parts for flexible matching
                wd_parts = wd_name.split()
                
                for text, x, y in text_positions:
                    text_upper = text.upper().strip()
                    if not text_upper:
                        continue
                    
                    # Check if this text fragment contains a significant part of the name
                    # Try matching last name (usually first part) or full name
                    matched = False
                    if len(wd_parts) >= 1:
                        # Check if any name part appears in the text
                        for part in wd_parts:
                            if len(part) >= 3 and part in text_upper:
                                matched = True
                                break
                    
                    if matched:
                        strikethrough_y_positions.append(y)
            
            if strikethrough_y_positions:
                # Create overlay with strikethrough lines
                overlay_buffer = BytesIO()
                overlay_canvas = canvas.Canvas(overlay_buffer, pagesize=(page_width, page_height))
                
                overlay_canvas.setStrokeColorRGB(0, 0, 0)  # Black
                overlay_canvas.setLineWidth(1)
                
                # Deduplicate Y positions (group nearby values within 2 points)
                unique_y = []
                for y in sorted(set(strikethrough_y_positions)):
                    if not unique_y or abs(y - unique_y[-1]) > 2:
                        unique_y.append(y)
                
                margin_left = 30
                margin_right = page_width - 30
                
                for y in unique_y:
                    # Draw strikethrough at the middle of the text line
                    line_y = y + 3  # Slightly above baseline
                    overlay_canvas.line(margin_left, line_y, margin_right, line_y)
                
                overlay_canvas.save()
                overlay_buffer.seek(0)
                
                # Merge overlay onto the page once it belongs to the writer
                # (pypdf 7 refuses to merge into a reader's page)
                overlay_reader = PdfReader(overlay_buffer)
                writer.add_page(page).merge_page(overlay_reader.pages[0])
            else:
                writer.add_page(page)
        
        with open(output_pdf_path, "wb") as f:
            writer.write(f)
        
        print(f"  Created strikethrough StartList: {os.path.basename(output_pdf_path)}")
        return True
        
    except Exception as e:
        print(f"  Error creating strikethrough StartList: {e}")
        return False
