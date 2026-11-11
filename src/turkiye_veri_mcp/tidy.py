"""Turn TUIK 'istab' Excel downloads into tidy long-format DataFrames.

TUIK publishes ~1,750 tables as Excel files built from house templates
rather than one canonical layout. Each sheet is read as a grid and split
into parts before anything is unpivoted:

  title / note lines   single-cell rows on top ("... , 2023", "(%)", "[15+ yaş]")
  column header rows   rows above the data that label the value columns
                       (multi-row, merged cells, TR and EN lines)
  stub columns         label columns on the left (and an EN copy on the right)
                       incl. Yıl / Ay / Çeyrek / code columns
  body rows            value rows, heading rows (a group or block title,
                       or a period such as "2020"), label fragments split
                       over two lines, repeated header rows, footnotes

Output columns: the table's stub columns (kept verbatim; '<name>_en' when a
column stacks TR and EN labels on consecutive rows), 'bolum' (heading rows
above the value, outermost first), 'donem' (period), 'donem_kaynak' (where
the period came from, only when it is not in the table grid), 'gosterge'
(the value column's header) and 'deger'; 'sayfa' for multi-sheet files.

Safety rules (never return silently wrong numbers):
  * a sheet whose labels map two different numbers to the same key is
    refused, not returned (a dimension was lost);
  * value series made of years or month numbers are refused;
  * ambiguous text numbers ('1.234') are dropped, not guessed;
  * a period is only taken from the title / sheet name / catalog name when
    exactly one is named there (a range such as '2015-2024' is not one).
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

_FOOTNOTE_MARKERS = (
    "kaynak", "source", "not:", "note:", "notlar", "notes", "(1)", "(*)", "dipnot",
    "açıklama", "explanation", "*", "(r)", "(p)", "r:", "p:", "tüik,", "turkstat,",
)

_PERIOD_PATTERNS = (
    re.compile(r"^(19|20)\d{2}$"),                      # 2020
    re.compile(r"^(19|20)\d{2}[-/](0?[1-9]|1[0-2])$"),  # 2020-01
    re.compile(r"^(19|20)\d{2}\s*[-/]\s*(19|20)\d{2}$"),  # 2019-2020
    re.compile(r"^(19|20)\d{2}\s*[QÇ][1-4]$", re.IGNORECASE),
    re.compile(r"^[QÇ][1-4]\s*(19|20)\d{2}$", re.IGNORECASE),
)

_TEXT_MARKERS = ("-", "–", "—", "..", "...", ".", ":", "*", "x", "X", "c", "C", "(x)", "(c)", "(C)")


class TidyError(RuntimeError):
    """Raised when an istab file cannot be tidied with any known template."""


# ----------------------------------------------------------------- cells


def looks_like_period(value: Any) -> bool:
    if value is None or value != value:
        return False
    if isinstance(value, bool):
        return False
    if hasattr(value, "year") and hasattr(value, "month") and not isinstance(value, str):
        return 1900 <= value.year <= 2100          # a date cell (e.g. an election day)
    if isinstance(value, (int, float)) and float(value).is_integer():
        return 1900 <= float(value) <= 2100
    text = str(value).strip()
    # Revision/footnote marks on periods: '2017(r)', '2020 (1)', '2021*', and
    # short qualifiers such as '2015 (07.06)' (two elections that year).
    text = re.sub(r"\s*(\([^)]{1,4}\)|\*+)$", "", text).strip()
    text = re.sub(r"^((?:19|20)\d{2})\s*\((\d{1,2}[./]\d{1,2})\)$", r"\1", text)
    return any(pattern.match(text) for pattern in _PERIOD_PATTERNS)


def _is_blank(value: Any) -> bool:
    return value is None or value != value or not str(value).strip()


def _is_marker(value: Any) -> bool:
    """Suppression / footnote marks standing in for a value: '-', '..', 'x', 'C', '(0)', '(*)'."""
    if not isinstance(value, str):
        return False
    text = value.strip()
    if text in _TEXT_MARKERS or re.fullmatch(r"\([^()]{0,3}\)", text) or re.fullmatch(r"[-–—.…]{2,}", text):
        return True
    # a mark followed by a footnote reference: '-(1)', '..(r)', 'x*'
    core = re.sub(r"\s*(\([^()]{1,3}\)|\*+)$", "", text)
    return core != text and core in _TEXT_MARKERS


def _is_footnote_row(row: pd.Series) -> bool:
    values = [str(v).strip().casefold() for v in row.tolist() if not _is_blank(v)]
    if len(values) > 2:
        return False
    return any(v.startswith(_FOOTNOTE_MARKERS) for v in values)


_SOURCE_LINE = re.compile(
    r"^\s*(kaynak|source|not\s*:|note\s*:|notlar|notes|dipnot|tüik\s*,|turkstat\s*,|tuik\s*,|"
    r"\(\*+\)|\*\s|\(\d{1,2}\)\s|\d{1,2}\)\s|[-–.:x]{1,3}\s+(bilgi|denotes|veri|data|gizli|confidential|sıfır|zero|"
    r"rakam|figure|yok|not available|n/a))",
    re.IGNORECASE,
)


def _texty(value: Any) -> bool:
    """A cell that can be a label: has a letter, or is a period/number ('2020', '15-19')."""
    if _is_blank(value):
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return True
    return any(ch.isalnum() for ch in str(value))


def _is_footnote_text(value: Any) -> bool:
    """'Kaynak: ...', 'TÜİK, ...', '(1) ...', '(*) ...' lines under a table."""
    return isinstance(value, str) and bool(_SOURCE_LINE.match(value))


def _numeric_value(value: Any) -> float | None:
    """Return the number a cell holds, if any (handles TR formatting)."""
    cleaned = _clean_number(value)
    if cleaned is None:
        return None
    try:
        return float(cleaned)
    except (TypeError, ValueError):
        return None


def _code_like(value: Any) -> bool:
    """Text codes that parse as numbers: '05', '05.1', '10.1.2' (NACE/CPA)."""
    if not isinstance(value, str):
        return False
    text = value.strip()
    return bool(re.fullmatch(r"0\d+(\.\d+)*|\d{1,2}(\.\d{1,2}){2,3}", text))


def _clean_number(value: Any) -> Any:
    """TUIK cells carry thousands separators, footnote marks and dashes."""
    if value is None or value != value:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    if text in ("-", "–", "—", "..", ".", ":", "*"):
        return None
    # footnote marks such as '(1)', '(r)', '(*)' -- but never a parenthetical with
    # words ('2006 (Ekim-Kasım)' is a header, not the number 2006)
    text = re.sub(r"\([^()]{0,4}\)", "", text)
    text = text.strip()
    # A genuine number never contains a letter -- reject free-text labels
    # (e.g. "15 ve daha yukarı yaştaki nüfus... Population 15 years...")
    # BEFORE stripping non-digit characters, or the stray digits embedded
    # in the sentence would concatenate into a fake number (e.g. "1515").
    if any(ch.isalpha() for ch in text):
        return None
    text = text.replace("\xa0", "").replace(" ", "").replace("−", "-")
    text = text.rstrip("*%")                      # footnote star, percent sign
    # Only a plain signed number with ',' / '.' separators is accepted;
    # anything else ('1/2', '15-19', '3:4') is a label, not a value --
    # stripping its punctuation would invent a number.
    if not re.fullmatch(r"[-+]?\d[\d.,]*", text):
        return None
    sign = "-" if text.startswith("-") else ""
    digits = text.lstrip("+-")
    if "," in digits and "." in digits:
        if digits.rfind(",") > digits.rfind("."):   # 1.234,5 (TR)
            digits = digits.replace(".", "").replace(",", ".")
        else:                                        # 1,234.5 (EN)
            digits = digits.replace(",", "")
    elif digits.count(".") > 1:                      # 1.234.567 (TR thousands)
        if not re.fullmatch(r"\d{1,3}(\.\d{3})+", digits):
            return None
        digits = digits.replace(".", "")
    elif digits.count(",") > 1:                      # 1,234,567 (EN thousands)
        if not re.fullmatch(r"\d{1,3}(,\d{3})+", digits):
            return None
        digits = digits.replace(",", "")
    elif re.fullmatch(r"[1-9]\d{0,2}[.,]\d{3}", digits):
        # '1.234' / '1,234' in a text cell: thousands or decimal depending
        # on the table's language -- refuse to guess.
        return None
    elif "," in digits:                              # 12,5 (TR decimal)
        digits = digits.replace(",", ".")
    return sign + digits


def _fold(text: Any) -> str:
    from turkiye_veri_mcp.portal import fold_tr

    return fold_tr(str(text))


def _header_words(name: str) -> set[str]:
    return set(_fold(name).split())


def _as_int(value: Any) -> int | None:
    number = _numeric_value(value)
    if number is None or not float(number).is_integer():
        return None
    return int(number)


_PERIOD_HEADER_WORDS = {
    "yil", "yillar", "year", "years", "ay", "aylar", "month", "months",
    "ceyrek", "quarter", "donem", "period", "tarih", "date", "yillik", "aylik",
}
_CODE_HEADER_WORDS = {"kod", "kodu", "code", "codes", "sira", "rank"}
_DIMENSION_HEADER_WORDS = {"yas", "age", "grubu", "group", "grup"}


def roles_period(name: str, column: pd.Series) -> bool:
    """True if a stub column holds periods (years, months, quarters)."""
    values = [v for v in column.tolist() if not _is_blank(v)]
    if not values:
        return False
    if all(looks_like_period(v) for v in values):
        return True
    if _header_words(name) & _PERIOD_HEADER_WORDS:
        ints = [_as_int(v) for v in values]
        if all(i is not None and 1 <= i <= 12 for i in ints):
            return True
        texts = [v for v in values if isinstance(v, str) and _numeric_value(v) is None]
        return len(texts) == len(values)
    return False


def _column_role(name: str, column: pd.Series) -> str:
    """'id' (label/stub column), 'value' (observations) or 'empty'.

    id: mostly text; or every value a year; or a period-named header
    (Yıl/Ay/Çeyrek/Dönem...) whose values are month/quarter numbers or
    text; or a code-named header (Kod/Code/Sıra) with integer values.
    Otherwise numbers make it a value column. Suppression markers ('-',
    '..', 'x', 'C') count as neither text nor numbers.
    """
    values = [v for v in column.tolist() if not _is_blank(v)]
    if not values:
        return "empty"
    if roles_period(name, column):
        return "id"
    words = _header_words(name)
    if words & _CODE_HEADER_WORDS and all(_as_int(v) is not None or isinstance(v, str) for v in values):
        return "id"
    numeric = sum(1 for v in values if _numeric_value(v) is not None and not _code_like(v))
    markers = sum(1 for v in values if _is_marker(v))
    text = len(values) - numeric - markers
    if numeric == 0 or text > 0.2 * len(values):
        return "id"
    return "value"


# ----------------------------------------------------------------- language


_TR_CHARS = set("çğıöşüÇĞİÖŞÜâîû")
_EN_WORDS = {
    "the", "of", "and", "or", "in", "by", "for", "to", "with", "without", "from", "at", "on", "as",
    "other", "others", "total", "all", "male", "males", "female", "females", "men", "women",
    "level", "education", "number", "rate", "year", "years", "age", "persons", "person",
    "population", "employed", "unemployed", "people", "household", "households", "income",
    "urban", "rural", "region", "regions", "province", "provinces", "type", "types", "group",
    "groups", "school", "primary", "secondary", "higher", "high", "university", "equivalent",
    "doctoral", "master", "bachelor", "short", "cycle", "tertiary", "lower", "upper", "below",
    "above", "under", "over", "less", "than", "more", "not", "no", "yes", "unknown", "average",
    "share", "value", "volume", "index", "price", "prices", "change", "changes", "public",
    "private", "sector", "sectors", "services", "service", "industry", "agriculture",
    "construction", "trade", "transport", "manufacturing", "mining", "goods", "products",
    "country", "countries", "world", "rest", "imports", "exports", "import", "export",
    "married", "single", "divorced", "widowed", "never", "illiterate", "literate", "graduate",
    "health", "children", "child", "family", "families", "work", "workers", "employees",
    "employment", "time", "full", "part", "headcount", "researchers", "technicians",
    "support", "staff", "business", "enterprise", "enterprises", "government", "non",
    "profit", "institutions", "institution", "january", "february", "march", "april", "may",
    "june", "july", "august", "september", "october", "november", "december", "quarter",
}
_TR_WORDS = {
    "toplam", "ve", "veya", "ile", "icin", "diger", "erkek", "kadin", "yil", "yillar", "ay",
    "kent", "kir", "bolge", "il", "iller", "nufus", "sayi", "sayisi", "oran", "orani", "endeks",
    "yas", "grubu", "egitim", "durumu", "hanehalki", "fert", "gelir", "lise", "lisans", "doktora",
    "yuksek", "ilkokul", "ortaokul", "okul", "bilinmeyen", "hayir", "evet", "ortalama", "deger",
    "degisim", "fiyat", "ticaret", "sanayi", "tarim", "hizmet", "hizmetler", "insaat", "ulastirma",
    "ocak", "subat", "mart", "nisan", "mayis", "haziran", "temmuz", "agustos", "eylul", "ekim",
    "kasim", "aralik", "ceyrek", "kamu", "ozel", "sektor", "ulke", "ithalat", "ihracat", "evli",
    "bekar", "bosandi", "esi", "oldu", "okuma", "yazma", "bilmeyen", "bilen", "mezun", "turkiye",
}
_EN_SUFFIX = re.compile(r"(tion|tions|sion|ing|ness|ment|ments|ity|ities|ture|ous|ive|ally|ship|ers)$")
_TR_SUFFIX = re.compile(r"(lar|ler|lari|leri|lik|lık|luk|lük|siz|sız|sini|sına|nin|nın|dir|dır|ndaki|ndeki|deki|daki)$")


def _lang(text: Any) -> str:
    """'tr', 'en' or '?' -- a light guess used only with structural evidence."""
    if _is_blank(text):
        return "?"
    raw = str(text)
    if any(ch in _TR_CHARS for ch in raw):
        return "tr"
    words = re.findall(r"[a-z]+", raw.lower())
    if not words:
        return "?"
    en = sum(1 for w in words if w in _EN_WORDS or (len(w) > 4 and _EN_SUFFIX.search(w)))
    tr = sum(1 for w in words if w in _TR_WORDS or (len(w) > 4 and _TR_SUFFIX.search(w)))
    if en > tr:
        return "en"
    if tr > en:
        return "tr"
    return "?"


def _is_bilingual_cell(text: Any) -> bool:
    """'Erkek - Male', 'Toplam\\nTotal', 'Kent (Urban)': both languages in one cell."""
    raw = str(text)
    parts = [p for p in re.split(r"\n| - | -|- |/|\(|(?<=[^\W\d_])-(?=[A-ZÇĞİÖŞÜ])", raw) if p.strip()]
    if len(parts) < 2:
        return False
    langs = {_lang(p) for p in parts}
    return "tr" in langs and "en" in langs


# ----------------------------------------------------------------- periods


_MONTHS = {
    "ocak": 1, "subat": 2, "mart": 3, "nisan": 4, "mayis": 5, "haziran": 6, "temmuz": 7,
    "agustos": 8, "eylul": 9, "ekim": 10, "kasim": 11, "aralik": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4}


def _month_of(value: Any) -> int | None:
    if _is_blank(value):
        return None
    number = _as_int(value)
    if number is not None:
        return number if 1 <= number <= 12 else None
    words = _fold(value).split()
    found = {_MONTHS[w] for w in words if w in _MONTHS}
    return found.pop() if len(found) == 1 else None


def _quarter_of(value: Any) -> int | None:
    if _is_blank(value):
        return None
    text = _fold(value)
    m = re.fullmatch(r"(?:q|c)?\s*([1-4])(?:\s*(?:ceyrek|quarter|q|c))?", text)
    if m:
        return int(m.group(1))
    m = re.match(r"^(i{1,3}|iv)\s+(?:ceyrek|quarter)", text) or re.fullmatch(r"(i{1,3}|iv)", text)
    if m:
        return _ROMAN[m.group(1)]
    m = re.match(r"^([1-4])\s*(?:st|nd|rd|th)?\s*(?:ceyrek|quarter)", text)
    if m:
        return int(m.group(1))
    return None


def _year_of(value: Any) -> int | None:
    if _is_blank(value):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value) if float(value).is_integer() and 1900 <= value <= 2100 else None
    text = re.sub(r"\s*(\([^)]{1,4}\)|\*+)$", "", str(value).strip())
    return int(text) if re.fullmatch(r"(19|20)\d{2}", text) else None


_BASE_BEFORE = re.compile(r"(cpa|nace|isic|esa|sna|coicop|rev|ver|kanun|law|sayılı)\W{0,3}$", re.IGNORECASE)
_BASE_AFTER = re.compile(
    r"^\s*(=\s*100|\)?\s*(yılı\s+)?(sabit\s+)?(fiyat|price|bazlı|baz\b|base|referans|reference|=)|"
    r"\s*tarihli|\s*sayılı)",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"(?<![\d.,/])((?:19|20)\d{2})(?!\d|[.,]\d)")
_MONTH_NAMES = "|".join(sorted(_MONTHS, key=len, reverse=True))


def periods_in_text(text: str) -> tuple[list[str], bool]:
    """Periods named in a title-like text -> (distinct periods, has_range).

    Skips base/reference years ('2003=100', '2009 fiyatlarıyla', 'CPA 2008').
    A span such as '2015-2024' sets has_range: the text then does not name
    one period. 'Ekim-Aralık 2019' is kept as '2019 Ekim-Aralık' (not December).
    """
    if _is_blank(text):
        return [], False
    raw = " ".join(str(text).split())
    folded = _fold(raw)
    found: list[str] = []
    has_range = False
    consumed: list[tuple[int, int]] = []

    def free(span: tuple[int, int]) -> bool:
        return not any(a <= span[0] < b or a < span[1] <= b for a, b in consumed)

    # dates: 31.12.2023, 1 Ocak 2024
    for m in re.finditer(r"(\d{1,2})[./](\d{1,2})[./]((?:19|20)\d{2})", raw):
        found.append(f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}")
        consumed.append(m.span())
    # school / season years: 2013/'14, 2013/2014, 2013/14
    for m in re.finditer(r"((?:19|20)\d{2})\s*/\s*'?((?:19|20)?\d{2})(?!\d)", raw):
        if not free(m.span()):
            continue
        y1, y2 = int(m.group(1)), int(m.group(2)[-2:])
        if (y1 + 1) % 100 == y2:
            found.append(f"{y1}/{y1 + 1}")
        else:
            has_range = True
        consumed.append(m.span())
    for m in re.finditer(r"((?:19|20)\d{2})\s*[-–—]\s*((?:19|20)\d{2})", raw):
        if free(m.span()):
            has_range = True
            consumed.append(m.span())
    for m in _YEAR_RE.finditer(raw):
        if not free(m.span()):
            continue
        if _BASE_BEFORE.search(raw[max(0, m.start() - 12): m.start()]) or _BASE_AFTER.match(raw[m.end(): m.end() + 30]):
            continue
        if re.search(r"\(\s*$", raw[: m.start()]) and re.match(r"\s*=", raw[m.end():]):
            continue
        year = m.group(1)
        before = _fold(raw[max(0, m.start() - 40): m.start()])
        after = _fold(raw[m.end(): m.end() + 40])
        # month ranges 'Ekim-Aralık 2019' / '2019 Ekim-Aralık'
        rng = re.search(rf"\b({_MONTH_NAMES})\s+({_MONTH_NAMES})\s*$", before) or re.match(
            rf"^\s*({_MONTH_NAMES})\s+({_MONTH_NAMES})\b", after
        )
        if rng and rng.group(1) != rng.group(2):
            found.append(f"{year} {rng.group(1).capitalize()}-{rng.group(2).capitalize()}")
            continue
        day_month = re.search(rf"\b(\d{{1,2}})\s+({_MONTH_NAMES})\s*$", before)
        if day_month:
            found.append(f"{year}-{_MONTHS[day_month.group(2)]:02d}-{int(day_month.group(1)):02d}")
            continue
        month = None
        m_after = re.match(rf"^\s*({_MONTH_NAMES})\b", after)
        m_before = re.search(rf"\b({_MONTH_NAMES})\s*$", before)
        if m_after or m_before:
            month = _MONTHS[(m_after or m_before).group(1)]
        quarter = None
        q = re.match(r"^\s*(yili\s+|year\s+)?(i{1,3}|iv|[1-4])\s*(\.)?\s*(ceyrek|quarter|c\b|q\b)", after) or re.search(
            r"\b(i{1,3}|iv|[1-4])\s*\.?\s*(ceyrek|quarter)\s*$", before
        ) or re.match(r"^\s*q([1-4])\b", after)
        if q:
            token = next(g for g in q.groups() if g and g.strip() not in ("yili", "year", ".") and re.fullmatch(r"i{1,3}|iv|[1-4]", g.strip()))
            quarter = _ROMAN.get(token.strip(), None) or int(token)
        if month:
            found.append(f"{year}-{month:02d}")
        elif quarter:
            found.append(f"{year}-Q{quarter}")
        else:
            found.append(year)
    distinct = list(dict.fromkeys(found))
    # a bare year that is also the year of a more precise period is the same period
    precise_years = {p[:4] for p in distinct if len(p) > 4}
    distinct = [p for p in distinct if not (len(p) == 4 and p in precise_years)]
    return distinct, has_range


def _header_period(text: str) -> str | None:
    """A header part naming a period: '2020', '2017(r)', '2006 (Ekim-Kasım)', '2012-2013'."""
    if looks_like_period(text):
        return text
    flat = " ".join(str(text).split())
    if re.fullmatch(r"(19|20)\d{2}(\s*[-/]\s*((19|20)?\d{2}))?(\s*\([^()]{3,60}\))+", flat):
        return flat
    return None


def _period_cell(value: Any) -> str | None:
    """A body heading cell that names a period: '2020', '2019 (r)', '31.03.2019 ...'."""
    if _is_blank(value):
        return None
    if looks_like_period(value):
        year = _year_of(value)
        return str(year) if year is not None else str(value).strip()
    text = " ".join(str(value).split())
    m = re.match(r"^(\d{1,2})[./](\d{1,2})[./]((?:19|20)\d{2})\b", text)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    m = re.fullmatch(r"((?:19|20)\d{2})\s*/\s*'?((?:19|20)?\d{2})(\s*\([^)]{1,4}\)|\*+)?", text)
    if m and (int(m.group(1)) + 1) % 100 == int(m.group(2)[-2:]):
        return f"{m.group(1)}/{int(m.group(1)) + 1}"
    # a period followed by words: "2013/ '14 Öğretim yılı sonu - End of the 2013/'14 ..."
    m = re.match(r"^((?:19|20)\d{2})(\s*/\s*'?((?:19|20)?\d{2}))?\s+[^\d\s]", text)
    if m:
        found, has_range = periods_in_text(text)
        if not has_range and len(found) == 1 and found[0].startswith(m.group(1)):
            return found[0]
    return None


# ----------------------------------------------------------------- workbook


@dataclass
class SheetData:
    name: str
    frame: pd.DataFrame
    merges: list[tuple[int, int, int, int]] = field(default_factory=list)  # r0, r1(excl), c0, c1(excl)
    indent: dict[tuple[int, int], int] = field(default_factory=dict)
    centered: set[tuple[int, int]] = field(default_factory=set)


def _read_formatting(content: bytes, kind: str) -> dict[str, tuple[list, dict, set]]:
    """Merged ranges, indent levels and centred cells per sheet (best effort)."""
    out: dict[str, tuple[list, dict, set]] = {}
    try:
        if kind == "xls":
            import xlrd

            book = xlrd.open_workbook(file_contents=content, formatting_info=True)
            for sh in book.sheets():
                indent: dict[tuple[int, int], int] = {}
                centered: set[tuple[int, int]] = set()
                for r in range(sh.nrows):
                    for c in range(sh.ncols):
                        cell = sh.cell(r, c)
                        if cell.ctype != 1:
                            continue
                        xf = book.xf_list[cell.xf_index]
                        if xf.alignment.indent_level:
                            indent[(r, c)] = int(xf.alignment.indent_level)
                        if xf.alignment.hor_align in (2, 6):
                            centered.add((r, c))
                out[sh.name] = (list(sh.merged_cells), indent, centered)
        else:
            import openpyxl

            wb = openpyxl.load_workbook(io.BytesIO(content))
            for ws in wb.worksheets:
                indent = {}
                centered = set()
                for row in ws.iter_rows():
                    for cell in row:
                        if not isinstance(cell.value, str):
                            continue
                        al = cell.alignment
                        if al is not None and al.indent:
                            indent[(cell.row - 1, cell.column - 1)] = int(al.indent)
                        if al is not None and al.horizontal in ("center", "centerContinuous"):
                            centered.add((cell.row - 1, cell.column - 1))
                merges = [
                    (m.min_row - 1, m.max_row, m.min_col - 1, m.max_col)
                    for m in ws.merged_cells.ranges
                ]
                out[ws.title] = (merges, indent, centered)
    except Exception:  # noqa: BLE001 -- formatting is a hint, values come from pandas
        return {}
    return out


def _file_kind(content: bytes) -> str:
    head = content[:8]
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.lstrip()[:1] == b"<":
        return "html"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return "xls"
    if head.startswith(b"PK"):
        try:
            import zipfile

            names = zipfile.ZipFile(io.BytesIO(content)).namelist()
        except Exception:  # noqa: BLE001
            return "unknown"
        if any(n.startswith("xl/") for n in names):
            return "xlsx"
        if any(n.startswith("word/") for n in names):
            return "docx"
        return "zip"
    return "unknown"


# ----------------------------------------------------------------- sheet model


@dataclass
class _Item:
    """One body row after classification."""

    kind: str               # value | heading | period | blank | header | foot
    row: int
    text: str = ""
    indent: int = 0
    block: bool = False


def _cell_text(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if hasattr(value, "year") and hasattr(value, "month") and not isinstance(value, str):
        text = value.isoformat()
        return text[:10] if text.endswith("T00:00:00") or len(text) == 10 else text
    return str(value).strip()


def _leading_spaces(value: Any) -> int:
    if not isinstance(value, str):
        return 0
    return len(value) - len(value.lstrip(" 　\xa0\t"))


def _is_unit_note(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if len(text) > 60:
        return False
    if re.fullmatch(r"[\(\[].*[\)\]]", text):
        return True
    return bool(re.fullmatch(r"(?i)(%|‰|tl|bin|milyon|milyar|adet|kişi|kg|ton|ha|dekar|usd|\$|€|"
                             r"bin tl|milyon tl|bin kişi|thousand|million|billion)[\s\w.,/()-]{0,25}", text))


class _Sheet:
    """Positions of title lines, header rows, stub columns and body rows."""

    def __init__(self, data: SheetData) -> None:
        self.data = data
        frame = data.frame
        self.f = frame
        self.nr, self.nc = frame.shape
        self.vals = frame.values.tolist()
        self.merge_anchor: dict[tuple[int, int], tuple[int, int]] = {}
        self.merge_span: dict[tuple[int, int], tuple[int, int, int, int]] = {}
        for (r0, r1, c0, c1) in data.merges:
            if r0 >= self.nr or c0 >= self.nc:
                continue
            self.merge_span[(r0, c0)] = (r0, min(r1, self.nr), c0, min(c1, self.nc))
            for r in range(r0, min(r1, self.nr)):
                for c in range(c0, min(c1, self.nc)):
                    if (r, c) != (r0, c0):
                        self.merge_anchor[(r, c)] = (r0, c0)

    # cell helpers -------------------------------------------------------
    def v(self, r: int, c: int) -> Any:
        return self.vals[r][c]

    def filled(self, r: int, c: int) -> Any:
        """Value with merged ranges filled from their anchor."""
        value = self.vals[r][c]
        if _is_blank(value) and (r, c) in self.merge_anchor:
            ar, ac = self.merge_anchor[(r, c)]
            return self.vals[ar][ac]
        return value

    def anchors(self, r: int) -> list[int]:
        return [c for c in range(self.nc) if not _is_blank(self.vals[r][c])]

    def is_num(self, value: Any) -> bool:
        return (
            not _is_blank(value)
            and _numeric_value(value) is not None
            and not _code_like(value)
        )

    def is_obs(self, value: Any) -> bool:
        return self.is_num(value) and not looks_like_period(value)

    def indent_of(self, r: int, c: int) -> int:
        """Indent level: Excel's indent setting plus leading spaces (about 3 per level)."""
        return self.data.indent.get((r, c), 0) + _leading_spaces(self.vals[r][c]) // 3

    def wide(self, r: int, c: int) -> bool:
        span = self.merge_span.get((r, c))
        return bool(span and span[3] - span[2] >= 2)


