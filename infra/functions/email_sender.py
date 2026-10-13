"""
"Send judges' papers" e-mail: templates, validation, MIME building and the
Mailgun HTTP call.

The MIME message is built here with the stdlib `email` package rather than
letting Mailgun assemble it, so the encoding is deterministic: non-ASCII headers (the emoji subject, a sender name with ä/ö)
come out RFC 2047 encoded, both body parts are UTF-8 quoted-printable. It is
posted to Mailgun's `messages.mime` endpoint as-is.

Configuration (app settings):
- MAILGUN_API_KEY     domain sending key; unset → dry run (subject + recipient count logged, nothing sent)
- MAILGUN_API_BASE    regional API base (default: EU, https://api.eu.mailgun.net)
- MAILGUN_DOMAIN      Mailgun sending domain (default: figureskatingtools.com)
- EMAIL_FROM_ADDRESS  From address (default: no-reply@figureskatingtools.com)
- EXAMPLE_VIDEO_URL   printing example video; empty → the video line is left out
"""

import base64
import html
import json
import logging
import os
import re
import unicodedata
import urllib.error
import urllib.request
from email import policy
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from uuid import uuid4
from zoneinfo import ZoneInfo

DEFAULT_API_BASE = "https://api.eu.mailgun.net"
DEFAULT_DOMAIN = "figureskatingtools.com"
DEFAULT_FROM_ADDRESS = "no-reply@figureskatingtools.com"
FROM_DISPLAY_NAME = "Figureskatingtools.com"
MAILGUN_TIMEOUT_SECONDS = 20

LANGUAGES = ("fi", "en")
SENDER_NAME_MAX_LENGTH = 80
# Characters that would let the free-text sender name carry a URL or an
# address into a mail sent from our domain.
_SENDER_NAME_FORBIDDEN = set('/:@<>\\"')

# Pragmatic single-address check (ASCII only, no quoted local parts, no IP
# literals); the frontend uses the same pattern.
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$"
)

# The site's privacy notice, linked from every mail's footer.
PRIVACY_NOTICE_URLS = {
    "fi": "https://figureskatingtools.com/tietosuoja/",
    "en": "https://figureskatingtools.com/privacy/",
}

_HELSINKI = ZoneInfo("Europe/Helsinki")
_EN_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
              "August", "September", "October", "November", "December")

# Templates: {{placeholder}} values are filled in by _render. A line holding
# {{exampleVideoUrl}} is dropped (with the blank line after it) while no
# video URL is configured.
TEMPLATES = {
    "fi": {
        "subject": "Tuomaripaperit tulostettavaksi",
        "linkText": "Avaa tuomaripaperit",
        "body": """Terve! 👋

🖨️ Ohessa linkki tuomaripapereiden tulostusta varten. Linkki on voimassa {{expiryDate}} asti ⏳, joten tulostattehan paperit ennen kilpailun alkua.

📋 Paperit ovat valmiiksi järjestyksessä: ne on niputettu tuomarikohtaisesti etunimen mukaan aakkosjärjestykseen, ja kunkin tuomarin paperit on lajiteltu sarjoittain.

🎥 Esimerkkivideo tulostuksesta löytyy täältä: {{exampleVideoUrl}}

🔗 Linkki tuomaripapereihin: {{judgePapersUrl}}

Mikäli jotain kysyttävää ilmenee, olethan yhteydessä.

Terveisin,
{{senderName}}
Tulospalvelu

---
Sent from Figureskatingtools.com - Supporting Figure Skating Community ❤️
Tietosuojaseloste: {{privacyUrl}}
""",
    },
    "en": {
        "subject": "Judges' papers ready for printing",
        "linkText": "Open the judges' papers",
        "body": """Hi there! 👋

🖨️ Here's the link for printing the judges' papers. The link is valid until {{expiryDate}} ⏳, so please make sure to print the papers before the competition starts.

📋 The papers are already in order: grouped by judge in alphabetical order by first name, with each judge's papers sorted by category.

🎥 You can find an example video of the printing process here: {{exampleVideoUrl}}

🔗 Link to the judges' papers: {{judgePapersUrl}}

If you have any questions, please get in touch.

Best regards,
{{senderName}}
Results Service

---
Sent from Figureskatingtools.com - Supporting Figure Skating Community ❤️
Privacy notice: {{privacyUrl}}
""",
    },
}


