"""Client for TCMB EVDS v3 (evds3.tcmb.gov.tr).

API behaviour this module relies on (each point checked against the live API):

- Base URL https://evds3.tcmb.gov.tr/igmevdsms-dis/ ; the API key goes in the
  ``key`` HTTP header; query parameters are appended to the path *without* a
  '?' separator; TCMB's TLS endpoint needs legacy renegotiation.
- Endpoints: ``categories/``, ``datagroups/`` (mode=0 all, mode=1 one
  datagroup by code, mode=2 by *leaf* category id), ``serieList/`` (code = a
  datagroup code -> its series, or a series code -> that one series), and the
  data endpoint (``series=A-B-...`` or ``datagroup=<code>``; formulas and
  aggregationTypes are rejected with ``datagroup=``).
- A data response holds at most 1000 rows and, when more match, EVDS silently
  returns the *latest* 1000 (``totalCount`` is capped too). This module pages
  backwards until a page has fewer than 1000 rows, so nothing is truncated.
- Aggregated periods (monthly, weekly...) are always computed over the whole
  period regardless of the request window, and formulas (yoy etc.) use data
  before the window, so stitched pages equal one unchunked request.
- Unknown codes inside a multi-series request are dropped silently (no
  column); a column of a known series with no data in range is all null.
  This module detects missing columns and names the unknown codes.
- Period labels in 'Tarih': dd-mm-yyyy (daily, workday, weekly, semimonthly),
  'yyyy-m' (monthly), 'yyyy-Qn', 'yyyy-Sn', int yyyy (annual); weekly rows
  carry an extra 'YEARWEEK' and may end after endDate (the week containing
  it). Requests mixing frequencies come back at the lowest one.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import tempfile
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import httpx
import pandas as pd

EVDS_BASE = "https://evds3.tcmb.gov.tr/igmevdsms-dis/"

#: EVDS returns at most this many rows per data request (the latest ones).
MAX_ROWS_PER_RESPONSE = 1000

FREQUENCIES = {
    "gunluk": 1, "daily": 1,
    "isgunu": 2, "workday": 2,
    "haftalik": 3, "weekly": 3,
    "ayda2": 4, "semimonthly": 4,
    "aylik": 5, "monthly": 5,
    "ceyreklik": 6, "quarterly": 6,
    "altiaylik": 7, "semiannual": 7,
    "yillik": 8, "annual": 8, "yearly": 8,
}

FORMULAS = {
    "duzey": 0, "level": 0,
    "yuzde_degisim": 1, "pct_change": 1,
    "fark": 2, "diff": 2,
    "yillik_yuzde": 3, "yoy_pct": 3,
    "yillik_fark": 4, "yoy_diff": 4,
    "yilsonu_yuzde": 5, "ytd_pct": 5,
    "yilsonu_fark": 6, "ytd_diff": 6,
    "hareketli_ortalama": 7, "moving_avg": 7,
    "hareketli_toplam": 8, "moving_sum": 8,
}

AGGREGATIONS = ("avg", "min", "max", "first", "last", "sum")

_NULL_TOKENS = {"", "ND", "nd", "null", "NULL", "None", "-"}
_RETRY_STATUS = {429, 500, 502, 503, 504}


class EvdsError(RuntimeError):
    """Raised for EVDS API errors, including missing API key."""


def _legacy_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    ctx.options |= 0x4  # OP_LEGACY_SERVER_CONNECT: TCMB requires this
    return ctx


def _normalize_date(value: str) -> str:
    """Accept dd-mm-yyyy (EVDS native) or ISO yyyy-mm-dd; return dd-mm-yyyy."""
    value = str(value).strip()
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).strftime("%d-%m-%Y")
        except ValueError:
            continue
    raise ValueError(f"Unrecognized date '{value}'. Use dd-mm-yyyy or yyyy-mm-dd.")


def _today() -> str:
    return date.today().strftime("%d-%m-%Y")


def _norm_lang(lang: str) -> str:
    return "ENG" if str(lang).strip().upper() in ("ENG", "EN", "ENGLISH") else "TR"


def _resolve(mapping: dict[str, int], value: str | int, what: str) -> str:
    if value in ("", None):
        return ""
    if isinstance(value, int) or (isinstance(value, str) and value.strip().isdigit()):
        code = int(value)
        if code not in set(mapping.values()):
            raise ValueError(
                f"Unknown {what} code {code}. Valid codes: "
                f"{min(mapping.values())}-{max(mapping.values())}."
            )
        return str(code)
    key = _fold(str(value)).strip().replace(" ", "_")
    if key in mapping:
        return str(mapping[key])
    raise ValueError(
        f"Unknown {what} '{value}'. Use a numeric code or one of: "
        + ", ".join(sorted(set(mapping)))
    )


def _split_items(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    return [part.strip() for part in str(value).split(",")]


def _per_series_param(value: Any, n_series: int, mapping: dict[str, int] | None, what: str) -> str:
    """EVDS wants dash-joined per-series values; a scalar applies to all.

    A list (or a comma-separated string) gives one value per series."""
    if value in ("", None) or value == []:
        return ""
    items = _split_items(value)
    if len(items) == 1:
        items = items * n_series
    if len(items) != n_series:
        raise ValueError(
            f"{what}: got {len(items)} values for {n_series} series; give one "
            "value for all series or exactly one per series (comma-separated)."
        )
    if mapping is not None:
        resolved = [_resolve(mapping, item, what) or "0" for item in items]
        return "-".join(resolved)
    out = []
    for item in items:
        item = str(item).strip().lower()
        if item not in AGGREGATIONS:
            raise ValueError(f"Unknown {what} '{item}'. Valid: {', '.join(AGGREGATIONS)}")
        out.append(item)
    return "-".join(out)


def column_name(code: str, formula: str | int = "") -> str:
    """The JSON column EVDS uses for a series (dots -> underscores, '-N' for
    a non-level formula)."""
    base = code.replace(".", "_")
    return f"{base}-{formula}" if str(formula) not in ("", "0") else base


def period_start(label: Any) -> date | None:
    """Start date of an EVDS 'Tarih' label (weekly/daily: the date itself)."""
    if label is None:
        return None
    if isinstance(label, (int, float)) and not isinstance(label, bool):
        return date(int(label), 1, 1)
    text = str(label).strip()
    match = re.fullmatch(r"(\d{1,2})-(\d{1,2})-(\d{4})", text)
    if match:
        return date(int(match[3]), int(match[2]), int(match[1]))
    match = re.fullmatch(r"(\d{4})-(\d{1,2})", text)
    if match:
        return date(int(match[1]), int(match[2]), 1)
    match = re.fullmatch(r"(\d{4})-Q([1-4])", text)
    if match:
        return date(int(match[1]), 3 * int(match[2]) - 2, 1)
    match = re.fullmatch(r"(\d{4})-S([12])", text)
    if match:
        return date(int(match[1]), 1 if match[2] == "1" else 7, 1)
    if re.fullmatch(r"\d{4}", text):
        return date(int(text), 1, 1)
    return None


def _unixtime(item: dict[str, Any]) -> int | None:
    raw = item.get("UNIXTIME")
    if isinstance(raw, dict):
        raw = raw.get("$numberLong")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _to_number(value: Any) -> tuple[float | None, bool]:
    """(number, was_unparseable). None/'ND'/'' are plain nulls."""
    if value is None:
        return None, False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value), False
    text = str(value).strip()
    if text in _NULL_TOKENS:
        return None, False
    try:
        return float(text), False
    except ValueError:
        return None, True


def _fold(text: Any) -> str:
    """Accent- and case-insensitive Turkish fold: 'İşsizlik', 'ISSIZLIK' and
    'issizlik' all become 'issizlik'. Whitespace runs collapse to one space."""
    text = str(text or "")
    text = text.replace("İ", "i").replace("I", "i").replace("ı", "i")
    text = text.casefold().replace("i̇", "i")
    table = str.maketrans("çğöşüâîû", "cgosuaiu")
    return " ".join(text.translate(table).split())


class EvdsClient:
    #: Datagroup/category list cache lifetime (seconds) and series-index age
    #: limit (days). Both keep a long-running server from hiding series that
    #: TCMB published after startup.
    CACHE_TTL_SECONDS = 3600.0
    INDEX_MAX_AGE_DAYS = 7.0
    #: Pause between consecutive requests of one paged/bulk operation.
    REQUEST_DELAY = 0.3

    def __init__(self, api_key: str | None = None, timeout: float = 120.0) -> None:
        self._api_key = api_key or os.environ.get("EVDS_API_KEY", "")
        self._timeout = timeout
        self._cache: dict[str, tuple[float, Any]] = {}
        self._http: httpx.Client | None = None
        self._http_lock = threading.Lock()

    # -- transport ----------------------------------------------------------

    def _require_key(self) -> str:
        if not self._api_key:
            raise EvdsError(
                "EVDS API key missing. Get a free key at https://evds3.tcmb.gov.tr "
                "(BENIM SAYFAM -> Kayit -> Profilim -> API Key) and set it as "
                "the EVDS_API_KEY environment variable (local/stdio), or send it "
                "in the 'X-Evds-Api-Key' HTTP header when using the hosted server."
            )
        return self._api_key

    def _client(self) -> httpx.Client:
        with self._http_lock:
            if self._http is None:
                self._http = httpx.Client(
                    timeout=self._timeout,
                    follow_redirects=True,
                    verify=_legacy_ssl_context(),
                )
            return self._http

    def _request(self, path: str, params: dict[str, Any]) -> Any:
        """GET an EVDS endpoint and return parsed JSON, with clear errors."""
        key = self._require_key()
        # EVDS appends params without '?': .../categories/type=json
        param_text = "&".join(f"{k}={v}" for k, v in params.items() if v != "" or k == "code")
        url = f"{EVDS_BASE}{path}{param_text}"
        response: httpx.Response | None = None
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                response = self._client().get(url, headers={"key": key})
            except httpx.HTTPError as exc:  # network / timeout
                last_exc = exc
                response = None
            if response is not None and response.status_code not in _RETRY_STATUS:
                break
            if attempt < 2:
                time.sleep(2.0 * (attempt + 1))
        if response is None:
            raise EvdsError(
                f"Could not reach EVDS ({type(last_exc).__name__}: {last_exc}). "
                "evds3.tcmb.gov.tr may be down or slow; try again later."
            )
        return _parse_response(response)

    # -- catalogue ----------------------------------------------------------

    def _cached(self, key: str, loader: Callable[[], Any]) -> Any:
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < self.CACHE_TTL_SECONDS:
            return hit[1]
        value = loader()
        self._cache[key] = (time.monotonic(), value)
        return value

    def _raw_categories(self) -> list[dict[str, Any]]:
        return self._cached("categories", lambda: self._request("categories/", {"type": "json"}) or [])

    def _raw_datagroups(self) -> list[dict[str, Any]]:
        return self._cached(
            "datagroups",
            lambda: self._request("datagroups/", {"mode": 0, "code": "", "type": "json"}) or [],
        )

    def categories(self, lang: str = "TR") -> list[dict[str, Any]]:
        """All categories (3-level tree: parent_id -1 marks a top category)."""
        title_field = "TOPIC_TITLE_ENG" if _norm_lang(lang) == "ENG" else "TOPIC_TITLE_TR"
        return [
            {
                "category_id": _as_int(r.get("CATEGORY_ID")),
                "title": str(r.get(title_field) or "").strip(),
                "level": r.get("SEVIYE"),
                "parent_id": _as_int(r.get("UST_CATEGORY_ID")),
            }
            for r in self._raw_categories()
        ]

    def _category_subtree(self, category_id: int) -> set[int]:
        children: dict[int, list[int]] = {}
        known: set[int] = set()
        for row in self._raw_categories():
            cid, parent = _as_int(row.get("CATEGORY_ID")), _as_int(row.get("UST_CATEGORY_ID"))
            known.add(cid)
            children.setdefault(parent, []).append(cid)
        if category_id not in known:
            raise EvdsError(
                f"Unknown EVDS category id {category_id}. List ids with evds_categories."
            )
        out, stack = set(), [category_id]
        while stack:
            current = stack.pop()
            if current in out:
                continue
            out.add(current)
            stack.extend(children.get(current, []))
        return out

    def datagroups(
        self, category_id: int | str | None = None, lang: str = "TR"
    ) -> list[dict[str, Any]]:
        """Datagroups, optionally limited to a category *and its subcategories*
        (EVDS itself only attaches datagroups to leaf categories)."""
        rows = self._raw_datagroups()
        if category_id not in (None, "", "ALL"):
            try:
                wanted = int(float(str(category_id).strip()))
            except ValueError as exc:
                raise ValueError(f"category_id must be numeric, got '{category_id}'") from exc
            subtree = self._category_subtree(wanted)
            rows = [r for r in rows if _as_int(r.get("CATEGORY_ID")) in subtree]
        return [_datagroup_row(r, lang) for r in rows]

    def datagroup_info(self, datagroup_code: str, lang: str = "TR") -> dict[str, Any]:
        """Full metadata of one datagroup (unit, source, notes, links...)."""
        for row in self._raw_datagroups():
            if row.get("DATAGROUP_CODE") == datagroup_code:
                return _datagroup_row(row, lang, full=True)
        rows = self._request("datagroups/", {"mode": 1, "code": datagroup_code, "type": "json"})
        rows = [r for r in (rows or []) if r]
        if not rows:
            raise EvdsError(
                f"Unknown EVDS datagroup '{datagroup_code}'. Find codes with "
                "evds_datagroups (query=...)."
            )
        return _datagroup_row(rows[0], lang, full=True)

    def search_datagroups(self, query: str, lang: str = "TR") -> list[dict[str, Any]]:
        terms = _fold(query).split()
        out = []
        for raw in self._raw_datagroups():
            hay = _fold(
                f"{raw.get('DATAGROUP_NAME')} {raw.get('DATAGROUP_NAME_ENG')} {raw.get('DATAGROUP_CODE')}"
            )
            if all(term in hay for term in terms):
                out.append(_datagroup_row(raw, lang))
        return out

    def series_list(self, datagroup_code: str, lang: str = "TR") -> list[dict[str, Any]]:
        rows = self._request("serieList/", {"type": "json", "code": datagroup_code}) or []
        return [_series_row(r, lang) for r in rows]

    def series_info(self, codes: list[str], lang: str = "TR") -> list[dict[str, Any]]:
        """Metadata for individual series codes (one request each)."""
        out = []
        for position, code in enumerate(codes):
            if position:
                time.sleep(self.REQUEST_DELAY)
            rows = self._request("serieList/", {"type": "json", "code": code.strip()}) or []
            if not rows:
                out.append({"serie_code": code, "error": "unknown series code"})
                continue
            row = _series_row(rows[0], lang, full=True)
            try:
                group = self.datagroup_info(row["datagroup_code"], lang)
                row["unit"] = group.get("unit")
                row["datagroup_name"] = group.get("name")
            except EvdsError:
                pass
            out.append(row)
        return out

    # -- data ---------------------------------------------------------------

    def _fetch_pages(
        self,
        selector: dict[str, str],
        start: str,
        end: str,
        extra: dict[str, str],
        max_rows: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch [start, end], paging backwards past EVDS's 1000-row cap.

        Pages are returned newest-first; callers dedupe keeping the first
        occurrence of a period (pages agree on overlapping periods)."""
        start_date = datetime.strptime(start, "%d-%m-%Y").date()
        cursor_end = end
        collected: list[dict[str, Any]] = []
        previous_first: date | None = None
        for page in range(10_000):
            if page:
                time.sleep(self.REQUEST_DELAY)
            params = {**selector, "startDate": start, "endDate": cursor_end, "type": "json", **extra}
            payload = self._request("", params)
            if not isinstance(payload, dict) or "items" not in payload:
                raise EvdsError(f"Unexpected EVDS data response: {str(payload)[:200]}")
            items = payload.get("items") or []
            collected.extend(items)
            if len(items) < MAX_ROWS_PER_RESPONSE:
                break
            if max_rows is not None and len(collected) >= max_rows:
                break
            starts = [period_start(item.get("Tarih")) for item in items]
            starts = [s for s in starts if s is not None]
            if not starts:
                raise EvdsError("EVDS response hit the 1000-row cap and its dates are unparseable.")
            first = min(starts)
            if previous_first is not None and first >= previous_first:
                raise EvdsError("Paging through EVDS's 1000-row cap made no progress.")
            previous_first = first
            next_end = first - timedelta(days=1)
            if next_end < start_date:
                break
            cursor_end = next_end.strftime("%d-%m-%Y")
        return collected

    def get_data(
        self,
        series: list[str],
        start: str = "",
        end: str = "",
        frequency: str | int = "",
        aggregation: Any = "",
        formula: Any = "",
        last_n: int = 0,
        max_series_per_request: int = 150,
        **_ignored: Any,
    ) -> pd.DataFrame:
        """Fetch series as a wide DataFrame: 'Tarih' [, 'YEARWEEK'], one column
        per series (EVDS column names), sorted chronologically.

        end defaults to today. With last_n and no start, returns the latest
        last_n observations. Never truncated: EVDS's 1000-row cap is paged.
        """
        codes = [str(code).strip() for code in series if str(code).strip()]
        if not codes:
            raise ValueError("series must be a non-empty list of EVDS series codes")
        if len(set(codes)) != len(codes):
            raise ValueError("series contains duplicate codes")
        if not start and not last_n:
            raise ValueError("Give a start date (or last_n for the latest N observations).")
        start_n = _normalize_date(start) if start else "01-01-1900"
        end_n = _normalize_date(end) if end else _today()
        if datetime.strptime(end_n, "%d-%m-%Y") < datetime.strptime(start_n, "%d-%m-%Y"):
            raise ValueError(f"end ({end_n}) is before start ({start_n})")
        freq = _resolve(FREQUENCIES, frequency, "frequency")
        formula_list = _per_series_param(formula, len(codes), FORMULAS, "formula")
        agg_list = _per_series_param(aggregation, len(codes), None, "aggregation")
        formulas = formula_list.split("-") if formula_list else [""] * len(codes)
        aggs = agg_list.split("-") if agg_list else [""] * len(codes)

        size = max(1, int(max_series_per_request or len(codes)))
        frames: list[pd.DataFrame] = []
        for offset in range(0, len(codes), size):
            batch = codes[offset : offset + size]
            batch_f = formulas[offset : offset + size]
            batch_a = aggs[offset : offset + size]
            extra = {
                "formulas": "-".join(batch_f) if formula_list else "",
                "frequency": freq,
                "aggregationTypes": "-".join(batch_a) if agg_list else "",
            }
            try:
                items = self._fetch_pages(
                    {"series": "-".join(batch)}, start_n, end_n, extra,
                    max_rows=last_n or None,
                )
            except EvdsError as exc:
                if "HTTP 400" in str(exc):
                    unknown = self._unknown_codes(batch)
                    if unknown:
                        raise EvdsError(_unknown_message(unknown)) from exc
                raise
            expected = [column_name(c, f) for c, f in zip(batch, batch_f)]
            if not items:
                raise EvdsError(self._empty_message(batch, start_n, end_n))
            frame = frame_from_items(items, expected_columns=expected)
            missing = [c for c, col in zip(batch, expected) if col not in frame.columns]
            if missing:
                unknown = self._unknown_codes(missing)
                raise EvdsError(
                    _unknown_message(unknown) if unknown else
                    f"EVDS returned no column for {missing} (expected {expected})."
                )
            frames.append(frame)

        bad = sum(f.attrs.get("non_numeric_values", 0) for f in frames)
        frame = frames[0]
        for other in frames[1:]:
            keys = [k for k in ("Tarih", "YEARWEEK", "_sort") if k in frame.columns and k in other.columns]
            frame = frame.merge(other, on=keys, how="outer", sort=False)
            if "_sort_x" in frame.columns:
                frame["_sort"] = frame.pop("_sort_x").fillna(frame.pop("_sort_y"))
        frame.attrs["non_numeric_values"] = bad
        frame = _finish(frame)
        if last_n:
            frame = frame.tail(int(last_n)).reset_index(drop=True)
        return frame

    # Backward-compatible name: get_data already pages and is never truncated.
    def get_data_chunked(self, series: list[str], start: str = "", end: str = "", **kwargs: Any) -> pd.DataFrame:
        return self.get_data(series, start=start, end=end, **kwargs)

    def get_datagroup_data(
        self,
        datagroup_code: str,
        start: str = "",
        end: str = "",
        frequency: str | int = "",
        aggregation: Any = "",
        formula: Any = "",
        lang: str = "TR",
        last_n: int = 0,
    ) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
        """Fetch every series in a datagroup. Returns (frame, series metadata).

        Without formula/aggregation this uses EVDS's own datagroup request
        (what the website's 'whole datagroup' download does); otherwise all
        series codes are requested together. Columns follow EVDS's series
        order."""
        series_meta = self.series_list(datagroup_code, lang=lang)
        codes = [s["serie_code"] for s in series_meta if s.get("serie_code")]
        if not codes:
            raise EvdsError(
                f"Datagroup '{datagroup_code}' has no series (or the code is wrong; "
                "find codes with evds_datagroups)."
            )
        if formula not in ("", None) or aggregation not in ("", None):
            frame = self.get_data(
                codes, start=start, end=end, frequency=frequency,
                aggregation=aggregation, formula=formula, last_n=last_n,
            )
            return frame, series_meta
        if not start and not last_n:
            raise ValueError("Give a start date (or last_n for the latest N observations).")
        start_n = _normalize_date(start) if start else "01-01-1900"
        end_n = _normalize_date(end) if end else _today()
        extra = {"frequency": _resolve(FREQUENCIES, frequency, "frequency")}
        items = self._fetch_pages(
            {"datagroup": datagroup_code}, start_n, end_n, extra, max_rows=last_n or None
        )
        if not items:
            raise EvdsError(
                f"Datagroup '{datagroup_code}' has no observations between {start_n} "
                f"and {end_n}."
            )
        expected = [column_name(c) for c in codes]
        frame = _finish(frame_from_items(items, expected_columns=expected))
        if last_n:
            frame = frame.tail(int(last_n)).reset_index(drop=True)
        return frame, series_meta

    def _unknown_codes(self, codes: list[str]) -> list[str]:
        unknown = []
        for code in codes[:30]:
            time.sleep(self.REQUEST_DELAY)
            if not (self._request("serieList/", {"type": "json", "code": code}) or []):
                unknown.append(code)
        return unknown

    def _empty_message(self, codes: list[str], start: str, end: str) -> str:
        unknown = self._unknown_codes(codes)
        if unknown:
            return _unknown_message(unknown)
        spans = []
        for info in self.series_info(codes[:10]):
            spans.append(f"{info['serie_code']}: {info.get('start_date')}..{info.get('end_date')}")
        return (
            f"EVDS has no observations between {start} and {end} for these series. "
            "Available ranges: " + "; ".join(spans)
        )

    # -- series search ------------------------------------------------------

    def index_age_days(self, lang: str = "TR") -> float | None:
        """Age of the cached series index in days, or None if not built."""
        index = _load_index()
        if index is None:
            return None
        return (time.time() - float(index.get("built_at", 0))) / 86400.0

    def build_series_index(
        self,
        lang: str = "TR",
        progress: bool = False,
        delay: float | None = None,
        refresh: bool = False,
    ) -> list[dict[str, Any]]:
        """Build (and cache) a flat index of every EVDS series (TR + ENG names).

        EVDS has no server-side series search, so this walks all datagroups
        (one serieList request each). Resumable: progress is saved to a
        partial file, so an interrupted build continues where it stopped.
        A datagroup that fails is retried once at the end and, if it still
        fails, listed under 'failed' in the index instead of aborting."""
        with _INDEX_LOCK:
            index = _load_index()
            if index is not None and not refresh:
                age = (time.time() - float(index.get("built_at", 0))) / 86400.0
                if age <= self.INDEX_MAX_AGE_DAYS:
                    return [_localize(r, index, lang) for r in index["series"]]
            pause = self.REQUEST_DELAY if delay is None else delay
            partial = _read_json(_cache_file(_PARTIAL_NAME)) or {}
            if refresh or time.time() - float(partial.get("started", 0)) > 2 * 86400:
                partial = {}
            done: dict[str, list] = partial.get("done", {})
            started = partial.get("started", time.time())
            groups = self._raw_datagroups()
            _BUILD_STATUS.update(total=len(groups), done=len(done), running=True, error="")
            failed: dict[str, str] = {}

            def walk(codes: list[dict[str, Any]], label: str) -> None:
                for position, group in enumerate(codes, start=1):
                    code = group.get("DATAGROUP_CODE")
                    if not code or code in done:
                        continue
                    if progress:
                        print(f"  [{label} {position}/{len(codes)}] {code}", flush=True)
                    try:
                        rows = self._request("serieList/", {"type": "json", "code": code}) or []
                        done[code] = [_index_row(r, code) for r in rows]
                        failed.pop(code, None)
                    except Exception as exc:  # noqa: BLE001 - recorded, retried later
                        if "API key" in str(exc) or "HTTP 401" in str(exc):
                            raise
                        failed[code] = str(exc)[:200]
                    _BUILD_STATUS["done"] = len(done)
                    if len(done) % 25 == 0:
                        _write_json(_cache_file(_PARTIAL_NAME), {"started": started, "done": done})
                    time.sleep(pause)

            try:
                walk(groups, "datagroup")
                if failed:
                    retry = [g for g in groups if g.get("DATAGROUP_CODE") in failed]
                    time.sleep(2.0)
                    walk(retry, "retry")
            except Exception as exc:
                _write_json(_cache_file(_PARTIAL_NAME), {"started": started, "done": done})
                _BUILD_STATUS.update(running=False, error=str(exc)[:300])
                raise
            series = [row for g in groups for row in done.get(g.get("DATAGROUP_CODE"), [])]
            index = {
                "version": _INDEX_VERSION,
                "built_at": time.time(),
                "n_datagroups": len(groups),
                "failed": failed,
                "datagroups": {
                    g.get("DATAGROUP_CODE"): [
                        g.get("DATAGROUP_NAME"), g.get("DATAGROUP_NAME_ENG"),
                        _as_int(g.get("CATEGORY_ID")),
                    ]
                    for g in groups if g.get("DATAGROUP_CODE")
                },
                "series": series,
            }
            _store_index(index)
            path = _cache_file(_PARTIAL_NAME)
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            _BUILD_STATUS.update(running=False, done=len(done))
            return [_localize(r, index, lang) for r in series]

    def index_status(self) -> dict[str, Any]:
        index = _load_index()
        status = {
            "index_ready": index is not None,
            "index_age_days": None if index is None else round(
                (time.time() - float(index.get("built_at", 0))) / 86400.0, 2),
            "index_failed_datagroups": [] if index is None else sorted(index.get("failed", {})),
            "building": bool(_BUILD_STATUS.get("running")),
            "build_progress": f"{_BUILD_STATUS.get('done', 0)}/{_BUILD_STATUS.get('total', 0)}",
            "cache_dir": str(_cache_dir()) if _cache_dir() else "memory-only",
        }
        if _BUILD_STATUS.get("error"):
            status["last_build_error"] = _BUILD_STATUS["error"]
        return status

    def start_index_build(self, refresh: bool = False) -> bool:
        """Build the index in a background thread. False if one is running."""
        if _BUILD_STATUS.get("running") or _INDEX_LOCK.locked():
            return False
        _BUILD_STATUS.update(running=True, error="")

        def run() -> None:
            try:
                self.build_series_index(refresh=refresh)
            except Exception as exc:  # noqa: BLE001 - surfaced via index_status
                _BUILD_STATUS.update(running=False, error=str(exc)[:300])

        threading.Thread(target=run, name="evds-index-build", daemon=True).start()
        return True

    def search_series(
        self,
        query: str,
        lang: str = "TR",
        limit: int = 50,
        refresh: bool = False,
        wait: bool = True,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int, bool]:
        """Search series by name, code, tags and datagroup name.
        Returns (hits, n_total, index_was_built_now).

        wait=False: if the index is missing, start building it in the
        background and raise IndexNotReady instead of blocking. A stale index
        (older than INDEX_MAX_AGE_DAYS) is used as-is while a background
        refresh runs."""
        index = _load_index()
        built_now = False
        stale = index is not None and (
            time.time() - float(index.get("built_at", 0))
        ) / 86400.0 > self.INDEX_MAX_AGE_DAYS
        if index is None or refresh:
            if wait:
                self.build_series_index(lang=lang, refresh=refresh)
                index = _load_index()
                built_now = True
            else:
                self.start_index_build(refresh=refresh)
                if index is None:
                    raise IndexNotReady(self.index_status())
        elif stale:
            self.start_index_build(refresh=True)
        assert index is not None
        terms = _fold(query).split()
        hits = [
            _localize(row, index, lang)
            for row, folded in zip(index["series"], _search_texts(index))
            if all(term in folded for term in terms)
        ]
        return hits[offset : offset + limit], len(hits), built_now


