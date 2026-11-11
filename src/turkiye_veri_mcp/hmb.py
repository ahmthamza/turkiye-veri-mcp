"""HMB (Hazine ve Maliye Bakanlığı) Muhasebat Genel Müdürlüğü istatistikleri.

Nasıl çalışıyor (2026-11-11'de canlı olarak doğrulandı)
--------------------------------------------------------
muhasebat.hmb.gov.tr bir React uygulaması; arkasında ``/portal/v2`` altında
bir WordPress REST API'si var. Sitenin kendi JS'i (static/js/main.*.chunk.js)
şu üç uç noktayı kullanıyor:

* ``GET /portal/v2/pages?slug=<slug>`` -> ``[{"content": {"rendered": html},
  "page_sidebar": html, ...}]``. İstatistik sayfalarının gövdesi bir
  ``.menu.hierarchical-content`` ağacı; her klasör ``<a data-id=.. data-name=..>``
  taşıyor (yıllık klasörler ve altındaki "Bütçe Gider Tabloları" /
  "Bütçe Gelir Tabloları" / "Bütçe Dengesi" klasörleri).
* ``genel-yonetim-mali-istatistikleri`` sayfasının ``page_sidebar`` alanı tüm
  istatistik bölümlerinin (Merkezi Yönetim, Mahalli İdareler, SGK, Genel
  Yönetim, İller İtibarıyla..., Paylar, GFS...) sayfa bağlantılarını listeliyor.
* ``GET /portal/v2/files?name=<data-name>&id=<data-id>`` -> ``{"content": html}``
  klasördeki dosyaların gerçek indirme bağlantıları (ms.hmb.gov.tr, tahmin
  edilemeyen hash sonekli). ``name`` birebir data-name olmalı (baştaki boşluk
  dahil); yanlış/eksik ad ``false`` döndürüyor. Üst klasörler boş içerik döner.

Bu yüzden klasör id'leri çalışma anında keşfediliyor (kenar çubuğu -> bölüm
sayfası -> yıl klasörü -> alt klasör). Sabit id tablosu (FALLBACK_IDS) yalnızca
site erişilemezse kullanılan, 2026-11-11 taramasıyla doğrulanmış bir yedektir.

Dosya düzenleri hakkında doğrulanmış gerçekler
----------------------------------------------
* İl bazlı genel bütçe gelirleri (00-81): her sayfa o yılın Ocak'tan o aya
  KÜMÜLATİF değerleri; başlıkta "(KÜMÜLATİF) EYLÜL 2026" yazıyor. "00-Merkez"
  ulusal toplam DEĞİL, bakanlık merkez muhasebe birimlerinin tahsilatıdır
  (başlık: "MERKEZ İLİ ..."); ulusal toplam 82 dosyanın toplamıdır.
* Birim: 2004-2005 dosyaları "(Milyar TL)" (eski TL), 2006-2008 "(Bin YTL)",
  2009+ "(Bin TL)". 1 milyar eski TL = 1.000 YTL = 1 bin YTL = 1 bin TL
  (2005 redenominasyonu 1.000.000:1). Yani sayısal çarpan 1'dir. Önceki sürüm
  Milyar TL'yi 1.000.000 ile çarpıyordu -- 2004-2005 değerleri 10^6 kat büyük
  çıkıyordu (hata). Bağımsız sağlama: 2004 Merkezi Yönetim il tablosunda
  Toplam gelir 110.720.859 "(Milyar TL)" ve gider 141.020.860 "(Bin YTL)" --
  2004 konsolide bütçe gerçekleşmeleriyle (~110,7 / ~141,0 milyar YTL) uyumlu.
* İller itibarıyla Merkezi Yönetim / Konsolide (1990-2003) tabloları: tek
  dosya, il satır x kategori sütun; 2004+ her sayfa bir ay (kümülatif),
  1990-2002 tek sayfa "OCAK-ARALIK" (yıllık). Mahalli İdareler il tabloları:
  sayfalar "I.-IV. DÖNEM" (Ocak-Mart ... Ocak-Aralık kümülatif), 2006-2008
  tek yıllık sayfa. Bazı dosyalarda birim etiketi hiç yok (2019 mahalli gider,
  2022 MY gelir/vergi, 2026 mahalli gelir/denge): bunlar yalnızca aynı yılın
  etiketli kardeş tablosuyla toplamları birebir tutarsa kabul edilir
  (UNIT_SIBLINGS, HmbClient._verify_unit).
* Paylar ve ulusal "Ay İçi" tabloları: kalem satır x ay sütun, değerler AYLIK
  (ay içi) -- "Toplam" sütunu ayların toplamına eşit (her dosyada kontrol).
"""

from __future__ import annotations

import html as _html
import io
import re
import threading
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

import httpx
import pandas as pd
from python_calamine import CalamineWorkbook

PORTAL = "https://muhasebat.hmb.gov.tr/portal/v2"
FILES_API = f"{PORTAL}/files"
PAGES_API = f"{PORTAL}/pages"
ROOT_STATS_SLUG = "genel-yonetim-mali-istatistikleri"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": "https://muhasebat.hmb.gov.tr/",
}

_PAGE_TTL = 6 * 3600
_FILES_TTL = 3600
_MIN_INTERVAL = 0.3  # hmb.gov.tr'ye ardışık istekler arası en az bekleme (nezaket)


class HmbError(RuntimeError):
    """HMB veri erişim hatası."""


class HmbLayoutError(HmbError):
    """Dosya düzeni tanınmadı / doğrulanamadı -- ham indirme önerilir."""


# ---------------------------------------------------------------------------
# Metin yardımcıları
# ---------------------------------------------------------------------------

_FOLD_MAP = str.maketrans(
    {
        "İ": "i", "I": "i", "ı": "i", "Ş": "s", "ş": "s", "Ğ": "g", "ğ": "g",
        "Ü": "u", "ü": "u", "Ö": "o", "ö": "o", "Ç": "c", "ç": "c",
        "Â": "a", "â": "a", "Î": "i", "î": "i", "Û": "u", "û": "u",
    }
)


def fold(text: Any) -> str:
    """Türkçe karakterleri ASCII'ye katlayıp küçültür, boşlukları sadeleştirir.

    ``.lower()`` öncesinde çevrilir; aksi halde "İ".lower() birleşik nokta
    (U+0307) bırakır ve "I".lower() noktalı "i" olur (Türkçe ı/İ tuzağı).
    """
    s = str(text).translate(_FOLD_MAP).lower().replace("̇", "")
    return re.sub(r"\s+", " ", s).strip()


def _clean(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text)).strip()


AYLAR = [
    "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
    "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık",
]
_MONTH_FULL = {fold(m): i + 1 for i, m in enumerate(AYLAR)}
_MONTH_ABBR = {k[:3]: v for k, v in _MONTH_FULL.items()}


def month_number(cell: Any) -> int | None:
    """Hücre TAMAMEN bir ay adıysa (tam veya 3 harf kısaltma) ay numarası.

    "Ocak", "OCAK", "Oca(*)", "Şub", "MAYIS " hepsi tanınır; "Ocak 2026" gibi
    başka metin içeren hücreler tanınmaz (yanlış eşleşmeyi önlemek için).
    """
    if not isinstance(cell, str):
        return None
    t = re.sub(r"[()*.:]", " ", fold(cell)).strip()
    if t in _MONTH_FULL:
        return _MONTH_FULL[t]
    if t in _MONTH_ABBR:
        return _MONTH_ABBR[t]
    return None


def _month_token(tok: str) -> int | None:
    if tok in _MONTH_FULL:
        return _MONTH_FULL[tok]
    # "ARALIKI" gibi yazım hataları: tam ay adı + en çok 1 harf ek
    for name, n in _MONTH_FULL.items():
        if tok.startswith(name) and len(tok) - len(name) == 1:
            return n
    return None


def months_in_text(text: str) -> list[int]:
    """Bir başlık metnindeki ay adları (sırayla)."""
    return [m for m in (_month_token(t) for t in re.findall(r"[a-z]+", fold(text))) if m]


def is_period_cell(cell: Any) -> bool:
    """Ay adı, "II. DÖNEM" ya da "Ocak-Mart"/"Ocak Haziran" gibi ay aralığı."""
    if not isinstance(cell, str):
        return False
    if month_number(cell) or donem_end_month(cell):
        return True
    m = re.fullmatch(r"([a-z]+)\s*[- ]\s*([a-z]+)", fold(cell))
    return bool(m) and m.group(1) in _MONTH_FULL and m.group(2) in _MONTH_FULL


_DONEM_RE = re.compile(r"^\s*(iv|i{1,3})\s*\.?\s*(?:donem|quarter|ceyrek)")
_DONEM_END = {"i": 3, "ii": 6, "iii": 9, "iv": 12}


def donem_end_month(text: Any) -> int | None:
    """"II. DÖNEM" / "IV.DÖNEM" -> 6 / 12 (çeyreklik kümülatif dönem sonu ayı)."""
    if not isinstance(text, str):
        return None
    m = _DONEM_RE.match(fold(text))
    return _DONEM_END[m.group(1)] if m else None


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value


def _years_in(text: str) -> list[int]:
    return [int(y) for y in re.findall(r"(?<!\d)(19\d\d|20\d\d)(?!\d)", str(text))]


# ---------------------------------------------------------------------------
# İller
# ---------------------------------------------------------------------------

# Plaka kodu -> resmi Türkçe il adı. Kodlar HMB'nin kendi il dosyası adlarıyla
# (ör. "34-İstanbul-2026") 2004-2026'nın 23 yılının tamamında çapraz kontrol
# edildi (2026-11-11). "00" = Merkez (bakanlık merkez muhasebe birimleri).
IL_ADLARI: dict[str, str] = {
    "00": "Merkez", "01": "Adana", "02": "Adıyaman", "03": "Afyonkarahisar",
    "04": "Ağrı", "05": "Amasya", "06": "Ankara", "07": "Antalya", "08": "Artvin",
    "09": "Aydın", "10": "Balıkesir", "11": "Bilecik", "12": "Bingöl",
    "13": "Bitlis", "14": "Bolu", "15": "Burdur", "16": "Bursa",
    "17": "Çanakkale", "18": "Çankırı", "19": "Çorum", "20": "Denizli",
    "21": "Diyarbakır", "22": "Edirne", "23": "Elazığ", "24": "Erzincan",
    "25": "Erzurum", "26": "Eskişehir", "27": "Gaziantep", "28": "Giresun",
    "29": "Gümüşhane", "30": "Hakkari", "31": "Hatay", "32": "Isparta",
    "33": "Mersin", "34": "İstanbul", "35": "İzmir", "36": "Kars",
    "37": "Kastamonu", "38": "Kayseri", "39": "Kırklareli", "40": "Kırşehir",
    "41": "Kocaeli", "42": "Konya", "43": "Kütahya", "44": "Malatya",
    "45": "Manisa", "46": "Kahramanmaraş", "47": "Mardin", "48": "Muğla",
    "49": "Muş", "50": "Nevşehir", "51": "Niğde", "52": "Ordu", "53": "Rize",
    "54": "Sakarya", "55": "Samsun", "56": "Siirt", "57": "Sinop", "58": "Sivas",
    "59": "Tekirdağ", "60": "Tokat", "61": "Trabzon", "62": "Tunceli",
    "63": "Şanlıurfa", "64": "Uşak", "65": "Van", "66": "Yozgat",
    "67": "Zonguldak", "68": "Aksaray", "69": "Bayburt", "70": "Karaman",
    "71": "Kırıkkale", "72": "Batman", "73": "Şırnak", "74": "Bartın",
    "75": "Ardahan", "76": "Iğdır", "77": "Yalova", "78": "Karabük",
    "79": "Kilis", "80": "Osmaniye", "81": "Düzce",
}
# Geriye dönük uyumluluk: önceki sürümün ASCII il adları.
IL_SLUGS = {k: fold(v).title().replace(" ", "") for k, v in IL_ADLARI.items()}

