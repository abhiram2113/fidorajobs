from __future__ import annotations

import hashlib
import hmac
import html
from email.parser import BytesParser
from email.policy import default as email_policy
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from io import BytesIO
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Vercel serverless functions do not provide persistent local storage.
# /tmp is writable for temporary runtime files only; use a persistent
# PostgreSQL database for production user/job/application/resume data.
DATA = Path(os.getenv("DATA_DIR", "/tmp/fidorapath-data" if os.getenv("VERCEL") else str(ROOT / "data")))
DB = DATA / "portal.sqlite3"
STATIC = ROOT / "static"

def load_dotenv():
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

load_dotenv()

# Optional Supabase mirror for centrally hosted job listings. SQLite remains the
# local source of truth for sessions/users until a separate Auth migration is done.
SUPABASE_ENABLED = os.getenv("SUPABASE_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
supabase = None
if SUPABASE_ENABLED:
    try:
        from supabase import create_client
        _supabase_url = os.getenv("SUPABASE_URL", "").strip()
        # Server-side key only. Prefer the newer secret/service-role variable;
        # SUPABASE_KEY remains supported for existing .env files.
        _supabase_key = (
            os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
            or os.getenv("SUPABASE_SECRET_KEY", "").strip()
            or os.getenv("SUPABASE_KEY", "").strip()
        )
        if not _supabase_url or not _supabase_key:
            raise RuntimeError(
                "SUPABASE_ENABLED=true requires SUPABASE_URL and a server-side "
                "SUPABASE_SERVICE_ROLE_KEY (or existing SUPABASE_KEY) in .env"
            )
        supabase = create_client(_supabase_url, _supabase_key)
    except Exception as _supabase_init_error:
        print(f"Supabase integration initialization failed: {_supabase_init_error}")
        SUPABASE_ENABLED = False

PORT = int(os.getenv("PORT", "8765"))
APP_SECRET = os.getenv("APP_SECRET", "local-development-secret-change-before-deploy").encode()
SYNC_LOCK = threading.Lock()
SOURCE_STATE: dict[str, dict] = {}
MAX_RESUME_BYTES = 5 * 1024 * 1024
MAX_RESUME_TEXT = 200_000

RESUME_STOPWORDS = set("""
a an and are as at be been being by for from had has have he her hers him his i if in into is it its of on or our ours she that the their theirs them they this to was we were which who will with you your yours
about above after again against all am any because before below between both but can could did do does doing down during each few further here how more most other out over own same so some such than then there these those through under until up very what when where while why would
resume cv curriculum vitae experience experiences work worked working education university college school degree bachelor masters master phd summary objective professional skills skill responsibilities responsible developed developing build built create created design designed led lead leading managed management using utilized use used including various strong excellent ability abilities results team teams company position role
phone email mail address linkedin github website www http https com org net years year month months currently previous present reference references page pages
project projects technology technologies tool tools proficiency proficient knowledge expertise expert advanced basic intermediate environment environments
various specific related regarding excellent responsible responsibility responsibilities
""".split())

RESUME_ALIASES = {
    "js": "javascript", "nodejs": "node", "reactjs": "react", "ts": "typescript",
    "py": "python", "postgresql": "postgres", "k8s": "kubernetes", "dotnet": ".net",
    "csharp": "c#", "cplusplus": "c++", "amazonwebservices": "aws",
    "microsoftazure": "azure", "googlecloudplatform": "gcp", "scikitlearn": "sklearn",
    "artificialintelligence": "ai", "powerbi": "powerbi",
}


def resume_keywords(text):
    words = re.findall(r"(?<![a-z0-9])(?:c\+\+|c#|\.net|[a-z][a-z0-9+#.]{1,30})(?![a-z0-9])", str(text or "").lower())
    terms = [word if word in ("c++", "c#", ".net") else word.strip(".+#") for word in words]
    terms = [RESUME_ALIASES.get(word, word) for word in terms]
    terms = [word for word in terms if word not in RESUME_STOPWORDS and (len(word) >= 2 or word in ("c#",))]
    return set(list(dict.fromkeys(terms))[:600])


def _decode_pdf_literal(value):
    out = bytearray(); i = 0
    escapes = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12}
    while i < len(value):
        b = value[i]
        if b != 92:
            out.append(b); i += 1; continue
        i += 1
        if i >= len(value): break
        b = value[i]
        if b in escapes:
            out.append(escapes[b]); i += 1
        elif b in (10, 13):
            i += 1
            if b == 13 and i < len(value) and value[i] == 10: i += 1
        elif 48 <= b <= 55:
            digits = bytearray([b]); i += 1
            while i < len(value) and len(digits) < 3 and 48 <= value[i] <= 55:
                digits.append(value[i]); i += 1
            out.append(int(digits.decode(), 8) & 255)
        else:
            out.append(b); i += 1
    try: return out.decode("utf-8")
    except UnicodeDecodeError: return out.decode("cp1252", errors="replace")


def _decode_pdf_hex(value):
    try: raw = bytes.fromhex(re.sub(rb"\s+", b"", value).decode("ascii"))
    except Exception: return ""
    if raw[:2] == bytes((254, 255)):
        try: return raw[2:].decode("utf-16-be", errors="replace")
        except Exception: pass
    return raw.decode("cp1252", errors="replace")


def _pdf_stream_text(stream):
    chunks = []
    literal = rb"\(((?:\\.|[^\\)])*)\)"
    for match in re.finditer(literal + rb"\s*(?:Tj|')", stream):
        chunks.append(_decode_pdf_literal(match.group(1)))
    for array in re.finditer(rb"\[((?:.|\r|\n)*?)\]\s*TJ", stream, re.S):
        pieces = []
        for item in re.finditer(literal + rb"|<([0-9A-Fa-f\s]{2,})>", array.group(1)):
            pieces.append(_decode_pdf_literal(item.group(1)) if item.group(1) is not None else _decode_pdf_hex(item.group(2)))
        chunks.append("".join(pieces))
    for match in re.finditer(rb"<([0-9A-Fa-f\s]{2,})>\s*Tj", stream):
        chunks.append(_decode_pdf_hex(match.group(1)))
    return " ".join(chunks)


def extract_resume_text(filename, raw):
    suffix = Path(filename).suffix.lower()
    if suffix == ".txt":
        try: text = raw.decode("utf-8-sig")
        except UnicodeDecodeError: text = raw.decode("cp1252", errors="replace")
    elif suffix in (".docx", ".odt"):
        try:
            with zipfile.ZipFile(BytesIO(raw)) as archive:
                member = "word/document.xml" if suffix == ".docx" else "content.xml"
                info = archive.getinfo(member)
                if info.file_size > 8 * 1024 * 1024: raise ValueError("This document is too large to read.")
                root = ET.fromstring(archive.read(member))
            text = " ".join(node.text or "" for node in root.iter() if node.tag.endswith("}t") or node.tag.endswith("}p"))
        except (zipfile.BadZipFile, KeyError, ET.ParseError):
            raise ValueError("That document could not be read. Upload a valid DOCX or ODT file.")
    elif suffix == ".rtf":
        try: text = raw.decode("utf-8", errors="replace")
        except Exception: text = raw.decode("cp1252", errors="replace")
        text = re.sub(r"\\'[0-9a-fA-F]{2}", lambda m: bytes([int(m.group(0)[2:], 16)]).decode("cp1252", errors="replace"), text)
        text = re.sub(r"\\[a-zA-Z]+-?\d* ?|\\[{}\\]|[{}]", " ", text)
    elif suffix == ".pdf":
        text = ""
        try:
            from pypdf import PdfReader
            reader = PdfReader(BytesIO(raw), strict=False)
            if len(reader.pages) > 120: raise ValueError("The PDF has too many pages. Upload a shorter resume.")
            text = " ".join((page.extract_text() or "") for page in reader.pages)[:MAX_RESUME_TEXT]
        except ImportError:
            pass
        except ValueError:
            raise
        except Exception:
            text = ""
        if not text.strip():
            streams = []
            for match in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", raw, re.S):
                payload = match.group(1)
                streams.append(payload[:MAX_RESUME_TEXT * 4])
                try: streams.append(zlib.decompressobj().decompress(payload, MAX_RESUME_TEXT * 4))
                except zlib.error: pass
            text = " ".join(_pdf_stream_text(block) for block in streams)[:MAX_RESUME_TEXT]
    else:
        raise ValueError("Choose a PDF, DOCX, ODT, RTF, or TXT resume file.")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", text)
    if len(re.sub(r"\s+", "", text)) < 40:
        raise ValueError("We could not read enough text. Use a text-based PDF, DOCX, or TXT file; scanned images need OCR first.")
    return text[:MAX_RESUME_TEXT]


def now():
    return datetime.now(timezone.utc)


