"""TÜİK (portal, SDMX/DataBrowser2, istab tidy) testleri.

Çevrimdışı testler küçük sabit girdilerle ayrıştırma mantığını sınar
(SDMX-JSON yapı mesajı, SDMX-CSV, dönem/filtre çözümleme, katalog arama,
sayı temizleme, Excel tidy). ``@pytest.mark.live`` testleri gerçek TÜİK
uç noktalarına (veriportali.tuik.gov.tr, databrowser2.tuik.gov.tr) bağlanır.
"""

from __future__ import annotations

import io
import json
from datetime import date

import httpx
import pandas as pd
import pytest
from openpyxl import Workbook

from turkiye_veri_mcp import portal, sdmx, tidy

DF = "TR,DF_TEST,1.0"


# ---------------------------------------------------------------- fixtures


def _dsd_payload() -> dict:
    """Minimal SDMX-JSON structure message, shaped like /download/structure."""
    cl = "urn:sdmx:org.sdmx.infomodel.codelist.Codelist="
    cs = "urn:sdmx:org.sdmx.infomodel.conceptscheme.Concept=TR:CONCEPTS(1.0)."
    return {
        "data": {
            "dataflows": [
                {
                    "id": "DF_TEST", "version": "1.0", "agencyID": "TR",
                    "names": {"tr": "Test akışı", "en": "Test flow"},
                    "structure": "urn:sdmx:org.sdmx.infomodel.datastructure.DataStructure=TR:DSD_TEST(1.0)",
                }
            ],
            "conceptSchemes": [
                {
                    "id": "CONCEPTS", "version": "1.0", "agencyID": "TR",
                    "concepts": [
                        {"id": "REF_AREA", "names": {"tr": "Coğrafi Kapsam", "en": "Reference area"}},
                        {"id": "SEX", "names": {"tr": "Cinsiyet", "en": "Sex"}},
                        {"id": "UNUSED", "names": {"tr": "Kullanılmayan"}},
                        {"id": "TIME_PERIOD", "names": {"tr": "Zaman", "en": "Time"}},
                        {"id": "OBS_STATUS", "names": {"tr": "Gözlem durumu"}},
                    ],
                }
            ],
            "codelists": [
                {"id": "CL_AREA", "version": "1.0", "agencyID": "TR", "codes": [
                    {"id": "TR", "names": {"tr": "Türkiye"}},
                    {"id": "NA", "names": {"tr": "Namibya", "en": "Namibia"}},
                ]},
                {"id": "CL_SEX", "version": "1.0", "agencyID": "TR", "codes": [
                    {"id": "_T", "names": {"tr": "Toplam", "en": "Total"}},
                    {"id": "1", "names": {"tr": "Erkek", "en": "Male"}},
                    {"id": "2", "names": {"tr": "Kadın", "en": "Female"}},
                ]},
                {"id": "CL_Z", "version": "1.0", "agencyID": "TR", "codes": [
                    {"id": "_Z", "names": {"tr": "Uygulanamaz"}},
                ]},
                {"id": "CL_OBS", "version": "1.0", "agencyID": "TR", "codes": [
                    {"id": "P", "names": {"tr": "Geçici", "en": "Provisional"}},
                ]},
            ],
            "dataStructures": [
                {
                    "id": "DSD_TEST", "version": "1.0", "agencyID": "TR",
                    "dataStructureComponents": {
                        "dimensionList": {
                            "dimensions": [
                                {"id": "SEX", "position": 1, "conceptIdentity": cs + "SEX",
                                 "localRepresentation": {"enumeration": cl + "TR:CL_SEX(1.0)"}},
                                {"id": "REF_AREA", "position": 0, "conceptIdentity": cs + "REF_AREA",
                                 "localRepresentation": {"enumeration": cl + "TR:CL_AREA(1.0)"}},
                                {"id": "UNUSED", "position": 2, "conceptIdentity": cs + "UNUSED",
                                 "localRepresentation": {"enumeration": cl + "TR:CL_Z(1.0)"}},
                            ],
                            "timeDimensions": [
                                {"id": "TIME_PERIOD", "position": 3, "conceptIdentity": cs + "TIME_PERIOD"}
                            ],
                        },
                        "attributeList": {
                            "attributes": [
                                {"id": "OBS_STATUS", "conceptIdentity": cs + "OBS_STATUS",
                                 "localRepresentation": {"enumeration": cl + "TR:CL_OBS(1.0)"}},
                                {"id": "UNIT_MULT", "conceptIdentity": cs + "UNIT_MULT"},
                            ]
                        },
                    },
                }
            ],
        }
    }


