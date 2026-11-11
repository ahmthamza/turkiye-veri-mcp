"""Tests for the TCMB EVDS client.

Offline tests use small synthetic payloads shaped like real EVDS responses.
Live tests (marked ``live``) need EVDS_API_KEY and are skipped without it.
"""

from __future__ import annotations

import os
from datetime import date

import httpx
import pandas as pd
import pytest

from turkiye_veri_mcp import evds
from turkiye_veri_mcp.evds import (
    EvdsClient,
    EvdsError,
    _fold,
    _parse_response,
    _per_series_param,
    _resolve,
    column_name,
    frame_from_items,
    period_start,
    to_long,
)

live = pytest.mark.live
needs_key = pytest.mark.skipif(not os.environ.get("EVDS_API_KEY"), reason="EVDS_API_KEY not set")


def _item(tarih, values, unixtime=None, yearweek=None):
    row = {"Tarih": tarih}
    if yearweek:
        row["YEARWEEK"] = yearweek
    row.update(values)
    if unixtime is not None:
        row["UNIXTIME"] = {"$numberLong": str(unixtime)}
    return row


# -- pure helpers -------------------------------------------------------------


def test_period_start_all_label_formats():
    assert period_start("02-01-2024") == date(2024, 1, 2)
    assert period_start("2023-1") == date(2023, 1, 1)
    assert period_start("2023-12") == date(2023, 12, 1)
    assert period_start("2022-Q3") == date(2022, 7, 1)
    assert period_start("2023-S2") == date(2023, 7, 1)
    assert period_start(2022) == date(2022, 1, 1)
    assert period_start("2022") == date(2022, 1, 1)
    assert period_start("garbage") is None
    assert period_start(None) is None


def test_column_name_formula_suffix():
    assert column_name("TP.FG.J0") == "TP_FG_J0"
    assert column_name("TP.FG.J0", "0") == "TP_FG_J0"
    assert column_name("TP.FG.J0", 3) == "TP_FG_J0-3"


def test_turkish_fold():
    assert _fold("İşsizlik Oranı") == "issizlik orani"
    assert _fold("ISSIZLIK") == "issizlik"
    assert _fold("işgücü") == _fold("İŞGÜCÜ") == "isgucu"
    assert _fold("(USD)  ABD  Doları") == "(usd) abd dolari"


def test_resolve_and_per_series_params():
    assert _resolve(evds.FREQUENCIES, "Aylık", "frequency") == "5"
    assert _resolve(evds.FREQUENCIES, 8, "frequency") == "8"
    with pytest.raises(ValueError):
        _resolve(evds.FREQUENCIES, "hourly", "frequency")
    with pytest.raises(ValueError):
        _resolve(evds.FREQUENCIES, 99, "frequency")
    assert _per_series_param("yillik_yuzde", 2, evds.FORMULAS, "formula") == "3-3"
    assert _per_series_param("duzey,yoy_pct", 2, evds.FORMULAS, "formula") == "0-3"
    assert _per_series_param("avg, last", 2, None, "aggregation") == "avg-last"
    assert _per_series_param("", 2, None, "aggregation") == ""
    with pytest.raises(ValueError):
        _per_series_param("avg,last,sum", 2, None, "aggregation")
    with pytest.raises(ValueError):
        _per_series_param("median", 1, None, "aggregation")


def test_date_normalisation():
    assert evds._normalize_date("2024-01-31") == "31-01-2024"
    assert evds._normalize_date("31.01.2024") == "31-01-2024"
    with pytest.raises(ValueError):
        evds._normalize_date("Jan 2024")


# -- frame building -----------------------------------------------------------


def test_frame_numeric_nulls_sorting_and_dedup():
    items = [
        _item("2023-10", {"TP_A": "3.5", "TP_B": None}, 1696107600),
        _item("2023-2", {"TP_A": "1.25", "TP_B": "ND"}, 1675198800),
        _item("2023-2", {"TP_A": "1.25", "TP_B": "ND"}, 1675195200),  # DST-shifted dup
        _item("2023-1", {"TP_A": "1.00000000", "TP_B": "7"}, 1672520400),
    ]
    frame = evds._finish(frame_from_items(items, expected_columns=["TP_B", "TP_A"]))
    assert list(frame.columns) == ["Tarih", "TP_B", "TP_A"]
    # chronological, not lexicographic ('2023-10' after '2023-2')
    assert frame["Tarih"].tolist() == ["2023-1", "2023-2", "2023-10"]
    assert frame["TP_A"].tolist() == [1.0, 1.25, 3.5]
    assert frame["TP_B"].isna().tolist() == [False, True, True]
    assert frame.attrs["non_numeric_values"] == 0


