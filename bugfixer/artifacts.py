from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import stat
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from .common import BugFixerError, decode_text, public_url, redact
from .http import validate_url


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Paths are per-run; context and summaries are never mixed with old snapshots.
    with path.open("x", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
    return str(path.resolve())


def _kind(head: bytes, content_type: str):
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
        return "zip"
    if head.startswith(b"\x89PNG"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if len(head) > 8 and head[4:8] == b"ftyp":
        return "mp4"
    if head.lstrip().lower().startswith((b"<!doctype html", b"<html")):
        return "html"
    if "text/" in content_type or "json" in content_type or b"\x00" not in head:
        return "text"
    return "binary"


def safe_members(archive: zipfile.ZipFile, max_bytes=512 * 1024 * 1024, max_files=1000):
    infos = archive.infolist()
    if len(infos) > max_files or sum(i.file_size for i in infos) > max_bytes:
        raise BugFixerError("archive_limit", "ZIP exceeds the file-count or expanded-size limit.")
    names = set()
    for info in infos:
        name = info.filename.replace("\\", "/")
        p = PurePosixPath(name)
        reserved = re.compile(r'^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', re.I)
        if p.is_absolute() or ".." in p.parts or ":" in name or not name or "\x00" in name or any(part.endswith((" ", ".")) or reserved.match(part) for part in p.parts):
            raise BugFixerError("unsafe_archive", "ZIP contains an unsafe member path.")
        if stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1:
            raise BugFixerError("unsafe_archive", "Symlink/encrypted ZIP entries are not supported.")
        normalized = str(p).casefold()
        if normalized in names:
            raise BugFixerError("unsafe_archive", "ZIP contains colliding member paths.")
        names.add(normalized)
    return infos


def extract_zip(path: Path, root: Path):
    members = []
    try:
        with zipfile.ZipFile(path) as archive:
            infos = safe_members(archive)
            # Validate the whole archive before creating any extracted member.
            for info in infos:
                target = root.joinpath(*PurePosixPath(info.filename.replace("\\", "/")).parts)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("xb") as dest:
                    total = 0
                    while chunk := source.read(64 * 1024):
                        total += len(chunk)
                        if total > info.file_size:
                            raise BugFixerError("archive_limit", "ZIP member exceeded its declared size.")
                        dest.write(chunk)
                members.append({"path": str(target.resolve()), "size_bytes": total, "sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError):
        raise BugFixerError("invalid_archive", "ZIP is damaged or uses unsupported compression.") from None
    return members


def index_text(path: Path, target: Path, secrets=()):
    if path.stat().st_size > 32 * 1024 * 1024:
        return {"status": "skipped", "reason": "text_index_size_limit"}
    text = decode_text(path.read_bytes())
    lines = text.splitlines()
    matches = []
    pattern = re.compile(r"exception|\berror\b|timeout|failed|fatal|黑屏|卡住|isOpen|HideModule|CreateInstance|scoutC/sign", re.I)
    for i, line in enumerate(lines):
        if pattern.search(line):
            matches.append({"line": i + 1, "text": redact(line[:1500], secrets)})
            if len(matches) >= 100:
                break
    result = {"line_count": len(lines), "matches_limit": 100, "matches": matches, "truncated": len(matches) >= 100, "interpretation": "Index entries are evidence candidates, not proven root causes."}
    write_json(target, result)
    return {"path": str(target.resolve()), "line_count": len(lines), "match_count": len(matches)}


class ArtifactStore:
    def __init__(self, root: Path, max_download_bytes=128 * 1024 * 1024):
        self.root = root.resolve()
        self.cache = self.root / ".cache"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_download_bytes

    def download(self, client, resource, run: Path, index: int, secrets=()):
        url = validate_url(resource["url"], client.hosts)
        cached, sha, size, kind, content_type, reuse, conditional = self._acquire(client,url)
        extension = {"zip": ".zip", "png": ".png", "jpg": ".jpg", "mp4": ".mp4", "text": ".txt"}.get(kind, ".bin")
        path = run / "files" / f"{index:03d}-{sha[:12]}{extension}"
        path.parent.mkdir(exist_ok=True)
        import shutil
        shutil.copyfile(cached, path)
        result = {"status": "downloaded", "source_url": public_url(url), "name": redact(str(resource.get("name", "")), secrets), "declared_kind": resource.get("kind", "attachment"), "detected_type": kind, "content_type": content_type, "size_bytes": size, "sha256": sha, "path": str(path.resolve()), "cache_reused": reuse, "conditional_not_modified":conditional, "fetched_at": now()}
        if kind == "zip":
            result["members"] = extract_zip(path, run / "extracted" / f"{index:03d}")
            for n, member in enumerate(result["members"]):
                file = Path(member["path"])
                with file.open("rb") as f:
                    member_type = _kind(f.read(4096), "")
                if member_type == "text":
                    member["index"] = index_text(file, run / "indexes" / f"{index:03d}-{n:04d}.json", secrets)
        elif kind == "text":
            result["index"] = index_text(path, run / "indexes" / f"{index:03d}.json", secrets)
        return result

    def _acquire(self,client,url):
        meta_path=self.cache/('url-'+hashlib.sha256(url.encode()).hexdigest()+'.json')
        meta={}
        if meta_path.is_file():
            try:
                meta=json.loads(meta_path.read_text(encoding='utf-8'))
                existing=self.cache/meta['sha256']
                if not re.fullmatch(r'[a-f0-9]{64}',meta['sha256']) or not existing.is_file() or hashlib.sha256(existing.read_bytes()).hexdigest()!=meta['sha256']:
                    meta={}
            except (OSError,ValueError,KeyError,TypeError):
                meta={}
        headers={}
        if meta.get('etag'):
            headers['If-None-Match']=meta['etag']
        elif meta.get('last_modified'):
            headers['If-Modified-Since']=meta['last_modified']
        for attempt in range(2):
            try:
                return self._acquire_once(client,url,meta_path,meta,headers)
            except (OSError,EOFError,http.client.IncompleteRead) as e:
                if attempt==1:
                    raise BugFixerError('download_incomplete','Attachment stream was interrupted; no complete evidence was claimed.',True) from None
                time.sleep(0.2)

    def _acquire_once(self,client,url,meta_path,meta,headers):
        temp_path = None
        try:
            response=client.open(url,headers=headers,accepted_statuses=(304,),download=True)
            with response:
                if getattr(response,'status',200)==304:
                    if not meta:
                        raise BugFixerError('invalid_cache_response','Received HTTP304 without a verified cached artifact.')
                    return self.cache/meta['sha256'],meta['sha256'],meta['size'],meta['kind'],meta['content_type'],True,True
                return self._consume(response,meta_path)
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink()

    def _consume(self,response,meta_path):
        temp_path=None
        try:
            with tempfile.NamedTemporaryFile(dir=self.cache, delete=False) as dest:
                temp_path = Path(dest.name)
                length = response.headers.get("Content-Length")
                if length and int(length) > self.max_bytes:
                    raise BugFixerError("download_limit", "Attachment exceeds the download-size limit.")
                digest, size, head = hashlib.sha256(), 0, b""
                while chunk := response.read(64 * 1024):
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise BugFixerError("download_limit", "Attachment exceeded the download-size limit.")
                    if len(head) < 4096:
                        head = (head + chunk)[:4096]
                    digest.update(chunk)
                    dest.write(chunk)
                if length and size != int(length):
                    raise OSError('incomplete response body')
                content_type = response.headers.get("Content-Type", "")
                kind = _kind(head, content_type)
                if kind == "html":
                    raise BugFixerError("unexpected_html", "Attachment returned HTML, possibly a login page; it is not a log download.")
            sha = digest.hexdigest()
            cached = self.cache / sha
            reuse = cached.exists() and hashlib.sha256(cached.read_bytes()).hexdigest() == sha
            if reuse:
                temp_path.unlink()
            else:
                os.replace(temp_path, cached)
            temp_path = None
            metadata={'sha256':sha,'size':size,'kind':kind,'content_type':content_type,'etag':response.headers.get('ETag'),'last_modified':response.headers.get('Last-Modified')}
            with tempfile.NamedTemporaryFile(dir=self.cache,mode='w',encoding='utf-8',delete=False) as f:
                meta_temp=Path(f.name)
                json.dump(metadata,f)
            os.replace(meta_temp,meta_path)
            return cached,sha,size,kind,content_type,reuse,False
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink()
