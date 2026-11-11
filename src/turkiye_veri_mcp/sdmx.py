"""Client for TUIK's SDMX dataflows via the DataBrowser2 backend.

TUIK's public data browser (https://databrowser2.tuik.gov.tr, an Istat
"Data Browser" deployment) is the only working programmatic path to TUIK's
SDMX dataflows: the legacy SDMX 2.1 REST service nsiws.tuik.gov.tr answers
HTTP 401 to every request and is no longer used here.

The endpoints below are exactly the ones TUIK's own web UI calls (checked
against the UI's network traffic and its JS bundle, 2026-11). All live under
``/api/core/nodes/1/datasets/{AGENCY,ID,VERSION}``:

  GET  /structure                -> the UI view: filterable dimensions
                                    ("criteria"), default selection, title.
  POST /download/structure       -> the full DSD as SDMX-JSON: every
                                    dimension in key order, concept names and
                                    codelists, labels in both tr and en.
  POST /PartialCodelists/{DIM}   -> codes of DIM that actually have data
                                    under the posted selection, plus
                                    ``obsCount`` (observations matching it).
                                    For the time dimension: first/last date.
  POST /download/csv             -> SDMX-CSV of the posted selection: one row
                                    per observation, every DSD dimension as a
                                    code column, plus OBS_VALUE and the
                                    observation attributes (OBS_STATUS,
                                    CONF_STATUS, UNIT_MULT, ...).

The POST body is a list of criteria::

    [{"id": "SEX", "type": "CodeValues", "filterValues": ["1", "2"]},
     {"id": "TIME_PERIOD", "type": "TimeRange",
      "from": "2020-01-01", "to": "2024-12-31"}]

A dimension that is not mentioned is not filtered. A TimeRange keeps every
period that overlaps [from, to]. The ``UserLang`` header (tr/en) picks label
language; codes are always language independent. Two server behaviours this
module guards against, because they would otherwise produce silently wrong
results:

  * an unknown dimension id in the criteria is silently ignored by the
    server (returns *everything*), so dimension ids are validated against
    the DSD before sending;
  * with ``UserLang: en`` PartialCodelists silently drops codes that have no
    English name, so available codes are always fetched in Turkish and only
    the labels are translated (from the DSD, which carries both languages).

The JSON-stat ``/data`` endpoint the UI uses for its table is deliberately
not used: it drops the time dimension when a single period is selected and
does not carry observation attributes. Every frame returned by
:func:`fetch_data` is checked against the server's own ``obsCount`` for the
same selection; a mismatch raises instead of returning partial data.
"""

from __future__ import annotations

import calendar
import io
import json
import re
import threading
import time
from datetime import date
from typing import Any

import httpx
import pandas as pd

from turkiye_veri_mcp.portal import _UA

DATABROWSER_ROOT = "https://databrowser2.tuik.gov.tr"
DATASETS_BASE = f"{DATABROWSER_ROOT}/api/core/nodes/1/datasets"

#: SDMX code for "not applicable" -- a dimension whose only code is this one
#: is not used by the dataflow (shared DSDs carry many such dimensions).
NOT_APPLICABLE = "_Z"

#: Refuse a single request above this many observations (ask for filters).
#: ~0.8 kB of RAM per observation at peak, so small hosts (e.g. a 512 MB
#: free tier) should lower it with the TUIK_MAX_OBSERVATIONS env var.
def _max_observations_default() -> int:
    import os

    try:
        return max(1, int(os.environ.get("TUIK_MAX_OBSERVATIONS", "2000000")))
    except ValueError:
        return 2_000_000


MAX_OBSERVATIONS = _max_observations_default()

_DESCRIBE_WORKERS = 4

_CACHE_TTL = 3600.0
_CACHE_MAX = 128
_cache: dict[tuple, tuple[float, Any]] = {}
_cache_lock = threading.Lock()


class SdmxError(RuntimeError):
    """Raised when TUIK cannot serve the requested dataflow/selection."""


# ---------------------------------------------------------------------------
# ids, caching, HTTP
# ---------------------------------------------------------------------------

_ID_RE = re.compile(r"^[A-Za-z0-9_@.\-]+$")


def validate_dataflow_id(dataflow_id: str) -> str:
    """Normalise a dataflow id to 'AGENCY,ID,VERSION'.

    Accepts the canonical form, the '+'-separated form used inside DSD
    responses, and a pasted DataBrowser2 URL ('.../#/tr/tuik/TR,DF_X,1.0').
    """
    text = (dataflow_id or "").strip()
    if "/" in text:
        text = text.rstrip("/").rsplit("/", 1)[-1]
    text = text.replace("+", ",")
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 3 or not all(parts) or not all(_ID_RE.match(p) for p in parts):
        raise ValueError(
            "dataflow_id must have three comma-separated parts "
            "(AGENCY,ID,VERSION), e.g. 'TR,DF_TUFE_SDMX_TT10,1.0' -- take it "
            "from the dataflow_id field of tuik_search_tables/tuik_list_tables."
        )
    return ",".join(parts)


