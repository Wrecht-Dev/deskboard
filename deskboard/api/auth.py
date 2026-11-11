"""Session handling: find, import and persist Tumblr cookies.

Tumblr's internal API authenticates with ordinary session cookies, so getting
the dashboard means supplying the cookies from a browser that is already signed
in.  Nothing is scraped, uploaded or logged: values stay in memory and in a
`0600` file under the user's own data directory.

Supported sources, in order of convenience:

* Firefox / Firefox ESR / snap / flatpak profiles (``cookies.sqlite``, plaintext)
* Chromium-family profiles (``Cookies``, v10/v11 encrypted, best effort)
* Netscape ``cookies.txt`` exports
* a pasted ``Cookie:`` header or ``a=b; c=d`` string

Cookie *values* are never written to logs or error messages.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .. import config

DOMAIN_HINT = "tumblr.com"

# Chromium on Linux derives its AES key from one of these secrets.
_CHROME_PASSWORDS = ("peanuts",)
_SALT = b"saltysalt"
_IV = b" " * 16


@dataclass
class ImportResult:
    ok: bool
    message: str
    count: int = 0
    source: str = ""

    def __bool__(self) -> bool:
        return self.ok


def _is_tumblr(host: str) -> bool:
    host = (host or "").lstrip(".").lower()
    return host == DOMAIN_HINT or host.endswith("." + DOMAIN_HINT)


def _cookie_paths() -> list[Path]:
    home = Path.home()
    patterns = [
        home / ".mozilla/firefox/*/cookies.sqlite",
        home / "snap/firefox/common/.mozilla/firefox/*/cookies.sqlite",
        home / ".var/app/org.mozilla.firefox/.mozilla/firefox/*/cookies.sqlite",
        home / ".config/chromium/*/Cookies",
        home / ".config/google-chrome/*/Cookies",
        home / ".config/google-chrome-beta/*/Cookies",
        home / ".config/BraveSoftware/Brave-Browser/*/Cookies",
        home / ".config/vivaldi/*/Cookies",
        home / ".config/microsoft-edge/*/Cookies",
    ]
    found: list[Path] = []
    for pattern in patterns:
        found.extend(sorted(home.glob(str(pattern.relative_to(home)))))
    return [p for p in found if p.exists()]


def _read_sqlite(path: Path) -> sqlite3.Connection:
    """Open a copy so a running browser's lock cannot block us."""
    handle = tempfile.NamedTemporaryFile(prefix="deskboard-ck-", suffix=".sqlite", delete=False)
    handle.close()
    shutil.copy2(path, handle.name)
    con = sqlite3.connect(handle.name)
    con.row_factory = sqlite3.Row
    return con


# --------------------------------------------------------------------------- #
# Chromium decryption (best effort)
# --------------------------------------------------------------------------- #

def _chrome_key(password: str) -> bytes:
    import hashlib

    return hashlib.pbkdf2_hmac("sha1", password.encode(), _SALT, 1, 16)


def _decrypt_chromium(blob: bytes) -> str | None:
    if not blob:
        return None
    if blob[:3] not in (b"v10", b"v11"):
        try:  # some builds store plaintext
            return blob.decode("utf-8")
        except UnicodeDecodeError:
            return None
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        return None
    payload = blob[3:]
    for password in _CHROME_PASSWORDS:
        try:
            key = _chrome_key(password)
            decryptor = Cipher(algorithms.AES(key), modes.CBC(_IV)).decryptor()
            plain = decryptor.update(payload) + decryptor.finalize()
        except Exception:  # noqa: BLE001 - wrong key or corrupt blob
            continue
        if not plain:
            continue
        pad = plain[-1]
        if 0 < pad <= 16:
            plain = plain[:-pad]
        try:
            return plain.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return None


# --------------------------------------------------------------------------- #
# Manager
# --------------------------------------------------------------------------- #