# HMB tablolarında görülen eski/kısaltılmış yazımlar (dosyalardan derlendi).
_IL_ALIASES = {
    "03": ["Afyon", "Afyon Karahisar", "Afyon-Karahisar"],
    "33": ["İçel"],
    "46": ["K.Maraş", "K. Maraş", "Maraş"],
    "63": ["Urfa", "Şanlı Urfa"],
}


def _il_key(label: str) -> str:
    return re.sub(r"[^a-z]", "", fold(label))


_IL_BY_KEY: dict[str, str] = {}
for _kod, _ad in IL_ADLARI.items():
    _IL_BY_KEY[_il_key(_ad)] = _kod
for _kod, _names in _IL_ALIASES.items():
    for _n in _names:
        _IL_BY_KEY[_il_key(_n)] = _kod
_IL_BY_KEY["toplam"] = "TOPLAM"
_IL_BY_KEY["geneltoplam"] = "TOPLAM"


def il_kodu_of(label: Any) -> str | None:
    """Tablo satır etiketinden il kodu ("00".."81", "TOPLAM") ya da None."""
    if not isinstance(label, str) or not label.strip():
        return None
    return _IL_BY_KEY.get(_il_key(label))


# ---------------------------------------------------------------------------
# Birimler
# ---------------------------------------------------------------------------

_UNIT_PAREN = re.compile(r"\(\s*(milyar|milyon|bin)\s*(ytl|tl)\s*\.?\s*\)")
_UNIT_BARE = re.compile(r"^\(?\s*(milyar|milyon|bin)\s*(ytl|tl)\s*\.?\s*\)?$")


@dataclass
class Birim:
    etiket: str  # dosyadaki hali, ör. "(Milyar TL)"
    carpan: float  # "bin TL"ye çevirmek için çarpan
    aciklama: str


def find_unit_label(cells: Iterable[Any]) -> str | None:
    """Hücrelerde birim etiketi ara. Parantezli biçim her hücrede, çıplak
    biçim ("BİN TL") yalnızca kısa hücrelerde kabul edilir (not satırlarındaki
    "1.369.238.833 Bin TL bütçe geliri" gibi metinler birim sayılmasın diye)."""
    for cell in cells:
        if not isinstance(cell, str) or not cell.strip():
            continue
        f = fold(cell)
        m = _UNIT_PAREN.search(f)
        if m:
            return _clean(cell)
        if len(f) <= 20 and _UNIT_BARE.match(f):
            return _clean(cell)
    return None


def birim_carpani(etiket: str, veri_yili: int | None) -> Birim:
    """Birim etiketini "bin TL"ye (2005 sonrası TL) çeviren çarpan.

    Doğrulanmış eşlemeler: "(Milyar TL)" (eski TL, <=2008 verisinde) = 1 bin
    YTL; "(Bin YTL)" = 1 bin TL; "(Bin TL)" (>=2005 verisinde) = 1 bin TL.
    Başka her şey (Milyon TL, 2009+ Milyar TL, 2005 öncesi Bin TL) sessizce
    yanlış ölçek vermemek için hata.
    """
    f = fold(etiket)
    m = _UNIT_PAREN.search(f) or _UNIT_BARE.match(f)
    if not m:
        raise HmbLayoutError(f"Birim etiketi çözümlenemedi: {etiket!r}")
    olcek, para = m.group(1), m.group(2)
    if olcek == "milyar" and para == "tl":
        if veri_yili is not None and veri_yili > 2008:
            raise HmbLayoutError(
                f"{veri_yili} verisinde '(Milyar TL)' birimi beklenmiyor -- yeni TL "
                "milyarı mı eski TL milyarı mı belirsiz; ham dosyayı inceleyin."
            )
        return Birim(etiket, 1.0, "Milyar eski TL = bin YTL = bin TL (1.000.000:1 redenominasyon)")
    if olcek == "bin" and para == "ytl":
        return Birim(etiket, 1.0, "Bin YTL = bin TL")
    if olcek == "bin" and para == "tl":
        if veri_yili is not None and veri_yili < 2005:
            raise HmbLayoutError(
                f"{veri_yili} verisinde '(Bin TL)' eski TL demektir (1 bin eski TL = "
                "0,001 TL); bu birim için doğrulanmış dönüşüm yok, ham dosyayı inceleyin."
            )
        return Birim(etiket, 1.0, "Bin TL")
    raise HmbLayoutError(
        f"Bilinmeyen birim: {etiket!r}. Yanlış ölçekte veri dönmemek için durduruldu."
    )


def _is_percent_header(text: str) -> bool:
    return "%" in str(text)


# ---------------------------------------------------------------------------
# HTTP + önbellek
# ---------------------------------------------------------------------------

_cache: dict[tuple, tuple[float, Any]] = {}
_cache_lock = threading.Lock()
_rate_lock = threading.Lock()
_last_request = [0.0]


def _cache_get(key: tuple, ttl: float) -> Any:
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    return None


def _cache_put(key: tuple, value: Any) -> None:
    with _cache_lock:
        _cache[key] = (time.time(), value)


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _check_hmb_url(url: str) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("https", "http") or not (
        host == "hmb.gov.tr" or host.endswith(".hmb.gov.tr")
    ):
        raise HmbError(f"Yalnızca *.hmb.gov.tr adreslerinden indirme yapılır: {url!r}")


# ---------------------------------------------------------------------------
# Sayfa ağacı ayrıştırma
# ---------------------------------------------------------------------------


@dataclass
class Folder:
    label: str
    id: int | None = None
    name: str | None = None  # files API'ye birebir gönderilecek data-name
    href: str | None = None
    children: list["Folder"] = field(default_factory=list)

    def walk(self, path: tuple[str, ...] = ()) -> Iterable[tuple[tuple[str, ...], "Folder"]]:
        here = path + ((self.label,) if self.label else ())
        yield here, self
        for child in self.children:
            yield from child.walk(here)


class _TreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Folder(label="")
        self._stack = [self.root]
        self._a: dict[str, str] | None = None
        self._text = ""

    def handle_starttag(self, tag, attrs):
        if tag == "li":
            node = Folder(label="")
            self._stack[-1].children.append(node)
            self._stack.append(node)
        elif tag == "a":
            self._a = {k: (v or "") for k, v in attrs}
            self._text = ""

    def handle_endtag(self, tag):
        if tag == "li" and len(self._stack) > 1:
            self._stack.pop()
        elif tag == "a" and self._a is not None:
            node = self._stack[-1]
            label = _clean(self._text)
            a = self._a
            self._a = None
            if node is self.root:
                return
            if not node.label:
                node.label = label
            if a.get("data-id", "").strip().isdigit():
                node.id = int(a["data-id"])
                node.name = a.get("data-name", "")
            elif a.get("href") and a["href"] != "#":
                node.href = a["href"]

    def handle_data(self, data):
        if self._a is not None:
            self._text += data


def parse_tree(page_html: str) -> Folder:
    parser = _TreeParser()
    parser.feed(page_html)
    _prune(parser.root)
    return parser.root


def _prune(node: Folder) -> None:
    node.children = [c for c in node.children if c.label or c.children]
    for c in node.children:
        _prune(c)


_YEAR_LABEL = re.compile(r"^\s*((?:19|20)\d{2})(?!\s*-\s*\d)\b")


def year_of_label(label: str) -> int | None:
    """"2026 Yılı ..." -> 2026; aralık etiketleri ("2006 - 2025 ...") -> None."""
    m = _YEAR_LABEL.match(label)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Tablo aileleri
# ---------------------------------------------------------------------------

_SUB_KEYS = {"butce gider tablolari": "gider", "butce gelir tablolari": "gelir", "butce dengesi": "denge"}
_SUB_NAMES = {"gider": "Bütçe Gider Tabloları", "gelir": "Bütçe Gelir Tabloları", "denge": "Bütçe Dengesi"}

FAMILIES: dict[str, dict[str, Any]] = {
    "genel_butce_il": {
        "baslik": "Genel Bütçe Gelirlerinin İller İtibarıyla Tahakkuk ve Tahsilatı",
        "sidebar_match": "genel butce gelirlerinin iller",
        "slug": "genel-butce-gelirlerinin-iller-itibariyle-tahakkuk-ve-tahsilati-2004-2026",
    },
    "merkezi_il": {
        "baslik": "İller İtibarıyla Merkezi Yönetim Bütçe İstatistikleri",
        "sidebar_match": "iller itibariyla merkezi yonetim",
        "slug": "iller-itibariyle-merkezi-yonetim-butce-istatistikleri-2004-2026",
    },
    "mahalli_il": {
        "baslik": "İller İtibarıyla Mahalli İdareler Bütçe İstatistikleri",
        "sidebar_match": "iller itibariyla mahalli idareler",
        "slug": "iller-itibariyle-mahalli-yonetim-butce-istatistikleri-2006-2026",
    },
    "konsolide_il": {
        "baslik": "İller İtibarıyla Konsolide Bütçe İstatistikleri (1990-2003)",
        "sidebar_match": "iller itibariyla konsolide",
        "slug": "iller-itibariyle-konsolide-butce-istatistikleri-1990-2003",
    },
    "paylar": {
        "baslik": "Genel Bütçe Vergi Gelirlerinden Mahalli İdare ve Fonlara Aktarılan Paylar",
        "sidebar_match": "mahalli idare ve fonlara aktarilan paylar",
        "slug": "genel-butce-vergi-gelirlerinden-mahalli-idare-ve-fonlara-aktarilan-paylar",
    },
}