def _cache_get(key: tuple) -> Any:
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < _CACHE_TTL:
            return hit[1]
    return None


def _cache_put(key: tuple, value: Any) -> None:
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            oldest = min(_cache, key=lambda k: _cache[k][0])
            _cache.pop(oldest, None)
        _cache[key] = (time.monotonic(), value)


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _headers(lang: str) -> dict[str, str]:
    return {
        "User-Agent": _UA,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "UserLang": lang,
        "Referer": f"{DATABROWSER_ROOT}/",
    }


def _error_from_response(response: httpx.Response, dataflow_id: str, what: str) -> SdmxError:
    code, message = "", ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            code = str(payload.get("errorCode") or "")
            message = str(payload.get("message") or "")
            if not code and payload.get("errors"):
                message = json.dumps(payload["errors"], ensure_ascii=False)[:300]
    except ValueError:
        message = response.text[:200]
    if code == "DATAFLOW_NOT_FOUND":
        return SdmxError(
            f"Dataflow '{dataflow_id}' TÜİK DataBrowser2'de bulunamadı "
            f"(HTTP {response.status_code} DATAFLOW_NOT_FOUND). Kaldırılmış ya "
            "da sürümü değişmiş olabilir; güncel id için tuik_search_tables "
            "veya tuik_list_tables kullanın."
        )
    detail = " ".join(x for x in (code, message) if x).strip()
    return SdmxError(
        f"TÜİK DataBrowser2 {what} isteğine HTTP {response.status_code} döndü"
        f" ({dataflow_id})" + (f": {detail}" if detail else "")
    )


def _request(
    method: str,
    dataflow_id: str,
    path: str,
    *,
    body: Any = None,
    lang: str = "tr",
    timeout: float = 60.0,
    what: str = "",
) -> httpx.Response:
    url = f"{DATASETS_BASE}/{dataflow_id}/{path}"
    what = what or path
    content = None if method == "GET" else json.dumps(body if body is not None else [])
    request_timeout = httpx.Timeout(timeout, connect=min(15.0, timeout))
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    response: httpx.Response | None = None
    # TUIK's front end resets connections under bursts of fresh TLS
    # handshakes; a shared keep-alive client plus a short retry on
    # connection-level errors (never on timeouts) handles that.
    for attempt, pause in enumerate((0.0, 1.5, 4.0)):
        if pause:
            if time.monotonic() + pause >= deadline:
                break
            time.sleep(pause)
        try:
            response = _client().request(
                method, url, content=content, headers=_headers(lang), timeout=request_timeout
            )
        except httpx.TimeoutException as exc:
            raise SdmxError(
                f"TÜİK DataBrowser2 {timeout:.0f} sn içinde yanıt vermedi ({what}, "
                f"{dataflow_id}). Daha dar bir seçim (filters/start/end) deneyin "
                "veya daha sonra tekrar deneyin."
            ) from exc
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError, httpx.WriteError) as exc:
            last_exc, response = exc, None
            _reset_client()
            continue
        except httpx.HTTPError as exc:
            last_exc, response = exc, None
            break
        if response.status_code in (429, 502, 503, 504):
            continue
        break
    if response is None:
        raise SdmxError(
            f"TÜİK DataBrowser2'ye bağlanılamadı ({what}, {dataflow_id}): "
            f"{type(last_exc).__name__}: {last_exc}"
        ) from last_exc
    if response.status_code != 200:
        raise _error_from_response(response, dataflow_id, what)
    return response


_client_obj: httpx.Client | None = None
_client_lock = threading.Lock()


def _client() -> httpx.Client:
    global _client_obj
    with _client_lock:
        if _client_obj is None:
            _client_obj = httpx.Client(follow_redirects=True)
        return _client_obj


def _reset_client() -> None:
    global _client_obj
    with _client_lock:
        if _client_obj is not None:
            try:
                _client_obj.close()
            except Exception:  # noqa: BLE001
                pass
        _client_obj = None


def _json(response: httpx.Response, dataflow_id: str, what: str) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise SdmxError(
            f"TÜİK DataBrowser2 {what} yanıtı JSON değil ({dataflow_id}); "
            "API değişmiş olabilir."
        ) from exc


# ---------------------------------------------------------------------------
# Structure: the DSD (all dimensions, codelists, tr/en labels)
# ---------------------------------------------------------------------------

def _urn_ref(urn: str) -> str:
    """'urn:...Codelist=TR:CL_X(1.2)' -> 'TR:CL_X(1.2)'."""
    return urn.rsplit("=", 1)[-1] if urn else ""


