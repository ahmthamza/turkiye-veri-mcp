"""Client for HMB (Hazine ve Maliye Bakanlığı) İller İtibarıyla Bütçe Gelirleri.

Endpoint discovered via browser DevTools (2026-08-05): a generic
file-listing API that muhasebat.hmb.gov.tr's JS-rendered pages call to
populate a folder's file list.

  GET https://muhasebat.hmb.gov.tr/portal/v2/files?name=<url-encoded folder title>&id=<folder id>

Returns {"content": "<ul>...<li><a href='...xls'>...</a></li>...</ul>"} --
HTML embedded in a JSON string -- listing every file in that folder, each
with its real download URL (including a per-file hash suffix that cannot
be predicted/constructed, only obtained from this API).

Verified end-to-end for "Genel Bütçe Gelirlerinin İller İtibarıyla Tahakkuk
ve Tahsilatı", 2026: id=4042, name="Bütçe Gelir Tabloları" -> 82 entries
(00-Merkez + 01..81), each an .xls download link. The 01-81 prefixes are
standard Turkish province plate/postal codes (34=İstanbul, 06=Ankara,
etc.) -- not invented, taken verbatim from HMB's own filenames.

Downloaded files are genuine binary XLS (OLE2/BIFF signature), but at
least some trip xlrd's strict parser on a malformed string record;
python_calamine reads them correctly and is used here instead.

Each province file has one sheet per month published so far that year
(e.g. OCAK..HAZİRAN for a file downloaded in July 2026), each sheet a
hierarchical (indented) budget-revenue statement with Tahakkuk (accrual),
Tahsilat (collection) and a ratio column for that single province+month.
Indentation encodes a category hierarchy (Genel Bütçe Gelirleri -> I-Vergi
Gelirleri -> 1. Gelir ve Kazanç... -> a) Gelir Vergisi -> ...); this module
keeps the label as-is (with its leading spaces) rather than parsing levels
out, since that structure hasn't been validated across enough files yet.

STATUS: only the 2026 table's folder id is known. Other years (2004-2025)
and other tables under "İller İtibarıyla Bütçe İstatistikleri" (Mahalli
İdareler, Merkezi Yönetim, Konsolide) use this same /portal/v2/files API
but need their own folder id, not yet captured.
"""

from __future__ import annotations

import html
import io
import re
from typing import Any

import httpx
import pandas as pd
from python_calamine import CalamineWorkbook

from turkiye_veri_mcp.tidy import _combine_header_rows, _dedupe_columns, find_header_block

FILES_API = "https://muhasebat.hmb.gov.tr/portal/v2/files"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": "https://muhasebat.hmb.gov.tr/",
}

# Bilinen klasör id'leri -- yalnızca 2026 yılı doğrulandı (2026-08-05).
# Diğer yıllar/tablolar aynı /portal/v2/files API'sini kullanıyor ama
# kendi id'lerini gerektiriyor, henüz yakalanmadı.
KNOWN_FOLDERS: dict[tuple[str, int], dict[str, Any]] = {
    ("genel-butce-gelirleri-iller", yil): {
        "id": _id,
        "name": "Bütçe Gelir Tabloları",
        "label": "Genel Bütçe Gelirlerinin İller İtibarıyla Tahakkuk ve Tahsilatı",
    }
    for yil, _id in {
        2026: 4042, 2025: 3946, 2024: 3860, 2023: 3786, 2022: 3342,
        2021: 3193, 2020: 3061, 2019: 1409, 2018: 1410, 2017: 1411,
        2016: 1412, 2015: 1413, 2014: 1414, 2013: 1415, 2012: 1416,
        2011: 1417, 2010: 1418, 2009: 1419, 2008: 1420, 2007: 1421,
        2006: 1422, 2005: 1423, 2004: 1424,
    }.items()
}
# Tüm 23 yılın id'si (2004-2026) DevTools ile tek tek tıklanıp doğrulandı
# (Claude for Chrome, 2026-08-05); 2026/2025/2024 için elde bulunan gerçek
# dosyalarla ayrıca çapraz doğrulandı. İlginç örüntü: 2020-2026 id'leri yıl
# arttıkça artıyor (daha yeni yükleme = daha yüksek id, beklenen), ama
# 2004-2019 tam tersi (2019:1409 en düşük, 2004:1424 en yüksek) -- bu 16
# yılın muhtemelen tek seferde, geriye doğru sırayla toplu yüklendiğini
# gösteriyor. id'ler yıl başına sabit bir formülle artmıyor (bkz. yukarıdaki
# 2026→2025→2024 farkları: -96, -86), bu yüzden hâlâ tahmin edilmiyor,
# her biri gerçekten tıklanıp doğrulandı.