class SessionManager:
    """Owns the cookie jar and its on-disk store."""

    def __init__(self, store: Path | None = None) -> None:
        self.store = Path(store) if store else config.COOKIE_FILE
        self.cookies: dict[str, str] = {}
        self.source = ""
        self.load()

    # -- persistence ------------------------------------------------------- #
    def load(self) -> bool:
        try:
            raw = json.loads(self.store.read_text("utf-8"))
        except (OSError, ValueError):
            return False
        cookies = raw.get("cookies") if isinstance(raw, dict) else raw
        if not isinstance(cookies, dict):
            return False
        self.cookies = {str(k): str(v) for k, v in cookies.items() if v}
        self.source = str(raw.get("source", "")) if isinstance(raw, dict) else ""
        return bool(self.cookies)

    def save(self) -> None:
        self.store.parent.mkdir(parents=True, exist_ok=True)
        payload = {"source": self.source, "saved": int(time.time()), "cookies": self.cookies}
        tmp = self.store.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), "utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.store)
        try:
            os.chmod(self.store, 0o600)
        except OSError:
            pass

    def clear(self) -> None:
        self.cookies = {}
        self.source = ""
        try:
            self.store.unlink()
        except OSError:
            pass

    # -- state ------------------------------------------------------------- #
    @property
    def has_cookies(self) -> bool:
        return bool(self.cookies)

    def describe(self) -> str:
        if not self.cookies:
            return "Browsing as a guest"
        return f"{len(self.cookies)} cookies from {self.source or 'manual entry'}"

    # -- importers --------------------------------------------------------- #
    def import_firefox(self) -> ImportResult:
        best: dict[str, str] = {}
        best_path = ""
        for path in _cookie_paths():
            if "Cookies" == path.name:
                continue
            try:
                con = _read_sqlite(path)
                rows = con.execute(
                    "select name, value, host, expiry from moz_cookies "
                    "where host like ? and (expiry = 0 or expiry > ?)",
                    ("%tumblr%", int(time.time())),
                ).fetchall()
                con.close()
            except sqlite3.Error:
                continue
            found = {r["name"]: r["value"] for r in rows if r["value"] and _is_tumblr(r["host"])}
            if len(found) > len(best):
                best, best_path = found, str(path)
        if not best:
            return ImportResult(False, "No Tumblr cookies found in any Firefox profile.")
        return self._adopt(best, f"Firefox ({Path(best_path).parent.name})")

    def import_chromium(self) -> ImportResult:
        best: dict[str, str] = {}
        best_path = ""
        for path in _cookie_paths():
            if path.name != "Cookies":
                continue
            try:
                con = _read_sqlite(path)
                rows = con.execute(
                    "select host_key, name, encrypted_value, value, expires_utc from cookies"
                ).fetchall()
                con.close()
            except sqlite3.Error:
                continue
            found: dict[str, str] = {}
            for row in rows:
                host = (row["host_key"] or "").lstrip(".")
                if not _is_tumblr(host):
                    continue
                value = row["value"] or _decrypt_chromium(row["encrypted_value"] or b"")
                if value:
                    found[row["name"]] = value
            if len(found) > len(best):
                best, best_path = found, str(path)
        if not best:
            return ImportResult(
                False,
                "No readable Tumblr cookies in Chromium-family profiles "
                "(encrypted store needs the browser keyring).",
            )
        return self._adopt(best, f"Chromium ({Path(best_path).parent.parent.name})")

    def import_file(self, path: str | os.PathLike) -> ImportResult:
        try:
            text = Path(path).expanduser().read_text("utf-8", "replace")
        except OSError as exc:
            return ImportResult(False, f"Could not read {path}: {exc.strerror}")
        return self.import_text(text)

    def import_text(self, text: str) -> ImportResult:
        cookies = parse_cookie_text(text)
        if not cookies:
            return ImportResult(
                False,
                "No Tumblr cookies recognised. Paste a `Cookie:` header, "
                "a cookies.txt export, or a JSON cookie export.",
            )
        return self._adopt(cookies, "pasted cookies")

    def _adopt(self, cookies: dict[str, str], source: str) -> ImportResult:
        self.cookies = dict(cookies)
        self.source = source
        self.save()
        return ImportResult(True, f"Imported {len(cookies)} cookies from {source}.", len(cookies), source)

    def apply(self, client) -> None:
        client.set_cookies(self.cookies)


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #

def parse_cookie_text(text: str) -> dict[str, str]:
    """Parse a pasted header, a Netscape cookies.txt, or a JSON export."""
    text = (text or "").strip()
    if not text:
        return {}

    # JSON export (browser extensions)
    if text.startswith("{") or text.startswith("["):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if data is not None:
            return _cookies_from_json(data)

    cookies: dict[str, str] = {}
    lines = text.splitlines()
    # Browser cookie exports mark HttpOnly cookies with a "#HttpOnly_" prefix on
    # an otherwise ordinary Netscape line. Session cookies are HttpOnly, so
    # skipping every "#" line would drop exactly the cookie we need.
    has_httponly = any(line.startswith("#HttpOnly_") for line in lines)
    looks_netscape = has_httponly or ("\t" in text and any(line.count("\t") >= 6 for line in lines))
    if looks_netscape:
        for line in lines:
            if not line:
                continue
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            elif line.startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) >= 7:
                domain, _flag, _path, _secure, _expiry, name, value = fields[:7]
                if _is_tumblr(domain) and name:
                    cookies[name] = value
        return cookies

    # "Cookie: a=b; c=d" or "a=b; c=d"
    header = text
    for prefix in ("cookie:", "Cookie:"):
        if header.startswith(prefix):
            header = header[len(prefix):]
            break
    for part in header.replace("\n", ";").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name, value = name.strip(), value.strip()
        if name and value:
            cookies[name] = value
    return cookies


def _cookies_from_json(data) -> dict[str, str]:
    cookies: dict[str, str] = {}

    def add(name, value, domain=".tumblr.com"):
        if name and value and _is_tumblr(domain or ""):
            cookies[str(name)] = str(value)

    items: Iterable = []
    if isinstance(data, dict):
        if isinstance(data.get("cookies"), list):
            items = data["cookies"]
        elif isinstance(data.get("cookies"), dict):
            return {str(k): str(v) for k, v in data["cookies"].items() if v}
        else:
            return {}
    elif isinstance(data, list):
        items = data
    for item in items:
        if not isinstance(item, dict):
            continue
        domain = item.get("domain") or item.get("host") or item.get("host_key") or ".tumblr.com"
        add(item.get("name"), item.get("value"), domain)
    return cookies


def find_browser_candidates() -> list[str]:
    """Which import buttons should be offered."""
    candidates = []
    for path in _cookie_paths():
        if path.name == "cookies.sqlite":
            if "Firefox" not in candidates:
                candidates.append("Firefox")
        elif path.name == "Cookies":
            name = path.parent.parent.name or "Chromium"
            label = {"google-chrome": "Chrome", "chromium": "Chromium",
                     "Brave-Browser": "Brave", "vivaldi": "Vivaldi",
                     "microsoft-edge": "Edge"}.get(name, name)
            if label not in candidates:
                candidates.append(label)
    return candidates
