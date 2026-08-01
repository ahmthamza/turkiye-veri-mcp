"""Client for TCMB EVDS v3 (evds3.tcmb.gov.tr).

Endpoint behaviour verified against the open-source `evds` package
(github.com/fatihmete/evds, MIT), which tracks the live EVDS3 API:

- Base URL: https://evds3.tcmb.gov.tr/igmevdsms-dis/
- API key goes in the 'key' HTTP header (free registration at evds3.tcmb.gov.tr)
- Query parameters are appended to the URL *without* a '?' separator
- TCMB's server requires legacy SSL renegotiation (OP_LEGACY_SERVER_CONNECT)
- Numeric values arrive as strings; series codes have dots replaced by
  underscores in JSON column names
"""

from __future__ import annotations

import json
import os
import ssl
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

EVDS_BASE = "https://evds3.tcmb.gov.tr/igmevdsms-dis/"

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


class EvdsError(RuntimeError):
    """Raised for EVDS API errors, including missing API key."""


def _legacy_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    ctx.options |= 0x4  # OP_LEGACY_SERVER_CONNECT: TCMB requires this
    return ctx


def _normalize_date(value: str) -> str:
    """Accept dd-mm-yyyy (EVDS native) or ISO yyyy-mm-dd; return dd-mm-yyyy."""
    value = value.strip()
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).strftime("%d-%m-%Y")
        except ValueError:
            continue
    raise ValueError(
        f"Unrecognized date '{value}'. Use dd-mm-yyyy or yyyy-mm-dd."
    )


def _resolve(mapping: dict[str, int], value: str | int, what: str) -> str:
    if value in ("", None):
        return ""
    if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
        return str(value)
    key = str(value).strip().casefold().replace(" ", "_")
    if key in mapping:
        return str(mapping[key])
    raise ValueError(
        f"Unknown {what} '{value}'. Use a numeric code or one of: "
        + ", ".join(sorted(set(mapping)))
    )


def _per_series_param(value: Any, n_series: int, mapping: dict[str, int] | None, what: str) -> str:
    """EVDS wants dash-joined per-series values; a scalar applies to all."""
    if value in ("", None):
        return ""
    if isinstance(value, list):
        items = value
    else:
        items = [value] * n_series
    if mapping is not None:
        return "-".join(_resolve(mapping, item, what) for item in items)
    for item in items:
        if str(item) not in AGGREGATIONS:
            raise ValueError(
                f"Unknown {what} '{item}'. Valid: {', '.join(AGGREGATIONS)}"
            )
    return "-".join(str(item) for item in items)


