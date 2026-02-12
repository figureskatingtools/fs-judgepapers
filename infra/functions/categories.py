"""
Categories module - provides table-driven competition category lookups.

The 'categories' Azure Table holds one row per competition category with:
  - PartitionKey: CompetitionType display name (e.g. "Figure skating")
  - RowKey: Abbreviation - the filename prefix (e.g. "FSKWSINGLES-ASILMW")
  - DisplayName: Human-readable English name (e.g. "A-Silmut, Girls")
  - DisplayNameFi: Human-readable Finnish name (e.g. "A-Silmut, Tytöt")
  - JudgingMethod: "ISU" or "MUPI"
"""

import logging
import time

# Module-level cache
_categories_cache = None
_categories_cache_time = 0
_CACHE_TTL_SECONDS = 300  # 5 minutes


def load_categories(table_client):
    """
    Fetches all rows from the 'categories' table and returns them as a list
    of dicts. Results are cached in memory for _CACHE_TTL_SECONDS.

    Each dict has keys: abbreviation, displayName, displayNameFi, judgingMethod, competitionType
    """
    global _categories_cache, _categories_cache_time

    now = time.time()
    if _categories_cache is not None and (now - _categories_cache_time) < _CACHE_TTL_SECONDS:
        return _categories_cache

    categories = []
    try:
        entities = table_client.list_entities()
        for entity in entities:
            display_name = entity.get("DisplayName", entity["RowKey"])
            categories.append({
                "abbreviation": entity["RowKey"],
                "displayName": display_name,
                "displayNameFi": entity.get("DisplayNameFi", display_name),
                "judgingMethod": entity.get("JudgingMethod", "ISU"),
                "competitionType": entity.get("PartitionKey", "Unknown"),
            })
    except Exception as e:
        logging.error(f"Failed to load categories from table: {e}")
        # Return stale cache if available
        if _categories_cache is not None:
            logging.warning("Returning stale categories cache")
            return _categories_cache
        return []

    # Sort by abbreviation length descending for longest-prefix-match
    categories.sort(key=lambda c: len(c["abbreviation"]), reverse=True)

    _categories_cache = categories
    _categories_cache_time = now
    logging.info(f"Loaded {len(categories)} categories from table")
    return categories


def match_category(filename, categories):
    """
    Finds the category whose abbreviation is the longest prefix match for
    the given filename. Returns the matching category dict or None.
    
    Categories must be sorted by abbreviation length descending (as
    returned by load_categories).
    """
    for cat in categories:
        if filename.startswith(cat["abbreviation"]):
            return cat
    return None


def invalidate_cache():
    """Force the next load_categories call to re-fetch from table."""
    global _categories_cache, _categories_cache_time
    _categories_cache = None
    _categories_cache_time = 0


def parse_filename_generic(filename, categories):
    """
    Generic filename parser that works for any competition type by matching
    the filename prefix against the categories table.

    Returns a dict with keys: type, category, categoryCode, judgingMethod,
    segment, raw_segment, suffix, filename  — or None if unparseable.

    Filename format (after abbreviation prefix):
        {ABBREVIATION}{DASHES}{OPTIONAL_SPLIT_DIGITS}{SEGMENT_PART}--_{SUFFIX}.pdf

    Segment detection: looks for QUAL or FNL in the remainder after stripping
    the abbreviation. Split/group numbers (e.g. '01') detected after the
    abbreviation, separated by dashes.
    """
    if "/" in filename:
        filename = filename.split("/")[-1]

    if not filename.endswith(".pdf"):
        return None

    # Detect CompetitionSchedule (competition-wide file, prefix is all dashes)
    if "_CompetitionSchedule.pdf" in filename:
        return {
            "filename": filename,
            "type": "Competition",
            "category": "Competition",
            "categoryCode": "",
            "judgingMethod": "",
            "segment": "General",
            "raw_segment": "General",
            "suffix": "CompetitionSchedule.pdf",
            "prefix": filename.replace("_CompetitionSchedule.pdf", ""),
        }

    # Try to match a category abbreviation
    matched = match_category(filename, categories)
    if not matched:
        return None

    abbrev = matched["abbreviation"]
    abbrev_len = len(abbrev)

    # Split suffix: everything after the LAST underscore
    if "_" not in filename:
        return None
    basename, suffix = filename.rsplit("_", 1)

    # The part after the abbreviation and before the suffix separator
    remainder = basename[abbrev_len:]

    # Detect split/group number: digits immediately after abbreviation (with optional dashes)
    stripped = remainder.lstrip("-")
    split_number = None
    if stripped and len(stripped) >= 2 and stripped[:2].isdigit():
        split_number = int(stripped[:2])

    # Segment detection — extract the full segment identifier from the
    # remainder (everything between the dashes).  This preserves qualifiers
    # like QUAL0001PK vs QUAL0002PK so ice-dance pattern-dances are not
    # merged into a single "QUAL" bucket.
    seg_stripped = remainder.strip("-")
    # If a split-group number sits at the front (e.g. "01QUAL"), strip it
    if split_number is not None and seg_stripped and len(seg_stripped) >= 2 and seg_stripped[:2].isdigit():
        seg_stripped = seg_stripped[2:].lstrip("-")

    segment = "Unknown"
    raw_segment = "Unknown"
    if "CalculationSetupVerificationforReferee" in suffix:
        segment = "Category General"
        raw_segment = "Category General"
    elif seg_stripped:
        segment = seg_stripped
        raw_segment = seg_stripped

    # Build display names, appending split number if present
    display_name = matched["displayName"]
    display_name_fi = matched.get("displayNameFi", display_name)
    if split_number is not None:
        display_name = f"{display_name} #{split_number}"
        display_name_fi = f"{display_name_fi} #{split_number}"

    return {
        "filename": filename,
        "type": matched["competitionType"],
        "category": display_name,
        "categoryFi": display_name_fi,
        "categoryCode": abbrev,
        "judgingMethod": matched["judgingMethod"],
        "segment": segment,
        "raw_segment": raw_segment,
        "suffix": suffix,
        "prefix": basename,
    }