def test_frame_counts_unparseable_values():
    items = [_item("2023-1", {"TP_A": "abc"}, 1672520400)]
    frame = frame_from_items(items)
    assert frame.attrs["non_numeric_values"] == 1


def test_weekly_frame_keeps_yearweek_and_drops_unixtime():
    items = [
        _item("12-01-2024", {"TP_W": "2"}, 1705006800, yearweek="2024-2"),
        _item("05-01-2024", {"TP_W": "1"}, 1704402000, yearweek="2024-1"),
    ]
    frame = evds._finish(frame_from_items(items))
    assert list(frame.columns) == ["Tarih", "YEARWEEK", "TP_W"]
    assert frame["YEARWEEK"].tolist() == ["2024-1", "2024-2"]


def test_annual_int_labels():
    items = [_item(2023, {"TP_Y": "2"}, 1672520400), _item(2022, {"TP_Y": "1"}, 1640984400)]
    frame = evds._finish(frame_from_items(items))
    assert frame["Tarih"].tolist() == [2022, 2023]


def test_empty_items_raise():
    with pytest.raises(EvdsError):
        frame_from_items([])


def test_to_long():
    wide = pd.DataFrame({"Tarih": ["2023-1", "2023-Q2"], "TP_FG_J0-3": [1.0, None], "TP_X": [2.0, 3.0]})
    long = to_long(wide, {"TP_FG_J0": "TP.FG.J0", "TP_X": "TP.X"})
    assert list(long.columns) == ["Tarih", "date", "series", "column", "value"]
    assert len(long) == 3  # null dropped
    assert set(long["series"]) == {"TP.FG.J0", "TP.X"}
    assert long.loc[long["Tarih"] == "2023-Q2", "date"].iloc[0] == "2023-04-01"


# -- paging past the 1000-row cap (fake transport) ------------------------------


class _FakeEvds(EvdsClient):
    """Serves a synthetic daily series and mimics EVDS's cap: when more than
    `cap` rows match, only the latest `cap` rows are returned."""

    REQUEST_DELAY = 0.0

    def __init__(self, n_days: int, cap: int = 1000) -> None:
        super().__init__(api_key="x")
        self.cap = cap
        base = date(2000, 1, 1).toordinal()
        self.days = [date.fromordinal(base + i) for i in range(n_days)]
        self.calls: list[dict] = []

    def _request(self, path, params):
        self.calls.append(dict(params))
        if path == "serieList/":
            return [{"SERIE_CODE": "TP.OK"}] if params["code"] == "TP.OK" else []
        start = pd.to_datetime(params["startDate"], dayfirst=True).date()
        end = pd.to_datetime(params["endDate"], dayfirst=True).date()
        rows = [d for d in self.days if start <= d <= end]
        rows = rows[-self.cap:]
        items = [
            {"Tarih": d.strftime("%d-%m-%Y"), "TP_OK": str(d.toordinal()), "UNIXTIME": {"$numberLong": "0"}}
            for d in rows
        ]
        if "TP.MISSING" in params.get("series", ""):
            pass  # EVDS drops unknown codes silently: no column
        return {"totalCount": len(items), "items": items}


def test_paging_reassembles_everything(monkeypatch):
    monkeypatch.setattr(evds, "MAX_ROWS_PER_RESPONSE", 100)
    client = _FakeEvds(n_days=350, cap=100)
    frame = client.get_data(["TP.OK"], start="01-01-2000", end="31-12-2001")
    assert len(frame) == 350
    assert frame["Tarih"].iloc[0] == "01-01-2000"
    assert frame["TP_OK"].is_monotonic_increasing
    assert not frame["Tarih"].duplicated().any()
    assert len(client.calls) == 4  # 100 + 100 + 100 + 50