class IndexNotReady(EvdsError):
    def __init__(self, status: dict[str, Any]) -> None:
        super().__init__(
            "EVDS series index is being built in the background "
            f"({status.get('build_progress')} datagroups); search again in a few minutes."
        )
        self.status = status


# -- response parsing --------------------------------------------------------


def _parse_response(response: httpx.Response) -> Any:
    status = response.status_code
    text = response.text or ""
    message = text.strip()[:200]
    try:
        body = response.json()
        if isinstance(body, dict) and body.get("message"):
            message = str(body["message"])
    except ValueError:
        body = None
    if status == 401:
        raise EvdsError(
            f"EVDS rejected the API key (HTTP 401: {message}). Check EVDS_API_KEY / "
            "the X-Evds-Api-Key header; keys are on your evds3.tcmb.gov.tr profile page."
        )
    if status == 403:
        raise EvdsError(f"EVDS refused the request (HTTP 403: {message}). Check the API key.")
    if status == 429:
        raise EvdsError("EVDS rate limit hit (HTTP 429). Wait a minute and retry with fewer requests.")
    if status == 400:
        raise EvdsError(
            f"EVDS rejected the request (HTTP 400: {message}). Check series/datagroup "
            "codes (case-sensitive, e.g. TP.DK.USD.A.YTL), dates, frequency, formula "
            "and aggregation (formula/aggregation cannot be used on a whole datagroup)."
        )
    if status != 200:
        raise EvdsError(f"EVDS returned HTTP {status}: {message}")
    if body is None:
        kind = response.headers.get("content-type", "?")
        raise EvdsError(
            f"EVDS returned a non-JSON response ({kind}): {message[:120]!r}. "
            "The service may be under maintenance; try again later."
        )
    return body