# 00 (Merkez/ulusal toplam) + 01-81 il plaka kodu -- HMB'nin kendi dosya
# adlarından alındı (2026-08-05), tahmin edilmedi.
IL_SLUGS = {
    "00": "Merkez", "01": "Adana", "02": "Adiyaman", "03": "Afyon", "04": "Agri",
    "05": "Amasya", "06": "Ankara", "07": "Antalya", "08": "Artvin", "09": "Aydin",
    "10": "Balikesir", "11": "Bilecik", "12": "Bingol", "13": "Bitlis", "14": "Bolu",
    "15": "Burdur", "16": "Bursa", "17": "Canakkale", "18": "Cankiri", "19": "Corum",
    "20": "Denizli", "21": "Diyarbakir", "22": "Edirne", "23": "Elazig",
    "24": "Erzincan", "25": "Erzurum", "26": "Eskisehir", "27": "Gaziantep",
    "28": "Giresun", "29": "Gumushane", "30": "Hakkari", "31": "Hatay",
    "32": "Isparta", "33": "Mersin", "34": "Istanbul", "35": "Izmir",
    "36": "Kars", "37": "Kastamonu", "38": "Kayseri", "39": "Kirklareli",
    "40": "Kirsehir", "41": "Kocaeli", "42": "Konya", "43": "Kutahya",
    "44": "Malatya", "45": "Manisa", "46": "K.Maras", "47": "Mardin",
    "48": "Mugla", "49": "Mus", "50": "Nevsehir", "51": "Nigde", "52": "Ordu",
    "53": "Rize", "54": "Sakarya", "55": "Samsun", "56": "Siirt", "57": "Sinop",
    "58": "Sivas", "59": "Tekirdag", "60": "Tokat", "61": "Trabzon",
    "62": "Tunceli", "63": "Sanliurfa", "64": "Usak", "65": "Van",
    "66": "Yozgat", "67": "Zonguldak", "68": "Aksaray", "69": "Bayburt",
    "70": "Karaman", "71": "Kirikkale", "72": "Batman", "73": "Sirnak",
    "74": "Bartin", "75": "Ardahan", "76": "Igdir", "77": "Yalova",
    "78": "Karabuk", "79": "Kilis", "80": "Osmaniye", "81": "Duzce",
}

# HTML gövdesindeki <a href="...xls">etiket</a> çiftlerini yakalar. content
# JSON içinde geldiği için "/" karakterleri "\/" olarak kaçırılmış olabilir;
# regex bunu tolere eder (\\? ile isteğe bağlı ters taksim).
_LINK_RE = re.compile(r'href=\\?"([^"\\]+\.xls)\\?"[^>]*>([^<]+)<')


class HmbError(RuntimeError):
    """Raised for HMB data-access errors."""


