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
        self, url: str, dosya_etiketi: str = "", birim_beyani: str = "",
        klasor_id: int = 0, klasor_adi: str = "",
    ) -> tuple[pd.DataFrame | None, bytes, dict[str, Any]]:
        """Herhangi bir HMB dosyasını indir; tanınan düzendeyse tidy tablo döndür.

        Dönüş: (frame veya None, ham bayt, meta). Tanınmayan düzende frame None
        ve meta["neden"] açıklama içerir -- asla tahmini bir tablo üretilmez.

        Birim etiketi olmayan dosyalar için iki kardeş kontrolü denenir (ikisi
        de tutmazsa ret): (1) İller itibarıyla il x kategori tabloları, aynı
        yılın etiketli kardeş il tablosuyla (UNIT_SIBLINGS); (2) klasor_id /
        klasor_adi verilirse, aynı klasördeki birim etiketli bir dosyanın aynı
        dönem değerleri arasında bu tablonun kök (ilk) satırı birebir
        bulunursa (ör. etiketsiz Fkod3 "HARCAMALAR" = etiketli Ekod4 "BÜTÇE
        GİDERLERİ HESABI").
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
        if frame is None and not birim_beyani and any("birim etiketi" in r for r in reasons):
            for attempt in (self._unit_by_il_sibling, self._unit_by_folder_sibling):
                try:
                    frame = attempt(content, url, dosya_etiketi, klasor_id, klasor_adi)
                except HmbError as exc:
                    reasons.append(f"birim kardeş kontrolü: {exc}")
                    frame = None
                if frame is not None:
                    break
        if frame is None:
            meta["neden"] = "Tanınan bir düzen bulunamadı: " + " | ".join(reasons)
        else:
            meta.update(frame.attrs.get("meta", {}))
        return frame, content, meta

    def _unit_by_il_sibling(self, content: bytes, url: str, etiket: str, *_: Any) -> pd.DataFrame | None:
        """Etiketsiz il x kategori tablosu: aynı yılın etiketli kardeş il
        tablosuyla toplamlar birebir tutarsa (bespoke araçtaki kuralın aynısı)."""
        sheets = _sheets(content)
        # yalnız başlık metni (uzun hücreler); sütun başlıkları ("Vergi Gelirleri")
        # tablo türü seçimini şaşırtmasın
        head = " ".join(c for _, d in sheets[:1] for r in d[:6] for c in r if isinstance(c, str) and len(c.strip()) > 25)
        years = _years_in(head)
        if not years:
            return None
        f = fold(head)
        if "iller" not in f and "il bazinda" not in f:
            return None
        yil = years[0]
        frame = parse_il_crosstab(content, yil=yil, kaynak="dosya", birim_dogrula=True)
        if not frame.attrs["meta"].pop("birim_dogrulanmali", False):
            return None
        prefix = "mahalli_" if "mahalli" in f else ""
        order = ["denge", "vergi", "gider", "gelir"]
        tablo = next((t for t in order if (prefix + t) in KARSILASTIRMA_TABLOLARI
                      and KARSILASTIRMA_TABLOLARI[prefix + t]["sec"](f)), None)
        if tablo is None:
            return None
        frame.attrs["meta"]["kaynak_birim"] = [self._verify_unit(prefix + tablo, yil, frame, 0)]
        frame.attrs["meta"]["duzen"] = "il satır x kategori sütun (çapraz)"
        return frame

    def _unit_by_folder_sibling(
        self, content: bytes, url: str, etiket: str, klasor_id: int, klasor_adi: str
    ) -> pd.DataFrame | None:
        """Etiketsiz tablonun kök değerleri (ilk kalem satırı ya da çapraz
        tablonun TOPLAM x TOPLAM hücresi, dönem başına) aynı klasördeki birim
        etiketli bir dosyanın aynı dönem değerleri arasında birebir varsa birim
        kabul edilir. Dönem içi seriler gerektiğinde kümülatif toplanarak
        karşılaştırılır (ör. Ekod4 çeyrekleri <-> Eko-Fonk "OCAK-HAZİRAN")."""
        if not klasor_id:
            return None

        def tclass(t: Any) -> str:
            t = str(t or "")
            if "kümülatif" in t or t == "yıllık":
                return "kum"
            return "ici" if t in ("dönem içi", "aylık") else t

        def cumulate(fr: pd.DataFrame, keys: list[str]) -> list[tuple[str, str, float]]:
            out = []
            sub = fr[fr["donem_tipi"].map(tclass) == "ici"]
            for _, grp in sub.groupby(keys, sort=False):
                run = 0.0
                for d, v in sorted(zip(grp["donem"], grp["deger"]), key=lambda x: str(x[0])):
                    run += float(v)
                    out.append((str(d), "kum", run))
            return out

        duzen = "kalem satır x dönem aralığı/yıl sütun"
        try:
            frame = parse_kalem_tablosu(content, dosya_etiketi=etiket, birim_beyani="Bin TL")
            _assert_unique_cells(frame)
            tl = frame[(frame["birim"] == "bin TL") & frame["donem"].notna()]
            own: list[tuple[str, str, float]] = []
            for sayfa, grp in tl.groupby("sayfa", sort=False):
                first = grp[grp["kalem_sira"] == grp["kalem_sira"].min()]
                own += [(str(d), tclass(t), float(v)) for d, t, v in zip(first["donem"], first["donem_tipi"], first["deger"])]
                own += cumulate(first, ["sayfa"])
        except HmbLayoutError:
            frame = parse_sheet_crosstab(content, yil=None, birim_beyani="Bin TL")
            _assert_unique_cells(frame)
            duzen = "sayfa başına dönem, satır x kategori (çapraz)"
            tot = frame[(frame["satir"].map(fold) == "toplam") & (frame["kategori"].map(fold).str.endswith("toplam"))]
            own = [(str(d), tclass(t), float(v)) for d, t, v in zip(tot["donem"], tot["donem_tipi"], tot["deger"])]
        own = [o for o in own if abs(o[2]) >= 1000]
        if not own:
            raise HmbLayoutError("kök satırda karşılaştırılabilir değer yok")
        files = [x for x in self.list_files(klasor_id, klasor_adi) if x.uzanti in ("xls", "xlsx") and x.url != url]
        if not files:
            raise HmbLayoutError("klasörde başka dosya yok")
        tried = []
        for sib in files[:6]:
            sframe, _ = auto_parse(self.download(sib.url), dosya_etiketi=sib.etiket)
            if sframe is None:
                tried.append(f"{sib.etiket}: tanınmadı")
                continue
            units = " ".join(sframe.attrs["meta"].get("kaynak_birim", []))
            if "kullanıcı" in units or "doğrulan" in units or not re.search(r"bin\s*(y?tl)", fold(units)):
                tried.append(f"{sib.etiket}: kendi birim etiketi yok")
                continue
            if "deger" not in sframe.columns or "donem" not in sframe.columns:
                continue
            sf = sframe[sframe["donem"].notna() & sframe["deger"].notna()]
            pool: dict[tuple[str, str], list[float]] = {}
            for d, t, v in zip(sf["donem"], sf["donem_tipi"], sf["deger"]):
                pool.setdefault((str(d), tclass(t)), []).append(float(v))
            keys = [k for k in ("sayfa", "kalem_sira", "satir_sira", "kategori", "il_kodu") if k in sf.columns]
            for d, t, v in cumulate(sf, keys):
                pool.setdefault((d, t), []).append(v)

            def hit(o: tuple[str, str, float]) -> bool:
                # kardeşin dönem tipi "belirsiz" olabilir (eski çapraz düzen); birim
                # kanıtı için ölçeğin (7-10 anlamlı basamağın) birebir tutması yeter.
                if o[1] == "belirsiz":
                    cands = [w for (d, _t), ws in pool.items() if d == o[0] for w in ws]
                else:
                    cands = pool.get((o[0], o[1]), []) + pool.get((o[0], "belirsiz"), [])
                return any(abs(o[2] - w) <= max(1.0, 1e-7 * abs(o[2])) for w in cands)

            raw = [o for o in own if o[1] != "kum" or duzen.startswith("sayfa")]
            cum = [o for o in own if o[1] == "kum"]
            for label, group in (("aynı dönem değeriyle", raw), ("dönemler kümülatif toplandığında aynı dönem değeriyle", cum)):
                if group and all(hit(o) for o in group):
                    unit = sframe.attrs["meta"]["kaynak_birim"][0]
                    frame.attrs["meta"]["kaynak_birim"] = [
                        f"(Bin TL) — dosyada etiket yok; {len(group)} dönemde kök değer, aynı klasördeki "
                        f"'{sib.etiket}' tablosunun {label} birebir aynı (o tablonun birimi: {unit}) — doğrulandı"
                    ]
                    frame.attrs["meta"]["duzen"] = duzen
                    return frame
            tried.append(f"{sib.etiket}: {sum(1 for o in raw if hit(o))}/{len(raw)} dönem eşleşti")
        raise HmbLayoutError("aynı klasörde kök değeri eşleşen etiketli tablo yok (" + "; ".join(tried) + ")")


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


_NOTE_LABEL = re.compile(
    r"^\s*(\*|\(\s*\*|\(\s*\d{1,2}\s*\)|\[\s*\d{1,2}\s*\]|\d{1,2}\s*\)\s|not\b|notlar|kaynak\b|aciklama|dipnot|uyari)"
)


def _is_note_label(label: str) -> bool:
    """Dipnot / açıklama satırı: işaretle ("*", "(1)", "Not:", "Kaynak") başlar ya da çok uzundur."""
    f = fold(label)
    text = _clean(label)
    return bool(_NOTE_LABEL.match(f)) or len(text) > 100


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
    uyarilar: list[str] = []
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
        # Sayısız etiketli satır (boş kalem ya da bölüm başlığı) tabloyu
        # bitirmez; yalnızca dipnot/not satırı ya da sağında metin olan satır
        # (alttaki ikinci tablonun başlığı) bitirir. (2026-11: önceki sürüm ilk
        # boş kalemde duruyordu; ör. Özel Bütçeli İdareler Gelirleri ~%2.)
        gap = False
        stop: tuple[int, str] | None = None
        for k in range(r0, len(data)):
            row = data[k]
            lab = _cell(row, lc)
            has_num = any(_is_num(c) for c in row[lc + 1 :])
            right_text = any(j > lc for j, _ in _row_texts(row))
            if not isinstance(lab, str) or not lab.strip():
                if has_num and not gap:
                    raise HmbLayoutError(f"'{sheet_name}': etiketsiz sayısal satır.")
                if gap and (right_text or has_num):
                    # boşluktan sonra düzen değişiyor (ikinci tablonun başlığı ya
                    # da etiketsiz sayılar): tablo burada biter, uyarı düşülür
                    stop = (k, "etiketsiz sayısal satır" if has_num else "başlık satırı")
                    break
                continue
            if not has_num:
                if right_text or _is_note_label(lab):
                    stop = (k, "not/başlık satırı")
                    break
                gap = True
                continue
            body.append((_clean(lab), row))
        if stop is not None:
            kalan = sum(
                1 for r in data[stop[0] + 1 :]
                if isinstance(_cell(r, lc), str) and _cell(r, lc).strip()
                and any(_is_num(c) for c in r[lc + 1 :])
            )
            if kalan:
                uyarilar.append(
                    f"'{_clean(sheet_name)}': tablo {stop[0] + 1}. satırdaki {stop[1]} "
                    f"({_clean(_cell(data[stop[0]], lc))[:60]!r}) ile bitti sayıldı; altındaki {kalan} "
                    "sayısal satır (ayrı tablo ya da not olabilir) alınmadı."
                )
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
        uyarilar.insert(0, "Dönem tipi (aylık/kümülatif) başlıktan doğrulanamadı.")
    if uyarilar:
        frame.attrs["meta"]["uyari"] = " ".join(uyarilar)
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


# -- D: genel kalem tabloları (dönem aralığı / yıl / tek dönem sütunlu) ------
#
# 2026-11-12 sınıflandırmasıyla eklendi: Genel Yönetim / Mahalli İdareler / SGK
# alt sektör ve bütçe istatistiklerinin büyük kısmı "kalem satır x dönem
# sütun" tablolarıdır ama dönem başlıkları tek ay adı değil ARALIKTIR
# ("Ocak-Mart", "OCAK-HAZİRAN", "Nisan - Haziran Dönemi", "2025 YILI"), satır
# kodları birkaç sütuna bölünmüştür (Ekod 1/1/10/1, hesap kodu 102, 630/1/0)
# ve bazen iki blok yan yanadır (varlık | yükümlülük). Kanıt kuralları:
#   * Dönem: her değer sütununun başlığındaki aralık/yıl ya da (sütun başlığı
#     dönem taşımıyorsa) başlıktaki aralık/ay; yalnızca başlıktaki yıl varsa
#     ve o yıl kapanmışsa "yıllık". Hiçbiri yoksa ret.
#   * Dönem tipi: tüm aralıklar Ocak'ta başlıyorsa kümülatif; ardışık ve
#     örtüşmeyen aralıklar (Ocak-Mart, Nisan-Haziran...) dönem içi akım
#     (varsa TOPLAM sütunu = aralıklar toplamı kontrol edilir); başka her
#     düzen ret. Hesap kodları 1-5 (bilanço hesapları) ya da başlıkta
#     "varlık ve yükümlülük" geçen tablolarda değerler dönem sonu bakiyesidir.
#   * Birim: tablo üstünde/başlıkta etiket ("(Bin TL)", "Tutar (Bin TL)");
#     yalnız "%" sütunları birimsiz kabul edilir. Etiket yoksa ret.
#   * İç tutarlılık: satır kodları varsa her üst kalem = alt kalemlerinin
#     toplamı (her değer sütununda) kontrol edilir; sapma oranı %5'i aşarsa
#     düzen yanlış okunmuş sayılır ve tablo reddedilir, daha azı meta'ya yazılır.

_CEYREK_RE = re.compile(r"^(iv|i{1,3})\s*\.?\s*(?:ceyrek|quarter)$")
_RANGE_WORDS = re.compile(r"\b(donemi?|ayi|aylari|toplami|arasi)\b")


def range_token(cell: Any) -> tuple[int, int] | None:
    """"Ocak-Mart" / "OCAK - HAZİRAN DÖNEMİ" / "(OCAK-EYLÜL)" / "I. Çeyrek" ->
    (başlangıç ayı, bitiş ayı). Hücre tamamen bir aralık olmalı."""
    if not isinstance(cell, str):
        return None
    t = fold(cell.replace("–", "-").replace("—", "-"))
    t = re.sub(r"[()*:]", " ", t)
    t = _RANGE_WORDS.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip()
    m = re.fullmatch(r"([a-z]+)\s*-\s*([a-z]+)", t)
    if m:
        a, b = _month_token(m.group(1)), _month_token(m.group(2))
        if a and b and a <= b:
            return (a, b)
        return None
    m = _CEYREK_RE.fullmatch(t)
    if m:
        q = _DONEM_END[m.group(1)]
        return (q - 2, q)
    return None


def _is_year_num(v: Any) -> bool:
    return _is_num(v) and float(v).is_integer() and 1900 <= v <= 2100


def _code_part(v: Any) -> int | None:
    """Kod hücresi: tam sayı (1.0) ya da yalnız rakamdan metin ('01')."""
    if _is_num(v) and float(v).is_integer() and 0 <= v < 10**7:
        return int(v)
    if isinstance(v, str) and re.fullmatch(r"\s*\d{1,7}\s*", v):
        return int(v)
    return None


@dataclass
class _ColHead:
    ad: str
    pct: bool = False
    toplam: bool = False
    aralik: tuple[int, int] | None = None
    ay: int | None = None
    yil: int | None = None


def _classify_head(parts: list[Any]) -> _ColHead:
    texts = [_clean(p) for p in parts if isinstance(p, str) and p.strip()]
    nums = [int(p) for p in parts if _is_year_num(p)]
    head = _ColHead(ad=" — ".join(texts + [str(n) for n in nums]))
    if nums:
        head.yil = nums[-1]
    for t in texts:
        f = fold(t)
        if "%" in t:
            head.pct = True
        ys = _years_in(t)
        if ys:
            head.yil = ys[-1]
        rest = re.sub(r"(?<!\d)(19|20)\d\d(?!\d)", " ", t)
        rest = re.sub(r"%|\b[Dd]a[ğg][ıi]l[ıi]m\b|[Yy][ıi]l[ıi]?\b|YILI|Yılı|yılı", " ", rest)
        rest = rest.strip(" .")
        r = range_token(rest)
        if r:
            head.aralik = r
            continue
        m = month_number(rest)
        if m:
            head.ay = m
            continue
        ft = f.strip(" .:")
        if ft in ("toplam", "toplami", "genel toplam", "yillik toplam") or ft.endswith((" toplam", " toplami")):
            head.toplam = True
    return head


def _title_period(text: str) -> tuple[tuple[int, int] | None, int | None]:
    """Başlıktaki dönem: ("OCAK-EYLÜL DÖNEMİ" -> ((1,9), None),
    "MART AYI" -> (None, 3)). Birden çok farklı dönem varsa (None, None)."""
    f = fold(text.replace("–", "-"))
    ranges = set()
    for a, b in re.findall(r"\b([a-z]+)\s*-\s*([a-z]+)\b", f):
        x, y = _month_token(a), _month_token(b)
        if x and y and x <= y:
            ranges.add((x, y))
    if len(ranges) == 1:
        return ranges.pop(), None
    if ranges:
        return None, None
    months = {m for m in (_month_token(t) for t in re.findall(r"[a-z]+", f)) if m}
    if len(months) == 1:
        return None, months.pop()
    return None, None


def _hier_check(entries: list[tuple[tuple, dict[int, float]]], cols: list[int]) -> tuple[int, list[str]]:
    """Kodlu satırlarda üst kalem = alt kalemler toplamı. entries: (kod tuple,
    {sütun: değer}). Kod hiyerarşisi tuple önekidir; aynı kod iki kez farklı
    değerle geçiyorsa o kod kontrol dışı."""
    by_code: dict[tuple, dict[int, float]] = {}
    dup: set[tuple] = set()
    for code, vals in entries:
        if code in by_code and by_code[code] != vals:
            dup.add(code)
        by_code[code] = vals
    children: dict[tuple, list[tuple]] = {}
    for code in by_code:
        if code and code not in dup:
            children.setdefault(code[:-1], []).append(code)
    ok, bad = 0, []
    for parent, kids in children.items():
        if parent not in by_code or parent in dup:
            continue
        for j in cols:
            pv = by_code[parent].get(j)
            kv = [by_code[k].get(j) for k in kids]
            if pv is None or all(v is None for v in kv):
                continue
            s = sum(v for v in kv if v is not None)
            if abs(s - pv) <= max(2.0, 1e-6 * abs(pv)) + 0.51 * len(kids):
                ok += 1
            else:
                bad.append(f"{'.'.join(map(str, parent)) or 'kök'} sütun {j}: alt toplam {s:,.1f} ≠ {pv:,.1f}")
    return ok, bad


class _SayfaAtla(HmbLayoutError):
    """Sayfada çözümlenecek yapı yok (yıl/başlık/birim eksik). Dosyanın
    başka sayfaları doğrulanırsa bu sayfa atlanır ve nedeni meta'ya yazılır;
    doğrulama hataları (toplam/hiyerarşi/dönem) ise tüm dosyayı reddeder."""


def _title_years(text: str) -> list[int]:
    """Başlıktaki veri yılları; "(2007 Yılı Bazında)" gibi sınıflandırma baz
    yılı ifadeleri sayılmaz."""
    f = re.sub(r"(19|20)\d\d\s*yili\s*bazinda", " ", fold(text))
    return _years_in(f)


def _row_kind(row: list[Any]) -> tuple[list[int], list[int]]:
    texts = [j for j, c in enumerate(row) if isinstance(c, str) and c.strip()
             and _code_part(c) is None and not range_token(c) and not month_number(c)]
    nums = [j for j, c in enumerate(row) if _is_num(c)]
    return texts, nums


def _is_data_row(row: list[Any]) -> bool:
    texts, nums = _row_kind(row)
    if not texts or not nums:
        return False
    vals = [row[j] for j in nums if j > texts[0]]
    if not vals:
        return False
    return not all(_is_year_num(v) for v in vals)


def _find_blocks(data: list[list[Any]], r0: int, rows: list[int]) -> list[tuple[int, int, int]]:
    """(blok başı, etiket sütunu, blok sonu) listesi. İkinci blok: veri
    satırlarının çoğunda sayılardan SONRA ikinci bir etiket metni varsa."""
    first_lab: dict[int, int] = {}
    second_lab: dict[int, int] = {}
    for i in rows:
        texts, nums = _row_kind(data[i])
        if not texts:
            continue
        first_lab[texts[0]] = first_lab.get(texts[0], 0) + 1
        later = [t for t in texts[1:] if any(texts[0] < n < t for n in nums)]
        if later:
            second_lab[later[0]] = second_lab.get(later[0], 0) + 1
    lc = max(first_lab, key=lambda j: (first_lab[j], -j))
    ncols = max(len(data[i]) for i in rows)
    if second_lab:
        lc2 = max(second_lab, key=lambda j: (second_lab[j], -j))
        if second_lab[lc2] >= 0.4 * len(rows) and lc2 > lc + 1:
            start2 = lc2
            if lc2 - 1 > lc and sum(1 for i in rows if (_code_part(_cell(data[i], lc2 - 1)) or 0) > 0) >= 0.3 * len(rows):
                start2 = lc2 - 1
            return [(0, lc, start2), (start2, lc2, ncols)]
    return [(0, lc, ncols)]


def _header_parts(data: list[list[Any]], hdr_rows: list[int], cols: list[int]) -> dict[int, list[Any]]:
    """Sütun başlık yığını; seyrek satırlardaki grup başlıkları sağa yayılır."""
    parts: dict[int, list[Any]] = {j: [] for j in cols}
    for i in hdr_rows:
        row = data[i]
        present = {}
        for j in cols:
            c = _cell(row, j)
            if isinstance(c, str) and c.strip():
                f = fold(c)
                if (_UNIT_BARE.match(f) or _UNIT_PAREN.fullmatch(f.strip())) and "%" not in c:
                    continue
                present[j] = c
            elif _is_year_num(c):
                present[j] = c
        if not present:
            continue
        sparse = len(present) < len(cols) and len(present) <= max(1, len(cols) // 2)
        current = None
        for j in cols:
            if j in present:
                current = present[j]
                parts[j].append(current)
            elif sparse and current is not None and (
                (isinstance(current, str) and not range_token(current) and not month_number(current))
                or _is_year_num(current)
            ):
                parts[j].append(current)
    return parts


def _parse_kalem_sheet(
    sheet_name: str, data: list[list[Any]], yil_hint: int | None, birim_beyani: str, notes: list[str]
) -> list[dict[str, Any]]:
    """Bir sayfadaki kalem tablosu -> kayıtlar (boş liste: tablo yok)."""
    import datetime as _dt

    for row in data[:30]:
        f = [fold(c) for c in row if isinstance(c, str)]
        if any(c.startswith("aktif") for c in f) and any(c.startswith("pasif") for c in f):
            raise HmbLayoutError(f"'{sheet_name}': bilanço düzeni (aktif | pasif); genel kalem ayrıştırıcısı uygulanmaz.")
    if _mizan_header(data) is not None:
        raise HmbLayoutError(f"'{sheet_name}': mizan düzeni; genel kalem ayrıştırıcısı uygulanmaz.")
    import datetime as _dt0

    dated = sum(1 for r in data if r and isinstance(r[0], (_dt0.date, _dt0.datetime)) and any(_is_num(c) for c in r[1:]))
    if dated >= 3:
        raise HmbLayoutError(
            f"'{sheet_name}': revizyon ölçüm (vintaj) tablosu — satırlar yayın tarihleri, sütunlar çeyrekler; "
            "bu düzen desteklenmiyor."
        )
    data_rows = [i for i, r in enumerate(data) if _is_data_row(r)]
    if len(data_rows) < 2:
        return []
    r0 = None
    for k, i in enumerate(data_rows):
        nxt = [x for x in data_rows[k + 1 : k + 4] if x - i <= 6]
        if len(nxt) >= min(2, len(data_rows) - k - 1):
            r0 = i
            break
    if r0 is None:
        return []
    if any(isinstance(c, (_dt.date, _dt.datetime)) for r in data[r0:] for c in r[:3]):
        raise HmbLayoutError(f"'{sheet_name}': satır etiketleri tarih (revizyon/vintaj tablosu) — desteklenmiyor.")
    body = [i for i in data_rows if i >= r0]
    blocks = _find_blocks(data, r0, body)
    title_rows = list(range(r0))
    out: list[dict[str, Any]] = []
    all_title = " ".join(c for i in title_rows for _, c in _row_texts(data[i]))
    for bstart, lc, bend in blocks:
        ncols = min(bend, max(len(data[i]) for i in body))
        value_cols = [j for j in range(lc + 1, ncols) if any(_is_num(_cell(data[i], j)) for i in body)]
        code_cols = [
            j for j in range(bstart, lc)
            if sum(1 for i in body if _code_part(_cell(data[i], j)) is not None) >= 0.3 * len(body)
        ]
        if not value_cols:
            continue
        heads_raw = _header_parts(data, title_rows, value_cols)
        heads = {j: _classify_head(heads_raw[j]) for j in value_cols}
        # başlık metni: değer sütunlarında olmayan hücreler
        title = " ".join(
            c for i in title_rows for j, c in _row_texts(data[i]) if j not in value_cols
        )
        bolum = _clean(_cell(data[r0 - 1], lc)) if r0 and isinstance(_cell(data[r0 - 1], lc), str) else ""
        unlabeled = [j for j in value_cols if not heads[j].ad and not heads[j].pct]
        if len(unlabeled) > 1:
            # başlıksız sütun yalnızca TEK ise kabul (dönemi başlıktan alınır)
            raise _SayfaAtla(f"'{sheet_name}': {unlabeled[0]}. sütunda veri var ama başlık yok.")
        # yıl
        col_years = {heads[j].yil for j in value_cols if heads[j].yil}
        t_years = sorted(set(_title_years(title)))
        if col_years:
            yil = t_years[0] if len(t_years) == 1 else None
        elif len(t_years) == 1:
            yil = t_years[0]
        elif not t_years and yil_hint:
            yil = yil_hint
            notes.append(f"{sheet_name}: yıl başlıkta yok, dosya etiketinden ({yil_hint}) alındı")
        else:
            raise _SayfaAtla(f"'{sheet_name}': veri yılı belirlenemedi (başlıktaki yıllar: {t_years}).")
        if yil and t_years and yil not in t_years:
            raise HmbLayoutError(f"'{sheet_name}': başlıkta {yil} yılı yok.")
        t_range, t_month = _title_period(title)
        # birim
        unit_cells = [c for i in title_rows for c in data[i]] + [_cell(data[r0 - 1], lc) if r0 else ""]
        unit_label = find_unit_label(unit_cells)
        unit_src = "dosya"
        all_pct = all(heads[j].pct for j in value_cols)
        if unit_label is None and not all_pct:
            if any(isinstance(c, str) and re.fullmatch(r"\(?\s*%\s*\)?", c.strip()) for c in unit_cells):
                all_pct = True
                for j in value_cols:
                    heads[j].pct = True
            elif birim_beyani:
                unit_label, unit_src = f"({birim_beyani})", "kullanıcı beyanı, dosyada etiket yok"
            else:
                raise _SayfaAtla(f"'{sheet_name}': birim etiketi bulunamadı.")
        # dönemler
        flow_cols = [j for j in value_cols if not heads[j].pct and not heads[j].toplam]
        col_period: dict[int, tuple[int, int | None, int | None]] = {}  # yıl, başlangıç ayı, bitiş ayı
        for j in value_cols:
            h = heads[j]
            y = h.yil or yil
            if h.aralik:
                col_period[j] = (y, h.aralik[0], h.aralik[1])
            elif h.ay:
                col_period[j] = (y, h.ay, h.ay)
            elif h.yil:
                col_period[j] = (y, None, None)
            elif not h.toplam:
                if t_range:
                    col_period[j] = (y, t_range[0], t_range[1])
                elif t_month:
                    col_period[j] = (y, None, t_month)
                elif h.pct and len(col_period) and j > min(col_period):
                    pass  # pay sütunu: dönemi önceki sütunlarınki, aşağıda
                else:
                    col_period[j] = (y, None, None)
        # bakiye (stok) tabloları
        codes_raw = {i: [_code_part(_cell(data[i], j)) for j in code_cols] for i in body}
        single_hesap = len(code_cols) == 1 and all(
            c[0] is None or (1 <= len(str(c[0])) <= 3) for c in codes_raw.values()
        )
        f_title = fold(all_title + " " + sheet_name)
        hesap_classes = {str(c[0])[0] for c in codes_raw.values() if c and c[0]}
        stok = ("varlik ve yukumluluk" in f_title or "bilanco" in f_title or "sarta bagli" in f_title
                or (single_hesap and hesap_classes and hesap_classes <= set("12345")
                    and any(c and c[0] and len(str(c[0])) == 3 for c in codes_raw.values())))
        if any(p[0] is None for p in col_period.values()):
            raise _SayfaAtla(f"'{sheet_name}': bazı sütunların yılı belirlenemedi.")
        periods = [col_period[j] for j in flow_cols if j in col_period]
        ranges = [(a, b) for _, a, b in periods if a]
        tip = None
        col_tip_override: dict[int, str] = {}
        month_cols = [j for j in flow_cols if j in col_period and heads[j].ay and not heads[j].aralik]
        year_only = [j for j in flow_cols if j in col_period and col_period[j][1] is None and col_period[j][2] is None]
        tcols0 = [j for j in value_cols if heads[j].toplam and not heads[j].pct]
        if month_cols and not stok and len(month_cols) + len(year_only) == len(periods) and len(month_cols) >= 2:
            # Ay sütunları: değerlerin AY İÇİ olduğu ancak "Toplam" sütunu = aylar
            # toplamıyla (ya da başlıkta "ay içi") kanıtlanır; yanlarındaki yıl
            # başlıklı sütunlar (ör. "2003 Bütçe Hedefi") yıllıktır.
            ok = bad = 0
            if tcols0:
                for i in body:
                    t = _cell(data[i], tcols0[0])
                    if not _is_num(t):
                        continue
                    sm = sum(_cell(data[i], j) for j in month_cols if _is_num(_cell(data[i], j)))
                    if abs(sm - t) <= max(2.0, 1e-6 * abs(t)) + 0.51 * len(month_cols):
                        ok += 1
                    else:
                        bad += 1
            if ok and bad <= (ok + bad) // 20:
                notes.append(f"{sheet_name}: {ok} satırda aylar toplamı = '{heads[tcols0[0]].ad}' sütunu (ay içi akım doğrulandı)"
                             + (f"; {bad} sapma" if bad else ""))
            elif "ay ici" not in fold(all_title):
                raise HmbLayoutError(
                    f"'{sheet_name}': ay sütunlarının ay içi mi kümülatif mi olduğu doğrulanamadı "
                    f"(Toplam sütunu kontrolü: {ok} tutan, {bad} tutmayan satır)."
                )
            tip = "aylık"
            col_tip_override = {j: "yıllık" for j in year_only}
        elif stok and periods and all(b for _, _, b in periods):
            # bakiye tablosunda "Ocak-Mart" de "MART" da dönem sonunu belirtir
            ends = [(y, b) for y, _, b in periods]
            if len(set(ends)) != len(ends):
                raise HmbLayoutError(f"'{sheet_name}': aynı dönem sonu birden fazla sütunda.")
            tip = "dönem sonu bakiye"
        elif ranges and len(ranges) == len(periods):
            starts = {a for a, _ in ranges}
            ends = [b for _, b in ranges]
            if len(set(ends)) != len(ends) and len({p for p in periods}) != len(periods):
                raise HmbLayoutError(f"'{sheet_name}': aynı dönem birden fazla sütunda.")
            if starts == {1}:
                tip = "kümülatif" if ranges != [(1, 12)] else "yıllık"
            else:
                srt = sorted(ranges)
                consecutive = srt[0][0] == 1 and all(srt[k][0] == srt[k - 1][1] + 1 for k in range(1, len(srt)))
                if not consecutive:
                    raise HmbLayoutError(f"'{sheet_name}': dönem aralıkları ne kümülatif ne ardışık: {ranges}")
                tip = "dönem içi"
        elif periods and all(a is None and b is None for _, a, b in periods):
            tip = "yıllık"
        elif periods and all(a is None and b for _, a, b in periods):
            tip = "belirsiz"  # yalnız "MART AYI": ay içi mi kümülatif mi bilinmez
        elif not periods:
            if not all_pct:
                raise HmbLayoutError(f"'{sheet_name}': değer sütunlarının dönemi belirlenemedi.")
            tip = "yıllık"
        else:
            raise HmbLayoutError(f"'{sheet_name}': sütun dönemleri karışık (aralık/yıl/ay).")
        if stok and tip in ("kümülatif", "yıllık", "belirsiz"):
            tip = "dönem sonu bakiye"
        elif stok and tip != "dönem sonu bakiye":
            raise HmbLayoutError(f"'{sheet_name}': bakiye tablosunda dönem içi aralıklar beklenmiyor.")
        elif tip == "belirsiz":
            raise HmbLayoutError(
                f"'{sheet_name}': başlıkta yalnız ay var; değerlerin ay içi mi kümülatif mi olduğu doğrulanamadı."
            )
        if tip == "yıllık":
            import datetime as _dtm

            yrs = {p[0] for p in periods} or {yil}
            only_title = not col_years and not any(heads[j].aralik for j in value_cols)
            if only_title and (None in yrs or max(y for y in yrs if y) >= _dtm.date.today().year):
                raise HmbLayoutError(
                    f"'{sheet_name}': dönem yalnızca başlıktaki yıldan ({sorted(y for y in yrs if y)}); "
                    "yıl kapanmadığı için yıllık olduğu varsayılamaz."
                )
            if only_title:
                notes.append(f"{sheet_name}: dönem sütun başlığında yok; başlıktaki kapanmış yıl → yıllık")
        ref_year = max([p[0] for p in col_period.values() if p[0]] or [yil or 0]) or None
        birim = birim_carpani(unit_label, ref_year) if unit_label else None
        # satırlar
        entries: list[tuple[tuple, dict[int, float]]] = []
        recs: list[dict[str, Any]] = []
        sira = 0
        for i in body:
            row = data[i]
            label = _cell(row, lc)
            vals = {j: _cell(row, j) for j in value_cols if _is_num(_cell(row, j))}
            if not vals:
                continue
            parts = codes_raw[i]
            if not isinstance(label, str) or not label.strip():
                # toplam satırları etiketi kod sütununa yazılmış olabilir
                # ("A- KISA VADELİ İÇ BORÇLAR TOPLAMI"); ikinci blokta etiket
                # satırın başında olabilir ("NET MALİ DEĞER (A-B)").
                fv = min(value_cols)
                others = [k for k in range(len(row)) if k < bstart or k >= bend]
                lo = 0 if not any(_is_num(_cell(row, k)) for k in others) else bstart
                cands = [_cell(row, k) for k in range(lo, fv)
                         if isinstance(_cell(row, k), str) and _cell(row, k).strip() and _code_part(_cell(row, k)) is None]
                if cands:
                    label = cands[-1] if lo == bstart else cands[0]
                    parts = [None] * len(parts)
                elif all(p is None for p in parts):
                    raise HmbLayoutError(f"'{sheet_name}': etiketsiz ve kodsuz sayısal satır ({i + 1}. satır).")
                else:
                    label = ""
            sira += 1
            if code_cols:
                if single_hesap:
                    code = tuple(str(parts[0])) if parts[0] is not None else None
                    kod = str(parts[0]) if parts[0] is not None else None
                else:
                    seq = [p or 0 for p in parts]
                    while seq and seq[-1] == 0:
                        seq.pop()
                    code = tuple(seq) if any(p is not None for p in parts) else None
                    kod = ".".join(map(str, seq)) if code is not None else None
                if code is not None:
                    entries.append((code, {j: float(v) for j, v in vals.items() if not heads[j].pct}))
            else:
                kod = None
            for j, v in vals.items():
                h = heads[j]
                pct = h.pct or birim is None
                per = col_period.get(j)
                if per is None and h.pct:
                    prev = [k for k in col_period if k < j]
                    per = col_period[max(prev)] if prev else None
                if per:
                    y, a, b = per
                    donem = _period(y, b) if b else (str(y) if y else None)
                    aralik = f"{AYLAR[a - 1]}-{AYLAR[b - 1]}" if a and b and a != b else (AYLAR[b - 1] if b and a == b else "")
                else:
                    donem, aralik = None, ""
                col_tip = col_tip_override.get(j, tip) if per else ""
                if tip == "yıllık" and per and per[2] == 12 and per[1] == 1:
                    donem = str(per[0])
                recs.append({
                    "sayfa": _clean(sheet_name),
                    "bolum": bolum or None,
                    "kalem_sira": sira,
                    "kod": kod,
                    "kalem": _clean(label),
                    "girinti": len(label) - len(label.lstrip(" ")) if label else 0,
                    "sutun": h.ad,
                    "donem": donem,
                    "donem_araligi": aralik or None,
                    "donem_tipi": col_tip,
                    "deger": v if pct else v * birim.carpan,
                    "birim": "%" if pct else "bin TL",
                })
        if not recs:
            continue
        # kontroller
        num_cols = [j for j in value_cols if not heads[j].pct]
        if entries:
            ok, bad = _hier_check(entries, num_cols)
            if bad and len(bad) > 0.05 * (ok + len(bad)):
                raise HmbLayoutError(
                    f"'{sheet_name}': kod hiyerarşisi tutmuyor ({len(bad)}/{ok + len(bad)} kontrol): {bad[:3]}"
                )
            if ok or bad:
                notes.append(f"{sheet_name}{'/' + bolum if bolum else ''}: {ok} üst kalem = alt kalemler toplamı"
                             + (f"; {len(bad)} sapma: {bad[:5]}" if bad else ""))
        # % dağılım sütunu: her satırda % = 100 x değer / ilk (toplam) satır.
        # Yayımcının kendi hesapladığı oran, değer sütununun ve toplam
        # satırının doğru okunduğunu kanıtlar.
        for pj in [j for j in value_cols if heads[j].pct and "dagilim" in fold(heads[j].ad)]:
            per = col_period.get(pj) or (col_period[max(k for k in col_period if k < pj)] if any(k < pj for k in col_period) else None)
            base = [j for j in num_cols if col_period.get(j) == per and not heads[j].toplam]
            if not base:
                continue
            vj = base[-1]
            rows_v = [(_cell(data[i], vj), _cell(data[i], pj)) for i in body if _is_num(_cell(data[i], vj)) and _is_num(_cell(data[i], pj))]
            if len(rows_v) < 3:
                continue
            ok = bad = 0
            root = None
            for v, pc in rows_v:
                if abs(pc - 100.0) <= 1e-6 and v:
                    root = v  # %100 satırı yeni bir bölümün tabanı (ör. giderler / gelirler)
                if root and abs(100.0 * v / root - pc) <= 0.06:
                    ok += 1
                else:
                    bad += 1
            if bad > len(rows_v) // 2:
                raise HmbLayoutError(f"'{sheet_name}': '% Dağılım' sütunu değerlerle tutmuyor ({bad}/{len(rows_v)} satır).")
            if bad > len(rows_v) // 20:
                notes.append(f"{sheet_name}: UYARI — yayımlanan % dağılım {bad}/{len(rows_v)} satırda değer/bölüm "
                             "toplamıyla tutmuyor (yayımcı oranı farklı tabandan hesaplamış); değerler dosyadaki gibi")
                continue
            notes.append(f"{sheet_name}: {ok} satırda % dağılım = 100 x değer / bölüm toplamı (%100 satırı) — sütun ve toplam satırı doğrulandı")
        # kodlu blokta kodsuz "... TOPLAMI" satırları = önceki toplam satırından
        # bu yana (ya da blok başından) listelenen en üst düzey kodlu satırlar
        if entries and num_cols and single_hesap:
            seq = []
            for i in body:
                lab0 = _cell(data[i], lc)
                cp = [p for p in codes_raw[i] if p is not None]
                labs = " ".join(c for c in data[i][bstart:min(value_cols)] if isinstance(c, str))
                seq.append((i, bool(cp) and isinstance(lab0, str) and lab0.strip() != "", "toplam" in fold(labs)))
            present = {c for c, _ in entries}
            top_of = {}
            for c, vals in entries:
                if not any(c[:n] in present for n in range(0 if single_hesap else 1, len(c))) or c == ():
                    top_of[c] = vals
            okt = badt = 0
            since: list[int] = []
            allrows: list[int] = []
            for i, coded, is_tot in seq:
                if coded:
                    since.append(i)
                    allrows.append(i)
                    continue
                if not is_tot:
                    continue
                j = num_cols[0]
                tv = _cell(data[i], j)
                if not _is_num(tv):
                    continue

                def topsum(rows: list[int]) -> float:
                    tot = 0.0
                    for x in rows:
                        cp = [p or 0 for p in codes_raw[x]]
                        key = tuple(str(cp[0])) if single_hesap else tuple(cp)
                        if single_hesap and key in top_of and _is_num(_cell(data[x], j)):
                            tot += _cell(data[x], j)
                    return tot

                if since and (abs(topsum(since) - tv) <= _tol(tv, len(since)) or abs(topsum(allrows) - tv) <= _tol(tv, len(allrows))):
                    okt += 1
                elif since:
                    badt += 1
                since = []
            if okt or badt:
                if badt > okt:
                    raise HmbLayoutError(f"'{sheet_name}': toplam satırları kodlu satırların toplamıyla tutmuyor ({badt}/{okt + badt}).")
                notes.append(f"{sheet_name}: {okt} toplam satırı = üstündeki kodlu satırların toplamı"
                             + (f" ({badt} tutmuyor)" if badt else ""))
        # girinti hiyerarşisi (yalnız bilgi): bir satırın hemen altındaki daha
        # içeride yazılmış satırlar toplamı = o satır
        if not entries:
            ind = [(len(_cell(data[i], lc)) - len(_cell(data[i], lc).lstrip(" ")), i) for i in body
                   if isinstance(_cell(data[i], lc), str) and _cell(data[i], lc).strip()]
            if sum(1 for g, _ in ind if g) >= 0.3 * len(ind) and num_cols:
                okc = badc = 0
                for k, (g, i) in enumerate(ind):
                    kids = []
                    child_g = None
                    for g2, i2 in ind[k + 1:]:
                        if g2 <= g:
                            break
                        child_g = g2 if child_g is None else child_g
                        if g2 == child_g:
                            kids.append(i2)
                    if len(kids) < 2:
                        continue
                    j = num_cols[0]
                    pv = _cell(data[i], j)
                    kv = [_cell(data[x], j) for x in kids if _is_num(_cell(data[x], j))]
                    if not _is_num(pv) or not kv:
                        continue
                    if abs(sum(kv) - pv) <= max(2.0, 1e-6 * abs(pv)) + 0.51 * len(kv):
                        okc += 1
                    else:
                        badc += 1
                if okc:
                    notes.append(f"{sheet_name}: girinti hiyerarşisinde {okc} üst kalem = alt kalemler toplamı"
                                 + (f" ({badc} kalemde tutmuyor; 'bunlardan' türü alt satırlar olabilir)" if badc else ""))
        tcols = [j for j in value_cols if heads[j].toplam and not heads[j].pct]
        if tcols and tip == "dönem içi":
            tj = tcols[0]
            ok = bad_n = 0
            for i in body:
                t = _cell(data[i], tj)
                if not _is_num(t):
                    continue
                s = sum(_cell(data[i], j) for j in flow_cols if j in col_period and _is_num(_cell(data[i], j)))
                if abs(s - t) <= max(2.0, 1e-6 * abs(t)) + 0.51 * len(flow_cols):
                    ok += 1
                else:
                    bad_n += 1
            if not ok or bad_n > (ok + bad_n) // 20:
                raise HmbLayoutError(f"'{sheet_name}': TOPLAM sütunu dönemler toplamına eşit değil ({bad_n} satır).")
            notes.append(f"{sheet_name}: {ok} satırda TOPLAM = dönemler toplamı (dönem içi akım doğrulandı)"
                         + (f"; {bad_n} sapma" if bad_n else ""))
        for r in recs:
            r["_birim_etiket"] = f"{unit_label} ({unit_src})" if unit_label else "yalnız %"
        out.extend(recs)
    return out


def parse_kalem_tablosu(
    content: bytes, dosya_etiketi: str = "", birim_beyani: str = ""
) -> pd.DataFrame:
    """Kalem satır x dönem (aralık / yıl / başlıktaki tek dönem) sütun
    tabloları: GFS sınıflandırma, Ekod/Fkod/kurumsal bütçe, bütçe denge,
    mali varlık-yükümlülük, iç-dış borç, şarta bağlı, nakit akış, faaliyet
    sonuçları. Kanıt kuralları için bölüm başındaki açıklamaya bakın."""
    notes: list[str] = []
    rows: list[dict[str, Any]] = []
    skipped: list[str] = []
    yil_hint = (_years_in(dosya_etiketi) or [None])[0]
    first_skip: _SayfaAtla | None = None
    for sheet_name, data in _sheets(content):
        try:
            recs = _parse_kalem_sheet(sheet_name, data, yil_hint, birim_beyani, notes)
        except _SayfaAtla as exc:
            first_skip = first_skip or exc
            skipped.append(f"{_clean(sheet_name)} ({exc})")
            continue
        if recs:
            rows.extend(recs)
        elif any(_row_texts(r) for r in data):
            skipped.append(_clean(sheet_name))
    if not rows:
        if first_skip is not None:
            raise HmbLayoutError(str(first_skip))
        raise HmbLayoutError("Kalem x dönem (aralık/yıl) düzeninde tablo bulunamadı.")
    frame = pd.DataFrame(rows)
    birimler = sorted(set(frame.pop("_birim_etiket")))
    for col in ("kod", "bolum", "donem_araligi"):
        if frame[col].isna().all():
            frame = frame.drop(columns=[col])
    tipler = sorted({t for t in frame["donem_tipi"] if t})
    frame.attrs["meta"] = {
        "birim": "bin TL (yüzde sütunları birim='%')",
        "kaynak_birim": birimler,
        "donem_tipi": ", ".join(tipler),
        "donemler": sorted({d for d in frame["donem"].dropna()}),
        "kontroller": notes,
    }
    if skipped:
        frame.attrs["meta"]["atlanan_sayfalar"] = skipped
    if "belirsiz" in tipler:
        frame.attrs["meta"]["uyari"] = "Değerlerin ay içi mi kümülatif mi olduğu dosyadan doğrulanamadı (donem_tipi='belirsiz')."
    return frame


# -- E: muhasebe tabloları: mizan ve bilanço -----------------------------------
#
# Mizan (hesap kodu, hesap adı, borç/alacak tutarı, borç/alacak kalanı):
#   * birim etiketi başlıkta; dönem başlıkta ("OCAK-EYLÜL DÖNEMİ", "ARALIK AYI
#     KESİN MİZANI"); aralık Ocak'ta başlamıyorsa ret.
#   * TOPLAM satırında borç = alacak (tutar ve kalan); 1 haneli hesapların
#     toplamı = TOPLAM; her üst hesap = alt hesapları toplamı (önek hiyerarşisi).
#   * Her hesapta borç − alacak = borç kalanı − alacak kalanı: tutarların yıl
#     başı açılışı dahil KÜMÜLATİF olduğunun kanıtı. Tutmazsa ret.
# Bilanço (iki yan blok: aktif | pasif, grup kodları bazen bir satır kaymış):
#   * grup kodu, altındaki hesapların kodundan türetilir; her grup = alt
#     hesapları, her ana grup = alt grupları, aktif toplamı = pasif toplamı.

def _tol(v: float, n: int = 1) -> float:
    return max(2.0, 1e-6 * abs(v)) + 0.51 * n


def _mizan_header(data: list[list[Any]]) -> int | None:
    """Mizan başlık satırı: aynı satırda "hesap", "borç", "alacak" ve altında
    en az 5 hesap kodlu sayısal satır (boş muhasebe formları sayılmaz)."""
    for i, row in enumerate(data[:30]):
        f = [fold(c) for c in row if isinstance(c, str)]
        if any("borc" in c for c in f) and any("alacak" in c for c in f) and any("hesap" in c for c in f):
            coded = sum(
                1 for r in data[i + 1 : i + 40]
                if any(_code_part(c) is not None for c in r[:2]) and sum(1 for c in r if _is_num(c)) >= 2
            )
            if coded >= 5:
                return i
    return None


def parse_mizan(content: bytes, dosya_etiketi: str = "", birim_beyani: str = "") -> pd.DataFrame:
    rows_out: list[dict[str, Any]] = []
    notes: list[str] = []
    birimler: set[str] = set()
    tipler: set[str] = set()
    found = False
    for sheet_name, data in _sheets(content):
        h = _mizan_header(data)
        if h is None:
            continue
        found = True
        # başlık yığını: h ve altındaki metin satırları (ilk sayısal satıra kadar)
        r0 = next((i for i in range(h + 1, len(data)) if any(_is_num(c) for c in data[i])), None)
        if r0 is None:
            raise HmbLayoutError(f"'{sheet_name}': mizan başlığı var ama veri yok.")
        ncols = max(len(r) for r in data[h:r0 + 5])
        heads = {j: fold(" ".join(str(_cell(data[i], j)) for i in range(h, r0) if isinstance(_cell(data[i], j), str))) for j in range(ncols)}
        kod_col = next((j for j, t in heads.items() if "kod" in t), None)
        ad_col = next((j for j, t in heads.items() if "ad" in t.split() or "adi" in t), None)
        cols: dict[str, int] = {}
        for j, t in heads.items():
            side = "borc" if "borc" in t else ("alacak" if "alacak" in t else None)
            kind = "tutar" if "tutar" in t else ("kalan" if ("kalan" in t or "bakiye" in t) else None)
            if side and kind:
                cols[f"{side}_{kind}"] = j
        if kod_col is None or ad_col is None or not {"borc_tutar", "alacak_tutar"} <= set(cols):
            raise HmbLayoutError(f"'{sheet_name}': mizan sütunları tanınmadı ({heads}).")
        title = " ".join(c for i in range(h) for _, c in _row_texts(data[i]))
        years = sorted(set(_years_in(title)))
        if len(years) != 1:
            raise HmbLayoutError(f"'{sheet_name}': mizan başlığında tek yıl bekleniyordu: {title[:120]!r}")
        yil = years[0]
        t_range, t_month = _title_period(title)
        if t_range:
            if t_range[0] != 1:
                raise HmbLayoutError(f"'{sheet_name}': mizan dönemi Ocak'ta başlamıyor: {t_range}")
            ay = t_range[1]
        elif t_month:
            ay = t_month
        elif re.search(r"\b([1-4])\s*\.\s*donem\b", fold(title)):
            # "2008 YILI 3. DÖNEM MİZANI": n. çeyrek sonu; mizan zaten yıl başından
            # kümülatiftir (aşağıdaki borç−alacak = kalan kontrolüyle doğrulanır)
            ay = 3 * int(re.search(r"\b([1-4])\s*\.\s*donem\b", fold(title)).group(1))
            notes.append(f"{_clean(sheet_name)}: başlıktaki '{ay // 3}. DÖNEM' = Ocak-{AYLAR[ay - 1]}")
        elif re.search(r"\b(kesin|gecici)\s+mizan", fold(title)):
            # kesin / geçici mizan tanımı gereği yıl sonu (Aralık) mizanıdır;
            # başlığında ay olanların hepsi de "ARALIK AYI KESİN/GEÇİCİ MİZANI".
            ay = 12
            notes.append(f"{_clean(sheet_name)}: başlıkta ay yok; kesin/geçici mizan = yıl sonu (Aralık)")
        else:
            raise HmbLayoutError(f"'{sheet_name}': mizan dönemi başlıkta yok: {title[:120]!r}")
        unit_label = find_unit_label([c for i in range(r0) for c in data[i]])
        src = "dosya"
        if unit_label is None:
            if not birim_beyani:
                raise HmbLayoutError(f"'{sheet_name}': mizanda birim etiketi yok.")
            unit_label, src = f"({birim_beyani})", "kullanıcı beyanı, dosyada etiket yok"
        birim = birim_carpani(unit_label, yil)
        birimler.add(f"{unit_label} ({src})")
        accounts: list[tuple[str, str, dict[str, float]]] = []
        toplam: dict[str, float] | None = None
        for row in data[r0:]:
            vals = {k: float(_cell(row, j)) for k, j in cols.items() if _is_num(_cell(row, j))}
            kod = _cell(row, kod_col)
            ad = _cell(row, ad_col)
            lab = " ".join(str(x) for x in (kod, ad) if isinstance(x, str))
            if "toplam" in fold(lab) and vals:
                toplam = vals
                continue
            code = _code_part(kod)
            if code is None:
                if vals and "nazim hesap" in fold(lab):
                    continue  # "NAZIM HESAPLAR" ara toplam satırı (hesap değil)
                if vals:
                    raise HmbLayoutError(f"'{sheet_name}': kodsuz sayısal satır: {row[:4]}")
                continue
            if not vals:
                continue
            ks = str(kod).strip() if isinstance(kod, str) else str(code)
            accounts.append((ks, _clean(ad) if isinstance(ad, str) else "", vals))
        if not accounts:
            raise HmbLayoutError(f"'{sheet_name}': mizanda hesap satırı yok.")
        checks: list[str] = []
        problems: list[str] = []
        # 1) borç = alacak
        if toplam is None:
            raise HmbLayoutError(f"'{sheet_name}': mizanda TOPLAM satırı yok (borç = alacak doğrulanamaz).")
        for a, b in (("borc_tutar", "alacak_tutar"), ("borc_kalan", "alacak_kalan")):
            if a in toplam and b in toplam:
                if abs(toplam[a] - toplam[b]) > _tol(toplam[a]):
                    problems.append(f"TOPLAM {a} {toplam[a]:,.0f} ≠ {b} {toplam[b]:,.0f}")
                else:
                    ad = {"borc_tutar": "borç tutarı", "alacak_tutar": "alacak tutarı",
                          "borc_kalan": "borç kalanı", "alacak_kalan": "alacak kalanı"}
                    checks.append(f"TOPLAM satırında {ad[a]} = {ad[b]}")
        # 2) 1 haneli hesaplar toplamı = TOPLAM
        top = [v for k, _, v in accounts if len(k) == 1]
        if top:
            for c in cols:
                s = sum(v.get(c, 0.0) for v in top)
                if c in toplam and abs(s - toplam[c]) > _tol(toplam[c], len(top)):
                    problems.append(f"1 haneli hesaplar toplamı {c} {s:,.0f} ≠ TOPLAM {toplam[c]:,.0f}")
        # 3) hiyerarşi
        ok, bad = _hier_check([(tuple(k), v) for k, _, v in accounts], list(cols))
        if bad and len(bad) > 0.05 * (ok + len(bad)):
            problems.append(f"hesap hiyerarşisi {len(bad)}/{ok + len(bad)} kontrolde tutmuyor: {bad[:3]}")
        elif ok:
            checks.append(f"{ok} üst hesap = alt hesaplar toplamı" + (f" ({len(bad)} sapma: {bad[:3]})" if bad else ""))
        # 4) kümülatif kanıtı
        idn_ok = idn_bad = 0
        if {"borc_kalan", "alacak_kalan"} <= set(cols):
            for k, _, v in accounts:
                net = v.get("borc_tutar", 0.0) - v.get("alacak_tutar", 0.0)
                kal = v.get("borc_kalan", 0.0) - v.get("alacak_kalan", 0.0)
                if abs(net - kal) <= _tol(max(abs(x) for x in v.values())):
                    idn_ok += 1
                else:
                    idn_bad += 1
            if idn_bad > 0.01 * (idn_ok + idn_bad):
                problems.append(f"{idn_bad} hesapta borç−alacak ≠ kalan farkı (tutarlar kümülatif değil?)")
            else:
                checks.append(f"{idn_ok} hesapta borç−alacak tutarı = borç−alacak kalanı (tutarlar yıl başı açılış dahil kümülatif)")
        else:
            problems.append("kalan sütunları yok; tutarların kümülatif olduğu doğrulanamaz")
        if problems:
            raise HmbLayoutError(f"'{sheet_name}': mizan doğrulanamadı: " + "; ".join(problems))
        kind = "kesin" if "kesin" in fold(title) else ("geçici" if "gecici" in fold(title) else "")
        notes.extend(f"{_clean(sheet_name)}: {c}" for c in checks)
        names = {"borc_tutar": "Borç Tutarı", "alacak_tutar": "Alacak Tutarı",
                 "borc_kalan": "Borç Kalanı", "alacak_kalan": "Alacak Kalanı"}
        for sira, (k, ad, vals) in enumerate(accounts, start=1):
            for c in cols:
                if c not in vals:
                    continue
                tip = "kümülatif (yıl başı açılış dahil)" if c.endswith("tutar") else "dönem sonu bakiye"
                tipler.add(tip)
                rows_out.append({
                    "sayfa": _clean(sheet_name), "kalem_sira": sira, "kod": k, "duzey": len(k),
                    "kalem": ad, "sutun": names[c], "donem": _period(yil, ay), "donem_tipi": tip,
                    "deger": vals[c] * birim.carpan, "birim": "bin TL",
                })
        if kind:
            notes.append(f"{_clean(sheet_name)}: {kind} mizan")
    if not found:
        raise HmbLayoutError("Mizan başlığı (hesap kodu / borç / alacak) bulunamadı.")
    frame = pd.DataFrame(rows_out)
    frame.attrs["meta"] = {
        "birim": "bin TL",
        "kaynak_birim": sorted(birimler),
        "donem_tipi": "; ".join(sorted(tipler)),
        "donemler": sorted(set(frame["donem"])),
        "kontroller": notes,
    }
    return frame


def _bilanco_block(
    data: list[list[Any]], start: int, end: int, h: int
) -> tuple[list[dict], list[dict], dict[int, int | None]]:
    """Bir bilanço yan bloğu -> (kalemler, toplam satırları, {değer sütunu: yıl}).

    Değer sütunları: başlık satırında yıl yazan sütunlar; yoksa bloğun en
    sağdaki sayısal sütunu (dönemi başlıktaki yıl). Grup satırlarının kodu
    (bazen bir satır aşağı kaymış yazılır) altındaki hesap kodlarından türetilir."""
    year_cols = {j: int(_cell(data[h], j)) for j in range(start, end) if _is_year_num(_cell(data[h], j))}
    if year_cols:
        vcols: dict[int, int | None] = dict(year_cols)
    else:
        counts = {j: sum(1 for i in range(h + 1, len(data)) if _is_num(_cell(data[i], j))) for j in range(start, end)}
        top = max(counts.values(), default=0)
        cand = [j for j, c in counts.items() if c and c >= 0.3 * top]
        if not cand:
            return [], [], {}
        vcols = {max(cand): None}
    fv = min(vcols)
    rows = []
    for i in range(h + 1, len(data)):
        vals = {j: float(_cell(data[i], j)) for j in vcols if _is_num(_cell(data[i], j))}
        if not vals or (year_cols and all(vals.get(j) == y for j, y in year_cols.items())):
            continue  # boş ya da tekrar eden yıl başlığı satırı ("NAZIM HESAPLAR 2015 2016 2017")
        pre = [_cell(data[i], j) for j in range(start, fv)]
        codes = [_code_part(c) for c in pre if _code_part(c) is not None]
        texts = [c for c in pre if isinstance(c, str) and c.strip() and _code_part(c) is None]
        rows.append({"i": i, "codes": codes, "ad": _clean(texts[0]) if texts else "", "vals": vals})
    items, totals = [], []
    for r in rows:
        if "toplam" in fold(r["ad"]):
            totals.append(r)
            continue
        leaf = next((c for c in r["codes"] if c >= 100), None)
        if leaf is not None:
            r["kod"] = str(leaf)
        items.append(r)
    # Kodsuz satır dizisi bir hesabın hemen ardından geliyor ve toplamı o
    # hesaba eşitse hesabın alt kırılımıdır ("2009 Yılı Olumlu Faaliyet
    # Sonucu" ... = 570); hiyerarşiye girmez.
    k = 0
    while k < len(items):
        r = items[k]
        if "kod" in r and len(r["kod"]) == 3:
            run = []
            m = k + 1
            while m < len(items) and "kod" not in items[m] and not items[m]["codes"]:
                run.append(items[m])
                m += 1
            after = items[m] if m < len(items) else None
            if run and not (after is not None and after.get("kod") and len(after["kod"]) == 3) and all(
                abs(sum(x["vals"].get(j, 0.0) for x in run) - v) <= _tol(v, len(run)) for j, v in r["vals"].items()
            ):
                for x in run:
                    x["kod"] = None
                    x["alt_kirilim"] = r["kod"]
            k = m if run else k + 1
        else:
            k += 1
    for k, r in enumerate(items):
        if "kod" in r:
            continue
        nxt = items[k + 1] if k + 1 < len(items) else None
        leaf = next((x["kod"] for x in items[k + 1:] if x.get("kod") and len(x["kod"]) == 3), None)
        level = 2 if (nxt is not None and nxt.get("kod") and len(nxt["kod"]) == 3) else 1
        derived = leaf[:level] if leaf else None
        explicit = [str(c) for c in r["codes"] if c < 100]
        if derived and (not explicit or any(derived.startswith(e) for e in explicit)):
            r["kod"] = derived
        elif explicit and len(explicit) == 1 and (not derived or not derived.startswith(explicit[0])) and (
            nxt is None or not nxt.get("kod") or not nxt["kod"].startswith(explicit[0])
        ) and leaf is None:
            r["kod"] = explicit[0]  # alt hesabı listelenmemiş grup (ör. nazım 90 ÖDENEK HESAPLARI)
        elif derived:
            r["kod"] = derived
        else:
            raise HmbLayoutError(f"bilanço satırı {r['i'] + 1} ({r['ad']!r}): grup kodu türetilemedi.")
    return items, totals, vcols


def parse_bilanco(content: bytes, dosya_etiketi: str = "", birim_beyani: str = "") -> pd.DataFrame:
    """Bilanço (aktif | pasif yan yana; tek yıl ya da karşılaştırmalı yıllar).

    Kanıt: birim etiketi başlıkta; dönem başlıktaki yıl ya da sütun başlığı
    yılları (başlıktaki yıl aralığıyla tutarlı); her yıl için her grup = alt
    hesapları, her ana grup = alt grupları, ana gruplar = "AKTİF/PASİF
    TOPLAMI" (nazım hesaplar dahil "GENEL TOPLAM") ve aktif = pasif."""
    rows_out: list[dict[str, Any]] = []
    notes: list[str] = []
    uyarilar: list[str] = []
    birimler: set[str] = set()
    skipped: list[str] = []
    for sheet_name, data in _sheets(content):
        h = p_col = None
        for i, row in enumerate(data[:15]):
            fa = [j for j, c in enumerate(row) if isinstance(c, str) and fold(c).startswith("aktif")]
            fp = [j for j, c in enumerate(row) if isinstance(c, str) and fold(c).startswith("pasif")]
            if fa and fp and fp[0] > fa[0]:
                h, p_col = i, fp[0]
                break
        if h is None:
            if any(_row_texts(r) for r in data):
                skipped.append(_clean(sheet_name))
            continue
        title = " ".join(c for i in range(h) for _, c in _row_texts(data[i]))
        t_years = sorted(set(_title_years(title)))
        _, t_month = _title_period(title)
        unit_label = find_unit_label([c for i in range(h + 1) for c in data[i]])
        src = "dosya"
        if unit_label is None:
            if not birim_beyani:
                raise HmbLayoutError(f"'{sheet_name}': bilançoda birim etiketi yok.")
            unit_label, src = f"({birim_beyani})", "kullanıcı beyanı, dosyada etiket yok"
        ncols = max(len(r) for r in data)
        sides = {}
        for name, (s, e) in (("Aktif (Varlıklar)", (0, p_col)), ("Pasif (Kaynaklar)", (p_col, min(ncols, 2 * p_col)))):
            items, totals, vcols = _bilanco_block(data, s, e, h)
            if not items:
                raise HmbLayoutError(f"'{sheet_name}': {name} bloğu boş.")
            sides[name] = (items, totals, vcols)
        years_a = sorted(sides["Aktif (Varlıklar)"][2].values(), key=str)
        years_p = sorted(sides["Pasif (Kaynaklar)"][2].values(), key=str)
        if years_a != years_p:
            raise HmbLayoutError(f"'{sheet_name}': aktif ve pasif sütun yılları farklı ({years_a} / {years_p}).")
        if None in years_a:
            if len(t_years) != 1:
                raise HmbLayoutError(f"'{sheet_name}': bilanço başlığında tek yıl bekleniyordu: {title[:100]!r}")
            for name in sides:
                sides[name] = (sides[name][0], sides[name][1], {j: t_years[0] for j in sides[name][2]})
            years = [t_years[0]]
        else:
            years = years_a
            if t_years and not (min(t_years) <= min(years) and max(years) <= max(t_years)):
                raise HmbLayoutError(f"'{sheet_name}': sütun yılları {years} başlıktaki yıllarla ({t_years}) uyuşmuyor.")
        if len(years) > 1 and t_month:
            raise HmbLayoutError(f"'{sheet_name}': karşılaştırmalı bilançoda ay beklenmiyor.")
        birimler.add(f"{unit_label} ({src})")
        problems, checks = [], []
        for y in years:
            main_totals, nazim_totals, unmatched = {}, {}, []
            for name, (items, totals, vcols) in sides.items():
                j = next(c for c, yy in vcols.items() if yy == y)
                by: dict[str, float] = {}
                for r in items:
                    v = r["vals"].get(j)
                    if v is None or not r.get("kod"):
                        continue
                    if r["kod"] in by and abs(by[r["kod"]] - v) > _tol(v):
                        problems.append(f"{y} {name}: {r['kod']} iki kez farklı değerle")
                    by[r["kod"]] = v
                ok, bad = _hier_check([(tuple(k), {0: v}) for k, v in by.items()], [0])
                if bad:
                    problems.append(f"{y} {name}: {len(bad)} grup alt kalemlerinin toplamına eşit değil: {bad[:3]}")
                else:
                    checks.append(f"{y} {name}: {ok} grup = alt kalemleri toplamı")
                main = sum(v for k, v in by.items() if len(k) == 1 and k != "9")
                # nazım (9) toplamı: her dalda listelenmiş en üst düzey kod
                nazim = sum(
                    v for k, v in by.items()
                    if k.startswith("9") and not any(k[:n] in by for n in range(1, len(k)))
                )
                main_totals[name], nazim_totals[name] = main, nazim
                for total in totals:
                    tv = total["vals"].get(j)
                    if tv is None:
                        continue
                    if abs(main - tv) <= _tol(tv, 10):
                        checks.append(f"{y} {name}: ana gruplar toplamı = '{total['ad']}'")
                    elif nazim and abs(main + nazim - tv) <= _tol(tv, 10):
                        checks.append(f"{y} {name}: ana gruplar + nazım hesaplar = '{total['ad']}'")
                    elif nazim and abs(nazim - tv) <= _tol(tv, 10):
                        checks.append(f"{y} {name}: nazım hesaplar toplamı = '{total['ad']}'")
                    else:
                        unmatched.append((name, total["ad"], tv, main, nazim))
            a, p = main_totals["Aktif (Varlıklar)"], main_totals["Pasif (Kaynaklar)"]
            if abs(a - p) > _tol(a, 20):
                problems.append(f"{y}: aktif toplamı {a:,.0f} ≠ pasif toplamı {p:,.0f}")
            else:
                checks.append(f"{y}: aktif toplamı = pasif toplamı ({a:,.0f})")
            na, npas = nazim_totals["Aktif (Varlıklar)"], nazim_totals["Pasif (Kaynaklar)"]
            nazim_ok = bool(na and npas and abs(na - npas) <= _tol(na, 20))
            if na and npas:  # nazım hesaplar yalnız bir yanda listelenmişse eşleştirme yapılamaz
                if nazim_ok:
                    checks.append(f"{y}: borçlu nazım = alacaklı nazım ({na:,.0f})")
                else:
                    problems.append(f"{y}: borçlu nazım {na:,.0f} ≠ alacaklı nazım {npas:,.0f}")
            for name, ad, tv, main, nazim in unmatched:
                if nazim_ok and "genel" in fold(ad):
                    uyarilar.append(
                        f"{y} {name}: dosyadaki '{ad}' ({tv:,.0f}) ana gruplar + nazım ({main + nazim:,.0f}) "
                        f"toplamından {tv - main - nazim:,.0f} farklı; ana gruplar, aktif = pasif ve borçlu = "
                        "alacaklı nazım kontrolleri tuttuğu için kalemler döndürüldü (yayımcı toplam satırı tutarsız)."
                    )
                else:
                    problems.append(f"{y} {name}: ana gruplar toplamı {main:,.0f} ≠ '{ad}' {tv:,.0f}")
        if problems:
            raise HmbLayoutError(f"'{sheet_name}': bilanço doğrulanamadı: " + "; ".join(problems[:6]))
        notes.extend(f"{_clean(sheet_name)}: {c}" for c in checks)
        for name, (items, totals, vcols) in sides.items():
            for j, y in vcols.items():
                birim = birim_carpani(unit_label, y)
                donem = _period(y, t_month) if t_month else str(y)
                for sira, r in enumerate(items + totals, start=1):
                    if j not in r["vals"]:
                        continue
                    rows_out.append({
                        "sayfa": _clean(sheet_name), "bolum": name, "kalem_sira": sira,
                        "kod": r.get("kod"), "duzey": len(r["kod"]) if r.get("kod") else None,
                        "kalem": r["ad"], "donem": donem,
                        "donem_tipi": "dönem sonu bakiye" if t_month else "yıl sonu bakiye",
                        "deger": r["vals"][j] * birim.carpan, "birim": "bin TL",
                    })
    if not rows_out:
        raise HmbLayoutError("Aktif/pasif başlıklı bilanço bulunamadı.")
    frame = pd.DataFrame(rows_out)
    frame["duzey"] = frame["duzey"].astype("Int64")
    frame.attrs["meta"] = {
        "birim": "bin TL",
        "kaynak_birim": sorted(birimler),
        "donem_tipi": ", ".join(sorted(set(frame["donem_tipi"]))),
        "donemler": sorted(set(frame["donem"])),
        "kontroller": notes,
    }
    if skipped:
        frame.attrs["meta"]["atlanan_sayfalar"] = skipped
    if uyarilar:
        frame.attrs["meta"]["uyari"] = " ".join(uyarilar)
    return frame


