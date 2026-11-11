"""HMB modülü testleri.

Çevrimdışı testler küçük, sentetik çalışma kitaplarıyla (openpyxl ile bellekte
üretilir) ayrıştırma / birim / dönem mantığını sınar. ``@pytest.mark.live``
testleri muhasebat.hmb.gov.tr'ye gerçekten bağlanır.
"""

from __future__ import annotations

import io
import json

import pytest
from openpyxl import Workbook

from turkiye_veri_mcp import hmb


def _xlsx(sheets: dict[str, list[list]]) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- metin / ay


def test_fold_handles_turkish_dotted_i():
    assert hmb.fold("İSTANBUL  İLİ") == "istanbul ili"
    assert hmb.fold("IĞDIR") == "igdir"
    assert hmb.fold("Şanlıurfa") == "sanliurfa"


@pytest.mark.parametrize(
    "cell,expected",
    [("Ocak", 1), ("OCAK", 1), ("Oca(*)", 1), ("Şub", 2), ("MAYIS ", 5), ("NİSAN", 4),
     ("Ağu", 8), ("ARALIK", 12), ("Ocak 2026", None), ("Toplam", None), (3, None)],
)
def test_month_number(cell, expected):
    assert hmb.month_number(cell) == expected


def test_months_in_text_and_donem():
    assert hmb.months_in_text("2026 YILI OCAK-HAZİRAN DÖNEMİ") == [1, 6]
    assert hmb.months_in_text("(KÜMÜLATİF) EYLÜL 2026") == [9]
    assert hmb.donem_end_month("II. DÖNEM ") == 6
    assert hmb.donem_end_month("IV.DÖNEM") == 12
    assert hmb.donem_end_month("IV.QUARTER") == 12
    assert hmb.donem_end_month("Ocak") is None


@pytest.mark.parametrize(
    "label,code",
    [("  Adana", "01"), ("Afyon Karahisar", "03"), ("Afyon", "03"), ("K.Maraş", "46"),
     ("Urfa", "63"), ("İçel", "33"), ("Igdır", "76"), ("Iğdır", "76"), ("  Merkez", "00"),
     ("  Toplam", "TOPLAM"), ("Not : İller Bazında", None), ("", None)],
)
def test_il_kodu_of(label, code):
    assert hmb.il_kodu_of(label) == code


def test_year_of_label():
    assert hmb.year_of_label("2026 Yılı Genel Bütçe ...") == 2026
    assert hmb.year_of_label("2007  Yılı ...") == 2007
    assert hmb.year_of_label("2006 - 2025 Merkezi Yönetim") is None
    assert hmb.year_of_label("1990-2005 Mahalli İdare") is None


# ---------------------------------------------------------------- birimler


def test_unit_factors():
    # 1 milyar eski TL = 1 bin YTL = 1 bin TL: çarpan 1 (eski ×10^6 hatası)
    assert hmb.birim_carpani("(Milyar TL.)", 2004).carpan == 1.0
    assert hmb.birim_carpani("(Bin YTL)", 2007).carpan == 1.0
    assert hmb.birim_carpani("BİN TL.", 2026).carpan == 1.0
    with pytest.raises(hmb.HmbLayoutError):
        hmb.birim_carpani("(Milyar TL)", 2026)  # yeni TL milyarı mı? belirsiz
    with pytest.raises(hmb.HmbLayoutError):
        hmb.birim_carpani("(Bin TL)", 2003)  # eski TL bini: doğrulanmış dönüşüm yok
    with pytest.raises(hmb.HmbLayoutError):
        hmb.birim_carpani("(Milyon TL)", 2010)


def test_find_unit_label_ignores_notes():
    assert hmb.find_unit_label(["", "(Bin TL) "]) == "(Bin TL)"
    assert hmb.find_unit_label(["BİN TL."]) == "BİN TL."
    note = "kaynaklanan 1.369.238.833 Bin TL bütçe geliri elimine edilmiştir."
    assert hmb.find_unit_label([note]) is None


# ---------------------------------------------------------------- sayfa ağacı