# Canlı keşif başarısız olursa kullanılan, 2026-11-11 taramasında her biri
# files API'si ile listelenip boş olmadığı doğrulanmış klasör id'leri.
FALLBACK_IDS: dict[str, dict[str, dict[int, int]]] = {
    "genel_butce_il": {
        "gelir": {2026: 4042, 2025: 3946, 2024: 3860, 2023: 3786, 2022: 3342, 2021: 3193, 2020: 3061, 2019: 1409, 2018: 1410, 2017: 1411, 2016: 1412, 2015: 1413, 2014: 1414, 2013: 1415, 2012: 1416, 2011: 1417, 2010: 1418, 2009: 1419, 2008: 1420, 2007: 1421, 2006: 1422, 2005: 1423, 2004: 1424},
    },
    "merkezi_il": {
        "gider": {2026: 4044, 2025: 3924, 2024: 3862, 2023: 3788, 2022: 3344, 2021: 3195, 2020: 3063, 2019: 935, 2018: 938, 2017: 941, 2016: 944, 2015: 947, 2014: 950, 2013: 953, 2012: 956, 2011: 959, 2010: 962, 2009: 965, 2008: 968, 2007: 971, 2006: 974, 2005: 977, 2004: 980},
        "gelir": {2026: 4045, 2025: 3925, 2024: 3863, 2023: 3789, 2022: 3345, 2021: 3196, 2020: 3064, 2019: 936, 2018: 939, 2017: 942, 2016: 945, 2015: 948, 2014: 951, 2013: 954, 2012: 957, 2011: 960, 2010: 963, 2009: 966, 2008: 969, 2007: 972, 2006: 975, 2005: 978, 2004: 981},
        "denge": {2026: 4046, 2025: 3926, 2024: 3864, 2023: 3790, 2022: 3346, 2021: 3197, 2020: 3065, 2019: 937, 2018: 940, 2017: 943, 2016: 946, 2015: 949, 2014: 952, 2013: 955, 2012: 958, 2011: 961, 2010: 964, 2009: 967, 2008: 970, 2007: 973, 2006: 976, 2005: 979, 2004: 982},
    },
    "mahalli_il": {
        "gider": {2026: 4098, 2025: 4003, 2024: 3908, 2023: 3838, 2022: 3454, 2021: 3280, 2020: 3171, 2019: 2872, 2018: 880, 2017: 883, 2016: 886, 2015: 889, 2014: 892, 2013: 895, 2012: 898, 2011: 901, 2010: 904, 2009: 907, 2008: 910, 2007: 913, 2006: 916},
        "gelir": {2026: 4099, 2025: 4004, 2024: 3909, 2023: 3839, 2022: 3455, 2021: 3281, 2020: 3172, 2019: 2871, 2018: 881, 2017: 884, 2016: 887, 2015: 890, 2014: 893, 2013: 896, 2012: 899, 2011: 902, 2010: 905, 2009: 908, 2008: 911, 2007: 914, 2006: 917},
        "denge": {2026: 4100, 2025: 4005, 2024: 3910, 2023: 3840, 2022: 3456, 2021: 3282, 2020: 3173, 2019: 2870, 2018: 882, 2017: 885, 2016: 888, 2015: 891, 2014: 894, 2013: 897, 2012: 900, 2011: 903, 2010: 906, 2009: 909, 2008: 912, 2007: 915, 2006: 918},
    },
    "konsolide_il": {
        "gider": {2003: 997, 2002: 1000, 2001: 1003, 2000: 1006, 1999: 1009, 1998: 1012, 1997: 1015, 1996: 1018, 1995: 1021, 1994: 1024, 1993: 1027, 1992: 1030, 1991: 1033, 1990: 1036},
        "gelir": {2003: 998, 2002: 1001, 2001: 1004, 2000: 1007, 1999: 1010, 1998: 1013, 1997: 1016, 1996: 1019, 1995: 1022, 1994: 1025, 1993: 1028, 1992: 1031, 1991: 1034, 1990: 1037},
        "denge": {2003: 999, 2002: 1002, 2001: 1005, 2000: 1008, 1999: 1011, 1998: 1014, 1997: 1017, 1996: 1020, 1995: 1023, 1994: 1026, 1993: 1029, 1992: 1032, 1991: 1035, 1990: 1038},
    },
    "paylar": {
        "": {2026: 4024, 2025: 3928, 2024: 3842, 2023: 3791, 2022: 3324, 2021: 3198, 2020: 3043, 2019: 419, 2018: 420, 2017: 421, 2016: 422, 2015: 423, 2014: 424, 2013: 425, 2012: 426, 2011: 427, 2010: 428, 2009: 429, 2008: 430, 2007: 431, 2006: 432, 2005: 433, 2004: 434, 2003: 435},
    },
}
# data-name'i standarttan farklı (baştaki boşluk) olan yedek klasörler.
_FALLBACK_ODD_NAMES = {1015: " Bütçe Gider Tabloları", 1021: " Bütçe Gider Tabloları", 1027: " Bütçe Gider Tabloları"}
_PAYLAR_TARIHSEL_FALLBACK = (436, "1990-2005 Mahalli İdare ve Fon Payları")


def _fallback_name(family: str, sub: str, yil: int, folder_id: int) -> str:
    if folder_id in _FALLBACK_ODD_NAMES:
        return _FALLBACK_ODD_NAMES[folder_id]
    if family == "paylar":
        return f"{yil} Mahalli İdare ve Fon Payları"
    return _SUB_NAMES[sub]


# Karşılaştırma (il x kategori) tabloları: tablo adı -> (aile, alt klasör, dosya seçici)
def _sel_gider(f: str) -> bool:
    return "gider" in f and "gelir" not in f


def _sel_gelir(f: str) -> bool:
    return "gelir" in f and "vergi" not in f and "gider" not in f


def _sel_vergi(f: str) -> bool:
    return "vergi" in f


def _sel_denge(f: str) -> bool:
    return ("gelir ve gider" in f) or ("denge" in f)


KARSILASTIRMA_TABLOLARI: dict[str, dict[str, Any]] = {
    "gider": {"alt": "gider", "sec": _sel_gider, "etiket": "İller bazında bütçe giderleri (ekonomik + fonksiyonel)"},
    "gelir": {"alt": "gelir", "sec": _sel_gelir, "etiket": "İller bazında bütçe gelirlerinin tahsilatı"},
    "vergi": {"alt": "gelir", "sec": _sel_vergi, "etiket": "İller bazında genel bütçe vergi gelirleri tahakkuk/tahsilat"},
    "denge": {"alt": "denge", "sec": _sel_denge, "etiket": "İller bazında bütçe gelir-gider karşılaştırması"},
    "mahalli_gider": {"alt": "gider", "sec": _sel_gider, "etiket": "İller itibarıyla mahalli idareler bütçe giderleri"},
    "mahalli_gelir": {"alt": "gelir", "sec": _sel_gelir, "etiket": "İller itibarıyla mahalli idareler bütçe gelirleri"},
    "mahalli_denge": {"alt": "denge", "sec": lambda f: "denge" in f, "etiket": "İller itibarıyla mahalli idareler bütçe dengesi"},
}

PAYLAR_TABLOLARI: dict[str, Callable[[str], bool]] = {
    "mahalli": lambda f: re.search(r"mahalli idare(ler)? pay", f) is not None,
    "fon_detay1": lambda f: "fon pay" in f and re.search(r"detay\s*1", f) is not None,
    "fon_detay2": lambda f: "fon pay" in f and re.search(r"detay\s*2", f) is not None,
}


@dataclass
class HmbFile:
    etiket: str
    url: str

    @property
    def uzanti(self) -> str:
        path = urlparse(self.url).path
        return path.rsplit(".", 1)[-1].lower() if "." in path else ""


_LINK_RE = re.compile(r"<a\s[^>]*?href=\"([^\"]+)\"[^>]*>(.*?)</a>", re.S | re.I)


# ---------------------------------------------------------------------------
# İstemci
# ---------------------------------------------------------------------------