def _data_rows(sh: _Sheet, from_col: int = 0) -> list[int]:
    """Rows that hold observations: >=1 number with (almost) no text to its right.

    from_col: ignore the stub area left of this column (numeric codes such
    as GTİP '2751122000' or 'İl kodu' are labels, not observations).
    """
    rows = []
    for r in range(sh.nr):
        cells = [c for c in sh.anchors(r) if c >= from_col]
        obs = [c for c in cells if sh.is_obs(sh.v(r, c))]
        if not obs:
            continue
        first_cell = sh.anchors(r)[0]
        if _is_footnote_text(sh.v(r, first_cell)):
            continue
        first = obs[0]
        right_text = [
            c for c in cells
            if c > first and not sh.is_num(sh.v(r, c)) and not _is_marker(sh.v(r, c))
        ]
        # a row of headers ('2020', 'B', '05.1', 'Toplam') is not data
        if len(right_text) <= max(1, len(obs) // 2):
            rows.append(r)
    return rows


def _header_like_after(sh: _Sheet, r: int, rows: set[int]) -> bool:
    """Is data-looking row r really a header (codes like '10', '10.1') because a
    row of text labels follows it in the same columns before any data row?"""
    obs = [c for c in sh.anchors(r) if sh.is_obs(sh.v(r, c))]
    if not obs:
        return False
    # label text among the value cells ('Toplam - Total | 1 | 2 | 3') while the next
    # data row has numbers in those columns: a header row of small numbers
    nxt = next((x for x in sorted(rows) if x > r), None)
    if nxt is not None:
        def is_text(rr: int, c: int) -> bool:
            v = sh.v(rr, c)
            return _texty(v) and not sh.is_num(v) and not _is_marker(v)

        label_cols_next = [c for c in sh.anchors(nxt) if is_text(nxt, c)]
        last_label = max(label_cols_next, default=-1)
        texts_over_numbers = [
            c for c in sh.anchors(r) if c > last_label and is_text(r, c) and sh.is_num(sh.v(nxt, c))
        ]
        if texts_over_numbers and all(_is_blank(sh.v(r, c)) for c in label_cols_next):
            return True
    checked = 0
    for nxt in range(r + 1, min(sh.nr, r + 6)):
        if nxt in rows:
            return False
        cells = sh.anchors(nxt)
        if not cells:
            continue
        checked += 1
        texts = [
            c for c in obs
            if not _is_blank(sh.v(nxt, c)) and not sh.is_num(sh.v(nxt, c)) and not _is_marker(sh.v(nxt, c))
        ]
        if len(texts) >= max(2, len(obs) // 2):
            return True
        if checked >= 3:
            return False
    return False


def _first_data_row(sh: _Sheet, rows: list[int]) -> int:
    row_set = set(rows)
    for r in rows:
        if sum(1 for c in sh.anchors(r) if sh.is_obs(sh.v(r, c))) >= 2:
            if _header_like_after(sh, r, row_set):
                row_set.discard(r)
                continue
            return r
    return min(row_set) if row_set else rows[0]


@dataclass
class _Parsed:
    long: pd.DataFrame
    note: str
    period_source: str | None
    assumptions: list[str]
    skipped: list[str]
    title_lines: list[str]
    debug: dict[str, Any]


def _header_parts(sh: _Sheet, rows: list[int], first_value_col: int) -> dict[int, list[tuple[int, str]]]:
    """Header text per column as (row, text) pairs, top to bottom.

    Merged ranges are filled from their anchor. A header that uses no merged
    cells at all ('centre across selection' layouts) is forward-filled to
    the right inside the value area, as Excel displays it.
    """
    result: dict[int, list[tuple[int, str]]] = {c: [] for c in range(sh.nc)}
    merged = any(
        (r, c) in sh.merge_anchor or (r, c) in sh.merge_span for r in rows for c in range(sh.nc)
    )
    def shape(c: int, below: list[int]) -> tuple[bool, ...]:
        return tuple(_is_blank(sh.filled(h, c)) for h in below)

    for i, r in enumerate(rows):
        last = None
        last_col = None
        below = rows[i + 1:]
        for c in range(sh.nc):
            value = sh.filled(r, c)
            if _is_blank(value):
                # 'centre across selection' group label: fill only when this column
                # has the same sub-header structure below as the label's own column
                rows_above_c = {h for h, _ in result[c]}
                rows_above_last = {h for h, _ in result[last_col]} if last_col is not None else set()
                if (
                    not merged and c > first_value_col and last is not None and below
                    and shape(c, below) == shape(last_col, below) and not all(shape(c, below))
                    and rows_above_c == rows_above_last
                ):
                    value = last
                else:
                    last = None
                    continue
            elif c >= first_value_col:
                last, last_col = value, c
            text = _cell_text(value)
            if not result[c] or result[c][-1][1] != text:
                result[c].append((r, text))
    return result


# TR/EN separators inside one cell: newline, ' - ', '/', or a dash glued to a
# capitalised word ('Öğrenim durumu-Educational status')
_SEP = re.compile(r"\n| - | -|- | / |/|(?<=[^\W\d_])-(?=[A-ZÇĞİÖŞÜ])")


def _single_language(text: str) -> bool:
    return not _SEP.search(text.strip())


def _continues(prev: str, nxt: str) -> bool:
    """Is `nxt` the continuation of a label wrapped from line `prev`?"""
    prev_s, nxt_s = prev.rstrip(), nxt.lstrip()
    if not prev_s or not nxt_s:
        return False
    if prev_s.endswith(("-", ",", "(", "/", "&")) or re.search(
        r"\b(ve|veya|ile|için|and|or|of|the|by|for|in)$", prev_s, re.IGNORECASE
    ):
        return True
    if nxt_s[0].islower() or nxt_s[0] in "-–)":
        return True
    return prev_s.count("(") > prev_s.count(")")


def _tr_then_en(first: str, second: str) -> bool:
    """`first` a Turkish-only line and `second` its English-only translation line."""
    return (
        _single_language(first) and _single_language(second)
        and _lang(first) == "tr" and _lang(second) == "en"
    )


def _sub_period(value: Any) -> tuple[str, int | None, int | None] | None:
    """Classify a stub label: ('Y', year, None), ('YM', year, month), ('Q', None, q), ('M', None, m)."""
    if _is_blank(value):
        return None
    year = _year_of(value)
    if year is not None:
        return ("Y", year, None)
    text = " ".join(str(value).split())
    m = re.fullmatch(r"((?:19|20)\d{2})\s*[-/ ]\s*(0?[1-9]|1[0-2])", text)
    if m:
        return ("YM", int(m.group(1)), int(m.group(2)))
    folded = _fold(text)
    if re.search(r"ceyrek|quarter|^q[1-4]$|^[1-4]\s*c$", folded):
        q = _quarter_of(text)
        if q:
            return ("Q", None, q)
    if re.fullmatch(r"0?[1-9]|1[0-2]", text) or any(w in _MONTHS for w in folded.split()):
        month = _month_of(text)
        if month:
            return ("M", None, month)
    return None


def _periods_from_stub(labels: list[Any]) -> list[str] | None:
    """Year rows + quarter/month rows in one stub column -> a period per row.

    '1.çeyrek .. 4.çeyrek, 1999' (the year closes the block) or
    '2004 01, 02, .. 12, 2005 01, ..' (the year opens it). Returns None
    unless every row fits one of the two shapes exactly.
    """
    kinds = [_sub_period(v) for v in labels]
    if any(k is None for k in kinds):
        return None
    tags = {k[0] for k in kinds}
    subs = tags & {"Q", "M"}
    if len(subs) != 1 or not (tags & {"Y", "YM"}):
        return None
    sub = subs.pop()
    limit = 4 if sub == "Q" else 12
    out: list[str | None] = [None] * len(kinds)

    def fmt(year: int, n: int) -> str:
        return f"{year}-Q{n}" if sub == "Q" else f"{year}-{n:02d}"

    if kinds[0][0] in ("Y", "YM"):              # leading year
        year = None
        prev = 0
        for i, (tag, y, n) in enumerate(kinds):
            if tag == "Y":
                year, prev = y, 0
                out[i] = str(y)
            elif tag == "YM":
                if sub != "M":
                    return None
                year, prev = y, n
                out[i] = fmt(y, n)
            else:
                if year is None or n <= prev or n > limit:
                    return None
                prev = n
                out[i] = fmt(year, n)
    else:                                       # trailing year closes each block
        block: list[int] = []
        for i, (tag, y, n) in enumerate(kinds):
            if tag == "YM":
                return None
            if tag == "Y":
                if not block:
                    return None
                ns = [kinds[j][2] for j in block]
                if ns != sorted(set(ns)) or max(ns) > limit:
                    return None
                for j in block:
                    out[j] = fmt(y, kinds[j][2])
                out[i] = str(y)
                block = []
            else:
                block.append(i)
        if block:
            return None
    return out if all(o is not None for o in out) else None


def _tidy_sheet_data(data: SheetData, meta: dict[str, Any] | None = None, trace: bool = False) -> _Parsed:
    sh = _Sheet(data)
    if sh.nr == 0 or all(_is_blank(v) for row in sh.vals for v in row):
        raise TidyError("sheet is empty")
    assumptions: list[str] = []
    skipped: list[str] = []

    data_rows = _data_rows(sh)
    if not data_rows:
        raise TidyError("no numeric observations in the sheet")
    data_start = _first_data_row(sh, data_rows)
    data_rows = [r for r in data_rows if r >= data_start]

    # code columns: integers with a text label right next to them on almost every
    # row ('1 | Kanun yapıcılar', '112 | Üst düzey devlet memurları', 'İl kodu | İl')
    code_cols: set[int] = set()
    obs_share = {
        c: sum(1 for r in data_rows[:300] if sh.is_obs(sh.v(r, c))) / max(1, len(data_rows[:300]))
        for c in range(sh.nc)
    }
    for c in range(sh.nc - 1):
        if any(obs_share[x] >= 0.3 for x in range(c) if x not in code_cols):
            break          # codes sit in the stub area, left of every data column
        cells = [(sh.v(r, c), sh.v(r, c + 1)) for r in data_rows[:300] if not _is_blank(sh.v(r, c))]
        if len(cells) < 3:
            continue
        ints = sum(1 for v, _ in cells if _as_int(v) is not None and not looks_like_period(v))
        labelled = sum(1 for _, w in cells if isinstance(w, str) and _texty(w) and _numeric_value(w) is None)
        if ints >= 0.8 * len(cells) and labelled >= 0.8 * len(cells):
            code_cols.add(c)
            continue
        if ints >= 0.9 * len(cells) and c > 0:
            # '5, 6, 7' under the same merged header as the section letters on its left
            for (r0, r1, c0, c1) in data.merges:
                if data_start - 6 <= r0 < data_start and c0 < c < c1 and c1 - c0 <= 3:
                    stub = [sh.v(r, c0) for r in data_rows[:300] if not _is_blank(sh.v(r, c0))]
                    if stub and sum(1 for v in stub if isinstance(v, str) and _texty(v)) >= 0.5 * len(stub):
                        code_cols.add(c)
                        break
    if code_cols:
        data_rows = [r for r in data_rows if any(
            sh.is_obs(sh.v(r, c)) for c in sh.anchors(r) if c not in code_cols
        )] or data_rows
        data_start = _first_data_row(sh, data_rows)
        data_rows = [r for r in data_rows if r >= data_start]

    # stub / value columns, provisionally from the observation rows
    obs_cols: dict[int, int] = {}
    for r in data_rows[:200]:
        for c in sh.anchors(r):
            if c not in code_cols and sh.is_obs(sh.v(r, c)):
                obs_cols[c] = obs_cols.get(c, 0) + 1
    n_rows = len(data_rows[:200])
    value_like = sorted(c for c, n in obs_cols.items() if n >= max(1, 0.3 * n_rows))
    fdc = value_like[0] if value_like else min(obs_cols)
    if fdc > 0:
        # second pass: numeric codes in the stub area must not hide data rows
        again = _data_rows(sh, fdc)
        if again and len(again) > len(data_rows):
            data_start = _first_data_row(sh, again)
            data_rows = [r for r in again if r >= data_start]

    # rows above the data: title lines, note rows, header rows, body headings
    title_lines: list[str] = []
    header_rows: list[int] = []
    pre_headings: list[int] = []
    above = [r for r in range(0, data_start) if sh.anchors(r)]
    last_col = max((max(sh.anchors(r)) for r in range(sh.nr) if sh.anchors(r)), default=0)
    in_title = True
    for r in above:
        cells = sh.anchors(r)
        texts = [sh.v(r, c) for c in cells]
        single = len(cells) == 1
        note_row = (
            len(cells) == 2 and cells[0] < fdc and cells[1] == last_col
            and _is_unit_note(sh.v(r, cells[1])) and not sh.wide(r, cells[1])
        ) or (single and _is_unit_note(texts[0]) and cells[0] >= fdc and not sh.wide(r, cells[0]))
        whole_width = single and cells[0] == 0 and sh.wide(r, 0) and sh.merge_span[(r, 0)][3] > last_col
        if in_title and single and ((cells[0] < fdc and not sh.wide(r, cells[0])) or whole_width):
            title_lines.append(_cell_text(texts[0]))
            continue
        if note_row:
            title_lines.extend(_cell_text(sh.v(r, c)) for c in cells)
            continue
        in_title = False
        has_data_cell = any(c >= fdc and (r, c) not in sh.merge_anchor for c in cells)
        if has_data_cell:
            header_rows.append(r)
            # stub-only rows seen so far sit between header rows: they are header too
            header_rows.extend(pre_headings)
            pre_headings = []
        else:
            pre_headings.append(r)
    header_rows = sorted(set(header_rows))
    header_end = header_rows[-1] if header_rows else (above[-1] if above and not pre_headings else -1)
    body_start = min([header_end + 1] + pre_headings)

    parts0 = _header_parts(sh, header_rows, fdc)
    names: list[str] = [
        " — ".join(t for _, t in parts0[c]) if parts0[c] else f"kolon_{c + 1}" for c in range(sh.nc)
    ]
    seen: dict[str, int] = {}
    for i, n in enumerate(names):
        seen[n] = seen.get(n, 0) + 1
        if seen[n] > 1:
            names[i] = f"{n}_{seen[n]}"

    body_rows = list(range(body_start, sh.nr))
    last_data = data_rows[-1]
    obs_set = set(r for r in data_rows if r >= body_start)
    if not obs_set:
        raise TidyError("no data rows below the header block")
    sample_rows = sorted(obs_set)

    # column roles on observation rows
    period_part: dict[int, str] = {}
    for c in range(fdc, sh.nc):
        periods = [t for _, t in parts0[c] if _header_period(t)]
        if len(periods) == 1:
            period_part[c] = periods[0]
    roles: dict[int, str] = {}
    for c in range(sh.nc):
        column = pd.Series([sh.v(r, c) for r in sample_rows], dtype=object)
        if c in period_part:
            if not any(sh.is_num(v) or _is_marker(v) for v in column):
                role = "empty" if all(_is_blank(v) for v in column) else "id"
            elif _column_role(names[c], column) == "id":
                role = "id"
            else:
                role = "value"
        elif c in code_cols:
            role = "id"
        else:
            role = _column_role(names[c], column)
            nonblank = [v for v in column if not _is_blank(v) and not _is_marker(v)]
            if role == "value" and nonblank and all(isinstance(v, str) for v in nonblank) and any(
                _code_like(v) for v in nonblank
            ):
                role = "id"      # codes stored as text ('05', '051', '10', '101')
            if role == "value" and c < fdc and _header_words(names[c]) & (
                _CODE_HEADER_WORDS | _PERIOD_HEADER_WORDS
            ) and all(_as_int(v) is not None or _is_blank(v) for v in column):
                role = "id"
        roles[c] = role
    # a column inside the value area whose cells are mostly numbers is never a
    # label column (its numbers would silently become labels)
    for c in range(fdc, sh.nc):
        if roles[c] != "id" or c in code_cols:
            continue
        if roles_period(names[c], pd.Series([sh.v(r, c) for r in sample_rows], dtype=object)):
            continue        # Yıl / Ay / Çeyrek stub (month numbers are labels)
        column = [sh.v(r, c) for r in sample_rows if not _is_blank(sh.v(r, c)) and not _is_marker(sh.v(r, c))]
        numeric = [v for v in column if _numeric_value(v) is not None and not _code_like(v)]
        years_only = numeric and all(looks_like_period(v) for v in numeric)
        if column and len(numeric) >= 0.5 * len(column) and not years_only:
            roles[c] = "value"
            other = len(column) - len(numeric) - sum(1 for v in column if _is_marker(v))
            if other:
                assumptions.append(
                    f"sütun {c + 1}: {other} metin hücresi sayı değil, atlandı (sütunun geri kalanı sayı)"
                )

    # "(%)" columns right after each period column belong to that period (strict pattern)
    pcols = sorted(c for c in period_part if roles[c] == "value")
    if len(pcols) >= 2:
        others = [c for c in range(pcols[0], sh.nc) if roles[c] == "value" and c not in period_part]
        followers = {c: max(p for p in pcols if p < c) for c in others}
        extra = {
            " — ".join(t for _, t in parts0[c] if t not in {x for _, x in parts0[followers[c]]})
            for c in followers
        }
        if (
            followers
            and len(extra) == 1
            and all(c == followers[c] + 1 for c in followers)
            and len(set(followers.values())) >= len(pcols) - 1
        ):
            for c, p in followers.items():
                period_part[c] = period_part[p]
            assumptions.append(f"'{extra.pop()}' sütunları solundaki dönem sütununa bağlandı")
    # value columns with no header text at all (decided after the body is read:
    # a later stacked table may carry their header)
    unlabeled_cols: list[int] = []
    labelled = [c for c in range(sh.nc) if roles[c] == "value" and parts0[c]]
    if labelled:
        for c in range(sh.nc):
            if roles[c] != "value" or parts0[c]:
                continue
            column = [sh.v(r, c) for r in sample_rows if not _is_blank(sh.v(r, c))]
            if c < labelled[0] and column and all(_as_int(v) is not None for v in column):
                roles[c] = "id"          # unlabeled row numbers / codes on the left
                continue
            unlabeled_cols.append(c)
    id_cols = [c for c in range(sh.nc) if roles[c] == "id"]
    value_cols = [c for c in range(sh.nc) if roles[c] == "value"]
    if not value_cols:
        raise TidyError("no period columns/headers found, and no numeric value columns either")
    value_set = set(value_cols)
    stub_cols = [c for c in id_cols if c < value_cols[0]] or id_cols[:1]
    main_stub = stub_cols[-1] if stub_cols else None

    # a single-anchor header row over the values (e.g. 'Endeks - Index') that a
    # block title in the body ('Aylık değişim (%)') replaces
    title_row = None
    for r in header_rows:
        cells = [c for c in sh.anchors(r) if c >= fdc]
        if len(cells) == 1 and (sh.wide(r, cells[0]) or len(value_cols) == 1):
            title_row = r

    # ---- classify body rows
    title_keys = {_fold(t) for t in title_lines if _texty(t)}
    items: list[_Item] = []
    for r in body_rows:
        cells = sh.anchors(r)
        if not cells:
            items.append(_Item("blank", r))
            continue
        if len(cells) == 1 and title_keys and _fold(_cell_text(sh.v(r, cells[0]))) in title_keys:
            # the file title printed again on a continuation page: not a block label
            items.append(_Item("note", r, _cell_text(sh.v(r, cells[0]))))
            continue
        text_all = " ".join(_cell_text(sh.v(r, c)) for c in cells)
        if r > last_data:
            items.append(_Item("foot", r, text_all))
            continue
        nums = [sh.v(r, c) for c in value_cols if sh.is_num(sh.v(r, c))]
        years = [_year_of(v) for v in nums]
        stub_labels = [
            sh.v(r, c) for c in id_cols
            if _texty(sh.v(r, c)) and not (_header_words(_cell_text(sh.v(r, c))) & _PERIOD_HEADER_WORDS)
        ]
        year_header = (
            len(nums) >= 2 and all(y is not None for y in years)
            and all(a <= b for a, b in zip(years, years[1:]))
            and not stub_labels
        )
        obs_here = bool(nums) and not year_header
        markers_only = not obs_here and any(_is_marker(sh.v(r, c)) for c in value_cols) and all(
            _is_marker(sh.v(r, c)) or _is_blank(sh.v(r, c)) for c in value_cols
        )
        has_val = obs_here or markers_only
        if obs_here and (r in obs_set or not any(
            not _is_blank(sh.v(r, c)) and not sh.is_num(sh.v(r, c)) and not _is_marker(sh.v(r, c))
            for c in value_cols
        )):
            items.append(_Item("value", r))
            continue
        if markers_only and any(not _is_blank(sh.v(r, c)) for c in id_cols):
            # every value suppressed ('-', 'c', '..'): a value row with no numbers
            items.append(_Item("value", r))
            continue
        if _is_footnote_row(pd.Series([sh.v(r, c) for c in cells])) or (
            not obs_here and _is_footnote_text(sh.v(r, cells[0]))
        ):
            items.append(_Item("foot", r, text_all))
            continue
        if obs_here:
            items.append(_Item("odd", r, text_all))
            continue
        # header text over the value columns (merged cells count from their anchor)
        cover: dict[tuple[int, int], Any] = {}
        for c in value_cols:
            value = sh.filled(r, c)
            if _is_blank(value) or not _texty(value):
                continue
            cover[sh.merge_anchor.get((r, c), (r, c))] = value
        in_values = sorted({ac for (_, ac) in cover})
        stub_cells = [c for c in cells if c not in value_set and (r, c) not in cover and _texty(sh.v(r, c))]
        if not cover and not stub_cells:
            texty_any = any(_texty(sh.v(r, c)) and not _is_unit_note(sh.v(r, c)) for c in cells)
            items.append(_Item("odd" if texty_any else "note", r, text_all))
            continue
        if len(in_values) >= 2 or (in_values and stub_cells):
            items.append(_Item("header", r))
        elif in_values and in_values[0] in value_set:
            items.append(_Item("coltitle", r, _cell_text(sh.v(r, in_values[0]))))
        elif in_values:
            # one merged cell starting in the stub area and spanning the values: a block title
            c0 = in_values[0]
            items.append(_Item("heading", r, _cell_text(sh.v(r, c0)), c0 * 1000 + sh.indent_of(r, c0), True))
        elif stub_cells:
            c0 = stub_cells[0]
            distinct_texts = list(dict.fromkeys(_cell_text(sh.v(r, c)) for c in stub_cells))
            text = " — ".join(distinct_texts)
            period = _period_cell(sh.v(r, c0)) if len(distinct_texts) == 1 and not in_values else None
            if period:
                items.append(_Item("period", r, period))
                continue
            block = (
                (sh.wide(r, c0) and sh.merge_span[(r, c0)][3] > fdc)
                or (r, c0) in data.centered
            ) and c0 < fdc
            items.append(_Item("heading", r, text, c0 * 1000 + sh.indent_of(r, c0), block))
        else:
            items.append(_Item("odd", r, text_all))

    if unlabeled_cols:
        covered: set[int] = set()
        for it in items:
            if it.kind in ("header", "coltitle"):
                for c in unlabeled_cols:
                    if _texty(sh.filled(it.row, c)) or (it.kind == "coltitle"):
                        covered.add(c)
        for c in unlabeled_cols:
            if c in covered:
                continue
            column = [sh.v(r, c) for r in sample_rows if not _is_blank(sh.v(r, c))]
            numbers = [_numeric_value(v) for v in column]
            roles[c] = "skip"
            value_cols.remove(c)
            value_set.discard(c)
            if any(n not in (None, 0) for n in numbers):
                skipped.append(f"başlıksız sütun {c + 1} atlandı ({len(column)} hücre; ne ölçtüğü belirsiz)")
            else:
                assumptions.append(f"başlıksız ve yalnızca 0/boş içeren sütun {c + 1} yok sayıldı")
        if not value_cols:
            raise TidyError("no value column has a header; the layout was not recognised safely")

    odd = [it for it in items if it.kind == "odd"]
    if odd:
        skipped.append(
            f"{len(odd)} satır okunamadı, atlandı (ör. satır {odd[0].row + 1}: '{odd[0].text[:60]}')"
        )

    value_rows = [it.row for it in items if it.kind == "value"]
    labels: dict[int, dict[int, Any]] = {r: {c: sh.filled(r, c) for c in id_cols} for r in value_rows}

    # ---- TR/EN labels stacked on consecutive rows of one stub column
    en_labels: dict[int, dict[int, str]] = {}
    consumed_rows: set[int] = set()
    for c in id_cols:
        seq = [(r, sh.v(r, c)) for r in body_rows if r <= last_data and not _is_blank(sh.v(r, c))]
        texts = [t for _, t in seq if isinstance(t, str)]
        pairs: list[tuple[int, int, str, str]] = []
        decisive = 0
        i = 0
        while i < len(seq) - 1:
            (r1, t1), (r2, t2) = seq[i], seq[i + 1]
            if (
                r2 == r1 + 1 and isinstance(t1, str) and isinstance(t2, str)
                and _single_language(t1) and _single_language(t2)
                and _lang(t1) != "en" and _lang(t2) != "tr"
            ):
                decisive += _lang(t1) == "tr" and _lang(t2) == "en"
                pairs.append((r1, r2, t1, t2))
                i += 2
                continue
            i += 1
        if decisive >= 2 and decisive >= 0.6 * len(pairs) and len(pairs) * 2 >= 0.8 * len(texts):
            for r1, r2, t1, t2 in pairs:
                for r in (r1, r2):
                    if r in labels:
                        labels[r][c] = t1.strip()
                        en_labels.setdefault(r, {})[c] = t2.strip()
                sh.vals[r2][c] = t1
                if r2 not in labels:
                    consumed_rows.add(r2)   # an EN line without numbers: part of the row above
            assumptions.append(
                f"'{names[c]}' sütununda alt alta yazılmış TR/EN etiketler birleştirildi ({len(pairs)} çift)"
            )
    _fill_merged_stubs(labels, value_rows, id_cols)
    for it in items:
        if it.row in consumed_rows and it.kind == "heading":
            it.kind = "attached"

    # TR value row + EN text line alternating (language-free): text-only stub lines
    # that sit directly under a value row, over and over, belong to that row
    nonblank = [it for it in items if it.kind != "blank"]
    sandwiched = []
    for k, it in enumerate(nonblank):
        if it.kind != "heading" or it.block or k == 0:
            continue
        prev = nonblank[k - 1]
        nxt = nonblank[k + 1] if k + 1 < len(nonblank) else None
        if prev.kind == "value" and prev.row == it.row - 1 and (
            nxt is None or nxt.kind != "heading" or nxt.row != it.row + 1
        ):
            sandwiched.append((prev.row, it))
    followed = {pr for pr, _ in sandwiched}
    if len(sandwiched) >= 3 and len(followed) >= 0.6 * max(1, len(value_rows)):
        for pr, it in sandwiched:
            col = it.indent // 1000
            if pr in labels and col in labels[pr]:
                en_labels.setdefault(pr, {})[col] = it.text
                it.kind = "attached"
        assumptions.append(
            f"{len(sandwiched)} metin satırı, üstündeki değer satırının İngilizce etiketi olarak okundu"
        )
    # EN labels follow the TR label they belong to when carried down
    for c in id_cols:
        last_en = None
        last_tr = None
        for r in value_rows:
            raw = sh.v(r, c)
            if c in en_labels.get(r, {}):
                last_en, last_tr = en_labels[r][c], labels[r][c]
            elif not _is_blank(raw):
                last_en = None
            elif last_en is not None and labels[r][c] == last_tr:
                en_labels.setdefault(r, {})[c] = last_en

    # ---- headings, label fragments, period rows, block titles
    def label_cell(r: int) -> tuple[int, Any] | None:
        """First stub cell with a label (after vertical fill-down) and its text."""
        for c in stub_cols:
            if r in labels and not _is_blank(labels[r][c]):
                return c, (sh.v(r, c) if not _is_blank(sh.v(r, c)) else labels[r][c])
            if r not in labels and not _is_blank(sh.v(r, c)):
                return c, sh.v(r, c)
        return None

    def eff_indent(r: int) -> int | None:
        found = label_cell(r)
        if found is None:
            return None
        return found[0] * 1000 + sh.indent_of(r, found[0])

    effs = [e for e in (eff_indent(r) for r in value_rows) if e is not None]
    effs += [it.indent for it in items if it.kind == "heading"]
    indentation_mode = len(set(effs)) > 1
    if indentation_mode:
        # a heading whose next value row is not deeper cannot be an indentation
        # parent: it titles a whole block (until the next such title)
        for i, it in enumerate(items):
            if it.kind != "heading" or it.block:
                continue
            j = i
            while j + 1 < len(items) and items[j + 1].kind == "heading" and items[j + 1].row == items[j].row + 1:
                j += 1
            nxt = next((x for x in items[j + 1:] if x.kind in ("value", "heading", "period")), None)
            if nxt is not None and nxt.kind == "value":
                e = eff_indent(nxt.row)
                if e is not None and e <= it.indent:
                    for k in range(i, j + 1):
                        items[k].block = True

    # value rows that are parents of deeper rows (Toplam / Erkek / Kadın with indented children)
    has_children: set[int] = set()
    if indentation_mode:
        seq_items = [it for it in items if it.kind in ("value", "heading", "period", "coltitle", "header")]
        for a, b in zip(seq_items, seq_items[1:]):
            if a.kind == "value" and b.kind in ("value", "heading"):
                ea = eff_indent(a.row)
                eb = eff_indent(b.row) if b.kind == "value" else (None if b.block else b.indent)
                if ea is not None and eb is not None and eb > ea:
                    has_children.add(a.row)
    parent_labels = {_fold(label_cell(r)[1]) for r in has_children if label_cell(r)}
    parent_mode = len(has_children) >= 2 and len(parent_labels) >= 2

    # repeated heading cycles (flat layouts): the value row right before each
    # restart of the cycle is that block's own (total) row and labels the block
    block_rows: set[int] = set()
    if not indentation_mode:
        heads = [i for i, it in enumerate(items) if it.kind == "heading" and not it.block]
        if heads:
            first_key = _fold(items[heads[0]].text)
            starts = [i for i in heads if _fold(items[i].text) == first_key]
            if len(starts) >= 2:
                label_count: dict[str, int] = {}
                for r in value_rows:
                    lc = label_cell(r)
                    if lc:
                        label_count[_fold(lc[1])] = label_count.get(_fold(lc[1]), 0) + 1
                for s in starts:
                    j = s - 1
                    while j >= 0 and items[j].kind == "blank":
                        j -= 1
                    if j >= 0 and items[j].kind == "value":
                        lc = label_cell(items[j].row)
                        if lc and label_count.get(_fold(lc[1])) == 1:
                            block_rows.add(items[j].row)
                if block_rows:
                    assumptions.append(
                        f"yinelenen başlık döngüsü: {len(block_rows)} toplam satırı blok etiketi olarak kullanıldı"
                    )

    bolum_of: dict[int, list[str]] = {}
    period_of: dict[int, str] = {}
    label_override: dict[int, str] = {}
    parts_for_row: dict[int, dict[int, list[tuple[int, str]]]] = {}
    stack: list[_Item] = []
    pending: list[_Item] = []
    pending_header: list[int] = []
    pending_titles: list[_Item] = []
    col_parts = parts0
    current_period: str | None = None
    period_rows = 0
    header_blocks = 0
    prev_item: _Item | None = None
    first_block_title_row: int | None = None
    bolum_rows_of: dict[int, list[int]] = {}

    def keep_blocks(items_: list[_Item]) -> list[_Item]:
        return [x for x in items_ if x.block and x.indent >= 0]

    def flush_headings(next_value_row: int | None) -> None:
        nonlocal stack
        lines = list(pending)
        pending.clear()
        if not lines:
            return
        if next_value_row is not None:
            lc = label_cell(next_value_row)
            if lc and isinstance(lc[1], str):
                col, label = lc
                k = len(lines) - 1
                last = lines[k]
                if last.row == next_value_row - 1 and not last.block and last.indent // 1000 == col and (
                    _continues(last.text, label) or _tr_then_en(last.text, label)
                ):
                    joined = [last.text]
                    k -= 1
                    while k >= 0 and lines[k].row == lines[k + 1].row - 1 and _continues(lines[k].text, lines[k + 1].text):
                        joined.insert(0, lines[k].text)
                        k -= 1
                    lines = lines[: k + 1]
                    if _tr_then_en(joined[-1], label):
                        label_override[next_value_row] = " ".join(joined)
                        en_labels.setdefault(next_value_row, {})[col] = label.strip()
                    else:
                        label_override[next_value_row] = " ".join(joined + [label.strip()])
        if not lines:
            return
        groups: list[list[_Item]] = []
        for line in lines:
            if groups and line.row == groups[-1][-1].row + 1 and (
                _continues(groups[-1][-1].text, line.text)
                or _tr_then_en(groups[-1][-1].text, line.text)
                or (line.block and groups[-1][-1].block)
            ):
                groups[-1].append(line)
            else:
                groups.append([line])
        heads = [_Item("heading", g[0].row, " ".join(x.text for x in g), g[0].indent, g[0].block) for g in groups]
        for k, h in enumerate(heads):
            if h.block:
                stack = [h]
            elif indentation_mode:
                stack = [x for x in stack if x.block or x.indent < h.indent] + [h]
            else:
                if k == 0:
                    stack = [x for x in stack if x.block]
                stack.append(h)

    for it in items:
        if it.kind == "value":
            r = it.row
            if pending_header or pending_titles:
                if pending_titles and first_block_title_row is None:
                    first_block_title_row = pending_titles[0].row
                title_text = " ".join(x.text for x in pending_titles)
                if pending_header:
                    newp = _header_parts(sh, pending_header, fdc)
                    col_parts = {
                        c: ([(pending_titles[0].row, title_text)] if title_text else []) + newp[c]
                        for c in range(sh.nc)
                    }
                elif title_row is not None:
                    col_parts = {
                        c: [(rr, title_text if rr == title_row else t) for rr, t in parts0[c]]
                        for c in range(sh.nc)
                    }
                else:
                    stack = [_Item("heading", pending_titles[0].row, title_text, -1, True)]
                header_blocks += 1
                pending_header, pending_titles = [], []
            flush_headings(r)
            e = eff_indent(r)
            if indentation_mode and e is not None:
                stack = [x for x in stack if x.block or x.indent < e]
            if r in block_rows:
                stack = keep_blocks(stack)
            bolum_of[r] = [x.text for x in stack]
            bolum_rows_of[r] = [x.row for x in stack]
            if current_period is not None:
                period_of[r] = current_period
            parts_for_row[r] = col_parts
            lc = label_cell(r)
            if r in block_rows and lc:
                stack.append(_Item("heading", r, _cell_text(lc[1]), -1, True))
            elif parent_mode and r in has_children and lc and e is not None:
                stack.append(_Item("heading", r, _cell_text(lc[1]), e, False))
        elif it.kind == "heading":
            prev_row = prev_item.row if prev_item is not None and prev_item.kind == "value" else None
            lc = label_cell(prev_row) if prev_row is not None else None
            if (
                prev_row == it.row - 1 and lc is not None and isinstance(lc[1], str)
                and not it.block and it.indent // 1000 == lc[0]
                and (_tr_then_en(label_override.get(prev_row, lc[1]), it.text) or (
                    _continues(label_override.get(prev_row, lc[1]), it.text) and it.text.lstrip()[:1].islower()
                ))
            ):
                base = label_override.get(prev_row, str(lc[1]).strip())
                if _tr_then_en(base, it.text):
                    en_labels.setdefault(prev_row, {})[lc[0]] = it.text
                else:
                    label_override[prev_row] = base + " " + it.text
                prev_item = _Item("attached", it.row)
                continue
            pending.append(it)
        elif it.kind == "period":
            flush_headings(None)
            current_period = it.text
            period_rows += 1
            stack = keep_blocks(stack)
        elif it.kind == "header":
            if pending and not pending_header:
                # heading lines right above a repeated header block title that block
                pending_titles.extend(pending)
                pending.clear()
            pending_header.append(it.row)
        elif it.kind == "coltitle":
            pending_titles.append(it)
        prev_item = it

    # stacked blocks: later blocks carry their own title row; the first block's
    # title is the last title line above the header (otherwise block 1 would be
    # told apart only by having no title)
    if first_block_title_row is not None:
        # the first two title lines are the file title (TR + EN); a block title
        # is a further line, not a unit/scope note such as '(%)' or '[15+ yaş]'
        candidates = [
            t for t in title_lines[2:]
            if not _is_unit_note(t) and not t.strip().startswith(("[", "("))
        ]
        first_title = candidates[-1] if candidates else None
        first_rows = [r for r in value_rows if r < first_block_title_row]
        if first_title and first_rows:
            for r in first_rows:
                bolum_of[r] = [first_title] + list(bolum_of.get(r, []))
                bolum_rows_of[r] = [-1] + list(bolum_rows_of.get(r, []))
            assumptions.append(f"ilk blok başlığı tablonun üstündeki satırdan alındı: '{first_title[:60]}'")
        elif first_rows:
            assumptions.append("ilk bloğun kendi başlığı yok; sonraki bloklar başlıklarıyla ayrılır")

    # "(%)" / "Oran" style unit-only headers repeated over several columns: each
    # belongs to the measure printed in the column on its left
    def qualify_units(parts_map: dict[int, list[tuple[int, str]]]) -> dict[int, list[tuple[int, str]]]:
        leafs = {c: parts_map[c][-1][1] for c in value_cols if parts_map.get(c)}
        unit_cols = [c for c, t in leafs.items() if _is_unit_note(t)]
        full = {c: tuple(t for _, t in parts_map[c]) for c in unit_cols}
        dup = {f for f in full.values() if sum(1 for x in unit_cols if full[x] == f) > 1}
        if not dup:
            return parts_map
        out = dict(parts_map)
        for c in unit_cols:
            if full[c] not in dup:
                continue
            left = next((x for x in range(c - 1, -1, -1) if x in leafs and not _is_unit_note(leafs[x])), None)
            if left is None or _header_period(leafs[left]):
                continue          # a period is not a measure name (and is already 'donem')
            own = parts_map[c]
            out[c] = own[:-1] + [(own[-1][0], leafs[left] + " — " + own[-1][1])]
        return out

    first_header_rows = {r for r in value_rows if parts_for_row.get(r, parts0) is parts0}
    qualified: dict[int, dict[int, list[tuple[int, str]]]] = {}
    for r in value_rows:
        pm = parts_for_row.get(r, parts0)
        key = id(pm)
        if key not in qualified:
            qualified[key] = qualify_units(pm)
            if qualified[key] is not pm:
                assumptions.append("tekrarlanan birim başlıkları ('(%)' vb.) solundaki sütunun başlığıyla nitelendi")
        parts_for_row[r] = qualified[key]

    # ---- long rows
    has_bolum = any(bolum_of.get(r) for r in value_rows)
    en_cols = {c for d in en_labels.values() for c in d}
    for r, text in label_override.items():
        lc = label_cell(r)
        if lc is not None and r in labels:
            labels[r][lc[0]] = text
    # quarter/month rows whose year is given in a year row before or after the block
    stub_periods: list[str] | None = None
    stub_period_col: int | None = None
    for c in stub_cols:
        column = [labels[r][c] for r in value_rows]
        got = _periods_from_stub(column)
        if got is not None:
            stub_periods, stub_period_col = got, c
            assumptions.append(f"'{names[c]}' sütunundaki yıl ve çeyrek/ay satırları dönem olarak birleştirildi")
            break
    stub_period_of = dict(zip(value_rows, stub_periods)) if stub_periods else {}

    # side-by-side tables: the stub columns repeat between value columns
    left_stubs = [c for c in id_cols if c < value_cols[0]]
    band_of: dict[int, list[int]] = {}
    band_cols: set[int] = set()
    inner = [c for c in id_cols if value_cols[0] < c < value_cols[-1]]
    if inner and left_stubs:
        runs: list[list[int]] = []
        for c in inner:
            if runs and all(roles.get(x) in ("empty", "skip") for x in range(runs[-1][-1] + 1, c)):
                runs[-1].append(c)
            else:
                runs.append([c])

        def leaf(c: int) -> str:
            return _fold(parts0[c][-1][1]) if parts0[c] else ""

        def similar(a: int, b: int) -> bool:
            if leaf(a) and leaf(a) == leaf(b):
                return True
            col_a = pd.Series([labels[r][a] for r in value_rows], dtype=object)
            col_b = pd.Series([labels[r][b] for r in value_rows], dtype=object)
            return roles_period(names[a], col_a) and roles_period(names[b], col_b)

        good_runs = [
            run for run in runs
            if len(run) == len(left_stubs) and all(similar(a, b) for a, b in zip(left_stubs, run))
        ]
        if good_runs:
            starts = sorted(run[0] for run in good_runs)
            by_start = {run[0]: run for run in good_runs}
            for c in value_cols:
                prior = [st for st in starts if st < c]
                band_of[c] = by_start[prior[-1]] if prior else left_stubs
            band_cols = {c for run in good_runs for c in run}
            assumptions.append(
                f"yan yana {len(good_runs) + 1} tablo: her değer kendi solundaki etiket sütunlarıyla okundu"
            )

    records: list[dict[str, Any]] = []
    headerless: list[tuple[int, int]] = []
    for r in value_rows:
        base: dict[str, Any] = {}
        for c in id_cols:
            if c in band_cols:
                continue
            base[names[c]] = labels[r][c]
            if c in en_cols:
                base[names[c] + "_en"] = en_labels.get(r, {}).get(c)
        if has_bolum:
            base["bolum"] = " > ".join(bolum_of.get(r, [])) or None
        base["_donem_bolum"] = period_of.get(r)
        base["_donem_satir"] = stub_period_of.get(r)
        parts_now = parts_for_row.get(r, parts0)
        for c in value_cols:
            number = _clean_number(sh.v(r, c))
            if number is None:
                continue
            try:
                number = float(number)
            except (TypeError, ValueError):
                continue
            rec = dict(base)
            if band_of and band_of[c] is not left_stubs:
                for left, mine in zip(left_stubs, band_of[c]):
                    rec[names[left]] = labels[r][mine]
                    if left in en_cols:
                        rec[names[left] + "_en"] = en_labels.get(r, {}).get(mine)
            parts = [t for _, t in parts_now.get(c, [])]
            col_period = period_part.get(c) if r in first_header_rows else None
            if col_period is None:
                found = [p for p in parts if _header_period(p)]
                col_period = found[0] if len(found) == 1 else None
            parts = [p for p in parts if p != col_period]
            if not parts and not col_period and len(value_cols) > 1:
                headerless.append((r, c))
                continue
            rec["_donem_sutun"] = col_period
            rec["gosterge"] = " — ".join(parts) if parts else (None if col_period else f"kolon_{c + 1}")
            rec["deger"] = number
            rec["kaynak_satir"] = r
            rec["kaynak_sutun"] = c
            if trace:
                rec["kaynak_bolum"] = ";".join(str(x) for x in bolum_rows_of.get(r, []))
                rec["kaynak_gosterge"] = ";".join(
                    f"{hr}:{t}" for hr, t in parts_now.get(c, []) if t != col_period
                )
            records.append(rec)
    if headerless:
        skipped.append(
            f"{len(headerless)} değer atlandı: bulunduğu blokta sütun başlığı yok "
            f"(ör. Excel satır {headerless[0][0] + 1}, sütun {headerless[0][1] + 1})"
        )
    if not records:
        raise TidyError("no numeric observations after cleaning")
    long = pd.DataFrame.from_records(records)

    # ---- period
    note_parts: list[str] = []
    period_source: str | None = None
    sources = {
        "bölüm satırı": long["_donem_bolum"],
        "satır başlığı": long["_donem_satir"],
        "sütun başlığı": long["_donem_sutun"],
    }
    present = {k: v for k, v in sources.items() if v.notna().any()}
    if present:
        combined = None
        for v in present.values():
            v = v.astype(object).where(v.notna(), None)
            combined = v if combined is None else [
                (f"{a} {b}" if a is not None and b is not None else (a if b is None else b))
                for a, b in zip(combined, v)
            ]
        long["donem"] = [" ".join(str(x).split()) if x is not None else None for x in combined]
        period_source = "tablo"
        note_parts.append("dönem: " + " + ".join(present))
    long = long.drop(columns=["_donem_bolum", "_donem_satir", "_donem_sutun"])
    period_ids = [
        names[c] for c in id_cols
        if c != stub_period_col and c not in band_cols
        and roles_period(names[c], pd.Series([labels[r][c] for r in value_rows], dtype=object))
    ]
    if "donem" not in long.columns and period_ids:
        month_like = {"ay", "month", "aylar", "months", "ceyrek", "quarter"}
        if len(period_ids) == 1 and not (_header_words(period_ids[0]) & month_like):
            long = long.rename(columns={period_ids[0]: "donem"})
            note_parts.append(f"uzun format (dönem sütunu '{period_ids[0]}')")
            period_source = "tablo"
        else:
            combined = _combine_period_columns(long, period_ids)
            if combined is not None:
                long["donem"] = combined
                note_parts.append(f"uzun format (dönem sütunları {period_ids} birleştirildi)")
                period_source = "tablo"
            else:
                note_parts.append(f"uzun format (dönem sütunları {period_ids})")
                period_source = "tablo"
    if period_source is None:
        note_parts.append("dönem tabloda yok")
    if header_blocks:
        note_parts.append(f"{header_blocks + 1} başlık bloğu")
    if has_bolum:
        note_parts.append(f"{long['bolum'].nunique()} bölüm")
    note_parts.append(f"{len(value_cols)} değer sütunu")
    if "gosterge" in long.columns and long["gosterge"].isna().all():
        long = long.drop(columns=["gosterge"])
    if "donem" in long.columns:
        long["donem"] = [None if _is_blank(v) else _cell_text(v) for v in long["donem"]]

    _check_values(long)
    long, dropped = _resolve_duplicates(long)
    if dropped:
        skipped.append(dropped)
    if long.attrs.get("tekrar_notu"):
        assumptions.append(long.attrs.pop("tekrar_notu"))
    if not trace:
        long = long.drop(columns=[c for c in ("kaynak_satir", "kaynak_sutun", "kaynak_bolum", "kaynak_gosterge") if c in long.columns])

    assumptions = list(dict.fromkeys(assumptions))
    debug = {
        "data_start": data_start,
        "header_rows": header_rows,
        "fdc": fdc,
        "names": names,
        "roles": {names[c]: roles[c] for c in range(sh.nc) if roles[c] != "empty"},
        "roles_idx": dict(roles),
        "row_kind": {it.row: it.kind for it in items},
        "body_start": body_start,
        "title_lines": title_lines,
        "indentation_mode": indentation_mode,
        "parent_mode": parent_mode,
        "items": [(it.kind, it.row, it.text[:40]) for it in items if it.kind not in ("value", "blank")][:60],
        "items_full": [(it.kind, it.row, it.indent, it.block, it.text[:30]) for it in items if it.kind != "blank"][:80],
        "bolum_of": {r: bolum_of.get(r) for r in value_rows[:80]},
        "block_rows": sorted(block_rows),
    }
    return _Parsed(long, "; ".join(note_parts), period_source, assumptions, skipped, title_lines, debug)


def _combine_period_columns(long: pd.DataFrame, period_ids: list[str]) -> list[str] | None:
    """Yıl + Ay (or Yıl + Çeyrek) stub columns -> 'YYYY-MM' / 'YYYY-Qn'.

    Several month columns (TR and EN names) are fine when they agree.
    Returns None unless every row parses.
    """
    year_col = next(
        (c for c in period_ids if long[c].dropna().map(lambda v: _year_of(v) is not None).all()), None
    )
    if year_col is None:
        return None
    others = [c for c in period_ids if c != year_col]
    if not others:
        return None
    quarter = any(_header_words(c) & {"ceyrek", "quarter"} for c in others)
    out: list[str] = []
    for i in range(len(long)):
        year = _year_of(long[year_col].iat[i])
        if year is None:
            return None
        found = set()
        for c in others:
            value = long[c].iat[i]
            if _is_blank(value):
                continue
            n = _quarter_of(value) if quarter else _month_of(value)
            if n is None:
                return None
            found.add(n)
        if len(found) != 1:
            return None
        n = found.pop()
        out.append(f"{year}-Q{n}" if quarter else f"{year}-{n:02d}")
    return out


def _fill_merged_stubs(labels: dict[int, dict[int, Any]], value_rows: list[int], id_cols: list[int]) -> None:
    """Fill vertically merged stub cells, in place.

    Excel merged cells arrive as one value followed by blanks. A blank in
    a stub column is filled from the nearest value above only when a more
    detailed stub column to its right has a value in that row (so it is a
    continuation of the group above, e.g. the year next to month 2..12).
    A row whose stub cells are all blank is left blank rather than guessed.
    """
    last: dict[int, Any] = {}
    for r in value_rows:
        row = labels[r]
        original = dict(row)
        for pos, c in enumerate(id_cols):
            right = id_cols[pos + 1:]
            if _is_blank(original[c]):
                if right and any(not _is_blank(original[x]) for x in right) and c in last:
                    row[c] = last[c]
            else:
                last[c] = original[c]


def _check_values(long: pd.DataFrame) -> None:
    """Refuse outputs whose 'values' are really stub labels.

    A value series made only of whole years (1900-2100) is a period column
    that slipped through as data; a series of month numbers 1..12 likewise.
    """
    if "gosterge" not in long.columns:
        return
    keys = ["gosterge"] + [c for c in ("sayfa",) if c in long.columns]
    for name, group in long.groupby(keys, sort=False, dropna=False):
        values = group["deger"]
        label = name[0] if isinstance(name, tuple) else name
        if len(values) >= 3 and values.map(looks_like_period).all():
            raise TidyError(
                f"value column '{str(label)[:60]}' holds only year numbers; the "
                "layout was not recognised safely"
            )
        if len(values) >= 12 and values.map(lambda x: float(x).is_integer() and 1 <= x <= 12).all() and (
            sorted(set(values)) == list(range(1, 13))
        ):
            raise TidyError(
                f"value column '{str(label)[:60]}' holds only month numbers 1-12; the "
                "layout was not recognised safely"
            )


def _resolve_duplicates(long: pd.DataFrame) -> tuple[pd.DataFrame, str | None]:
    """Refuse a sheet whose labels give two different numbers the same key.

    Exception: rows printed twice in the source (the same label on rows at
    most 3 apart, a handful of them) are dropped and reported instead of
    failing the whole sheet. Identical repeats (same labels, same number)
    keep one copy.
    """
    keys = [c for c in long.columns if c not in ("deger", "kaynak_satir", "kaynak_sutun", "kaynak_bolum", "kaynak_gosterge")]
    if not keys:
        return long, None
    dup = long.duplicated(subset=keys, keep=False)
    if not dup.any():
        return long, None
    conflict_idx: list[int] = []
    repeat_idx: list[int] = []
    n_conflicts = 0
    near = True
    example = None
    for key, group in long[dup].groupby(keys, dropna=False, sort=False):
        if group["deger"].nunique() > 1:
            n_conflicts += 1
            conflict_idx.extend(group.index)
            if group["kaynak_satir"].max() - group["kaynak_satir"].min() > 3:
                near = False
            if example is None:
                example = key if isinstance(key, tuple) else (key,)
        else:
            repeat_idx.extend(group.index[1:])
    note = None
    if n_conflicts:
        rows_involved = {int(long.at[i, "kaynak_satir"]) for i in conflict_idx}
        if near and len(rows_involved) <= 4 and len(conflict_idx) <= 0.02 * len(long):
            rows = sorted({int(long.at[i, "kaynak_satir"]) + 1 for i in conflict_idx})
            note = (
                f"{len(conflict_idx)} değer atlandı: kaynakta aynı etiketle iki kez yazılmış "
                f"satırlar (Excel satır {rows[:6]}) ayırt edilemiyor"
            )
            long = long.drop(index=conflict_idx)
        else:
            shown = ", ".join(str(x)[:30] for x in example if x is not None and x == x)[:160]
            raise TidyError(
                f"labels do not tell values apart ({n_conflicts} label sets carry different "
                f"numbers, e.g. [{shown}]); the layout was not recognised safely"
            )
    if repeat_idx:
        long = long.drop(index=[i for i in repeat_idx if i in long.index])
        extra = f"{len(repeat_idx)} değer aynı etiket ve aynı sayıyla tekrarlandığı için bir kez verildi"
        note = f"{note}; {extra}" if note else None
        if note is None:
            long.attrs["tekrar_notu"] = extra
    return long.reset_index(drop=True), note



# ----------------------------------------------------------------- released (v0.35) reader
# Kept only as a fallback for sheets the structural reader refuses because its
# labels collide; its output is accepted only if its own labels are unique, and
# it is never reported as 'yüksek'.

def _legacy_find_header_block(frame: pd.DataFrame, scan: int = 15) -> tuple[int, int]:
    """Index range (start, end) of the rows that make up the header block.

    TUIK tables often use 2 or 3 rows for headers (e.g. Unit -> Indicator -> Gender).
    This function finds where the data actually starts, and assumes everything 
    above it forms a multi-row header block.
    """
    data_start = -1
    for index in range(min(scan, len(frame))):
        row = frame.iloc[index]
        values = [v for v in row.tolist() if not _is_blank(v)]
        
        # A row is data if it contains multiple numeric observations (not years)
        observations = sum(
            1
            for v in values
            if not looks_like_period(v) and _numeric_value(v) is not None
        )
        if observations > 1:
            data_start = index
            break
            
    if data_start <= 0:
        return 0, 0  # Fallback if no clear data row is found

    # Header block is everything from top to data_start - 1
    # Let's ignore completely empty rows at the very top
    # Skip leading fully-blank rows AND single-cell decorative titles (a
    # row spanning the whole sheet with just one filled cell, e.g. a report
    # title) -- but stop skipping as soon as a row has >=2 filled cells,
    # since that is a real structural header row (even a sparse one, like
    # a Toplam/Erkek/Kadın group-label row with one label per block).
    # Folding a decorative title into the header would corrupt period
    # detection downstream (a "2020" column becomes "Title — 2020").
    start_idx = 0
    while start_idx < data_start - 1:
        non_blank = [v for v in frame.iloc[start_idx].tolist() if not _is_blank(v)]
        if len(non_blank) >= 2:
            break
        start_idx += 1
    return start_idx, data_start - 1


def _legacy_combine_header_rows(frame: pd.DataFrame, start_idx: int, end_idx: int) -> list[str]:
    """Merge a multi-row header block into a single flat string per column.
    
    Handles Excel merged cells by forward-filling horizontally (for repeating labels
    across columns) and vertically (for multi-level categories).
    """
    block = frame.iloc[start_idx : end_idx + 1].copy()
    
    # Fill horizontally (axis=1) for merged header cells across columns
    block = block.ffill(axis=1)
    
    combined_headers = []
    for col in block.columns:
        parts = []
        for val in block[col]:
            if not _is_blank(val):
                # Format years cleanly (e.g., 2020.0 -> "2020")
                if isinstance(val, float) and val.is_integer():
                    val = str(int(val))
                else:
                    val = str(val).strip()
                
                # Avoid appending duplicate labels like "Toplam — Toplam"
                if not parts or val != parts[-1]:
                    parts.append(val)
        
        # Join the parts with an em-dash, or fallback to an empty string
        combined_headers.append(" — ".join(parts) if parts else "")
        
    return combined_headers


def _legacy_dedupe_columns(names: list[str]) -> list[str]:
    """Suffix repeated header labels so every column name is unique.

    TUIK sheets sometimes repeat a label across merged/adjacent columns
    (e.g. two "Toplam" columns). Without this, body[name] returns a
    DataFrame instead of a Series for the repeated name, which silently
    breaks every per-column boolean check downstream.
    """
    seen: dict[str, int] = {}
    result: list[str] = []
    for name in names:
        seen[name] = seen.get(name, 0) + 1
        result.append(name if seen[name] == 1 else f"{name}_{seen[name]}")
    return result


def _legacy_extract_sections(body: pd.DataFrame) -> tuple[pd.DataFrame, list[str | None]]:
    """Detect intra-sheet section-title rows and strip them out.

    TUIK sometimes stacks several indicators in one sheet, each block
    introduced by a title row (e.g. "İşgücüne katılma oranı (%)") before
    its own years/regions rows resume. Such a title typically comes from a
    merged Excel cell, which pandas reads with the text only in the first
    column and NaN everywhere else in that row -- so the test is "only
    column 0 is filled", not just "few cells filled" (a row that repeats
    the same text across several duplicate label columns, e.g. a
    "Yıllar - Years" axis marker, must NOT match). Returns the body with
    marker rows removed, plus a list (aligned with kept rows) naming each
    row's section -- all None if no markers were found.
    """
    labels: list[str | None] = []
    keep_mask: list[bool] = []
    current: str | None = None
    any_section = False
    for _, row in body.iterrows():
        values = row.tolist()
        first, rest = values[0], values[1:]
        if (
            not _is_blank(first)
            and isinstance(first, str)
            and not looks_like_period(first)
            and all(_is_blank(v) for v in rest)
        ):
            current = first.strip()
            any_section = True
            keep_mask.append(False)
            continue
        keep_mask.append(True)
        labels.append(current)
    if not any_section:
        return body, [None] * len(body)
    kept = body[keep_mask].copy()
    return kept, labels







def _legacy_roles_period(name: str, column: pd.Series) -> bool:
    """True if a stub column holds periods (years, months, quarters)."""
    values = [v for v in column.tolist() if not _is_blank(v)]
    if not values:
        return False
    if all(looks_like_period(v) for v in values):
        return True
    if _header_words(name) & _PERIOD_HEADER_WORDS:
        ints = [_as_int(v) for v in values]
        if all(i is not None and 1 <= i <= 12 for i in ints):
            return True
        texts = [v for v in values if isinstance(v, str) and _numeric_value(v) is None]
        return len(texts) == len(values)
    return False


def _legacy_column_role(name: str, column: pd.Series) -> str:
    """'id' (label/stub column), 'value' (observations) or 'empty'.

    id: mostly text; or every value a year; or a period-named header
    (Yıl/Ay/Çeyrek/Dönem...) whose values are month/quarter numbers or
    text; or a code-named header (Kod/Code/Sıra) with integer values.
    Otherwise numbers make it a value column. Suppression markers ('-',
    '..', 'x', 'C') count as neither text nor numbers.
    """
    values = [v for v in column.tolist() if not _is_blank(v)]
    if not values:
        return "empty"
    if _legacy_roles_period(name, column):
        return "id"
    words = _header_words(name)
    if words & _CODE_HEADER_WORDS and all(_as_int(v) is not None or isinstance(v, str) for v in values):
        return "id"
    numeric = sum(1 for v in values if _numeric_value(v) is not None)
    markers = sum(1 for v in values if isinstance(v, str) and v.strip() in _TEXT_MARKERS)
    text = len(values) - numeric - markers
    if numeric == 0 or text > 0.2 * len(values):
        return "id"
    return "value"


def _legacy_fill_merged_stubs(body: pd.DataFrame, id_columns: list[str]) -> None:
    """Fill vertically merged stub cells, in place.

    Excel merged cells arrive as one value followed by blanks. A blank in
    an id column is filled from above only when a more detailed id column
    to its right has a value in that row (so it is a continuation of the
    group above, e.g. the year next to month 2..12). A row whose stub
    cells are all blank is left blank rather than guessed.
    """
    for position, column in enumerate(id_columns):
        right = id_columns[position + 1:]
        if not right:
            break
        blank = body[column].map(_is_blank)
        right_has_value = ~body[right].apply(lambda row: all(_is_blank(v) for v in row), axis=1)
        filled = body[column].where(~blank).ffill()
        body[column] = body[column].where(~(blank & right_has_value), filled)


def _legacy_tidy_sheet(raw: pd.DataFrame) -> tuple[pd.DataFrame, str, dict[str, Any]]:
    """Tidy one sheet. Returns (long DataFrame, confidence note, debug info)."""
    frame = raw.dropna(axis=0, how="all").dropna(axis=1, how="all")
    if frame.empty:
        raise TidyError("sheet is empty")

    header_start, header_end = _legacy_find_header_block(frame)
    header = _legacy_combine_header_rows(frame, header_start, header_end)
    
    debug = {
        "header_start": header_start,
        "header_end": header_end,
        "header_combined": header,
        "rows_above_header": [
            [None if c != c else c for c in frame.iloc[i].tolist()]
            for i in range(max(0, header_start - 2), header_start)
        ],
        "rows_below_header": [
            [None if c != c else c for c in frame.iloc[i].tolist()]
            for i in range(header_end + 1, min(len(frame), header_end + 4))
        ],
    }
    
    # Body (data) starts right after the header block
    body = frame.iloc[header_end + 1 :].copy()
    body = body[~body.apply(_is_footnote_row, axis=1)]
    if body.empty:
        raise TidyError("no data rows below the header block")

    body, section_labels = _legacy_extract_sections(body)
    if body.empty:
        raise TidyError("sheet is only section titles, no data rows")
    has_sections = any(label is not None for label in section_labels)

    columns: list[str] = []
    for position, value in enumerate(header):
        if _is_blank(value):
            columns.append(f"kolon_{position + 1}")
        elif isinstance(value, float) and value.is_integer():
            columns.append(str(int(value)))
        else:
            columns.append(str(value).strip())
    columns = _legacy_dedupe_columns(columns)
    body.columns = columns
    if has_sections:
        body["_bolum"] = section_labels

    def _col(name: str) -> pd.Series:
        # body[name] is a DataFrame, not a Series, if `columns` still has a
        # duplicate slip through _legacy_dedupe_columns (shouldn't happen, but a
        # DataFrame result silently breaks every boolean check below).
        selected = body[name]
        return selected.iloc[:, 0] if isinstance(selected, pd.DataFrame) else selected

    period_columns = [c for c in columns if looks_like_period(c)]
    # Judge column roles on observation rows only: repeated header rows
    # ('Yıl'/'Year' between blocks) and source/footnote lines would
    # otherwise make a year column look like text-or-numbers.
    obs_rows = body.apply(
        lambda row: any(
            _numeric_value(v) is not None and not looks_like_period(v) for v in row.tolist()
        ),
        axis=1,
    )
    sample = body[obs_rows] if obs_rows.any() else body

    def _sample_col(name: str) -> pd.Series:
        selected = sample[name]
        return selected.iloc[:, 0] if isinstance(selected, pd.DataFrame) else selected

    roles = {c: _legacy_column_role(c, _sample_col(c)) for c in columns if c not in period_columns}
    id_columns = [c for c, role in roles.items() if role == "id"]
    value_columns = [c for c, role in roles.items() if role == "value"]
    section_id = ["_bolum"] if has_sections else []
    debug["column_roles"] = roles

    if period_columns:
        # Period headers (crosstab): every non-period column is an id.
        ids = [c for c in columns if c not in period_columns]
        _legacy_fill_merged_stubs(body, ids)
        long = body.melt(
            id_vars=ids + section_id or None,
            value_vars=period_columns,
            var_name="donem",
            value_name="deger",
        )
        note = (
            f"crosstab unpivot ({len(ids)} etiket, {len(period_columns)} dönem)"
            if ids else "yalnızca dönem sütunları"
        )
    else:
        _legacy_fill_merged_stubs(body, id_columns)
        if not value_columns:
            raise TidyError(
                "no period columns/headers found, and no numeric value "
                "columns either"
            )
        long = body.melt(
            id_vars=id_columns + section_id,
            value_vars=value_columns,
            var_name="gosterge",
            value_name="deger",
        )
        period_ids = [c for c in id_columns if _legacy_roles_period(c, _sample_col(c))]
        if len(period_ids) == 1 and "donem" not in long.columns:
            long = long.rename(columns={period_ids[0]: "donem"})
            note = f"uzun format (dönem sütunu '{period_ids[0]}'), {len(value_columns)} değer sütunu"
        elif period_ids:
            note = f"uzun format (dönem sütunları {period_ids}), {len(value_columns)} değer sütunu"
        else:
            note = "dönem bilgisi yok (tek dönemlik/snapshot tablo)"

    if has_sections:
        bolum = long["_bolum"]
        bolum_present = bolum.notna()
        if "gosterge" in long.columns:
            existing = long["gosterge"]
            placeholder = existing.astype(str).str.match(r"^kolon_\d+$")
            combined = bolum.astype(str) + " — " + existing.astype(str)
            new_gosterge = existing.copy()
            # bölüm bilgisi var + eski değer placeholder/boş -> sadece bölüm adı
            new_gosterge = new_gosterge.mask(
                bolum_present & (placeholder | existing.isna()), bolum
            )
            # bölüm bilgisi var + eski değer gerçek metin -> ikisini birleştir
            new_gosterge = new_gosterge.mask(
                bolum_present & ~placeholder & ~existing.isna(), combined
            )
            # bölüm bilgisi yok (ilk başlıktan önceki satırlar) -> eski değeri
            # olduğu gibi bırak (gerçek sütun adı ya da placeholder)
            long["gosterge"] = new_gosterge
        else:
            long["gosterge"] = bolum.where(bolum_present, "(bölüm belirtilmemiş)")
        long = long.drop(columns=["_bolum"])
        n_sections = len({l for l in section_labels if l is not None})
        note += f"; {n_sections} gösterge bloğu tespit edildi"

    long["deger"] = pd.to_numeric(
        long["deger"].map(_clean_number), errors="coerce"
    )
    long = long[long["deger"].notna()].reset_index(drop=True)
    if long.empty:
        raise TidyError("no numeric observations after cleaning")
    _check_values(long)
    return long, note, debug




# ----------------------------------------------------------------- workbook level


def _sheet_period_from_context(
    title_lines: list[str], sheet_name: str, meta: dict[str, Any] | None, multi_sheet: bool
) -> tuple[str | None, str | None, str | None]:
    """(period, source, note) from title lines, sheet name or catalog -- only if unambiguous."""
    sources: list[tuple[str, str]] = []
    title_text = " ".join(title_lines)
    periods, has_range = periods_in_text(title_text)
    if has_range or len(periods) > 1:
        return None, None, "başlık birden çok dönem/dönem aralığı içeriyor"
    if periods:
        sources.append((periods[0], "başlık"))
    s_periods, s_range = periods_in_text(sheet_name)
    if not s_range and len(s_periods) == 1:
        sources.append((s_periods[0], "sayfa adı"))
    if meta and not multi_sheet:
        for key, label in (("name", "katalog adı"), ("path", "katalog yolu")):
            text = meta.get(key) or ""
            if key == "path":
                text = str(text).split(">")[-1]
            p, rng = periods_in_text(text)
            if rng or len(p) > 1:
                if not sources:
                    return None, None, f"{label} birden çok dönem/dönem aralığı içeriyor"
                continue
            if p:
                sources.append((p[0], label))
    if not sources:
        return None, None, None
    # the in-file source comes first; a catalog year that the title refines
    # (title '2019 Ekim-Aralık', catalog '2019') agrees with it
    period, source = sources[0]
    for other, other_source in sources[1:]:
        if other == period or period.startswith(other) or other.startswith(period):
            continue
        return None, None, "çelişkili dönem bilgisi: " + ", ".join(f"{s}={p}" for p, s in sources)
    return period, source, None


def _legacy_fallback(raw: pd.DataFrame) -> "_Parsed | None":
    """Released reader for one sheet, accepted only when its labels are unique."""
    try:
        long, note, _ = _legacy_tidy_sheet(raw)
        _check_values(long)
    except (TidyError, Exception):  # noqa: BLE001
        return None
    keys = [c for c in long.columns if c != "deger"]
    if not keys or long.duplicated(subset=keys).any():
        return None
    period = "tablo" if "donem" in long.columns else None
    if "donem" in long.columns:
        long["donem"] = [None if _is_blank(v) else _cell_text(v) for v in long["donem"]]
    return _Parsed(long.reset_index(drop=True), "önceki yöntem: " + note, period, [], [], [], {})


def _read_book(content: bytes) -> tuple[dict[str, SheetData], str]:
    kind = _file_kind(content)
    if kind == "pdf":
        raise TidyError("file is a PDF, not a spreadsheet; use tuik_download_table_file")
    if kind == "html":
        raise TidyError("file is HTML, not a spreadsheet (portal error page?)")
    if kind in ("zip", "docx"):
        raise TidyError(f"file is a {kind.upper()} archive/document, not a spreadsheet; use tuik_download_table_file")
    try:
        book = pd.read_excel(io.BytesIO(content), sheet_name=None, header=None)
    except Exception as exc:  # noqa: BLE001
        raise TidyError(f"file is not a readable Excel workbook ({type(exc).__name__})") from exc
    if not book:
        raise TidyError("workbook has no sheets")
    fmt = _read_formatting(content, kind)
    sheets: dict[str, SheetData] = {}
    for name, frame in book.items():
        merges, indent, centered = fmt.get(str(name), ([], {}, set()))
        sheets[str(name)] = SheetData(str(name), frame, merges, indent, centered)
    return sheets, kind


def tidy_istab(
    content: bytes, debug: bool = False, meta: dict[str, Any] | None = None, trace: bool = False
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Tidy a downloaded istab workbook. Returns (frame, report).

    meta: optional catalog info {'name', 'path'} used only to recover a
    single reference period the sheet itself does not state.

    tidy_confidence:
      'yüksek'  every data sheet tidied from a plain grid: labels straight from
                header cells and label columns, period from the grid itself;
      'orta'    every data sheet tidied, but the period came from the title /
                sheet name / catalog ('donem_kaynagi') or none was found, or
                the structure was inferred (heading hierarchy 'bolum', repeated
                header blocks, TR/EN merges...; listed in 'yapi_cikarimi');
      'kısmi'   some sheets or rows could not be read (listed in
                'sheet_failures' / 'atlananlar').
    """
    sheets, _kind = _read_book(content)
    frames: list[pd.DataFrame] = []
    notes: dict[str, str] = {}
    failures: dict[str, str] = {}
    no_data: list[str] = []
    period_sources: dict[str, str] = {}
    assumptions: dict[str, list[str]] = {}
    skipped: dict[str, list[str]] = {}
    debug_by_sheet: dict[str, Any] = {}
    structure_notes: dict[str, list[str]] = {}
    multi = len(sheets) > 1
    for name, data in sheets.items():
        try:
            parsed = _tidy_sheet_data(data, meta, trace=trace)
        except TidyError as exc:
            msg = str(exc)
            if msg in ("sheet is empty", "no numeric observations in the sheet"):
                no_data.append(name)
                continue
            legacy = _legacy_fallback(data.frame) if "do not tell values apart" in msg else None
            if legacy is None:
                failures[name] = msg
                continue
            parsed = legacy
            parsed.assumptions.append(
                "yapısal okuyucu etiketlerin değerleri ayırt etmediğini buldu; sayfa önceki "
                "sürümün yöntemiyle okundu (etiketleri benzersiz) -- etiket yapısı doğrulanmadı"
            )
        long = parsed.long
        source = parsed.period_source
        if source is None:
            period, ctx_source, ctx_note = _sheet_period_from_context(parsed.title_lines, name, meta, multi)
            if period is not None:
                insert_at = list(long.columns).index("gosterge") if "gosterge" in long.columns else len(long.columns) - 1
                long.insert(insert_at, "donem", period)
                long.insert(insert_at + 1, "donem_kaynak", ctx_source)
                source = ctx_source
                parsed.note += f"; dönem {ctx_source} kaynağından: {period}"
            else:
                source = "yok" + (f" ({ctx_note})" if ctx_note else "")
        else:
            source = "tablo"
        inferred = []
        if "bolum" in long.columns:
            inferred.append("satır başlıkları/üst satırlar 'bolum' hiyerarşisi olarak çıkarıldı")
        if "başlık bloğu" in parsed.note:
            inferred.append("tablo içinde tekrarlanan başlık blokları birleştirildi")
        depth = len(parsed.debug.get("header_rows", []))
        if depth >= 3:
            inferred.append(f"sütun etiketleri {depth} başlık satırından birleştirildi (çok katmanlı başlık)")
        inferred += parsed.assumptions
        if inferred:
            structure_notes[name] = inferred
        if multi:
            long.insert(0, "sayfa", name)
        frames.append(long)
        notes[name] = parsed.note
        period_sources[name] = source
        if parsed.assumptions:
            assumptions[name] = parsed.assumptions
        if parsed.skipped:
            skipped[name] = parsed.skipped
        debug_by_sheet[name] = parsed.debug

    if not frames:
        if failures:
            raise TidyError(
                "no sheet could be tidied: "
                + "; ".join(f"{k}: {v}" for k, v in failures.items())
            )
        raise TidyError("no sheet holds numeric data")

    merged = pd.concat(frames, ignore_index=True)
    # keep a stable column order: labels first, then period, indicator, value
    tail = [c for c in ("bolum", "donem", "donem_kaynak", "gosterge", "deger", "kaynak_satir", "kaynak_sutun",
                        "kaynak_bolum", "kaynak_gosterge") if c in merged.columns]
    head = [c for c in merged.columns if c not in tail]
    merged = merged[head + tail]
    if failures or skipped:
        confidence = "kısmi"
    elif any(src != "tablo" for src in period_sources.values()) or structure_notes:
        # 'yüksek' only for plain grids: no inferred heading hierarchy, no
        # repeated header blocks, no interpretation (see 'yapi_cikarimi')
        confidence = "orta"
    else:
        confidence = "yüksek"
    report: dict[str, Any] = {
        "n_sheets": len(sheets),
        "n_sheets_tidied": len(frames),
        "sheet_notes": notes,
        "sheet_failures": failures,
        "tidy_confidence": confidence,
        "donem_kaynagi": period_sources,
    }
    if no_data:
        report["veri_icermeyen_sayfalar"] = no_data
    if assumptions:
        report["varsayimlar"] = assumptions
    if structure_notes:
        report["yapi_cikarimi"] = structure_notes
    if skipped:
        report["atlananlar"] = skipped
    if debug:
        report["debug_header"] = debug_by_sheet
    return merged, report