def frame_from_items(
    items: list[dict[str, Any]],
    series: list[str] | None = None,
    expected_columns: list[str] | None = None,
) -> pd.DataFrame:
    """Convert EVDS 'items' JSON to a typed DataFrame.

    Keeps 'Tarih' (and 'YEARWEEK' for weekly data) as EVDS sends them, makes
    every value column numeric (null/'ND' -> NaN), removes duplicate periods
    (first occurrence wins) and adds a private '_sort' key (period start)."""
    if not items:
        raise EvdsError(
            "EVDS returned no observations for this query "
            "(check series codes, dates and frequency)."
        )
    if expected_columns is None and series is not None:
        expected_columns = [column_name(code) for code in series]
    frame = pd.DataFrame(items)
    # Sort key: the period's start date. UNIXTIME is only a fallback: pages of
    # one request can disagree on it by an hour (DST), so it is not a key.
    frame["_sort"] = [_sort_key(item) for item in items]
    frame = frame.drop(columns=["UNIXTIME"], errors="ignore")
    id_cols = [c for c in ("Tarih", "YEARWEEK") if c in frame.columns]
    frame = frame.drop_duplicates(subset=id_cols or ["_sort"], keep="first")
    value_cols = [c for c in frame.columns if c not in id_cols and c != "_sort"]
    bad = 0
    for column in value_cols:
        converted = [_to_number(v) for v in frame[column]]
        bad += sum(1 for _, unparseable in converted if unparseable)
        frame[column] = pd.Series([v for v, _ in converted], index=frame.index, dtype="float64")
    if expected_columns:
        ordered = [c for c in expected_columns if c in value_cols]
        ordered += [c for c in value_cols if c not in ordered]
    else:
        ordered = value_cols
    frame = frame[id_cols + ordered + ["_sort"]]
    frame.attrs["non_numeric_values"] = bad
    return frame


