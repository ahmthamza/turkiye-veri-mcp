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


def _gelir_sheet(month, tail):
    return [
        [f"2026 YILI {month} ÖZEL BÜTÇELİ İDARELER GELİRLERİ (KÜMÜLATİF)"],
        [None, None, None, "(Bin TL)"],
        [None, "Tahakkuk", "Tahsilat", "%"],
        ["Özel Bütçe Gelirleri", 10.0, 8.0, 80.0],
        ["I-Vergi Gelirleri", 4.0, 3.0, 75.0],
        ["Diğer Harçlar", None, None, None],  # boş kalem / bölüm başlığı: tablo sürer
        ["Gemi Sağlık Resmi", 4.0, 3.0, 75.0],
        ["II-Teşebbüs ve Mülkiyet Gelirleri", 6.0, 5.0, 83.3],
        *tail,
    ]


def test_parse_sheet_crosstab_continues_past_empty_labelled_row():
    # 2026-11: önceki sürüm "Diğer Harçlar" satırında duruyordu (ilk 2 satır)
    tail = [[None], ["* Bütçe hedefi kanun ödeneğidir.", None, None, None], ["(1) Not satırı", 99.0, None, None]]
    content = _xlsx({"EYLÜL": _gelir_sheet("EYLÜL", tail)})
    f, _ = hmb.auto_parse(content)
    assert f is not None and f.attrs["meta"]["duzen"].startswith("sayfa başına")
    assert f.drop_duplicates("satir").satir.tolist() == [
        "Özel Bütçe Gelirleri", "I-Vergi Gelirleri", "Gemi Sağlık Resmi", "II-Teşebbüs ve Mülkiyet Gelirleri",
    ]
    assert f.drop_duplicates("satir").satir_sira.tolist() == [1, 2, 3, 4]
    tl = f[f.kategori == "Tahakkuk"].set_index("satir").deger
    assert tl["I-Vergi Gelirleri"] + tl["II-Teşebbüs ve Mülkiyet Gelirleri"] == tl["Özel Bütçe Gelirleri"]
    # dipnot (sayılı olsa bile) veri olarak alınmaz, uyarı düşülür
    assert "Not satırı" not in set(f.satir) and "(1) Not satırı" not in set(f.satir)
    assert "bitti sayıldı" in f.attrs["meta"]["uyari"]


def test_parse_sheet_crosstab_footnote_tail_and_stacked_table():
    # yalnız dipnot kuyruğu: uyarı yok
    plain = hmb.parse_sheet_crosstab(_xlsx({"EYLÜL": _gelir_sheet("EYLÜL", [["Kaynak: HMB"]])}))
    assert "uyari" not in plain.attrs["meta"] and plain.satir.nunique() == 4
    # alt alta ikinci tablo (başlık + sütun başlıkları): birleştirilmez, uyarı
    second = [["2025 YILI ÖZEL BÜTÇELİ İDARELER GELİRLERİ"], [None, "Tahakkuk", "Tahsilat", "%"], ["Özel Bütçe Gelirleri", 7.0, 6.0, 85.7]]
    f = hmb.parse_sheet_crosstab(_xlsx({"EYLÜL": _gelir_sheet("EYLÜL", second)}))
    assert f.satir.nunique() == 4 and len(f[f.satir == "Özel Bütçe Gelirleri"]) == 3
    assert "1 sayısal satır" in f.attrs["meta"]["uyari"]
    # tablonun içindeki etiketsiz sayısal satır hâlâ reddedilir
    bad = _gelir_sheet("EYLÜL", [])
    bad.insert(5, [None, 1.0, 1.0, 100.0])
    with pytest.raises(hmb.HmbLayoutError):
        hmb.parse_sheet_crosstab(_xlsx({"EYLÜL": bad}))


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


# ---------------------------------------------------------------- D: kalem x dönem aralığı (2026-11-12)


