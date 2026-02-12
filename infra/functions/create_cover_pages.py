import os
import re
import sys
from datetime import datetime
from io import BytesIO
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm

# --- Helper Functions ---

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

def create_cover_pdf(output_path, competition_name, date_obj, person_name):
    c = canvas.Canvas(output_path, pagesize=landscape(A4))
    width, height = landscape(A4)
    
    # Layout settings
    # "second column should have some space after the center of page"
    # Center X is width/2. Let's start text at width/2 + 10mm
    x_pos = width / 2 + 10 * mm
    
    # Vertical position: Middle of page
    y_start = height / 2 + 20 * mm
    line_height = 10 * mm # Space between lines
    
    c.setFont("Helvetica", 12)
    
    # Draw Competition Name
    c.drawString(x_pos, y_start, competition_name)
    
    # Draw Date (dd.MM.yyyy)
    date_str = date_obj.strftime("%d.%m.%Y")
    c.drawString(x_pos, y_start - line_height, date_str)
    
    # Draw Person Name
    c.drawString(x_pos, y_start - 2 * line_height, person_name)
    
    # Footer
    footer_y_line = 25 * mm
    footer_y_text = 20 * mm
    x_end = width - 10 * mm
    
    c.setLineWidth(0.5)
    c.line(x_pos, footer_y_line, x_end, footer_y_line)
    
    c.setFont("Helvetica", 8)
    c.drawRightString(x_end, footer_y_text, "Created with JudgePaperCreator - Supporting the Figure Skating Community")
    
    c.save()

def create_segment_cover_pdf(output_path, competition_name, date_obj, segment_name, category_name=None, withdrawn_competitors=None):
    # Vertical A4 for Segment Cover
    c = canvas.Canvas(output_path, pagesize=A4)
    width, height = A4
    
    # Center X
    center_x = width / 2
    
    # Layout (Top-down)
    y_pos = height - 100 * mm
    
    c.setFont("Helvetica-Bold", 16)
    c.drawCentredString(center_x, y_pos, competition_name)
    
    y_pos -= 20 * mm
    c.setFont("Helvetica", 14)
    date_str = date_obj.strftime("%d.%m.%Y")
    c.drawCentredString(center_x, y_pos, date_str)
    
    # Category name (if provided)
    if category_name:
        y_pos -= 30 * mm
        c.setFont("Helvetica-Bold", 18)
        c.drawCentredString(center_x, y_pos, category_name)
    
    y_pos -= 25 * mm
    c.setFont("Helvetica-Bold", 20)
    c.drawCentredString(center_x, y_pos, segment_name)
    
    # Withdrawn competitors list (only if there are any)
    if withdrawn_competitors:
        y_pos -= 20 * mm
        c.setFont("Helvetica-Bold", 14)
        c.drawCentredString(center_x, y_pos, "Withdrawn:")
        
        c.setFont("Helvetica", 12)
        for name in withdrawn_competitors:
            y_pos -= 8 * mm
            c.drawCentredString(center_x, y_pos, name)
    
    # Footer
    footer_y_line = 25 * mm
    footer_y_text = 20 * mm
    x_start = 10 * mm
    x_end = width - 10 * mm
    
    c.setLineWidth(0.5)
    c.line(x_start, footer_y_line, x_end, footer_y_line)
    
    c.setFont("Helvetica", 8)
    c.drawRightString(x_end, footer_y_text, "Created with JudgePaperCreator - Supporting the Figure Skating Community")
    
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
                
                # Merge overlay onto the original page
                overlay_reader = PdfReader(overlay_buffer)
                page.merge_page(overlay_reader.pages[0])
            
            writer.add_page(page)
        
        with open(output_pdf_path, "wb") as f:
            writer.write(f)
        
        print(f"  Created strikethrough StartList: {os.path.basename(output_pdf_path)}")
        return True
        
    except Exception as e:
        print(f"  Error creating strikethrough StartList: {e}")
        return False