def _finish(frame: pd.DataFrame) -> pd.DataFrame:
    bad = frame.attrs.get("non_numeric_values", 0)
    if "_sort" in frame.columns:
        frame = frame.sort_values("_sort", kind="stable").drop(columns=["_sort"])
    frame = frame.reset_index(drop=True)
    frame.attrs["non_numeric_values"] = bad
    return frame


def _sort_key(item: dict[str, Any]) -> int:
    start = period_start(item.get("Tarih"))
    if start is not None:
        return start.toordinal()
    stamp = _unixtime(item)
    return 719163 + stamp // 86400 if stamp is not None else 0


def to_long(frame: pd.DataFrame, column_to_series: dict[str, str] | None = None) -> pd.DataFrame:
    """Wide -> tidy long: Tarih [, YEARWEEK], date (ISO period start), series,
    column, value. Null observations are dropped."""
    id_cols = [c for c in ("Tarih", "YEARWEEK") if c in frame.columns]
    value_cols = [c for c in frame.columns if c not in id_cols]
    long = frame.melt(id_vars=id_cols, value_vars=value_cols, var_name="column", value_name="value")
    long = long.dropna(subset=["value"])
    mapping = column_to_series or {}
    long["series"] = [
        mapping.get(col.split("-")[0]) or col.split("-")[0].replace("_", ".") for col in long["column"]
    ]
    long["date"] = [
        (period_start(t).isoformat() if period_start(t) else None) for t in long["Tarih"]
    ]
    return long[id_cols + ["date", "series", "column", "value"]].reset_index(drop=True)