@pytest.fixture
def dsd() -> dict:
    return sdmx.parse_dsd_json(_dsd_payload(), DF)


_CSV = (
    "DATAFLOW,REF_AREA,SEX,UNUSED,TIME_PERIOD,OBS_VALUE,OBS_STATUS,UNIT_MULT\n"
    "TR:DF_TEST(1.0),TR,1,_Z,2024,10.5,,\n"
    "TR:DF_TEST(1.0),TR,2,_Z,2024,11.25,P,\n"
    "TR:DF_TEST(1.0),NA,_T,_Z,2024,,,\n"
    "TR:DF_TEST(1.0),TR,_T,_Z,2025,abc,,\n"
)


# ---------------------------------------------------------------- dataflow ids


@pytest.mark.parametrize(
    "raw",
    [
        "TR,DF_X,1.0",
        " TR,DF_X,1.0 ",
        "TR+DF_X+1.0",
        "https://databrowser2.tuik.gov.tr/#/tr/tuik/TR,DF_X,1.0",
    ],
)
def test_validate_dataflow_id_normalises(raw):
    assert sdmx.validate_dataflow_id(raw) == "TR,DF_X,1.0"


@pytest.mark.parametrize("raw", ["", "DF_X", "TR,DF_X", "TR,,1.0", "TR,DF X,1.0"])
def test_validate_dataflow_id_rejects(raw):
    with pytest.raises(ValueError):
        sdmx.validate_dataflow_id(raw)


# ---------------------------------------------------------------- DSD parsing


def test_parse_dsd_orders_dimensions_and_reads_labels(dsd):
    assert [d["id"] for d in dsd["dimensions"]] == ["REF_AREA", "SEX", "UNUSED"]
    assert dsd["time_dimension"] == "TIME_PERIOD"
    sex = next(d for d in dsd["dimensions"] if d["id"] == "SEX")
    assert sdmx.pick_label(sex["label"], "en", "SEX") == "Sex"
    assert sdmx.pick_label(sex["codes"]["2"], "tr", "2") == "Kadın"
    assert sdmx.pick_label(dsd["name"], "en", "") == "Test flow"


def test_pick_label_falls_back_to_turkish_then_code():
    assert sdmx.pick_label({"tr": "Türkiye"}, "en", "TR") == "Türkiye"
    assert sdmx.pick_label({}, "en", "TR") == "TR"
    assert sdmx.pick_label(None, "tr", "X") == "X"


# ---------------------------------------------------------------- periods


@pytest.mark.parametrize(
    "text,first,last",
    [
        ("2024", date(2024, 1, 1), date(2024, 12, 31)),
        ("2024-02", date(2024, 2, 1), date(2024, 2, 29)),
        ("2023-M02", date(2023, 2, 1), date(2023, 2, 28)),
        ("2024-Q2", date(2024, 4, 1), date(2024, 6, 30)),
        ("2024Q4", date(2024, 10, 1), date(2024, 12, 31)),
        ("2024-S2", date(2024, 7, 1), date(2024, 12, 31)),
        ("2024-03-15", date(2024, 3, 15), date(2024, 3, 15)),
    ],
)
def test_period_bounds(text, first, last):
    assert sdmx.period_bounds(text) == (first, last)


