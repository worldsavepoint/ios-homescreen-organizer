"""Authenticate immutable release inputs and inspect upstream source archives."""
from datetime import timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import errno
import os
from pathlib import Path
import re
import stat
import tarfile
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import urlopen
import zipfile

LOCKS = Path(__file__).with_name("sources")
BLOCK = 1024 * 1024
INVENTORY_LIMIT = 16 * 1024**2
HEX = re.compile(r"[0-9a-f]{64}")
_WEEKDAY = r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)"
_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
_CLOCK = r"[0-9]{2}:[0-9]{2}:[0-9]{2}"
_HTTP_DATE = re.compile(
    rf"{_WEEKDAY}, [0-9]{{2}} {_MONTH} [0-9]{{4}} {_CLOCK} GMT"
    rf"|(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), [0-9]{{2}}-{_MONTH}-[0-9]{{2}} {_CLOCK} GMT"
    rf"|{_WEEKDAY} {_MONTH} (?: [0-9]|[0-9]{{2}}) {_CLOCK} [0-9]{{4}}"
)


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def canonical(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def relative_name(name):
    if (not isinstance(name, str) or not name or "\\" in name or ":" in name
            or any(ord(c) < 32 or ord(c) == 127 for c in name)
            or any(p in ("", ".", "..", ".git") for p in name.split("/"))):
        raise ValueError("Expected a public relative source path")
    return name


def pin_name(pin):
    parsed = urlsplit(pin["url"])
    name = pin.get("name", unquote(parsed.path.rsplit("/", 1)[-1]))
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or "/" in name or not HEX.fullmatch(pin["sha256"])
            or type(pin["size"]) is not int or not 0 < pin["size"] <= 2 * 1024**3):
        raise ValueError("Invalid immutable source input")
    return relative_name(name)


class _SourceMismatch(ValueError):
    pass


def _copy_pinned(stream, pin, outputs):
    remaining = pin["size"]
    digest = hashlib.sha256()
    while remaining:
        chunk = stream.read(min(BLOCK, remaining))
        if not chunk:
            break
        if len(chunk) > remaining:
            raise _SourceMismatch(f"Incorrect source size: {pin_name(pin)}")
        digest.update(chunk)
        for output in outputs:
            output.write(chunk)
        remaining -= len(chunk)
    if stream.read(1):
        raise _SourceMismatch(f"Incorrect source size: {pin_name(pin)}")
    if remaining or digest.hexdigest() != pin["sha256"]:
        raise _SourceMismatch(f"Incorrect source digest: {pin_name(pin)}")


def _cached_source(path, pin, outputs):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as error:
        if error.errno in (errno.ENOENT, errno.ELOOP):
            return False
        if error.errno in (errno.EACCES, errno.EPERM):
            try:
                info = path.lstat()
            except FileNotFoundError:
                return False
            if stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and info.st_size != pin["size"]:
                return False
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size != pin["size"]:
            return False
        try:
            with os.fdopen(fd, "rb", buffering=0, closefd=False) as stream:
                _copy_pinned(stream, pin, outputs)
        except _SourceMismatch:
            return False
    finally:
        os.close(fd)
    return True


def _transient_source_error(error):
    if isinstance(error, HTTPError):
        return error.code in (408, 429, 500, 502, 503, 504)
    reason = error.reason if isinstance(error, URLError) else error
    return isinstance(reason, (TimeoutError, ConnectionResetError, ConnectionAbortedError))


def _retry_delay(error, backoff):
    if not isinstance(error, HTTPError) or not error.headers:
        return backoff
    value = error.headers.get("Retry-After", "").strip()
    if re.fullmatch(r"[0-9]+", value):
        return max(backoff, float(value))
    if not _HTTP_DATE.fullmatch(value):
        return backoff
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return max(backoff, date.timestamp() - time.time())
    except (TypeError, ValueError, OverflowError, OSError):
        return backoff