# -- metadata rows -----------------------------------------------------------


def _as_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _datagroup_row(r: dict[str, Any], lang: str, full: bool = False) -> dict[str, Any]:
    eng = _norm_lang(lang) == "ENG"
    row = {
        "category_id": _as_int(r.get("CATEGORY_ID")),
        "datagroup_code": r.get("DATAGROUP_CODE"),
        "name": (r.get("DATAGROUP_NAME_ENG") if eng else None) or r.get("DATAGROUP_NAME"),
        "frequency": r.get("FREQUENCY_STR"),
        "start_date": r.get("START_DATE"),
        "end_date": r.get("END_DATE"),
        "unit": (r.get("BIRIMI_EN") if eng else None) or r.get("BIRIMI"),
        "source": (r.get("DATASOURCE_ENG") if eng else None) or r.get("DATASOURCE"),
        "last_updated": r.get("LAST_UPDATED"),
    }
    if full:
        row.update(
            note=(r.get("NOTE_ENG") if eng else None) or r.get("NOTE"),
            metadata_link=(r.get("METADATA_LINK_ENG") if eng else None) or r.get("METADATA_LINK"),
            revision_policy_link=(r.get("REV_POL_LINK_ENG") if eng else None) or r.get("REV_POL_LINK"),
            methodology_changes_link=(r.get("APP_CHA_LINK_ENG") if eng else None) or r.get("APP_CHA_LINK"),
        )
    return row


