"""Client for TUIK's SDMX-equivalent data access.

Primary path: TUIK DataBrowser2 (databrowser2.tuik.gov.tr), the backend
behind TUIK's current official web data browser. TUIK's older SDMX 2.1 REST
service (nsiws.tuik.gov.tr) started returning HTTP 401 on every request --
confirmed from multiple networks and with/without session cookies, so this
is very likely TUIK retiring or restricting that endpoint rather than
anything fixable client-side. DataBrowser2 was found by inspecting the
network calls TUIK's own web UI makes and is not itself documented; the
request/response shapes below were reverse-engineered from real captured
traffic (2026-08) and validated against real TUIK observations.

DataBrowser2 shape, for a dataflow_id like "TR,DF_ADNKS_T30,1.1":

  GET  /api/core/nodes/{node}/datasets/{dataflow_id}/structure
       -> filterable dimensions (`criteria`) and a ready-to-send default
          selection (`template.criteria`) -- exactly the body /data expects.

  POST /api/core/nodes/{node}/datasets/{dataflow_id}/data
       body = the criteria array, e.g.
       [{"id": "REF_AREA", "filterValues": ["TR"], "type": "CodeValues", "period": 0}, ...]
       -> a JSON-stat 2.0 dataset (https://json-stat.org/), parsed here with
          pyjstat. JSON-stat is sparse: a flat index absent from `value`
          means that observation is missing/suppressed, not zero -- pyjstat
          handles this correctly, a hand-rolled unflattening would not.

`node` has been 1 for every dataflow observed so far.

The legacy nsiws.tuik.gov.tr SDMX 2.1 path is kept as a fallback (tried
second) in case DataBrowser2 ever fails to cover something nsiws did.
"""

from __future__ import annotations

import io
import json
import xml.etree.ElementTree as ET
from typing import Any

import httpx
import pandas as pd
from pyjstat import pyjstat

from turkiye_veri_mcp.portal import _UA

DATABROWSER_BASE = "https://databrowser2.tuik.gov.tr/api/core/nodes"
_DB_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/json",
    "Content-Type": "application/json",
}

NSIWS_BASE = "https://nsiws.tuik.gov.tr/rest"
_CSV_ACCEPT = "application/vnd.sdmx.data+csv; version=1.0.0; labels=both"
_XML_DATA_ACCEPT = "application/vnd.sdmx.genericdata+xml; version=2.1"
_XML_STRUCTURE_ACCEPT = "application/vnd.sdmx.structure+xml; version=2.1"
_BASE_HEADERS = {"User-Agent": _UA, "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8"}


class SdmxError(RuntimeError):
    """Raised when neither TUIK data-access path can serve a dataflow."""


def validate_dataflow_id(dataflow_id: str) -> str:
    parts = dataflow_id.split(",")
    if len(parts) != 3 or not all(parts):
        raise ValueError(
            "dataflow_id must have three comma-separated parts, "
            "e.g. 'TR,DF_ADNKS_T26,1.0'"
        )
    return dataflow_id


# ---------------------------------------------------------------------------
# Primary path: TUIK DataBrowser2
# ---------------------------------------------------------------------------

def _dataset_url(dataflow_id: str, node: int = 1) -> str:
    validate_dataflow_id(dataflow_id)
    return f"{DATABROWSER_BASE}/{node}/datasets/{dataflow_id}"


def _fetch_structure_raw(dataflow_id: str, node: int = 1, timeout: float = 60.0) -> dict[str, Any]:
    url = f"{_dataset_url(dataflow_id, node)}/structure"
    with httpx.Client(timeout=timeout, headers=_DB_HEADERS, follow_redirects=True) as client:
        response = client.get(url)
    if response.status_code != 200:
        raise SdmxError(f"TUIK DataBrowser2 returned HTTP {response.status_code} for {url}")
    return response.json()