@pytest.mark.parametrize("text", ["", "abc", "2024-13", "2024-W05", "24"])
def test_period_bounds_unknown(text):
    assert sdmx.period_bounds(text) is None


# ---------------------------------------------------------------- selection


def test_parse_filters_forms():
    assert sdmx.parse_filters("SEX=1+2; ref_area=TR") == {"SEX": ["1", "2"], "REF_AREA": ["TR"]}
    assert sdmx.parse_filters('{"SEX": ["1"], "REF_AREA": "TR,NA"}') == {
        "SEX": ["1"], "REF_AREA": ["TR", "NA"],
    }
    assert sdmx.parse_filters({"SEX": "2"}) == {"SEX": ["2"]}
    assert sdmx.parse_filters("") == {}
    with pytest.raises(sdmx.SdmxError):
        sdmx.parse_filters("SEX")


def test_build_criteria_from_filters_and_period(dsd):
    criteria = sdmx.build_criteria(dsd, filters="SEX=1+2", start="2020", end="2024-Q2")
    assert criteria[0] == {"id": "SEX", "type": "CodeValues", "filterValues": ["1", "2"], "period": 0}
    assert criteria[1] == {"id": "TIME_PERIOD", "type": "TimeRange", "from": "2020-01-01", "to": "2024-06-30"}


def test_build_criteria_from_dot_key(dsd):
    criteria = sdmx.build_criteria(dsd, key="TR.1+2.")
    assert {c["id"]: c["filterValues"] for c in criteria} == {"REF_AREA": ["TR"], "SEX": ["1", "2"]}
    assert sdmx.build_criteria(dsd, key="ALL") == []


def test_build_criteria_rejects_unknown_dimension(dsd):
    # The server silently ignores unknown dimensions (returns everything),
    # so this must fail client-side.
    with pytest.raises(sdmx.SdmxError, match="boyut değil"):
        sdmx.build_criteria(dsd, filters="NOPE=1")


def test_build_criteria_rejects_unknown_code(dsd):
    with pytest.raises(sdmx.SdmxError, match="geçersiz kod"):
        sdmx.build_criteria(dsd, filters="SEX=9")


def test_build_criteria_rejects_bad_key_length_and_period(dsd):
    with pytest.raises(sdmx.SdmxError, match="parçadan"):
        sdmx.build_criteria(dsd, key="TR.1")
    with pytest.raises(sdmx.SdmxError, match="Dönem anlaşılamadı"):
        sdmx.build_criteria(dsd, start="dün")
    with pytest.raises(sdmx.SdmxError, match="sonra olamaz"):
        sdmx.build_criteria(dsd, start="2025", end="2020")
    with pytest.raises(sdmx.SdmxError, match="start/end"):
        sdmx.build_criteria(dsd, filters="TIME_PERIOD=2020")


# ---------------------------------------------------------------- SDMX-CSV


def test_sdmx_csv_to_frame_codes_labels_values(dsd):
    frame = sdmx.sdmx_csv_to_frame(_CSV, dsd, lang="tr")
    # '_Z'-only dimension and all-empty attribute dropped; DATAFLOW dropped.
    assert "UNUSED" not in frame.columns
    assert "UNIT_MULT" not in frame.columns
    assert "DATAFLOW" not in frame.columns
    assert list(frame.columns[:4]) == ["REF_AREA", "REF_AREA_label", "SEX", "SEX_label"]
    # 'NA' (Namibia) must stay a code, not become NaN.
    assert "NA" in set(frame["REF_AREA"].astype(str))
    assert frame.loc[frame["REF_AREA"] == "NA", "REF_AREA_label"].iloc[0] == "Namibya"
    assert frame["deger"].tolist()[:2] == [10.5, 11.25]
    assert pd.isna(frame["deger"].iloc[2])
    # A non-numeric OBS_VALUE is never coerced silently.
    assert frame["deger_ham"].iloc[3] == "abc"
    assert frame["OBS_STATUS_label"].iloc[1] == "Geçici"