_AGG_FLAGS = (("avg", "AVGABLE"), ("min", "MINABLE"), ("max", "MAXABLE"),
              ("first", "FIRSTABLE"), ("last", "LASTABLE"), ("sum", "SUMABLE"))


def _series_row(r: dict[str, Any], lang: str, full: bool = False) -> dict[str, Any]:
    eng = _norm_lang(lang) == "ENG"
    row = {
        "serie_code": r.get("SERIE_CODE"),
        "name": " ".join(str((r.get("SERIE_NAME_ENG") if eng else None) or r.get("SERIE_NAME") or "").split()),
        "frequency": r.get("FREQUENCY_STR"),
        "start_date": r.get("START_DATE"),
        "end_date": r.get("END_DATE"),
        "default_aggregation": r.get("DEFAULT_AGG_METHOD"),
    }
    if full:
        row.update(
            datagroup_code=r.get("DATAGROUP_CODE"),
            source=(r.get("DATASOURCE_ENG") if eng else None) or r.get("DATASOURCE"),
            allowed_aggregations=[name for name, flag in _AGG_FLAGS if r.get(flag) == 1],
            tags=" ".join(str((r.get("TAG_ENG") if eng else None) or r.get("TAG") or "").split()),
            metadata_link=(r.get("METADATA_LINK_ENG") if eng else None) or r.get("METADATA_LINK"),
            revision_policy_link=(r.get("REV_POL_LINK_ENG") if eng else None) or r.get("REV_POL_LINK"),
        )
    return row


