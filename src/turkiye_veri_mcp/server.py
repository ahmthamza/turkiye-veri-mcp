"""Türkiye Veri MCP: TUIK, TCMB EVDS, BDDK and HMB (Turkish official data)
in a single MCP server."""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

try:  # MCP SDK >= 2.0
    from mcp.server.mcpserver import Context, MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # MCP SDK 1.x
    from mcp.server.fastmcp import Context, FastMCP as MCPServer
    from mcp.server.fastmcp.exceptions import ToolError

from turkiye_veri_mcp import sdmx, usage
import threading
from collections import OrderedDict

from turkiye_veri_mcp.evds import EvdsClient, EvdsError, IndexNotReady
from turkiye_veri_mcp.evds import to_long as evds_to_long
from turkiye_veri_mcp.tidy import TidyError, tidy_istab
from turkiye_veri_mcp.portal import PORTAL_BASE, _UA, PortalClient

app = MCPServer(
    "turkiye-veri-mcp",
    instructions=(
        "Official Turkish data from four sources; every tool returns a small "
        "preview and most take output_path to write the full tidy CSV. "
        "TUIK (tuik_*, no key): tuik_search_tables / tuik_list_tables -> for "
        "'dataflow' rows tuik_describe_dataflow (dimensions, codes with data) "
        "then tuik_get_data / tuik_download_data with filters; for 'istab' "
        "rows tuik_get_table_data (tidy, check tidy_confidence) or "
        "tuik_download_table_file (raw Excel). "
        "TCMB EVDS (evds_*, needs free EVDS_API_KEY): evds_search_series or "
        "evds_categories -> evds_datagroups -> evds_series_list, then "
        "evds_get_data (frequency conversion, aggregation, formulas, last_n) "
        "or evds_get_datagroup_data; evds_series_info for metadata. "
        "BDDK (bddk_*, no key): call bddk_metadata first for valid tables, "
        "group codes and periods (codes differ per bulletin); bddk_get_data "
        "is province-level FinTurk, bddk_aylik_bulten / bddk_haftalik_bulten "
        "/ bddk_gunluk_bulten / bddk_bdmk_bulten are sector-level. "
        "HMB (hmb_*, no key): province budget data via hmb_get_data, "
        "hmb_get_karsilastirma, hmb_get_paylar; anything else HMB publishes "
        "via hmb_list_tables -> hmb_list_files -> hmb_get_file. "
        "Tools raise a clear error rather than return unverified numbers."
    ),
)

_portal = PortalClient()
_evds = EvdsClient()
_EVDS_MAX_CLIENTS = 64
_evds_by_key: "OrderedDict[str, EvdsClient]" = OrderedDict()
_evds_lock = threading.Lock()


def _evds_client(ctx: Context | None) -> EvdsClient:
    """Per-request EVDS client when the caller supplies their own key.

    Remote HTTP callers can send their own EVDS_API_KEY via an
    'X-Evds-Api-Key' header, so many people can share this hosted server
    without sharing one person's TCMB quota. Falls back to the server's
    own EVDS_API_KEY env var (stdio/local usage, or no header sent).
    Per-key clients live in a bounded LRU (a hosted server sees many keys).
    """
    headers = None
    if ctx is not None:
        try:  # raises outside a request; None on stdio. Works on SDK 1.x and 2.x.
            request = getattr(ctx.request_context, "request", None)
            headers = getattr(request, "headers", None)
        except Exception:  # noqa: BLE001
            headers = None
    if headers:
        for name, value in headers.items():
            if name.lower() == "x-evds-api-key" and value and value.strip():
                value = value.strip()
                with _evds_lock:
                    client = _evds_by_key.pop(value, None) or EvdsClient(api_key=value)
                    _evds_by_key[value] = client
                    while len(_evds_by_key) > _EVDS_MAX_CLIENTS:
                        _evds_by_key.popitem(last=False)
                return client
    return _evds


def _evds_errors(func):
    """Surface EVDS/validation errors to the caller. MCP SDK 2.x hides the
    text of any exception that is not a ToolError ('Error executing tool
    X'), which would swallow every actionable EVDS message."""
    import functools

    try:
        from mcp.server.mcpserver.exceptions import ToolError
    except ImportError:  # MCP SDK 1.x
        from mcp.server.fastmcp.exceptions import ToolError

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except (EvdsError, ValueError, OSError) as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


def _evds_output(frame, fmt: str, column_to_series: dict | None = None):
    fmt = (fmt or "wide").strip().lower()
    if fmt not in ("wide", "long"):
        raise ValueError("format must be 'wide' or 'long'")
    return frame if fmt == "wide" else evds_to_long(frame, column_to_series)


def _evds_frame_summary(frame) -> dict:
    out = {"n_rows": int(frame.shape[0]), "n_cols": int(frame.shape[1])}
    if frame.shape[0] and "Tarih" in frame.columns:
        out["first_period"] = str(frame["Tarih"].iloc[0])
        out["last_period"] = str(frame["Tarih"].iloc[-1])
    if frame.attrs.get("non_numeric_values"):
        out["warning"] = (
            f"{frame.attrs['non_numeric_values']} values from EVDS were not numeric "
            "and are shown as empty."
        )
    return out


def _evds_write(frame, output_path: str) -> str:
    path = Path(output_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False)
    return str(path.resolve())

_MAX_LIST = 60
_MAX_CODES_SHOWN = 40


@app.custom_route("/healthz", methods=["GET"])
async def _healthz(request):
    # Plain HTTP health check, independent of MCP session semantics --
    # a bare GET on /mcp itself returns 400 (streamable-http requires a
    # session), which would make Render's health check flap forever.
    from starlette.responses import PlainTextResponse

    return PlainTextResponse("ok")


@app.custom_route("/stats", methods=["GET"])
async def _stats(request):
    # Same aggregates as the usage_stats tool, for a browser. Counts only --
    # usage.summary() never returns visitor ids or hashes.
    from starlette.responses import JSONResponse

    try:
        days = min(max(int(request.query_params.get("days", "7")), 1), 365)
    except ValueError:
        days = 7
    import anyio.to_thread  # mcp dependency; keeps the file read off the event loop

    payload = await anyio.to_thread.run_sync(usage.summary, days)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


async def _usage_middleware(ctx, call_next):
    """SDK-level (MCP message) middleware: logs one session event per HTTP
    initialize / server/discover, and exposes the request's anonymous ids to
    usage.record() (called by _track, possibly in a worker thread -- the
    contextvar is copied there). Sees ctx.request (the Starlette Request) on
    HTTP and None on stdio. Never raises on its own account."""
    token = ident = None
    method = getattr(ctx, "method", None)
    try:
        if method in ("tools/call", *usage.SESSION_METHODS):
            headers, peer = usage.http_request_parts(getattr(ctx, "request", None))
            if headers is not None or peer is not None:
                ident = usage.identity(headers, peer)
                token = usage.set_current(ident)
    except Exception:  # noqa: BLE001
        ident = None
    try:
        result = await call_next(ctx)
        if ident is not None and method in usage.SESSION_METHODS:
            try:
                headers, _ = usage.http_request_parts(ctx.request)
                name, version = usage.client_info(method, ctx.params, headers)
                usage.record_session(
                    protocol_version=getattr(ctx, "protocol_version", None),
                    client=name,
                    client_version=version,
                    ident=ident,
                )
            except Exception:  # noqa: BLE001
                pass
        return result
    finally:
        if token is not None:
            usage.reset_current(token)


# MCP SDK >= 2.x exposes a message-level middleware list; on 1.x there is none
# and usage logging falls back to plain call counts.
if isinstance(getattr(app, "middleware", None), list):
    app.middleware.append(_usage_middleware)


def _json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=1, default=str)


def _track(func):
    """Log one usage line before running the tool. Never blocks the call.
    Over HTTP the line also carries the anonymous visitor id that
    _usage_middleware put in usage's contextvar; on stdio it does not."""
    import functools

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        usage.record(func.__name__)
        try:
            return func(*args, **kwargs)
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001
            # MCP SDK 2.x hides the text of any exception that is not a
            # ToolError ("Error executing tool X"), which would swallow
            # every actionable message from TUIK/EVDS/BDDK/HMB.
            raise ToolError(f"{type(exc).__name__}: {exc}") from exc

    return wrapper


def _truncated(rows: list, limit: int = _MAX_LIST) -> dict:
    return {"n_total": len(rows), "truncated": len(rows) > limit, "rows": rows[:limit]}


# ---------------------------------------------------------------------------
# TUIK tools
# ---------------------------------------------------------------------------

@app.tool()
@_track
def tuik_list_themes(lang: str = "tr") -> str:
    """List TUIK statistical themes (top-level categories) with their ids.

    Args:
        lang: 'tr' for Turkish names, 'en' for English.
    """
    return _json(_portal.list_themes(lang=lang))


@app.tool()
@_track
def tuik_list_tables(
    theme_id: str, lang: str = "tr", resource_type: str = "all", max_rows: int = 300
) -> str:
    """List tables under a TUIK theme: SDMX 'dataflow' entries (use dataflow_id
    with tuik_get_data/tuik_download_data), 'istab' file downloads (use url
    with tuik_get_table_data / tuik_download_table_file) and 'database'
    (MEDAS/Biruni, link only). Each row has a 'path' (folder trail).

    Args:
        theme_id: Theme id from tuik_list_themes (e.g. '11').
        lang: 'tr' or 'en'.
        resource_type: 'dataflow', 'istab', 'database', 'press', 'report'
            (comma-separated allowed), or 'all' (default: dataflow + istab +
            database).
        max_rows: Maximum rows returned (default 300; large themes have
            700+ tables -- narrow with resource_type or tuik_search_tables).
    """
    types = (
        ("dataflow", "istab", "database")
        if resource_type == "all"
        else tuple(t.strip() for t in resource_type.split(",") if t.strip())
    )
    rows = _portal.list_resources(theme_id, lang=lang, types=types)
    for row in rows:
        row.pop("metadata_url", None)
    limit = max(1, max_rows)
    return _json(
        {
            "n_tables": len(rows),
            "truncated": len(rows) > limit,
            "tables": rows[:limit],
        }
    )


@app.tool()
@_track
def tuik_search_tables(query: str, lang: str = "tr", resource_type: str = "all") -> str:
    """Search TUIK tables across all themes.

    Turkish-aware: case and diacritics are ignored with Turkish rules
    ('isgucu' finds 'İşgücü', 'issizlik' finds 'İşsizlik'). Every word must
    match (AND), in the table name or its folder path; name matches come
    first. Each row has a 'path' (folder trail) to tell same-named tables
    apart (e.g. monthly vs quarterly).

    Args:
        query: Search words, e.g. 'işgücü' or 'tufe aylik'.
        lang: Language of table names to search in ('tr' or 'en').
        resource_type: 'dataflow' (SDMX, use tuik_get_data), 'istab' (Excel
            file, use tuik_get_table_data), 'database' (MEDAS/Biruni, link
            only), 'press', 'report', or 'all' (default: dataflow + istab +
            database).
    """
    types = (
        ("dataflow", "istab", "database")
        if resource_type == "all"
        else tuple(t.strip() for t in resource_type.split(",") if t.strip())
    )
    rows = _portal.search(query, lang=lang, types=types)
    for row in rows:
        row.pop("metadata_url", None)
    return _json(_truncated(rows))


@app.tool()
@_track
def tuik_describe_dataflow(dataflow_id: str, lang: str = "tr", dimension: str = "") -> str:
    """Describe a TUIK SDMX dataflow without downloading its data: title,
    observation count, period range, and every dimension it uses with the
    codes that actually have data (labelled in `lang`).

    Use the codes with tuik_get_data/tuik_download_data filters, e.g.
    filters='SEX=2;ISGUCU_GOSTERGE=8'. Codes per dimension are capped at 40
    here; pass `dimension` to list all codes of one dimension (with parent
    codes for hierarchical lists such as regions or COICOP).

    Args:
        dataflow_id: Three-part SDMX id, e.g. 'TR,DF_TUFE_SDMX_TT10,1.0'
            (the dataflow_id field from tuik_search_tables/tuik_list_tables).
        lang: Label language ('tr' or 'en'); codes are the same in both.
        dimension: Optional dimension id to list in full (e.g. 'COICOP_2018').
    """
    structure = sdmx.fetch_structure(
        dataflow_id, lang=lang, dimension=dimension or None, max_codes=_MAX_CODES_SHOWN
    )
    return _json(structure)


def _tuik_fetch(dataflow_id, key, start, end, filters, lang, labels=True,
                max_observations=sdmx.MAX_OBSERVATIONS):
    return sdmx.fetch_data(
        dataflow_id,
        key=key or "ALL",
        start=start or None,
        end=end or None,
        filters=filters or None,
        lang=lang,
        labels=labels,
        max_observations=max_observations,
        timeout=300.0,
    )


@app.tool()
@_track
def tuik_get_data(
    dataflow_id: str,
    key: str = "ALL",
    start: str = "",
    end: str = "",
    max_rows: int = 50,
    filters: str = "",
    lang: str = "tr",
) -> str:
    """Fetch observations from a TUIK SDMX dataflow and preview them in chat.

    Filtering happens on TUIK's server, so narrow large dataflows with
    `filters` and `start`/`end` (see tuik_describe_dataflow for codes).
    Output columns: one code column per used dimension (SDMX id) plus
    '<DIM>_label', the time column (TIME_PERIOD), 'deger' (numeric value)
    and observation attributes when present (e.g. OBS_STATUS, CONF_STATUS).
    Columns that are constant across the result are reported once under
    'constant_columns' and left out of the preview to keep it small.

    Args:
        dataflow_id: Three-part SDMX id, e.g. 'TR,DF_TUFE_SDMX_TT10,1.0'.
        key: 'ALL' (default) or an SDMX dot key in the order of key_template
            from tuik_describe_dataflow ('+' = or, empty slot = all).
        start: Optional first period: '2020', '2020-01', '2020-Q1', '2020-S1'.
            Periods overlapping [start, end] are returned.
        end: Optional last period (same formats).
        max_rows: Rows to include in the preview (default 50).
        filters: Optional 'DIM=code1+code2;DIM2=code' (or a JSON object),
            e.g. 'SEX=2;ISGUCU_GOSTERGE=8'. Unknown dimensions/codes are
            rejected rather than ignored.
        lang: Label language for the *_label columns ('tr' or 'en').
    """
    # A chat preview never needs a whole million-row flow: ask for filters
    # (tuik_download_data allows up to sdmx.MAX_OBSERVATIONS).
    frame = _tuik_fetch(dataflow_id, key, start, end, filters, lang, max_observations=min(300_000, sdmx.MAX_OBSERVATIONS))
    meta = frame.attrs.get("meta", {})
    constant = {
        column: frame[column].iloc[0]
        for column in frame.columns
        if column != "deger" and len(frame) > 1 and frame[column].nunique(dropna=False) == 1
    }
    preview = frame.drop(columns=list(constant)).head(max(0, max_rows)).to_csv(index=False)
    if len(preview) > 12000:
        preview = preview[:12000] + "\n... (kırpıldı; tamamı için tuik_download_data)"
    return _json(
        {
            "dataflow_id": meta.get("dataflow_id", dataflow_id),
            "n_rows": int(frame.shape[0]),
            "n_cols": int(frame.shape[1]),
            "columns": list(frame.columns),
            "n_missing_values": meta.get("n_missing_values"),
            "constant_columns": constant,
            "preview_rows": min(max_rows, int(frame.shape[0])),
            "preview_csv": preview,
        }
    )


@app.tool()
@_track
def tuik_download_data(
    dataflow_id: str,
    output_path: str,
    key: str = "ALL",
    start: str = "",
    end: str = "",
    filters: str = "",
    lang: str = "tr",
    labels: bool = True,
) -> str:
    """Download a TUIK SDMX dataflow (or a filtered part of it) and write it
    as a tidy CSV file (columns as in tuik_get_data).

    Args:
        dataflow_id: Three-part SDMX id, e.g. 'TR,DF_TUFE_SDMX_TT10,1.0'.
        output_path: Where to write the CSV (e.g. 'data/tufe.csv').
        key: 'ALL' (default) or an SDMX dot key (see tuik_get_data).
        start: Optional first period ('2020', '2020-01', '2020-Q1').
        end: Optional last period.
        filters: Optional 'DIM=code1+code2;DIM2=code' selection.
        lang: Label language for *_label columns ('tr' or 'en').
        labels: False writes code columns only (smaller file).
    """
    frame = _tuik_fetch(dataflow_id, key, start, end, filters, lang, labels=labels)
    meta = frame.attrs.get("meta", {})
    path = Path(output_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False)
    return _json(
        {
            "written": str(path.resolve()),
            "n_rows": int(frame.shape[0]),
            "n_cols": int(frame.shape[1]),
            "columns": list(frame.columns),
            "n_missing_values": meta.get("n_missing_values"),
        }
    )