def _fetch_data_raw(
    dataflow_id: str,
    criteria: list[dict[str, Any]],
    node: int = 1,
    timeout: float = 120.0,
) -> dict[str, Any]:
    url = f"{_dataset_url(dataflow_id, node)}/data"
    with httpx.Client(timeout=timeout, headers=_DB_HEADERS, follow_redirects=True) as client:
        response = client.post(url, content=json.dumps(criteria))
    if response.status_code != 200:
        raise SdmxError(f"TUIK DataBrowser2 returned HTTP {response.status_code} for {url}")
    return response.json()


def _jsonstat_to_frame(payload: dict[str, Any]) -> pd.DataFrame:
    """Convert a JSON-stat 2.0 dataset to a tidy long DataFrame via pyjstat."""
    dataset = pyjstat.Dataset.read(json.dumps(payload))
    frame = dataset.write("dataframe")
    if "value" in frame.columns:
        frame = frame.rename(columns={"value": "deger"})
        frame["deger"] = pd.to_numeric(frame["deger"], errors="coerce")
    return frame


def _databrowser_fetch_data(
    dataflow_id: str,
    key: str,
    start: str | None,
    end: str | None,
    timeout: float,
) -> pd.DataFrame:
    structure = _fetch_structure_raw(dataflow_id, timeout=timeout)
    criteria = structure.get("template", {}).get("criteria")
    if not criteria:
        raise SdmxError(f"No default selection found in structure for '{dataflow_id}'.")
    if key not in ("", "ALL"):
        raise SdmxError(
            "Narrowed SDMX keys aren't supported on the DataBrowser2 path yet "
            "-- only key='ALL' works right now. Call tuik_describe_dataflow "
            "to see the dimensions, then filter the returned rows yourself."
        )
    payload = _fetch_data_raw(dataflow_id, criteria, timeout=timeout)
    frame = _jsonstat_to_frame(payload)

    time_id = (payload.get("role", {}).get("time") or [None])[0]
    time_label = payload.get("dimension", {}).get(time_id or "", {}).get("label")
    period_col = time_label if time_label in frame.columns else None
    if period_col and (start or end):
        as_str = frame[period_col].astype(str)
        if start:
            frame = frame[as_str >= str(start)]
        if end:
            frame = frame[as_str <= str(end)]
    return frame


def _databrowser_fetch_structure(dataflow_id: str, lang: str, timeout: float) -> dict[str, Any]:
    structure = _fetch_structure_raw(dataflow_id, timeout=timeout)
    criteria = structure.get("template", {}).get("criteria") or []
    payload = _fetch_data_raw(dataflow_id, criteria, timeout=timeout)

    order: list[str] = payload.get("id", [])
    dims_raw: dict[str, Any] = payload.get("dimension", {})
    time_dim = (payload.get("role", {}).get("time") or [None])[0]

    dimensions = []
    for position, dim_id in enumerate(order):
        info = dims_raw.get(dim_id, {})
        codes: dict[str, str] = info.get("category", {}).get("label", {}) or {}
        dimensions.append(
            {
                "id": dim_id,
                "position": position,
                "label": info.get("label", dim_id),
                "is_time": dim_id == time_dim,
                "n_codes": len(codes),
                "codes": dict(list(codes.items())[:40]),
            }
        )

    key_dims = [d for d in dimensions if not d["is_time"]]
    return {
        "dimensions": dimensions,
        "key_template": ".".join(d["id"] for d in key_dims) or "ALL",
        "n_key_dimensions": len(key_dims),
        "note": (
            "DataBrowser2 API (databrowser2.tuik.gov.tr) — yalnızca key='ALL' "
            "destekleniyor; dot-key daraltma henüz yok."
        ),
    }


# ---------------------------------------------------------------------------
# Legacy fallback: TUIK SDMX 2.1 REST (nsiws.tuik.gov.tr)
# ---------------------------------------------------------------------------

