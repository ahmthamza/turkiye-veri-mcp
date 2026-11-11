"""Tests for turkiye_veri_mcp.bddk.

Offline tests parse small fixtures trimmed from real bddk.org.tr pages and
responses captured on 2026-11-11 (tests/fixtures/bddk). Live tests hit
bddk.org.tr and are marked ``live`` (deselect with ``-m "not live"``).
"""

from __future__ import annotations

import datetime as dt
import json
import ssl
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from turkiye_veri_mcp import bddk

FIX = Path(__file__).parent / "fixtures" / "bddk"


def _text(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def _json(name: str) -> dict:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch, request):
    # offline tests need no pacing; live tests keep the polite default
    if request.node.get_closest_marker("live") is None:
        monkeypatch.setattr(bddk, "MIN_INTERVAL", 0.0)
    bddk.clear_cache()
    yield
    bddk.clear_cache()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("18.535.605,59384", 18535605.59384),
        (" 10.465.173,7722", 10465173.7722),
        ("62186,36", 62186.36),
        ("8.743", 8743.0),
        ("-2.314.424,05086", -2314424.05086),
        ("0,00", 0.0),
        ("2,87", 2.87),
        ("", None),
    ],
)
def test_parse_number(raw, expected):
    assert bddk.parse_number(raw) == expected


def test_parse_number_rejects_garbage():
    with pytest.raises(bddk.BddkError):
        bddk.parse_number("abc")


def test_tr_fold():
    assert bddk.tr_fold("İSTANBUL") == bddk.tr_fold("istanbul") == "ISTANBUL"
    assert bddk.tr_fold("Muş") == "MUS"


def test_sutun_index():
    assert bddk.sutun_index("tp") == (1, "TP")
    assert bddk.sutun_index(2) == (2, "YP")
    assert bddk.sutun_index("TOPLAM") == (3, "Toplam")
    with pytest.raises(bddk.BddkError):
        bddk.sutun_index("EUR")


def test_series_title_check():
    bddk._check_series_title("Toplam Krediler (2+10) (TRY) [Toplam] [Sektör]", "TL", "Toplam", "Sektör")
    with pytest.raises(bddk.BddkError):
        bddk._check_series_title("Toplam Krediler (2+10) (TRY) [Toplam] [Sektör]", "USD", "Toplam", "Sektör")
    with pytest.raises(bddk.BddkError):
        bddk._check_series_title("Toplam Krediler (2+10) (TRY) [Toplam] [Kamu]", "TL", "Toplam", "Sektör")


def test_ssl_context_always_verifies():
    ctx = bddk.ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    src = Path(bddk.__file__).read_text(encoding="utf-8")
    assert "verify=False" not in src and "CERT_NONE" not in src


# --------------------------------------------------------------------------
# FinTürk
# --------------------------------------------------------------------------

def _finturk_meta():
    return bddk.parse_finturk_page(_text("finturk_page.html"))


def test_finturk_page_metadata():
    m = _finturk_meta()
    assert m.tables[1] == "Krediler (Bin TL)" and len(m.tables) == 7
    assert m.periods[0] == "2026-9" and m.periods[-1] == "2007-12"
    assert all(int(p.split("-")[1]) in (3, 6, 9, 12) for p in m.periods)
    # FinTürk's own codes (differ from Aylık Bülten!)
    assert m.taraf["10003"] == "KALKINMA VE YATIRIM" and m.taraf["10004"] == "KATILIM"
    assert m.sehir[0] == "HEPSİ"  # dotted capital İ -- "HEPSI" makes the server 500
    assert "İSTANBUL" in m.sehir and "YURT DIŞI" not in m.sehir[:1]


def test_finturk_periods():
    m = _finturk_meta()
    assert bddk._resolve_periods(m.periods, "", "", "x", 40) == ["2026-9"]
    assert bddk._resolve_periods(m.periods, "2025-09", "2026-9", "x", 40) == [
        "2025-9", "2025-12", "2026-3", "2026-6", "2026-9"]
    with pytest.raises(bddk.BddkError, match="yayımlanmamış"):
        bddk._resolve_periods(m.periods, "2026-12", "", "x", 40)
    with pytest.raises(bddk.BddkError, match="yayımlanmamış"):
        bddk._resolve_periods(m.periods, "2026-7", "", "x", 40)
    with pytest.raises(bddk.BddkError, match="en fazla"):
        bddk._resolve_periods(m.periods, "2007-12", "2026-9", "x", 40)
    with pytest.raises(bddk.BddkError):
        bddk._resolve_periods(m.periods, "dün", "", "x", 40)