def _names(obj: dict[str, Any]) -> dict[str, str]:
    names = dict(obj.get("names") or {})
    if obj.get("name") and "tr" not in names:
        names.setdefault("_", obj["name"])
    return names


def pick_label(names: dict[str, str] | None, lang: str, fallback: str) -> str:
    """Label in `lang`, else Turkish, else any, else `fallback`."""
    if not names:
        return fallback
    for key in (lang, "tr", "_", "en"):
        if names.get(key):
            return names[key]
    return next((v for v in names.values() if v), fallback)


def parse_dsd_json(payload: dict[str, Any], dataflow_id: str) -> dict[str, Any]:
    """Parse the SDMX-JSON structure message from /download/structure."""
    data = payload.get("data") or {}
    agency, flow_id, version = dataflow_id.split(",")
    flows = data.get("dataflows") or []
    flow = next(
        (f for f in flows if f.get("id") == flow_id and str(f.get("version")) == version),
        flows[0] if flows else {},
    )
    structures = data.get("dataStructures") or []
    if not structures:
        raise SdmxError(f"DSD yanıtında dataStructures yok ({dataflow_id}).")
    dsd_ref = _urn_ref(str(flow.get("structure") or ""))
    dsd = next(
        (
            s for s in structures
            if f"{s.get('agencyID')}:{s.get('id')}({s.get('version')})" == dsd_ref
        ),
        structures[0],
    )

    codelists: dict[str, dict[str, Any]] = {}
    for cl in data.get("codelists") or []:
        ref = f"{cl.get('agencyID')}:{cl.get('id')}({cl.get('version')})"
        codes: dict[str, dict[str, str]] = {}
        parents: dict[str, str] = {}
        for code in cl.get("codes") or []:
            cid = str(code.get("id"))
            codes[cid] = _names(code)
            if code.get("parent"):
                parents[cid] = str(code["parent"])
        codelists[ref] = {"codes": codes, "parents": parents}

    concepts: dict[str, dict[str, str]] = {}
    for scheme in data.get("conceptSchemes") or []:
        prefix = f"{scheme.get('agencyID')}:{scheme.get('id')}({scheme.get('version')})"
        for concept in scheme.get("concepts") or []:
            concepts[f"{prefix}.{concept.get('id')}"] = _names(concept)

    def component(comp: dict[str, Any], kind: str) -> dict[str, Any]:
        rep = comp.get("localRepresentation") or {}
        cl_ref = _urn_ref(str(rep.get("enumeration") or ""))
        cl = codelists.get(cl_ref) or {"codes": {}, "parents": {}}
        return {
            "id": str(comp.get("id")),
            "kind": kind,
            "position": comp.get("position"),
            "label": concepts.get(_urn_ref(str(comp.get("conceptIdentity") or "")), {}),
            "codelist": cl_ref or None,
            "codes": cl["codes"],
            "parents": cl["parents"],
        }

    comps = dsd.get("dataStructureComponents") or {}
    dim_list = comps.get("dimensionList") or {}
    dims = [component(d, "dimension") for d in dim_list.get("dimensions") or []]
    dims.sort(key=lambda d: (d["position"] is None, d["position"] or 0))
    time_dims = [component(d, "time") for d in dim_list.get("timeDimensions") or []]
    attributes = [
        component(a, "attribute")
        for a in (comps.get("attributeList") or {}).get("attributes") or []
    ]
    return {
        "dataflow_id": dataflow_id,
        "dsd": dsd_ref,
        "name": _names(flow),
        "description": dict(flow.get("descriptions") or {}),
        "dimensions": dims,
        "time_dimension": time_dims[0]["id"] if time_dims else None,
        "attributes": attributes,
    }


def fetch_dsd(dataflow_id: str, timeout: float = 60.0) -> dict[str, Any]:
    dataflow_id = validate_dataflow_id(dataflow_id)
    key = ("dsd", dataflow_id)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    response = _request(
        "POST", dataflow_id, "download/structure", body=[], timeout=timeout,
        what="yapı (DSD)",
    )
    parsed = parse_dsd_json(_json(response, dataflow_id, "yapı (DSD)"), dataflow_id)
    _cache_put(key, parsed)
    return parsed


def fetch_view(dataflow_id: str, lang: str = "tr", timeout: float = 60.0) -> dict[str, Any]:
    """The UI's view of a dataflow (filterable dimensions, title)."""
    dataflow_id = validate_dataflow_id(dataflow_id)
    key = ("view", dataflow_id, lang)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    payload = _json(
        _request("GET", dataflow_id, "structure", lang=lang, timeout=timeout, what="structure"),
        dataflow_id, "structure",
    )
    if not isinstance(payload, dict) or "criteria" not in payload:
        raise SdmxError(f"Beklenmeyen /structure yanıtı ({dataflow_id}).")
    _cache_put(key, payload)
    return payload