class EvdsClient:
    #: Datagroup list cache lifetime (seconds) and series-index cache age
    #: limit (days). Both keep a long-running server from hiding series that
    #: TCMB published after startup.
    CACHE_TTL_SECONDS = 3600.0
    INDEX_MAX_AGE_DAYS = 7.0

    def __init__(self, api_key: str | None = None, timeout: float = 120.0) -> None:
        self._api_key = api_key or os.environ.get("EVDS_API_KEY", "")
        self._timeout = timeout
        self._datagroup_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}

    def _require_key(self) -> str:
        if not self._api_key:
            raise EvdsError(
                "EVDS_API_KEY is not set. Get a free key at "
                "https://evds3.tcmb.gov.tr (BENIM SAYFAM -> Kayit -> Profilim "
                "-> API Key) and set it as the EVDS_API_KEY environment "
                "variable in your MCP config."
            )
        return self._api_key

    def _request(self, path: str, params: dict[str, Any]) -> httpx.Response:
        key = self._require_key()
        # EVDS appends params without '?': .../categories/type=json
        param_text = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{EVDS_BASE}{path}{param_text}"
        with httpx.Client(
            timeout=self._timeout,
            follow_redirects=True,
            verify=_legacy_ssl_context(),
        ) as client:
            response = client.get(url, headers={"key": key})
        if response.status_code != 200:
            raise EvdsError(
                f"EVDS returned HTTP {response.status_code}. "
                "Check your API key and request parameters."
            )
        return response

    def categories(self, lang: str = "TR") -> list[dict[str, Any]]:
        rows = self._request("categories/", {"type": "json"}).json()
        title_field = "TOPIC_TITLE_ENG" if lang.upper() == "ENG" else "TOPIC_TITLE_TR"
        return [
            {"category_id": r.get("CATEGORY_ID"), "title": r.get(title_field)}
            for r in rows
        ]

    def datagroups(
        self, category_id: int | str | None = None, lang: str = "TR"
    ) -> list[dict[str, Any]]:
        cache_key = str(category_id or "ALL")
        cached = self._datagroup_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < self.CACHE_TTL_SECONDS:
            rows = cached[1]
        else:
            if category_id in (None, "", "ALL"):
                params = {"mode": 0, "code": "", "type": "json"}
            else:
                params = {"mode": 2, "code": category_id, "type": "json"}
            rows = self._request("datagroups/", params).json() or []
            self._datagroup_cache[cache_key] = (time.monotonic(), rows)
        name_field = "DATAGROUP_NAME_ENG" if lang.upper() == "ENG" else "DATAGROUP_NAME"
        return [
            {
                "category_id": r.get("CATEGORY_ID"),
                "datagroup_code": r.get("DATAGROUP_CODE"),
                "name": r.get(name_field) or r.get("DATAGROUP_NAME"),
                "frequency": r.get("FREQUENCY_STR"),
                "start_date": r.get("START_DATE"),
                "end_date": r.get("END_DATE"),
            }
            for r in rows
        ]

    def search_datagroups(self, query: str, lang: str = "TR") -> list[dict[str, Any]]:
        needle = query.casefold()
        return [
            row
            for row in self.datagroups(None, lang=lang)
            if needle in str(row["name"] or "").casefold()
        ]

    def series_list(self, datagroup_code: str, lang: str = "TR") -> list[dict[str, Any]]:
        rows = self._request(
            "serieList/", {"type": "json", "code": datagroup_code}
        ).json() or []
        name_field = "SERIE_NAME_ENG" if lang.upper() == "ENG" else "SERIE_NAME"
        return [
            {
                "serie_code": r.get("SERIE_CODE"),
                "name": r.get(name_field) or r.get("SERIE_NAME"),
                "start_date": r.get("START_DATE"),
                "frequency": r.get("FREQUENCY_STR"),
            }
            for r in rows
        ]

    def get_data(
        self,
        series: list[str],
        start: str,
        end: str = "",
        frequency: str | int = "",
        aggregation: Any = "",
        formula: Any = "",
    ) -> pd.DataFrame:
        if not series:
            raise ValueError("series must be a non-empty list of EVDS series codes")
        params = {
            "series": "-".join(series),
            "startDate": _normalize_date(start),
            "endDate": _normalize_date(end) if end else _normalize_date(start),
            "type": "json",
            "formulas": _per_series_param(formula, len(series), FORMULAS, "formula"),
            "frequency": _resolve(FREQUENCIES, frequency, "frequency"),
            "aggregationTypes": _per_series_param(
                aggregation, len(series), None, "aggregation"
            ),
        }
        payload = self._request("", params).json()
        return frame_from_items(payload.get("items", []), series)

    # -- large requests -----------------------------------------------------

    def get_data_chunked(
        self,
        series: list[str],
        start: str,
        end: str = "",
        frequency: str | int = "",
        aggregation: Any = "",
        formula: Any = "",
        max_series_per_request: int = 8,
        years_per_request: int = 10,
    ) -> pd.DataFrame:
        """Fetch many series / long spans by splitting into safe requests.

        EVDS caps the size of a single response, so a wide-and-long query can
        come back truncated. This splits by series and by time window, then
        stitches the pieces back together on the date column.
        """
        if not series:
            raise ValueError("series must be a non-empty list of EVDS series codes")

        windows = _date_windows(
            _normalize_date(start),
            _normalize_date(end) if end else _normalize_date(start),
            years_per_request,
        )
        batches = [
            series[i : i + max_series_per_request]
            for i in range(0, len(series), max_series_per_request)
        ]

        per_batch: list[pd.DataFrame] = []
        for batch in batches:
            pieces: list[pd.DataFrame] = []
            for window_start, window_end in windows:
                try:
                    pieces.append(
                        self.get_data(
                            batch,
                            start=window_start,
                            end=window_end,
                            frequency=frequency,
                            aggregation=aggregation,
                            formula=formula,
                        )
                    )
                except EvdsError:
                    continue  # window with no observations
            if pieces:
                per_batch.append(pd.concat(pieces, ignore_index=True))

        if not per_batch:
            raise EvdsError(
                "EVDS returned no observations for any chunk "
                "(check series codes, dates and frequency)."
            )

        merged = per_batch[0]
        for frame in per_batch[1:]:
            # EVDS always returns the date/period column first, but its
            # name varies by frequency (e.g. "Tarih"). Using the actual
            # first column name -- rather than a hardcoded guess -- avoids
            # silently falling back to a row-position concat, which would
            # misalign values whenever two batches have different numbers
            # of observations (e.g. one series starts later than another).
            key = merged.columns[0]
            if key in frame.columns:
                merged = merged.merge(frame, on=key, how="outer")
            else:
                merged = pd.concat([merged, frame], axis=1)
        date_col = merged.columns[0]
        merged = merged.drop_duplicates(subset=[date_col]).reset_index(drop=True)
        return merged

    def get_datagroup_data(
        self,
        datagroup_code: str,
        start: str,
        end: str = "",
        frequency: str | int = "",
        aggregation: Any = "",
        formula: Any = "",
        lang: str = "TR",
    ) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
        """Fetch every series in a datagroup (chunked). Returns (frame, series)."""
        series_meta = self.series_list(datagroup_code, lang=lang)
        codes = [s["serie_code"] for s in series_meta if s.get("serie_code")]
        if not codes:
            raise EvdsError(f"Datagroup '{datagroup_code}' has no series.")
        frame = self.get_data_chunked(
            codes, start=start, end=end,
            frequency=frequency, aggregation=aggregation, formula=formula,
        )
        return frame, series_meta

    # -- series search ------------------------------------------------------

    def index_age_days(self, lang: str = "TR") -> float | None:
        """Age of the cached series index in days, or None if not built."""
        cache = _index_cache_path(lang)
        if not cache.exists():
            return None
        return (time.time() - cache.stat().st_mtime) / 86400.0

    def build_series_index(
        self,
        lang: str = "TR",
        progress: bool = False,
        delay: float = 0.2,
        refresh: bool = False,
    ) -> list[dict[str, Any]]:
        """Build (and cache to disk) a flat index of every EVDS series.

        EVDS has no server-side series search, so we walk datagroups once and
        keep the result in a local cache file; later searches are instant.
        """
        cache = _index_cache_path(lang)
        age = self.index_age_days(lang)
        fresh_enough = age is not None and age <= self.INDEX_MAX_AGE_DAYS
        if cache.exists() and fresh_enough and not refresh:
            try:
                return json.loads(cache.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                pass

        index: list[dict[str, Any]] = []
        groups = self.datagroups(None, lang=lang)
        for position, group in enumerate(groups, start=1):
            code = group.get("datagroup_code")
            if not code:
                continue
            if progress:
                print(f"  [{position}/{len(groups)}] {code}")
            try:
                for serie in self.series_list(code, lang=lang):
                    index.append(
                        {
                            "serie_code": serie["serie_code"],
                            "name": serie["name"],
                            "frequency": serie.get("frequency"),
                            "start_date": serie.get("start_date"),
                            "datagroup_code": code,
                            "datagroup_name": group.get("name"),
                            "category_id": group.get("category_id"),
                        }
                    )
            except Exception:  # noqa: BLE001 - skip unreadable groups
                continue
            time.sleep(delay)

        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        return index

    def search_series(
        self, query: str, lang: str = "TR", limit: int = 50, refresh: bool = False
    ) -> tuple[list[dict[str, Any]], int, bool]:
        """Search series by name. Returns (hits, n_total, index_was_built_now).

        The index is rebuilt automatically once it is older than
        INDEX_MAX_AGE_DAYS, so series TCMB adds later still show up.
        """
        age_before = self.index_age_days(lang)
        was_fresh = age_before is not None and age_before <= self.INDEX_MAX_AGE_DAYS
        index = self.build_series_index(lang=lang, refresh=refresh)
        needle = _fold(query)
        terms = [t for t in needle.split() if t]
        hits = [
            row
            for row in index
            if all(term in _fold(f"{row['name']} {row['datagroup_name']}") for term in terms)
        ]
        return hits[:limit], len(hits), not (was_fresh and not refresh)


def _fold(text: str) -> str:
    """Casefold with Turkish dotted/dotless i normalised, so 'işgücü'
    matches 'İŞGÜCÜ' and 'isgucu' matches neither too loosely."""
    return (
        str(text)
        .replace("İ", "i").replace("I", "ı")
        .casefold()
    )


def _index_cache_path(lang: str) -> Path:
    root = os.environ.get("TURKIYE_VERI_CACHE") or (Path.home() / ".cache" / "turkiye-veri-mcp")
    return Path(root) / f"evds_series_index_{lang.upper()}.json"


def _date_windows(start: str, end: str, years: int) -> list[tuple[str, str]]:
    """Split a dd-mm-yyyy range into windows of at most `years` years."""
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


def frame_from_items(items: list[dict[str, Any]], series: list[str]) -> pd.DataFrame:
    """Convert EVDS 'items' JSON to a typed DataFrame."""
    if not items:
        raise EvdsError(
            "EVDS returned no observations for this query "
            "(check series codes, dates and frequency)."
        )
    frame = pd.DataFrame(items)
    if "UNIXTIME" in frame.columns:
        frame = frame.drop(columns=["UNIXTIME"])
    for code in series:
        column = code.replace(".", "_")
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame
