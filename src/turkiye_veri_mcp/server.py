"""Türkiye Veri MCP: TUIK (Turkish Statistical Institute) and TCMB EVDS
(Central Bank Electronic Data Delivery System) in a single MCP server."""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

try:  # MCP SDK >= 2.0
    from mcp.server.mcpserver import MCPServer
except ImportError:  # MCP SDK 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer

from turkiye_veri_mcp import sdmx, usage
from turkiye_veri_mcp.evds import EvdsClient
from turkiye_veri_mcp.tidy import TidyError, tidy_istab
from turkiye_veri_mcp.portal import PORTAL_BASE, _UA, PortalClient

app = MCPServer(
    "turkiye-veri-mcp",
    instructions=(
        "Official Turkish statistics from two sources. "
        "TUIK (tuik_* tools): themes -> tables -> SDMX dataflows; use "
        "tuik_describe_dataflow to learn dimensions/keys, tuik_get_data for "
        "previews, tuik_download_data for full CSVs. "
        "TCMB EVDS (evds_* tools, requires free EVDS_API_KEY): categories -> "
        "datagroups -> series; evds_get_data supports frequency conversion, "
        "aggregation and formulas (yoy %, diff, moving avg...). "
        "For research pipelines prefer the *_download_data tools (tidy CSV)."
    ),
)

_portal = PortalClient()
_evds = EvdsClient()

_MAX_LIST = 60
_MAX_CODES_SHOWN = 40


def _json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=1, default=str)


def _track(func):
    """Log one usage line before running the tool. Never blocks the call."""
    import functools

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        usage.record(func.__name__)
        return func(*args, **kwargs)

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
def tuik_list_tables(theme_id: str, lang: str = "tr", resource_type: str = "all") -> str:
    """List tables under a TUIK theme: SDMX 'dataflow' entries (use dataflow_id
    with tuik_get_data/tuik_download_data) or 'istab' file downloads (use url
    with tuik_download_table_file).

    Args:
        theme_id: Theme id from tuik_list_themes (e.g. '11').
        lang: 'tr' or 'en'.
        resource_type: 'dataflow', 'istab', 'database' (legacy MEDAS/Biruni,
            link only), or 'all' (default: all three).
    """
    types = (
        ("dataflow", "istab", "database")
        if resource_type == "all"
        else (resource_type,)
    )
    rows = _portal.list_resources(theme_id, lang=lang, types=types)
    return _json({"n_tables": len(rows), "tables": rows})


@app.tool()
@_track
def tuik_search_tables(query: str, lang: str = "tr") -> str:
    """Search TUIK table names across all themes (case-insensitive substring).

    Args:
        query: Search text, e.g. 'işgücü' or 'tüketici fiyat'.
        lang: Language of table names to search in ('tr' or 'en').
    """
    return _json(_truncated(_portal.search(query, lang=lang)))


@app.tool()
@_track
def tuik_describe_dataflow(dataflow_id: str, lang: str = "tr") -> str:
    """Describe a TUIK SDMX dataflow: dimensions, codelists, key template.

    Use before tuik_get_data when a dataflow refuses key='ALL' or to build a
    narrower key. The SDMX key joins one code per non-time dimension with
    dots, in position order; an empty slot means 'all' (e.g. 'TR..2021').

    Args:
        dataflow_id: Three-part SDMX id, e.g. 'TR,DF_ADNKS_T26,1.0'.
        lang: Label language for codelists ('tr' or 'en').
    """
    structure = sdmx.fetch_structure(dataflow_id, lang=lang)
    for dim in structure["dimensions"]:
        codes = dim["codes"]
        dim["n_codes"] = len(codes)
        if len(codes) > _MAX_CODES_SHOWN:
            dim["codes"] = dict(list(codes.items())[:_MAX_CODES_SHOWN])
            dim["codes_truncated"] = True
    return _json(structure)


@app.tool()
@_track
def tuik_get_data(
    dataflow_id: str,
    key: str = "ALL",
    start: str = "",
    end: str = "",
    max_rows: int = 50,
) -> str:
    """Fetch observations from a TUIK SDMX dataflow (all dimensions, full
    time span unless narrowed) and preview them in chat.

    Args:
        dataflow_id: Three-part SDMX id, e.g. 'TR,DF_ADNKS_T26,1.0'.
        key: SDMX key path ('ALL' or dot-separated codes like 'TR..2021').
        start: Optional start period (e.g. '2010' or '2020-01').
        end: Optional end period.
        max_rows: Rows to include in the preview (default 50).
    """
    frame = sdmx.fetch_data(dataflow_id, key=key, start=start or None, end=end or None)
    return _json(
        {
            "n_rows": int(frame.shape[0]),
            "n_cols": int(frame.shape[1]),
            "columns": list(frame.columns),
            "preview_rows": min(max_rows, int(frame.shape[0])),
            "preview_csv": frame.head(max_rows).to_csv(index=False),
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
) -> str:
    """Download a full TUIK SDMX dataset and write it as a tidy CSV file.

    Args:
        dataflow_id: Three-part SDMX id, e.g. 'TR,DF_ADNKS_T26,1.0'.
        output_path: Where to write the CSV (e.g. 'data/adnks_t26.csv').
        key: SDMX key path ('ALL' or dot-separated codes).
        start: Optional start period.
        end: Optional end period.
    """
    frame = sdmx.fetch_data(dataflow_id, key=key, start=start or None, end=end or None)
    path = Path(output_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False)
    return _json(
        {
            "written": str(path.resolve()),
            "n_rows": int(frame.shape[0]),
            "n_cols": int(frame.shape[1]),
            "columns": list(frame.columns),
        }
    )


@app.tool()
@_track
def tuik_download_table_file(url: str, output_path: str) -> str:
    """Download an 'istab' table file (usually Excel) from the TUIK portal.

    Args:
        url: The url field of an istab row from tuik_list_tables/tuik_search_tables.
        output_path: Where to save the file (e.g. 'data/table.xlsx').
    """
    path = Path(output_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    download_headers = {
        "User-Agent": _UA,
        "Accept": "*/*",
        "Referer": f"{PORTAL_BASE}/tr/statistical-themes",
    }
    with httpx.Client(timeout=180.0, follow_redirects=True, headers=download_headers) as client:
        response = client.get(url)
        response.raise_for_status()
        path.write_bytes(response.content)
    return _json(
        {
            "written": str(path.resolve()),
            "bytes": len(response.content),
            "content_type": response.headers.get("content-type"),
        }
    )


@app.tool()
@_track
def tuik_get_table_data(url: str, output_path: str = "", max_rows: int = 50) -> str:
    """Download an 'istab' Excel table and convert it to tidy long format.

    Handles TUIK's common layouts (title/footnote rows, year-per-sheet
    workbooks, crosstabs with label columns and period headers). Output
    columns: the table's own label columns plus 'donem' and 'deger'.
    Layouts vary, so check tidy_confidence; tuik_download_table_file always
    gets you the raw file instead.

    Args:
        url: The url field of an istab row from tuik_list_tables/tuik_search_tables.
        output_path: Optional CSV path; if given, the tidy table is written there.
        max_rows: Rows to include in the preview (default 50).
    """
    download_headers = {
        "User-Agent": _UA,
        "Accept": "*/*",
        "Referer": f"{PORTAL_BASE}/tr/statistical-themes",
    }
    with httpx.Client(timeout=180.0, follow_redirects=True, headers=download_headers) as client:
        response = client.get(url)
        response.raise_for_status()
    try:
        frame, report = tidy_istab(response.content)
    except TidyError as exc:
        return _json(
            {
                "error": str(exc),
                "hint": "Bu tablo bilinen şablonlara uymuyor; ham dosya için "
                        "tuik_download_table_file kullanın.",
            }
        )
    result = {
        "n_rows": int(frame.shape[0]),
        "columns": list(frame.columns),
        **report,
        "preview_csv": frame.head(max_rows).to_csv(index=False),
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
def evds_categories(lang: str = "TR") -> str:
    """List TCMB EVDS main categories (exchange rates, interest rates,
    inflation, balance of payments, surveys...).

    Args:
        lang: 'TR' or 'ENG'.
    """
    return _json(_evds.categories(lang=lang))


@app.tool()
@_track
def evds_datagroups(category_id: str = "", query: str = "", lang: str = "TR") -> str:
    """List or search EVDS datagroups (thematic bundles of series).

    Provide category_id to list one category's datagroups, query to search
    all datagroup names, or neither to list everything (truncated).

    Args:
        category_id: A category id from evds_categories (optional).
        query: Substring to search in datagroup names (optional).
        lang: 'TR' or 'ENG'.
    """
    if query:
        rows = _evds.search_datagroups(query, lang=lang)
    else:
        rows = _evds.datagroups(category_id or None, lang=lang)
    return _json(_truncated(rows))


@app.tool()
@_track
def evds_series_list(datagroup_code: str, lang: str = "TR") -> str:
    """List all series in an EVDS datagroup with codes and start dates.

    Args:
        datagroup_code: Datagroup code from evds_datagroups (e.g. 'bie_dkdovytl').
        lang: 'TR' or 'ENG'.
    """
    return _json(_truncated(_evds.series_list(datagroup_code, lang=lang), 200))


@app.tool()
@_track
def evds_search_series(
    query: str, lang: str = "TR", limit: int = 50, refresh: bool = False
) -> str:
    """Search EVDS series by name across every datagroup.

    EVDS has no server-side series search, so the first call walks all
    datagroups once (a few minutes) and caches the index locally; later
    searches are instant. Multiple words are ANDed.

    Args:
        query: Words to look for, e.g. 'konut fiyat' or 'issizlik'.
        lang: 'TR' or 'ENG'.
        limit: Maximum hits to return (default 50).
        refresh: Rebuild the index now instead of using the cache. The index
            also rebuilds itself automatically once it is a week old, so newly
            published TCMB series appear without any manual step.
    """
    hits, n_total, built_now = _evds.search_series(
        query, lang=lang, limit=limit, refresh=refresh
    )
    return _json(
        {
            "n_hits": n_total,
            "truncated": n_total > len(hits),
            "index_built_this_call": built_now,
            "index_age_days": _evds.index_age_days(lang),
            "hits": hits,
        }
    )


@app.tool()
@_track
def evds_get_datagroup_data(
    datagroup_code: str,
    start: str,
    output_path: str = "",
    end: str = "",
    frequency: str = "",
    aggregation: str = "",
    formula: str = "",
    max_rows: int = 30,
) -> str:
    """Fetch every series in an EVDS datagroup at once (auto-chunked).

    Mirrors the website's 'download the whole datagroup' action. Large groups
    and long spans are split into several requests and stitched back together,
    so EVDS response limits do not silently truncate the result.

    Args:
        datagroup_code: Datagroup code from evds_datagroups (e.g. 'bie_dkdovytl').
        start: Start date (dd-mm-yyyy or yyyy-mm-dd).
        output_path: Optional CSV path for the full table.
        end: End date (defaults to start).
        frequency: Target frequency name or code 1-8.
        aggregation: avg/min/max/first/last/sum.
        formula: Transformation name or code 0-8.
        max_rows: Rows in the preview (default 30).
    """
    frame, series_meta = _evds.get_datagroup_data(
        datagroup_code, start=start, end=end,
        frequency=frequency, aggregation=aggregation, formula=formula, lang="TR",
    )
    result = {
        "datagroup_code": datagroup_code,
        "n_series": len(series_meta),
        "n_rows": int(frame.shape[0]),
        "n_cols": int(frame.shape[1]),
        "preview_csv": frame.head(max_rows).to_csv(index=False),
    }
    if output_path:
        path = Path(output_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(path, index=False)
        result["written"] = str(path.resolve())
    return _json(result)


@app.tool()
@_track
def evds_get_data(
    series: list[str],
    start: str,
    end: str = "",
    frequency: str = "",
    aggregation: str = "",
    formula: str = "",
    max_rows: int = 50,
) -> str:
    """Fetch one or more EVDS series with full API features and preview in chat.

    Args:
        series: EVDS series codes, e.g. ['TP.DK.USD.A.YTL', 'TP.FG.J0'].
        start: Start date (dd-mm-yyyy or yyyy-mm-dd).
        end: End date (defaults to start).
        frequency: Target frequency: gunluk/isgunu/haftalik/ayda2/aylik/
            ceyreklik/altiaylik/yillik (or codes 1-8). Empty = series default.
        aggregation: How to aggregate when reducing frequency:
            avg, min, max, first, last, sum.
        formula: Transformation: duzey, yuzde_degisim, fark, yillik_yuzde,
            yillik_fark, yilsonu_yuzde, yilsonu_fark, hareketli_ortalama,
            hareketli_toplam (or codes 0-8). Empty = level.
        max_rows: Rows in the preview (default 50).
    """
    frame = _evds.get_data_chunked(
        series, start=start, end=end,
        frequency=frequency, aggregation=aggregation, formula=formula,
    )
    return _json(
        {
            "n_rows": int(frame.shape[0]),
            "n_cols": int(frame.shape[1]),
            "columns": list(frame.columns),
            "preview_rows": min(max_rows, int(frame.shape[0])),
            "preview_csv": frame.head(max_rows).to_csv(index=False),
        }
    )


@app.tool()
@_track
def evds_download_data(
    series: list[str],
    start: str,
    output_path: str,
    end: str = "",
    frequency: str = "",
    aggregation: str = "",
    formula: str = "",
) -> str:
    """Fetch EVDS series and write them as a tidy CSV file (for analysis
    pipelines). Same parameters as evds_get_data.

    Args:
        series: EVDS series codes.
        start: Start date (dd-mm-yyyy or yyyy-mm-dd).
        output_path: Where to write the CSV (e.g. 'data/usd_try.csv').
        end: End date (defaults to start).
        frequency: Target frequency name or code 1-8.
        aggregation: avg/min/max/first/last/sum.
        formula: Transformation name or code 0-8.
    """
    frame = _evds.get_data_chunked(
        series, start=start, end=end,
        frequency=frequency, aggregation=aggregation, formula=formula,
    )
    path = Path(output_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False)
    return _json(
        {
            "written": str(path.resolve()),
            "n_rows": int(frame.shape[0]),
            "n_cols": int(frame.shape[1]),
            "columns": list(frame.columns),
        }
    )


@app.tool()
@_track
def usage_stats(days_recent: int = 7) -> str:
    """Show how much this MCP server has been used (this instance's log).

    Counts tool calls, not people: one session calling several tools logs
    several lines. On a hosted free-tier deployment the count resets on
    redeploy/restart, so treat it as a rough activity signal, not a durable
    total. Anthropic does not expose per-connector usage to server owners,
    so this local counter is the closest available substitute.

    Args:
        days_recent: Size of the "recent" window in days (default 7).
    """
    return _json(usage.summary(days_recent=days_recent))


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
