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
    # Yıl and Ay stay as they are; 'donem' (added) combines them.
    assert list(frame.columns) == ["Yıl\nYear", "Ay\nMonth", "donem", "gosterge", "deger"]
    assert frame["deger"].tolist() == [57.4, 57.3, 67.9, 60.1, 61.0]
    assert frame["Yıl\nYear"].tolist() == [2010, 2010, 2010, 2011, 2011]
    assert frame["donem"].tolist() == ["2010-01", "2010-02", "2010-03", "2011-01", "2011-02"]
    assert report["tidy_confidence"] == "yüksek"
    assert report["donem_kaynagi"] == {"Sheet": "tablo"}


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
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<w/>")
    with pytest.raises(tidy.TidyError, match="DOCX"):
        tidy.tidy_istab(buf.getvalue())


# ------------------------------------------------- istab layouts (synthetic)


def _book(rows: list[list], merges: tuple[str, ...] = (), indent: dict | None = None,
          title: str = "Sheet") -> bytes:
    """Tiny xlsx: rows, merged ranges ('A1:C1') and Excel indent levels {(row, col): n} (0-based)."""
    from openpyxl.styles import Alignment

    wb = Workbook()
    ws = wb.active
    ws.title = title
    for row in rows:
        ws.append(row)
    for rng in merges:
        ws.merge_cells(rng)
    for (r, c), level in (indent or {}).items():
        ws.cell(row=r + 1, column=c + 1).alignment = Alignment(indent=level)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _value(frame: pd.DataFrame, **labels) -> float:
    sel = frame
    for column, value in labels.items():
        sel = sel[sel[column] == value]
    assert len(sel) == 1, (labels, sel)
    return float(sel["deger"].iloc[0])


@pytest.mark.parametrize(
    "raw,expected",
    [("2006 (Ekim-Kasım)", None), ("-(1)", None), ("12 (r)", 12), ("1 234 567", "1234567")],
)
def test_clean_number_never_reads_a_worded_header(raw, expected):
    got = tidy._clean_number(raw)
    assert (got if got is None else float(got)) == (expected if expected is None else float(expected))


@pytest.mark.parametrize(
    "text,periods,has_range",
    [
        ("Nüfus, 2023", ["2023"], False),
        ("Tablo, 2015-2024", [], True),
        ("Endeks (2003=100), 2020", ["2020"], False),
        ("Yurt içi ÜFE, 2003=100, CPA 2008", [], False),
        ("Hanehalkı tüketimi (1998 Fiyatlarıyla)", [], False),
        ("Gençler, Ekim-Aralık 2019", ["2019 Ekim-Aralik"], False),
        ("2023 yılı I. Çeyrek", ["2023-Q1"], False),
        ("31.12.2023 tarihi itibarıyla", ["2023-12-31"], False),
        ("Sağlık harcamaları, 2022, 2025", ["2022", "2025"], False),
        ("2013/'14 öğretim yılı", ["2013/2014"], False),
    ],
)
def test_periods_in_title_text(text, periods, has_range):
    assert tidy.periods_in_text(text) == (periods, has_range)


def test_period_section_rows_become_donem():
    rows = [
        ["Suç türüne göre hükümlüler", None, None],
        [None, None, None],
        ["Suç türü", "Erkek", "Kadın"],
        [2020, None, None],
        ["Hırsızlık - Theft", 10, 2],
        ["Yağma - Robbery", 5, 1],
        [2019, None, None],
        ["Hırsızlık - Theft", 11, 3],
        ["Yağma - Robbery", 6, 0],
    ]
    frame, report = tidy.tidy_istab(_book(rows))
    assert _value(frame, donem="2019", gosterge="Erkek", **{"Suç türü": "Hırsızlık - Theft"}) == 11
    assert report["tidy_confidence"] == "yüksek"


def test_wrapped_label_is_not_a_section_heading():
    # 'Kişiyi hürriyetinden yoksun kılma -' / 'Kidnapping' is one label split over
    # two lines; it must not become a heading for the rows below it.
    rows = [
        ["Suç türü", "Toplam"],
        ["Öldürme - Homicide", 5],
        ["Kişiyi hürriyetinden yoksun kılma -", None],
        ["Kidnapping", 7],
        ["Hırsızlık - Theft", 9],
    ]
    frame, _ = tidy.tidy_istab(_book(rows))
    assert "bolum" not in frame.columns
    labels = frame["Suç türü"].tolist()
    assert labels == ["Öldürme - Homicide", "Kişiyi hürriyetinden yoksun kılma - Kidnapping", "Hırsızlık - Theft"]


