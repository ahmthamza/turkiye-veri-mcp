"""Client for the TUIK data portal catalog (veriportali.tuik.gov.tr).

The portal exposes a JSON theme tree at /api/{lang}/data/statistical-themes.
Access requires a cookie session established by first visiting the landing
page, plus browser-like headers. Endpoint behaviour discovered via the
tuikr R package (github.com/emraher/tuikr, MIT).

Tree shape (checked 2026-11): 19 themes; nodes carry id/name/icon/url and
optionally metadataUrl/downloadable. Icons seen: folder, dataflow, istab,
database, press, report -- nothing else. 'database' links point to the
MEDAS/Biruni ZK web app (biruni.tuik.gov.tr/medas/?kn=N), plus two
bi.tuik.gov.tr and one evds3.tcmb.gov.tr link.
"""

from __future__ import annotations

import threading
import time
import unicodedata
from typing import Any

import httpx

PORTAL_BASE = "https://veriportali.tuik.gov.tr"

RESOURCE_TYPES = ("dataflow", "istab", "database", "press", "report")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

_ACCEPT_LANGUAGE = {
    "tr": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
    "en": "en-US,en;q=0.9,tr-TR;q=0.8,tr;q=0.7",
}


#: Pauses before the 2nd and 3rd catalog attempts (transient errors only).
_RETRY_PAUSES = (1.0, 2.0)
_RETRY_STATUSES = (502, 503, 504)


class PortalError(RuntimeError):
    """Raised when the TUIK portal returns an error or unexpected payload."""


def _validate_lang(lang: str) -> str:
    if lang not in ("tr", "en"):
        raise ValueError("lang must be 'tr' or 'en'")
    return lang


class PortalClient:
    """Fetches and caches the TUIK portal theme tree."""

    #: Catalog cache lifetime. Keeps a long-running (hosted) server from
    #: serving a stale theme tree after TUIK publishes new tables.
    CACHE_TTL_SECONDS = 3600.0

    def __init__(self, timeout: float = 60.0) -> None:
        self._timeout = timeout
        self._tree_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}

    def theme_tree(self, lang: str = "tr", refresh: bool = False) -> list[dict[str, Any]]:
        lang = _validate_lang(lang)
        cached = self._tree_cache.get(lang)
        if cached and not refresh and time.monotonic() - cached[0] < self.CACHE_TTL_SECONDS:
            return cached[1]

        page_url = f"{PORTAL_BASE}/{lang}/statistical-themes"
        api_url = f"{PORTAL_BASE}/api/{lang}/data/statistical-themes"
        base_headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": _ACCEPT_LANGUAGE[lang],
            "User-Agent": _UA,
        }

        try:
            response = self._fetch_with_retry(page_url, api_url, base_headers)
        except PortalError:
            # A stale tree beats a failed call: the catalog changes rarely.
            if cached:
                return cached[1]
            raise

        try:
            payload = response.json()
        except ValueError as exc:
            raise PortalError(
                "TUIK portal did not return JSON; the endpoint may have changed."
            ) from exc

        if payload.get("isError"):
            raise PortalError(f"TUIK API error: {payload.get('message')}")

        tree = payload.get("data")
        if not isinstance(tree, list):
            raise PortalError("Unexpected TUIK portal payload shape.")

        self._tree_cache[lang] = (time.monotonic(), tree)
        return tree

    def _fetch_with_retry(
        self, page_url: str, api_url: str, base_headers: dict[str, str]
    ) -> httpx.Response:
        """Landing page (cookie session) + JSON API GET, retrying transient errors.

        TUIK's front end occasionally resets fresh connections; each attempt
        opens a new client and re-establishes the cookie session. Only
        transport errors and HTTP 502/503/504 are retried, within a total
        budget of `self._timeout` seconds; other HTTP errors fail at once.
        """
        timeout = httpx.Timeout(self._timeout, connect=min(15.0, self._timeout))
        deadline = time.monotonic() + self._timeout
        last_error = ""
        for pause in (0.0, *_RETRY_PAUSES):
            if pause:
                if time.monotonic() + pause >= deadline:
                    break
                time.sleep(pause)
            try:
                with httpx.Client(
                    timeout=timeout, follow_redirects=True, headers=base_headers
                ) as client:
                    # Step 1: landing page sets session cookies.
                    client.get(page_url).raise_for_status()
                    # Step 2: JSON API call with the session cookies.
                    response = client.get(
                        api_url,
                        headers={
                            "Referer": page_url,
                            "Origin": PORTAL_BASE,
                            "X-Requested-With": "XMLHttpRequest",
                        },
                    )
                    response.raise_for_status()
                    return response
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status in _RETRY_STATUSES:
                    last_error = f"HTTP {status}"
                    continue
                raise PortalError(
                    f"TÜİK portalı HTTP {status} döndürdü ({exc.request.url}); "
                    "katalog uç noktası değişmiş olabilir."
                ) from exc
            except httpx.TransportError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                continue
        raise PortalError(
            "TÜİK portalına (veriportali.tuik.gov.tr) bağlanılamadı; tekrar denemeler "
            f"de başarısız oldu ({last_error}). Portal geçici olarak erişilemez "
            "olabilir; birkaç dakika sonra tekrar deneyin."
        )

    def list_themes(self, lang: str = "tr") -> list[dict[str, str]]:
        return [
            {"theme_id": str(node.get("id")), "theme_name": str(node.get("name"))}
            for node in self.theme_tree(lang)
        ]

    def list_resources(
        self,
        theme_id: str,
        lang: str = "tr",
        types: tuple[str, ...] = ("dataflow", "istab"),
    ) -> list[dict[str, Any]]:
        bad = [t for t in types if t not in RESOURCE_TYPES]
        if bad:
            raise ValueError(f"Unknown resource type(s): {bad}. Valid: {RESOURCE_TYPES}")

        tree = self.theme_tree(lang)
        theme_node = next(
            (n for n in tree if str(n.get("id")) == str(theme_id)), None
        )
        if theme_node is None:
            valid = ", ".join(f"{t['theme_id']}={t['theme_name']}" for t in self.list_themes(lang))
            raise ValueError(f"Unknown theme_id '{theme_id}'. Valid themes: {valid}")

        rows: list[dict[str, Any]] = []
        for node, path in _collect_by_icon(theme_node.get("children") or [], set(types)):
            rows.append(_resource_row(theme_node, node, path))
        return rows

    def search(
        self,
        query: str,
        lang: str = "tr",
        types: tuple[str, ...] = ("dataflow", "istab", "database"),
    ) -> list[dict[str, Any]]:
        """Turkish-aware search over table names (and their folder path).

        Every word of `query` must occur (AND), compared after folding case
        with Turkish rules and removing diacritics, so 'isgucu', 'İŞGÜCÜ'
        and 'işgücü' are equivalent. Rows whose *name* contains all words
        rank before rows that match only through their folder path.
        """
        terms = fold_tr(query).split()
        if not terms:
            raise ValueError("query must contain at least one word")
        in_name: list[dict[str, Any]] = []
        in_path: list[dict[str, Any]] = []
        for theme in self.list_themes(lang):
            for row in self.list_resources(theme["theme_id"], lang=lang, types=types):
                name = fold_tr(row["name"])
                if all(term in name for term in terms):
                    in_name.append(row)
                    continue
                haystack = name + " " + fold_tr(row["path"]) + " " + fold_tr(row["theme_name"])
                if all(term in haystack for term in terms):
                    in_path.append(row)
        return in_name + in_path