PAGE_HTML = """
<ul class="menu hierarchical-content">
 <li class="menu-item-has-children"><a href="#" data-id="4041" data-name="2026 Yılı Genel Bütçe">2026 Yılı Genel Bütçe</a>
  <ul class="sub-menu"><li><a href="#" data-id="4042" data-name="Bütçe Gelir Tabloları">Bütçe Gelir Tabloları</a></li></ul></li>
 <li class="menu-item-has-children"><a href="#" data-id="989" data-name="1997 Yılı İller">1997 Yılı İller</a>
  <ul class="sub-menu"><li><a href="#" data-id="1015" data-name=" Bütçe Gider Tabloları">Bütçe Gider Tabloları</a></li></ul></li>
 <li><a href="#" data-id="436" data-name="1990-2005 Mahalli İdare ve Fon Payları">1990-2005 Mahalli İdare ve Fon Payları</a></li>
</ul>"""


def test_parse_tree_keeps_exact_data_name():
    tree = hmb.parse_tree(PAGE_HTML)
    assert [n.id for n in tree.children] == [4041, 989, 436]
    assert tree.children[1].children[0].name == " Bütçe Gider Tabloları"  # baştaki boşluk korunur
    rows = hmb.tree_to_rows(tree)
    assert rows[1]["klasor_id"] == 4042 and rows[1]["alt_klasor"] is False


class _FakeResp:
    def __init__(self, status: int, payload):
        self.status_code = status
        self._payload = payload
        self.content = json.dumps(payload).encode()

    def json(self):
        return self._payload


def test_year_folders_live_and_fallback(monkeypatch):
    hmb.clear_cache()
    client = hmb.HmbClient()

    def fake_get(url, params=None):
        if params and params.get("slug") == hmb.ROOT_STATS_SLUG:
            return _FakeResp(200, [{"content": {"rendered": ""}, "page_sidebar": "", "title": {"rendered": "x"}}])
        return _FakeResp(200, [{"content": {"rendered": PAGE_HTML}, "page_sidebar": "", "title": {"rendered": "x"}}])

    monkeypatch.setattr(client, "_get", fake_get)
    years, kaynak = client.year_folders("genel_butce_il")
    assert kaynak == "canlı"
    assert years[2026]["gelir"] == (4042, "Bütçe Gelir Tabloları")
    assert years[1997]["gider"] == (1015, " Bütçe Gider Tabloları")

    hmb.clear_cache()

    def broken(url, params=None):
        raise hmb.HmbError("ağ yok")

    monkeypatch.setattr(client, "_get", broken)
    years, kaynak = client.year_folders("merkezi_il")
    assert kaynak.startswith("yedek")
    assert years[2004]["gider"] == (980, "Bütçe Gider Tabloları")
    hmb.clear_cache()


def test_list_files_parses_links_and_rejects_wrong_name(monkeypatch):
    hmb.clear_cache()
    client = hmb.HmbClient()
    content = (
        '<ul><li><a href="https://ms.hmb.gov.tr/uploads/a&amp;b.xls" target="_blank">2026-Vergi Tah&amp;Tahs</a></li>'
        '<li><a href="https://ms.hmb.gov.tr/uploads/m.pdf">META VERİ</a></li></ul>'
    )
    monkeypatch.setattr(client, "_get", lambda url, params=None: _FakeResp(200, {"content": content}))
    files = client.list_files(1, "X")
    assert [(f.etiket, f.uzanti) for f in files] == [("2026-Vergi Tah&Tahs", "xls"), ("META VERİ", "pdf")]
    assert files[0].url.endswith("a&b.xls")
    hmb.clear_cache()
    monkeypatch.setattr(client, "_get", lambda url, params=None: _FakeResp(200, False))
    with pytest.raises(hmb.HmbError, match="data-name"):
        client.list_files(2, "yanlış ad")
    hmb.clear_cache()


def test_download_only_from_hmb():
    with pytest.raises(hmb.HmbError):
        hmb._check_hmb_url("https://example.com/x.xls")
    hmb._check_hmb_url("https://ms.hmb.gov.tr/uploads/x.xls")


# ---------------------------------------------------------------- C: il tablosu