class HmbClient:
    def __init__(self, timeout: float = 90.0) -> None:
        self._timeout = timeout

    # -- HTTP ---------------------------------------------------------------
    def _get(self, url: str, params: dict | None = None) -> httpx.Response:
        _check_hmb_url(url)
        last_exc: Exception | None = None
        for attempt in range(3):
            with _rate_lock:
                wait = _MIN_INTERVAL - (time.time() - _last_request[0])
                if wait > 0:
                    time.sleep(wait)
                _last_request[0] = time.time()
            try:
                with httpx.Client(timeout=self._timeout, headers=_HEADERS, follow_redirects=True) as client:
                    response = client.get(url, params=params)
                if response.status_code >= 500 and attempt < 2:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                return response
            except httpx.HTTPError as exc:  # ağ hatası -> tekrar dene
                last_exc = exc
                time.sleep(2.0 * (attempt + 1))
        raise HmbError(f"HMB'ye ulaşılamadı ({url}): {last_exc}")

    # -- Keşif --------------------------------------------------------------
    def get_page(self, slug: str) -> dict[str, Any]:
        key = ("page", slug)
        hit = _cache_get(key, _PAGE_TTL)
        if hit is not None:
            return hit
        response = self._get(PAGES_API, params={"slug": slug})
        if response.status_code != 200:
            raise HmbError(f"HMB pages API HTTP {response.status_code} ({slug})")
        data = response.json()
        if not isinstance(data, list) or not data:
            raise HmbError(f"HMB'de '{slug}' sayfası bulunamadı.")
        page = data[0]
        result = {
            "slug": slug,
            "title": _clean(_html.unescape(re.sub("<[^>]+>", "", page.get("title", {}).get("rendered", "")))),
            "modified": page.get("modified"),
            "content": page.get("content", {}).get("rendered", "") or "",
            "sidebar": page.get("page_sidebar") or "",
        }
        _cache_put(key, result)
        return result

    def sections(self) -> list[dict[str, Any]]:
        """İstatistik bölümleri: kök istatistik sayfasının kenar çubuğundaki tüm
        bağlantılar + ana menünün "İstatistik" ve "Raporlar" altındaki sayfalar
        (bültenler, mutabakat/analiz raporları, döner sermaye)."""
        root = self.get_page(ROOT_STATS_SLUG)
        tree = parse_tree(root["sidebar"])
        out = []
        seen = set()
        for path, node in tree.walk():
            if node.href and node.href.startswith("/"):
                slug = node.href.strip("/")
                seen.add(slug)
                out.append({"baslik": node.label, "yol": " > ".join(path[:-1]), "slug": slug})
        try:
            out.extend(r for r in self._main_menu_sections() if r["slug"] not in seen)
        except (HmbError, ValueError, KeyError, TypeError):
            pass
        return out

    def _main_menu_sections(self) -> list[dict[str, Any]]:
        key = ("menu", "main")
        items = _cache_get(key, _PAGE_TTL)
        if items is None:
            response = self._get(f"{PORTAL}/menus", params={"slug": "main"})
            if response.status_code != 200:
                raise HmbError(f"HMB menus API HTTP {response.status_code}")
            items = response.json()
            _cache_put(key, items)
        by_id = {int(i.get("db_id") or i["id"]): i for i in items}

        def chain(item):
            names = []
            while item:
                names.append(_clean(_html.unescape(re.sub("<[^>]+>", "", item.get("title", "")))))
                item = by_id.get(int(item.get("menu_item_parent") or 0))
            return list(reversed(names))

        out = []
        for item in items:
            url = item.get("url", "")
            names = chain(item)
            if url.startswith("/") and len(url) > 1 and names and names[0] in ("İstatistik", "Raporlar ve Kılavuzlar"):
                out.append({"baslik": names[-1], "yol": "Ana menü > " + " > ".join(names[:-1]), "slug": url.strip("/")})
        return out

    def section_tree(self, slug: str) -> Folder:
        return parse_tree(self.get_page(slug)["content"])

    def family_slug(self, family: str) -> str:
        spec = FAMILIES[family]
        try:
            for sec in self.sections():
                if spec["sidebar_match"] in fold(sec["baslik"]):
                    return sec["slug"]
        except HmbError:
            pass
        return spec["slug"]

    def year_folders(self, family: str) -> tuple[dict[int, dict[str, tuple[int, str]]], str]:
        """{yıl: {alt: (id, data-name)}} ve kaynak ("canlı"/"yedek")."""
        try:
            tree = self.section_tree(self.family_slug(family))
            years: dict[int, dict[str, tuple[int, str]]] = {}
            for node in tree.children:
                yil = year_of_label(node.label)
                if yil is None or node.id is None:
                    continue
                subs = {}
                for child in node.children:
                    key = _SUB_KEYS.get(fold(child.name or child.label))
                    if key and child.id is not None:
                        subs[key] = (child.id, child.name or "")
                if not node.children:
                    subs[""] = (node.id, node.name or "")
                if subs:
                    years[yil] = subs
            if years:
                return years, "canlı"
        except HmbError:
            pass
        fb: dict[int, dict[str, tuple[int, str]]] = {}
        for sub, by_year in FALLBACK_IDS[family].items():
            for yil, fid in by_year.items():
                fb.setdefault(yil, {})[sub] = (fid, _fallback_name(family, sub, yil, fid))
        return fb, "yedek (2026-11-11 doğrulanmış id tablosu)"

    def list_files(self, folder_id: int, name: str) -> list[HmbFile]:
        key = ("files", int(folder_id), name)
        hit = _cache_get(key, _FILES_TTL)
        if hit is not None:
            return hit
        response = self._get(FILES_API, params={"name": name, "id": int(folder_id)})
        if response.status_code != 200:
            raise HmbError(f"HMB files API HTTP {response.status_code} (id={folder_id})")
        payload = response.json()
        if payload is False:
            raise HmbError(
                f"HMB files API klasör id={folder_id} için adı ({name!r}) kabul etmedi. "
                "Ad, sayfadaki data-name ile birebir (baştaki boşluk dahil) aynı olmalı; "
                "hmb_list_tables ile doğru adı alın."
            )
        content = (payload or {}).get("content", "") or ""
        files = [
            HmbFile(_clean(_html.unescape(re.sub("<[^>]+>", "", label))), _html.unescape(url))
            for url, label in _LINK_RE.findall(content)
        ]
        _cache_put(key, files)
        return files

    def download(self, url: str) -> bytes:
        response = self._get(url)
        if response.status_code != 200:
            raise HmbError(f"HMB dosya indirme HTTP {response.status_code}: {url}")
        return response.content

    def _resolve(self, family: str, sub: str, yil: int | None) -> tuple[int, int, str, list[HmbFile], str]:
        """(yıl, klasör id, ad, dosyalar, kaynak). yil None -> dosyası olan en son yıl."""
        years, kaynak = self.year_folders(family)
        candidates = sorted((y for y in years if sub in years[y]), reverse=True)
        if not candidates:
            raise HmbError(f"{FAMILIES[family]['baslik']}: hiç yıl klasörü bulunamadı.")
        if yil is not None and yil not in candidates:
            raise HmbError(
                f"{FAMILIES[family]['baslik']}: {yil} yılı yayımlanmamış. "
                f"Mevcut yıllar: {min(candidates)}-{max(candidates)}."
            )
        for y in [yil] if yil is not None else candidates:
            fid, name = years[y][sub]
            files = self.list_files(fid, name)
            if files or yil is not None:
                return y, fid, name, files, kaynak
        raise HmbError(f"{FAMILIES[family]['baslik']}: dosya içeren yıl klasörü bulunamadı.")

    def available_years(self, family: str) -> list[int]:
        years, _ = self.year_folders(family)
        return sorted(years)

    # -- Bespoke tablolar -----------------------------------------------------
    def get_il_data(self, il_kodu: str, yil: int | None = None) -> pd.DataFrame:
        """Genel bütçe gelirlerinin il bazında tahakkuk/tahsilatı (kümülatif)."""
        il_kodu = str(il_kodu).strip().zfill(2)
        if il_kodu not in IL_ADLARI:
            raise ValueError(f"Bilinmeyen il kodu: {il_kodu!r}. 00 (Merkez) veya 01-81 olmalı.")
        yil = yil or None
        y, fid, name, files, kaynak = self._resolve("genel_butce_il", "gelir", yil)
        match = [f for f in files if re.match(rf"^\s*{il_kodu}\s*[-_]", f.etiket)]
        if len(match) != 1:
            raise HmbError(
                f"{y} klasöründe '{il_kodu}-' ile başlayan tam olarak bir dosya bekleniyordu, "
                f"{len(match)} bulundu."
            )
        content = self.download(match[0].url)
        frame = parse_il_statement(content, il_kodu=il_kodu, yil=y, birim_dogrula=True)
        if frame.attrs["meta"].get("dogrulanacak_donemler"):
            frame = self._verify_il_months(frame, il_kodu, y)
        frame.attrs["meta"] = {
            **frame.attrs.get("meta", {}),
            "kaynak_dosya": match[0].etiket,
            "kaynak_url": match[0].url,
            "klasor_id": fid,
            "klasor_kaynagi": kaynak,
        }
        return frame

    def get_karsilastirma_data(self, tablo: str = "denge", yil: int | None = None, _depth: int = 0) -> pd.DataFrame:
        """İl satır x kategori sütun tabloları (Merkezi Yönetim 2004+, Konsolide
        1990-2003, Mahalli İdareler 2006+)."""
        if tablo not in KARSILASTIRMA_TABLOLARI:
            raise ValueError(f"tablo şunlardan biri olmalı: {sorted(KARSILASTIRMA_TABLOLARI)}")
        spec = KARSILASTIRMA_TABLOLARI[tablo]
        yil = yil or None
        if tablo.startswith("mahalli_"):
            family = "mahalli_il"
        elif yil is not None and yil <= 2003:
            family = "konsolide_il"
        else:
            family = "merkezi_il"
        y, fid, name, files, kaynak = self._resolve(family, spec["alt"], yil)
        cands = [f for f in files if f.uzanti in ("xls", "xlsx") and spec["sec"](fold(f.etiket))]
        if len(cands) != 1:
            raise HmbError(
                f"{y} '{name.strip()}' klasöründe '{tablo}' için tam olarak bir dosya bekleniyordu; "
                f"bulunanlar: {[f.etiket for f in files]}"
            )
        content = self.download(cands[0].url)
        frame = parse_il_crosstab(content, yil=y, kaynak=tablo, birim_dogrula=True)
        if frame.attrs["meta"].pop("birim_dogrulanmali", False):
            if _depth >= 2:
                raise HmbLayoutError(f"{tablo} {y}: birim etiketi yok ve doğrulanamadı.")
            frame.attrs["meta"]["kaynak_birim"] = [self._verify_unit(tablo, y, frame, _depth)]
        frame.attrs["meta"] = {
            **frame.attrs.get("meta", {}),
            "seri": FAMILIES[family]["baslik"],
            "kaynak_dosya": cands[0].etiket,
            "kaynak_url": cands[0].url,
            "klasor_id": fid,
            "klasor_kaynagi": kaynak,
        }
        return frame

    def _verify_il_months(self, frame: pd.DataFrame, il_kodu: str, yil: int) -> pd.DataFrame:
        """Şüpheli aylar (birim etiketi yok / başlıkta başka yıl): ilin "Vergi
        Gelirleri" tahakkuk ve tahsilatı, aynı yılın bağımsız "İller Bazında
        Genel Bütçe Vergi Gelirleri" tablosundaki il satırıyla birebir aynıysa
        ay tutulur (not düşülür), değilse o ayın satırları ÇIKARILIR ve
        meta["reddedilen_donemler"]'e yazılır. Hiç ay kalmazsa hata."""
        meta = dict(frame.attrs["meta"])
        suspects: dict[str, str] = meta.pop("dogrulanacak_donemler")
        try:
            vergi = self.get_karsilastirma_data("vergi", yil)
        except HmbError as exc:
            raise HmbLayoutError(f"Şüpheli aylar ({suspects}) bağımsız tabloyla doğrulanamadı: {exc}") from exc
        vr = vergi[vergi.il_kodu == il_kodu]
        mine = frame[frame.kalem.map(lambda k: re.sub(r"^i\s*-\s*", "", fold(k)) == "vergi gelirleri")]
        kept, dropped = [], {}
        for d, why in sorted(suspects.items()):
            m = mine[mine.donem == d]
            problems = []
            for col, kat in (("tahakkuk_bin_tl", "Tahakkuk"), ("tahsilat_bin_tl", "Tahsilat")):
                t = vr[(vr.donem == d) & (vr.kategori == kat)]
                if m.empty or t.empty:
                    problems.append(f"{kat}: karşılık yok")
                    continue
                a, b = float(m[col].iloc[0]), float(t.deger.iloc[0])
                # ≤%0,1: iki HMB tablosu arasındaki küçük revizyon farkları (ör. 2026
                # Ankara Ağustos %0,02) kabul; yanlış birim/yıl çok daha büyük fark verir.
                if abs(a - b) > max(2.0, 1e-3 * abs(b)):
                    problems.append(f"{kat} {a:,.0f} ≠ vergi tablosu {b:,.0f}")
            if problems:
                dropped[d] = f"{why}; bağımsız vergi tablosuyla tutmuyor ({'; '.join(problems)})"
            else:
                kept.append(f"{d} ({why})")
        frame = frame[~frame.donem.isin(list(dropped))].reset_index(drop=True)
        if frame.empty:
            raise HmbLayoutError(f"Hiçbir ay doğrulanamadı: {dropped}")
        ref_unit = vergi.attrs["meta"]["kaynak_birim"][0]
        if kept:
            meta["kaynak_birim"] = list(meta["kaynak_birim"]) + [
                "Doğrulanan aylar: " + ", ".join(kept)
                + f" — 'Vergi Gelirleri' bağımsız il vergi tablosuyla ({ref_unit}) aynı (fark ≤%0,1)"
            ]
        if dropped:
            meta["reddedilen_donemler"] = dropped
            meta["aylar"] = [AYLAR[int(d[5:]) - 1] for d in sorted(frame.donem.unique())]
            meta["eksik_ay"] = sorted(set(range(1, 13)) - {int(d[5:]) for d in frame.donem.unique()})
        frame.attrs["meta"] = meta
        return frame

    def _verify_unit(self, tablo: str, yil: int, frame: pd.DataFrame, depth: int) -> str:
        """Birim etiketi olmayan bir il tablosunun toplamlarını, aynı yılın
        birim etiketli kardeş tablosuyla karşılaştırır; tüm dönemlerde birebir
        aynıysa birimi kabul eder, değilse HmbLayoutError."""
        def totals(fr: pd.DataFrame, names: list[str]) -> dict[str, float]:
            for name in names:
                sel = fr[(fr.il == "Toplam") & (fr.kategori.map(fold) == fold(name))]
                if not sel.empty:
                    return dict(zip(sel.donem, sel.deger))
            return {}

        tried = []
        for sib, sib_cat, my_cats in UNIT_SIBLINGS.get(tablo, []):
            try:
                other = self.get_karsilastirma_data(sib, yil, _depth=depth + 1)
            except HmbError as exc:
                tried.append(f"{sib}: {exc}")
                continue
            mine, theirs = totals(frame, my_cats), totals(other, [sib_cat])
            common = sorted(set(mine) & set(theirs))
            if not common:
                tried.append(f"{sib}: ortak dönem/kategori yok")
                continue
            bad = [d for d in common if abs(mine[d] - theirs[d]) > max(2.0, 1e-6 * abs(theirs[d]))]
            if bad:
                tried.append(f"{sib}: {len(bad)} dönemde toplam farklı")
                continue
            sib_unit = other.attrs["meta"]["kaynak_birim"][0]
            base = sib_unit.split(" ")[0] + (" " + sib_unit.split(" ")[1] if sib_unit.startswith("(Bin") else "")
            kaynak = "dosya etiketi" if sib_unit.endswith("(dosya)") else "zincirleme doğrulandı"
            return (
                f"{base} — dosyada etiket yok; {len(common)} dönemde Toplam = '{sib}' tablosu "
                f"'{sib_cat}' toplamı (o tablonun birimi: {kaynak}) — doğrulandı"
            )
        raise HmbLayoutError(
            f"{tablo} {yil}: dosyada birim etiketi yok ve kardeş tabloyla doğrulanamadı ({'; '.join(tried)}). "
            "Ham dosyayı hmb_get_file(ham_path=...) ile inceleyin."
        )

    def get_paylar(self, tablo: str = "mahalli", yil: int | None = None) -> pd.DataFrame:
        """Genel bütçe vergi gelirlerinden mahalli idare ve fonlara aktarılan paylar."""
        yil = yil or None
        if tablo == "tarihsel":
            fid, name = _PAYLAR_TARIHSEL_FALLBACK
            kaynak = "yedek (2026-11-11 doğrulanmış id)"
            try:
                tree = self.section_tree(self.family_slug("paylar"))
                for node in tree.children:
                    if re.match(r"^\s*1990\s*-\s*2005", node.label) and node.id:
                        fid, name, kaynak = node.id, node.name or "", "canlı"
            except HmbError:
                pass
            files = [f for f in self.list_files(fid, name) if f.uzanti in ("xls", "xlsx")]
            if len(files) != 1:
                raise HmbError(f"1990-2005 klasöründe tek dosya bekleniyordu: {[f.etiket for f in files]}")
            chosen, y = files[0], None
        else:
            if tablo not in PAYLAR_TABLOLARI:
                raise ValueError(f"tablo şunlardan biri olmalı: {sorted(PAYLAR_TABLOLARI) + ['tarihsel']}")
            y, fid, name, files, kaynak = self._resolve("paylar", "", yil)
            cands = [f for f in files if f.uzanti in ("xls", "xlsx") and PAYLAR_TABLOLARI[tablo](fold(f.etiket))]
            if len(cands) != 1:
                raise HmbError(
                    f"{y} paylar klasöründe '{tablo}' için tam olarak bir dosya bekleniyordu; "
                    f"bulunanlar: {[f.etiket for f in files]}"
                )
            chosen = cands[0]
        content = self.download(chosen.url)
        frame = parse_wide(content, yil=y, dosya_etiketi=chosen.etiket)
        frame.attrs["meta"] = {
            **frame.attrs.get("meta", {}),
            "kaynak_dosya": chosen.etiket,
            "kaynak_url": chosen.url,
            "klasor_id": fid,
            "klasor_kaynagi": kaynak,
        }
        return frame

    def fetch_file(
        self, url: str, dosya_etiketi: str = "", birim_beyani: str = ""
    ) -> tuple[pd.DataFrame | None, bytes, dict[str, Any]]:
        """Herhangi bir HMB dosyasını indir; tanınan düzendeyse tidy tablo döndür.

        Dönüş: (frame veya None, ham bayt, meta). Tanınmayan düzende frame None
        ve meta["neden"] açıklama içerir -- asla tahmini bir tablo üretilmez.
        """
        content = self.download(url)
        ext = urlparse(url).path.rsplit(".", 1)[-1].lower()
        meta: dict[str, Any] = {"kaynak_url": url, "uzanti": ext, "boyut_bayt": len(content)}
        if ext not in ("xls", "xlsx", "xlsm"):
            meta["neden"] = f".{ext} dosyası tablo olarak ayrıştırılmaz; ham dosya olarak indirin."
            return None, content, meta
        if birim_beyani:
            # yalnızca doğrulanmış birim adları kabul edilir
            birim_carpani(f"({birim_beyani})", None)
        frame, reasons = auto_parse(content, dosya_etiketi=dosya_etiketi, birim_beyani=birim_beyani)
        if frame is None:
            meta["neden"] = "Tanınan bir düzen bulunamadı: " + " | ".join(reasons)
        else:
            meta.update(frame.attrs.get("meta", {}))
        return frame, content, meta