def fetch_available_codes(
    dataflow_id: str,
    dimension: str,
    criteria: list[dict[str, Any]] | None = None,
    timeout: float = 60.0,
) -> tuple[list[dict[str, Any]], int | None]:
    """Codes of `dimension` that have data under `criteria` (+ obsCount).

    Always queried in Turkish: with UserLang=en the server drops codes that
    lack an English name.
    """
    dataflow_id = validate_dataflow_id(dataflow_id)
    body = [c for c in (criteria or []) if c.get("id") != dimension]
    key = ("pcl", dataflow_id, dimension, json.dumps(body, sort_keys=True))
    cached = _cache_get(key)
    if cached is not None:
        return cached
    payload = _json(
        _request(
            "POST", dataflow_id, f"PartialCodelists/{dimension}", body=body,
            lang="tr", timeout=timeout, what=f"kod listesi {dimension}",
        ),
        dataflow_id, f"kod listesi {dimension}",
    )
    values: list[dict[str, Any]] = []
    for crit in payload.get("criteria") or []:
        if crit.get("id") == dimension:
            values = list(crit.get("values") or [])
    obs = payload.get("obsCount")
    result = (values, int(obs) if isinstance(obs, (int, float)) else None)
    _cache_put(key, result)
    return result


# ---------------------------------------------------------------------------
# Periods
# ---------------------------------------------------------------------------

_PERIOD_RES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^(\d{4})$"), "A"),
    (re.compile(r"^(\d{4})-A1$"), "A"),
    (re.compile(r"^(\d{4})-(\d{1,2})$"), "M"),
    (re.compile(r"^(\d{4})-M(\d{1,2})$"), "M"),
    (re.compile(r"^(\d{4})-(\d{2})-(\d{2})$"), "D"),
    (re.compile(r"^(\d{4})-?Q([1-4])$", re.I), "Q"),
    (re.compile(r"^(\d{4})-?[SH]([12])$", re.I), "S"),
)


def period_bounds(text: str) -> tuple[date, date] | None:
    """First and last day of an SDMX-style period, or None if unrecognised.

    Handles 2024, 2024-A1, 2024-03, 2024-M03, 2024-03-15, 2024-Q2, 2024Q2,
    2024-S1/2024-H1.
    """
    value = str(text or "").strip()
    for pattern, kind in _PERIOD_RES:
        match = pattern.match(value)
        if not match:
            continue
        year = int(match.group(1))
        try:
            if kind == "A":
                return date(year, 1, 1), date(year, 12, 31)
            if kind == "M":
                month = int(match.group(2))
                return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
            if kind == "D":
                day = date(year, int(match.group(2)), int(match.group(3)))
                return day, day
            if kind == "Q":
                q = int(match.group(2))
                first, last = 3 * q - 2, 3 * q
                return date(year, first, 1), date(year, last, calendar.monthrange(year, last)[1])
            if kind == "S":
                s = int(match.group(2))
                return (date(year, 1, 1), date(year, 6, 30)) if s == 1 else (date(year, 7, 1), date(year, 12, 31))
        except ValueError:
            return None
    return None


def _time_criterion(time_dim: str, start: str | None, end: str | None) -> dict[str, Any] | None:
    if not start and not end:
        return None
    criterion: dict[str, Any] = {"id": time_dim, "type": "TimeRange", "from": None, "to": None}
    for value, slot, idx in ((start, "from", 0), (end, "to", 1)):
        if not value:
            continue
        bounds = period_bounds(value)
        if bounds is None:
            raise SdmxError(
                f"Dönem anlaşılamadı: '{value}'. Kabul edilen biçimler: 2024, "
                "2024-03, 2024-Q2, 2024-S1, 2024-03-15."
            )
        criterion[slot] = bounds[idx].isoformat()
    if criterion["from"] and criterion["to"] and criterion["from"] > criterion["to"]:
        raise SdmxError(f"start ({start}) end'den ({end}) sonra olamaz.")
    return criterion


# ---------------------------------------------------------------------------
# Selection: key / filters -> criteria
# ---------------------------------------------------------------------------