def build_data_url(
    dataflow_id: str,
    key: str = "ALL",
    start: str | None = None,
    end: str | None = None,
) -> str:
    validate_dataflow_id(dataflow_id)
    if not key:
        raise ValueError("key must not be empty (use 'ALL' for everything)")
    query = ["detail=full", "dimensionAtObservation=TIME_PERIOD"]
    if start:
        query.append(f"startPeriod={start}")
    if end:
        query.append(f"endPeriod={end}")
    return f"{NSIWS_BASE}/data/{dataflow_id}/{key}/?" + "&".join(query)


def build_structure_url(dataflow_id: str) -> str:
    agency, flow, version = validate_dataflow_id(dataflow_id).split(",")
    return (
        f"{NSIWS_BASE}/dataflow/{agency}/{flow}/{version}"
        "?detail=Full&references=Descendants"
    )


def _legacy_fetch_data(
    dataflow_id: str,
    key: str = "ALL",
    start: str | None = None,
    end: str | None = None,
    timeout: float = 120.0,
) -> pd.DataFrame:
    url = build_data_url(dataflow_id, key=key, start=start, end=end)
    client = httpx.Client(timeout=timeout, follow_redirects=True, headers=_BASE_HEADERS)
    try:
        response = client.get(url, headers={"Accept": _CSV_ACCEPT})
        if response.status_code == 200:
            content_type = response.headers.get("content-type", "")
            if "csv" in content_type or "text/plain" in content_type:
                try:
                    return pd.read_csv(io.StringIO(response.text))
                except Exception:  # noqa: BLE001 - fall through to XML
                    pass
        elif response.status_code == 404:
            raise SdmxError(
                f"No data found for '{dataflow_id}' with key '{key}'. "
                "The key may be too restrictive or the dataflow id wrong."
            )

        response = client.get(url, headers={"Accept": _XML_DATA_ACCEPT})
        if response.status_code != 200:
            raise SdmxError(
                f"TUIK SDMX service returned HTTP {response.status_code} for {url}"
            )
        return _parse_generic_data(response.content)
    finally:
        client.close()


def _legacy_fetch_structure(dataflow_id: str, lang: str = "tr", timeout: float = 120.0) -> dict[str, Any]:
    url = build_structure_url(dataflow_id)
    client = httpx.Client(timeout=timeout, follow_redirects=True, headers=_BASE_HEADERS)
    try:
        response = client.get(url, headers={"Accept": _XML_STRUCTURE_ACCEPT})
    finally:
        client.close()
    if response.status_code != 200:
        raise SdmxError(f"TUIK SDMX service returned HTTP {response.status_code} for {url}")
    return _parse_structure(response.content, lang=lang)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _iter_local(root: ET.Element, name: str):
    for element in root.iter():
        if _local(element.tag) == name:
            yield element


def _parse_generic_data(content: bytes) -> pd.DataFrame:
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise SdmxError("Could not parse SDMX-ML response from TUIK.") from exc

    rows: list[dict[str, Any]] = []
    for series in _iter_local(root, "Series"):
        series_key: dict[str, Any] = {}
        for child in series:
            if _local(child.tag) in ("SeriesKey", "Attributes"):
                for value in child:
                    if _local(value.tag) == "Value":
                        series_key[value.get("id", "")] = value.get("value")
        for obs in series:
            if _local(obs.tag) != "Obs":
                continue
            row = dict(series_key)
            for part in obs:
                part_name = _local(part.tag)
                if part_name == "ObsDimension":
                    row["TIME_PERIOD"] = part.get("value")
                elif part_name == "ObsValue":
                    row["OBS_VALUE"] = part.get("value")
                elif part_name == "Attributes":
                    for value in part:
                        if _local(value.tag) == "Value":
                            row[value.get("id", "")] = value.get("value")
            rows.append(row)

    if not rows:
        for obs in _iter_local(root, "Obs"):
            row: dict[str, Any] = {}
            for part in obs:
                part_name = _local(part.tag)
                if part_name in ("ObsKey", "Attributes"):
                    for value in part:
                        if _local(value.tag) == "Value":
                            row[value.get("id", "")] = value.get("value")
                elif part_name == "ObsDimension":
                    row["TIME_PERIOD"] = part.get("value")
                elif part_name == "ObsValue":
                    row["OBS_VALUE"] = part.get("value")
            if row:
                rows.append(row)

    if not rows:
        raise SdmxError(
            "TUIK returned an SDMX document with no observations "
            "(empty result for this key/period)."
        )

    frame = pd.DataFrame(rows)
    if "OBS_VALUE" in frame.columns:
        frame["OBS_VALUE"] = pd.to_numeric(frame["OBS_VALUE"], errors="coerce")
    return frame