def test_finturk_sehir_resolution():
    m = _finturk_meta()
    assert bddk._resolve_sehir(m, None) == ["HEPSİ"]
    assert bddk._resolve_sehir(m, ["HEPSI"]) == ["HEPSİ"]
    assert bddk._resolve_sehir(m, ["istanbul", "Mus", "IĞDIR"]) == ["İSTANBUL", "MUŞ", "IĞDIR"]
    with pytest.raises(bddk.BddkError):
        bddk._resolve_sehir(m, ["ISTANBULL"])
    with pytest.raises(bddk.BddkError):
        bddk._resolve_sehir(m, ["HEPSİ", "ANKARA"])


def test_finturk_taraf_resolution():
    m = _finturk_meta()
    assert bddk._resolve_taraf(m.taraf, None) == ["10001"]
    assert bddk._resolve_taraf(m.taraf, ["katılım"]) == ["10004"]
    with pytest.raises(bddk.BddkError, match="10001=SEKTÖR"):
        bddk._resolve_taraf(m.taraf, ["10011"])


def test_finturk_frame_numeric():
    body = _json("finturk_t1_2026-9.json")["Json"]
    frame, labels = bddk.finturk_frame(body)
    assert list(frame.columns)[:5] == ["EftKodu", "Yil", "Ay", "Sehir", "Grup"]
    assert frame.loc[0, "Sehir"] == "ADANA"
    assert frame.loc[0, "ToplamNakdiKrediler"] == 671124407
    assert frame["ToplamNakdiKrediler"].dtype.kind in "if"
    assert labels["ToplamNakdiKrediler"] == "Toplam Nakdi Krediler"
    m = _finturk_meta()
    bddk._check_finturk(frame, "2026-9", ["10001"], ["ADANA", "ADIYAMAN", "AFYONKARAHİSAR"], m)
    with pytest.raises(bddk.BddkError, match="dönem"):
        bddk._check_finturk(frame, "2026-6", ["10001"], ["ADANA"], m)
    # a requested group the server has no rows for is reported, not hidden
    notes = bddk._check_finturk(frame, "2026-9", ["10001", "10003"], ["ADANA", "ADIYAMAN", "AFYONKARAHİSAR"], m)
    assert any("10003" in n for n in notes)
    # rows for a group that was NOT requested are an error
    with pytest.raises(bddk.BddkError, match="istenmeyen"):
        bddk._check_finturk(frame, "2026-9", ["10004"], ["ADANA"], m)


def test_finturk_request_shape(monkeypatch):
    """Regression for the HTTP 500 root cause: a concrete donem and 'HEPSİ' are sent."""
    meta = _finturk_meta()
    body = json.loads(_text("finturk_t1_2026-9.json"))
    rows = [{"cell": [10001, 2026, 9, f"IL{i}", "SEKTÖR", 1, 1, 0, 0]} for i in range(82)]
    body["Json"]["data"]["rows"] = rows
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text=_text("finturk_page.html"))
        form = parse_qs(request.content.decode())
        seen.append(form)
        if form.get("donem") != ["2026-9"] or form.get("sehirList[0]") != ["HEPSİ"]:
            return httpx.Response(500, text="Runtime Error")
        return httpx.Response(200, json=body)

    real_init = bddk.Session.__init__

    def fake_init(self, budget=bddk.TOTAL_BUDGET):
        real_init(self, budget)
        self.client = httpx.Client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(bddk.Session, "__init__", fake_init)
    monkeypatch.setitem(bddk._meta_cache, "finturk", (1e18, meta))
    frame, info = bddk.BddkClient().get_dataframe(tablo_no=1)
    assert info["donemler"] == ["2026-9"] and frame.shape[0] == 82
    assert seen[0]["tabloNo"] == ["1"] and seen[0]["tarafList[0]"] == ["10001"]
    # table 7 (gold) starts in 2015-9: older periods fail fast with a clear message
    with pytest.raises(bddk.BddkError, match="2015-9"):
        bddk.BddkClient().get_dataframe(tablo_no=7, donem="2015-6")
    assert len(seen) == 1


# --------------------------------------------------------------------------
# Aylık
# --------------------------------------------------------------------------

def test_aylik_page_metadata():
    m = bddk.parse_aylik_page(_text("aylik_page.html"))
    assert m.latest == (2026, 9)
    assert len(m.tables) == 17 and m.tables[15] == "Rasyolar"
    assert m.taraf["10003"] == "Katılım" and m.taraf["10004"] == "Kalkınma ve Yatırım"
    assert m.para == ["TL", "USD"]
    assert m.all_taraf["10014"] == "Kalkınma ve Yatırım-Yerli Özel"
    assert min(m.years) == 2002


def test_aylik_periods():
    m = bddk.parse_aylik_page(_text("aylik_page.html"))
    assert bddk._aylik_periods(m, "", "") == [(2026, 9)]
    assert bddk._aylik_periods(m, "2025-11", "2026-02") == [(2025, 11), (2025, 12), (2026, 1), (2026, 2)]
    with pytest.raises(bddk.BddkError, match="henüz yayımlanmadı"):
        bddk._aylik_periods(m, "2026-10", "")
    with pytest.raises(bddk.BddkError, match="en fazla"):
        bddk._aylik_periods(m, "2020-01", "2026-09")