def parse_filters(filters: str | dict[str, Any] | None) -> dict[str, list[str]]:
    """Parse filters given as a dict, a JSON object string, or
    'DIM=code1+code2;DIM2=code' (',' also accepted between codes)."""
    if not filters:
        return {}
    if isinstance(filters, dict):
        raw = filters
    else:
        text = filters.strip()
        if text.startswith("{"):
            try:
                raw = json.loads(text)
            except ValueError as exc:
                raise SdmxError(f"filters JSON olarak çözümlenemedi: {exc}") from exc
        else:
            raw = {}
            for part in re.split(r"[;&\n]", text):
                part = part.strip()
                if not part:
                    continue
                if "=" not in part:
                    raise SdmxError(
                        f"filters parçası '{part}' DIM=KOD biçiminde değil. "
                        "Örnek: 'SEX=1+2;ISGUCU_GOSTERGE=8'."
                    )
                dim, codes = part.split("=", 1)
                raw[dim.strip()] = codes
    result: dict[str, list[str]] = {}
    for dim, codes in raw.items():
        if isinstance(codes, str):
            items = [c.strip() for c in re.split(r"[+,]", codes)]
        else:
            items = [str(c).strip() for c in codes]
        items = [c for c in items if c]
        if items:
            result.setdefault(str(dim).strip().upper(), [])
            result[str(dim).strip().upper()].extend(i for i in items if i not in result[str(dim).strip().upper()])
    return result


def parse_key(key: str, dimension_ids: list[str]) -> dict[str, list[str]]:
    """SDMX dot key (DSD dimension order, '+' = OR, empty = all)."""
    key = (key or "").strip()
    if key in ("", "ALL", "all", "*"):
        return {}
    slots = key.split(".")
    if len(slots) != len(dimension_ids):
        raise SdmxError(
            f"key {len(slots)} parçadan oluşuyor ama bu dataflow'un "
            f"{len(dimension_ids)} boyutu var (sıra: {'.'.join(dimension_ids)}). "
            "Daha kolayı: key='ALL' bırakıp filters='BOYUT=KOD;...' kullanın."
        )
    result: dict[str, list[str]] = {}
    for dim, slot in zip(dimension_ids, slots):
        codes = [c for c in slot.split("+") if c]
        if codes:
            result[dim] = codes
    return result


def build_criteria(
    dsd: dict[str, Any],
    key: str = "ALL",
    filters: str | dict[str, Any] | None = None,
    start: str | None = None,
    end: str | None = None,
) -> list[dict[str, Any]]:
    """Validated DataBrowser2 criteria for a key/filters/period selection."""
    dims = {d["id"]: d for d in dsd["dimensions"]}
    order = [d["id"] for d in dsd["dimensions"]]
    selection = parse_key(key, order)
    for dim, codes in parse_filters(filters).items():
        if dim in selection and set(selection[dim]) != set(codes):
            raise SdmxError(f"{dim} hem key'de hem filters'ta farklı değerlerle verilmiş.")
        selection[dim] = codes

    time_dim = dsd.get("time_dimension") or "TIME_PERIOD"
    if time_dim in selection:
        raise SdmxError(
            f"Dönem filtresi için {time_dim} yerine start/end parametrelerini kullanın."
        )
    criteria: list[dict[str, Any]] = []
    for dim, codes in selection.items():
        if dim not in dims:
            raise SdmxError(
                f"'{dim}' bu dataflow'da bir boyut değil (sunucu bilinmeyen "
                f"boyutu sessizce yok sayar, bu yüzden reddedildi). Geçerli "
                f"boyutlar: {', '.join(order)}."
            )
        known = dims[dim]["codes"]
        if known:
            unknown = [c for c in codes if c not in known]
            if unknown:
                sample = ", ".join(list(known)[:15])
                raise SdmxError(
                    f"{dim} için geçersiz kod(lar): {', '.join(unknown)}. "
                    f"Kod listesinden örnekler: {sample}... "
                    "(tuik_describe_dataflow ile verisi olan kodları görün)."
                )
        criteria.append({"id": dim, "type": "CodeValues", "filterValues": codes, "period": 0})
    time_crit = _time_criterion(time_dim, start, end)
    if time_crit:
        criteria.append(time_crit)
    return criteria


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def _label_map(component: dict[str, Any], lang: str) -> dict[str, str]:
    return {code: pick_label(names, lang, code) for code, names in component["codes"].items()}