@app.tool()
@_track
def tuik_download_table_file(url: str, output_path: str) -> str:
    """Download an 'istab' table file (usually Excel) from the TUIK portal.

    The portal serves one file per ~5 seconds per client; this tool waits
    and retries automatically and never saves the portal's "please wait"
    page as the file.

    Args:
        url: The url field of an istab row from tuik_list_tables/tuik_search_tables.
        output_path: Where to save the file (e.g. 'data/table.xls'). The
            portal's own file name is reported as 'source_filename'.
    """
    from turkiye_veri_mcp.portal import download_file

    downloaded = download_file(url)
    path = Path(output_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(downloaded.content)
    return _json(
        {
            "written": str(path.resolve()),
            "bytes": len(downloaded.content),
            "content_type": downloaded.content_type,
            "file_kind": downloaded.kind,
            "source_filename": downloaded.filename,
        }
    )


@app.tool()
@_track
def tuik_get_table_data(url: str, output_path: str = "", max_rows: int = 50) -> str:
    """Download an 'istab' Excel table and convert it to tidy long format.

    Handles TUIK's common layouts (title/footnote rows, year-per-sheet
    workbooks, crosstabs with period headers, Yıl/Ay stub columns with
    merged cells). Output columns: the table's own label columns (kept
    verbatim), 'donem' when a single period column/header exists, 'gosterge'
    (the value column's header) and 'deger'. tidy_confidence: 'yüksek'
    (all sheets, with periods), 'orta' (all sheets, some without a period:
    check the title), 'kısmi' (some sheets failed). Layouts the parser cannot
    read safely are refused with an error rather than guessed (ambiguous
    '1.234' text cells are dropped, not guessed); tuik_download_table_file
    always gets you the raw file.

    Args:
        url: The url field of an istab row from tuik_list_tables/tuik_search_tables.
        output_path: Optional CSV path; if given, the tidy table is written there.
        max_rows: Rows to include in the preview (default 50).
    """
    from turkiye_veri_mcp.portal import download_file

    downloaded = download_file(url)
    try:
        frame, report = tidy_istab(downloaded.content)
    except TidyError as exc:
        return _json(
            {
                "error": str(exc),
                "hint": "Bu tablo bilinen şablonlara uymuyor; ham dosya için "
                        "tuik_download_table_file kullanın.",
            }
        )
    for field in ("sheet_notes", "sheet_failures"):
        items = report.get(field) or {}
        if len(items) > 20:
            report[field] = dict(list(items.items())[:20])
            report[f"{field}_truncated"] = len(items)
    preview = frame.head(max(0, max_rows)).to_csv(index=False)
    if len(preview) > 12000:
        preview = preview[:12000] + "\n... (kırpıldı; tamamı için output_path verin)"
    result = {
        "n_rows": int(frame.shape[0]),
        "columns": list(frame.columns),
        **report,
        "source_filename": downloaded.filename,
        "preview_csv": preview,
    }
    if output_path:
        path = Path(output_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(path, index=False)
        result["written"] = str(path.resolve())
    return _json(result)


# ---------------------------------------------------------------------------
# EVDS tools (require EVDS_API_KEY; free at https://evds3.tcmb.gov.tr)
# ---------------------------------------------------------------------------

@app.tool()
@_track
@_evds_errors
def evds_categories(lang: str = "TR", ctx: Context = None) -> str:
    """List TCMB EVDS categories (exchange rates, interest rates, inflation,
    balance of payments, surveys...) as a 3-level tree.

    Each row: category_id, title, level (1-3) and parent_id (-1 = top level).
    Pass any category_id (any level) to evds_datagroups.

    Args:
        lang: 'TR' or 'ENG'.
    """
    return _json(_evds_client(ctx).categories(lang=lang))


@app.tool()
@_track
@_evds_errors
def evds_datagroups(
    category_id: str = "",
    query: str = "",
    lang: str = "TR",
    offset: int = 0,
    limit: int = 60,
    ctx: Context = None,
) -> str:
    """List or search EVDS datagroups (thematic bundles of series).

    Provide category_id to list a category's datagroups (subcategories
    included), query to search datagroup names/codes (Turkish-insensitive:
    'issizlik' finds 'İşsizlik'; words are ANDed), or neither to list all.
    Rows include frequency, start/end date, unit, source and last update.

    Args:
        category_id: A category id from evds_categories (optional).
        query: Words to search in datagroup names (optional).
        lang: 'TR' or 'ENG'.
        offset: Skip this many rows (paging).
        limit: Rows to return (default 60, max 500).
    """
    client = _evds_client(ctx)
    if query:
        rows = client.search_datagroups(query, lang=lang)
        if category_id:
            allowed = {r["datagroup_code"] for r in client.datagroups(category_id, lang=lang)}
            rows = [r for r in rows if r["datagroup_code"] in allowed]
    else:
        rows = client.datagroups(category_id or None, lang=lang)
    limit = max(1, min(int(limit), 500))
    page = rows[offset : offset + limit]
    return _json(
        {
            "n_total": len(rows),
            "offset": offset,
            "truncated": offset + len(page) < len(rows),
            "rows": page,
        }
    )


@app.tool()
@_track
@_evds_errors
def evds_series_list(
    datagroup_code: str,
    lang: str = "TR",
    offset: int = 0,
    limit: int = 200,
    ctx: Context = None,
) -> str:
    """List the series in an EVDS datagroup, plus the datagroup's metadata
    (unit, source, notes, methodology links).

    Each series row: serie_code, name, frequency, start_date, end_date,
    default_aggregation. Page with offset/limit when n_total > limit.

    Args:
        datagroup_code: Datagroup code from evds_datagroups (e.g. 'bie_dkdovytl').
        lang: 'TR' or 'ENG'.
        offset: Skip this many series (paging).
        limit: Series to return (default 200, max 1000).
    """
    client = _evds_client(ctx)
    rows = client.series_list(datagroup_code, lang=lang)
    if not rows:
        client.datagroup_info(datagroup_code, lang=lang)  # raises if unknown
    limit = max(1, min(int(limit), 1000))
    page = rows[offset : offset + limit]
    info = client.datagroup_info(datagroup_code, lang=lang)
    return _json(
        {
            "datagroup": info,
            "n_total": len(rows),
            "offset": offset,
            "truncated": offset + len(page) < len(rows),
            "rows": page,
        }
    )


@app.tool()
@_track
@_evds_errors
def evds_series_info(series: list[str], lang: str = "TR", ctx: Context = None) -> str:
    """Metadata for specific EVDS series codes: name, datagroup, unit,
    frequency, first/last observation date, default and allowed aggregations,
    source, tags and methodology links. Up to 20 codes per call.

    Args:
        series: EVDS series codes, e.g. ['TP.DK.USD.A.YTL', 'TP.FG.J0'].
        lang: 'TR' or 'ENG'.
    """
    if len(series) > 20:
        raise ValueError("At most 20 series per call.")
    return _json(_evds_client(ctx).series_info(series, lang=lang))


@app.tool()
@_track
@_evds_errors
def evds_search_series(
    query: str,
    lang: str = "TR",
    limit: int = 50,
    refresh: bool = False,
    offset: int = 0,
    wait: bool = False,
    ctx: Context = None,
) -> str:
    """Search EVDS series by name, code, tags and datagroup name across all
    datagroups. Turkish-insensitive ('issizlik' finds 'İşsizlik'); words are
    ANDed; TR and ENG names are both searched.

    EVDS has no server-side series search, so the index is built once by
    walking every datagroup (~680 requests, about 10 minutes) and cached on
    disk. While it is being built this returns matching *datagroups*
    instead (index_ready=false); call again later for series hits. The
    index refreshes itself in the background once it is a week old.

    Args:
        query: Words to look for, e.g. 'konut fiyat', 'issizlik' or 'TP.DK.USD'.
        lang: 'TR' or 'ENG' (language of returned names).
        limit: Maximum hits to return (default 50).
        refresh: Rebuild the index now instead of using the cache.
        offset: Skip this many hits (paging).
        wait: Block until the index is built instead of returning early.
    """
    client = _evds_client(ctx)
    try:
        hits, n_total, built_now = client.search_series(
            query, lang=lang, limit=limit, refresh=refresh, wait=wait, offset=offset
        )
    except IndexNotReady as exc:
        groups = client.search_datagroups(query, lang=lang)
        return _json(
            {
                **exc.status,
                "message": str(exc),
                "n_datagroup_hits": len(groups),
                "datagroup_hits": groups[:limit],
            }
        )
    return _json(
        {
            "n_hits": n_total,
            "offset": offset,
            "truncated": offset + len(hits) < n_total,
            "index_built_this_call": built_now,
            **client.index_status(),
            "hits": hits,
        }
    )


@app.tool()
@_track
@_evds_errors
def evds_get_datagroup_data(
    datagroup_code: str,
    start: str = "",
    output_path: str = "",
    end: str = "",
    frequency: str = "",
    aggregation: str = "",
    formula: str = "",
    max_rows: int = 30,
    lang: str = "TR",
    format: str = "wide",
    last_n: int = 0,
    ctx: Context = None,
) -> str:
    """Fetch every series in an EVDS datagroup at once.

    Mirrors the website's 'download the whole datagroup' action. EVDS
    returns at most 1000 rows per request; longer spans are paged
    automatically, so nothing is truncated.

    Args:
        datagroup_code: Datagroup code from evds_datagroups (e.g. 'bie_dkdovytl').
        start: Start date (dd-mm-yyyy or yyyy-mm-dd). Optional if last_n is set.
        output_path: Optional CSV path for the full table.
        end: End date (default: today).
        frequency: Target frequency name or code 1-8.
        aggregation: avg/min/max/first/last/sum (one for all, or comma-separated per series).
        formula: Transformation name or code 0-8 (one for all, or comma-separated per series).
        max_rows: Rows in the chat preview (default 30).
        lang: 'TR' or 'ENG' for the series names returned.
        format: 'wide' (Tarih + one column per series, EVDS column names) or
            'long' (Tarih, date, series, column, value; empty values dropped).
        last_n: Only the latest N periods (use instead of start).
    """
    frame, series_meta = _evds_client(ctx).get_datagroup_data(
        datagroup_code, start=start, end=end,
        frequency=frequency, aggregation=aggregation, formula=formula,
        lang=lang, last_n=last_n,
    )
    mapping = {s["serie_code"].replace(".", "_"): s["serie_code"] for s in series_meta}
    out = _evds_output(frame, format, mapping)
    result = {
        "datagroup_code": datagroup_code,
        "n_series": len(series_meta),
        **_evds_frame_summary(frame),
        "format": format,
        "preview_csv": out.head(max_rows).to_csv(index=False),
    }
    if output_path:
        result["written"] = _evds_write(out, output_path)
        result["rows_written"] = int(out.shape[0])
    return _json(result)


@app.tool()
@_track
@_evds_errors
def evds_get_data(
    series: list[str],
    start: str = "",
    end: str = "",
    frequency: str = "",
    aggregation: str = "",
    formula: str = "",
    max_rows: int = 50,
    output_path: str = "",
    format: str = "wide",
    last_n: int = 0,
    ctx: Context = None,
) -> str:
    """Fetch one or more EVDS series with full API features and preview in chat.

    Values equal what EVDS returns (numbers; empty = no observation).
    Columns are EVDS names: dots become underscores and a non-level formula
    adds '-N' (TP.FG.J0 with formula 3 -> 'TP_FG_J0-3'). Requests mixing
    frequencies are returned by EVDS at the lowest frequency. Weekly data
    has an extra YEARWEEK column. Never truncated (EVDS's 1000-row cap is
    paged). Unknown series codes raise an error naming them.

    Args:
        series: EVDS series codes, e.g. ['TP.DK.USD.A.YTL', 'TP.FG.J0'].
        start: Start date (dd-mm-yyyy or yyyy-mm-dd). Optional if last_n is set.
        end: End date (default: today).
        frequency: Target frequency: gunluk/isgunu/haftalik/ayda2/aylik/
            ceyreklik/altiaylik/yillik (or codes 1-8). Empty = series default.
        aggregation: How to aggregate when reducing frequency:
            avg, min, max, first, last, sum. One for all series or
            comma-separated per series. Empty = each series' default.
        formula: Transformation: duzey, yuzde_degisim, fark, yillik_yuzde,
            yillik_fark, yilsonu_yuzde, yilsonu_fark, hareketli_ortalama,
            hareketli_toplam (or codes 0-8). One for all or comma-separated
            per series. Empty = level.
        max_rows: Rows in the preview (default 50).
        output_path: Optional CSV path to also write the full result to.
        format: 'wide' or 'long' (Tarih, date, series, column, value; empty
            values dropped).
        last_n: Only the latest N periods (use instead of start).
    """
    frame = _evds_client(ctx).get_data(
        series, start=start, end=end, last_n=last_n,
        frequency=frequency, aggregation=aggregation, formula=formula,
    )
    mapping = {s.strip().replace(".", "_"): s.strip() for s in series}
    out = _evds_output(frame, format, mapping)
    result = {
        **_evds_frame_summary(frame),
        "columns": list(frame.columns),
        "format": format,
        "preview_rows": min(max_rows, int(out.shape[0])),
        "preview_csv": out.head(max_rows).to_csv(index=False),
    }
    if output_path:
        result["written"] = _evds_write(out, output_path)
        result["rows_written"] = int(out.shape[0])
    return _json(result)


@app.tool()
@_track
@_evds_errors
def evds_download_data(
    series: list[str],
    start: str,
    output_path: str,
    end: str = "",
    frequency: str = "",
    aggregation: str = "",
    formula: str = "",
    format: str = "wide",
    ctx: Context = None,
) -> str:
    """Fetch EVDS series and write them to a CSV file (for analysis
    pipelines). Same parameters as evds_get_data.

    Args:
        series: EVDS series codes.
        start: Start date (dd-mm-yyyy or yyyy-mm-dd).
        output_path: Where to write the CSV (e.g. 'data/usd_try.csv').
        end: End date (default: today).
        frequency: Target frequency name or code 1-8.
        aggregation: avg/min/max/first/last/sum (or comma-separated per series).
        formula: Transformation name or code 0-8 (or comma-separated per series).
        format: 'wide' (default) or 'long' (Tarih, date, series, column, value).
    """
    frame = _evds_client(ctx).get_data(
        series, start=start, end=end,
        frequency=frequency, aggregation=aggregation, formula=formula,
    )
    mapping = {s.strip().replace(".", "_"): s.strip() for s in series}
    out = _evds_output(frame, format, mapping)
    return _json(
        {
            "written": _evds_write(out, output_path),
            **_evds_frame_summary(frame),
            "rows_written": int(out.shape[0]),
            "columns": list(out.columns),
        }
    )


@app.tool()
@_track
def usage_stats(days_recent: int = 7) -> str:
    """Show how much this MCP server has been used (this instance's log).

    Tool calls (total, by tool) plus, for the hosted HTTP server, sessions
    (by client name) and estimated unique visitors (all time, recent window,
    per day, per client name -- a visitor seen with two clients counts under
    both), and tool calls attributed to a client only when the visitor maps
    to exactly one client name (the rest: 'birden_cok_veya_bilinmiyor').
    A visitor is a salted hash of the client IP, so this is an
    estimate, not a head count -- see 'note' in the output. On a hosted
    free-tier deployment the log resets on redeploy/restart. Anthropic does
    not expose per-connector usage to server owners, so this local counter
    is the closest available substitute. Same data: GET /stats.

    Args:
        days_recent: Size of the "recent" window in days (default 7).
    """
    return _json(usage.summary(days_recent=days_recent))


@app.tool()
@_track
def bddk_get_data(
    tablo_no: int = 1,
    donem: str = "",
    donem_bitis: str = "",
    taraf_list: list[str] | None = None,
    sehir_list: list[str] | None = None,
    output_path: str = "",
    max_rows: int = 90,
) -> str:
    """BDDK FinTürk: province-level (il bazında) banking data, quarterly.

    A geographic breakdown EVDS does not have. Tables (units in the name):
      1 Krediler (Bin TL), 2 Mevduat (Bin TL), 3 Bireysel Bankacılık (Bin TL),
      4 Seçilmiş Sektörel Krediler (Bin TL), 5 Oranlar (%),
      6 Şubeler (Adet) ve Nüfusa Göre Dağılım (TL),
      7 Altın Kredileri ve Altın Mevduatı (Bin TL)
    Periods are quarter ends "YYYY-M" (M = 3, 6, 9, 12) from 2007-12; list
    them with bddk_metadata(bulten="finturk"). An unpublished period is an
    error, never an empty table.

    Args:
        tablo_no: Table 1-7 (see list above).
        donem: Period "YYYY-M", e.g. "2026-9". Empty = latest published.
            With donem_bitis, the start of a range.
        donem_bitis: Optional end of a range (inclusive) -> time series, one
            request per quarter, max 40 quarters per call.
        taraf_list: FinTürk group codes (default ["10001"] = whole sector):
            10001 SEKTÖR, 10002 MEVDUAT, 10003 KALKINMA VE YATIRIM,
            10004 KATILIM, 10005 YABANCI, 10006 KAMU, 10007 YERLİ ÖZEL.
            (These differ from the Aylık Bülten codes!)
        sehir_list: Province names (default all provinces + YURT DIŞI = 82
            rows). Case/diacritics tolerant ("istanbul", "Mus" -> MUŞ);
            unknown names raise an error listing valid ones.
        output_path: Optional CSV path for the full result.
        max_rows: Rows in the chat preview (default 90).
    """
    from turkiye_veri_mcp import bddk

    return _json(bddk.tool_finturk(
        tablo_no=tablo_no, donem=donem, donem_bitis=donem_bitis, taraf_list=taraf_list,
        sehir_list=sehir_list, output_path=output_path, max_rows=max_rows))


@app.tool()
@_track
def bddk_metadata(bulten: str = "", n_periods: int = 12) -> str:
    """List BDDK bulletins and their valid parameters (live from bddk.org.tr).

    Use before the other bddk_* tools to discover table numbers, bank-group
    (taraf) codes, currencies and published periods instead of guessing.
    Taraf codes are NOT the same across bulletins.

    Args:
        bulten: "" (overview), "finturk", "aylik", "haftalik", "gunluk" or
            "bdmk" (non-bank financial institutions).
        n_periods: How many most-recent periods to list (default 12).
    """
    from turkiye_veri_mcp import bddk

    return _json(bddk.tool_metadata(bulten=bulten, n_periods=n_periods))


@app.tool()
@_track
def bddk_aylik_bulten(
    tablo_no: int = 1,
    donem: str = "",
    donem_bitis: str = "",
    taraf_list: list[str] | None = None,
    para_birimi: str = "TL",
    output_path: str = "",
    max_rows: int = 80,
) -> str:
    """BDDK Aylık Bülten: monthly banking-sector tables by bank group.

    Tables: 1 Bilanço, 2 Kar Zarar, 3 Krediler, 4 Tüketici Kredileri,
    5 Sektörel Kredi Dağılımı, 6 KOBİ Kredileri, 7 Sendikasyon Seküritizasyon
    Kredileri, 8 Menkul Kıymetler, 9 Mevduat Türler İtibarıyla, 10 Mevduat
    Vade İtibarıyla, 11 Likidite Durumu, 12 Sermaye Yeterliliği, 13 Yabancı
    Para Pozisyonu, 14 Bilanço Dışı İşlemler, 15 Rasyolar (NPL, ROE, ...),
    16 Diğer Bilgiler (banka/şube/personel sayısı), 17 Yurt Dışı Şube Rasyoları.
    Output: one row per item (kalem) x group x month; value columns as on the
    site (TP/YP/Toplam, maturity buckets, ...). Unit is in 'birim' (most
    tables milyon TL; table 5 bin TL; USD -> milyon $ / bin $; ratios %).
    Data from 2002-12; unpublished months return BDDK's own error message.

    Args:
        tablo_no: 1-17 (see above).
        donem: "YYYY-MM", e.g. "2026-09". Empty = latest published month.
            With donem_bitis, the start of a range.
        donem_bitis: Optional end month (inclusive); max 36 months per call.
        taraf_list: Aylık Bülten group codes (default ["10001"] Sektör):
            10001 Sektör, 10002 Mevduat, 10008 Mevduat-Yerli Özel,
            10009 Mevduat-Kamu, 10010 Mevduat-Yabancı, 10003 Katılım,
            10004 Kalkınma ve Yatırım, 10005 Yerli Özel, 10006 Kamu,
            10007 Yabancı; also 10011-10016 (Katılım / Kalkınma ve Yatırım x
            Yerli Özel / Kamu / Yabancı; not in the site's dropdown but served).
        para_birimi: "TL" or "USD" (tables 11-13, 15-17 only TL).
        output_path: Optional CSV path for the full result.
        max_rows: Rows in the chat preview (default 80).
    """
    from turkiye_veri_mcp import bddk

    return _json(bddk.tool_aylik(
        tablo_no=tablo_no, donem=donem, donem_bitis=donem_bitis, taraf_list=taraf_list,
        para_birimi=para_birimi, output_path=output_path, max_rows=max_rows))


@app.tool()
@_track
def bddk_haftalik_bulten(
    tablo_id: str = "289",
    donem: str = "",
    taraf_list: list[str] | None = None,
    para_birimi: str = "TL",
    output_path: str = "",
    max_rows: int = 60,
) -> str:
    """BDDK Haftalık Bülten: weekly banking-sector table for one week.

    Tables (site ids): 289 Krediler, 290 Takipteki Alacaklar, 291 Menkul
    Değerler, 292 Mevduat, 293 Diğer Bilanço Kalemleri, 294 Bilanço Dışı
    İşlemler, 295/296 Bankalarda Saklanan Menkul Değerler 1/2, 297 Yabancı
    Para Pozisyonu. Values in milyon TL (or milyon USD), columns TP/YP/TOPLAM.
    Weeks from 2014-01-03; list them with bddk_metadata(bulten="haftalik").
    Each row carries a kalem_id for bddk_haftalik_seri.

    Args:
        tablo_id: Table id (see above), default 289 Krediler.
        donem: Week date "YYYY-MM-DD" (the bulletin's data date, usually a
            Friday). Empty = latest week. A non-published date is an error
            that lists the nearest weeks.
        taraf_list: Haftalık group codes (default ["10001"]): 10001 Sektör,
            10002 Mevduat, 10003 Kalkınma ve Yatırım, 10004 Katılım,
            10005 Kamu, 10006 Yabancı, 10007 Yerli Özel. Max 7.
        para_birimi: "TL" or "USD".
        output_path: Optional CSV path for the full result.
        max_rows: Rows in the chat preview (default 60).
    """
    from turkiye_veri_mcp import bddk

    return _json(bddk.tool_haftalik(
        tablo_id=tablo_id, donem=donem, taraf_list=taraf_list, para_birimi=para_birimi,
        output_path=output_path, max_rows=max_rows))


@app.tool()
@_track
def bddk_haftalik_seri(
    kalem_id: str,
    sutun: str = "TOPLAM",
    taraf: str = "10001",
    para_birimi: str = "TL",
    baslangic: str = "",
    bitis: str = "",
    output_path: str = "",
    max_rows: int = 120,
) -> str:
    """Weekly time series of one Haftalık Bülten item (e.g. total loans).

    Args:
        kalem_id: Row id from bddk_haftalik_bulten output, e.g. "1.0.1"
            (Toplam Krediler), "4.0.1" (Toplam Mevduat).
        sutun: "TP", "YP" or "TOPLAM".
        taraf: Haftalık group code (default "10001" Sektör).
        para_birimi: "TL" or "USD" (values in millions).
        baslangic: Start date "YYYY-MM-DD" (default: 12 weeks before bitis).
        bitis: End date "YYYY-MM-DD" (default: latest week). Max ~4 years
            per call (fetched in 13-week windows, paced).
        output_path: Optional CSV path.
        max_rows: Rows in the chat preview (default 120).
    """
    from turkiye_veri_mcp import bddk

    return _json(bddk.tool_haftalik_seri(
        kalem_id=kalem_id, sutun=sutun, taraf=taraf, para_birimi=para_birimi,
        baslangic=baslangic, bitis=bitis, output_path=output_path, max_rows=max_rows))


@app.tool()
@_track
def bddk_gunluk_bulten(
    para_birimi: str = "TL",
    kalem_id: str = "",
    sutun: str = "TOPLAM",
    output_path: str = "",
    max_rows: int = 60,
) -> str:
    """BDDK Günlük Bülten: sector summary balance sheet for the latest day.

    BDDK only publishes the most recent day (no archive). Without kalem_id
    returns the full table (AKTİFLER / PASİFLER / BİLANÇO DIŞI; TP, YP,
    TOPLAM in milyon TL or USD). With kalem_id returns that item's last ~7
    business days.

    Args:
        para_birimi: "TL" or "USD".
        kalem_id: Optional row id from the table output (e.g. "1.6.0" Krediler,
            "2.1.0" Mevduat) to get its short daily series.
        sutun: For the series: "TP", "YP" or "TOPLAM".
        output_path: Optional CSV path.
        max_rows: Rows in the chat preview (default 60).
    """
    from turkiye_veri_mcp import bddk

    return _json(bddk.tool_gunluk(
        para_birimi=para_birimi, kalem_id=kalem_id, sutun=sutun,
        output_path=output_path, max_rows=max_rows))


@app.tool()
@_track
def bddk_bdmk_bulten(
    sektor: str = "finansal_kiralama",
    tablo_no: str = "1",
    donem: str = "",
    donem_bitis: str = "",
    output_path: str = "",
    max_rows: int = 80,
) -> str:
    """BDDK monthly bulletin for non-bank financial institutions (sector totals).

    Sectors and tables (ids as on the site; list live with
    bddk_metadata(bulten="bdmk")):
      finansal_kiralama: 1 Bilanço, 2 Kâr Zarar, 3 Sektörel Dağılım,
        6 Finansal Varlıklar, 7 Rasyolar, 8 Diğer Bilgiler, 9 Kiralama
        Alacakları - Taraf İtibariyle
      faktoring: 1, 2, 4 Sektörel Dağılım, 5 Sektörel Dağılım (Ciro), 7, 8,
        10 Faktoring Alacakları - Taraf İtibariyle
      finansman: 1, 2, 7, 8, 11 Finansman Kredileri - Taraf İtibariyle,
        12 Tüketici Kredileri
      varlik_yonetim (quarterly, from 2020): 14 Bilanço, 15 Kar/Zarar,
        16 Bilanço Dışı İşlemler, 17 Diğer Bilgiler
    Unit per table in 'birim' (Milyon TL, Bin TL, Yüzde, ...). Monthly from
    2008 (VYS quarterly). Unpublished periods raise an error.

    Args:
        sektor: finansal_kiralama | faktoring | finansman | varlik_yonetim.
        tablo_no: Table id for that sector (see above).
        donem: "YYYY-MM"; empty = latest. With donem_bitis, start of range.
        donem_bitis: Optional end (inclusive), max 36 periods per call.
        output_path: Optional CSV path.
        max_rows: Rows in the chat preview (default 80).
    """
    from turkiye_veri_mcp import bddk

    return _json(bddk.tool_bdmk(
        sektor=sektor, tablo_no=tablo_no, donem=donem, donem_bitis=donem_bitis,
        output_path=output_path, max_rows=max_rows))


@app.tool()
@_track
def hmb_get_data(
    il_kodu: str, yil: int = 0, max_rows: int = 40, output_path: str = "", ay: int = 0
) -> str:
    """HMB: general budget revenue by province -- Tahakkuk (accrual) and
    Tahsilat (collection), one file per province, 2004-latest.

    Values are CUMULATIVE year-to-date: the row for donem "2026-09" is
    January-September 2026. Unit is always thousand TL (bin TL); 2004-2005
    files say "(Milyar TL)" (old lira) which is numerically identical to
    bin YTL/TL and is NOT rescaled. "00" is HMB's "Merkez" unit (ministries'
    central accounting offices), NOT the national total -- the national total
    is the sum of 00-81. Each month is a hierarchical revenue statement;
    kalem_sira keeps HMB's row order (hierarchy). Folder ids are discovered
    live from muhasebat.hmb.gov.tr, so new years appear automatically.

    Args:
        il_kodu: "00" (Merkez) or plate code "01"-"81" (34 = İstanbul).
        yil: Year (2004-latest). 0 = latest year that has files.
        max_rows: Preview rows in the reply (latest month first; max 500).
        output_path: Optional CSV path for the full table.
        ay: Optional month filter 1-12 (0 = all published months).
    """
    from turkiye_veri_mcp import hmb

    frame = hmb.HmbClient().get_il_data(il_kodu=il_kodu, yil=yil or None)
    frame = hmb.filter_frame(frame, ay=ay)
    return _json(hmb.tool_payload(frame, max_rows, output_path))


@app.tool()
@_track
def hmb_get_karsilastirma(
    tablo: str = "denge",
    yil: int = 0,
    max_rows: int = 40,
    output_path: str = "",
    ay: int = 0,
    il_kodu: str = "",
) -> str:
    """HMB: province x category budget tables (one file, provinces as rows).

    Series (picked automatically from tablo + yil):
      - "gider"/"gelir"/"vergi"/"denge": Merkezi Yönetim by province,
        2004-latest, one sheet per month, CUMULATIVE year-to-date. For
        yil 1990-2003 the same names return the predecessor series
        "İller İtibarıyla Konsolide Bütçe" (1990-2002 annual only,
        2003 monthly cumulative).
      - "mahalli_gider"/"mahalli_gelir"/"mahalli_denge": Mahalli İdareler
        by province, 2006-latest; cumulative quarters (donem 2026-06 =
        Jan-Jun); 2006-2008 single annual sheet.
    deger_bin_tl is thousand TL (old-lira "Milyar TL" = bin TL numerically,
    not rescaled). Share/ratio columns (kategori containing "%") have
    birim="%" and an empty deger_bin_tl -- always read `deger` + `birim`.
    "Merkez" row = ministries' central accounting units; il "Toplam" =
    HMB's own total row (checked against the sum of rows, see meta.kontroller).

    Args:
        tablo: one of the seven names above.
        yil: Year. 0 = latest year with files.
        max_rows: Preview rows (latest period first; max 500).
        output_path: Optional CSV path for the full table.
        ay: Optional period-end month filter 1-12 (e.g. 6 for II. DÖNEM).
        il_kodu: Optional province filter ("34", "00" = Merkez).
    """
    from turkiye_veri_mcp import hmb

    frame = hmb.HmbClient().get_karsilastirma_data(tablo=tablo, yil=yil or None)
    frame = hmb.filter_frame(frame, ay=ay, il_kodu=il_kodu)
    return _json(hmb.tool_payload(frame, max_rows, output_path))


@app.tool()
@_track
def hmb_get_paylar(tablo: str = "mahalli", yil: int = 0, max_rows: int = 40, output_path: str = "") -> str:
    """HMB: shares transferred from general-budget tax revenue to local
    governments and funds ("Genel Bütçe Vergi Gelirlerinden Mahalli İdare
    ve Fonlara Aktarılan Paylar"), 2003-latest, plus 1990-2005 annual totals.

    Values are MONTHLY flows (ay içi), verified per file: each row's months
    sum to HMB's "Toplam" column (see meta.kontroller). Months not yet
    published are dropped (HMB fills them with formula zeros). Unit bin TL
    ("Milyar TL" in 2003-2005 files = bin TL numerically).

    Args:
        tablo: "mahalli" (İller Bankası / büyükşehir belediye payları),
            "fon_detay1" (fund shares), "fon_detay2" (fund shares by legal
            source), or "tarihsel" (1990-2005 annual realisations and % of
            GNP; yil ignored).
        yil: Year 2003-latest. 0 = latest year with files.
        max_rows: Preview rows (latest period first; max 500).
        output_path: Optional CSV path for the full table.
    """
    from turkiye_veri_mcp import hmb

    frame = hmb.HmbClient().get_paylar(tablo=tablo, yil=yil or None)
    return _json(hmb.tool_payload(frame, max_rows, output_path))


@app.tool()
@_track
def hmb_list_tables(bolum: str = "", ara: str = "", yil: int = 0, max_items: int = 60) -> str:
    """Browse everything HMB Muhasebat publishes as statistics (live).

    Without bolum: lists all statistics sections (Merkezi Yönetim, Genel
    Bütçe, Özel Bütçe, Mahalli İdareler, SGK, Genel Yönetim, GFS, İller
    İtibarıyla..., Paylar, 1994-2005 historic...) with their slug.
    With bolum (slug or part of the title): lists that section's folder
    tree with klasor_id + klasor_adi; pass those to hmb_list_files, then
    a file's url to hmb_get_file.

    Args:
        bolum: Section slug or title fragment ("" = list sections).
        ara: Optional text filter on the folder path (case/diacritic-insensitive).
        yil: Optional year filter (folder path must contain this year).
        max_items: Max rows returned.
    """
    from turkiye_veri_mcp import hmb

    client = hmb.HmbClient()
    sections = client.sections()
    if not bolum:
        rows = [s for s in sections if not ara or hmb.fold(ara) in hmb.fold(s["yol"] + " " + s["baslik"])]
        return _json({"bolumler": rows[:max_items], "n": len(rows),
                      "not": "hmb_list_tables(bolum=<slug>) ile klasör ağacını görün."})
    key = hmb.fold(bolum)
    match = [s for s in sections if s["slug"] == bolum.strip("/")] or [
        s for s in sections if key in hmb.fold(s["baslik"]) or key in s["slug"]
    ]
    slug = match[0]["slug"] if len(match) >= 1 else bolum.strip("/")
    rows = hmb.tree_to_rows(client.section_tree(slug))
    if ara:
        rows = [r for r in rows if hmb.fold(ara) in hmb.fold(r["yol"])]
    if yil:
        rows = [r for r in rows if str(yil) in r["yol"]]
    return _json({
        "bolum": slug,
        "eslesen_bolumler": [m["slug"] for m in match][:10],
        "n": len(rows),
        "klasorler": rows[:max_items],
        "not": "alt_klasor=false olan klasörler dosya içerir: hmb_list_files(klasor_id, klasor_adi).",
    })


@app.tool()
@_track
def hmb_list_files(klasor_id: int, klasor_adi: str) -> str:
    """List the files (label, url, extension) in one HMB folder.

    Args:
        klasor_id: Folder id from hmb_list_tables.
        klasor_adi: The folder's klasor_adi from hmb_list_tables, verbatim
            (HMB rejects the request if the name differs, even by a leading space).
    """
    from turkiye_veri_mcp import hmb

    files = hmb.HmbClient().list_files(klasor_id, klasor_adi)
    return _json({
        "n": len(files),
        "dosyalar": [{"etiket": f.etiket, "url": f.url, "uzanti": f.uzanti} for f in files],
        "not": "Bir dosyayı tablo olarak almak için hmb_get_file(url=...).",
    })


@app.tool()
@_track
def hmb_get_file(
    url: str,
    max_rows: int = 40,
    output_path: str = "",
    ham_path: str = "",
    etiket: str = "",
    birim: str = "",
) -> str:
    """Fetch any HMB statistics file (ms.hmb.gov.tr URL from hmb_list_files).

    If the layout is one of the recognised, validated ones (province revenue
    statement; province x category tables; item x month/year tables such as
    the national Merkezi Yönetim denge / gelir / Ekod4 / Fkod3 tables and
    paylar; one-sheet-per-month crosstabs such as Eko_Fonk) a tidy table is
    returned with unit, period (donem) and period type (aylık / kümülatif /
    yıllık / belirsiz). Otherwise NO table is guessed: the reply explains
    why and, if ham_path is given, the original file is saved there.

    Args:
        url: File URL on *.hmb.gov.tr.
        max_rows: Preview rows (max 500).
        output_path: Optional CSV path for the full tidy table.
        ham_path: Optional path to save the original file (xls/xlsx/pdf...).
        etiket: Optional file label from hmb_list_files (helps detect
            province code / year when the sheet title lacks them).
        birim: Only for files that carry NO unit label at all (e.g. 2026
            Fkod3): your assertion of the unit, "Bin TL" or "Bin YTL". It
            is recorded in meta.kaynak_birim as a user assertion and is
            ignored when the file has its own unit label.
    """
    from turkiye_veri_mcp import hmb

    frame, content, meta = hmb.HmbClient().fetch_file(url, dosya_etiketi=etiket, birim_beyani=birim)
    extra: dict = {"kaynak_url": url}
    if ham_path:
        path = Path(ham_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        extra["ham_dosya"] = str(path.resolve())
    if frame is None:
        return _json({
            "tanindi": False,
            **meta,
            **extra,
            "oneri": "Ham dosya kaydedildi." if ham_path else "Ham dosya için ham_path verin.",
        })
    return _json({"tanindi": True, **hmb.tool_payload(frame, max_rows, output_path, **extra)})


def main() -> None:
    """Entry point. Defaults to stdio (Claude Code / Desktop); pass
    --transport http to serve over HTTP for claude.ai custom connectors."""
    import argparse

    parser = argparse.ArgumentParser(prog="turkiye-veri-mcp")
    parser.add_argument(
        "--transport",
        default=os.environ.get("MCP_TRANSPORT", "stdio"),
        choices=["stdio", "http", "sse"],
        help="stdio (default, local clients) or http (hosted, remote clients)",
    )
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = parser.parse_args()

    if args.transport == "stdio":
        app.run()
        return

    transport = "streamable-http" if args.transport == "http" else "sse"
    app.run(transport=transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