def test_tr_en_labels_on_consecutive_rows_form_one_series():
    rows = [
        ["Eğitim", "Ölçü", 2016, 2017],
        ["Toplam", "Sayı-HC", 1388, 1564],
        ["Total", "TZE-FTE", 1150.5, 1287.8],
        ["Doktora", "Sayı-HC", 274, 308],
        ["Doctoral level", "TZE-FTE", 222.7, 247.4],
        ["Lisans", "Sayı-HC", 490, 531],
        ["Bachelor level", "TZE-FTE", 393.7, 421.0],
    ]
    frame, report = tidy.tidy_istab(_book(rows))
    assert set(frame["Eğitim"]) == {"Toplam", "Doktora", "Lisans"}
    assert _value(frame, Eğitim="Doktora", Ölçü="TZE-FTE", donem="2017") == 247.4
    assert set(frame["Eğitim_en"]) == {"Total", "Doctoral level", "Bachelor level"}
    assert "varsayimlar" in report


def test_block_title_in_value_area_replaces_header_title():
    # CPI layout: 'Endeks' block, then 'Aylık değişim (%)' block with the same rows.
    rows = [
        ["Yıl", "Ocak", "Şubat"],
        [None, "Endeks - Index", None],
        [2005, 100.0, 101.0],
        [2006, 110.0, 111.0],
        [None, "Aylık değişim (%)", None],
        [2005, 1.5, 1.0],
        [2006, 2.5, 0.9],
    ]
    frame, _ = tidy.tidy_istab(_book(rows, merges=("B2:C2", "B5:C5")))
    assert _value(frame, donem="2006", gosterge="Ocak — Aylık değişim (%)") == 2.5
    assert _value(frame, donem="2006", gosterge="Ocak — Endeks - Index") == 110.0


def test_indented_groups_under_block_titles():
    # Two population blocks repeat the same groups; the block title must stay.
    rows = [
        [None, "Evli", "Bekar"],
        ["Engelli nüfus", None, None],
        ["Türkiye", 54.1, 34.4],
        ["Cinsiyet", None, None],
        ["Erkek", 60.9, 34.7],
        ["Kadın", 44.5, 34.0],
        ["Süreğen hastalığı olan nüfus", None, None],
        ["Türkiye", 76.7, 7.4],
        ["Cinsiyet", None, None],
        ["Erkek", 80.1, 9.6],
        ["Kadın", 73.0, 6.0],
    ]
    indent = {(4, 0): 1, (5, 0): 1, (9, 0): 1, (10, 0): 1}
    frame, _ = tidy.tidy_istab(_book(rows, merges=("A2:C2", "A7:C7"), indent=indent))
    erkek = frame[(frame["kolon_1"] == "Erkek") & (frame["gosterge"] == "Evli")]
    assert dict(zip(erkek["bolum"], erkek["deger"])) == {
        "Engelli nüfus > Cinsiyet": 60.9,
        "Süreğen hastalığı olan nüfus > Cinsiyet": 80.1,
    }


def test_lost_dimension_is_refused_not_returned():
    # Two blocks with identical row labels and nothing telling them apart.
    rows = [
        ["Bölge", "Değer"],
        ["Kent", 1.0],
        ["Kır", 2.0],
        [None, None],
        ["Kent", 3.0],
        ["Kır", 4.0],
        ["Toplam", 10.0],
    ]
    with pytest.raises(tidy.TidyError, match="do not tell values apart"):
        tidy.tidy_istab(_book(rows))


def test_source_row_printed_twice_is_dropped_and_reported():
    rows = [["Ürün", "Endeks"]] + [[f"Ürün {i}", float(i)] for i in range(1, 150)]
    rows[30] = ["Ürün 29", 99.0]          # same label as row 29, different number
    frame, report = tidy.tidy_istab(_book(rows))
    assert report["tidy_confidence"] == "kısmi"
    assert "aynı etiketle" in report["atlananlar"]["Sheet"][0]
    assert "Ürün 29" not in set(frame["Ürün"])