# -- Otomatik ----------------------------------------------------------------


_VALUE_COLS = {"deger", "deger_bin_tl", "tahakkuk_bin_tl", "tahsilat_bin_tl"}


def _assert_unique_cells(frame: pd.DataFrame) -> None:
    """Tidy tabloda aynı (satır, sütun, dönem) anahtarı iki kez geçemez. Geçiyorsa
    bir yapı öğesi (yan yana ikinci blok, sütun grubu yılı...) kaybolmuştur:
    değerler yanlış etikete yazılmış olabilir -> ret. (2026-11-12: önceki
    sürüm yan yana aktif|pasif bilançolarda pasif değerlerini aktif
    kalemlerine, çok yıllı "Hedef ve Gerçekleşmeleri" tablolarında tüm
    yılları ilk yıla yazıyordu.)"""
    keys = [c for c in frame.columns if c not in _VALUE_COLS]
    dup = frame.duplicated(subset=keys, keep=False)
    if dup.any():
        ex = frame.loc[dup, keys].head(2).to_dict("records")
        raise HmbLayoutError(
            f"{int(dup.sum())} hücre aynı satır/sütun/dönem etiketini taşıyor (ör. {ex}); "
            "tablo yapısı (yan yana blok ya da sütun grubu) tam okunamadı."
        )


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
    # A: il x kategori; B: kalem x dönem; A': sayfa başına dönem çapraz.
    # Ardından muhasebe tabloları ve genel kalem tablosu -- bunlar yalnızca
    # öncekilerin hiçbiri tutmazsa denenir.
    kaynak = "mahalli" if "mahalli" in fold(head_text) else "dosya"
    attempts: list[tuple[str, Callable[[], pd.DataFrame], str]] = []
    if yil:
        attempts.append((
            "il-çapraz",
            lambda: parse_il_crosstab(content, yil=yil, kaynak=kaynak, birim_beyani=birim_beyani),
            "il satır x kategori sütun (çapraz)",
        ))
    attempts += [
        ("geniş", lambda: parse_wide(content, yil=None, dosya_etiketi=dosya_etiketi, birim_beyani=birim_beyani),
         "kalem satır x dönem sütun (geniş)"),
        ("çapraz", lambda: parse_sheet_crosstab(content, yil=None, birim_beyani=birim_beyani),
         "sayfa başına dönem, satır x kategori (çapraz)"),
        ("mizan", lambda: parse_mizan(content, dosya_etiketi=dosya_etiketi, birim_beyani=birim_beyani),
         "mizan (hesap x borç/alacak)"),
        ("bilanço", lambda: parse_bilanco(content, dosya_etiketi=dosya_etiketi, birim_beyani=birim_beyani),
         "bilanço (aktif | pasif)"),
        ("kalem", lambda: parse_kalem_tablosu(content, dosya_etiketi=dosya_etiketi, birim_beyani=birim_beyani),
         "kalem satır x dönem aralığı/yıl sütun"),
    ]
    for ad, fn, duzen in attempts:
        try:
            frame = fn()
            _assert_unique_cells(frame)
            frame.attrs["meta"]["duzen"] = duzen
            return frame, reasons
        except HmbLayoutError as exc:
            reasons.append(f"{ad}: {exc}")
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