def iso(dt=None):
    return (dt or now()).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_date(value):
    if not value:
        return None
    try:
        if isinstance(value, (int, float)) or re.fullmatch(r"\d+(?:\.\d+)?", str(value).strip()):
            epoch = float(value)
            if abs(epoch) > 100_000_000_000:
                epoch /= 1000
            if abs(epoch) > 100_000_000:
                return datetime.fromtimestamp(epoch, tz=timezone.utc)
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def db():
    """Connect to Supabase Postgres while safely decoding URL credentials."""
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is required for persistent Vercel storage.")
    # Be tolerant of common copy/paste formats from Vercel environment settings:
    # surrounding quotes or accidentally including the variable name.
    if url.startswith("DATABASE_URL="):
        url = url.split("=", 1)[1].strip()
    url = url.strip().strip('"').strip("'").strip()
    import psycopg
    from psycopg.rows import dict_row
    from urllib.parse import urlsplit, unquote

    # Parse URL components explicitly. Passing the full URL to libpq can fail
    # when a database password contains reserved URL characters.
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("postgres", "postgresql") or not parsed.hostname:
            raise ValueError("DATABASE_URL must be a PostgreSQL URL")
        username = unquote(parsed.username or "")
        password = unquote(parsed.password or "")
        database = unquote(parsed.path.lstrip("/"))
        if not username or not database:
            raise ValueError("DATABASE_URL is missing its username or database name")
        raw = psycopg.connect(
            host=parsed.hostname,
            port=parsed.port or 5432,
            dbname=database,
            user=username,
            password=password,
            sslmode="require",
            row_factory=dict_row,
            connect_timeout=10,
        )
        # Disable auto-prepared statements for serverless/pooler connections.
        raw.prepare_threshold = None
    except Exception as exc:
        # Do not log the connection URL or password.
        print(f"PostgreSQL connection setup failed ({type(exc).__name__}): {exc}")
        raise
    return PGConnection(raw)


class HybridRow(dict):
    """Support both row['column'] and row[0], as sqlite3.Row did."""
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class PGCursor:
    def __init__(self, cursor):
        self.cursor = cursor
        self.rowcount = cursor.rowcount

    def execute(self, sql, params=()):
        sql = sql.replace("INSERT OR IGNORE INTO", "INSERT INTO")
        if "INSERT OR IGNORE" not in sql and "INSERT INTO saved_jobs" in sql:
            sql = sql.rstrip().rstrip(";") + " ON CONFLICT(user_id,job_id) DO NOTHING"
        # psycopg uses percent-style placeholders. Escape literal percent signs
        # in SQL (for example LIKE '%sponsor%') before translating SQLite ? params.
        # Translate SQLite named placeholders (:name) too; psycopg expects %(name)s.
        # This is required for job imports, which pass dictionaries of values.
        sql = sql.replace("%", "%%")
        if isinstance(params, dict):
            sql = re.sub(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)", lambda m: "%(" + m.group(1) + ")s", sql)
        else:
            sql = sql.replace("?", "%s")
        try:
            self.cursor.execute(sql, params or (), prepare=False)
        except Exception as exc:
            import psycopg
            if isinstance(exc, psycopg.errors.UniqueViolation):
                raise sqlite3.IntegrityError(str(exc)) from exc
            if isinstance(exc, psycopg.Error):
                raise sqlite3.OperationalError(str(exc)) from exc
            raise
        self.rowcount = self.cursor.rowcount
        return self

    def fetchone(self):
        row = self.cursor.fetchone()
        return HybridRow(row) if row is not None else None

    def fetchall(self):
        return [HybridRow(row) for row in self.cursor.fetchall()]


