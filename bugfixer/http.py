from __future__ import annotations

import http.cookiejar
import http.client
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .common import BugFixerError
from . import __version__


SERVICE_HOSTS = frozenset({"zen.wckj.com", "cscmanage.galasports.com", "cscmanage.test-galasports.com", "cscmanage-sea.galasports.com", "login2.galasports.com", "login.galasports.com", "login.test-galasports.com"})
DOWNLOAD_HOSTS = SERVICE_HOSTS | {"galasports-img.oss-cn-beijing.aliyuncs.com", "galasports-na-img.oss-ap-southeast-1.aliyuncs.com"}


def validate_url(url: str, hosts=DOWNLOAD_HOSTS):
    try:
        p = urllib.parse.urlsplit(url)
        allowed_port = p.port in (None, 80, 443) or (p.hostname == 'cscmanage-sea.galasports.com' and p.scheme == 'https' and p.port == 8000)
        if p.scheme not in ("http", "https") or p.hostname not in hosts or p.username or p.password or not allowed_port:
            raise ValueError()
    except ValueError:
        raise BugFixerError("url_not_allowed", "The URL is outside the configured service/download allowlist.") from None
    return url


def validate_download_url(url: str, hosts=DOWNLOAD_HOSTS):
    validate_url(url,hosts)
    p=urllib.parse.urlsplit(url)
    path=urllib.parse.unquote(p.path)
    if p.hostname in SERVICE_HOSTS:
        # An attachment must never turn an authenticated GET into a business action.
        allowed=p.hostname=='zen.wckj.com' and (re.fullmatch(r'/file-(?:read|download)-[1-9][0-9]*(?:\.[A-Za-z0-9]+)?',path) or path.startswith('/data/upload/'))
        if not allowed or '..' in path.replace('\\','/').split('/'):
            raise BugFixerError('download_url_not_allowed','Service attachment URL is not a recognized read-only file route.')
    return url


class _Redirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, hosts):
        self.hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl, self.hosts)
        if getattr(req,'_bugfixer_download',False):
            validate_download_url(newurl,self.hosts)
        if req.data is not None and urllib.parse.urlsplit(req.full_url).netloc != urllib.parse.urlsplit(newurl).netloc:
            raise BugFixerError("auth_redirect_blocked", "A credential-bearing request attempted a cross-origin redirect.")
        redirected=super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and getattr(req,'_bugfixer_download',False):
            redirected._bugfixer_download=True
        return redirected


@dataclass
class Response:
    url: str
    status: int
    headers: dict
    body: bytes


class Client:
    def __init__(self, timeout=20, retries=1, hosts=DOWNLOAD_HOSTS, max_response_bytes=16 * 1024 * 1024):
        self.timeout, self.retries, self.hosts = timeout, retries, hosts
        self.max_response_bytes = max_response_bytes
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies), _Redirect(hosts))
        self.headers = {"User-Agent": "BugFixer/" + __version__, "Accept": "application/json,text/html,text/plain,*/*"}

    def open(self, url, data=None, headers=None, accepted_statuses=(), download=False):
        validate_url(url, self.hosts)
        if download:
            validate_download_url(url,self.hosts)
        attempts = self.retries + 1 if data is None else 1
        for attempt in range(attempts):
            try:
                request=urllib.request.Request(url, data=data, headers={**self.headers, **(headers or {})})
                request._bugfixer_download=download
                return self.opener.open(request, timeout=self.timeout)
            except urllib.error.HTTPError as e:
                if e.code in accepted_statuses:
                    return e
                if e.code in (401, 403):
                    raise BugFixerError("authentication_required" if e.code == 401 else "permission_denied", "The service requires login or the account lacks permission.") from None
                if e.code == 404:
                    raise BugFixerError("not_found", "The requested endpoint or record was not found.") from None
                if e.code >= 500 and attempt + 1 < attempts:
                    time.sleep(0.2 * (attempt + 1))
                    continue
                raise BugFixerError("http_error", f"Service returned HTTP {e.code}.", e.code >= 500) from None
            except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError, OSError):
                if attempt + 1 < attempts:
                    time.sleep(0.2 * (attempt + 1))
                    continue
                raise BugFixerError("network_error", "Service request failed or timed out; no business write was retried.", True) from None

    def _read(self, url, data=None, headers=None, accepted_statuses=()):
        with self.open(url, data, headers, accepted_statuses) as r:
            try:
                body = r.read(self.max_response_bytes + 1)
            except (OSError,http.client.IncompleteRead):
                raise BugFixerError('response_incomplete','Service response was truncated or interrupted.',True) from None
            if len(body) > self.max_response_bytes:
                raise BugFixerError("response_too_large", "Context response exceeded the configured limit.")
            length=r.headers.get('Content-Length')
            if length and len(body)!=int(length):
                raise BugFixerError('response_incomplete','Service response length did not match the declared length.',True)
            return Response(r.url, r.status, dict(r.headers), body)

    def get(self, url):
        return self._read(url)

    def get_response(self, url, accepted_statuses=(401, 403)):
        """Read SSO challenge response, including headers; adapter must check status."""
        return self._read(url, accepted_statuses=accepted_statuses)

    def post_form(self, url, values, purpose="auth"):
        if purpose not in ("auth", "read"):
            raise BugFixerError("write_not_allowed", "Only authentication and documented read-only queries are supported.")
        return self._read(url, urllib.parse.urlencode(values).encode(), {"Content-Type": "application/x-www-form-urlencoded", "X-Requested-With": "XMLHttpRequest"})

    def post_json(self, url, values, purpose="read"):
        if purpose not in ("auth", "read"):
            raise BugFixerError("write_not_allowed", "Business writes are not supported.")
        return self._read(url, json.dumps(values).encode(), {"Content-Type": "application/json"})

    def post_write_form(self, url, values):
        """Narrow business-write entry; POSTs are never automatically retried."""
        parts = urllib.parse.urlsplit(url)
        if (parts.scheme + '://' + parts.netloc != 'http://zen.wckj.com'
                or not re.fullmatch(r'/bug-(?:edit|resolve)-[1-9][0-9]*\.html', parts.path)
                or parts.query or parts.fragment):
            raise BugFixerError('write_not_allowed', 'Only the verified ZenTao edit/resolve form routes are writable.')
        return self._read(url, urllib.parse.urlencode(values, doseq=True).encode(),
                          {'Content-Type': 'application/x-www-form-urlencoded', 'X-Requested-With': 'XMLHttpRequest'})