# "İller İtibarıyla Merkezi Yönetim Bütçe İstatistikleri" ailesindeki
# crosstab tabloları -- il bazlı çok dosyalı tablodan farklı bir aile,
# kendi klasör id'lerini gerektiriyor. Üçü de doğrulandı (2026-08-05).
# "gelir" klasöründe (id=4045) İKİ dosya var (Gelirleri Tahs. + Vergi
# Gelirleri Tah&Tahs) -- file_match ile hangisinin isteneceği ayırt edilir.
KNOWN_CROSSTAB_FOLDERS: dict[tuple[str, int], dict[str, Any]] = {
    ("gider", 2026): {
        "id": 4044,
        "name": "Bütçe Gider Tabloları",
        "file_match": "Gider",
        "label": "İller Bazında Merkezi Yönetim Bütçe Harcamaları",
    },
    ("gelir", 2026): {
        "id": 4045,
        "name": "Bütçe Gelir Tabloları",
        "file_match": "Gelirleri Tahs",
        "label": "İller Bazında Merkezi Yönetim Bütçe Gelirlerinin Tahsilatı",
    },
    ("vergi", 2026): {
        "id": 4045,
        "name": "Bütçe Gelir Tabloları",
        "file_match": "Vergi Gelirleri",
        "label": "İller Bazında Genel Bütçe Vergi Gelirlerinin Tahakkuk ve Tahsilatı",
    },
    ("denge", 2026): {
        "id": 4046,
        "name": "Bütçe Dengesi",
        "file_match": "Gelir ve Gider",
        "label": "İller Bazında Merkezi Yönetim Bütçe Gelir ve Giderlerinin Karşılaştırması",
    },
    ("mahalli_gider", 2026): {
        "id": 4098,
        "name": "Bütçe Gider Tabloları",
        "file_match": "Giderleri",
        "label": "İller İtibarıyla Mahalli İdareler Bütçe Giderleri",
    },
    ("mahalli_gelir", 2026): {
        "id": 4099,
        "name": "Bütçe Gelir Tabloları",
        "file_match": "Gelirleri",
        "label": "İller İtibarıyla Mahalli İdareler Bütçe Gelirleri",
    },
    ("mahalli_denge", 2026): {
        "id": 4100,
        "name": "Bütçe Dengesi",
        "file_match": "Dengesi",
        "label": "İller İtibarıyla Mahalli İdareler Bütçe Dengesi",
    },
}


class HmbClient:
    def __init__(self, timeout: float = 60.0) -> None:
        self._timeout = timeout
        self._folder_cache: dict[int, dict[str, str]] = {}

    def list_files(self, folder_id: int, name: str) -> dict[str, str]:
        """Bir klasördeki dosyaları {görünen ad: indirme URL'si} olarak döndürür."""
        if folder_id in self._folder_cache:
            return self._folder_cache[folder_id]
        with httpx.Client(timeout=self._timeout, headers=_HEADERS, follow_redirects=True) as client:
            response = client.get(FILES_API, params={"name": name, "id": folder_id})
        if response.status_code != 200:
            raise HmbError(f"HMB files API returned HTTP {response.status_code}")
        payload = response.json()
        content = html.unescape(payload.get("content", ""))
        files = {label.strip(): url for url, label in _LINK_RE.findall(content)}
        if not files:
            raise HmbError("HMB files API returned no parseable file links.")
        self._folder_cache[folder_id] = files
        return files

    def download(self, url: str) -> bytes:
        with httpx.Client(timeout=self._timeout, headers=_HEADERS, follow_redirects=True) as client:
            response = client.get(url)
        if response.status_code != 200:
            raise HmbError(f"HMB file download returned HTTP {response.status_code}")
        return response.content

    def get_il_data(self, il_kodu: str, yil: int = 2026) -> pd.DataFrame:
        """Bir ilin genel bütçe geliri tablosunu tidy DataFrame olarak döndürür."""
        folder = KNOWN_FOLDERS.get(("genel-butce-gelirleri-iller", yil))
        if not folder:
            known_years = sorted(y for (_, y) in KNOWN_FOLDERS)
            raise HmbError(
                f"{yil} yılı için klasör id'si henüz keşfedilmedi. "
                f"Bilinen yıllar: {known_years}"
            )
        il_kodu = il_kodu.zfill(2)
        if il_kodu not in IL_SLUGS:
            raise ValueError(f"Bilinmeyen il kodu: {il_kodu!r}. 00-81 arası olmalı.")

        files = self.list_files(folder["id"], folder["name"])
        url = next((u for lbl, u in files.items() if lbl.startswith(f"{il_kodu}-")), None)
        if url is None:
            raise HmbError(
                f"'{il_kodu}-{IL_SLUGS[il_kodu]}-{yil}' için indirme linki bulunamadı."
            )
        content = self.download(url)
        return xls_to_tidy_frame(content, il_kodu=il_kodu, yil=yil)

    def get_karsilastirma_data(self, tablo: str = "denge", yil: int = 2026) -> pd.DataFrame:
        """İller Bazında Merkezi Yönetim Bütçe Gelir/Gider crosstab tablosu.

        Args:
            tablo: "gider", "gelir", "vergi" veya "denge".
        """
        folder = KNOWN_CROSSTAB_FOLDERS.get((tablo, yil))
        if not folder:
            known = sorted(f"{t}/{y}" for (t, y) in KNOWN_CROSSTAB_FOLDERS)
            raise HmbError(
                f"'{tablo}' ({yil}) için klasör id'si henüz keşfedilmedi. "
                f"Bilinen tablolar: {known}"
            )
        files = self.list_files(folder["id"], folder["name"])
        match = folder["file_match"]
        candidates = {lbl: u for lbl, u in files.items() if match in lbl}
        if len(candidates) != 1:
            raise HmbError(
                f"'{folder['name']}' klasöründe '{match}' ile eşleşen tam olarak bir "
                f"dosya bulunamadı (bulunan: {list(files)})."
            )
        url = next(iter(candidates.values()))
        content = self.download(url)
        return crosstab_xls_to_tidy_frame(content, yil=yil, kaynak=tablo)


