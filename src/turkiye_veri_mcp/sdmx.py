"""Client for TUIK's SDMX 2.1 REST web service (nsiws.tuik.gov.tr).

Data retrieval strategy:
1. Ask for SDMX-CSV (supported by .Stat Suite NSI services) -> pandas directly.
2. If the service refuses CSV, fall back to SDMX-ML GenericData and parse it
   with a namespace-agnostic ElementTree walker (the generic format is
   self-describing, so no DSD is needed to read it).

Structure queries (dataflow?references=Descendants) return the data structure
definition plus codelists, used to describe dimensions and build SDMX keys.
"""

from __future__ import annotations

import io
import xml.etree.ElementTree as ET
from typing import Any

import httpx
import pandas as pd

from turkiye_veri_mcp.portal import _UA

NSIWS_BASE = "https://nsiws.tuik.gov.tr/rest"

_CSV_ACCEPT = "application/vnd.sdmx.data+csv; version=1.0.0; labels=both"
_XML_DATA_ACCEPT = "application/vnd.sdmx.genericdata+xml; version=2.1"
_XML_STRUCTURE_ACCEPT = "application/vnd.sdmx.structure+xml; version=2.1"
_BASE_HEADERS = {"User-Agent": _UA, "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8"}


class SdmxError(RuntimeError):
    """Raised when the TUIK SDMX service returns an error or unparseable data."""


def validate_dataflow_id(dataflow_id: str) -> str:
    parts = dataflow_id.split(",")
    if len(parts) != 3 or not all(parts):
        raise ValueError(
            "dataflow_id must have three comma-separated parts, "
            "e.g. 'TR,DF_ADNKS_T26,1.0'"
        )
    return dataflow_id


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


def _primed_client(timeout: float) -> httpx.Client:
    """Open an httpx.Client that carries a veriportali session cookie.

    Unverified hypothesis: nsiws.tuik.gov.tr may reject requests with no
    prior veriportali session, the same way its JSON catalog API does
    (portal.py already primes a cookie for that). A cookie-carrying client
    costs nothing extra if this theory is wrong -- the request just goes
    out with an unused cookie -- so it's a safe thing to always do.
    """
    from turkiye_veri_mcp.portal import PORTAL_BASE

    client = httpx.Client(timeout=timeout, follow_redirects=True, headers=_BASE_HEADERS)
    try:
        client.get(f"{PORTAL_BASE}/tr/statistical-themes")
    except httpx.HTTPError:
        pass  # priming is best-effort; proceed with the SDMX call regardless
    return client


def fetch_data(
    dataflow_id: str,
    key: str = "ALL",
    start: str | None = None,
    end: str | None = None,
    timeout: float = 120.0,
) -> pd.DataFrame:
    """Fetch observations as a tidy DataFrame (CSV first, XML fallback)."""
    url = build_data_url(dataflow_id, key=key, start=start, end=end)

    client = _primed_client(timeout)
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


def fetch_structure(dataflow_id: str, lang: str = "tr", timeout: float = 120.0) -> dict[str, Any]:
    """Fetch dimensions and codelists for a dataflow."""
    url = build_structure_url(dataflow_id)
    client = _primed_client(timeout)
    try:
        response = client.get(url, headers={"Accept": _XML_STRUCTURE_ACCEPT})
    finally:
        client.close()
    if response.status_code != 200:
        raise SdmxError(
            f"TUIK SDMX service returned HTTP {response.status_code} for {url}"
        )
    return _parse_structure(response.content, lang=lang)


# ---------------------------------------------------------------------------
# XML parsing helpers (namespace-agnostic on purpose: TUIK's NSI service may
# serve different namespace prefixes/versions over time).
# ---------------------------------------------------------------------------

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
        # Flat (non-series) generic data: Obs directly under DataSet.
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
        break  # first DimensionList belongs to the DSD we asked for

    dimensions.sort(key=lambda d: d["position"])
    key_dimensions = [d for d in dimensions if not d["is_time"]]
    key_template = ".".join(d["id"] or "?" for d in key_dimensions)

    return {
        "dimensions": dimensions,
        "key_template": key_template,
        "n_key_dimensions": len(key_dimensions),
    }