def test_range_token_and_head():
    assert hmb.range_token("Ocak-Mart") == (1, 3)
    assert hmb.range_token("NİSAN-HAZİRAN") == (4, 6)
    assert hmb.range_token("Ocak - Mart Dönemi") == (1, 3)
    assert hmb.range_token("(OCAK-EYLÜL)") == (1, 9)
    assert hmb.range_token(" II. Çeyrek") == (4, 6)
    assert hmb.range_token("Mart-Ocak") is None and hmb.range_token("Ocak") is None
    h = hmb._classify_head(["Ocak-Aralık            % Dağılım"])
    assert h.pct and h.aralik == (1, 12)
    h = hmb._classify_head([2009.0, "Bütçe", "Tahmini"])
    assert h.yil == 2009 and not h.pct
    assert hmb._classify_head(["Harcama", "Toplamı"]).toplam
    assert hmb._title_years("2006 YILI BELEDİYELER BÜTÇE GELİRLERİ (2007 Yılı Bazında)") == [2006]


def _gfs(ranges=("Ocak-Mart", "Ocak-Haziran"), title="GENEL YÖNETİM 2014 YILI GELİRLERİN EKONOMİK SINIFLANDIRILMASI TABLOSU (Bin TL)"):
    return [
        [title],
        [],
        ["", *ranges, "Ocak-Haziran % Dağılım"],
        ["FAALİYET GELİRLERİ TOPLAMI", 100.0, 210.0, 100.0],
        ["Vergi Gelirleri", 60.0, 130.0, 61.9],
    ]


def test_kalem_cumulative_ranges_and_form_sheets_skipped():
    form = [["ÖDEME EMRİ BELGESİ"], ["Muhasebe Birimi Kodu"], ["Hesap No", "", "Kurumsal Kod"], ["", "", 1.0, 2.0]]
    f = hmb.parse_kalem_tablosu(_xlsx({"GYMY ÖRNEK 1": form, "Sayfa1": _gfs()}))
    assert f.attrs["meta"]["donem_tipi"] == "kümülatif"
    r = f[(f.kalem == "Vergi Gelirleri") & (f.donem == "2014-06") & (f.birim == "bin TL")]
    assert r.deger.item() == 130.0 and r.donem_araligi.item() == "Ocak-Haziran"
    assert set(f[f.birim == "%"].donem) == {"2014-06"}
    assert "GYMY ÖRNEK 1" in f.attrs["meta"]["atlanan_sayfalar"]
    with pytest.raises(hmb.HmbLayoutError, match="birim"):
        hmb.parse_kalem_tablosu(_xlsx({"s": _gfs(title="GENEL YÖNETİM 2014 YILI GELİRLER")}))
    with pytest.raises(hmb.HmbLayoutError, match="ne kümülatif ne ardışık"):
        hmb.parse_kalem_tablosu(_xlsx({"s": _gfs(ranges=("Ocak-Mart", "Temmuz-Eylül"))}))


def _ekod(q2_total=12.0, toplam_ok=True, unit="Bin TL", k=1.0):
    rows = [
        ["2026 YILI MAHALLİ İDARELER BÜTÇE GİDERLERİNİN EKONOMİK SINIFLANDIRILMASI TABLOSU"],
        ["", "", "", "", "", "", "", "", unit],
        ["", "", "", "", "OCAK-MART", "NİSAN-HAZİRAN", "TEMMUZ-EYLÜL", "EKİM-ARALIK", "TOPLAM"],
        [0.0, 0.0, 0.0, "BÜTÇE GİDERLERİ HESABI", 10.0, q2_total, "", "", 22.0 if toplam_ok else 99.0],
        [1.0, 0.0, 0.0, "Personel Giderleri", 6.0, 7.0, "", "", 13.0],
        [1.0, 1.0, 0.0, "Memurlar", 6.0, 7.0, "", "", 13.0],
        ["02", 0.0, 0.0, "SGK Devlet Primi", 4.0, 5.0, "", "", 9.0],
    ]
    return [[c * k if isinstance(c, float) and j > 3 else c for j, c in enumerate(r)] for r in rows]