class EmailSendError(Exception):
    """Mailgun rejected the message or could not be reached."""


def validate_email_address(value):
    """Return the trimmed address if it is one valid address, else None."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 254 or not _EMAIL_RE.match(value):
        return None
    if len(value.split("@", 1)[0]) > 64:
        return None
    try:
        Address(addr_spec=value)
    except Exception:
        return None
    return value


def validate_sender_name(value):
    """Return the trimmed sender name if acceptable, else None."""
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    if not value or len(value) > SENDER_NAME_MAX_LENGTH:
        return None
    if any(c in _SENDER_NAME_FORBIDDEN or unicodedata.category(c).startswith("C") for c in value):
        return None
    return value


def format_expiry(expiry, language):
    """Expiry as Finnish local time: '16.10.2026 klo 14.30' / '16 October 2026 at 14:30 (Finnish time)'."""
    local = expiry.astimezone(_HELSINKI)
    if language == "fi":
        return f"{local.day}.{local.month}.{local.year} klo {local.hour}.{local.minute:02d}"
    return f"{local.day} {_EN_MONTHS[local.month - 1]} {local.year} at {local.hour}:{local.minute:02d} (Finnish time)"


def example_video_url():
    """The configured example video URL, or '' when unset or not https."""
    url = os.environ.get("EXAMPLE_VIDEO_URL", "").strip()
    return url if url.startswith("https://") else ""


def _template_lines(language, video_url):
    lines = TEMPLATES[language]["body"].split("\n")
    if video_url:
        return lines
    out = []
    skip_blank = False
    for line in lines:
        if "{{exampleVideoUrl}}" in line:
            skip_blank = True
            continue
        if skip_blank and not line.strip():
            skip_blank = False
            continue
        skip_blank = False
        out.append(line)
    return out


def _render(language, values):
    """Return (subject, text, html) for the template filled with values."""
    template = TEMPLATES[language]
    lines = _template_lines(language, values["exampleVideoUrl"])

    text = "\n".join(lines)
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)

    html_values = {key: html.escape(value) for key, value in values.items()}
    if values["exampleVideoUrl"]:
        html_values["exampleVideoUrl"] = (
            f'<a href="{html.escape(values["exampleVideoUrl"])}">{html.escape(values["exampleVideoUrl"])}</a>')
    html_values["privacyUrl"] = (
        f'<a href="{html.escape(values["privacyUrl"])}">{html.escape(values["privacyUrl"])}</a>')
    html_values["judgePapersUrl"] = (
        f'<a href="{html.escape(values["judgePapersUrl"])}">{html.escape(template["linkText"])}</a>')

    blocks = []
    for block in "\n".join(lines).strip().split("\n\n"):
        if block.startswith("---"):
            blocks.append('<hr style="border:none;border-top:1px solid #ccc">')
            block = block[3:].strip()
            if not block:
                continue
        escaped = html.escape(block)
        for key, value in html_values.items():
            escaped = escaped.replace(html.escape("{{" + key + "}}"), value)
        blocks.append("<p>" + escaped.replace("\n", "<br>\n") + "</p>")
    body_html = (
        '<!DOCTYPE html>\n<html><head><meta charset="utf-8"></head>\n'
        '<body style="font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.5;color:#222">\n'
        + "\n".join(blocks) + "\n</body></html>\n")

    return template["subject"], text, body_html


def _from_address():
    from_address = os.environ.get("EMAIL_FROM_ADDRESS") or DEFAULT_FROM_ADDRESS
    from_user, from_domain = from_address.split("@", 1)
    return Address(display_name=FROM_DISPLAY_NAME,
                   username=from_user, domain=from_domain)


def _render_for(language, sender_name, judge_papers_url, expiry):
    if language not in LANGUAGES:
        raise ValueError(f"Unsupported language: {language}")
    return _render(language, {
        "expiryDate": format_expiry(expiry, language),
        "exampleVideoUrl": example_video_url(),
        "judgePapersUrl": judge_papers_url,
        "senderName": sender_name,
        "privacyUrl": PRIVACY_NOTICE_URLS[language],
    })


def render_preview(language, sender_name, expiry):
    """
    What build_message would send, for the UI preview: {subject, from, html}.
    The download link is only minted at send time, so it points nowhere here.
    """
    subject, _text, body_html = _render_for(language, sender_name, "#", expiry)
    return {"subject": subject, "from": str(_from_address()), "html": body_html}


def build_message(language, to, results_service_email, sender_name, judge_papers_url, expiry):
    """Build the MIME message (an EmailMessage) for the judges'-papers mail."""
    subject, text, body_html = _render_for(language, sender_name, judge_papers_url, expiry)

    from_address = _from_address()
    msg = EmailMessage(policy=policy.SMTP)
    msg["Subject"] = subject
    msg["From"] = from_address
    msg["To"] = to
    msg["Cc"] = results_service_email
    msg["Date"] = formatdate(usegmt=True)
    msg["Message-ID"] = make_msgid(domain=from_address.domain)
    msg.set_content(text, charset="utf-8", cte="quoted-printable")
    msg.add_alternative(body_html, subtype="html", charset="utf-8", cte="quoted-printable")
    return msg