class PGConnection:
    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql, params=()):
        return PGCursor(self.raw.cursor()).execute(sql, params)

    def executescript(self, sql):
        for statement in sql.split(";"):
            if statement.strip():
                self.execute(statement)

    def commit(self):
        self.raw.commit()

    def close(self):
        self.raw.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type:
            self.raw.rollback()
        else:
            self.raw.commit()
        self.raw.close()
        return False


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
          id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
          email TEXT UNIQUE NOT NULL, name TEXT NOT NULL, salt TEXT NOT NULL,
          password_hash TEXT NOT NULL, created_at TEXT NOT NULL,
          target_roles TEXT NOT NULL DEFAULT '', target_skills TEXT NOT NULL DEFAULT '',
          role TEXT NOT NULL DEFAULT 'client', is_active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS sessions (
          token_hash TEXT PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          expires_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS jobs (
          id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, source_key TEXT UNIQUE NOT NULL,
          source TEXT NOT NULL, employer TEXT NOT NULL, title TEXT NOT NULL,
          location TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
          job_url TEXT NOT NULL, apply_url TEXT NOT NULL, posted_at TEXT,
          first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
          remote_type TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '',
          salary TEXT NOT NULL DEFAULT '', sponsor_hint TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS applications (
          id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
          user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          job_id BIGINT REFERENCES jobs(id) ON DELETE SET NULL, source_key TEXT NOT NULL,
          title TEXT NOT NULL, employer TEXT NOT NULL, job_url TEXT NOT NULL,
          applied_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Applied',
          notes TEXT NOT NULL DEFAULT '', UNIQUE(user_id, source_key)
        );
        CREATE TABLE IF NOT EXISTS saved_jobs (
          user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          job_id BIGINT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
          saved_at TEXT NOT NULL, PRIMARY KEY(user_id, job_id)
        );
        CREATE TABLE IF NOT EXISTS resumes (
          user_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
          filename TEXT NOT NULL, content_type TEXT NOT NULL, file_data BYTEA NOT NULL,
          extracted_text TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS source_registry (
          id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, provider TEXT NOT NULL,
          board_token TEXT NOT NULL, added_at TEXT NOT NULL, UNIQUE(provider, board_token)
        );
        CREATE TABLE IF NOT EXISTS source_sync_state (
          source_key TEXT PRIMARY KEY, last_run TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
          error TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS source_sync_cursor (
          source_key TEXT PRIMARY KEY, cursor TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS jobs_seen_idx ON jobs(first_seen_at DESC);
        CREATE INDEX IF NOT EXISTS jobs_posted_idx ON jobs(posted_at DESC);
        CREATE INDEX IF NOT EXISTS applications_user_idx ON applications(user_id, applied_at DESC);
        """)
        c.execute("UPDATE jobs SET active=0 WHERE source LIKE 'Adzuna%'")
        c.execute("UPDATE jobs SET active=0 WHERE source LIKE 'Employer ATS · %' OR source LIKE 'Employer careers · %'")
    email, password = os.getenv("ADMIN_EMAIL", "").strip().lower(), os.getenv("ADMIN_PASSWORD", "")
    if email and password:
        salt = secrets.token_hex(16)
        with db() as c:
            c.execute("""INSERT INTO users(email,name,salt,password_hash,created_at,role,is_active)
              VALUES(?,?,?,?,?,'admin',1)
              ON CONFLICT(email) DO UPDATE SET name=excluded.name,salt=excluded.salt,password_hash=excluded.password_hash,role='admin',is_active=1""",
              (email, "FidoraPath Admin", salt, hash_password(password, salt), iso()))


def request_json(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": "FidoraPathJobs/1.0 contact: support@fidorapath.com", "Accept": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=18) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def request_jobscanner_text(url, accept="application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, text/html;q=0.7"):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in {"jobscanner.online", "www.jobscanner.online"}:
        raise ValueError("The India feed returned an unexpected link.")
    req = urllib.request.Request(url, headers={"User-Agent": "FidoraPathJobs/1.0", "Accept": accept})
    with urllib.request.urlopen(req, timeout=18) as response:
        payload = response.read(5_000_001)
    if len(payload) > 5_000_000:
        raise ValueError("The India feed response is too large to process.")
    return payload.decode("utf-8", errors="replace")


def normalize_job(source, source_id, employer, title, location, description, job_url, apply_url=None,
                  posted_at=None, remote_type="", category="", salary="", sponsor_hint=""):
    if not title or not employer or not job_url:
        return None
    parsed = urllib.parse.urlparse(job_url)
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    apply_url = apply_url or job_url
    ap = urllib.parse.urlparse(apply_url)
    if ap.scheme != "https" or not ap.netloc:
        apply_url = job_url
    key = f"{source}:{source_id}"
    return {"source_key": key, "source": source, "employer": str(employer)[:220], "title": str(title)[:300],
            "location": str(location or "")[:300], "description": str(description or "")[:30000],
            "job_url": job_url[:2000], "apply_url": apply_url[:2000], "posted_at": iso(posted_at) if posted_at else None,
            "remote_type": str(remote_type or "")[:80], "category": str(category or "")[:1500], "salary": str(salary or "")[:160],
            "sponsor_hint": str(sponsor_hint or "")[:40]}


def greenhouse_jobs(token):
    data = request_json(f"https://boards-api.greenhouse.io/v1/boards/{urllib.parse.quote(token)}/jobs?content=true")
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        offices = ", ".join(x.get("location", "") for x in j.get("offices", []) if x.get("location"))
        location = loc or offices
        if not re.search(r"\b(US|USA|United States|UK|GB|United Kingdom|Germany|DE|Ireland|IE|Remote|[A-Z]{2})\b", location, re.I):
            continue
        url = j.get("absolute_url", "")
        yield normalize_job("Greenhouse", f"{token}:{j.get('id')}", token, j.get("title"), location, j.get("content", ""), url, url,
                            None, category=", ".join(x.get("name", "") for x in j.get("departments", [])))


def lever_jobs(token):
    data = request_json(f"https://api.lever.co/v0/postings/{urllib.parse.quote(token)}?mode=json")
    for j in data if isinstance(data, list) else []:
        cats = j.get("categories") or {}
        location = cats.get("allLocations") or cats.get("location") or ""
        if isinstance(location, list):
            location = ", ".join(str(x) for x in location)
        if not re.search(r"\b(US|USA|United States|UK|GB|United Kingdom|Germany|DE|Ireland|IE|Remote|[A-Z]{2})\b", str(location), re.I):
            continue
        desc = "\n".join(str(j.get(k) or "") for k in ("openingPlain", "descriptionPlain", "descriptionBodyPlain"))
        yield normalize_job("Lever", f"{token}:{j.get('id')}", token, j.get("text"), location, desc,
                            j.get("hostedUrl", ""), j.get("applyUrl") or j.get("hostedUrl"),
                            parse_date(j.get("createdAt")), j.get("workplaceType", ""), cats.get("team", ""),
                            str((j.get("salaryRange") or {}).get("min", "")) if j.get("salaryRange") else "")


def ashby_jobs(token):
    data = request_json(f"https://api.ashbyhq.com/posting-api/job-board/{urllib.parse.quote(token)}?includeCompensation=true")
    for j in data.get("jobs", []):
        location = j.get("location", "")
        if not re.search(r"\b(US|USA|United States|UK|GB|United Kingdom|Germany|DE|Ireland|IE|Remote|[A-Z]{2})\b", location, re.I):
            continue
        posted = parse_date(j.get("publishedAt"))
        url = j.get("jobUrl") or j.get("applyUrl") or ""
        comp = j.get("compensation") or {}
        yield normalize_job("Ashby", f"{token}:{j.get('id') or j.get('jobUrl')}", token, j.get("title"), location,
                            j.get("descriptionPlain") or j.get("description", ""), url,
                            j.get("applyUrl") or url, posted, j.get("workplaceType", ""), j.get("department", ""),
                            json.dumps(comp) if comp else "")


def usajobs_jobs():
    key, email = os.getenv("USAJOBS_API_KEY"), os.getenv("USAJOBS_EMAIL")
    if not key or not email:
        return []
    url = "https://data.usajobs.gov/api/search?ResultsPerPage=500&DatePosted=1&Fields=FullText,Details"
    data = request_json(url, {"User-Agent": email, "Authorization-Key": key, "Host": "data.usajobs.gov"})
    out = []
    for item in (data.get("SearchResult", {}).get("SearchResultItems", [])):
        m = item.get("MatchedObjectDescriptor", {})
        locs = m.get("PositionLocation", [])
        location = ", ".join(x.get("LocationName", "") for x in locs)
        if not location:
            location = "United States"
        url = m.get("PositionURI") or ""
        created = parse_date(m.get("PublicationStartDate"))
        out.append(normalize_job("USAJOBS", m.get("PositionID"), m.get("OrganizationName", "US Federal Government"),
                                 m.get("PositionTitle"), location, m.get("UserArea", {}).get("Details", {}).get("JobSummary", ""),
                                 url, url, created, category=m.get("JobCategory", [{}])[0].get("Name", "") if m.get("JobCategory") else ""))
    return out


JOBSCANNER_INDIA_FEED = "https://jobscanner.online/feeds/country/india.xml"
JOBSCANNER_INDIA_CURSOR_KEY = "jobscanner-india-v1"
try:
    JOBSCANNER_INDIA_REFRESH_MINUTES = max(60, int(os.getenv("JOBSCANNER_INDIA_REFRESH_MINUTES", "180")))
except ValueError:
    JOBSCANNER_INDIA_REFRESH_MINUTES = 180


def _jobscanner_local_name(tag):
    return str(tag or "").rsplit("}", 1)[-1].replace("-", "").replace("_", "").lower()


def _jobscanner_values(item, names):
    wanted = {_jobscanner_local_name(name) for name in names}
    values = []
    for node in item.iter():
        if _jobscanner_local_name(node.tag) not in wanted:
            continue
        value = " ".join(" ".join(node.itertext()).split())
        if not value:
            value = str(node.attrib.get("href") or node.attrib.get("url") or "").strip()
        if value:
            values.append(value)
    return values


def _jobscanner_stable_id(item):
    stable = next(iter(_jobscanner_values(item, {"guid", "id", "link", "url"})), "")
    if stable:
        return stable
    return "|".join(next(iter(_jobscanner_values(item, {field})), "") for field in ("title", "company", "location"))


class _JobscannerPageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.json_ld = []
        self._anchor = None
        self._script = False
        self._script_text = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag.lower() == "a":
            self._anchor = {"href": attrs.get("href", ""), "text": ""}
        if tag.lower() == "script" and "ld+json" in str(attrs.get("type", "")).lower():
            self._script = True
            self._script_text = []

    def handle_data(self, data):
        if self._anchor is not None:
            self._anchor["text"] += data
        if self._script:
            self._script_text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._anchor is not None:
            self._anchor["text"] = " ".join(self._anchor["text"].split())
            self.links.append(self._anchor)
            self._anchor = None
        if tag.lower() == "script" and self._script:
            self.json_ld.append("".join(self._script_text))
            self._script = False
            self._script_text = []


def _jobscanner_schema(parser):
    def visit(value):
        if isinstance(value, dict):
            kind = value.get("@type")
            if kind == "JobPosting" or isinstance(kind, list) and "JobPosting" in kind:
                return value
            for child in value.values():
                found = visit(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = visit(child)
                if found:
                    return found
        return None

    for raw in parser.json_ld:
        try:
            found = visit(json.loads(raw))
        except (ValueError, TypeError):
            found = None
        if found:
            return found
    return {}


def _jobscanner_direct_url(value):
    try:
        parsed = urllib.parse.urlparse(html.unescape(str(value or "").strip()))
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if parsed.scheme != "https" or not host or "@" in parsed.netloc:
        return ""
    blocked = set(FREEHIRE_BLOCKED_HOSTS) | {
        "jobscanner.online", "jobicy.com", "jobr.pro", "yubhub.co", "careernest.cloud", "jobvetta.com",
        "jobs.lever.co", "boards.greenhouse.io", "job-boards.greenhouse.io",
        "jobs.ashbyhq.com", "myworkdayjobs.com", "workday.com", "smartrecruiters.com",
        "workable.com", "icims.com", "oraclecloud.com", "successfactors.com", "taleo.net",
    }
    if any(host == domain or host.endswith("." + domain) for domain in blocked):
        # ATS pages are the employer's application site and are valid destinations.
        ats_hosts = {"jobs.lever.co", "boards.greenhouse.io", "job-boards.greenhouse.io", "jobs.ashbyhq.com", "myworkdayjobs.com", "workday.com", "smartrecruiters.com", "workable.com", "icims.com", "oraclecloud.com", "successfactors.com", "taleo.net"}
        if not any(host == domain or host.endswith("." + domain) for domain in ats_hosts):
            return ""
    return urllib.parse.urlunparse(parsed._replace(fragment=""))


def _jobscanner_direct_from_item(item, description):
    direct_names = {"applyurl", "applicationurl", "applicationlink", "externalurl", "employerurl", "sourceurl", "careersurl"}
    candidates = []
    for node in item.iter():
        name = _jobscanner_local_name(node.tag)
        if name in direct_names or name == "link":
            candidates.extend([node.text or "", node.attrib.get("href", ""), node.attrib.get("url", "")])
    parser = _JobscannerPageParser()
    try:
        parser.feed(html.unescape(str(description or "")))
    except Exception:
        pass
    candidates.extend(link.get("href", "") for link in sorted(parser.links, key=lambda row: "apply" not in row.get("text", "").lower()))
    for candidate in candidates:
        url = _jobscanner_direct_url(candidate)
        if url:
            return url
    return ""


def _jobscanner_detail(detail_url):
    page = request_jobscanner_text(detail_url, "text/html, application/xhtml+xml;q=0.9")
    parser = _JobscannerPageParser()
    parser.feed(page)
    schema = _jobscanner_schema(parser)
    links = [link for link in parser.links if _jobscanner_direct_url(link.get("href"))]
    links.sort(key=lambda link: ("apply" not in link.get("text", "").lower(), "career" not in link.get("text", "").lower()))
    apply_url = _jobscanner_direct_url(links[0]["href"]) if links else ""
    return schema, apply_url


def _jobscanner_item_to_record(item):
    title = next(iter(_jobscanner_values(item, {"title"})), "")
    employer = next(iter(_jobscanner_values(item, {"company", "employer", "companyname", "organization"})), "")
    location = next(iter(_jobscanner_values(item, {"location", "joblocation", "geo", "country"})), "")
    description = next(iter(_jobscanner_values(item, {"description", "summary", "contentencoded"})), "")
    detail_url = next(iter(_jobscanner_values(item, {"link", "guid", "url"})), "")
    if detail_url and not detail_url.startswith("https://"):
        detail_url = urllib.parse.urljoin("https://jobscanner.online/", detail_url)
    detail_host = (urllib.parse.urlparse(detail_url).hostname or "").lower()
    if detail_host not in {"jobscanner.online", "www.jobscanner.online"}:
        detail_url = ""

    schema = {}
    apply_url = _jobscanner_direct_from_item(item, description)
    if detail_url and not apply_url:
        schema, apply_url = _jobscanner_detail(detail_url)
    elif detail_url:
        try:
            schema, _ = _jobscanner_detail(detail_url)
        except Exception:
            schema = {}

    if not apply_url:
        return None
    title = str(schema.get("title") or title).strip()
    hiring = schema.get("hiringOrganization") or {}
    employer = str((hiring.get("name") if isinstance(hiring, dict) else "") or employer or "").strip()
    employer = employer or "Employer"

    feed_location = str(location or "").strip()
    locations = []
    non_india_location = False
    schema_locations = schema.get("jobLocation") or []
    if isinstance(schema_locations, dict):
        schema_locations = [schema_locations]
    india_seen = False
    for place in schema_locations if isinstance(schema_locations, list) else []:
        address = (place or {}).get("address") or {}
        if not isinstance(address, dict):
            continue
        country = address.get("addressCountry") or ""
        if isinstance(country, dict):
            country = country.get("name") or country.get("@id") or ""
        if "india" in str(country).lower() or str(country).upper() == "IN":
            india_seen = True
        elif str(country).strip():
            non_india_location = True
        piece = ", ".join(str(value).strip() for value in (address.get("addressLocality"), address.get("addressRegion"), country) if value)
        if piece and piece not in locations:
            locations.append(piece)
    applicant_places = schema.get("applicantLocationRequirements") or []
    if isinstance(applicant_places, dict):
        applicant_places = [applicant_places]
    for place in applicant_places if isinstance(applicant_places, list) else []:
        if isinstance(place, dict) and "india" in str(place.get("name") or place.get("addressCountry") or "").lower():
            india_seen = True
    if locations:
        location = "; ".join(locations)
    if non_india_location and not india_seen:
        return None
    if "india" not in location.lower() and (india_seen or "india" in feed_location.lower()):
        location = ", ".join(x for x in (location, "India") if x)
    if "india" not in location.lower():
        return None

    description = str(schema.get("description") or description or "")
    posted_value = schema.get("datePosted") or next(iter(_jobscanner_values(item, {"pubdate", "datepublished", "dateposted"})), "")
    posted = parse_date(posted_value)
    if not posted and posted_value:
        try:
            posted = parsedate_to_datetime(posted_value).astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            pass
    remote = next(iter(_jobscanner_values(item, {"remote", "workstyle", "workmode"})), "")
    if remote.strip().lower() in {"true", "1", "yes"}:
        remote = "Remote"
    elif remote.strip().lower() in {"false", "0", "no"}:
        remote = ""
    if not remote:
        remote = "Remote" if re.search(r"\bremote\b", location, re.I) else ""
    category = "|".join(dict.fromkeys(_jobscanner_values(item, {"category", "department", "jobtype"})))[:1500]
    salary = next(iter(_jobscanner_values(item, {"salary", "salaryrange"})), "")
    stable_id = _jobscanner_stable_id(item) or detail_url or apply_url
    source_id = hashlib.sha256(stable_id.encode("utf-8", errors="replace")).hexdigest()[:24]
    return normalize_job("Jobscanner India", source_id, employer, title, location, description,
                         apply_url, apply_url, posted, remote, category, salary)


def _jobscanner_safe_record(item):
    try:
        return _jobscanner_item_to_record(item)
    except Exception:
        return None


def sync_jobscanner_india(force=False):
    """Import recent India jobs from Jobscanner's public country RSS feed; no key or account is required."""
    started = now()
    with db() as c:
        row = c.execute("SELECT cursor FROM source_sync_cursor WHERE source_key=?", (JOBSCANNER_INDIA_CURSOR_KEY,)).fetchone()
        count = c.execute("SELECT COUNT(*) FROM jobs WHERE source LIKE 'Jobscanner India%' AND active=1").fetchone()[0]
    previous = parse_date(row["cursor"]) if row else None
    if not force and previous and started - previous < timedelta(minutes=JOBSCANNER_INDIA_REFRESH_MINUTES):
        SOURCE_STATE["jobscanner_india"] = {"status": "ok", "last_run": iso(previous), "count": count,
                                             "warning": f"Automatic India feed refresh runs every {JOBSCANNER_INDIA_REFRESH_MINUTES} minutes."}
        return 0

    xml_text = request_jobscanner_text(JOBSCANNER_INDIA_FEED)
    root = ET.fromstring(xml_text)
    items = [node for node in root.iter() if _jobscanner_local_name(node.tag) in {"item", "entry"}]
    if not items:
        raise RuntimeError("The public India feed did not contain any readable job records.")

    pending = []
    stamp = iso(started)
    with db() as c:
        for item in items:
            stable = _jobscanner_stable_id(item)
            key = "Jobscanner India:" + hashlib.sha256(stable.encode("utf-8", errors="replace")).hexdigest()[:24]
            existing = c.execute("SELECT id FROM jobs WHERE source_key=?", (key,)).fetchone()
            if existing:
                c.execute("UPDATE jobs SET last_seen_at=?,active=1,updated_at=? WHERE source_key=?", (stamp, stamp, key))
                continue
            pending.append(item)

    saved = 0
    # Fetch employer destinations only for new feed rows; the recurring feed poll remains lightweight.
    with ThreadPoolExecutor(max_workers=3) as pool:
        records = list(pool.map(_jobscanner_safe_record, pending[:150]))
    resolved = sum(bool(record) for record in records)
    if pending and not resolved:
        raise RuntimeError("India feed returned listings, but none had a resolvable direct employer application link. The feed will retry on the next refresh.")
    with db() as c:
        for record in records:
            if not record:
                continue
            duplicate = c.execute("SELECT 1 FROM jobs WHERE lower(apply_url)=lower(?) AND source_key<>? AND active=1 LIMIT 1",
                                  (record["apply_url"], record["source_key"])).fetchone()
            if duplicate:
                continue
            c.execute("""INSERT INTO jobs(source_key,source,employer,title,location,description,job_url,apply_url,posted_at,first_seen_at,last_seen_at,active,remote_type,category,salary,sponsor_hint,updated_at)
              VALUES(:source_key,:source,:employer,:title,:location,:description,:job_url,:apply_url,:posted_at,:stamp,:stamp,1,:remote_type,:category,:salary,:sponsor_hint,:stamp)
              ON CONFLICT(source_key) DO UPDATE SET employer=excluded.employer,title=excluded.title,location=excluded.location,description=excluded.description,job_url=excluded.job_url,apply_url=excluded.apply_url,posted_at=COALESCE(excluded.posted_at,jobs.posted_at),last_seen_at=excluded.last_seen_at,active=1,remote_type=excluded.remote_type,category=excluded.category,salary=excluded.salary,updated_at=excluded.updated_at""",
              {**record, "stamp": stamp})
            saved += 1
        c.execute("INSERT INTO source_sync_cursor(source_key,cursor,updated_at) VALUES(?,?,?) ON CONFLICT(source_key) DO UPDATE SET cursor=excluded.cursor,updated_at=excluded.updated_at",
                  (JOBSCANNER_INDIA_CURSOR_KEY, stamp, stamp))
        count = c.execute("SELECT COUNT(*) FROM jobs WHERE source LIKE 'Jobscanner India%' AND active=1").fetchone()[0]
    SOURCE_STATE["jobscanner_india"] = {"status": "partial" if resolved < len(records) else "ok", "last_run": stamp, "count": count,
                                        "new_count": saved,
                                        "warning": "Some India listings did not expose a direct employer application link and were skipped." if resolved < len(records) else "India listings are imported only when an official employer application link is available."}
    return saved


FREEHIRE_API = "https://freehire.me/api/v1/jobs/search"
FREEHIRE_COUNTRIES = "US,GB,DE,IE,IN"
FREEHIRE_CURSOR_KEY = "freehire-v2-india"
FREEHIRE_BLOCKED_HOSTS = (
    "freehire.me", "indeed.com", "linkedin.com", "glassdoor.com", "ziprecruiter.com",
    "monster.com", "careerbuilder.com", "simplyhired.com", "adzuna.com", "talent.com",
    "jooble.org", "careerjet.com", "himalayas.app", "remoteok.com", "reed.co.uk",
    "totaljobs.com", "jobrapido.com", "whatjobs.com",
    "naukri.com", "foundit.in", "shine.com", "timesjobs.com", "freshersworld.com",
    "workindia.in", "apna.co", "cutshort.io", "hirist.tech", "iimjobs.com",
    "instahyre.com", "wellfound.com", "jobsora.com", "devglobaljobs.com",
)


def freehire_job_record(item):
    """Normalize a Freehire listing, keeping only a direct employer/ATS destination."""
    posting_url = str(item.get("url") or "").strip()
    apply_url = str(item.get("apply_url") or posting_url).strip()
    def blocked_url(value):
        parsed_url = urllib.parse.urlparse(value)
        host = (parsed_url.hostname or "").lower().removeprefix("www.")
        return parsed_url.scheme != "https" or not host or any(host == blocked or host.endswith("." + blocked) for blocked in FREEHIRE_BLOCKED_HOSTS)
    if blocked_url(apply_url):
        apply_url = posting_url
    if blocked_url(apply_url):
        return None

    enrichment = item.get("enrichment") or {}
    company = item.get("company") or ""
    if isinstance(company, dict):
        company = company.get("name") or company.get("display_name") or ""
    country_aliases = {"UNITED STATES": "US", "UNITED STATES OF AMERICA": "US", "USA": "US",
                       "UNITED KINGDOM": "GB", "UK": "GB", "GREAT BRITAIN": "GB",
                       "GERMANY": "DE", "IRELAND": "IE", "INDIA": "IN", "REPUBLIC OF INDIA": "IN"}
    country_values = item.get("countries") or []
    if isinstance(country_values, str): country_values = [country_values]
    countries = [country_aliases.get(str(country).strip().upper(), str(country).strip().upper())
                 for country in country_values]
    country_names = {"US": "United States", "GB": "United Kingdom", "DE": "Germany", "IE": "Ireland", "IN": "India"}
    if countries and not set(countries).intersection(country_names):
        return None
    location = str(item.get("location") or "").strip()
    cities = item.get("cities") or []
    if isinstance(cities, str): cities = [cities]
    city_names = [str(city).strip() for city in cities if str(city).strip()]
    if city_names and not any(city.lower() in location.lower() for city in city_names):
        location = ", ".join(value for value in (location, ", ".join(city_names[:5])) if value)
    country_label = ", ".join(country_names[x] for x in countries if x in country_names)
    if country_label:
        missing_names = [name for name in country_label.split(", ") if name.lower() not in location.lower()]
        if missing_names: location = ", ".join(value for value in (location, ", ".join(missing_names)) if value)

    source_provider = str(item.get("source") or "employer listing")[:55]
    source = f"Freehire · {source_provider}"
    domains = enrichment.get("domains") or []
    skills = item.get("skills") or []
    if isinstance(domains, str): domains = [domains]
    if isinstance(skills, str): skills = [skills]
    categories = [enrichment.get("category") or ""] + list(domains) + list(skills)
    category = "|".join(dict.fromkeys(str(value).strip() for value in categories if value))[:1500]
    low, high = enrichment.get("salary_min"), enrichment.get("salary_max")
    salary = ""
    if low is not None or high is not None:
        amount = f"{low if low is not None else ''}–{high if high is not None else ''}".strip("–")
        salary = " ".join(x for x in (amount, enrichment.get("salary_currency"),
                                      f"per {enrichment.get('salary_period')}" if enrichment.get("salary_period") else "") if x)
    remote = str(item.get("work_mode") or "").replace("onsite", "on-site").replace("on_site", "on-site")
    visa_hint = enrichment.get("visa_sponsorship")
    sponsor_hint = "provider_positive" if visa_hint is True else "provider_no_signal" if visa_hint is False else ""
    slug = str(item.get("public_slug") or item.get("external_id") or "").strip()
    if not slug:
        return None
    if blocked_url(posting_url):
        posting_url = apply_url
    return normalize_job(source, slug, str(company), item.get("title"), location,
                         item.get("description") or "", posting_url or apply_url, apply_url,
                         parse_date(item.get("posted_at")), remote, category, salary, sponsor_hint)


def save_freehire_record(c, item, stamp):
    normalized = freehire_job_record(item)
    if not normalized:
        return False
    c.execute("""INSERT INTO jobs(source_key,source,employer,title,location,description,job_url,apply_url,posted_at,first_seen_at,last_seen_at,active,remote_type,category,salary,sponsor_hint,updated_at)
      VALUES(:source_key,:source,:employer,:title,:location,:description,:job_url,:apply_url,:posted_at,:stamp,:stamp,1,:remote_type,:category,:salary,:sponsor_hint,:stamp)
      ON CONFLICT(source_key) DO UPDATE SET source=excluded.source,employer=excluded.employer,title=excluded.title,location=excluded.location,description=excluded.description,job_url=excluded.job_url,apply_url=excluded.apply_url,posted_at=COALESCE(excluded.posted_at,jobs.posted_at),last_seen_at=excluded.last_seen_at,active=1,remote_type=excluded.remote_type,category=excluded.category,salary=excluded.salary,sponsor_hint=excluded.sponsor_hint,updated_at=excluded.updated_at""",
      {**normalized, "stamp": stamp})
    return True


def sync_freehire():
    """Pull recent open listings from the public API; no key or account is needed."""
    started = now()
    with db() as c:
        row = c.execute("SELECT cursor FROM source_sync_cursor WHERE source_key=?", (FREEHIRE_CURSOR_KEY,)).fetchone()
    previous = parse_date(row["cursor"]) if row else None
    # One-hour overlap protects against new records shifting offset pages between syncs.
    lower_bound = (previous - timedelta(hours=1)) if previous else started - timedelta(hours=24)
    offset = 0
    saved = 0
    pages = 0
    max_seen = previous
    capped = False
    try:
        while offset + 100 <= 10000:
            params = {"countries": FREEHIRE_COUNTRIES, "open_within_days": 2,
                      "sort": "created_at", "order": "desc", "limit": 100, "offset": offset}
            url = FREEHIRE_API + "?" + urllib.parse.urlencode(params)
            page = request_json(url)
            if page.get("error"):
                raise RuntimeError(str(page["error"]))
            ignored = (page.get("meta") or {}).get("ignored_params") or []
            if ignored:
                raise RuntimeError("Freehire ignored required search filters: " + ", ".join(map(str, ignored)))
            rows = page.get("data")
            if not isinstance(rows, list):
                raise RuntimeError("Freehire returned an unexpected response format.")
            pages += 1
            stamp = iso()
            reached_old = False
            with db() as c:
                for item in rows:
                    created = parse_date(item.get("created_at"))
                    if created and (max_seen is None or created > max_seen):
                        max_seen = created
                    if previous and created and created < lower_bound:
                        reached_old = True
                        continue
                    if not previous and created and created < lower_bound:
                        reached_old = True
                        continue
                    saved += bool(save_freehire_record(c, item, stamp))
            if reached_old or len(rows) < 100:
                break
            offset += len(rows)
            if offset + 100 > 10000:
                capped = True
        with db() as c:
            c.execute("INSERT INTO source_sync_cursor(source_key,cursor,updated_at) VALUES(?,?,?) ON CONFLICT(source_key) DO UPDATE SET cursor=excluded.cursor,updated_at=excluded.updated_at",
                      (FREEHIRE_CURSOR_KEY, iso(started), iso()))
        SOURCE_STATE["freehire"] = {"status": "partial" if capped else "ok", "last_run": iso(),
                                    "count": saved, "pages": pages,
                                    "warning": "Freehire pagination limit reached; newest results imported." if capped else "",
                                    "note": "Open listings checked when returned by Freehire; application link opens the job source."}
        return saved
    except Exception as e:
        SOURCE_STATE["freehire"] = {"status": "error", "last_run": iso(), "error": str(e)[:220]}
        raise


def all_sources(force=False):
    path = ROOT / "sources.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        config = {"greenhouse": [], "lever": [], "ashby": []}
    registered = {kind: list(config.get(kind, [])) for kind in ("greenhouse", "lever", "ashby")}
    try:
        with db() as c:
            for r in c.execute("SELECT provider,board_token FROM source_registry").fetchall():
                registered.setdefault(r["provider"], []).append(r["board_token"])
    except sqlite3.OperationalError:
        pass
    for kind, fn in (("greenhouse", greenhouse_jobs), ("lever", lever_jobs), ("ashby", ashby_jobs)):
        for token in dict.fromkeys(registered.get(kind, [])):
            yield f"{kind}:{token}", lambda token=token, fn=fn: list(fn(token))
    if os.getenv("USAJOBS_API_KEY"):
        yield "usajobs", usajobs_jobs
    yield "freehire", sync_freehire
    yield "jobscanner_india", lambda: sync_jobscanner_india(force=force)


def mirror_jobs_to_supabase(batch_size=500):
    """Mirror local jobs to Supabase. Requires the companion SQL migration first."""
    if not SUPABASE_ENABLED or supabase is None:
        return {"status": "disabled"}
    # Requires supabase_migration.sql to be run first. source_key is the
    # stable unique key used for idempotent upserts; this never deletes SQLite data.
    columns = ("source_key", "source", "employer", "title", "location", "description", "job_url", "apply_url",
               "posted_at", "first_seen_at", "last_seen_at", "active", "remote_type", "category", "salary", "sponsor_hint", "updated_at")
    with db() as c:
        rows = c.execute("SELECT " + ",".join(columns) + " FROM jobs ORDER BY id").fetchall()
    total = 0
    for start in range(0, len(rows), batch_size):
        batch = []
        for row in rows[start:start + batch_size]:
            item = dict(row)
            # Supabase columns use its existing names; migration adds the extra metadata columns.
            item["company_name"] = item.pop("employer")
            item["job_title"] = item.pop("title")
            item["job_url"] = item.get("apply_url") or item["job_url"]
            item["posted_at"] = item.get("posted_at") or item.get("first_seen_at")
            batch.append(item)
        supabase.table("job_listings").upsert(batch, on_conflict="source_key").execute()
        total += len(batch)
    return {"status": "ok", "count": total}


def sync_sources(force=False):
    if not SYNC_LOCK.acquire(blocking=False):
        return {"status": "already_running"}
    total = 0
    errors = []
    try:
        for name, loader in all_sources(force=force):
            try:
                if name in {"freehire", "jobscanner_india"}:
                    total += loader()
                    continue
                records = loader()
                stamp = iso()
                with db() as c:
                    family = {"greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby"}
                    prefix = family.get(name.split(":", 1)[0])
                    if prefix:
                        board = name.split(":", 1)[1]
                        c.execute("UPDATE jobs SET active=0 WHERE source_key LIKE ?", (f"{prefix}:{board}:%",))
                    for item in records:
                        if not item:
                            continue
                        c.execute("""INSERT INTO jobs(source_key,source,employer,title,location,description,job_url,apply_url,posted_at,first_seen_at,last_seen_at,active,remote_type,category,salary,sponsor_hint,updated_at)
                          VALUES(:source_key,:source,:employer,:title,:location,:description,:job_url,:apply_url,:posted_at,:stamp,:stamp,1,:remote_type,:category,:salary,:sponsor_hint,:stamp)
                          ON CONFLICT(source_key) DO UPDATE SET employer=excluded.employer,title=excluded.title,location=excluded.location,description=excluded.description,job_url=excluded.job_url,apply_url=excluded.apply_url,posted_at=COALESCE(excluded.posted_at,jobs.posted_at),last_seen_at=excluded.last_seen_at,active=1,remote_type=excluded.remote_type,category=excluded.category,salary=excluded.salary,sponsor_hint=excluded.sponsor_hint,updated_at=excluded.updated_at""", {**item, "stamp": stamp})
                        total += 1
                SOURCE_STATE[name] = {"status": "ok", "last_run": stamp, "count": len(records)}
            except Exception as e:
                errors.append({"source": name, "error": str(e)[:220]})
                SOURCE_STATE[name] = {"status": "error", "last_run": iso(), "error": str(e)[:220]}
        remote_sync = None
        if SUPABASE_ENABLED:
            try:
                remote_sync = mirror_jobs_to_supabase()
            except Exception as remote_error:
                remote_sync = {"status": "error", "error": str(remote_error)[:220]}
                print(f"Supabase job mirror failed: {remote_error}")
        result = {"status": "partial" if errors else "ok", "records": total, "errors": errors, "sources": SOURCE_STATE}
        if remote_sync is not None:
            result["supabase"] = remote_sync
        return result
    finally:
        SYNC_LOCK.release()


def scheduler():
    while True:
        try:
            sync_sources()
        except Exception:
            pass
        time.sleep(900)


def hash_password(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 310000).hex()


def token_hash(token):
    return hmac.new(APP_SECRET, token.encode(), hashlib.sha256).hexdigest()


class Handler(BaseHTTPRequestHandler):
    server_version = "FidoraPathPortal/1.0"
    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def send_json(self, data, status=200, headers=None):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items(): self.send_header(k, v)
        self.end_headers(); self.wfile.write(body)

    def body_json(self):
        try:
            n = min(int(self.headers.get("Content-Length", 0)), 1_000_000)
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def upload_resume(self):
        user = self.require_user()
        if not user: return
        try: length = int(self.headers.get("Content-Length", "0"))
        except ValueError: return self.send_json({"error": "Invalid upload size."}, 400)
        if length < 1 or length > MAX_RESUME_BYTES + 128 * 1024:
            return self.send_json({"error": "Choose a resume smaller than 5 MB."}, 413)
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data;"):
            return self.send_json({"error": "Choose a resume file to upload."}, 400)
        raw_body = self.rfile.read(length)
        envelope = BytesParser(policy=email_policy).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + raw_body
        )
        upload = next((part for part in envelope.iter_parts()
                       if part.get_content_disposition() == "form-data"
                       and part.get_param("name", header="content-disposition") == "resume"), None)
        if not upload:
            return self.send_json({"error": "Choose a resume file to upload."}, 400)
        filename = str(upload.get_filename() or "").replace("\\", "/").split("/")[-1]
        filename = re.sub(r"[\x00-\x1f\x7f]", "", filename).strip()[:160]
        payload = upload.get_payload(decode=True) or b""
        if not filename or not payload:
            return self.send_json({"error": "The selected resume file is empty."}, 400)
        if len(payload) > MAX_RESUME_BYTES:
            return self.send_json({"error": "Choose a resume smaller than 5 MB."}, 413)
        suffix = Path(filename).suffix.lower()
        if suffix == ".pdf" and not payload.startswith(b"%PDF-"):
            return self.send_json({"error": "This file is not a valid PDF."}, 400)
        if suffix in (".docx", ".odt") and not zipfile.is_zipfile(BytesIO(payload)):
            return self.send_json({"error": "This file is not a valid DOCX document."}, 400)
        try: extracted = extract_resume_text(filename, payload)
        except ValueError as error: return self.send_json({"error": str(error)}, 400)
        except Exception: return self.send_json({"error": "We could not read this resume. Try a text-based PDF, DOCX, or TXT file."}, 400)
        mime = "application/pdf" if suffix == ".pdf" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document" if suffix == ".docx" else "application/vnd.oasis.opendocument.text" if suffix == ".odt" else "application/rtf" if suffix == ".rtf" else "text/plain"
        with db() as c:
            c.execute("""INSERT INTO resumes(user_id,filename,content_type,file_data,extracted_text,updated_at)
              VALUES(?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET filename=excluded.filename,
              content_type=excluded.content_type,file_data=excluded.file_data,extracted_text=excluded.extracted_text,
              updated_at=excluded.updated_at""", (user["id"], filename, mime, sqlite3.Binary(payload), extracted, iso()))
        return self.send_json({"ok": True, "filename": filename}, 201)

    def current_user(self):
        cookie = self.headers.get("Cookie", "")
        m = re.search(r"(?:^|;\s*)fp_session=([^;]+)", cookie)
        if not m: return None
        with db() as c:
            return c.execute("SELECT users.* FROM sessions JOIN users ON users.id=sessions.user_id WHERE token_hash=? AND expires_at>? AND users.is_active=1", (token_hash(m.group(1)), iso())).fetchone()

    def require_user(self):
        user = self.current_user()
        if not user:
            self.send_json({"error": "Sign in required"}, 401); return None
        return user

    def require_admin(self):
        user = self.require_user()
        if user and user["role"] != "admin":
            self.send_json({"error": "Administrator access required"}, 403); return None
        return user

    def do_GET(self):
        path = urllib.parse.urlparse(self.path)
        if path.path.startswith("/api/"):
            return self.api_get(path.path, urllib.parse.parse_qs(path.query))
        name = "index.html" if path.path == "/" else path.path.lstrip("/")
        candidate = (STATIC / name).resolve()
        try:
            candidate.relative_to(STATIC.resolve())
        except ValueError:
            self.send_error(404); return
        if not candidate.is_file():
            self.send_error(404); return
        data = candidate.read_bytes()
        mime = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".avif": "image/avif", ".svg": "image/svg+xml", ".pdf": "application/pdf"}.get(candidate.suffix.lower(), "application/octet-stream")
        self.send_response(200); self.send_header("Content-Type", mime); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/profile/resume": return self.upload_resume()
        data = self.body_json()
        if path == "/api/login":
            email = str(data.get("email", "")).strip().lower()[:254]
            password = str(data.get("password", ""))
            if not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email) or len(password) < 4:
                return self.send_json({"error": "Enter a valid email and a password of at least 4 characters."}, 400)
            try:
                with db() as c:
                    row = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
                    if not row or not row["is_active"] or not hmac.compare_digest(hash_password(password,row["salt"]),row["password_hash"]):
                        return self.send_json({"error": "Email or password is incorrect."}, 401)
                    token = secrets.token_urlsafe(36)
                    c.execute("INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,?)", (token_hash(token), row["id"], iso(now()+timedelta(days=14))))
            except Exception as exc:
                import traceback
                print("FidoraJobs login database operation failed:")
                traceback.print_exc()
                return self.send_json({"error": "Login database operation failed", "type": type(exc).__name__, "detail": str(exc)[:300]}, 500)
            cookie = f"fp_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=1209600" + ("; Secure" if os.getenv("VERCEL") else "")
            return self.send_json({"id": row["id"], "name": row["name"], "email": email}, headers={"Set-Cookie": cookie})
        user = self.require_user()
        if not user: return
        if path == "/api/assistant/chat":
            api_key = os.getenv("AI_API_KEY", "").strip() or os.getenv("OPENAI_API_KEY", "").strip()
            if not api_key:
                return self.send_json({"error":"The AI assistant is not connected yet. FidoraPath support can help at support@fidorapath.in. The portal administrator needs to configure AI_API_KEY in Vercel to enable AI chat."}, 503)
            messages = data.get("messages", [])
            if not isinstance(messages, list):
                return self.send_json({"error":"Send a valid conversation history."}, 400)
            safe_messages = []
            for item in messages[-12:]:
                if not isinstance(item, dict) or item.get("role") not in ("user", "assistant"):
                    continue
                content = str(item.get("content", "")).strip()[:2500]
                if content:
                    safe_messages.append({"role": item["role"], "content": content})
            if not safe_messages or safe_messages[-1]["role"] != "user":
                return self.send_json({"error":"Please enter a question first."}, 400)
            if sum(len(m["content"]) for m in safe_messages) > 12000:
                return self.send_json({"error":"This conversation is too long. Start a new question."}, 413)
            system_prompt = (
                "You are Fidora Assistant, a helpful, capable career and job-search assistant inside FidoraJobs, "
                "a FidoraPath service. Answer naturally and thoughtfully, support follow-up questions, explain job search, "
                "resume writing, ATS formatting, LinkedIn profiles, application tracking, interviews, and job-match estimates. "
                "Never claim a job sponsors H-1B unless there is explicit, current employer evidence; distinguish evidence from "
                "guarantees and advise checking the official employer career page. Match percentages are estimates, not hiring "
                "probabilities. Do not invent portal data, job listings, employer facts, or visa/legal advice. Ask concise clarifying "
                "questions when useful. Do not claim to have inspected the user's resume or applications: no resume, password, "
                "email, or application records are provided to you by default. Respect privacy and ask the user to paste only "
                "information they are comfortable sharing. Mention FidoraPath services (resume/ATS optimization, LinkedIn profile "
                "support, and job application assistance) only when genuinely relevant, as an optional way to get help; never pressure "
                "the user and never display service prices. Do not say you are ChatGPT or Gemini; you are Fidora Assistant."
            )
            endpoint = os.getenv("AI_BASE_URL", "https://api.openai.com/v1/chat/completions").strip()
            model = os.getenv("AI_MODEL", "gpt-4o-mini").strip()
            if not endpoint.startswith("https://"):
                return self.send_json({"error":"AI_BASE_URL must use HTTPS."}, 500)
            payload = json.dumps({"model":model,"messages":[{"role":"system","content":system_prompt}]+safe_messages,"temperature":0.5,"max_tokens":900}).encode("utf-8")
            req = urllib.request.Request(endpoint, data=payload, headers={"Content-Type":"application/json","Authorization":"Bearer "+api_key}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=22) as response:
                    result = json.loads(response.read(1_000_000).decode("utf-8"))
                answer = str(result.get("choices", [{}])[0].get("message", {}).get("content", "")).strip()
                if not answer:
                    return self.send_json({"error":"The AI provider returned an empty response. Please try again."}, 502)
                return self.send_json({"reply":answer[:10000],"model":model})
            except urllib.error.HTTPError as exc:
                print("Fidora Assistant provider error:", exc.code)
                return self.send_json({"error":"The AI service could not complete that request. Check the AI provider key, model and billing configuration, then try again."}, 502)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
                print("Fidora Assistant request failed:", type(exc).__name__)
                return self.send_json({"error":"The AI service is temporarily unavailable. Please try again in a moment."}, 502)
        if path == "/api/profile":
            extracted = str(data.get("extracted_text", ""))[:MAX_RESUME_TEXT]
            filename = str(data.get("filename", "resume.txt"))[:160] or "resume.txt"
            if len(extracted.strip()) < 20:
                return self.send_json({"error": "Paste at least 20 characters of resume text."}, 400)
            with db() as c:
                c.execute("UPDATE users SET target_roles=?,target_skills=? WHERE id=?",
                          (str(data.get("target_roles") or "")[:1000], str(data.get("target_skills") or "")[:2000], user["id"]))
                c.execute("""INSERT INTO resumes(user_id,filename,content_type,file_data,extracted_text,updated_at)
                  VALUES(?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET filename=excluded.filename,
                  content_type=excluded.content_type,file_data=excluded.file_data,extracted_text=excluded.extracted_text,
                  updated_at=excluded.updated_at""",
                  (user["id"], filename, "text/plain", sqlite3.Binary(extracted.encode("utf-8")), extracted, iso()))
            return self.send_json({"ok": True, "filename": filename})
        if path == "/api/admin/sources":
            if user["role"] != "admin": return self.send_json({"error": "Administrator access required"}, 403)
            provider = str(data.get("provider", "")).strip().lower()
            board = str(data.get("board", "")).strip()
            if provider not in ("greenhouse", "lever", "ashby") or not board:
                return self.send_json({"error": "Choose Greenhouse, Lever, or Ashby and enter its official board URL or token."}, 400)
            if "://" in board:
                parsed = urllib.parse.urlparse(board)
                host = (parsed.hostname or "").lower()
                permitted = {
                    "greenhouse": {"boards.greenhouse.io", "job-boards.greenhouse.io"},
                    "lever": {"jobs.lever.co", "jobs.eu.lever.co"},
                    "ashby": {"jobs.ashbyhq.com"},
                }
                if parsed.scheme != "https" or host not in permitted[provider]:
                    return self.send_json({"error": "Use a supported official careers-board URL for the selected provider."}, 400)
                board = (parsed.path or "").strip("/").split("/")[0]
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", board):
                return self.send_json({"error": "The board token could not be read. Paste the employer's hosted job-board URL."}, 400)
            try:
                with db() as c:
                    c.execute("INSERT INTO source_registry(provider,board_token,added_at) VALUES(?,?,?)", (provider,board,iso()))
            except sqlite3.IntegrityError:
                return self.send_json({"error": "That board is already connected."}, 409)
            threading.Thread(target=sync_sources, daemon=True).start()
            return self.send_json({"ok": True, "provider": provider, "board": board}, 201)
        if path == "/api/admin/users":
            if user["role"] != "admin": return self.send_json({"error": "Administrator access required"}, 403)
            email = str(data.get("email", "")).strip().lower()[:254]
            name = str(data.get("name", "")).strip()[:100]
            password = str(data.get("password", ""))
            if not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email) or not name:
                return self.send_json({"error": "Enter a valid email and client name."}, 400)
            if len(password) < 4:
                return self.send_json({"error": "Create a password with at least 4 characters."}, 400)
            salt = secrets.token_hex(16)
            try:
                with db() as c:
                    c.execute("INSERT INTO users(email,name,salt,password_hash,created_at,role,is_active) VALUES(?,?,?,?,?,'client',1)", (email,name,salt,hash_password(password,salt),iso()))
            except sqlite3.IntegrityError:
                return self.send_json({"error": "An account already exists for this email."}, 409)
            return self.send_json({"ok": True, "email": email})
        if path.startswith("/api/admin/users/") and path.endswith("/reset"):
            if user["role"] != "admin": return self.send_json({"error": "Administrator access required"}, 403)
            try: client_id = int(path.split("/")[4])
            except Exception: return self.send_json({"error": "Invalid client id"}, 400)
            password = str(data.get("password", ""))
            if len(password) < 4:
                return self.send_json({"error": "Use a new password with at least 4 characters."}, 400)
            salt = secrets.token_hex(16)
            with db() as c:
                changed = c.execute("UPDATE users SET salt=?,password_hash=? WHERE id=? AND role='client'", (salt,hash_password(password,salt),client_id)).rowcount
                if changed: c.execute("DELETE FROM sessions WHERE user_id=?", (client_id,))
            if not changed: return self.send_json({"error": "Client account not found."}, 404)
            return self.send_json({"ok": True})
        if path == "/api/logout":
            m = re.search(r"(?:^|;\s*)fp_session=([^;]+)", self.headers.get("Cookie", ""))
            if m:
                with db() as c: c.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(m.group(1)),))
            return self.send_json({"ok": True}, headers={"Set-Cookie": "fp_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"})
        if path == "/api/sync":
            if user["role"] != "admin":
                return self.send_json({"error": "Administrator access required"}, 403)
            # Vercel serverless runtimes may freeze background threads as soon as
            # the response returns. Run the sync in this request so results persist.
            try:
                result = sync_sources(force=True)
                return self.send_json(result, 200 if result.get("status") in {"ok", "partial", "already_running"} else 502)
            except Exception as exc:
                return self.send_json({"status": "error", "error": str(exc)[:300]}, 502)
        if path == "/api/applications":
            job_id = data.get("job_id")
            if job_id:
                with db() as c: job = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                if not job: return self.send_json({"error":"Job not found"},404)
                record = {"source_key":job["source_key"],"title":job["title"],"employer":job["employer"],"job_url":job["job_url"],"job_id":job["id"]}
            else:
                record = {"source_key":str(data.get("source_key") or secrets.token_hex(12)),"title":str(data.get("title") or "")[:300],"employer":str(data.get("employer") or "")[:220],"job_url":str(data.get("job_url") or "")[:2000],"job_id":None}
            if not record["title"] or not record["employer"]: return self.send_json({"error":"Job title and employer are required."},400)
            if urllib.parse.urlparse(record["job_url"]).scheme != "https": return self.send_json({"error":"A secure job URL is required."},400)
            with db() as c:
                c.execute("""INSERT INTO applications(user_id,job_id,source_key,title,employer,job_url,applied_at,status,notes) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,source_key) DO UPDATE SET applied_at=excluded.applied_at,status=excluded.status,notes=excluded.notes""", (user["id"],record["job_id"],record["source_key"],record["title"],record["employer"],record["job_url"],data.get("applied_at") or iso(),str(data.get("status") or "Applied")[:50],str(data.get("notes") or "")[:2000]))
            return self.send_json({"ok":True})
        if path == "/api/saved":
            try: jid=int(data.get("job_id"))
            except Exception: return self.send_json({"error":"Invalid job id"},400)
            with db() as c: c.execute("INSERT OR IGNORE INTO saved_jobs(user_id,job_id,saved_at) VALUES(?,?,?)",(user["id"],jid,iso()))
            return self.send_json({"ok":True})
        if path == "/api/profile":
            with db() as c: c.execute("UPDATE users SET target_roles=?,target_skills=? WHERE id=?",(str(data.get("target_roles") or "")[:1000],str(data.get("target_skills") or "")[:2000],user["id"]))
            return self.send_json({"ok":True})
        return self.send_json({"error":"Not found"},404)

    def do_DELETE(self):
        path = urllib.parse.urlparse(self.path).path
        user=self.require_user()
        if not user:return
        if path == "/api/profile/resume":
            with db() as c: c.execute("DELETE FROM resumes WHERE user_id=?", (user["id"],))
            return self.send_json({"ok":True})
        if path.startswith("/api/admin/sources/"):
            if user["role"]!="admin": return self.send_json({"error":"Administrator access required"},403)
            try: source_id=int(path.rsplit("/",1)[1])
            except Exception:return self.send_json({"error":"Invalid source id"},400)
            with db() as c:
                source=c.execute("SELECT provider,board_token FROM source_registry WHERE id=?",(source_id,)).fetchone()
                if source:
                    family={"greenhouse":"Greenhouse","lever":"Lever","ashby":"Ashby"}[source["provider"]]
                    c.execute("DELETE FROM source_registry WHERE id=?",(source_id,))
                    c.execute("UPDATE jobs SET active=0 WHERE source_key LIKE ?",(f"{family}:{source['board_token']}:%",))
            return self.send_json({"ok":True})
        if path.startswith("/api/applications/"):
            try: aid=int(path.rsplit("/",1)[1])
            except Exception:return self.send_json({"error":"Invalid id"},400)
            with db() as c:c.execute("DELETE FROM applications WHERE id=? AND user_id=?",(aid,user["id"]))
            return self.send_json({"ok":True})
        if path.startswith("/api/saved/"):
            try: jid=int(path.rsplit("/",1)[1])
            except Exception:return self.send_json({"error":"Invalid id"},400)
            with db() as c:c.execute("DELETE FROM saved_jobs WHERE job_id=? AND user_id=?",(jid,user["id"]))
            return self.send_json({"ok":True})
        return self.send_json({"error":"Not found"},404)

    def do_PATCH(self):
        path=urllib.parse.urlparse(self.path).path
        user=self.require_user()
        if not user:return
        if path.startswith("/api/applications/"):
            try:aid=int(path.rsplit("/",1)[1])
            except Exception:return self.send_json({"error":"Invalid id"},400)
            data=self.body_json()
            with db() as c:c.execute("UPDATE applications SET status=?,notes=?,applied_at=? WHERE id=? AND user_id=?",(str(data.get("status") or "Applied")[:50],str(data.get("notes") or "")[:2000],data.get("applied_at") or iso(),aid,user["id"]))
            return self.send_json({"ok":True})
        return self.send_json({"error":"Not found"},404)

    def api_get(self,path,query):
        if path == "/api/health":
            try:
                with db() as c:
                    users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
                    sessions = c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
                return self.send_json({"ok": True, "database": "connected", "users": users, "sessions": sessions})
            except Exception as exc:
                print("FidoraJobs health check failed:", type(exc).__name__, str(exc)[:500])
                return self.send_json({"ok": False, "database": "error", "type": type(exc).__name__, "detail": str(exc)[:300]}, 500)
        if path=="/api/me":
            cookie = self.headers.get("Cookie", "")
            match = re.search(r"(?:^|;\s*)fp_session=([^;]+)", cookie)
            if not match:
                return self.send_json({"user": None, "session_status": "cookie_missing"})
            try:
                user = self.current_user()
            except Exception as exc:
                print("FidoraJobs /api/me session lookup failed:", type(exc).__name__, str(exc)[:300])
                return self.send_json({"user": None, "session_status": "lookup_error", "error_type": type(exc).__name__}, 503)
            if not user:
                return self.send_json({"user": None, "session_status": "cookie_present_session_not_valid"})
            return self.send_json({"user":{"id":user["id"],"name":user["name"],"email":user["email"],"role":user["role"],"target_roles":user["target_roles"],"target_skills":user["target_skills"]}, "session_status":"valid"})
        user=self.require_user()
        if not user:return
        with db() as c:
            if path=="/api/jobs":
                where=["active=1"]; args=[]
                q=query.get("q",[""])[0].strip().lower()
                if q: where.append("(lower(title||' '||employer||' '||location||' '||description||' '||category) LIKE ?)");args.append("%"+q[:100]+"%")
                if query.get("last24",["0"])[0]=="1": where.append("COALESCE(posted_at,first_seen_at)>=?");args.append(iso(now()-timedelta(hours=24)))
                date_from=query.get("date_from",[""])[0].strip()
                date_to=query.get("date_to",[""])[0].strip()
                try: datetime.strptime(date_from,"%Y-%m-%d")
                except (TypeError,ValueError): date_from=""
                try: datetime.strptime(date_to,"%Y-%m-%d")
                except (TypeError,ValueError): date_to=""
                if date_from: where.append("substr(COALESCE(posted_at,first_seen_at),1,10)>=?");args.append(date_from)
                if date_to: where.append("substr(COALESCE(posted_at,first_seen_at),1,10)<=?");args.append(date_to)
                if query.get("location",[""])[0]:where.append("lower(location) LIKE ?");args.append("%"+query["location"][0].lower()[:100]+"%")
                if query.get("remote",[""])[0]:where.append("lower(remote_type) LIKE ?");args.append("%"+query["remote"][0].lower()[:30]+"%")
                if query.get("domain",[""])[0]:where.append("lower(category) LIKE ?");args.append("%"+query["domain"][0].lower()[:80]+"%")
                if query.get("sponsor",[""])[0]=="explicit":where.append("(lower(description) LIKE '%sponsor%' OR lower(description) LIKE '%visa support%' OR lower(description) LIKE '%work authorization%' OR sponsor_hint='provider_positive')")
                if query.get("saved",["0"])[0]=="1":where.append("id IN (SELECT job_id FROM saved_jobs WHERE user_id=?)");args.append(user["id"])
                rows=c.execute("SELECT * FROM jobs WHERE "+" AND ".join(where)+" ORDER BY COALESCE(posted_at,first_seen_at) DESC",args).fetchall()
                resume=c.execute("SELECT extracted_text FROM resumes WHERE user_id=?",(user["id"],)).fetchone()
                has_resume=bool(resume)
                resume_terms=resume_keywords(resume["extracted_text"]) if has_resume else set()
                role_terms=resume_keywords(user["target_roles"])
                skill_terms=resume_keywords(user["target_skills"])
                terms=resume_terms|role_terms|skill_terms if has_resume else set()
                out=[]
                applied={r[0] for r in c.execute("SELECT source_key FROM applications WHERE user_id=?",(user["id"],)).fetchall()}
                saved={r[0] for r in c.execute("SELECT job_id FROM saved_jobs WHERE user_id=?",(user["id"],)).fetchall()}
                for r in rows:
                    job_text=(r["title"]+" "+r["description"]+" "+r["category"]).lower()
                    job_terms=resume_keywords(job_text);hits=sorted(terms & job_terms)
                    title_terms=resume_keywords(r["title"])
                    # Give the client's chosen target role and skills more weight than incidental resume words.
                    def match_weight(term):
                        return 2.5 if term in role_terms or term in skill_terms else 2.0 if term in title_terms else 1.0
                    weight_total=sum(match_weight(term) for term in terms)
                    weight_match=sum(match_weight(term) for term in hits)
                    hits.sort(key=lambda term: (term not in title_terms, term not in role_terms, term not in skill_terms, term))
                    score=round(100*weight_match/weight_total) if weight_total and has_resume else 0
                    desc=r["description"] or ""
                    positive_match=re.search(r"(sponsor(ship)? (is )?(available|provided|offered)|visa (sponsorship|support)|will sponsor|H.?1B sponsorship)",desc,re.I)
                    negative_match=re.search(r"(unable to sponsor|will not sponsor|no sponsorship|without (the need for )?sponsorship)",desc,re.I)
                    provider_signal=(r["sponsor_hint"]=="provider_positive")
                    sponsorship="negative" if negative_match else "explicit" if positive_match else "provider_signal" if provider_signal else "not_stated"
                    evidence=negative_match.group(0) if negative_match else positive_match.group(0) if positive_match else "An external sponsorship signal was returned; verify on the employer posting." if provider_signal else "No clear sponsorship statement found in the available text."
                    out.append({"id":r["id"],"source_key":r["source_key"],"employer":r["employer"],"title":r["title"],"location":r["location"],"description":r["description"],"job_url":r["job_url"],"apply_url":r["apply_url"],"posted_at":r["posted_at"],"first_seen_at":r["first_seen_at"],"last_seen_at":r["last_seen_at"],"date_type":"employer_posted" if r["posted_at"] else "first_seen","remote_type":r["remote_type"],"category":r["category"],"salary":r["salary"],"match":score,"has_resume":has_resume,"matched_terms":hits[:12],"sponsorship":sponsorship,"sponsorship_evidence":evidence,"sponsorship_negative":bool(negative_match),"applied":r["source_key"] in applied,"saved":r["id"] in saved})
                out.sort(key=lambda x:(x["match"],x["posted_at"] or x["first_seen_at"]),reverse=True)
                return self.send_json({"jobs":out,"count":len(out),"sources":SOURCE_STATE,"last_sync":max((x.get("last_run","") for x in SOURCE_STATE.values()),default="")})
            if path=="/api/applications":
                rows=c.execute("SELECT * FROM applications WHERE user_id=? ORDER BY applied_at DESC",(user["id"],)).fetchall()
                return self.send_json({"applications":[dict(r) for r in rows]})
            if path=="/api/sources":
                if user["role"]!="admin": return self.send_json({"error":"Administrator access required"},403)
                return self.send_json({"sources":SOURCE_STATE,"configured":sum(1 for _ in all_sources()),"interval_minutes":15})
            if path=="/api/profile":
                resume=c.execute("SELECT filename,updated_at,extracted_text FROM resumes WHERE user_id=?",(user["id"],)).fetchone()
                return self.send_json({"target_roles":user["target_roles"],"target_skills":user["target_skills"],"resume":dict(resume) if resume else None})
            if path=="/api/admin/users":
                if user["role"]!="admin": return self.send_json({"error":"Administrator access required"},403)
                rows=c.execute("SELECT id,name,email,role,is_active,created_at FROM users WHERE role='client' ORDER BY created_at DESC").fetchall()
                return self.send_json({"users":[dict(r) for r in rows]})
            if path=="/api/admin/sources":
                if user["role"]!="admin": return self.send_json({"error":"Administrator access required"},403)
                rows=c.execute("SELECT id,provider,board_token,added_at FROM source_registry ORDER BY added_at DESC").fetchall()
                return self.send_json({"sources":[dict(r) for r in rows]})
            if path=="/api/summary":
                cutoff=iso(now()-timedelta(hours=24))
                n=c.execute("SELECT COUNT(*) FROM jobs WHERE active=1 AND COALESCE(posted_at,first_seen_at)>=?",(cutoff,)).fetchone()[0]
                a=c.execute("SELECT COUNT(*) FROM applications WHERE user_id=?",(user["id"],)).fetchone()[0]
                return self.send_json({"new_jobs":n,"applications":a,"last_sync":max((x.get("last_run","") for x in SOURCE_STATE.values()),default="")})
        return self.send_json({"error":"Not found"},404)


def main():
    if len(APP_SECRET) < 32 or APP_SECRET == b"local-development-secret-change-before-deploy" or APP_SECRET.decode(errors="ignore").startswith("replace-with"):
        raise SystemExit("Set a unique APP_SECRET of at least 32 characters in .env before starting the portal.")
    if not os.getenv("ADMIN_EMAIL", "").strip() or not os.getenv("ADMIN_PASSWORD", ""):
        raise SystemExit("Set ADMIN_EMAIL and ADMIN_PASSWORD in .env. Client self-registration is disabled.")
    admin_password = os.getenv("ADMIN_PASSWORD", "")
    if admin_password and (len(admin_password) < 16 or admin_password.startswith("replace-with")):
        raise SystemExit("Set ADMIN_PASSWORD to a unique password of at least 16 characters in .env.")
    init_db()
    threading.Thread(target=scheduler, daemon=True).start()
    server=ThreadingHTTPServer(("127.0.0.1",PORT),Handler)
    print(f"FidoraPath Jobs Portal running at http://127.0.0.1:{PORT}")
    server.serve_forever()


if __name__=="__main__": main()