def test_sdmx_csv_to_frame_english_and_no_labels(dsd):
    en = sdmx.sdmx_csv_to_frame(_CSV, dsd, lang="en")
    assert en["SEX_label"].iloc[0] == "Male"
    assert en["REF_AREA_label"].iloc[0] == "Türkiye"  # no English name -> Turkish
    bare = sdmx.sdmx_csv_to_frame(_CSV, dsd, labels=False)
    assert not any(c.endswith("_label") for c in bare.columns)


def test_sdmx_csv_keeps_codes_verbatim(dsd):
    # Codes are strings: '01' must not become 1, single-valued numeric-looking
    # code columns must still map to labels.
    text = (
        "DATAFLOW,REF_AREA,SEX,TIME_PERIOD,OBS_VALUE\n"
        "TR:DF_TEST(1.0),TR,1,2024-01,5\n"
        "TR:DF_TEST(1.0),TR,1,2024-02,6\n"
    )
    frame = sdmx.sdmx_csv_to_frame(text, dsd)
    assert frame["SEX"].astype(str).tolist() == ["1", "1"]
    assert frame["SEX_label"].iloc[0] == "Erkek"
    assert frame["TIME_PERIOD"].astype(str).tolist() == ["2024-01", "2024-02"]
    dsd["dimensions"][1]["codes"]["01"] = {"tr": "Ocak"}
    frame = sdmx.sdmx_csv_to_frame(text.replace(",1,", ",01,"), dsd)
    assert frame["SEX"].astype(str).tolist() == ["01", "01"]
    assert frame["SEX_label"].iloc[0] == "Ocak"


def test_filter_periods_overlap_and_refusal():
    frame = pd.DataFrame({"TIME_PERIOD": ["2008", "2012", "2017", "2024"], "deger": [1, 2, 3, 4]})
    kept = sdmx.filter_periods(frame, "TIME_PERIOD", "2013-01-01", "2020-12-31")
    assert kept["TIME_PERIOD"].tolist() == ["2017"]
    kept = sdmx.filter_periods(frame, "TIME_PERIOD", "2012-06-01", None)
    assert kept["TIME_PERIOD"].tolist() == ["2012", "2017", "2024"]
    odd = pd.DataFrame({"TIME_PERIOD": ["2020-W05"], "deger": [1]})
    with pytest.raises(sdmx.SdmxError, match="tanınmadı"):
        sdmx.filter_periods(odd, "TIME_PERIOD", "2020-01-01", None)


def test_sdmx_csv_requires_obs_value(dsd):
    with pytest.raises(sdmx.SdmxError):
        sdmx.sdmx_csv_to_frame("A,B\n1,2\n", dsd)


def test_error_message_for_missing_dataflow():
    response = httpx.Response(
        500, json={"errorCode": "DATAFLOW_NOT_FOUND", "message": "Dataflow not found"}
    )
    err = sdmx._error_from_response(response, "TR,DF_ADNKS_T26,1.0", "structure")
    assert "bulunamadı" in str(err) and "tuik_search_tables" in str(err)


# ---------------------------------------------------------------- catalog


@pytest.mark.parametrize(
    "text,expected",
    [
        ("İŞGÜCÜ", "isgucu"),
        ("işgücü", "isgucu"),
        ("ISPARTA", "isparta"),
        ("Iğdır", "igdir"),
        ("Tüketici Fiyat Endeksi (TÜFE)", "tuketici fiyat endeksi tufe"),
        ("Çalışma", "calisma"),
    ],
)
def test_fold_tr(text, expected):
    assert portal.fold_tr(text) == expected