def test_heading_cycle_total_rows_label_their_block():
    rows = [
        [None, "Toplam", "Yük getirdi"],
        ["Muayene", 100, 7.3],
        ["Cinsiyet", None, None],
        ["Erkek", 100, 7.0],
        ["Kadın", 100, 7.5],
        ["Yaş", None, None],
        ["15-24", 100, 7.6],
        ["İlaç", 100, 5.5],
        ["Cinsiyet", None, None],
        ["Erkek", 100, 5.3],
        ["Kadın", 100, 5.6],
        ["Yaş", None, None],
        ["15-24", 100, 6.1],
    ]
    frame, _ = tidy.tidy_istab(_book(rows))
    ilac = frame[(frame["kolon_1"] == "İlaç") & (frame["gosterge"] == "Yük getirdi")]
    assert ilac["bolum"].isna().all()           # not under 'Yaş'
    assert _value(frame, kolon_1="Erkek", bolum="İlaç > Cinsiyet", gosterge="Yük getirdi") == 5.3


def test_side_by_side_tables_take_their_own_labels():
    rows = [
        ["Yıllar", "Hekim", "Hemşire", None, "Yıllar", "Hekim", "Hemşire"],
        [1928, 12841, 106485, None, 1977, 23000, 9000],
        [1929, 12971, 86207, None, 1978, 24000, 9500],
    ]
    frame, report = tidy.tidy_istab(_book(rows))
    assert _value(frame, donem="1977", gosterge="Hekim") == 23000
    assert _value(frame, donem="1928", gosterge="Hekim") == 12841
    assert any("yan yana" in a for a in report["varsayimlar"]["Sheet"])


def test_percent_columns_after_year_columns_get_that_year():
    rows = [
        ["Faaliyet türü", 2005, "(%)", 2006, "(%)", 2007, "(%)"],
        ["Toplam", 44100098, 100, 50817084, 100, 48843768, 100],
        ["Fuar", 2800523, 6.35, 3132661, 6.16, 3835146, 7.85],
    ]
    frame, _ = tidy.tidy_istab(_book(rows))
    assert _value(frame, **{"Faaliyet türü": "Fuar"}, donem="2006", gosterge="(%)") == 6.16
    assert frame["Faaliyet türü"].map(lambda v: isinstance(v, str)).all()


def test_repeated_unit_headers_are_qualified_by_their_measure():
    rows = [
        [None, "Seçmen sayısı", "Oy kullanan", "(%)", "Geçerli oy", "(%)"],
        ["Türkiye", 64197454, 54023601, 84.2, 53339313, 98.7],
        ["Yurt dışı", 3426218, 1930226, 56.3, 1920757, 99.5],
    ]
    frame, _ = tidy.tidy_istab(_book(rows))
    assert _value(frame, kolon_1="Yurt dışı", gosterge="Geçerli oy — (%)") == 99.5
    assert _value(frame, kolon_1="Yurt dışı", gosterge="Oy kullanan — (%)") == 56.3


def test_code_column_left_of_names_is_a_label():
    rows = [
        ["Meslek grubu", None, "Kazanç", "Temel ücret (%)"],
        ["Toplam - Total", None, 14251.8, 82.0],
        [1, "Kanun yapıcılar", 35914, 82.7],
        [11, "Kanun yapıcılar ve üst düzey", 27609, 89.4],
        [112, "Üst düzey devlet memurları", "(0)", "(0)"],
        [12, "Şirket müdürleri", 37027, 82.9],
    ]
    frame, _ = tidy.tidy_istab(_book(rows, merges=("A1:B1",)))
    assert 112.0 not in set(frame["deger"]) and 11.0 not in set(frame["deger"])
    assert sorted(frame["deger"]) == sorted([14251.8, 82.0, 35914, 82.7, 27609, 89.4, 37027, 82.9])


def test_marker_with_footnote_keeps_a_year_column_numeric():
    rows = [
        [None, 2006, 2007, 2008, 2009],
        ["Sürekli yoksulluk oranı", "-(1)", "-(1)", 17.3, 18.5],
        ["Maddi yoksunluk oranı", 60.4, 58.8, 57.7, 56.7],
        ["Maddi ve sosyal yoksunluk", "-(2)", "-(2)", "-(2)", "-(2)"],
    ]
    frame, _ = tidy.tidy_istab(_book(rows))
    assert _value(frame, kolon_1="Maddi yoksunluk oranı", donem="2006") == 60.4
    assert len(frame) == 6
    assert set(frame.columns) <= {"kolon_1", "donem", "deger"}