def sdmx_csv_to_frame(
    text: str | bytes,
    dsd: dict[str, Any],
    lang: str = "tr",
    labels: bool = True,
) -> pd.DataFrame:
    """Tidy an SDMX-CSV payload: code + label columns, numeric 'deger'.

    Columns: for each dimension used by the result (dimensions whose only
    value is '_Z' = not applicable are dropped) the code column (SDMX id)
    and, if labels, '<DIM>_label'; then the time column, 'deger' (numeric
    OBS_VALUE) and any non-empty observation attributes.
    """
    raw = text.encode("utf-8") if isinstance(text, str) else text
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    # Every column is read as text: codes such as '01' or 'NA' must stay
    # exactly as sent (dtype='category' in read_csv would infer integer
    # categories and turn '01' into 1; NA sniffing would turn 'NA' into NaN).
    frame = pd.read_csv(
        io.BytesIO(raw), dtype=str, keep_default_na=False, na_values=[""],
        encoding="utf-8",
    )
    if "OBS_VALUE" not in frame.columns:
        raise SdmxError("SDMX-CSV yanıtında OBS_VALUE sütunu yok; API değişmiş olabilir.")
    frame = frame.drop(columns=[c for c in ("DATAFLOW", "STRUCTURE", "STRUCTURE_ID", "ACTION") if c in frame.columns])

    dims = [d for d in dsd["dimensions"] if d["id"] in frame.columns]
    time_dim = dsd.get("time_dimension") or "TIME_PERIOD"
    attrs = [a for a in dsd["attributes"] if a["id"] in frame.columns]
    known = {d["id"] for d in dims} | {a["id"] for a in attrs} | {time_dim, "OBS_VALUE"}

    out = pd.DataFrame(index=frame.index)
    for dim in dims:
        column = frame[dim["id"]]
        if len(frame) and set(column.dropna().unique()) <= {NOT_APPLICABLE}:
            continue
        out[dim["id"]] = column
        if labels:
            mapping = _label_map(dim, lang)
            out[f"{dim['id']}_label"] = column.map(lambda c, m=mapping: m.get(c, c) if isinstance(c, str) else c)
    # Columns the DSD did not describe (should not happen) are kept verbatim.
    for extra in [c for c in frame.columns if c not in known]:
        out[extra] = frame[extra]
    if time_dim in frame.columns:
        out[time_dim] = frame[time_dim]

    raw_values = frame["OBS_VALUE"]
    numeric = pd.to_numeric(raw_values, errors="coerce")
    out["deger"] = numeric
    unparsed = raw_values.notna() & numeric.isna()
    if unparsed.any():
        # Never coerce silently: keep what TUIK sent next to the NaN.
        out["deger_ham"] = raw_values.where(unparsed)
    for attr in attrs:
        column = frame[attr["id"]]
        if column.isna().all():
            continue
        out[attr["id"]] = column
        if labels and attr["codes"]:
            mapping = _label_map(attr, lang)
            out[f"{attr['id']}_label"] = column.map(lambda c, m=mapping: m.get(c, c) if isinstance(c, str) else c)
    # Code/label columns repeat a handful of values over many rows; storing
    # them as categoricals cuts memory several-fold on million-row flows.
    for column in out.columns:
        if column not in ("deger", "deger_ham"):
            out[column] = out[column].astype("category")
    return out.reset_index(drop=True)


def filter_periods(frame: pd.DataFrame, time_dim: str, start: str | None, end: str | None) -> pd.DataFrame:
    """Keep rows whose period overlaps [start, end] (ISO dates).

    Raises instead of guessing when a period cannot be parsed.
    """
    lo = date.fromisoformat(start) if start else date.min
    hi = date.fromisoformat(end) if end else date.max
    periods = frame[time_dim].astype(str)
    bounds = {p: period_bounds(p) for p in periods.unique()}
    unknown = [p for p, b in bounds.items() if b is None]
    if unknown:
        raise SdmxError(
            f"Sunucu bu dataflow için dönem filtresi uygulamıyor ve dönem "
            f"biçimi tanınmadı ({', '.join(unknown[:5])}); start/end olmadan "
            "indirip kendiniz süzün."
        )
    keep = {p for p, (first, last) in bounds.items() if last >= lo and first <= hi}
    return frame[periods.isin(keep)].reset_index(drop=True)


def count_observations(
    dataflow_id: str, criteria: list[dict[str, Any]], dsd: dict[str, Any], timeout: float = 60.0
) -> int | None:
    """Server-side observation count for a selection (no data download)."""
    time_dim = dsd.get("time_dimension") or "TIME_PERIOD"
    # Ask for the time codelist *without* removing our own time criterion:
    # PartialCodelists drops the queried dimension from the body, so query
    # another dimension when a time filter is present.
    probe_dim = time_dim
    if any(c.get("id") == time_dim for c in criteria):
        others = [d["id"] for d in dsd["dimensions"] if not any(c.get("id") == d["id"] for c in criteria)]
        if not others:
            return None
        probe_dim = others[0]
    values, obs = fetch_available_codes(dataflow_id, probe_dim, criteria, timeout=timeout)
    if obs is None and not values:
        # No codes and no count: the server holds no observations for this
        # selection (seen for whole dataflows, e.g. DF_UGOC_T1, whose
        # /download/csv then answers 404 "Dataflow not found").
        return 0
    return obs