def _pick_localized_name(element: ET.Element, lang: str) -> str | None:
    fallback: str | None = None
    for child in element:
        if _local(child.tag) != "Name":
            continue
        text = (child.text or "").strip()
        if not text:
            continue
        xml_lang = child.get("{http://www.w3.org/XML/1998/namespace}lang")
        if xml_lang == lang:
            return text
        if fallback is None:
            fallback = text
    return fallback


def _parse_structure(content: bytes, lang: str = "tr") -> dict[str, Any]:
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise SdmxError("Could not parse SDMX structure response from TUIK.") from exc

    codelists: dict[str, dict[str, str]] = {}
    for codelist in _iter_local(root, "Codelist"):
        codelist_id = codelist.get("id")
        if not codelist_id:
            continue
        codes: dict[str, str] = {}
        for code in codelist:
            if _local(code.tag) != "Code":
                continue
            code_id = code.get("id")
            if code_id is None:
                continue
            codes[code_id] = _pick_localized_name(code, lang) or code_id
        codelists[codelist_id] = codes

    dimensions: list[dict[str, Any]] = []
    for dim_list in _iter_local(root, "DimensionList"):
        for dim in dim_list:
            dim_name = _local(dim.tag)
            if dim_name not in ("Dimension", "TimeDimension"):
                continue
            codelist_ref: str | None = None
            for enum in _iter_local(dim, "Enumeration"):
                for ref in enum:
                    if _local(ref.tag) == "Ref":
                        codelist_ref = ref.get("id")
            dimensions.append(
                {
                    "id": dim.get("id"),
                    "position": int(dim.get("position", "0")),
                    "is_time": dim_name == "TimeDimension",
                    "codelist_id": codelist_ref,
                    "codes": codelists.get(codelist_ref, {}) if codelist_ref else {},
                }
            )
        break

    dimensions.sort(key=lambda d: d["position"])
    key_dimensions = [d for d in dimensions if not d["is_time"]]
    key_template = ".".join(d["id"] or "?" for d in key_dimensions)

    return {
        "dimensions": dimensions,
        "key_template": key_template,
        "n_key_dimensions": len(key_dimensions),
    }


# ---------------------------------------------------------------------------
# Public API (unchanged signatures -- server.py calls these two functions)
# ---------------------------------------------------------------------------

def fetch_data(
    dataflow_id: str,
    key: str = "ALL",
    start: str | None = None,
    end: str | None = None,
    timeout: float = 120.0,
) -> pd.DataFrame:
    """Fetch observations as a tidy DataFrame.

    Tries TUIK's DataBrowser2 backend first (JSON-stat, no auth issue as of
    2026-08); falls back to the legacy nsiws SDMX 2.1 path if that fails,
    in case some dataflow only exists there.
    """
    try:
        return _databrowser_fetch_data(dataflow_id, key=key, start=start, end=end, timeout=timeout)
    except SdmxError:
        return _legacy_fetch_data(dataflow_id, key=key, start=start, end=end, timeout=timeout)


def fetch_structure(dataflow_id: str, lang: str = "tr", timeout: float = 120.0) -> dict[str, Any]:
    """Describe a dataflow's dimensions and codelists (DataBrowser2 first)."""
    try:
        return _databrowser_fetch_structure(dataflow_id, lang=lang, timeout=timeout)
    except SdmxError:
        return _legacy_fetch_structure(dataflow_id, lang=lang, timeout=timeout)