def test_kalem_quarterly_codes_hierarchy_and_toplam():
    f = hmb.parse_kalem_tablosu(_xlsx({"Y1-6": _ekod()}))
    m = f.attrs["meta"]
    assert m["donem_tipi"] == "dönem içi" and m["donemler"] == ["2026-03", "2026-06"]  # boş çeyrekler atlanır
    assert set(f.kod) == {"", "1", "1.1", "2"}
    assert any("üst kalem = alt kalemler toplamı" in k for k in m["kontroller"])
    assert any("TOPLAM = dönemler toplamı" in k for k in m["kontroller"])
    with pytest.raises(hmb.HmbLayoutError, match="hiyerarşi"):
        hmb.parse_kalem_tablosu(_xlsx({"Y1-6": _ekod(q2_total=40.0, toplam_ok=False)}))
    with pytest.raises(hmb.HmbLayoutError, match="TOPLAM|hiyerarşi"):
        hmb.parse_kalem_tablosu(_xlsx({"Y1-6": _ekod(toplam_ok=False)}))


def test_kalem_stock_two_blocks():
    rows = [
        [" GENEL YÖNETİM 2014 YILI MALİ VARLIK VE YÜKÜMLÜLÜKLER TABLOSU (Bin TL)"],
        ["", "MALİ VARLIĞIN TÜRÜ", "Ocak-Mart", "Ocak-Haziran", "", "YÜKÜMLÜLÜĞÜN TÜRÜ", "Ocak-Mart", "Ocak-Haziran"],
        [100.0, "KASA", 5.0, 6.0, 300.0, "BANKA KREDİLERİ", 7.0, 8.0],
        [102.0, "BANKA", 50.0, 60.0, 303.0, "KAMU İDARELERİNE MALİ BORÇLAR", 9.0, 10.0],
        ["MALİ VARLIKLAR TOPLAMI (A)", "", 55.0, 66.0, "YÜKÜMLÜLÜKLER TOPLAMI (B)", "", 16.0, 18.0],
    ]
    f = hmb.parse_kalem_tablosu(_xlsx({"MALİ VARLIK": rows}))
    assert f.attrs["meta"]["donem_tipi"] == "dönem sonu bakiye"
    kasa = f[(f.kod == "100") & (f.donem == "2014-06")]
    banka_kredi = f[(f.kod == "300") & (f.donem == "2014-06")]
    assert kasa.deger.item() == 6.0 and banka_kredi.deger.item() == 8.0
    assert set(f.bolum) == {"MALİ VARLIĞIN TÜRÜ", "YÜKÜMLÜLÜĞÜN TÜRÜ"}
    assert f[f.kalem == "YÜKÜMLÜLÜKLER TOPLAMI (B)"].bolum.iloc[0] == "YÜKÜMLÜLÜĞÜN TÜRÜ"


def test_kalem_months_need_toplam_evidence_and_closed_year_rule():
    def sheet(total):
        return [
            ["2003 KONSOLİDE BÜTÇE GİDERLERİ AYLIK GERÇEKLEŞMELER"],
            [" ", "Oca", "Şub", "", 2003.0],
            ["(Milyar TL)", "", "", "Harcama", "Bütçe"],
            [" ", "", "", "Toplamı", "Hedefi"],
            ["Giderler", 10.0, 20.0, total, 300.0],
            ["  Cari", 4.0, 5.0, 9.0, 100.0],
        ]

    f = hmb.parse_kalem_tablosu(_xlsx({"T2": sheet(30.0)}))
    assert set(f[f.donem_tipi == "aylık"].donem) == {"2003-01", "2003-02"}
    assert f[f.sutun.str.contains("Hedefi")].donem_tipi.iloc[0] == "yıllık"
    with pytest.raises(hmb.HmbLayoutError, match="ay içi mi kümülatif mi"):
        hmb.parse_kalem_tablosu(_xlsx({"T2": sheet(99.0)}))
    # dönem yalnızca başlıktaki yıl: kapanmış yıl -> yıllık, içinde bulunulan yıl -> ret
    import datetime

    def single(year):
        return [[f"{year} YILI BELEDİYELER BÜTÇE GELİRLERİ"], ["", "Bin YTL" if year < 2009 else "Bin TL"],
                ["BÜTÇE GELİRLERİ", 100.0], ["Vergi Gelirleri", 40.0]]

    f = hmb.parse_kalem_tablosu(_xlsx({"YB": single(2006)}))
    assert f.attrs["meta"]["donem_tipi"] == "yıllık" and set(f.donem) == {"2006"}
    with pytest.raises(hmb.HmbLayoutError, match="kapanmadığı"):
        hmb.parse_kalem_tablosu(_xlsx({"YB": single(datetime.date.today().year)}))


