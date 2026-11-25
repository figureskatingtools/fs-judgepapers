"""
Trainees (issue #93): people named in this tool, not in FS Manager, who get a
packet of copies of an official's sheets with their own name on them.

Stored in the competition's metadata.json next to judgingMethodOverrides:

    "trainees": [
        {"name": "Väinö Åkerström", "role": "judge", "segments": ["FSKMSINGLES-JUNIOR----QUAL000100--"]},
        ...
    ]

`segments` holds FSM file prefixes (the key segmentCovers uses too). A stored
prefix that is not among the competition's current files is kept and ignored
when generating, so a trainee survives a re-import.

`parse_trainees` is the strict write side (save_competition_settings: a
malformed list is refused, never silently trimmed, so the client never
believes something was stored that wasn't); `sanitize_trainees` is the
lenient read side (a hand-edited metadata.json can never break a read or a
generate run). Messages name the rule and the entry's position, never the
name: they end up in responses and the site may log them.
"""
import hashlib
import re
import unicodedata

from reportlab.pdfbase import pdfmetrics

try:
    from .combine_judging_papers import slugify
    from .create_cover_pages import TABLE_FONT, TABLE_FONT_BOLD
    from .email_sender import SENDER_NAME_MAX_LENGTH, validate_sender_name
except ImportError:
    from combine_judging_papers import slugify
    from create_cover_pages import TABLE_FONT, TABLE_FONT_BOLD
    from email_sender import SENDER_NAME_MAX_LENGTH, validate_sender_name

MAX_TRAINEES = 50
MAX_SEGMENTS = 200
MAX_SEGMENT_LENGTH = 100
TRAINEE_ROLES = ("judge", "technical")
# stored role -> (pipeline task role, label printed on the cover)
TRAINEE_TASKS = {"judge": ("judge_trainee", "Judge Trainee"),
                 "technical": ("technical_trainee", "Technical Trainee")}

TRAINEE_KEYS = frozenset(("name", "role", "segments"))
_SEGMENT_FORBIDDEN = frozenset("/\\")


def _drawable(name):
    """
    Every non-space character of name has a glyph in Liberation Sans, the font
    the cover table and the stamped sheets print it in, so a name never comes
    out as empty boxes on paper. Both weights are checked: the stamp copies
    FSM's weight, which is bold on some exports and regular on others. Skipped
    when the font failed to register (Helvetica has no glyph table to ask),
    as create_cover_pages._font_for does.
    """
    for font in {TABLE_FONT, TABLE_FONT_BOLD}:
        try:
            glyphs = pdfmetrics.getFont(font).face.charToGlyph
        except (AttributeError, KeyError):
            continue
        if not all(ord(ch) in glyphs for ch in name if not ch.isspace()):
            return False
    return True


def _nfc_sender_name(raw):
    """validate_sender_name on the NFC form: a decomposed Ä would otherwise be
    drawn as A + a combining mark of zero advance. None if invalid."""
    if not isinstance(raw, str):
        return None
    return validate_sender_name(unicodedata.normalize("NFC", raw))


def _has_letter_or_digit(name):
    """A lone combining mark or punctuation is no name."""
    return any(ch.isalnum() for ch in name)


def _clean_name(raw):
    """The cleaned, printable name, or None."""
    name = _nfc_sender_name(raw)
    if name is None or not _has_letter_or_digit(name) or not _drawable(name):
        return None
    return name


def _name_key(name):
    """One person's spelling, whatever the case, spacing or Unicode form."""
    return unicodedata.normalize("NFC", " ".join(name.split())).casefold()


def _valid_segment(value):
    return (isinstance(value, str)
            and 1 <= len(value) <= MAX_SEGMENT_LENGTH
            and value == value.strip()
            and not any(ch in _SEGMENT_FORBIDDEN or unicodedata.category(ch).startswith("C")
                        for ch in value))


def _unique(values):
    """Duplicates collapse, first-seen order kept."""
    return list(dict.fromkeys(values))