_TREE = [
    {"id": "7", "name": "İstihdam, İşsizlik ve Ücret", "icon": "folder", "url": "", "children": [
        {"id": "7.1", "name": "İşgücü İstatistikleri", "icon": "folder", "url": "", "children": [
            {"id": "a", "name": "Temel İşgücü Göstergeleri", "icon": "dataflow",
             "url": "https://databrowser2.tuik.gov.tr/#/tr/tuik/TR,DF_ISGUCU,1.0"},
            {"id": "b", "name": "İl düzeyinde işsizlik oranı", "icon": "istab",
             "url": "/api/tr/data/downloads?t=i&p=x", "metadataUrl": "/api/tr/data/downloads?t=m&p=y"},
            {"id": "c", "name": "Ücret yapısı", "icon": "database",
             "url": "http://biruni.tuik.gov.tr/medas/?kn=1&locale=tr"},
            {"id": "d", "name": "İşgücü bülteni", "icon": "press", "url": "/tr/press/1"},
        ]},
    ]},
]


@pytest.fixture
def portal_client():
    client = portal.PortalClient()
    import time as _time

    client._tree_cache["tr"] = (_time.monotonic(), _TREE)
    return client


def test_search_is_turkish_aware_and_multiword(portal_client):
    names = [r["name"] for r in portal_client.search("isgucu")]
    assert names[0] == "Temel İşgücü Göstergeleri"          # name match first
    assert "İl düzeyinde işsizlik oranı" in names            # matched via folder path
    assert [r["name"] for r in portal_client.search("ISSIZLIK il")] == ["İl düzeyinde işsizlik oranı"]
    assert portal_client.search("isgucu bulteni") == []      # press not in default types
    assert len(portal_client.search("isgucu bulteni", types=("press",))) == 1


def test_search_includes_database_and_row_fields(portal_client):
    rows = portal_client.search("ucret yapisi")
    assert rows[0]["type"] == "database" and "link-only" in rows[0]["access"]
    flow = portal_client.search("temel isgucu")[0]
    assert flow["dataflow_id"] == "TR,DF_ISGUCU,1.0"
    assert flow["path"] == "İşgücü İstatistikleri"
    istab = portal_client.search("il duzeyinde")[0]
    assert istab["url"].startswith("https://veriportali.tuik.gov.tr/api/")
    assert istab["metadata_url"].startswith("https://veriportali.tuik.gov.tr/")


def test_download_file_rejects_foreign_hosts():
    with pytest.raises(portal.PortalError):
        portal.download_file("https://example.com/file.xls")


def test_download_file_waits_out_throttle_page(monkeypatch):
    throttle = (
        "<!DOCTYPE html><html><body><span id=time></span> saniye sonra tekrar "
        "dosya indirebilirsiniz.</body></html>"
    )
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, text=throttle, headers={"content-type": "text/html"})
        return httpx.Response(
            200, content=b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest",
            headers={"content-type": "application/vnd.ms-excel",
                     "content-disposition": "attachment; filename*=UTF-8''T%C3%BCrkiye.xls"},
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        portal.httpx, "Client",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )
    monkeypatch.setattr(portal, "DOWNLOAD_MIN_INTERVAL", 0.01)
    got = portal.download_file("https://veriportali.tuik.gov.tr/api/tr/data/downloads?t=i&p=x")
    assert calls["n"] == 2
    assert got.kind == "xls" and got.filename == "Türkiye.xls"


def _mock_portal(monkeypatch, handler):
    real_client = httpx.Client
    monkeypatch.setattr(
        portal.httpx, "Client",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )
    sleeps = []
    monkeypatch.setattr(portal.time, "sleep", sleeps.append)
    return sleeps


