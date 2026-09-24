"""URL normalisation, crawl scope and URL-pattern budgets. Pure functions, no browser."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from fnmatch import fnmatchcase
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from scoutqa.config.models import ProjectConfig

_TRACKING_PARAMS = re.compile(r"^(utm_[a-z]+|gclid|fbclid|msclkid|mc_[a-z]+|_ga|_gl|ref_src)$", re.I)
_FILE_EXTENSIONS = frozenset(
    (
        ".pdf .zip .gz .tar .rar .7z .exe .msi .dmg .apk .csv .xls .xlsx .doc .docx .ppt .pptx .odt .ods "
        ".png .jpg .jpeg .gif .svg .webp .ico .bmp .tif .tiff .mp3 .mp4 .avi .mov .wav .webm .iso .bin"
    ).split()
)
# Never followed, regardless of configuration: they end the session.
_SESSION_ENDING = re.compile(r"(?i)(^|[/_\-.])(log-?out|log-?off|sign-?out|sign-?off|end-?session)([/_\-.?]|$)")

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_NUMERIC = re.compile(r"^\d+$")
_HEXISH = re.compile(r"^[0-9a-f]{8,}$", re.I)
_TOKENISH = re.compile(r"^(?=.*\d)[A-Za-z0-9_-]{16,}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def normalize_url(url: str, base: str | None = None) -> str | None:
    """Canonical form used as the identity of a URL. Returns None for non-http(s) URLs.

    - lowercase scheme/host, default ports removed, empty path -> "/"
    - fragment dropped unless it is a hash route ("#/..." or "#!/...")
    - tracking params dropped, remaining query params sorted
    """
    if base is not None:
        from urllib.parse import urljoin

        url = urljoin(base, url)
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    port = parts.port
    netloc = host if port is None or (scheme, port) in (("http", 80), ("https", 443)) else f"{host}:{port}"
    path = parts.path or "/"
    query_items = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING_PARAMS.match(k)]
    query = urlencode(sorted(query_items))
    fragment = parts.fragment if parts.fragment.startswith(("/", "!/")) else ""
    return urlunsplit((scheme, netloc, path, query, fragment))


def url_pattern(url: str) -> str:
    """Generalise volatile segments: /items/42/edit -> /items/{id}/edit, ?page=3 -> ?page={v}."""
    parts = urlsplit(url)
    segments = [_generalise(seg) for seg in parts.path.split("/")]
    path = "/".join(segments) or "/"
    keys = sorted({k for k, _ in parse_qsl(parts.query, keep_blank_values=True)})
    query = "&".join(f"{k}={{v}}" for k in keys)
    fragment = ""
    if parts.fragment:
        fragment = "#" + "/".join(_generalise(seg) for seg in parts.fragment.split("/"))
    return f"{parts.netloc}{path}{'?' + query if query else ''}{fragment}"


def _generalise(segment: str) -> str:
    if not segment:
        return segment
    if _NUMERIC.match(segment) or _UUID.match(segment) or _DATE.match(segment):
        return "{id}"
    if (_HEXISH.match(segment) and any(c.isdigit() for c in segment)) or _TOKENISH.match(segment):
        return "{id}"
    return segment


def is_session_ending(url: str) -> bool:
    parts = urlsplit(url)
    return bool(_SESSION_ENDING.search(parts.path) or _SESSION_ENDING.search(parts.fragment))


def is_file_download(url: str) -> bool:
    path = urlsplit(url).path.lower()
    dot = path.rfind(".")
    return dot != -1 and "/" not in path[dot:] and path[dot:] in _FILE_EXTENSIONS


def host_allowed(host: str, allowed: list[str]) -> bool:
    host = host.lower()
    for pattern in allowed:
        pattern = pattern.lower()
        if pattern.startswith("*."):
            if host == pattern[2:] or host.endswith(pattern[1:]):
                return True
        elif host == pattern:
            return True
    return False


class SkipReason(StrEnum):
    OUT_OF_DOMAIN = "out_of_domain"
    EXCLUDED = "excluded_by_config"
    NOT_INCLUDED = "not_in_include_list"
    SESSION_ENDING = "session_ending_link"
    FILE_DOWNLOAD = "file_download"
    UNSAFE_ACTION = "unsafe_action"
    MAX_DEPTH = "max_depth"
    PATTERN_BUDGET = "pattern_budget"
    MAX_PAGES = "max_pages"
    MAX_DURATION = "max_duration"
    REDIRECTED_OUT = "redirected_out_of_scope"


@dataclass
class Scope:
    allowed_domains: list[str]
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)

    @classmethod
    def from_config(cls, cfg: ProjectConfig) -> Scope:
        return cls(cfg.allowed_domains, list(cfg.scope.include), list(cfg.scope.exclude))

    def check(self, normalized_url: str) -> SkipReason | None:
        """None if the URL may be visited, else the reason it may not."""
        host = urlsplit(normalized_url).hostname or ""
        if not host_allowed(host, self.allowed_domains):
            return SkipReason.OUT_OF_DOMAIN
        if is_session_ending(normalized_url):
            return SkipReason.SESSION_ENDING
        if is_file_download(normalized_url):
            return SkipReason.FILE_DOWNLOAD
        if any(fnmatchcase(normalized_url, g) for g in self.exclude):
            return SkipReason.EXCLUDED
        if self.include and not any(fnmatchcase(normalized_url, g) for g in self.include):
            return SkipReason.NOT_INCLUDED
        return None


@dataclass
class PatternBudget:
    """Admit at most `limit` URLs per URL pattern — unless their pages turn out to differ.

    After visiting, the explorer reports each page's structure fingerprint via `observe`. If instances of a
    pattern have more than one structure (e.g. /items/{id} renders different page types), the pattern's
    allowance grows to `limit * widen_factor` so those variants get explored.
    """

    limit: int
    widen_factor: int = 4
    _counts: Counter[str] = field(default_factory=Counter)
    _structures: dict[str, set[str]] = field(default_factory=dict)

    def allowance(self, pattern: str) -> int:
        return self.limit * self.widen_factor if len(self._structures.get(pattern, ())) > 1 else self.limit

    def admit(self, normalized_url: str) -> bool:
        pattern = url_pattern(normalized_url)
        if self._counts[pattern] >= self.allowance(pattern):
            return False
        self._counts[pattern] += 1
        return True

    def observe(self, normalized_url: str, structure_hash: str) -> str:
        """Record the structure seen at a URL; returns the URL's pattern."""
        pattern = url_pattern(normalized_url)
        self._structures.setdefault(pattern, set()).add(structure_hash)
        return pattern