def _il_sheet(month: str, year: int, unit: str = "(Bin TL)", il: str = "İSTANBUL İLİ ", gbg=(100.0, 80.0)):
    return [
        ["", f"{il} GENEL  BÜTÇE GELİRLERİNİN TAHSİLATI, TAHAKKUKU VE TAHSİLATIN TAHAKKUKA  ORANI (KÜMÜLATİF) {month} {year}"],
        ["", "", "OCAK", "ŞUBAT", "MART"],
        ["", unit, "Tahakkuk", "Tahsilat", "Tahsilat / Tahakkuk"],
        ["", "Genel Bütçe Gelirleri", gbg[0], gbg[1], 80.0],
        ["", "  I-Vergi Gelirleri", 90.0, 75.0, 83.3],
        ["", "      Diğer", None, None, None],
        [" Not: tahakkuk tutarları kümülatiftir."],
    ]


def test_parse_il_statement_basic():
    content = _xlsx({"EYLÜL": _il_sheet("EYLÜL", 2026), "AĞUSTOS": _il_sheet("AĞUSTOS", 2026, gbg=(90.0, 70.0))})
    f = hmb.parse_il_statement(content, "34", 2026)
    assert list(f.donem.unique()) == ["2026-08", "2026-09"]
    row = f[(f.donem == "2026-09") & (f.kalem_sira == 1)].iloc[0]
    assert (row.il, row.kalem, row.tahakkuk_bin_tl, row.tahsilat_bin_tl) == ("İstanbul", "Genel Bütçe Gelirleri", 100.0, 80.0)
    assert set(f.donem_tipi) == {"kümülatif (Ocak-ay sonu)"}
    assert f.attrs["meta"]["eksik_ay"] == list(range(1, 8))


def test_parse_il_statement_milyar_tl_not_rescaled():
    content = _xlsx({"Ocak": _il_sheet("OCAK", 2004, unit="(Milyar TL.)", gbg=(9612363.0, 2480276.0))})
    f = hmb.parse_il_statement(content, "34", 2004)
    assert f.iloc[0].tahakkuk_bin_tl == 9612363.0  # eski sürüm 9.6e12 döndürüyordu


def test_parse_il_statement_guards():
    with pytest.raises(hmb.HmbLayoutError, match="il"):
        hmb.parse_il_statement(_xlsx({"EYLÜL": _il_sheet("EYLÜL", 2026)}), "35", 2026)
    with pytest.raises(hmb.HmbLayoutError, match="uyuşmuyor"):
        hmb.parse_il_statement(_xlsx({"AĞUSTOS": _il_sheet("EYLÜL", 2026)}), "34", 2026)
    with pytest.raises(hmb.HmbLayoutError, match="birim"):
        hmb.parse_il_statement(_xlsx({"EYLÜL": _il_sheet("EYLÜL", 2026, unit="")}), "34", 2026)
    with pytest.raises(hmb.HmbLayoutError, match="2025"):
        hmb.parse_il_statement(_xlsx({"EYLÜL": _il_sheet("EYLÜL", 2026)}), "34", 2025)
    # 2004 Karaman: bir sayfanın başlığında yıl yok -> diğer sayfalar yılı taşıdığı sürece kabul
    no_year = _il_sheet("MAYIS", 2004, unit="(Milyar TL.)", il="KARAMAN")
    no_year[0][1] = no_year[0][1].replace(" 2004", "")
    f = hmb.parse_il_statement(_xlsx({"Mayıs": no_year, "Nisan": _il_sheet("NİSAN", 2004, unit="(Milyar TL.)", il="KARAMAN")}), "70", 2004)
    assert sorted(f.donem.unique()) == ["2004-04", "2004-05"]


def test_parse_il_statement_repairs_sheet_name_via_title():
    # 2004 Merkez dosyasında "Mayıs" sayfasının adı "00 Merkez"; ay başlıktan okunur
    content = _xlsx({"00 Merkez": _il_sheet("MAYIS", 2004, unit="(Milyar TL.)", il="MERKEZ")})
    f = hmb.parse_il_statement(content, "00", 2004)
    assert list(f.donem.unique()) == ["2004-05"]


# ---------------------------------------------------------------- A: il x kategori


