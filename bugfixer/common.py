from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


class BugFixerError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable


@dataclass
class Credential:
    account: str = ""
    password: str = field(default="", repr=False)
    session: str = field(default="", repr=False)
    login_url: str = ""


def decode_text(body: bytes, content_type: str = "") -> str:
    encoding = re.search(r"charset=[\"']?([\w-]+)", content_type, re.I)
    candidates = ["utf-8-sig"]
    if encoding:
        candidates.append(encoding.group(1))
    candidates += ["gb18030", "utf-16"]
    for name in candidates:
        try:
            return body.decode(name)
        except (UnicodeError, LookupError):
            continue
    return body.decode("utf-8", errors="replace")


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        if tag in ("p", "div", "br", "li", "tr", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        if tag in ("p", "div", "li", "tr"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_text(value: str) -> str:
    parser = _Text()
    parser.feed(str(value or ""))
    return re.sub(r"\n[ \t]*\n+", "\n\n", "".join(parser.parts)).strip()


SECRET_KEYS = re.compile(r"^(?:password|passwd|pwd|token|access_?token|refresh_?token|cookie|authorization|client_?secret|sessionid|sessionkey|verifyrand|zentaosid)$", re.I)
SECRET_ASSIGNMENT = re.compile(r"(?i)(\b(?:password|passwd|pwd|access_?token|refresh_?token|authorization|cookie|zentaosid)\b[\s\"']*[:=][\s\"']*)([^\s\"'<>,;&]+)")


def redact(value, secrets=()):
    if isinstance(value, dict):
        return {str(k): "[REDACTED]" if SECRET_KEYS.match(str(k)) else redact(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, secrets) for v in value]
    if isinstance(value, str):
        for secret in sorted((s for s in secrets if s), key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(r'(?i)(https?://)[^\s/@:]+:[^\s/@]+@',r'\1[REDACTED]@',value)
        value = re.sub(r'(?i)(\bauthorization\s*[:=]\s*)(?:bearer|basic)\s+[^\s,;]+',r'\1[REDACTED]',value)
        value = re.sub(r'(?im)(\bcookie\s*:\s*)[^\r\n]+',r'\1[REDACTED]',value)
        value = SECRET_ASSIGNMENT.sub(r"\1[REDACTED]", value)
        return value
    return value


def public_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        hostname=parts.hostname or ''
        port=parts.port
        netloc=('['+hostname+']') if ':' in hostname else hostname
        if port:
            netloc+=':'+str(port)
        query = [(k, "[REDACTED]" if SECRET_KEYS.match(k) or re.search(r"signature|credential|security-token|ossaccesskeyid", k, re.I) else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
        return urlunsplit((parts.scheme, netloc, parts.path, urlencode(query), ""))
    except (ValueError,TypeError):
        return '[invalid-url]'


def parse_json(body: bytes | str):
    try:
        return json.loads(body)
    except (ValueError, UnicodeError):
        raise BugFixerError("invalid_response", "Service did not return valid JSON; login or service contract may have changed.") from None