# ---------------------------------------------------------------------------
# Ayrıştırıcılar
# ---------------------------------------------------------------------------


def _sheets(content: bytes) -> list[tuple[str, list[list[Any]]]]:
    try:
        workbook = CalamineWorkbook.from_filelike(io.BytesIO(content))
    except Exception as exc:  # bozuk / desteklenmeyen dosya
        raise HmbLayoutError(f"Excel dosyası açılamadı: {exc}") from exc
    out = []
    for name in workbook.sheet_names:
        try:
            out.append((name, workbook.get_sheet_by_name(name).to_python()))
        except Exception as exc:
            raise HmbLayoutError(f"'{name}' sayfası okunamadı: {exc}") from exc
    return out


def _cell(row: list[Any], j: int) -> Any:
    return row[j] if j < len(row) else ""


def _period(yil: int | None, ay: int | None) -> str | None:
    if yil is None:
        return None
    return f"{yil}-{ay:02d}" if ay else str(yil)


# -- C: il bazlı genel bütçe gelir tablosu ------------------------------------


def parse_il_statement(content: bytes, il_kodu: str, yil: int, birim_dogrula: bool = False) -> pd.DataFrame:
    """İl bazlı genel bütçe gelirleri .xls -> tidy uzun tablo.

    Her sayfa = Ocak'tan o aya KÜMÜLATİF tablo. Ay, sayfanın başlığından
    ("... (KÜMÜLATİF) EYLÜL 2026") okunur ve sayfa adıyla çapraz kontrol edilir;
    başlıktaki il adı istenen il koduyla karşılaştırılır (HMB'nin dosya
    etiketlerinde "35-İzmit" gibi yazım hataları var).
    """
    rows_out: list[dict[str, Any]] = []
    entries: list[tuple[str, int | None, int | None]] = []
    birim_etiketleri: set[str] = set()
    supheli: dict[str, str] = {}
    yil_gorulen = False
    for sheet_name, data in _sheets(content):
        header_idx = None
        for i, row in enumerate(data[:30]):
            texts = {fold(c) for c in row if isinstance(c, str)}
            if "tahakkuk" in texts and "tahsilat" in texts:
                header_idx = i
                break
        if header_idx is None:
            continue
        header = data[header_idx]
        tah_pos = next(j for j, c in enumerate(header) if isinstance(c, str) and fold(c) == "tahakkuk")
        tahs_pos = next(j for j, c in enumerate(header) if isinstance(c, str) and fold(c) == "tahsilat")
        kalem_pos = tah_pos - 1
        title = " ".join(str(c) for r in data[:header_idx] for c in r if isinstance(c, str) and len(c) > 25)

        # Ay: başlıktaki tam ay adı ve sayfa adı; karar tüm sayfalar okununca.
        title_months = months_in_text(title.split("(KÜMÜLATİF)")[-1]) or months_in_text(title)
        entries.append((sheet_name, title_months[-1] if title_months else None, month_number(sheet_name)))
        title_years = _years_in(title)
        if title_years and yil not in title_years:
            if not birim_dogrula:
                raise HmbLayoutError(f"'{sheet_name}' başlığında başka yıl var ({title_years}), {yil} bekleniyordu.")
            # 2020 Erzincan Temmuz / 2026 Kars Eylül: başlık önceki yıldan kopya;
            # ay, bağımsız vergi tablosuyla doğrulanmadıkça kullanılmaz.
            supheli[sheet_name] = f"başlıkta başka yıl ({title_years[0]})"
        yil_gorulen = yil_gorulen or yil in title_years
        # İl: başlık "İSTANBUL İLİ GENEL BÜTÇE..." / "MERKEZ GENEL BÜTÇE..."
        il_part = re.split(r"\s(?:ili|genel)\s", " " + fold(title) + " ", maxsplit=1)[0]
        title_code = il_kodu_of(il_part)
        if title_code is not None and title_code != il_kodu:
            raise HmbLayoutError(
                f"Dosya başlığındaki il ({il_part!r}) istenen il koduyla ({il_kodu}) uyuşmuyor."
            )
        unit_label = find_unit_label([_cell(header, kalem_pos)] + [c for r in data[:header_idx] for c in r])
        if unit_label is None:
            if not birim_dogrula:
                raise HmbLayoutError(f"'{sheet_name}' sayfasında birim etiketi yok.")
            # geçici; çağıran bağımsız tabloyla doğrulamazsa veri reddedilir
            unit_label = "(Bin YTL)" if yil <= 2008 else "(Bin TL)"
            supheli[sheet_name] = (supheli.get(sheet_name, "") + "; " if sheet_name in supheli else "") + "birim etiketi yok"
        birim = birim_carpani(unit_label, yil)
        if "birim etiketi yok" not in supheli.get(sheet_name, ""):
            birim_etiketleri.add(unit_label)
        sira = 0
        for row in data[header_idx + 1 :]:
            kalem = _cell(row, kalem_pos)
            if not isinstance(kalem, str) or not kalem.strip():
                continue
            tah, tahs = _cell(row, tah_pos), _cell(row, tahs_pos)
            if not _is_num(tah) and not _is_num(tahs):
                continue
            sira += 1
            rows_out.append(
                {
                    "_sayfa": sheet_name,
                    "il_kodu": il_kodu,
                    "il": IL_ADLARI[il_kodu],
                    "yil": yil,
                    "ay": None,
                    "donem": None,
                    "kalem_sira": sira,
                    "kalem": _clean(kalem),
                    "tahakkuk_bin_tl": tah * birim.carpan if _is_num(tah) else None,
                    "tahsilat_bin_tl": tahs * birim.carpan if _is_num(tahs) else None,
                    "birim": "bin TL",
                    "donem_tipi": "kümülatif (Ocak-ay sonu)",
                }
            )
    if not rows_out:
        raise HmbLayoutError("Dosyada Tahakkuk/Tahsilat başlıklı işlenebilir sayfa bulunamadı.")
    if not yil_gorulen and not (birim_dogrula and supheli):
        raise HmbLayoutError(f"Hiçbir sayfa başlığında {yil} yılı yok.")
    resolved, notes = resolve_sheet_months(entries, single_sheet=False)
    for rec in rows_out:
        ay = resolved[rec.pop("_sayfa")]
        rec["ay"], rec["donem"] = AYLAR[ay - 1], _period(yil, ay)
    aylar = sorted(m for m in resolved.values() if m)
    frame = pd.DataFrame(rows_out).sort_values(["donem", "kalem_sira"], kind="stable").reset_index(drop=True)
    frame.attrs["meta"] = {
        "birim": "bin TL",
        "kaynak_birim": sorted(birim_etiketleri),
        "donem_tipi": "kümülatif: her ay Ocak'tan o ayın sonuna kadarki toplam",
        "aylar": [AYLAR[a - 1] for a in aylar],
        "eksik_ay": sorted(set(range(1, max(aylar) + 1)) - set(aylar)),
    }
    if notes:
        frame.attrs["meta"]["duzeltmeler"] = notes
    if supheli:
        frame.attrs["meta"]["dogrulanacak_donemler"] = {_period(yil, resolved[sh]): why for sh, why in supheli.items()}
    return frame


# -- A: tek sayfa = tek dönem, satır x sütun çapraz tablolar -------------------


def _row_texts(row: list[Any]) -> list[tuple[int, str]]:
    return [(j, c) for j, c in enumerate(row) if isinstance(c, str) and c.strip()]


def _is_legend_row(row: list[Any]) -> bool:
    """Sayfa gezinme satırları: hücrelerin tamamı ay adı / dönem / ay aralığı."""
    cells = [c for _, c in _row_texts(row)]
    if len(cells) < 2:
        return False
    return all(is_period_cell(c) for c in cells)