def test_aylik_frame():
    body = _json("aylik_t1_2026-09.json")["Json"]
    frame = bddk.aylik_frame(body)
    assert list(frame.columns) == ["taraf", "sira", "kalem", "bicim", "TP", "YP", "Toplam"]
    first = frame.iloc[0]
    assert (first["kalem"], first["TP"], first["YP"], first["Toplam"]) == ("Nakit Değerler", 73959, 447993, 521952)
    assert bddk.aylik_unit(body["caption"]) == "milyon TL"
    r = _json("aylik_t15_2026-09.json")["Json"]
    rf = bddk.aylik_frame(r)
    assert rf.iloc[0]["Rasyo"] == pytest.approx(3.053786)
    assert bddk.aylik_unit(r["caption"]).startswith("%")
    assert bddk.aylik_unit("Sektörel Kredi Dağılımı (bin TL), Dönem:2026/9") == "bin TL"
    assert bddk.aylik_unit("Bilanço (milyon $), Dönem:2026/9") == "milyon $"


# --------------------------------------------------------------------------
# Haftalık
# --------------------------------------------------------------------------

def test_haftalik_page_metadata():
    m = bddk.parse_haftalik_page(_text("haftalik_page.html"))
    assert m.tables["289"] == "Krediler" and m.tables["297"] == "Yabancı Para Pozisyonu"
    assert m.taraf["10005"] == "Kamu" and m.taraf["10007"] == "Yerli Özel"
    assert m.periods[0].tarih == dt.date(2026, 10, 30) and m.periods[0].donem_id == "670"
    assert m.periods[-1].tarih == dt.date(2014, 1, 3)
    assert m.para == ["TL", "USD"]
    assert bddk._form_token(_text("haftalik_page.html"), "TarafSec")


def test_haftalik_table_parse():
    page = _text("haftalik_page.html")
    parsed = bddk.parse_haftalik_table(page)
    assert (parsed["taraf"], parsed["tablo"], parsed["birim"]) == ("Sektör", "Krediler", "Milyon TL")
    assert parsed["tarih"] == dt.date(2026, 10, 30)
    f = parsed["frame"]
    top = f.iloc[0]
    assert top["kalem"] == "Toplam Krediler (2+10)" and top["kalem_id"] == "1.0.1"
    assert top["TOPLAM"] == pytest.approx(29000779.36604)
    assert f["kalem_id"].notna().all()
    m = bddk.parse_haftalik_page(page)
    bddk.HaftalikClient._verify(parsed, m, "289", "10001", m.periods[0], "TL")
    with pytest.raises(bddk.BddkError, match="oturum"):
        bddk.HaftalikClient._verify(parsed, m, "289", "10004", m.periods[0], "TL")
    with pytest.raises(bddk.BddkError, match="oturum"):
        bddk.HaftalikClient._verify(parsed, m, "289", "10001", m.periods[0], "USD")


def test_haftalik_pick_period():
    m = bddk.parse_haftalik_page(_text("haftalik_page.html"))
    assert bddk.HaftalikClient._pick_period(m, "").donem_id == "670"
    assert bddk.HaftalikClient._pick_period(m, "2026-10-23").donem_id == "669"
    with pytest.raises(bddk.BddkError, match="En yakın"):
        bddk.HaftalikClient._pick_period(m, "2026-10-31")


# --------------------------------------------------------------------------
# Günlük
# --------------------------------------------------------------------------

def test_gunluk_parse():
    out = bddk.parse_gunluk_page(_text("gunluk_page.html"))
    tl, usd = out["TL"], out["USD"]
    assert tl["tarih"] == usd["tarih"] == dt.date(2026, 11, 5)
    assert tl["birim"] == "Milyon TL" and usd["birim"] == "Milyon USD"
    f = tl["frame"]
    nakit = f[f.kalem == "Nakit Değerler"].iloc[0]
    assert (nakit["kalem_id"], nakit["TP"], nakit["TOPLAM"]) == ("1.1.0", 62186.36, 482570.68)
    assert nakit["bolum"] == "AKTİFLER"
    # the same label appears under assets and liabilities -> ids resolved in order
    yurtici = f[f.kalem == "-Yurtiçi Bankalar"]
    assert list(yurtici["kalem_id"]) == ["1.4.1", "2.2.1"]
    assert list(yurtici["bolum"]) == ["AKTİFLER", "PASİFLER"]
    vadesiz = f[f.kalem == "-Vadesiz Mevduat"].iloc[0]
    assert vadesiz["alt_bolum"] == "Vadeye Göre"