def parse_trainees(raw):
    """
    Strict validation for a write. Returns (clean_list, None), the normalized
    form to store (cleaned names, de-duplicated segments, keys in the order
    name/role/segments), or (None, message) naming the broken rule.
    """
    if not isinstance(raw, list):
        return None, "trainees must be a list"
    if len(raw) > MAX_TRAINEES:
        return None, f"at most {MAX_TRAINEES} trainees"

    clean = []
    slugs = {}  # slug -> (position, name key) of the first entry with it
    for position, entry in enumerate(raw, start=1):
        where = f"trainee {position}"
        if not isinstance(entry, dict) or set(entry) != TRAINEE_KEYS:
            return None, f"{where}: must be an object with exactly the keys name, role and segments"

        name = _nfc_sender_name(entry["name"])
        if name is None:
            return None, (f"{where}: name must be 1 to {SENDER_NAME_MAX_LENGTH} characters, "
                          "without control characters or any of / : @ < > \\ \"")
        if not _has_letter_or_digit(name):
            return None, f"{where}: name must contain at least one letter or digit"
        if not _drawable(name):
            return None, f"{where}: name has a character the papers' font cannot print"
        # One packet per slug: two people whose names slugify alike would share
        # one packet under the first one's name. The same name in two roles
        # (one person) is fine.
        key = _name_key(name)
        first_position, first_key = slugs.setdefault(trainee_slug(name), (position, key))
        if first_key != key:
            return None, (f"{where}: name is too close to trainee {first_position}'s (they would "
                          "share one packet); use the same spelling for the same person")

        role = entry["role"]
        if not isinstance(role, str) or role not in TRAINEE_ROLES:
            return None, f"{where}: role must be one of {', '.join(TRAINEE_ROLES)}"

        segments = entry["segments"]
        if not isinstance(segments, list) or len(segments) > MAX_SEGMENTS:
            return None, f"{where}: segments must be a list of at most {MAX_SEGMENTS} segments"
        if not all(_valid_segment(s) for s in segments):
            return None, (f"{where}: each segment must be 1 to {MAX_SEGMENT_LENGTH} characters, "
                          "without leading or trailing spaces, / \\ or control characters")

        clean.append({"name": name, "role": role, "segments": _unique(segments)})
    return clean, None


def sanitize_trainees(raw):
    """
    The stored list as the pipeline and the UI may use it: anything invalid is
    dropped (a whole entry when its name or role is, or when its name would
    share an earlier entry's packet without being the same name; a single
    segment when only that is), never raises. Mirrors
    categories.sanitize_judging_method_overrides.
    """
    if not isinstance(raw, list):
        return []
    clean = []
    slugs = {}  # slug -> name key of the first entry kept with it
    for entry in raw:
        if len(clean) >= MAX_TRAINEES:
            break
        if not isinstance(entry, dict):
            continue
        try:
            name = _clean_name(entry.get("name"))
        except Exception:
            name = None
        role = entry.get("role")
        if name is None or not isinstance(role, str) or role not in TRAINEE_ROLES:
            continue
        key = _name_key(name)
        if slugs.setdefault(trainee_slug(name), key) != key:
            continue
        segments = entry.get("segments")
        if not isinstance(segments, list):
            segments = []
        segments = _unique(s for s in segments if _valid_segment(s))[:MAX_SEGMENTS]
        clean.append({"name": name, "role": role, "segments": segments})
    return clean


def trainee_slug(name):
    """
    The file-name slug of a trainee: slugify(name) as for the panel's
    officials (so the same person on a panel shares one packet). A name with
    no a-z/0-9 character left after slugify (all-Cyrillic, all-Š) gets
    trainee-<the first 8 hex digits of sha1 of its _name_key>: the same for
    the same name in two entries, and never a slugify result (so never a
    panel member's slug), since slugify keeps no "-".
    """
    name = name if isinstance(name, str) else ""
    slug = slugify(name)
    if not re.search(r"[a-z0-9]", slug):
        return "trainee-" + hashlib.sha1(_name_key(name).encode("utf-8")).hexdigest()[:8]
    return slug