def fetch(pin, cache, offline=False, *, staged=None, retry_transient=False):
    name = pin_name(pin)
    cache.mkdir(parents=True, exist_ok=True)
    out = cache / (pin["sha256"] + "-" + name)
    outputs = (staged,) if staged is not None else ()
    if _cached_source(out, pin, outputs):
        return out
    if offline:
        raise ValueError(f"Missing verified source input: {name}")
    attempts = 4 if retry_transient else 1
    wait_budget = 45
    for attempt in range(attempts):
        if staged is not None:
            staged.seek(0)
            staged.truncate()
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=cache, delete=False) as output:
                temporary = Path(output.name)
                with urlopen(pin["url"], timeout=45) as response:
                    if urlsplit(response.geturl()).scheme != "https":
                        raise ValueError("Expected HTTPS for release sources")
                    _copy_pinned(response, pin, (output, *outputs))
            os.replace(temporary, out)
            return out
        except OSError as error:
            if attempt + 1 == attempts or not _transient_source_error(error):
                raise
            delay = _retry_delay(error, 2 ** attempt)
            if delay > wait_budget:
                raise
            if isinstance(error, HTTPError):
                error.close()
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        time.sleep(delay)
        wait_budget -= delay


def read_locks():
    return (json.loads((LOCKS / "python.json").read_bytes()),
            json.loads((LOCKS / "native.json").read_bytes()))


def cache_directory():
    return Path(os.environ.get("HOMESCREEN_SOURCE_CACHE", Path.home() / ".cache/screen-organizer/sources"))


def prepare_intel_sources(directory, openssl_version):
    """Stage the authenticated native archives with their upstream filenames."""
    python, native = read_locks()
    openssl = next(asset for asset in native["assets"] if asset["id"] == "openssl/" + openssl_version)
    crypto = python["cryptography"]["source"]
    requirement = f"cryptography @ {crypto['url']} --hash=sha256:{crypto['sha256']}"
    lock = LOCKS.parents[1] / "requirements-cryptography-x86_64.lock"
    if [line for line in lock.read_text().splitlines() if line and not line.startswith("#")] != [requirement]:
        raise ValueError("Intel cryptography source differs from the build lock")
    cache = cache_directory()
    for pin in (openssl, crypto):
        target = directory / pin_name(pin)
        with tempfile.NamedTemporaryFile(dir=directory) as staged:
            fetch(pin, cache, staged=staged, retry_transient=True)
            staged.flush()
            os.link(staged.name, target)
    local_sdist = (directory / pin_name(crypto)).resolve().as_uri()
    (directory / "cryptography.lock").write_text(f"cryptography @ {local_sdist} --hash=sha256:{crypto['sha256']}\n")


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def source_members(path, members):
    """Read selected regular notices once, checking their optional pinned digests."""
    for member in members:
        relative_name(member)
    found = {}
    if not members:
        return found

    def record(name, size, regular, stream):
        if name in found or not regular or not 0 <= size <= 32 * BLOCK:
            raise ValueError("Unexpected source notice size, type or duplicate")
        with stream() as source:
            data = source.read(32 * BLOCK + 1)
        if len(data) != size or (members[name] is not None and hashlib.sha256(data).hexdigest() != members[name]):
            raise ValueError("Different embedded source license")
        found[name] = data

    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.filename in members:
                    regular = not info.is_dir() and stat.S_IFMT(info.external_attr >> 16) in (0, stat.S_IFREG)
                    record(info.filename, info.file_size, regular, lambda: archive.open(info))
    else:
        with tarfile.open(path, "r|*") as archive:
            for info in archive:
                if info.name in members:
                    record(info.name, info.size, info.isfile(), lambda: archive.extractfile(info))
    if found.keys() != members.keys():
        raise ValueError("Missing embedded source license")
    return found


def source_member(path, member):
    """Read a specific regular member verbatim, without extracting a tree."""
    return source_members(path, {member: None})[member]


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Prepare authenticated source archives for an Intel build.")
    parser.add_argument("directory", type=Path)
    parser.add_argument("openssl_version")
    args = parser.parse_args()
    prepare_intel_sources(args.directory, args.openssl_version)