def _cross_sheet(month: str, toplam_tl: float = 30.0, unit_cell: str = "(Bin TL) "):
    return [
        [f"2020 YILI {month} AYI İLLER BAZINDA MERKEZİ YÖNETİM BÜTÇE GELİR ve GİDERLERİNİN KARŞILAŞTIRMASI (KÜMÜLATİF)"],
        [],
        ["Ocak", "Şubat", "Mart", "Nisan"],
        ["Kasım", "Aralık", None, None, unit_cell],
        [None, "EKONOMİK SINIFLANDIRMA", None, None],
        [None, "Tahakkuk", "Tahsilat", "%"],
        [" ", "[1]", "[2]", "[2/1]"],
        ["  Merkez", 10.0, 5.0, 50.0],
        ["  Adana", 12.0, 6.0, 50.0],
        ["  Afyon Karahisar", 8.0, 4.0, 50.0],
        ["  Toplam", toplam_tl, 15.0, 50.0],
        ["Not : İller Bazında Bütçe Gelirleri ..."],
    ]


def test_parse_il_crosstab():
    content = _xlsx({"Aralık": _cross_sheet("ARALIK"), "Kasım": _cross_sheet("KASIM")})
    f = hmb.parse_il_crosstab(content, 2020, "denge")
    assert sorted(f.donem.unique()) == ["2020-11", "2020-12"]
    assert list(dict.fromkeys(f.kategori)) == [
        "EKONOMİK SINIFLANDIRMA — Tahakkuk", "EKONOMİK SINIFLANDIRMA — Tahsilat", "EKONOMİK SINIFLANDIRMA — % [2/1]"]
    afyon = f[(f.il_kodu == "03") & (f.donem == "2020-12")]
    assert set(afyon.il) == {"Afyonkarahisar"} and set(afyon.il_kaynak) == {"Afyon Karahisar"}
    pct = f[f.birim == "%"]
    assert pct.deger_bin_tl.isna().all() and (pct.deger == 50.0).all()
    assert f.attrs["meta"]["kontroller"] == ["Toplam satırı = iller toplamı (tüm TL sütunları)"]
    assert f.attrs["meta"]["il_sayisi"] == 3


def test_parse_il_crosstab_total_mismatch_reported():
    f = hmb.parse_il_crosstab(_xlsx({"Aralık": _cross_sheet("ARALIK", toplam_tl=99.0)}), 2020, "denge")
    assert any("≠ Toplam" in k for k in f.attrs["meta"]["kontroller"])


def test_parse_il_crosstab_rejects_unknown_row_and_missing_unit():
    rows = _cross_sheet("ARALIK")
    rows.insert(9, ["  Atlantis", 1.0, 1.0, 1.0])
    with pytest.raises(hmb.HmbLayoutError, match="Atlantis"):
        hmb.parse_il_crosstab(_xlsx({"Aralık": rows}), 2020, "denge")
    with pytest.raises(hmb.HmbLayoutError, match="birim"):
        hmb.parse_il_crosstab(_xlsx({"Aralık": _cross_sheet("ARALIK", unit_cell=None)}), 2020, "denge")


def test_parse_mahalli_quarters_and_unitless_flag():
    def sheet(rng):
        return [
            [f"2026 YILI {rng} DÖNEMİ   İLLER İTİBARIYLA MAHALLİ İDARELER  BÜTÇE DENGESİ"],
            [],
            [None, "BÜTÇE GELİRLERİ", "BÜTÇE GİDERLERİ", "BÜTÇE DENGESİ"],
            ["  Adana", 3.0, 2.0, 1.0],
            ["  Toplam", 3.0, 2.0, 1.0],
        ]

    content = _xlsx({"II. DÖNEM": sheet("OCAK-HAZİRAN"), "I. DÖNEM ": sheet("OCAK-MART")})
    with pytest.raises(hmb.HmbLayoutError, match="birim"):
        hmb.parse_il_crosstab(content, 2026, "mahalli_denge")  # etiketsiz: varsayım yok
    f = hmb.parse_il_crosstab(content, 2026, "mahalli_denge", birim_dogrula=True)
    assert sorted(f.donem.unique()) == ["2026-03", "2026-06"]
    assert f.attrs["meta"]["birim_dogrulanmali"] is True
    with pytest.raises(hmb.HmbLayoutError, match="uyuşmuyor"):
        hmb.parse_il_crosstab(_xlsx({"I. DÖNEM": sheet("OCAK-HAZİRAN")}), 2026, "mahalli_denge", birim_dogrula=True)