def _multipart_form(fields, file_field, file_name, file_bytes):
    """Encode a multipart/form-data body. Returns (body, content_type)."""
    boundary = uuid4().hex
    parts = []
    for name, value in fields:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8"))
    parts.append(
        (f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{file_name}"\r\n'
         f'Content-Type: message/rfc822\r\n\r\n').encode("utf-8") + file_bytes + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def send_message(msg, tag="judgepapers"):
    """
    Send the message through Mailgun. Envelope recipients are To + Cc.
    Returns {"id": <Mailgun id or None>, "dryRun": bool}; raises EmailSendError.
    """
    raw = msg.as_bytes(policy=policy.SMTP)
    api_key = os.environ.get("MAILGUN_API_KEY", "").strip()
    if not api_key:
        # Subject and recipient count only: the body carries a live download
        # link, and addresses don't belong in logs.
        recipient_count = sum(len(msg[header].addresses) for header in ("To", "Cc") if msg[header])
        logging.warning("MAILGUN_API_KEY not set: dry run, message not sent. "
                        f"Subject: {msg['Subject']} | {recipient_count} recipient(s)")
        return {"id": None, "dryRun": True}

    api_base = (os.environ.get("MAILGUN_API_BASE") or DEFAULT_API_BASE).rstrip("/")
    domain = os.environ.get("MAILGUN_DOMAIN") or DEFAULT_DOMAIN
    recipients = [a.addr_spec for header in ("To", "Cc") for a in msg[header].addresses]

    fields = [("to", r) for r in recipients] + [
        ("o:tracking", "no"),
        ("o:tracking-clicks", "no"),
        ("o:tracking-opens", "no"),
        ("o:tag", tag),
    ]
    body, content_type = _multipart_form(fields, "message", "message.mime", raw)
    auth = base64.b64encode(f"api:{api_key}".encode("utf-8")).decode("ascii")
    request = urllib.request.Request(
        f"{api_base}/v3/{domain}/messages.mime",
        data=body,
        method="POST",
        headers={"Content-Type": content_type, "Authorization": f"Basic {auth}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=MAILGUN_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        raise EmailSendError(f"Mailgun returned HTTP {e.code}: {detail}") from e
    except Exception as e:
        raise EmailSendError(f"Mailgun request failed: {e}") from e

    return {"id": payload.get("id"), "dryRun": False}