def test_theme_tree_retries_connection_reset(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if len(calls) == 1:
            raise httpx.ConnectError("[Errno 54] Connection reset by peer", request=request)
        if request.url.path.startswith("/api/"):
            return httpx.Response(200, json={"isError": False, "data": _TREE})
        return httpx.Response(200, text="<html></html>", headers={"set-cookie": "s=1"})

    sleeps = _mock_portal(monkeypatch, handler)
    client = portal.PortalClient()
    assert client.theme_tree() == _TREE
    # failed landing, then landing again (fresh session) + API
    assert calls == ["/tr/statistical-themes", "/tr/statistical-themes",
                     "/api/tr/data/statistical-themes"]
    assert sleeps == [1.0]


def test_theme_tree_persistent_failure_and_stale_cache(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503, text="busy")

    sleeps = _mock_portal(monkeypatch, handler)
    client = portal.PortalClient()
    with pytest.raises(portal.PortalError, match="bağlanılamadı.*HTTP 503"):
        client.theme_tree()
    assert calls["n"] == 3 and sleeps == [1.0, 2.0]

    # an expired cached tree is served instead of failing
    client._tree_cache["tr"] = (0.0, _TREE)
    assert client.theme_tree(refresh=True) == _TREE


def test_theme_tree_does_not_retry_4xx(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(404)

    sleeps = _mock_portal(monkeypatch, handler)
    with pytest.raises(portal.PortalError, match="HTTP 404"):
        portal.PortalClient().theme_tree()
    assert calls["n"] == 1 and sleeps == []


# ---------------------------------------------------------------- tidy


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("12,5", "12.5"), ("1.234,5", "1234.5"), ("1,234.5", "1234.5"),
        ("1.234.567", "1234567"), ("-3,2", "-3.2"), ("12*", "12"), ("45 (r)", "45"),
        ("0,123", "0.123"), (7, 7),
        ("1.234", None), ("1,234", None),       # ambiguous: refuse
        ("1/2", None), ("15-19", None), ("3:4", None), ("..", None), ("-", None),
        ("Toplam 15 yaş", None),
    ],
)
def test_clean_number(raw, expected):
    assert tidy._clean_number(raw) == expected


def test_looks_like_period_with_revision_marks():
    assert tidy.looks_like_period("2017(r)")
    assert tidy.looks_like_period("2020 (1)")
    assert tidy.looks_like_period(2021.0)
    assert not tidy.looks_like_period("Toplam")


def _xls_bytes(rows: list[list]) -> bytes:
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_tidy_year_month_stub_is_not_a_value():
    # Merged 'Yıl' cells (blank under the first month) and an 'Ay' column of
    # month numbers: months must not become values, years must be filled.
    rows = [
        ["Perakende satış endeksi", None, None],
        [None, None, None],
        ["Yıl\nYear", "Ay\nMonth", "Endeks\nIndex"],
        [2010, 1, 57.4],
        [None, 2, 57.3],
        [None, 3, 67.9],
        [2011, 1, 60.1],
        [None, 2, 61.0],
    ]
    frame, report = tidy.tidy_istab(_xls_bytes(rows))
    assert list(frame.columns) == ["Yıl\nYear", "Ay\nMonth", "gosterge", "deger"]
    assert frame["deger"].tolist() == [57.4, 57.3, 67.9, 60.1, 61.0]
    assert frame["Yıl\nYear"].tolist() == [2010, 2010, 2010, 2011, 2011]
    assert report["tidy_confidence"] == "yüksek"


def test_tidy_crosstab_period_headers():
    rows = [
        ["Ülkelere göre endeks", None, None, None],
        [None, None, None, None],
        ["Ülke", 2021, 2022, "2023(r)"],
        ["Almanya", 100.5, 101.2, 103.0],
        ["Fransa", 98.1, "-", 99.9],
    ]
    frame, _ = tidy.tidy_istab(_xls_bytes(rows))
    assert set(frame.columns) == {"Ülke", "donem", "deger"}
    assert len(frame) == 5  # the '-' cell is dropped, not zero
    assert frame.loc[(frame["Ülke"] == "Fransa") & (frame["donem"] == "2023(r)"), "deger"].iloc[0] == 99.9


def test_tidy_refuses_year_values():
    # A layout where a year series would land in 'deger' must fail loudly.
    long = pd.DataFrame({"gosterge": ["Yıl"] * 3, "deger": [2019.0, 2020.0, 2021.0]})
    with pytest.raises(tidy.TidyError, match="year numbers"):
        tidy._check_values(long)