# ---------------------------------------------------------------- B: kalem x dönem


def _wide(unit="(Bin TL)", toplam_ok=True):
    months = ["Ocak", "Şubat", "Mart", "Nisan"]
    return [
        ["2026  YILI  AYLIK  İLLER BANKASI ve BÜYÜKŞEHİR BELEDİYE PAYLARI"],
        [],
        [unit, *months, "Toplam"],
        ["İller Bankası Toplamı %6.5", 10.0, 20.0, None, None, 30.0 if toplam_ok else 31.0],
        ["  Belediyeler", 4.0, 6.0, None, None, 10.0],
        ["Toplam", 14.0, 26.0, 0.0, 0.0, 40.0],  # Mart/Nisan: formül sıfırı
    ]


def test_parse_wide_monthly_verified_and_unpublished_months_dropped():
    f = hmb.parse_wide(_xlsx({"t5-26": _wide()}), yil=2026)
    assert f.attrs["meta"]["donemler"] == ["2026-01", "2026-02"]
    assert f.attrs["meta"]["donem_tipi"] == "aylık"
    assert any("yayımlanmamış" in k for k in f.attrs["meta"]["kontroller"])
    assert f[(f.kalem == "Belediyeler") & (f.donem == "2026-02")].deger.item() == 6.0
    assert f[f.kalem == "Belediyeler"].girinti.iloc[0] == 2
    toplam = f[(f.sutun == "Toplam") & (f.kalem == "Toplam")]
    assert toplam.donem.isna().all() and toplam.deger.item() == 40.0


def test_parse_wide_type_unknown_without_evidence():
    rows = [r[:5] for r in _wide()]  # Toplam sütunu yok
    rows[0] = ["2026 YILI AYLIK ŞARTA BAĞLI VARLIKLAR"]  # "aylık" tek başına kanıt değil
    f = hmb.parse_wide(_xlsx({"s": rows}), yil=2026)
    assert f.attrs["meta"]["donem_tipi"] == "belirsiz" and "uyari" in f.attrs["meta"]


def test_parse_wide_missing_unit_and_assertion():
    content = _xlsx({"s": _wide(unit="")})
    with pytest.raises(hmb.HmbLayoutError, match="birim"):
        hmb.parse_wide(content, yil=2026)
    f = hmb.parse_wide(content, yil=2026, birim_beyani="Bin TL")
    assert any("kullanıcı beyanı" in b for b in f.attrs["meta"]["kaynak_birim"])


def test_parse_wide_years_with_percent_group():
    rows = [
        ["1990-2005 MAHALLİ İDARE ve FON PAYLARI"],
        [None, "Gerçekleşmeler (Milyar TL)", None, "GSMH İçindeki Payları (%)", None],
        [None, 1990, 1991, 1990, 1991],
        ["İller Bankası Toplamı", 4571.0, 7868.0, 1.15, 1.24],
    ]
    f = hmb.parse_wide(_xlsx({"T5-7": rows}))
    assert f.attrs["meta"]["donem_tipi"] == "yıllık"
    assert set(f[f.birim == "%"].deger) == {1.15, 1.24}
    assert f[(f.birim == "bin TL") & (f.donem == "1990")].deger.item() == 4571.0


def test_parse_sheet_crosstab_and_auto_parse():
    def sheet(month):
        return [
            [f"2026 YILI {month} AYI MERKEZİ YÖNETİM FONKSİYONEL VE EKONOMİK SINIFLANDIRMA (KÜMÜLATİF)"],
            [None, None, None, "(Bin TL)"],
            [None, "Personel Giderleri", "Faiz Harcamaları", "Toplam"],
            ["Genel Kamu Hizmetleri   ", 1.0, 2.0, 3.0],
            ["TOPLAM", 1.0, 2.0, 3.0],
        ]

    content = _xlsx({"EYLÜL": sheet("EYLÜL"), "AĞUSTOS": sheet("AĞUSTOS")})
    f, reasons = hmb.auto_parse(content)
    assert f is not None and f.attrs["meta"]["duzen"].startswith("sayfa başına")
    assert f.attrs["meta"]["donem_tipi"] == "kümülatif"
    assert set(f.satir) == {"Genel Kamu Hizmetleri", "TOPLAM"}
    junk, reasons = hmb.auto_parse(_xlsx({"x": [["merhaba"], ["dünya", "a"]]}))
    assert junk is None and reasons