# Compact index row: [serie_code, name_tr, name_en, frequency, start, end, datagroup_code]
def _index_row(r: dict[str, Any], datagroup_code: str) -> list[Any]:
    return [
        r.get("SERIE_CODE"),
        " ".join(str(r.get("SERIE_NAME") or "").split()),
        " ".join(str(r.get("SERIE_NAME_ENG") or "").split()),
        r.get("FREQUENCY_STR"),
        r.get("START_DATE"),
        r.get("END_DATE"),
        datagroup_code,
    ]


def _search_texts(index: dict[str, Any]) -> list[str]:
    """Folded 'code + TR/EN names + datagroup code/names' per series (cached)."""
    cached = index.get("_folded")
    if cached is None:
        groups = {
            code: _fold(f"{code} {values[0] or ''} {values[1] or ''}")
            for code, values in index.get("datagroups", {}).items()
        }
        cached = [
            f"{_fold(f'{row[0]} {row[1]} {row[2]}')} {groups.get(row[6], '')}"
            for row in index["series"]
        ]
        index["_folded"] = cached
    return cached


def _localize(row: list[Any], index: dict[str, Any], lang: str) -> dict[str, Any]:
    eng = _norm_lang(lang) == "ENG"
    group = index.get("datagroups", {}).get(row[6]) or [None, None, None]
    return {
        "serie_code": row[0],
        "name": (row[2] if eng else None) or row[1],
        "frequency": row[3],
        "start_date": row[4],
        "end_date": row[5],
        "datagroup_code": row[6],
        "datagroup_name": (group[1] if eng else None) or group[0],
        "category_id": group[2],
    }