def _column_headers(header_rows: list[list[Any]], cols: list[int]) -> dict[int, str]:
    """Başlık satırlarını sütun adlarına çevirir. Seyrek satırlar (grup başlığı,
    ör. "EKONOMİK SINIFLANDIRMA") sağa doğru, bir sonraki grup hücresine kadar
    yayılır; "[1]", "[2/1]" gibi kod satırları yalnızca "%" sütunlarında eklenir."""
    parts: dict[int, list[str]] = {j: [] for j in cols}
    rows_present = []
    for row in header_rows:
        present = {
            j: _clean(_cell(row, j))
            for j in cols
            if isinstance(_cell(row, j), str) and _cell(row, j).strip()
            and not is_period_cell(_cell(row, j))
            and not _UNIT_BARE.match(fold(_cell(row, j)))
        }
        if present:
            rows_present.append(present)
    full = max((len(p) for p in rows_present), default=0)
    for present in rows_present:
        sparse = len(present) < full
        current = None
        for j in cols:
            if j in present:
                current = present[j]
                parts[j].append(current)
            elif sparse and current is not None:
                parts[j].append(current)
    out = {}
    for j, ps in parts.items():
        main = [p for p in ps if not re.fullmatch(r"\[[0-9/ ]+\]", p)]
        codes = [p for p in ps if re.fullmatch(r"\[[0-9/ ]+\]", p)]
        name = " — ".join(main)
        if main and main[-1] == "%" and codes:
            name = f"{name} {codes[-1]}"
        out[j] = name
    return out


def _title_month(title: str) -> int | None:
    t_months = months_in_text(title)
    return max(t_months) if t_months else None


def _sheet_month(sheet_name: str) -> int | None:
    return month_number(sheet_name) or donem_end_month(sheet_name)


def resolve_sheet_months(
    entries: list[tuple[str, int | None, int | None]], single_sheet: bool
) -> tuple[dict[str, int | None], list[str]]:
    """Her sayfanın dönem sonu ayını (başlık ayı, sayfa adı ayı) çiftlerinden,
    çalışma kitabı düzeyinde karar vererek belirler.

    HMB dosyalarında iki tür hata doğrulandı: başlık başka sayfadan
    kopyalanmış (2008 vergi "Temmuz" sayfasının başlığı "AĞUSTOS"; veri il
    dosyasıyla Temmuz olarak teyit edildi) ve sayfa adı bozuk (2004 "00
    Merkez" = Mayıs). Çelişkide, kaynaklardan biri başka bir sayfanın ayını
    tekrar ediyor ve diğeri benzersizse benzersiz olan seçilir ve not düşülür;
    aksi halde hata (tahmin yok).
    """
    from collections import Counter

    t_count = Counter(t for _, t, _ in entries if t)
    s_count = Counter(sm for _, _, sm in entries if sm)
    resolved: dict[str, int | None] = {}
    notes: list[str] = []
    for sheet, t, sm in entries:
        if t and sm and t != sm:
            if t_count[t] > 1 and s_count[sm] == 1 and t_count.get(sm, 0) == 0:
                resolved[sheet] = sm
                notes.append(
                    f"'{_clean(sheet)}': başlıktaki ay ({AYLAR[t - 1]}) başka sayfayla çakışıyor; "
                    f"sayfa adı ({AYLAR[sm - 1]}) kullanıldı"
                )
            elif s_count[sm] > 1 and t_count[t] == 1 and s_count.get(t, 0) == 0:
                resolved[sheet] = t
                notes.append(
                    f"'{_clean(sheet)}': sayfa adı başka sayfayla çakışıyor; başlıktaki ay ({AYLAR[t - 1]}) kullanıldı"
                )
            else:
                raise HmbLayoutError(
                    f"'{_clean(sheet)}' sayfa adı ({sm}. ay) ile başlık ({t}. ay) uyuşmuyor."
                )
        else:
            resolved[sheet] = t or sm
    unknown = [sh for sh, m in resolved.items() if m is None]
    if unknown and not single_sheet:
        free = set(range(1, 13)) - {m for m in resolved.values() if m}
        if len(unknown) == 1 and len(free) == 1:
            # 2007 Bursa: Aralık sayfasının adı ve başlığı "ANKARA"; diğer 11 ay
            # tekil olarak belli -> kalan tek ay atanır (eleme), not düşülür.
            resolved[unknown[0]] = free.pop()
            notes.append(
                f"'{_clean(unknown[0])}': ay ne başlıkta ne sayfa adında; diğer sayfalar tekil olduğundan "
                f"kalan tek ay ({AYLAR[resolved[unknown[0]] - 1]}) atandı"
            )
        else:
            raise HmbLayoutError(f"'{_clean(unknown[0])}' sayfasının dönemi belirlenemedi.")
    vals = [m for m in resolved.values() if m]
    if len(vals) != len(set(vals)):
        raise HmbLayoutError(f"Aynı dönem birden fazla sayfada: {sorted(vals)}")
    return resolved, notes


# Birim etiketi olmayan il tabloları (ör. 2022 MY gelir/vergi, 2026 mahalli
# gelir/denge) yalnızca aynı yılın etiketli kardeş tablosuyla toplamları
# birebir tuttuğunda kabul edilir: tablo -> [(kardeş tablo, kardeşteki
# kategori, bu tablodaki kategori adayları)].
UNIT_SIBLINGS: dict[str, list[tuple[str, str, list[str]]]] = {
    "gelir": [("denge", "Tahsilat", ["Merkezi Bütçe", "Konsolide Bütçe", "Merkezi Yönetim"])],
    "vergi": [("gelir", "Vergi Gelirleri", ["Tahsilat"])],
    "denge": [
        ("gelir", "Merkezi Bütçe", ["Tahsilat"]),
        ("gelir", "Konsolide Bütçe", ["Tahsilat"]),
        ("gelir", "Merkezi Yönetim", ["Tahsilat"]),
    ],
    "gider": [("denge", "Giderler", ["TOPLAM"])],
    "mahalli_gelir": [("mahalli_denge", "BÜTÇE GELİRLERİ", ["Bütçe Gelirleri"])],
    "mahalli_gider": [("mahalli_denge", "BÜTÇE GİDERLERİ", ["TOPLAM"])],
    "mahalli_denge": [
        ("mahalli_gider", "TOPLAM", ["BÜTÇE GİDERLERİ"]),
        ("mahalli_gelir", "Bütçe Gelirleri", ["BÜTÇE GELİRLERİ"]),
    ],
}


def parse_il_crosstab(
    content: bytes, yil: int, kaynak: str, birim_dogrula: bool = False, birim_beyani: str = ""
) -> pd.DataFrame:
    """İl satır x kategori sütun tabloları -> tidy uzun tablo.

    Doğrulamalar: tüm satır etiketleri bilinen il / Merkez / Toplam olmalı;
    başlıktaki yıl istenen yıl olmalı; ay (başlık) ile sayfa adı çelişmemeli;
    TL sütunlarında iller (+Merkez) toplamı "Toplam" satırına eşit olmalı
    (sapmalar meta["kontroller"]'e yazılır).
    """
    sheets = _sheets(content)
    birim_dogrulanmali = False
    entries: list[tuple[str, int | None, int | None]] = []
    rows_out: list[dict[str, Any]] = []
    kontroller: list[str] = []
    birimler: set[str] = set()
    donemler: list[tuple[int | None, str]] = []
    for sheet_name, data in sheets:
        # ilk veri satırı ve etiket sütunu
        r0 = lc = None
        for i, row in enumerate(data):
            for j in range(min(3, len(row))):
                code = il_kodu_of(row[j])
                if code and code != "TOPLAM" and any(_is_num(c) for c in row[j + 1 :]):
                    r0, lc = i, j
                    break
            if r0 is not None:
                break
        if r0 is None:
            if any(_row_texts(r) for r in data):
                raise HmbLayoutError(f"'{sheet_name}' sayfasında il satırı bulunamadı.")
            continue
        body: list[tuple[str, str, list[Any]]] = []
        for row in data[r0:]:
            label = _cell(row, lc)
            has_num = any(_is_num(c) for c in row[lc + 1 :])
            if not isinstance(label, str) or not label.strip():
                if has_num:
                    raise HmbLayoutError(f"'{sheet_name}': etiketsiz sayısal satır {row[:4]}")
                continue
            code = il_kodu_of(label)
            if code is None:
                if has_num:
                    raise HmbLayoutError(f"'{sheet_name}': tanınmayan satır etiketi {label!r}")
                break  # not satırları
            body.append((code, _clean(label), row))
            if code == "TOPLAM":
                break
        head_rows = [r for r in data[:r0] if not _is_legend_row(r)]
        ncols = max(len(r) for _, _, r in body)
        value_cols = [j for j in range(lc + 1, ncols) if any(_is_num(_cell(r, j)) for _, _, r in body)]
        headers = _column_headers(head_rows, list(range(lc + 1, ncols)))
        for j in value_cols:
            if not headers.get(j):
                raise HmbLayoutError(f"'{sheet_name}': {j}. sütunda veri var ama başlık yok.")
        title = " ".join(
            c for r in data[:r0] if not _is_legend_row(r) for _, c in _row_texts(r)
            if not is_period_cell(c)
        )
        if yil not in _years_in(title):
            raise HmbLayoutError(f"'{sheet_name}' başlığında {yil} yılı bulunamadı: {title[:100]!r}")
        entries.append((sheet_name, _title_month(title), _sheet_month(sheet_name)))
        unit_label = find_unit_label(c for r in data[:r0] for c in r)
        unit_src = "dosya"
        if unit_label is None:
            if birim_beyani:
                unit_label, unit_src = f"({birim_beyani})", "kullanıcı beyanı, dosyada etiket yok"
            elif birim_dogrula:
                # geçici; çağıran kardeş tabloyla doğrulamazsa veri reddedilir
                unit_label = "(Bin YTL)" if yil <= 2008 else "(Bin TL)"
                unit_src = "dosyada etiket yok — kardeş tabloyla doğrulanacak"
                birim_dogrulanmali = True
            elif all(_is_percent_header(headers[j]) for j in value_cols):
                unit_label = None
            else:
                raise HmbLayoutError(f"'{sheet_name}' sayfasında birim etiketi bulunamadı.")
        birim = birim_carpani(unit_label, yil) if unit_label else None
        birimler.add(f"{unit_label} ({unit_src})" if unit_label else "yalnız %")
        # Toplam tutarlılığı
        toplam = next((r for c, _, r in body if c == "TOPLAM"), None)
        if toplam is not None:
            for j in value_cols:
                if _is_percent_header(headers[j]) or not _is_num(_cell(toplam, j)):
                    continue
                s = sum(_cell(r, j) for c, _, r in body if c != "TOPLAM" and _is_num(_cell(r, j)))
                t = _cell(toplam, j)
                if abs(s - t) > max(2.0, 1e-6 * abs(t)) + 0.5 * sum(1 for c, _, _ in body if c != "TOPLAM"):
                    kontroller.append(f"{sheet_name}/{headers[j]}: iller toplamı {s:,.0f} ≠ Toplam {t:,.0f}")
        for code, label, row in body:
            for j in value_cols:
                v = _cell(row, j)
                if not _is_num(v):
                    continue
                pct = _is_percent_header(headers[j]) or birim is None
                rows_out.append(
                    {
                        "_sayfa": sheet_name,
                        "il_kodu": code if code != "TOPLAM" else "",
                        "il": IL_ADLARI.get(code, "Toplam"),
                        "il_kaynak": label,
                        "yil": yil,
                        "ay": None,
                        "donem": None,
                        "kaynak": kaynak,
                        "kategori": headers[j],
                        "deger_bin_tl": None if pct else v * birim.carpan,
                        "deger": v if pct else v * birim.carpan,
                        "birim": "%" if pct else "bin TL",
                        "donem_tipi": None,
                    }
                )
    if not rows_out:
        raise HmbLayoutError("Dosyada işlenebilir il satırı bulunamadı.")
    resolved, notes = resolve_sheet_months(entries, single_sheet=len(entries) == 1)
    for rec in rows_out:
        sheet_name = rec.pop("_sayfa")
        ay = resolved[sheet_name]
        tip = "kümülatif" if ay else "yıllık"
        donemler.append((ay, tip))
        rec["donem"] = _period(yil, ay)
        rec["donem_tipi"] = tip
        rec["ay"] = _clean(sheet_name) if _sheet_month(sheet_name) == ay and ay else (AYLAR[ay - 1] if ay else "Yıllık")
    kontroller = notes + kontroller
    frame = pd.DataFrame(rows_out)
    frame = frame.sort_values(["donem"], kind="stable").reset_index(drop=True)
    tipler = sorted({t for _, t in donemler})
    frame.attrs["meta"] = {
        "birim": "bin TL (yüzde sütunları birim='%' ve deger_bin_tl boş)",
        "kaynak_birim": sorted(birimler),
        "donem_tipi": (
            "kümülatif: her dönem yıl başından o dönem sonuna kadarki toplam"
            if tipler == ["kümülatif"]
            else ("yıllık" if tipler == ["yıllık"] else ", ".join(tipler))
        ),
        "donemler": sorted({d for d in frame["donem"].dropna()}),
        "il_sayisi": int(frame.loc[frame.il_kodu.str.len() == 2, "il_kodu"].nunique()),
        "kontroller": kontroller if any("≠" in k for k in kontroller)
        else kontroller + ["Toplam satırı = iller toplamı (tüm TL sütunları)"],
        "birim_dogrulanmali": birim_dogrulanmali,
    }
    return frame