# ---------------------------------------------------------------- araç yardımcıları


def test_filter_and_payload(tmp_path):
    content = _xlsx({"Aralık": _cross_sheet("ARALIK"), "Kasım": _cross_sheet("KASIM")})
    f = hmb.parse_il_crosstab(content, 2020, "denge")
    g = hmb.filter_frame(f, ay=11, il_kodu="1")
    assert set(g.donem) == {"2020-11"} and set(g.il_kodu) == {"01"}
    assert g.attrs["meta"] == f.attrs["meta"]
    with pytest.raises(hmb.HmbError):
        hmb.filter_frame(f, ay=3)
    out = hmb.tool_payload(f, max_rows=2, output_path=str(tmp_path / "x.csv"))
    assert out["preview_csv"].splitlines()[1].split(",")[5] == "2020-12"  # en son dönem önce
    assert len(out["preview_csv"].splitlines()) == 3
    assert (tmp_path / "x.csv").exists()


# ---------------------------------------------------------------- canlı testler


@pytest.fixture(scope="module")
def live_client():
    hmb.clear_cache()
    return hmb.HmbClient()


@pytest.mark.live
def test_live_sections_cover_families(live_client):
    sections = live_client.sections()
    titles = " | ".join(hmb.fold(s["baslik"]) for s in sections)
    for spec in hmb.FAMILIES.values():
        assert spec["sidebar_match"] in titles


@pytest.mark.live
@pytest.mark.parametrize(
    "family,first",
    [("genel_butce_il", 2004), ("merkezi_il", 2004), ("mahalli_il", 2006), ("konsolide_il", 1990), ("paylar", 2003)],
)
def test_live_year_folders(live_client, family, first):
    years, kaynak = live_client.year_folders(family)
    assert kaynak == "canlı"
    assert min(years) == first
    if family != "konsolide_il":
        assert max(years) >= 2026


@pytest.mark.live
def test_live_il_2004_unit_regression(live_client):
    f = live_client.get_il_data("34", 2004)
    row = f[(f.donem == "2004-01") & (f.kalem_sira == 1)].iloc[0]
    assert row.kalem == "Genel Bütçe Gelirleri"
    assert row.tahakkuk_bin_tl == 9612363.0  # (Milyar TL.) = bin TL, ölçeklenmez
    assert len(f.attrs["meta"]["aylar"]) == 12


@pytest.mark.live
def test_live_il_latest_default(live_client):
    f = live_client.get_il_data("06")
    assert f.yil.iloc[0] >= 2026 and f.attrs["meta"]["eksik_ay"] == []


@pytest.mark.live
@pytest.mark.parametrize("tablo,yil", [("denge", 2010), ("gider", 1995), ("vergi", 2020), ("mahalli_gelir", 0)])
def test_live_karsilastirma(live_client, tablo, yil):
    f = live_client.get_karsilastirma_data(tablo, yil or None)
    meta = f.attrs["meta"]
    assert not any("≠" in k for k in meta["kontroller"])
    assert f.il.isin(list(hmb.IL_ADLARI.values()) + ["Toplam"]).all()


@pytest.mark.live
def test_live_paylar_monthly_verified(live_client):
    f = live_client.get_paylar("mahalli")
    assert f.attrs["meta"]["donem_tipi"] == "aylık"
    assert any("aylık akım doğrulandı" in k for k in f.attrs["meta"]["kontroller"])


@pytest.mark.live
def test_live_generic_fetch(live_client):
    years, _ = live_client.year_folders("merkezi_il")
    files = live_client.list_files(*years[2026]["denge"])
    xls = [x for x in files if x.uzanti == "xls"]
    frame, content, meta = live_client.fetch_file(xls[0].url, dosya_etiketi=xls[0].etiket)
    assert frame is not None and content[:4] == b"\xd0\xcf\x11\xe0"  # OLE2 .xls