def crosstab_xls_to_tidy_frame(content: bytes, yil: int, kaynak: str) -> pd.DataFrame:
    """İl satır / kategori sütun biçimindeki HMB .xls dosyalarını tidy'ler.

    'İller İtibarıyla Merkezi Yönetim Bütçe İstatistikleri' gibi bazı HMB
    tabloları, il başına ayrı dosya yerine TEK bir dosyada tüm illeri satır,
    kategorileri sütun olarak veriyor (klasik crosstab) -- il bazlı çok
    sayfalı hiyerarşik dosyalardan (xls_to_tidy_frame) farklı bir düzen.

    Her sayfa bir aya karşılık gelir. Bazı dosyalarda başlık tek satır
    (ör. Tahakkuk/Tahsilat), bazılarında iki satır -- bir grup başlığı
    (ör. "EKONOMİK SINIFLANDIRMA" / "FONKSİYONEL SINIFLANDIRMA", sadece
    birkaç hücrede) + altında gerçek kategori adları (tüm sütunlarda dolu).
    Bunu TÜİK istab dosyalarında zaten çözdüğümüz aynı mantıkla
    (tidy.find_header_block/_combine_header_rows) birleştiriyoruz, ayrı bir
    ad-hoc sezgi yazmıyoruz. İl adları başında birkaç boşluk taşıyor
    (HMB'nin kendi biçimlendirmesi), burada temizleniyor.
    """
    workbook = CalamineWorkbook.from_filelike(io.BytesIO(content))
    rows: list[dict[str, Any]] = []
    for sheet_name in workbook.sheet_names:
        data = workbook.get_sheet_by_name(sheet_name).to_python()
        raw = pd.DataFrame(data)
        frame = raw.dropna(axis=0, how="all").dropna(axis=1, how="all")
        if frame.empty:
            continue
        # boş/tamamı-metin değerleri tidy.find_header_block'un beklediği
        # gibi NaN'a çevir (calamine boş hücreleri "" olarak veriyor)
        frame = frame.replace("", None)
        header_start, header_end = find_header_block(frame)
        columns = _dedupe_columns(_combine_header_rows(frame, header_start, header_end))
        body = frame.iloc[header_end + 1 :]
        for _, row in body.iterrows():
            il_raw = row.iloc[0]
            if not isinstance(il_raw, str) or not il_raw.strip():
                continue
            il = il_raw.strip()
            for position, kategori in enumerate(columns):
                if position == 0 or kategori is None or position >= len(row):
                    continue
                deger = row.iloc[position]
                if not isinstance(deger, (int, float)):
                    continue
                rows.append(
                    {
                        "il": il,
                        "yil": yil,
                        "ay": sheet_name,
                        "kaynak": kaynak,
                        "kategori": kategori,
                        "deger_bin_tl": deger,
                    }
                )
    if not rows:
        raise HmbError("Dosyada işlenebilir satır bulunamadı.")
    return pd.DataFrame(rows)