def parse_sheet_crosstab(content: bytes, yil: int | None = None, birim_beyani: str = "") -> pd.DataFrame:
    """Her sayfası bir dönem olan, satır etiketi serbest çapraz tablolar
    (ör. Merkezi Yönetim "Eko_Fonk": fonksiyon satır x ekonomik sınıf sütun,
    sayfa başına bir ay, kümülatif). İl tablolarındaki doğrulamaların aynısı
    (yıl başlıkta, ay başlık/sayfa uyumu, birim etiketi, her veri sütununun
    başlığı) burada da zorunlu; il adı kontrolü yok."""
    sheets = _sheets(content)
    rows_out: list[dict[str, Any]] = []
    birimler: set[str] = set()
    tipler: set[str] = set()
    entries: list[tuple[str, int | None, int | None]] = []
    for sheet_name, data in sheets:
        r0 = lc = None
        for i in range(1, len(data)):
            row = data[i]
            for j in range(min(3, len(row))):
                lab = row[j]
                if (
                    isinstance(lab, str) and lab.strip() and not _is_num(lab)
                    and sum(1 for c in row[j + 1 :] if _is_num(c)) >= 2
                    and sum(1 for _, c in _row_texts(data[i - 1]) if _ > j) >= 2
                ):
                    r0, lc = i, j
                    break
            if r0 is not None:
                break
        if r0 is None:
            continue
        body = []
        for row in data[r0:]:
            lab = _cell(row, lc)
            has_num = any(_is_num(c) for c in row[lc + 1 :])
            if not isinstance(lab, str) or not lab.strip():
                if has_num:
                    raise HmbLayoutError(f"'{sheet_name}': etiketsiz sayısal satır.")
                continue
            if not has_num:
                break
            body.append((_clean(lab), row))
        ncols = max(len(r) for _, r in body)
        value_cols = [j for j in range(lc + 1, ncols) if any(_is_num(_cell(r, j)) for _, r in body)]
        head_rows = [r for r in data[:r0] if not _is_legend_row(r)]
        headers = _column_headers(head_rows, list(range(lc + 1, ncols)))
        for j in value_cols:
            if not headers.get(j):
                raise HmbLayoutError(f"'{sheet_name}': {j}. sütunda veri var ama başlık yok.")
        title = " ".join(
            c for r in data[:r0] if not _is_legend_row(r) for _, c in _row_texts(r)
            if not is_period_cell(c)
        )
        years = _years_in(title)
        y = yil or (years[0] if years else None)
        if y is None or (years and y not in years):
            raise HmbLayoutError(f"'{sheet_name}': yıl başlıktan doğrulanamadı: {title[:100]!r}")
        entries.append((sheet_name, _title_month(title), _sheet_month(sheet_name)))
        tip = "kümülatif" if "kumulatif" in fold(title) else "belirsiz"
        unit_label = find_unit_label(c for r in data[:r0] for c in r)
        if unit_label is None and birim_beyani:
            unit_label = f"({birim_beyani})"
        if unit_label is None and not all(_is_percent_header(headers[j]) for j in value_cols):
            raise HmbLayoutError(f"'{sheet_name}' sayfasında birim etiketi bulunamadı.")
        birim = birim_carpani(unit_label, y) if unit_label else None
        birimler.add(unit_label or "yalnız %")
        for sira, (lab, row) in enumerate(body, start=1):
            for j in value_cols:
                v = _cell(row, j)
                if not _is_num(v):
                    continue
                pct = _is_percent_header(headers[j]) or birim is None
                rows_out.append(
                    {
                        "sayfa": _clean(sheet_name),
                        "_sayfa": sheet_name,
                        "_yil": y,
                        "donem": None,
                        "satir_sira": sira,
                        "satir": lab,
                        "kategori": headers[j],
                        "deger": v if pct else v * birim.carpan,
                        "birim": "%" if pct else "bin TL",
                        "donem_tipi": tip,
                    }
                )
    if not rows_out:
        raise HmbLayoutError("Sayfa başına dönem düzeninde çapraz tablo bulunamadı.")
    resolved, notes = resolve_sheet_months(entries, single_sheet=len(entries) == 1)
    for rec in rows_out:
        ay = resolved[rec.pop("_sayfa")]
        rec["donem"] = _period(rec.pop("_yil"), ay)
        if ay is None:
            rec["donem_tipi"] = "yıllık"
        tipler.add(rec["donem_tipi"])
    frame = pd.DataFrame(rows_out).sort_values(["donem"], kind="stable").reset_index(drop=True)
    frame.attrs["meta"] = {
        "birim": "bin TL (yüzde sütunları birim='%')",
        "kaynak_birim": sorted(birimler),
        "donem_tipi": ", ".join(sorted(tipler)),
        "donemler": sorted({d for d in frame["donem"].dropna()}),
        "kontroller": notes,
    }
    if "belirsiz" in tipler:
        frame.attrs["meta"]["uyari"] = "Dönem tipi (aylık/kümülatif) başlıktan doğrulanamadı."
    return frame


# -- B: kalem satır x dönem sütun (geniş) tablolar -----------------------------


def _period_token(cell: Any) -> tuple[str, int] | None:
    m = month_number(cell)
    if m:
        return ("ay", m)
    if _is_num(cell) and float(cell).is_integer() and 1900 <= cell <= 2100:
        return ("yil", int(cell))
    if isinstance(cell, str) and re.fullmatch(r"\s*(19|20)\d\d\s*", cell):
        return ("yil", int(cell))
    return None