def fetch_data(
    dataflow_id: str,
    key: str = "ALL",
    start: str | None = None,
    end: str | None = None,
    timeout: float = 120.0,
    filters: str | dict[str, Any] | None = None,
    lang: str = "tr",
    labels: bool = True,
    max_observations: int = MAX_OBSERVATIONS,
) -> pd.DataFrame:
    """Observations of a dataflow as a tidy DataFrame (see sdmx_csv_to_frame).

    Filtering (key/filters) and the period (start/end, overlap semantics)
    are applied by TUIK's server, so only the selection is downloaded.
    ``frame.attrs['meta']`` carries the request summary.
    """
    if lang not in ("tr", "en"):
        raise ValueError("lang must be 'tr' or 'en'")
    dataflow_id = validate_dataflow_id(dataflow_id)
    started = time.monotonic()
    dsd = fetch_dsd(dataflow_id, timeout=min(timeout, 60.0))
    criteria = build_criteria(dsd, key=key, filters=filters, start=start, end=end)

    expected = count_observations(dataflow_id, criteria, dsd, timeout=min(timeout, 60.0))
    time_dim = dsd.get("time_dimension") or "TIME_PERIOD"
    time_crit = next((c for c in criteria if c.get("id") == time_dim), None)
    client_time_filter = False
    if expected == 0 and time_crit is not None:
        # Some dataflows (e.g. DF_BOLGESEL_SGP_TUKETIM_ANA_GRUPLAR_V1) answer
        # every TimeRange with nothing although they hold data for those
        # years. Check without the period; if data exists, download without
        # it and apply the period on our side (same overlap semantics).
        without_time = [c for c in criteria if c is not time_crit]
        unfiltered = count_observations(dataflow_id, without_time, dsd, timeout=min(timeout, 60.0))
        if unfiltered:
            criteria, expected, client_time_filter = without_time, unfiltered, True
    if expected == 0 and not criteria:
        raise SdmxError(
            f"Dataflow '{dataflow_id}' katalogda var ama TÜİK DataBrowser2 "
            "sunucusunda hiç gözlem içermiyor (veri yüklenmemiş/kaldırılmış). "
            "Aynı konudaki istab tablosunu tuik_search_tables ile arayın."
        )
    if expected == 0:
        raise SdmxError(
            f"Bu seçim için TÜİK'te gözlem yok ({dataflow_id}, kriterler: "
            f"{json.dumps(criteria, ensure_ascii=False)}). tuik_describe_dataflow "
            "ile verisi olan kodları ve dönem aralığını kontrol edin."
        )
    if expected is not None and expected > max_observations:
        raise SdmxError(
            f"Seçim {expected:,} gözlem içeriyor (sınır {max_observations:,}). "
            "filters ve/veya start/end ile daraltın."
        )

    remaining = max(10.0, timeout - (time.monotonic() - started))
    response = _request(
        "POST", dataflow_id, "download/csv", body=criteria, timeout=remaining,
        what="veri (SDMX-CSV)",
    )
    content = response.content
    if not content.strip():
        raise SdmxError(f"TÜİK boş SDMX-CSV döndürdü ({dataflow_id}).")
    frame = sdmx_csv_to_frame(content, dsd, lang=lang, labels=labels)
    del content, response
    if frame.empty:
        raise SdmxError(
            f"Bu seçim için TÜİK'te gözlem yok ({dataflow_id}). "
            "tuik_describe_dataflow ile kodları ve dönem aralığını kontrol edin."
        )
    if expected is not None and len(frame) != expected:
        raise SdmxError(
            f"TÜİK'in bildirdiği gözlem sayısı ({expected}) ile indirilen satır "
            f"sayısı ({len(frame)}) uyuşmuyor ({dataflow_id}); eksik/yanlış veri "
            "döndürmemek için iptal edildi. Seçimi daraltıp tekrar deneyin."
        )
    if client_time_filter:
        frame = filter_periods(frame, time_dim, time_crit.get("from"), time_crit.get("to"))
        if frame.empty:
            raise SdmxError(
                f"Bu dönem aralığında TÜİK'te gözlem yok ({dataflow_id}). "
                "tuik_describe_dataflow ile dönem aralığını kontrol edin."
            )
    frame.attrs["meta"] = {
        "dataflow_id": dataflow_id,
        "criteria": criteria,
        "time_filter": "client" if client_time_filter else "server",
        "expected_observations": expected,
        "n_missing_values": int(frame["deger"].isna().sum()),
        "seconds": round(time.monotonic() - started, 2),
    }
    return frame


# ---------------------------------------------------------------------------
# Describe
# ---------------------------------------------------------------------------