_TR_FOLD = str.maketrans({
    "ı": "i", "ş": "s", "ğ": "g", "ü": "u", "ö": "o", "ç": "c",
    "â": "a", "î": "i", "û": "u",
})


def fold_tr(text: str) -> str:
    """Case- and diacritic-insensitive key with Turkish casing rules.

    'İ' -> 'i' and 'I' -> 'ı' (Turkish lowercase), then dotless/dotted i,
    ş, ğ, ü, ö, ç and circumflex vowels are folded to ASCII, and any other
    combining marks are removed. Punctuation becomes whitespace.
    """
    value = str(text or "").replace("İ", "i").replace("I", "ı").lower()
    value = value.translate(_TR_FOLD)
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = "".join(ch if ch.isalnum() else " " for ch in value)
    return " ".join(value.split())


def _resource_row(theme_node: dict[str, Any], node: dict[str, Any], path: list[str]) -> dict[str, Any]:
    raw_url = str(node.get("url") or "")
    resource_type = str(node.get("icon"))
    row: dict[str, Any] = {
        "theme_id": str(theme_node.get("id")),
        "theme_name": str(theme_node.get("name")),
        "path": " > ".join(path),
        "name": str(node.get("name")),
        "type": resource_type,
        "dataflow_id": _extract_dataflow_id(raw_url) if resource_type == "dataflow" else None,
        "url": _absolute_url(raw_url),
    }
    if node.get("metadataUrl"):
        row["metadata_url"] = _absolute_url(str(node["metadataUrl"]))
    if resource_type == "database":
        row["access"] = "link-only (MEDAS/Biruni etkileşimli arayüz; programatik erişim yok)"
    return row


def _collect_by_icon(
    nodes: list[dict[str, Any]], target_icons: set[str], path: list[str] | None = None
) -> list[tuple[dict[str, Any], list[str]]]:
    path = path or []
    found: list[tuple[dict[str, Any], list[str]]] = []
    for node in nodes:
        if node.get("icon") in target_icons:
            found.append((node, path))
        children = node.get("children")
        if children:
            found.extend(_collect_by_icon(children, target_icons, path + [str(node.get("name"))]))
    return found


def _absolute_url(raw_url: str) -> str:
    if raw_url.startswith(("http://", "https://")):
        return raw_url
    return f"{PORTAL_BASE}{raw_url}"


def _extract_dataflow_id(raw_url: str) -> str:
    return raw_url.rstrip("/").rsplit("/", 1)[-1]