# --------------------------------------------------------------------------
# BDMK
# --------------------------------------------------------------------------

def test_bdmk_page_and_tables():
    m = bddk.parse_bdmk_page(_text("bdmk_vys_page.html"), "varlik_yonetim")
    assert m.tables == {"14": "Bilanço", "15": "Kar/Zarar", "16": "Bilanço Dışı İşlemler", "17": "Diğer Bilgiler"}
    assert m.quarterly and m.latest == (2026, 6)
    landing = bddk.parse_bdmk_table(_text("bdmk_vys_page.html"))
    assert landing["donem"] == (2026, 6) and landing["birim"] == "Milyon TL"
    rat = bddk.parse_bdmk_table(_text("bdmk_faktoring_t7.html"))
    assert rat["birim"] == "Yüzde" and rat["sutunlar"] == ["Yüzde"]
    assert rat["frame"].iloc[0]["Yüzde"] == pytest.approx(2.87)
    assert bddk.BdmkClient._periods(m, "2025-01", "2025-12") == [(2025, 3), (2025, 6), (2025, 9), (2025, 12)]
    with pytest.raises(bddk.BddkError, match="henüz"):
        bddk.BdmkClient._periods(m, "2026-09", "")
    assert bddk._resolve_bdmk_sektor("VYS") == "varlik_yonetim"
    with pytest.raises(bddk.BddkError):
        bddk._resolve_bdmk_sektor("sigorta")


# --------------------------------------------------------------------------
# Live (bddk.org.tr)
# --------------------------------------------------------------------------

live = pytest.mark.live


@live
def test_live_tls_plain_certifi():
    # The server now sends its full chain: plain certifi verification works.
    r = httpx.get(bddk.FINTURK_PAGE, timeout=20)
    assert r.status_code == 200


@live
def test_live_finturk_default_and_errors():
    frame, info = bddk.BddkClient().get_dataframe(tablo_no=1)
    assert info["donemler"] == [info["en_son_donem"]]
    assert frame.shape[0] == 82 and frame["ToplamNakdiKrediler"].dtype.kind in "if"
    with pytest.raises(bddk.BddkError, match="yayımlanmamış"):
        bddk.BddkClient().get_dataframe(tablo_no=1, donem="2030-3")


@live
def test_live_finturk_range_city():
    frame, info = bddk.BddkClient().get_dataframe(
        tablo_no=2, donem="2025-9", donem_bitis="2026-3", taraf_list=["10006"], sehir_list=["istanbul"])
    assert info["donemler"] == ["2025-9", "2025-12", "2026-3"]
    assert set(frame["Sehir"]) == {"İSTANBUL"} and set(frame["Grup"]) == {"KAMU"}


@live
def test_live_finturk_matches_aylik_total():
    """Sum over provinces (FinTürk, bin TL) ~= Aylık Bülten sector total (bin TL)."""
    f, _ = bddk.BddkClient().get_dataframe(tablo_no=1, donem="2026-9")
    a, _ = bddk.AylikClient().get_dataframe(tablo_no=5, donem="2026-09")
    total = a[a.kalem == "TOPLAM"].iloc[0]["Toplam Nakdi Krediler"]
    assert f["ToplamNakdiKrediler"].sum() == pytest.approx(total, rel=1e-4)


@live
def test_live_aylik():
    frame, info = bddk.AylikClient().get_dataframe(tablo_no=15, taraf_list=["10003", "10011"])
    assert set(frame["taraf_kodu"]) == {"10003", "10011"}
    with pytest.raises(bddk.BddkError, match="USD"):
        bddk.AylikClient().get_dataframe(tablo_no=11, para_birimi="USD")
    with pytest.raises(bddk.BddkError, match="sadece 12"):
        bddk.AylikClient().get_dataframe(tablo_no=1, donem="2002-01")


@live
def test_live_haftalik_table_and_series():
    t, info = bddk.HaftalikClient().get_table(tablo_id="289")
    s, sinfo = bddk.HaftalikClient().get_series("1.0.1", bitis=info["tarih"])
    assert s.iloc[-1]["tarih"] == info["tarih"]
    assert s.iloc[-1]["deger"] == pytest.approx(t.iloc[0]["TOPLAM"])


@live
def test_live_gunluk():
    t, info = bddk.GunlukClient().get_table()
    s, _ = bddk.GunlukClient().get_series("1.6.0")
    assert s.iloc[-1]["tarih"] == info["tarih"]
    assert s.iloc[-1]["deger"] == pytest.approx(t[t.kalem_id == "1.6.0"].iloc[0]["TOPLAM"], abs=0.01)


@live
def test_live_bdmk():
    f, info = bddk.BdmkClient().get_dataframe("faktoring", "7")
    assert info["birim"] == "Yüzde" and f.shape[0] > 10