def test_kalem_refuses_revision_tables_and_accounting_layouts():
    import datetime

    rev = [["2015 YILI NAKİT DENGESİ REVİZYON (Bin TL)"], ["Referans Dönem", "", " I. Çeyrek"]] + [
        [datetime.date(2015, m, 1), "", 1.0] for m in range(6, 10)
    ]
    with pytest.raises(hmb.HmbLayoutError, match="revizyon"):
        hmb.parse_kalem_tablosu(_xlsx({"Nakit": rev}))


# ---------------------------------------------------------------- E: mizan / bilanço


def _mizan(alacak_total=150.0, kalan_shift=0.0, title="GENEL YÖNETİM 2014 YILI OCAK-EYLÜL DÖNEMİ MİZANI (Bin TL)"):
    return [
        [title],
        [],
        ["HESAP KODU", "HESAP ADI", "BORÇ TUTARI", "ALACAK TUTARI", "BORÇ KALANI", "ALACAK KALANI"],
        [1.0, "DÖNEN VARLIKLAR", 150.0, 100.0, 50.0, 0.0],
        [10.0, "HAZIR DEĞERLER", 150.0, 100.0, 50.0, 0.0],
        [100.0, "KASA", 100.0, 70.0, 30.0 + kalan_shift, 0.0],
        [102.0, "BANKA", 50.0, 30.0, 20.0, 0.0],
        [3.0, "KISA VADELİ YABANCI KAYNAKLAR", 0.0, 50.0, 0.0, 50.0],
        [30.0, "İÇ MALİ BORÇLAR", 0.0, 50.0, 0.0, 50.0],
        [300.0, "BANKA KREDİLERİ", 0.0, 50.0, 0.0, 50.0],
        ["TOPLAM", "", 150.0, alacak_total, 50.0, 50.0],
        ["1) Not"],
    ]


def test_parse_mizan():
    f = hmb.parse_mizan(_xlsx({"MİZAN": _mizan()}))
    m = f.attrs["meta"]
    assert m["donemler"] == ["2014-09"]
    k = f[(f.kod == "100") & (f.sutun == "Borç Kalanı")]
    assert k.deger.item() == 30.0 and k.donem_tipi.item() == "dönem sonu bakiye" and k.duzey.item() == 3
    assert set(f[f.sutun == "Borç Tutarı"].donem_tipi) == {"kümülatif (yıl başı açılış dahil)"}
    assert any("borç tutarı = alacak tutarı" in c for c in m["kontroller"])
    with pytest.raises(hmb.HmbLayoutError, match="TOPLAM"):
        hmb.parse_mizan(_xlsx({"MİZAN": _mizan(alacak_total=140.0)}))
    with pytest.raises(hmb.HmbLayoutError, match="kalan"):
        hmb.parse_mizan(_xlsx({"MİZAN": _mizan(kalan_shift=5.0)}))
    f = hmb.parse_mizan(_xlsx({"MİZAN": _mizan(title="DDK 2012 YILI KESİN MİZANI (Bin TL)")}))
    assert f.attrs["meta"]["donemler"] == ["2012-12"]
    # mizan biçimli dosyayı genel kalem ayrıştırıcısı almaz
    with pytest.raises(hmb.HmbLayoutError, match="mizan"):
        hmb.parse_kalem_tablosu(_xlsx({"MİZAN": _mizan(alacak_total=140.0)}))