def parse_wide(
    content: bytes, yil: int | None = None, dosya_etiketi: str = "", birim_beyani: str = ""
) -> pd.DataFrame:
    """Kalem satır x dönem (ay veya yıl) sütun tabloları -> tidy uzun tablo.

    Ay sütunlu tablolarda "Toplam" sütunu varsa ayların toplamına eşitliği
    kontrol edilir; eşitse değerler AYLIK (ay içi akım) olarak işaretlenir.
    Bu kanıt yoksa ve başlık/etikette "ay içi" ya da "kümülatif" yazmıyorsa
    dönem tipi "belirsiz" raporlanır (stok tabloları dahil; tahmin edilmez).
    """
    rows_out: list[dict[str, Any]] = []
    kontroller: list[str] = []
    birimler: set[str] = set()
    tipler: set[str] = set()
    atlanan: list[str] = []
    for sheet_name, data in _sheets(content):
        h = None
        for i, row in enumerate(data[:40]):
            toks = [(j, _period_token(c)) for j, c in enumerate(row)]
            toks = [(j, t) for j, t in toks if t]
            if len(toks) >= 3:
                h, period_cols = i, dict(toks)
                break
        if h is None:
            if any(_row_texts(r) for r in data):
                atlanan.append(_clean(sheet_name))
            continue
        kinds = {t[0] for t in period_cols.values()}
        if len(kinds) != 1:
            raise HmbLayoutError(f"'{sheet_name}': başlık satırında ay ve yıl karışık.")
        kind = kinds.pop()
        first_p = min(period_cols)
        body_rows = data[h + 1 :]
        # etiket sütunu: dönem sütunlarının solunda en çok metin içeren sütun
        counts = {j: sum(1 for r in body_rows if isinstance(_cell(r, j), str) and _cell(r, j).strip() and any(_is_num(_cell(r, p)) for p in period_cols)) for j in range(first_p)}
        if not counts or max(counts.values()) == 0:
            raise HmbLayoutError(f"'{sheet_name}': kalem (etiket) sütunu bulunamadı.")
        lc = max(counts, key=lambda j: (counts[j], j))
        code_cols = [j for j in range(lc) if sum(1 for r in body_rows if _is_num(_cell(r, j))) > len(body_rows) // 3]
        header = data[h]
        ncols = max(len(r) for r in data[h:])
        extra_cols = [
            j for j in range(lc + 1, ncols)
            if j not in period_cols and isinstance(_cell(header, j), str) and _cell(header, j).strip()
        ]
        # grup başlıkları (başlık satırı üstündeki seyrek satırlar) -> sütun grubu
        groups: dict[int, str] = {}
        for row in data[:h]:
            cells = {j: _clean(c) for j, c in _row_texts(row) if j > lc and not _UNIT_BARE.match(fold(c))}
            if not cells or len(cells) > len(period_cols) // 2:
                continue
            current = None
            for j in range(lc + 1, ncols):
                if j in cells:
                    current = cells[j]
                if current is not None:
                    groups[j] = current
        top_cells = [c for r in data[: h + 1] for c in r]
        unit_label = find_unit_label(top_cells)
        if unit_label is None and birim_beyani:
            unit_label = f"({birim_beyani})"
            birimler.add(f"{unit_label} — kullanıcı beyanı, dosyada birim etiketi yok")
        title = " ".join(c for r in data[:h] for _, c in _row_texts(r))
        f_title = fold(title + " " + dosya_etiketi)
        data_years = [t[1] for t in period_cols.values()] if kind == "yil" else ([yil] if yil else _years_in(title)[:1])
        ref_year = max(data_years) if data_years else None
        if kind == "ay" and yil is None and _years_in(title):
            yil = _years_in(title)[0]
        if kind == "ay" and yil is not None and _years_in(title) and yil not in _years_in(title):
            raise HmbLayoutError(f"'{sheet_name}' başlığında {yil} yılı yok: {title[:100]!r}")
        if kind == "ay" and yil is None:
            raise HmbLayoutError(f"'{sheet_name}': ay sütunlu tablonun yılı belirlenemedi.")

        def col_unit(j: int) -> tuple[str, float | None]:
            g = groups.get(j, "")
            hdr = _cell(header, j) if j not in period_cols else ""
            if _is_percent_header(g) or (isinstance(hdr, str) and _is_percent_header(hdr)):
                return "%", None
            lbl = find_unit_label([g]) or unit_label
            if lbl is None:
                raise HmbLayoutError(f"'{sheet_name}': birim etiketi bulunamadı.")
            b = birim_carpani(lbl, ref_year)
            birimler.add(lbl)
            return "bin TL", b.carpan

        # Yayımlanmamış ay sütunları: yalnızca boş hücre ya da formül sıfırı
        # içerir (ör. 2026 Fon Payları "Fon Payları Toplamı" satırında Ekim-Aralık
        # = 0). Sıfır olmayan tek bir değer bile yoksa sütun veri sayılmaz.
        if kind == "ay":
            unpublished = [
                j for j in period_cols
                if not any(_is_num(_cell(r, j)) and _cell(r, j) != 0 for r in body_rows)
            ]
            if unpublished:
                kontroller.append(
                    f"{sheet_name}: yayımlanmamış ay sütunları atlandı (yalnızca boş/sıfır): "
                    + ", ".join(AYLAR[period_cols[j][1] - 1] for j in sorted(unpublished))
                )
                for j in unpublished:
                    del period_cols[j]
            if not period_cols:
                continue
        units = {j: col_unit(j) for j in list(period_cols) + extra_cols}
        toplam_cols = [j for j in extra_cols if fold(_cell(header, j)) == "toplam"]
        sira = 0
        sheet_rows = []
        for row in body_rows:
            label = _cell(row, lc)
            if not isinstance(label, str) or not label.strip():
                continue
            if not any(_is_num(_cell(row, j)) for j in list(period_cols) + extra_cols):
                continue
            sira += 1
            kod = ".".join(str(int(_cell(row, j))) for j in code_cols if _is_num(_cell(row, j)))
            sheet_rows.append((sira, label, kod, row))
        # dönem tipi
        if kind == "yil":
            tip = "yıllık"
        elif "kumulatif" in f_title:
            tip = "kümülatif"
        else:
            tip = "belirsiz"
            if toplam_cols:
                tj = toplam_cols[0]
                ok = bad = 0
                for _, label, _, row in sheet_rows:
                    t = _cell(row, tj)
                    if not _is_num(t) or units[tj][0] == "%":
                        continue
                    s = sum(_cell(row, p) for p in period_cols if _is_num(_cell(row, p)))
                    if abs(s - t) <= max(2.0, 1e-6 * abs(t)):
                        ok += 1
                    else:
                        bad += 1
                        if bad <= 5:
                            kontroller.append(f"{sheet_name}/{_clean(label)}: aylar toplamı {s:,.0f} ≠ Toplam {t:,.0f}")
                if ok and bad <= max(1, ok // 20):
                    tip = "aylık"
                    kontroller.append(f"{sheet_name}: {ok} satırda aylar toplamı = Toplam sütunu (aylık akım doğrulandı)")
            # "ay içi" açıkça akım demektir; tek başına "aylık" (ör. "Şarta Bağlı
            # Varlıklar (Aylık)" stok tablosu) kanıt sayılmaz.
            if tip == "belirsiz" and "ay ici" in f_title:
                tip = "aylık"
        tipler.add(tip)
        for sira, label, kod, row in sheet_rows:
            for j in list(period_cols) + extra_cols:
                v = _cell(row, j)
                if not _is_num(v):
                    continue
                birim, carpan = units[j]
                if j in period_cols:
                    k, n = period_cols[j]
                    donem = _period(yil, n) if k == "ay" else str(n)
                    sutun = AYLAR[n - 1] if k == "ay" else str(n)
                    if groups.get(j):
                        sutun = f"{groups[j]} — {sutun}"
                else:
                    donem, sutun = None, _clean(_cell(header, j))
                rec = {
                    "sayfa": _clean(sheet_name),
                    "kalem_sira": sira,
                    "kod": kod or None,
                    "kalem": _clean(label),
                    "girinti": len(label) - len(label.lstrip(" ")),
                    "sutun": sutun,
                    "donem": donem,
                    "deger": v if carpan is None else v * carpan,
                    "birim": birim,
                    "donem_tipi": tip if donem else "",
                }
                rows_out.append(rec)
    if not rows_out:
        raise HmbLayoutError("Dönem (ay/yıl) başlık satırı olan bir tablo bulunamadı.")
    frame = pd.DataFrame(rows_out)
    if frame["kod"].isna().all():
        frame = frame.drop(columns=["kod"])
    frame.attrs["meta"] = {
        "birim": "bin TL (yüzde sütunları birim='%')",
        "kaynak_birim": sorted(birimler),
        "donem_tipi": ", ".join(sorted(tipler)),
        "donemler": sorted({d for d in frame["donem"].dropna()}),
        "kontroller": kontroller,
    }
    if atlanan:
        frame.attrs["meta"]["atlanan_sayfalar"] = atlanan
    if "belirsiz" in tipler:
        frame.attrs["meta"]["uyari"] = (
            "Değerlerin aylık mı kümülatif mi olduğu dosyadan doğrulanamadı (donem_tipi='belirsiz')."
        )
    return frame


# -- Otomatik ----------------------------------------------------------------


def auto_parse(
    content: bytes, dosya_etiketi: str = "", birim_beyani: str = ""
) -> tuple[pd.DataFrame | None, list[str]]:
    """Bilinen düzenleri sırayla dener. Hiçbiri tüm doğrulamaları geçmezse
    (None, nedenler) döner; kısmen tutan bir tablo asla döndürülmez."""
    reasons: list[str] = []
    try:
        sheets = _sheets(content)
    except HmbLayoutError as exc:
        return None, [str(exc)]
    head_text = " ".join(
        c for _, d in sheets[:1] for r in d[:12] for c in r if isinstance(c, str)
    )
    years = _years_in(head_text) or _years_in(dosya_etiketi)
    yil = years[0] if years else None
    # C: il bazlı Tahakkuk/Tahsilat tablosu
    if any(
        {"tahakkuk", "tahsilat"} <= {fold(c) for r in d[:30] for c in r if isinstance(c, str)} for _, d in sheets
    ) and "kumulatif" in fold(head_text):
        m = re.match(r"^\s*(\d{2})\s*[-_]", dosya_etiketi)
        il_kodu = m.group(1) if m else None
        if il_kodu is None:
            il_part = re.split(r"\s(?:ili|genel)\s", " " + fold(head_text) + " ", maxsplit=1)[0]
            il_kodu = il_kodu_of(il_part)
        if il_kodu and il_kodu in IL_ADLARI and yil:
            try:
                frame = parse_il_statement(content, il_kodu=il_kodu, yil=yil)
                frame.attrs["meta"]["duzen"] = "il bazlı genel bütçe gelirleri (tahakkuk/tahsilat)"
                return frame, reasons
            except HmbLayoutError as exc:
                reasons.append(f"il-tablosu: {exc}")
    # A: il x kategori
    if yil:
        try:
            kaynak = "mahalli" if "mahalli" in fold(head_text) else "dosya"
            frame = parse_il_crosstab(content, yil=yil, kaynak=kaynak, birim_beyani=birim_beyani)
            frame.attrs["meta"]["duzen"] = "il satır x kategori sütun (çapraz)"
            return frame, reasons
        except HmbLayoutError as exc:
            reasons.append(f"il-çapraz: {exc}")
    # B: kalem x dönem
    try:
        frame = parse_wide(content, yil=None, dosya_etiketi=dosya_etiketi, birim_beyani=birim_beyani)
        frame.attrs["meta"]["duzen"] = "kalem satır x dönem sütun (geniş)"
        return frame, reasons
    except HmbLayoutError as exc:
        reasons.append(f"geniş: {exc}")
    # A': sayfa başına dönem, serbest satır etiketli çapraz tablo
    try:
        frame = parse_sheet_crosstab(content, yil=None, birim_beyani=birim_beyani)
        frame.attrs["meta"]["duzen"] = "sayfa başına dönem, satır x kategori (çapraz)"
        return frame, reasons
    except HmbLayoutError as exc:
        reasons.append(f"çapraz: {exc}")
    return None, reasons


def tree_to_rows(node: Folder, max_depth: int = 6) -> list[dict[str, Any]]:
    """Klasör ağacını düz satırlara çevirir (hmb_list_tables için)."""
    out = []
    for path, n in node.walk():
        if n is node or len(path) > max_depth:
            continue
        if n.id is None and not n.href:
            continue
        rec: dict[str, Any] = {"yol": " > ".join(path)}
        if n.id is not None:
            rec["klasor_id"] = n.id
            rec["klasor_adi"] = n.name
            rec["alt_klasor"] = bool(n.children)
        else:
            rec["baglanti"] = n.href
        out.append(rec)
    return out


# ---------------------------------------------------------------------------
# Araç (MCP) yardımcıları
# ---------------------------------------------------------------------------


def _write_csv(frame: pd.DataFrame, output_path: str) -> str:
    from pathlib import Path

    path = Path(output_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False)
    return str(path.resolve())


def tool_payload(frame: pd.DataFrame, max_rows: int, output_path: str = "", **extra: Any) -> dict[str, Any]:
    """Sohbete dönecek küçük özet: meta + en son dönem önce gelen kısa önizleme.
    Tam tablo yalnızca output_path verilirse CSV olarak yazılır."""
    meta = dict(frame.attrs.get("meta", {}))
    preview = frame
    if "donem" in frame.columns:
        order = frame["donem"].fillna("").astype(str)
        preview = frame.assign(_o=order).sort_values("_o", ascending=False, kind="stable").drop(columns="_o")
    max_rows = max(0, min(int(max_rows), 500))
    result: dict[str, Any] = {
        "n_rows": int(frame.shape[0]),
        "columns": list(frame.columns),
        **extra,
        "meta": meta,
        "preview_note": f"İlk {min(max_rows, len(frame))} satır, en son dönem önce. Tamamı için output_path verin.",
        "preview_csv": preview.head(max_rows).to_csv(index=False),
    }
    if output_path:
        result["written"] = _write_csv(frame, output_path)
    return result


def filter_frame(frame: pd.DataFrame, ay: int = 0, il_kodu: str = "") -> pd.DataFrame:
    attrs = frame.attrs
    out = frame
    if ay:
        if not 1 <= int(ay) <= 12:
            raise ValueError("ay 1-12 arası olmalı (0 = tüm aylar).")
        out = out[out["donem"].astype(str).str.endswith(f"-{int(ay):02d}")]
        if out.empty:
            raise HmbError(
                f"{ay}. ay bu tabloda yok. Mevcut dönemler: {attrs.get('meta', {}).get('donemler') or sorted(frame['donem'].dropna().unique())}"
            )
    if il_kodu:
        kod = str(il_kodu).strip().zfill(2) if str(il_kodu).strip().isdigit() else str(il_kodu).strip()
        out = out[out["il_kodu"] == kod]
        if out.empty:
            raise HmbError(f"il_kodu={il_kodu!r} bu tabloda yok.")
    out = out.reset_index(drop=True)
    out.attrs = attrs
    return out