def _unknown_message(codes: list[str]) -> str:
    return (
        f"Unknown EVDS series code(s): {', '.join(codes)}. Codes are case-sensitive "
        "(e.g. TP.DK.USD.A.YTL); find them with evds_search_series or evds_series_list."
    )


# -- index cache -------------------------------------------------------------

_INDEX_VERSION = 3
_INDEX_NAME = "evds_series_index_v3.json"
_PARTIAL_NAME = "evds_series_index_v3.partial.json"
_INDEX_LOCK = threading.Lock()
_BUILD_STATUS: dict[str, Any] = {"running": False, "done": 0, "total": 0, "error": ""}
_MEMORY_INDEX: dict[str, Any] = {}
_CACHE_DIR: list[Path | None] = []


def _cache_dir() -> Path | None:
    """First writable cache dir: $TURKIYE_VERI_CACHE, ~/.cache/turkiye-veri-mcp,
    then the system temp dir. None -> memory only (read-only host)."""
    if _CACHE_DIR:
        return _CACHE_DIR[0]
    candidates: list[Path] = []
    if os.environ.get("TURKIYE_VERI_CACHE"):
        candidates.append(Path(os.environ["TURKIYE_VERI_CACHE"]).expanduser())
    candidates.append(Path.home() / ".cache" / "turkiye-veri-mcp")
    candidates.append(Path(tempfile.gettempdir()) / "turkiye-veri-mcp")
    chosen: Path | None = None
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / f".write_test_{os.getpid()}"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            chosen = candidate
            break
        except OSError:
            continue
    _CACHE_DIR.append(chosen)
    return chosen


def _cache_file(name: str) -> Path | None:
    root = _cache_dir()
    return root / name if root else None


def _index_cache_path(lang: str = "TR") -> Path | None:  # kept for compatibility
    return _cache_file(_INDEX_NAME)


def _read_json(path: Path | None) -> Any:
    if path is None or not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path | None, payload: Any) -> bool:
    if path is None:
        return False
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def _load_index() -> dict[str, Any] | None:
    if _MEMORY_INDEX.get("index"):
        return _MEMORY_INDEX["index"]
    data = _read_json(_cache_file(_INDEX_NAME))
    if (
        isinstance(data, dict)
        and data.get("version") == _INDEX_VERSION
        and isinstance(data.get("series"), list)
    ):
        _MEMORY_INDEX["index"] = data
        return data
    return None


def _store_index(index: dict[str, Any]) -> None:
    _MEMORY_INDEX["index"] = index
    _write_json(_cache_file(_INDEX_NAME), {k: v for k, v in index.items() if not k.startswith("_")})


def _date_windows(start: str, end: str, years: int) -> list[tuple[str, str]]:
    """Split a dd-mm-yyyy range into windows of at most `years` years.
    (Kept for compatibility; data fetching now pages on the row cap.)"""
    start_date = datetime.strptime(start, "%d-%m-%Y")
    end_date = datetime.strptime(end, "%d-%m-%Y")
    if end_date < start_date:
        raise ValueError("end date is before start date")
    windows: list[tuple[str, str]] = []
    cursor = start_date
    while cursor <= end_date:
        try:
            stop = cursor.replace(year=cursor.year + years)
        except ValueError:  # 29 Feb
            stop = cursor.replace(year=cursor.year + years, day=28)
        stop = min(stop, end_date)
        windows.append((cursor.strftime("%d-%m-%Y"), stop.strftime("%d-%m-%Y")))
        if stop >= end_date:
            break
        cursor = stop + timedelta(days=1)
    return windows