# Türkçe .upper()'a güvenilmiyor: Python "Nisan"/"Haziran" gibi noktalı-i
# içeren kelimeleri ASCII "I" ile büyütüyor ("NISAN"), doğru Türkçe hali
# noktalı "İ" ("NİSAN") -- bu yüzden her ayın olası TÜM büyük/küçük harf
# yazımları burada elle listeleniyor, .upper()'a güvenilmiyor (bugün
# TÜİK'te de aynı tuzağa denk gelinmişti).
_MONTH_SPELLINGS: dict[str, str] = {}
for _canonical, _variants in {
    "Ocak": ["OCAK", "Ocak"],
    "Şubat": ["ŞUBAT", "Şubat"],
    "Mart": ["MART", "Mart"],
    "Nisan": ["NİSAN", "NISAN", "Nisan"],
    "Mayıs": ["MAYIS", "Mayıs"],
    "Haziran": ["HAZİRAN", "HAZIRAN", "Haziran"],
    "Temmuz": ["TEMMUZ", "Temmuz"],
    "Ağustos": ["AĞUSTOS", "Ağustos"],
    "Eylül": ["EYLÜL", "Eylül"],
    "Ekim": ["EKİM", "EKIM", "Ekim"],
    "Kasım": ["KASIM", "Kasım"],
    "Aralık": ["ARALIK", "Aralık"],
}.items():
    for _variant in _variants:
        _MONTH_SPELLINGS[_variant] = _canonical
_MONTHS_TR = list(_MONTH_SPELLINGS)  # eşleştirme (recognized/missing) için


def _birim_carpani(etiket: str) -> float:
    """Başlık hücresindeki birim etiketinden ("(Bin TL)", "(Milyar TL.)")
    değerleri "bin TL"ye normalize eden çarpanı döndürür.

    2004 dosyasında birimin "(Milyar TL.)" olduğu, 2010+ dosyalarında ise
    "(Bin TL)" olduğu doğrulandı (2026-08-05) -- fark edilmeseydi eski
    yılların rakamları sessizce 1 milyon kat küçük görünürdü.
    """
    normalized = etiket.upper()
    if "MİLYAR" in normalized or "MILYAR" in normalized:
        return 1_000_000.0  # 1 milyar TL = 1.000.000 bin TL
    if "BİN" in normalized or "BIN" in normalized:
        return 1.0
    raise HmbError(
        f"Bilinmeyen birim etiketi: {etiket!r}. Yeni bir birim mi eklendi? "
        "Sessizce yanlış ölçekte veri dönmemek için burada durduruluyor."
    )


def _fixed_month_names(raw_names: list[str]) -> list[str]:
    """Sayfa adlarını kanonik forma (Ocak, Şubat, ...) normalize eder;
    bozuk/tanınmayan tam olarak bir sayfa adını eksik ayla değiştirir.

    2004 dosyasında "Mayıs" sayfasının adı hatalı olarak "00 Merkez"
    yazıyordu (HMB'nin kendi dosyasındaki bir hata, 2026-08-05'te
    doğrulandı). 12 ayın 11'i tanınıyor ve tam olarak bir ay eksikse,
    tanınmayan tek sayfaya o eksik ay adı verilir -- konuma göre tahmin
    değil, "hangi ay hâlâ eksik" mantığıyla.

    Ayrıca yıllar arasında yazım tutarsız (2004: "Ocak", 2010+: "OCAK") --
    hepsi burada kanonik başlık-harfli forma (`Ocak`) çevriliyor, tüm
    yıllarda `ay` sütunu tutarlı olsun diye.
    """
    canonical = [_MONTH_SPELLINGS.get(n.strip(), None) for n in raw_names]
    missing = [m for m in _MONTH_SPELLINGS.values() if m not in canonical]
    # yalnızca kanonik değerleri tekilleştirilmiş olarak say (aynı ay için
    # birden fazla yazım varyantı listede tekrar etmesin)
    missing = list(dict.fromkeys(missing))
    unrecognized_positions = [i for i, c in enumerate(canonical) if c is None]

    if len(missing) == 1 and len(unrecognized_positions) == 1:
        canonical[unrecognized_positions[0]] = missing[0]

    return [c if c is not None else raw_names[i] for i, c in enumerate(canonical)]


