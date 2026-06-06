import os
import re
import sys
from datetime import datetime
from pypdf import PdfReader, PdfWriter

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

def get_date_from_start_list(pdf_path):
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
                    return dt.strftime("%Y%m%d")
                except ValueError:
                    pass
    except Exception as e:
        print(f"Error extracting date from {pdf_path}: {e}")
    return None

def get_first_start_time_from_start_list(pdf_path):
    """
    Extract the first skater/team start time from a StartListwithTimes PDF.
    The table rows have two HH:MM:SS columns (start-time and end-time windows).
    Returns the first matched time as "HH:MM" or None.
    """
    try:
        reader = PdfReader(pdf_path)
        if len(reader.pages) == 0:
            return None
        text = reader.pages[0].extract_text(extraction_mode="layout")
        if not text:
            return None
        # Match table rows that contain two time columns.
        # Real PDFs use dot separators (11.10.00-11.15.00) or colon separators
        # (11:10:00  11:15:00). Handle both, with optional dash/space between.
        time_row_pattern = re.compile(
            r'(\d{1,2}[.:]+\d{2}[.:]+\d{2})'
            r'[\s\-]+'
            r'(\d{1,2}[.:]+\d{2}[.:]+\d{2})'
        )
        for line in text.split('\n'):
            m = time_row_pattern.search(line)
            if m:
                # Found the first skater row — extract start time
                raw = m.group(1)
                # Normalize: replace dots with colons
                raw = raw.replace('.', ':')
                parts = raw.split(':')
                hh = parts[0].zfill(2)
                mm = parts[1]
                return f"{hh}:{mm}"
    except Exception as e:
        print(f"Error extracting start time from {pdf_path}: {e}")
    return None


def get_panel_info(pdf_path):
    # Returns list of (role, name, original_name)
    # Handles TS1 vs TS2 distinction
    panel_info = []
    try:
        reader = PdfReader(pdf_path)
        ts_count = 0
        
        for page in reader.pages:
            text = page.extract_text(extraction_mode="layout")
            lines = text.split('\n')
            
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                
                parts = re.split(r'\s{2,}', line)
                
                if len(parts) >= 2:
                    role_part = parts[0].strip()
                    name_part = parts[1].strip()
                    
                    # Identify Role
                    role = None
                    if role_part.startswith("Referee"):
                        role = "referee"
                    elif role_part.startswith("Judge"):
                        role = "judge"
                    elif role_part.startswith("Technical Controller"):
                        role = "technical_controller"
                    elif role_part.startswith("Technical Specialist"):
                        ts_count += 1
                        if ts_count == 1:
                            role = "technical_specialist_1"
                        else:
                            role = "technical_specialist_2"
                    elif role_part.startswith("Data Operator"):
                        role = "data_operator"
                    elif role_part.startswith("Replay Operator"):
                        role = "replay_operator"
                    
                    if role:
                        clean_name = re.sub(r'^(Ms\.|Mr\.|Dr\.)\s*', '', name_part).strip()
                        panel_info.append((role, clean_name))
                        
    except Exception as e:
        print(f"Error reading panel info from {pdf_path}: {e}")
    return panel_info

def merge_pdfs(output_path, file_list):
    writer = PdfWriter()
    
    for file_path in file_list:
        if os.path.exists(file_path):
            try:
                reader = PdfReader(file_path)
                for page in reader.pages:
                    writer.add_page(page)
                print(f"  Added: {os.path.basename(file_path)}")
            except Exception as e:
                print(f"  Error adding {os.path.basename(file_path)}: {e}")
        else:
            print(f"  Warning: File not found: {file_path}")
            
    with open(output_path, "wb") as f:
        writer.write(f)
    print(f"Created: {output_path}")
    # Page count of the merged PDF — used by the processor for usage statistics.
    return len(writer.pages)