def _bilanco(pasif_total=100.0, years=None):
    y = years or ["", ""]
    return [
        ["GENEL YÖNETİM 2012 YILI KONSOLİDE BİLANÇOSU (Bin TL)"],
        ["AKTİF (VARLIKLAR)", "", "", y[0], "PASİF (KAYNAKLAR)", "", "", y[1]],
        [1.0, "DÖNEN VARLIKLAR", "", 100.0, 3.0, "KISA VADELİ YABANCI KAYNAKLAR", "", 60.0],
        [10.0, "HAZIR DEĞERLER", "", 70.0, 30.0, "İÇ MALİ BORÇLAR", "", 60.0],
        ["", 100.0, "KASA", 30.0, "", 300.0, "BANKA KREDİLERİ", 60.0],
        ["", 102.0, "BANKA", 40.0, 5.0, "ÖZ KAYNAKLAR", "", 40.0],
        ["", "DİĞER ALACAKLAR", "", 30.0, 57.0, "GEÇMİŞ YILLAR OLUMLU", "", 40.0],
        [14.0, 140.0, "KİŞİLERDEN ALACAKLAR", 30.0, "", 570.0, "GEÇMİŞ YILLAR OLUMLU", 40.0],  # 14 bir satır kaymış
        ["", "", "", "", "", "2010 Yılı Olumlu Faaliyet Sonucu", "", 15.0],
        ["", "", "", "", "", "2011 Yılı Olumlu Faaliyet Sonucu", "", 25.0],
        ["", "", "AKTİF TOPLAMI", 100.0, "", "", "PASİF TOPLAMI", pasif_total],
    ]


def test_parse_bilanco_shifted_codes_and_balance():
    f = hmb.parse_bilanco(_xlsx({"BİLANÇO": _bilanco()}))
    m = f.attrs["meta"]
    assert m["donemler"] == ["2012"] and m["donem_tipi"] == "yıl sonu bakiye"
    akt = f[f.bolum == "Aktif (Varlıklar)"].set_index("kalem")
    assert akt.loc["DİĞER ALACAKLAR", "kod"] == "14"  # kod alt hesaptan türetildi
    assert any("aktif toplamı = pasif toplamı" in c for c in m["kontroller"])
    alt = f[f.kalem.str.startswith("2010 Yılı")]
    assert alt.kod.isna().all()  # 570'in alt kırılımı, hiyerarşiye girmez
    with pytest.raises(hmb.HmbLayoutError, match="doğrulanamadı"):
        hmb.parse_bilanco(_xlsx({"BİLANÇO": _bilanco(pasif_total=90.0)}))


def test_auto_parse_refuses_duplicate_cells_and_routes_bilanco():
    # dünkü geniş ayrıştırıcı yan yana aktif|pasif yıllarını aynı kaleme yazıyordu
    rows = _bilanco(years=[2012.0, 2012.0])
    for r in rows[2:]:
        r += [""] * (8 - len(r))
    years_rows = [["GENEL YÖNETİM 2011-2013 YILLARI KARŞILAŞTIRMALI BİLANÇOSU (Bin TL)"],
                  ["AKTİF (VARLIKLAR)", "", "", 2011.0, 2012.0, 2013.0, "PASİF (KAYNAKLAR)", "", "", 2011.0, 2012.0, 2013.0]]
    body = []
    for r in rows[2:]:
        a, p = r[:4], r[4:8]
        body.append(a[:3] + [a[3]] * 3 + p[:3] + [p[3]] * 3 if a[3] != "" or p[3] != "" else a + p)
    f, reasons = hmb.auto_parse(_xlsx({"BİLANÇO": years_rows + body}))
    assert f is not None and f.attrs["meta"]["duzen"].startswith("bilanço")
    assert sorted(set(f.donem)) == ["2011", "2012", "2013"]
    assert any("aynı satır/sütun/dönem" in r for r in reasons)  # geniş ayrıştırıcı reddedildi