def test_tidy_rejects_non_spreadsheets():
    with pytest.raises(tidy.TidyError, match="PDF"):
        tidy.tidy_istab(b"%PDF-1.7 ...")
    with pytest.raises(tidy.TidyError, match="HTML"):
        tidy.tidy_istab(b"<!DOCTYPE html><html></html>")


# ---------------------------------------------------------------- live


LIVE_FLOW = "TR,DF_TUFE_SDMX_TT10,1.0"   # TÜFE ve değişim oranları (aylık)


@pytest.mark.live
def test_live_catalog_search_and_themes():
    client = portal.PortalClient()
    themes = client.list_themes()
    assert len(themes) >= 15
    hits = client.search("isgucu", types=("dataflow",))
    assert hits and all(h["dataflow_id"] for h in hits)
    assert any(h["dataflow_id"] == LIVE_FLOW for h in client.search("tuketici fiyat endeksi degisim", types=("dataflow",)))


@pytest.mark.live
def test_live_missing_dataflow_has_clear_error():
    sdmx.clear_cache()
    with pytest.raises(sdmx.SdmxError, match="bulunamadı"):
        sdmx.fetch_dsd("TR,DF_ADNKS_T26,1.0")


@pytest.mark.live
def test_live_describe_without_data_download():
    info = sdmx.fetch_structure(LIVE_FLOW, lang="en")
    ids = [d["id"] for d in info["dimensions"]]
    assert "DEGISIM" in ids and info["n_observations"] > 1000
    degisim = next(d for d in info["dimensions"] if d["id"] == "DEGISIM")
    assert degisim["n_codes"] >= 4 and "4" in degisim["codes"]
    assert info["time_range"]["first"].startswith("2005")


@pytest.mark.live
def test_live_filtered_data_matches_server_count():
    frame = sdmx.fetch_data(LIVE_FLOW, filters="DEGISIM=4", start="2024-01", end="2024-12")
    assert len(frame) == 12 == frame.attrs["meta"]["expected_observations"]
    assert set(frame["DEGISIM"].astype(str)) == {"4"}
    assert frame["TIME_PERIOD"].astype(str).tolist() == [f"2024-{m:02d}" for m in range(1, 13)]
    assert frame["deger"].notna().all()


@pytest.mark.live
def test_live_single_quarter_keeps_time_column():
    # JSON-stat /data drops the time dimension for single-period selections;
    # the SDMX-CSV path must not.
    frame = sdmx.fetch_data(
        "TR,DF_ISGUCU_CEYREK_TEMEL_ISGUCU_C,1.0",
        filters="ISGUCU_GOSTERGE=8;SEX=_T;SEASONAL_ADJUST=S;TEMEL_YAS=Y_GE15",
        start="2025-Q1", end="2025-Q1", lang="en",
    )
    assert len(frame) == 1
    assert str(frame["TIME_PERIOD"].iloc[0]) == "2025-Q1"
    assert frame["ISGUCU_GOSTERGE_label"].iloc[0] == "Unemployment Rate (%)"


@pytest.mark.live
def test_live_empty_selection_raises():
    with pytest.raises(sdmx.SdmxError, match="gözlem yok"):
        sdmx.fetch_data(LIVE_FLOW, start="1990", end="1991")


@pytest.mark.live
def test_live_istab_download_and_tidy():
    client = portal.PortalClient()
    rows = client.search("bina insaati maliyet endeksi", types=("istab",))
    assert rows
    got = portal.download_file(rows[0]["url"])
    assert got.kind in ("xls", "zip/xlsx")
    frame, report = tidy.tidy_istab(got.content)
    assert len(frame) > 0 and "deger" in frame.columns
    assert report["tidy_confidence"] in ("yüksek", "orta", "kısmi")