def test_last_n_stops_early(monkeypatch):
    monkeypatch.setattr(evds, "MAX_ROWS_PER_RESPONSE", 100)
    client = _FakeEvds(n_days=350, cap=100)
    frame = client.get_data(["TP.OK"], last_n=5, end="31-12-2001")
    assert len(frame) == 5
    assert len(client.calls) == 1
    assert frame["Tarih"].iloc[-1] == client.days[-1].strftime("%d-%m-%Y")


def test_missing_column_names_unknown_code(monkeypatch):
    client = _FakeEvds(n_days=10)
    with pytest.raises(EvdsError, match="TP.MISSING"):
        client.get_data(["TP.OK", "TP.MISSING"], start="01-01-2000", end="10-01-2000")


def test_requires_start_or_last_n():
    with pytest.raises(ValueError):
        _FakeEvds(n_days=1).get_data(["TP.OK"])


# -- error mapping --------------------------------------------------------------


def _resp(status, text, ctype="application/json"):
    return httpx.Response(status, text=text, headers={"content-type": ctype})


def test_error_messages_are_actionable():
    with pytest.raises(EvdsError, match="API key"):
        _parse_response(_resp(401, "Invalid API Key", "text/plain"))
    with pytest.raises(EvdsError, match="Series does not exist"):
        _parse_response(_resp(400, '{"status":"400","message":"Bad Request. Series does not exist."}'))
    with pytest.raises(EvdsError, match="rate limit"):
        _parse_response(_resp(429, ""))
    with pytest.raises(EvdsError, match="non-JSON"):
        _parse_response(_resp(200, "<html><body>Bakim</body></html>", "text/html"))
    assert _parse_response(_resp(200, '{"items": []}')) == {"items": []}


def test_missing_key_message(monkeypatch):
    monkeypatch.delenv("EVDS_API_KEY", raising=False)
    with pytest.raises(EvdsError, match="EVDS_API_KEY"):
        EvdsClient(api_key="").categories()


# -- cache location ---------------------------------------------------------------