def test_code_header_row_is_not_data():
    rows = [
        ["Fiyat endeksi", None, None, None],
        ["CPA", "05", "05.1", "10", "10.1"][:4],
        ["Yıl", "Madencilik", "Kömür", "Gıda"],
        [2003, 91.5, 92.0, 93.1],
        [2004, 95.2, 96.0, 97.3],
    ]
    frame, _ = tidy.tidy_istab(_book(rows))
    assert sorted(frame["deger"]) == sorted([91.5, 92.0, 93.1, 95.2, 96.0, 97.3])
    assert 10.0 not in set(frame["deger"])


def test_source_line_with_a_number_is_not_data():
    rows = [
        ["Yıl", "Göç"],
        [2024, 2513950],
        [2025, 2314447],
        ["TÜİK, İç Göç İstatistikleri, 2025", 1],
    ]
    frame, _ = tidy.tidy_istab(_book(rows))
    assert sorted(frame["deger"]) == [2314447, 2513950]


def test_quarter_rows_closed_by_a_year_row():
    rows = [
        ["Çeyrek", "Tarım", "Sanayi"],
        ["1.çeyrek-quarter", 36.1, 27.2],
        ["2.çeyrek-quarter", 28.0, 28.7],
        [1999, 25.4, 24.3],
        [None, None, None],
        ["1.çeyrek-quarter", 57.3, 36.1],
        ["2.çeyrek-quarter", 45.2, 39.4],
        [2000, 53.8, 31.3],
    ]
    frame, _ = tidy.tidy_istab(_book(rows))
    assert _value(frame, donem="2000-Q2", gosterge="Tarım") == 45.2
    assert _value(frame, donem="1999", gosterge="Sanayi") == 24.3


def test_group_header_fill_needs_the_same_sub_header_shape():
    # No merged cells: 'Büyüme hızı' sits over its own column only; the next
    # column is a sector ('Tarım') whose label is two rows lower.
    rows = [
        [None, None, "Sektör", "Büyüme", None, "Sektör"],
        [None, None, "payı", "hızı", None, "payı"],
        ["Yıl", "GSMH", "%", "%", "Tarım", "%"],
        [1968, 163892.7, 100, None, 65151.1, 39.75],
        [1969, 183356.2, 100, 11.88, 71205.8, 38.83],
    ]
    frame, _ = tidy.tidy_istab(_book(rows))
    tarim = frame[frame["deger"] == 65151.1]["gosterge"].iloc[0]
    assert "Büyüme" not in tarim and "Tarım" in tarim


def test_counts_that_look_like_years_stay_values():
    rows = [
        ["İBBS", None, "Toplam", "Erkek", "Kadın"],
        ["TR333", "Kütahya", 8617, 8617, "-"],
        ["TR334", "Uşak", 1925, 1925, "-"],
        ["TR411", "Bursa", 20924, 20924, "-"],
    ]
    frame, _ = tidy.tidy_istab(_book(rows, merges=("A1:B1",)))
    assert _value(frame, **{"İBBS_2": "Uşak"}, gosterge="Erkek") == 1925


def test_period_from_title_has_provenance_and_range_title_does_not():
    rows = [["Medeni duruma göre engelli nüfus oranı, 2002"], [None, None],
            [None, "Evli", "Bekar"], ["Türkiye", 54.1, 34.4], ["Kent", 55.2, 33.1]]
    frame, report = tidy.tidy_istab(_book(rows))
    assert set(frame["donem"]) == {"2002"} and set(frame["donem_kaynak"]) == {"başlık"}
    assert report["tidy_confidence"] == "orta" and report["donem_kaynagi"]["Sheet"] == "başlık"
    rows[0] = ["Medeni duruma göre engelli nüfus oranı, 2002-2010"]
    frame, report = tidy.tidy_istab(_book(rows))
    assert "donem" not in frame.columns
    assert report["donem_kaynagi"]["Sheet"].startswith("yok")
    # catalog name is used only when the sheet itself names no period
    rows[0] = ["Medeni duruma göre engelli nüfus oranı"]
    frame, report = tidy.tidy_istab(_book(rows), meta={"name": "Engelli nüfus, 2002", "path": "Sağlık"})
    assert set(frame["donem_kaynak"]) == {"katalog adı"}