def test_resolve_sheet_months_uses_unique_source():
    # 2008 vergi: "Temmuz" sayfasının başlığı "AĞUSTOS" (kopya) -> sayfa adı kazanır
    resolved, notes = hmb.resolve_sheet_months(
        [("Ağustos", 8, 8), ("Temmuz", 8, 7), ("Haziran", 6, 6)], single_sheet=False
    )
    assert resolved == {"Ağustos": 8, "Temmuz": 7, "Haziran": 6} and len(notes) == 1
    # 2004 Merkez: bozuk sayfa adı -> başlık kazanır
    resolved, _ = hmb.resolve_sheet_months([("00 Merkez", 5, None), ("Nisan", 4, 4)], single_sheet=False)
    assert resolved["00 Merkez"] == 5
    with pytest.raises(hmb.HmbLayoutError):
        hmb.resolve_sheet_months([("Temmuz", 8, 7)], single_sheet=True)
    # 2007 Bursa: Aralık sayfası "ANKARA" -> diğer 11 ay tekilse eleme
    entries = [(hmb.AYLAR[m - 1], m, m) for m in range(1, 12)] + [("ANKARA", None, None)]
    resolved, notes = hmb.resolve_sheet_months(entries, single_sheet=False)
    assert resolved["ANKARA"] == 12 and "eleme" not in notes[0] and "kalan tek ay" in notes[0]
    with pytest.raises(hmb.HmbLayoutError, match="belirlenemedi"):
        hmb.resolve_sheet_months(entries[:9] + [("ANKARA", None, None)], single_sheet=False)
    with pytest.raises(hmb.HmbLayoutError, match="Aynı dönem"):
        hmb.resolve_sheet_months([("A", 8, None), ("B", 8, None)], single_sheet=False)


def test_unitless_table_verified_against_sibling(monkeypatch):
    def sheet(unit, gelir_total):
        return [
            ["2022 YILI ARALIK AYI İLLER BAZINDA TABLO (KÜMÜLATİF)"],
            [None, None, unit],
            [None, "Merkezi Bütçe" if unit is None else "Tahsilat", "Diğer"],
            ["  Merkez", 1000.0, 1000.0],
            ["  Adana", 2000.0, 1000.0],
            ["  Toplam", gelir_total, 2000.0],
        ]

    client = hmb.HmbClient()
    files = {
        "gelir": hmb.HmbFile("2022-Merkezi Yönetim Kümülatif Bütçe Gelirleri Tahs.", "https://ms.hmb.gov.tr/g.xls"),
        "denge": hmb.HmbFile("2022-Merkezi Yönetim Kümülatif Bütçe Gelir ve Gider Karş.", "https://ms.hmb.gov.tr/d.xls"),
    }
    payload = {"https://ms.hmb.gov.tr/g.xls": _xlsx({"Aralık": sheet(None, 3000.0)})}
    monkeypatch.setattr(client, "_resolve", lambda fam, sub, yil: (2022, 1, "x", [files[sub]], "canlı"))
    monkeypatch.setattr(client, "download", lambda url: payload[url])
    payload["https://ms.hmb.gov.tr/d.xls"] = _xlsx({"Aralık": sheet("(Bin TL)", 3000.0)})
    f = client.get_karsilastirma_data("gelir", 2022)
    assert "doğrulandı" in f.attrs["meta"]["kaynak_birim"][0]
    payload["https://ms.hmb.gov.tr/d.xls"] = _xlsx({"Aralık": sheet("(Bin TL)", 4000.0)})
    with pytest.raises(hmb.HmbLayoutError, match="doğrulanamadı"):
        client.get_karsilastirma_data("gelir", 2022)


def test_period_cells_and_title_typos():
    assert hmb.months_in_text("2022 YILI OCAK-ARALIKI DÖNEMİ") == [1, 12]  # HMB yazım hatası
    assert hmb.is_period_cell("Ocak Haziran") and hmb.is_period_cell("Ocak-Eylül")
    assert hmb.is_period_cell("IV.QUARTER") and not hmb.is_period_cell("Pers. Giderleri")
    assert hmb._is_legend_row(["Ocak-Mart", "Ocak Haziran", "Ocak-Eylül"])
    assert not hmb.is_period_cell(" OCAK-ARALIK 1990")  # yıl taşıyan başlık hücresi