def test_cache_dir_falls_back_when_unwritable(monkeypatch, tmp_path):
    blocked = tmp_path / "file-not-dir"
    blocked.write_text("x")
    monkeypatch.setattr(evds, "_CACHE_DIR", [])
    monkeypatch.setenv("TURKIYE_VERI_CACHE", str(blocked / "sub"))
    monkeypatch.setattr(evds.Path, "home", lambda: blocked)
    monkeypatch.setattr(evds.tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    assert evds._cache_dir() == tmp_path / "tmp" / "turkiye-veri-mcp"


def test_cache_dir_memory_only(monkeypatch, tmp_path):
    blocked = tmp_path / "file-not-dir"
    blocked.write_text("x")
    monkeypatch.setattr(evds, "_CACHE_DIR", [])
    monkeypatch.delenv("TURKIYE_VERI_CACHE", raising=False)
    monkeypatch.setattr(evds.Path, "home", lambda: blocked)
    monkeypatch.setattr(evds.tempfile, "gettempdir", lambda: str(blocked))
    assert evds._cache_dir() is None


# -- index build: resumable and robust ---------------------------------------------


class _FakeIndexEvds(EvdsClient):
    REQUEST_DELAY = 0.0

    def __init__(self, fail: set[str]) -> None:
        super().__init__(api_key="x")
        self.fail = fail
        self.calls: list[str] = []

    def _raw_datagroups(self):
        return [
            {"DATAGROUP_CODE": "bie_a", "DATAGROUP_NAME": "İşsizlik", "DATAGROUP_NAME_ENG": "Unemployment", "CATEGORY_ID": 1.0},
            {"DATAGROUP_CODE": "bie_b", "DATAGROUP_NAME": "Kurlar", "DATAGROUP_NAME_ENG": "Rates", "CATEGORY_ID": 2.0},
        ]

    def _request(self, path, params):
        code = params["code"]
        self.calls.append(code)
        if code in self.fail:
            raise EvdsError("HTTP 500")
        return [{"SERIE_CODE": f"TP.{code.upper()}", "SERIE_NAME": "İşsizlik Oranı", "SERIE_NAME_ENG": "Unemployment rate"}]


@pytest.fixture
def isolated_index(monkeypatch, tmp_path):
    monkeypatch.setattr(evds, "_CACHE_DIR", [tmp_path])
    monkeypatch.setattr(evds, "_MEMORY_INDEX", {})
    return tmp_path


def test_index_build_survives_failures_and_search_folds(isolated_index):
    client = _FakeIndexEvds(fail={"bie_b"})
    rows = client.build_series_index(delay=0)
    assert [r["serie_code"] for r in rows] == ["TP.BIE_A"]
    assert "bie_b" in evds._load_index()["failed"]
    assert client.calls.count("bie_b") == 2  # retried once
    hits, n, _ = client.search_series("issizlik orani")
    assert n == 1 and hits[0]["name"] == "İşsizlik Oranı"
    hits, n, _ = client.search_series("unemployment", lang="ENG")
    assert n == 1 and hits[0]["name"] == "Unemployment rate"


def test_index_build_resumes_from_partial(isolated_index):
    evds._write_json(
        evds._cache_file(evds._PARTIAL_NAME),
        {"started": __import__("time").time(), "done": {"bie_a": []}},
    )
    client = _FakeIndexEvds(fail=set())
    client.build_series_index(delay=0)
    assert client.calls == ["bie_b"]


def test_search_without_index_builds_in_background(isolated_index, monkeypatch):
    monkeypatch.setattr(evds, "_BUILD_STATUS", {"running": False, "done": 0, "total": 0, "error": ""})
    client = _FakeIndexEvds(fail=set())
    with pytest.raises(evds.IndexNotReady) as info:
        client.search_series("issizlik", wait=False)
    assert "index_ready" in info.value.status
    for _ in range(100):  # background thread finishes quickly on fake data
        if evds._load_index() is not None and not evds._BUILD_STATUS["running"]:
            break
        __import__("time").sleep(0.05)
    hits, n, _ = client.search_series("issizlik", wait=False)
    assert n == 2


# -- live --------------------------------------------------------------------------


@live
@needs_key
def test_live_usd_matches_small_request():
    client = EvdsClient()
    frame = client.get_data(["TP.DK.USD.A.YTL"], start="2024-01-01", end="2024-01-10")
    assert frame["Tarih"].tolist()[0] == "01-01-2024"
    assert len(frame) == 10
    assert frame["TP_DK_USD_A_YTL"].iloc[1] == pytest.approx(29.4382)


@live
@needs_key
def test_live_paging_past_1000_rows():
    client = EvdsClient()
    frame = client.get_data(["TP.DK.USD.A.YTL"], start="2020-01-01", end="2023-12-31")
    assert len(frame) == (date(2023, 12, 31) - date(2020, 1, 1)).days + 1
    assert not frame["Tarih"].duplicated().any()


@live
@needs_key
def test_live_formula_column_and_monthly_labels():
    client = EvdsClient()
    frame = client.get_data(["TP.FG.J0"], start="2023-01-01", end="2023-12-31", formula="yillik_yuzde")
    assert list(frame.columns) == ["Tarih", "TP_FG_J0-3"]
    assert frame["Tarih"].tolist()[-1] == "2023-12"
    assert frame["TP_FG_J0-3"].iloc[0] == pytest.approx(57.68248103)


@live
@needs_key
def test_live_unknown_code_is_named():
    client = EvdsClient()
    with pytest.raises(EvdsError, match="TP.NOPE.XYZ"):
        client.get_data(["TP.FG.J0", "TP.NOPE.XYZ"], start="2023-01-01", end="2023-03-31")


@live
@needs_key
def test_live_datagroup_equals_series_request():
    client = EvdsClient()
    group, meta = client.get_datagroup_data("bie_dkdovytl", start="2024-01-01", end="2024-01-05")
    codes = [m["serie_code"] for m in meta]
    direct = client.get_data(codes, start="2024-01-01", end="2024-01-05")
    pd.testing.assert_frame_equal(group, direct)


@live
@needs_key
def test_live_invalid_key():
    with pytest.raises(EvdsError, match="401"):
        EvdsClient(api_key="invalid-key-for-test").categories()