def fetch_structure(
    dataflow_id: str,
    lang: str = "tr",
    timeout: float = 120.0,
    dimension: str | None = None,
    max_codes: int = 40,
) -> dict[str, Any]:
    """Describe a dataflow without downloading its data.

    Lists every dimension the dataflow actually uses, with the codes that
    have data (from PartialCodelists) labelled in `lang`, the period range
    and the total observation count. Dimensions whose only code is '_Z'
    (not applicable) are listed separately in `inactive_dimensions`.
    If `dimension` is given, only that dimension is returned, with all its
    codes (up to 1000) and parent codes for hierarchical lists.
    """
    if lang not in ("tr", "en"):
        raise ValueError("lang must be 'tr' or 'en'")
    dataflow_id = validate_dataflow_id(dataflow_id)
    started = time.monotonic()
    deadline = started + timeout

    def left() -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 1:
            raise SdmxError(
                f"tuik_describe_dataflow {timeout:.0f} sn içinde tamamlanamadı "
                f"({dataflow_id}); TÜİK yavaş yanıt veriyor, tekrar deneyin."
            )
        return min(remaining, 60.0)

    dsd = fetch_dsd(dataflow_id, timeout=left())
    view = fetch_view(dataflow_id, lang=lang, timeout=left())
    ui_dims = [c.get("id") for c in view.get("criteria") or []]
    time_dim = dsd.get("time_dimension") or view.get("timeDimension") or "TIME_PERIOD"

    targets = dsd["dimensions"]
    if dimension:
        wanted = dimension.strip().upper()
        targets = [d for d in dsd["dimensions"] if d["id"] == wanted]
        if not targets:
            raise SdmxError(
                f"'{dimension}' bu dataflow'da bir boyut değil. Boyutlar: "
                + ", ".join(d["id"] for d in dsd["dimensions"])
            )
        max_codes = 1000

    # One PartialCodelists call per dimension (shared DSDs can have 30+);
    # a small worker pool keeps describe in seconds without hammering TUIK.
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=_DESCRIBE_WORKERS) as pool:
        futures = {
            dim["id"]: pool.submit(fetch_available_codes, dataflow_id, dim["id"], [], left())
            for dim in targets
        }
        available = {dim_id: future.result() for dim_id, future in futures.items()}

    dimensions: list[dict[str, Any]] = []
    inactive: list[str] = []
    n_obs: int | None = None
    for dim in targets:
        values, obs = available[dim["id"]]
        n_obs = obs if obs is not None else n_obs
        codes = [str(v.get("id")) for v in values]
        if not codes or codes == [NOT_APPLICABLE]:
            inactive.append(dim["id"])
            if not dimension:
                continue
        label_names = dim["codes"]
        shown = codes[:max_codes]
        entry: dict[str, Any] = {
            "id": dim["id"],
            "position": dim["position"],
            "label": pick_label(dim["label"], lang, dim["id"]),
            "n_codes": len(codes),
            "codes": {
                c: pick_label(label_names.get(c), lang, next((str(v.get("name")) for v in values if str(v.get("id")) == c), c))
                for c in shown
            },
            "in_ui_filters": dim["id"] in ui_dims,
        }
        if len(codes) > len(shown):
            entry["codes_truncated"] = True
        parents = {c: dim["parents"][c] for c in shown if c in dim["parents"]}
        if parents and dimension:
            entry["parents"] = parents
        dimensions.append(entry)

    time_range: dict[str, Any] = {}
    if not dimension:
        values, obs = fetch_available_codes(dataflow_id, time_dim, [], timeout=left())
        n_obs = obs if obs is not None else n_obs
        ids = [str(v.get("id")) for v in values]
        if ids:
            time_range = {"first": ids[0][:10], "last": ids[-1][:10]}

    title = pick_label(dsd["name"], lang, "") or str(
        ((view.get("template") or {}).get("title") or {}).get(lang) or ""
    )
    description = pick_label(dsd["description"], lang, "")
    key_dims = [d["id"] for d in dsd["dimensions"]]
    example = ";".join(
        f"{d['id']}={next(iter(d['codes']))}" for d in dimensions[:2] if d["codes"]
    )
    return {
        "dataflow_id": dataflow_id,
        "title": title,
        "description": description[:600],
        "n_observations": n_obs,
        "time_dimension": time_dim,
        "time_range": time_range,
        "dimensions": dimensions,
        "inactive_dimensions": inactive,
        "key_template": ".".join(key_dims),
        "n_key_dimensions": len(key_dims),
        "filters_example": example,
        "note": (
            "Seçim için tuik_get_data/tuik_download_data'ya filters='BOYUT=KOD1+KOD2;"
            "BOYUT2=KOD' ve start/end (2020, 2020-01, 2020-Q1) verin; filtreleme "
            "TÜİK sunucusunda yapılır. key (SDMX nokta anahtarı) da desteklenir: "
            "key_template sırasında, boş = hepsi, '+' = veya. inactive_dimensions "
            "'_Z' (uygulanamaz) değerli, kullanılmayan boyutlardır."
        ),
        "source": f"{DATABROWSER_ROOT}/#/{lang}/tuik/{dataflow_id}",
    }