# ---------------------------------------------------------------------------
# File downloads (istab / report / metadata links)
# ---------------------------------------------------------------------------

#: The portal's download endpoint allows one file per ~5 s per client. A
#: faster request gets HTTP 200 with a small HTML "N saniye sonra tekrar
#: dosya indirebilirsiniz" page instead of the file -- which must never be
#: saved or parsed as the table. Measured 2026-11: 4 s gap -> throttled,
#: 5 s gap -> served.
DOWNLOAD_MIN_INTERVAL = 5.5
_THROTTLE_MARKER = "tekrar dosya indirebilirsiniz"
_ALLOWED_DOWNLOAD_HOSTS = ("veriportali.tuik.gov.tr", "data.tuik.gov.tr", "www.tuik.gov.tr", "biruni.tuik.gov.tr")

_download_lock = threading.Lock()
_last_download = 0.0


class DownloadedFile:
    def __init__(self, content: bytes, content_type: str, filename: str | None, url: str) -> None:
        self.content = content
        self.content_type = content_type
        self.filename = filename
        self.url = url

    @property
    def kind(self) -> str:
        head = self.content[:8]
        if head.startswith(b"\xd0\xcf\x11\xe0"):
            return "xls"
        if head.startswith(b"PK"):
            return "zip/xlsx"
        if head.startswith(b"%PDF"):
            return "pdf"
        if head.lstrip()[:1] == b"<":
            return "html"
        return "unknown"


def _filename_from_disposition(value: str | None) -> str | None:
    if not value:
        return None
    from urllib.parse import unquote

    for part in value.split(";"):
        part = part.strip()
        if part.lower().startswith("filename*="):
            raw = part.split("=", 1)[1]
            if "''" in raw:
                raw = raw.split("''", 1)[1]
            return unquote(raw.strip('"'))
    for part in value.split(";"):
        part = part.strip()
        if part.lower().startswith("filename="):
            return part.split("=", 1)[1].strip('"')
    return None


def download_file(url: str, timeout: float = 180.0, max_wait: float = 30.0) -> DownloadedFile:
    """Download a portal file link, honouring the portal's 5 s throttle.

    Raises PortalError (never returns the throttle/HTML page as a file).
    """
    global _last_download
    from urllib.parse import urlparse

    url = (url or "").strip()
    if url.startswith("/"):
        url = f"{PORTAL_BASE}{url}"
    host = urlparse(url).hostname or ""
    if urlparse(url).scheme not in ("http", "https") or not any(
        host == h or host.endswith("." + h) for h in _ALLOWED_DOWNLOAD_HOSTS
    ):
        raise PortalError(
            f"Yalnızca TÜİK portal bağlantıları indirilebilir (verilen host: '{host}'). "
            "url alanını tuik_list_tables/tuik_search_tables çıktısından alın."
        )
    headers = {
        "User-Agent": _UA,
        "Accept": "*/*",
        "Referer": f"{PORTAL_BASE}/tr/statistical-themes",
    }
    started = time.monotonic()
    with _download_lock:
        with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
            while True:
                gap = time.monotonic() - _last_download
                if gap < DOWNLOAD_MIN_INTERVAL:
                    time.sleep(DOWNLOAD_MIN_INTERVAL - gap)
                try:
                    response = client.get(url)
                except httpx.TimeoutException as exc:
                    raise PortalError(f"TÜİK portalı {timeout:.0f} sn içinde dosyayı göndermedi.") from exc
                except httpx.HTTPError as exc:
                    _last_download = time.monotonic()
                    if time.monotonic() - started + DOWNLOAD_MIN_INTERVAL < max_wait:
                        continue
                    raise PortalError(f"TÜİK portalına bağlanılamadı: {type(exc).__name__}: {exc}") from exc
                finally:
                    _last_download = time.monotonic()
                content_type = response.headers.get("content-type", "")
                if response.status_code == 200 and "html" in content_type.lower() and _THROTTLE_MARKER in response.text:
                    if time.monotonic() - started + DOWNLOAD_MIN_INTERVAL < max_wait:
                        continue
                    raise PortalError(
                        "TÜİK portalı indirme hız sınırına takıldı (5 sn'de bir dosya); "
                        "birkaç saniye sonra tekrar deneyin."
                    )
                if response.status_code in _RETRY_STATUSES and (
                    time.monotonic() - started + DOWNLOAD_MIN_INTERVAL < max_wait
                ):
                    continue
                break
    if response.status_code != 200:
        raise PortalError(f"TÜİK portalı HTTP {response.status_code} döndürdü: {url[:120]}")
    result = DownloadedFile(
        response.content,
        content_type,
        _filename_from_disposition(response.headers.get("content-disposition")),
        url,
    )
    if result.kind == "html":
        raise PortalError(
            "TÜİK portalı dosya yerine bir HTML sayfası döndürdü (bağlantı geçersiz "
            "veya süresi dolmuş olabilir; katalogdan güncel url alın)."
        )
    return result