def test_fetch_file_unit_from_folder_sibling(monkeypatch):
    client = hmb.HmbClient()
    unitless = _xlsx({"YBIR-6": _ekod(unit="", k=1000.0)})
    labelled = [
        ["2026 YILI MAHALLİ İDARELER BÜTÇE DENGE TABLOSU"], ["", "", "", "", "", "(Bin TL)"],
        ["", "OCAK-MART", "NİSAN-HAZİRAN", "TEMMUZ-EYLÜL", "EKİM-ARALIK", "TOPLAM"],
        ["Harcamalar", 10000.0, 12000.0, "", "", 22000.0], ["Gelirler", 8000.0, 9000.0, "", "", 17000.0],
    ]
    files = {"https://ms.hmb.gov.tr/u.xls": unitless, "https://ms.hmb.gov.tr/d.xls": _xlsx({"Özet": labelled})}
    monkeypatch.setattr(client, "download", lambda url: files[url])
    monkeypatch.setattr(client, "list_files", lambda fid, name: [
        hmb.HmbFile("Ekod4", "https://ms.hmb.gov.tr/u.xls"), hmb.HmbFile("Denge", "https://ms.hmb.gov.tr/d.xls")])
    f, _, meta = client.fetch_file("https://ms.hmb.gov.tr/u.xls")
    assert f is None and "birim" in meta["neden"]  # klasör bilgisi yoksa ret
    f, _, meta = client.fetch_file("https://ms.hmb.gov.tr/u.xls", klasor_id=1, klasor_adi="x")
    assert f is not None and "doğrulandı" in meta["kaynak_birim"][0]
    labelled[3][2] = 13000.0  # ikinci çeyrek tutmazsa ret
    labelled[3][5] = 23000.0
    files["https://ms.hmb.gov.tr/d.xls"] = _xlsx({"Özet": labelled})
    f, _, meta = client.fetch_file("https://ms.hmb.gov.tr/u.xls", klasor_id=1, klasor_adi="x")
    assert f is None and "eşleşen" in meta["neden"]


# ---------------------------------------------------------------- canlı (yeni aileler)


def _live_file(client, slug, path_contains, label_contains):
    tree = client.section_tree(slug)
    for path, node in tree.walk():
        if node.id is None or node.children:
            continue
        if all(p in " > ".join(path) for p in path_contains):
            for f in client.list_files(node.id, node.name or ""):
                if label_contains in f.etiket:
                    return f, node
    pytest.skip(f"{slug}: {path_contains}/{label_contains} bulunamadı")


@pytest.mark.live
def test_live_mizan_and_bilanco(live_client):
    f, node = _live_file(live_client, "genel-yonetim-sektoru-mali-istatistikleri-2", ["2014", "Mizan"], "Kesin")
    frame, _, meta = live_client.fetch_file(f.url, dosya_etiketi=f.etiket)
    assert frame is not None and meta["duzen"].startswith("mizan") and meta["donemler"] == ["2014-12"]
    f, node = _live_file(live_client, "genel-yonetim-mali-raporlama", ["Merkezi Yönetim"], "Bilanço")
    frame, _, meta = live_client.fetch_file(f.url, dosya_etiketi=f.etiket)
    assert frame is not None and meta["duzen"].startswith("bilanço")
    assert any("aktif toplamı = pasif toplamı" in c for c in meta["kontroller"])


@pytest.mark.live
def test_live_quarterly_ekod4_with_unit_sibling(live_client):
    f, node = _live_file(live_client, "mahalli-idareler-butce-istatistikleri", ["2026", "Gider"], "Mahalli İdare Birlikleri Bütçe Giderleri Ekod4")
    frame, _, meta = live_client.fetch_file(f.url, dosya_etiketi=f.etiket, klasor_id=node.id, klasor_adi=node.name)
    assert frame is not None, meta.get("neden")
    assert meta["donem_tipi"] == "dönem içi" and "doğrulandı" in meta["kaynak_birim"][0]