def test_unlabeled_zero_column_is_ignored_and_reported():
    rows = [
        ["Ad", "Değer", None],
        ["A", 1.5, 0],
        ["B", 2.5, 0],
        ["C", 3.5, 0],
    ]
    frame, report = tidy.tidy_istab(_book(rows))
    assert sorted(frame["deger"]) == [1.5, 2.5, 3.5]
    assert any("başlıksız" in a for a in report["varsayimlar"]["Sheet"])


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
    frame, report = tidy.tidy_istab(got.content, meta={"name": rows[0]["name"], "path": rows[0]["path"]})
    assert len(frame) > 0 and "deger" in frame.columns
    assert report["tidy_confidence"] in ("yüksek", "orta", "kısmi")
    assert set(report["donem_kaynagi"]) == set(report["sheet_notes"])
    keys = [c for c in frame.columns if c != "deger"]
    assert not frame.duplicated(subset=keys).any()      # labels identify every value


def test_glued_tr_en_heading_is_not_merged_into_the_next_label():
    # 'Öğrenim durumu-Educational status' is a group heading, not the first line
    # of 'Bir okul bitirmedi-No school completed'; the age heading must not go stale.
    rows = [
        [None, "Katılıyor"],
        ["Türkiye-Türkiye", 43.1],
        ["Yaş grubu-Age group", None],
        ["15-24", 45.1],
        ["25-34", 39.0],
        ["Öğrenim durumu-Educational status", None],
        ["Bir okul bitirmedi-No school completed", 48.8],
        ["Lise-High school", 39.7],
    ]
    frame, _ = tidy.tidy_istab(_book(rows))
    lise = frame[frame["kolon_1"] == "Lise-High school"]
    assert lise["bolum"].tolist() == ["Öğrenim durumu-Educational status"]
    assert "Bir okul bitirmedi-No school completed" in set(frame["kolon_1"])


def test_en_lines_without_numbers_belong_to_the_row_above():
    rows = [
        [None, "Erkek", "Kadın"],
        ["Yemek yapma", 2, 87.1],
        ["Cooking", None, None],
        ["Ütü", 2.2, 84.3],
        ["Ironing", None, None],
        ["Günlük yiyecek-içecek alışverişi", 33.3, 37.7],
        ["Daily shopping", None, None],
        ["Aylık faturaların ödenmesi", 69.1, 17],
        ["Payment of monthly bills", None, None],
    ]
    frame, _ = tidy.tidy_istab(_book(rows))
    assert "bolum" not in frame.columns
    assert _value(frame, kolon_1="Ütü", kolon_1_en="Ironing", gosterge="Kadın") == 84.3


def test_alternating_en_lines_are_labels_not_headings():
    rows = [
        [None, "Erkek", "Kadın"],
        ["Çocuk babanın hayatını olumsuz etkiler", 21.9, 21.2],
        ["Children negatively affect the father", None, None],
        ["Erkek çocuk annenin itibarını artırır", 32.6, 31.3],
        ["Sons increase the prestige of the mother", None, None],
        ["Soyun devamını erkek çocuk sağlar", 44.6, 36.4],
        ["Son keeps the race up", None, None],
        ["Çocuk eşleri birbirine yaklaştırır", 86.9, 85.0],
        ["Children bring the spouses closer", None, None],
    ]
    frame, _ = tidy.tidy_istab(_book(rows))
    assert "bolum" not in frame.columns
    assert _value(frame, kolon_1="Soyun devamını erkek çocuk sağlar", gosterge="Erkek") == 44.6
    assert set(frame["kolon_1_en"]) >= {"Son keeps the race up", "Sons increase the prestige of the mother"}


def test_inferred_structure_is_never_yuksek():
    rows = [[None, "Değer"], ["Cinsiyet", None], ["Erkek", 1.0], ["Kadın", 2.0],
            ["Yaş", None], ["15-24", 3.0], ["25-34", 4.0]]
    frame, report = tidy.tidy_istab(_book(rows), meta={"name": "Tablo, 2020"})
    assert "bolum" in frame.columns
    assert report["tidy_confidence"] == "orta"
    assert "bolum" in report["yapi_cikarimi"]["Sheet"][0]