def xls_to_tidy_frame(content: bytes, il_kodu: str, yil: int) -> pd.DataFrame:
    """HMB'nin il bazlı bütçe geliri .xls dosyasını tidy uzun formata çevirir.

    Her sayfa bir aya karşılık gelir; hiyerarşik (girintili) bir gelir
    kalemi listesi + Tahakkuk/Tahsilat sütunları içerir. Kalem etiketi
    baştaki boşluklarıyla birlikte korunur (hiyerarşi seviyesi henüz ayrı
    bir sütuna ayrıştırılmadı, yeterince dosyada doğrulanmadan yapılmadı).

    Sütun konumu dosyaya göre kayıyor -- ör. "00-Merkez" dosyasında kategori
    0. sütunda, il dosyalarında (ör. Adana) 1. sütunda -- bu yüzden sabit
    indeks yerine her sayfanın kendi başlık satırından "Tahakkuk"/"Tahsilat"
    sütunlarının konumu okunuyor.

    İki gerçek HMB kaynak-dosyası tutarsızlığı burada düzeltiliyor
    (2004 dosyasıyla doğrulandı, 2026-08-05):
      1. Birim yıla göre değişiyor -- 2010+ dosyalarında "(Bin TL)", 2004
         dosyasında "(Milyar TL.)". Tüm çıktı "bin_tl" birimine
         normalize ediliyor (Milyar -> ×1.000.000); aksi halde eski
         yılların rakamları sessizce 1 milyon kat küçük görünürdü.
      2. 2004 dosyasında bir sayfa adı bozuk ("Mayıs" yerine "00 Merkez"
         yazıyor -- HMB'nin kendi dosyasındaki bir hata). 12 ayın tümü
         beklenirken tam olarak biri eksikse ve tanınmayan tam olarak bir
         sayfa adı varsa, eksik ay o sayfaya atanıyor (konumdan değil,
         "hangi ay eksik" mantığından -- tahmin değil).
    """
    workbook = CalamineWorkbook.from_filelike(io.BytesIO(content))
    sheet_names = _fixed_month_names(list(workbook.sheet_names))

    rows: list[dict[str, Any]] = []
    for raw_name, ay in zip(workbook.sheet_names, sheet_names):
        data = workbook.get_sheet_by_name(raw_name).to_python()

        header_row = None
        for row in data:
            texts = [str(c).strip() for c in row if isinstance(c, str)]
            if "Tahakkuk" in texts and "Tahsilat" in texts:
                header_row = row
                break
        if header_row is None:
            continue  # bu sayfada beklenen başlık bulunamadı, atla

        tahakkuk_pos = next(i for i, c in enumerate(header_row) if c == "Tahakkuk")
        tahsilat_pos = next(i for i, c in enumerate(header_row) if c == "Tahsilat")
        # Kategori adı her zaman Tahakkuk sütununun hemen bir öncesinde --
        # bazı dosyalarda (ör. il dosyaları) fazladan boş bir ilk sütun var
        # ("00-Merkez" dosyasında yok), bu yüzden sabit 0 yerine buradan
        # türetiliyor. Aynı hücre birim etiketini de taşıyor (ör. "(Bin
        # TL)", "(Milyar TL.)").
        kalem_pos = tahakkuk_pos - 1
        birim_etiketi = str(header_row[kalem_pos]) if kalem_pos < len(header_row) else ""
        carpan = _birim_carpani(birim_etiketi)

        for row in data:
            if len(row) <= max(kalem_pos, tahakkuk_pos, tahsilat_pos):
                continue
            kalem = row[kalem_pos]
            if not kalem or not isinstance(kalem, str) or not kalem.strip():
                continue
            tahakkuk, tahsilat = row[tahakkuk_pos], row[tahsilat_pos]
            if not isinstance(tahakkuk, (int, float)) and not isinstance(tahsilat, (int, float)):
                continue  # başlık / birim / boş satır
            rows.append(
                {
                    "il_kodu": il_kodu,
                    "il": IL_SLUGS.get(il_kodu, il_kodu),
                    "yil": yil,
                    "ay": ay,
                    "kalem": kalem.strip(),
                    "tahakkuk_bin_tl": tahakkuk * carpan if isinstance(tahakkuk, (int, float)) else None,
                    "tahsilat_bin_tl": tahsilat * carpan if isinstance(tahsilat, (int, float)) else None,
                }
            )
    if not rows:
        raise HmbError("Dosyada işlenebilir satır bulunamadı.")
    return pd.DataFrame(rows)
