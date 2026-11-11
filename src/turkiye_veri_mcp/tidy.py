"""Turn TUIK 'istab' Excel downloads into tidy long-format DataFrames.

TUIK publishes most tables as Excel files built from a handful of house
templates rather than one canonical layout. This module handles the shapes
the probe classifies:

  clean        one header row               -> used as-is, then melted
  multiheader  title/footnote rows on top   -> header row detected, rest dropped
  multisheet   one sheet per year/breakdown -> each sheet parsed, then stacked
  crosstab     label columns + period cols  -> unpivoted to long format

Output columns: the table's label columns (kept verbatim), plus `donem`
(period) and `deger` (value); `sayfa` (sheet) is added for multisheet files.

This is best-effort by design: layouts vary, so every result carries a
`tidy_confidence` note and the raw file stays one call away.
"""

from __future__ import annotations

import io
import re
from typing import Any

import pandas as pd

_FOOTNOTE_MARKERS = (
    "kaynak", "source", "not:", "note:", "(1)", "dipnot", "açıklama",
)

_PERIOD_PATTERNS = (
    re.compile(r"^(19|20)\d{2}$"),                      # 2020
    re.compile(r"^(19|20)\d{2}[-/](0?[1-9]|1[0-2])$"),  # 2020-01
    re.compile(r"^(19|20)\d{2}\s*[-/]\s*(19|20)\d{2}$"),  # 2019-2020
    re.compile(r"^(19|20)\d{2}\s*[QÇ][1-4]$", re.IGNORECASE),
    re.compile(r"^[QÇ][1-4]\s*(19|20)\d{2}$", re.IGNORECASE),
)


class TidyError(RuntimeError):
    """Raised when an istab file cannot be tidied with any known template."""


def looks_like_period(value: Any) -> bool:
    if value is None or value != value:
        return False
    if isinstance(value, (int, float)) and float(value).is_integer():
        return 1900 <= float(value) <= 2100
    text = str(value).strip()
    # Revision/footnote marks on periods: '2017(r)', '2020 (1)', '2021*'.
    text = re.sub(r"\s*(\([^)]{1,4}\)|\*+)$", "", text).strip()
    return any(pattern.match(text) for pattern in _PERIOD_PATTERNS)


def _is_blank(value: Any) -> bool:
    return value is None or value != value or not str(value).strip()


def _is_footnote_row(row: pd.Series) -> bool:
    values = [str(v).strip().casefold() for v in row.tolist() if not _is_blank(v)]
    if len(values) > 2:
        return False
    return any(v.startswith(_FOOTNOTE_MARKERS) for v in values)


def _numeric_value(value: Any) -> float | None:
    """Return the number a cell holds, if any (handles TR formatting)."""
    cleaned = _clean_number(value)
    if cleaned is None:
        return None
    try:
        return float(cleaned)
    except (TypeError, ValueError):
        return None


def find_header_block(frame: pd.DataFrame, scan: int = 15) -> tuple[int, int]:
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


def _combine_header_rows(frame: pd.DataFrame, start_idx: int, end_idx: int) -> list[str]:
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


def _dedupe_columns(names: list[str]) -> list[str]:
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


def _extract_sections(body: pd.DataFrame) -> tuple[pd.DataFrame, list[str | None]]:
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


_PERIOD_HEADER_WORDS = {
    "yil", "yillar", "year", "years", "ay", "aylar", "month", "months",
    "ceyrek", "quarter", "donem", "period", "tarih", "date",
}
_CODE_HEADER_WORDS = {"kod", "kodu", "code", "codes", "sira", "rank"}
_TEXT_MARKERS = ("-", "–", "—", "..", ".", ":", "*", "x", "X", "c", "C")


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
    numeric = sum(1 for v in values if _numeric_value(v) is not None)
    markers = sum(1 for v in values if isinstance(v, str) and v.strip() in _TEXT_MARKERS)
    text = len(values) - numeric - markers
    if numeric == 0 or text > 0.2 * len(values):
        return "id"
    return "value"


def _fill_merged_stubs(body: pd.DataFrame, id_columns: list[str]) -> None:
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


def tidy_sheet(raw: pd.DataFrame) -> tuple[pd.DataFrame, str, dict[str, Any]]:
    """Tidy one sheet. Returns (long DataFrame, confidence note, debug info)."""
    frame = raw.dropna(axis=0, how="all").dropna(axis=1, how="all")
    if frame.empty:
        raise TidyError("sheet is empty")

    header_start, header_end = find_header_block(frame)
    header = _combine_header_rows(frame, header_start, header_end)
    
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

    body, section_labels = _extract_sections(body)
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
    columns = _dedupe_columns(columns)
    body.columns = columns
    if has_sections:
        body["_bolum"] = section_labels

    def _col(name: str) -> pd.Series:
        # body[name] is a DataFrame, not a Series, if `columns` still has a
        # duplicate slip through _dedupe_columns (shouldn't happen, but a
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

    roles = {c: _column_role(c, _sample_col(c)) for c in columns if c not in period_columns}
    id_columns = [c for c, role in roles.items() if role == "id"]
    value_columns = [c for c, role in roles.items() if role == "value"]
    section_id = ["_bolum"] if has_sections else []
    debug["column_roles"] = roles

    if period_columns:
        # Period headers (crosstab): every non-period column is an id.
        ids = [c for c in columns if c not in period_columns]
        _fill_merged_stubs(body, ids)
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
        _fill_merged_stubs(body, id_columns)
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
        period_ids = [c for c in id_columns if roles_period(c, _sample_col(c))]
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


def _check_values(long: pd.DataFrame) -> None:
    """Refuse outputs whose 'values' are really stub labels.

    A value series made only of whole years (1900-2100) is a period column
    that slipped through as data; returning it would put years in 'deger'.
    """
    if "gosterge" not in long.columns:
        return
    for name, group in long.groupby("gosterge", sort=False, dropna=False):
        values = group["deger"]
        if len(values) >= 3 and values.map(looks_like_period).all():
            raise TidyError(
                f"value column '{str(name)[:60]}' holds only year numbers; the "
                "layout was not recognised safely"
            )


def _clean_number(value: Any) -> Any:
    """TUIK cells carry thousands separators, footnote marks and dashes."""
    if value is None or value != value:
        return None
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    if text in ("-", "–", "—", "..", ".", ":", "*"):
        return None
    text = re.sub(r"\([^)]*\)", "", text)          # footnote markers
    text = text.strip()
    # A genuine number never contains a letter -- reject free-text labels
    # (e.g. "15 ve daha yukarı yaştaki nüfus... Population 15 years...")
    # BEFORE stripping non-digit characters, or the stray digits embedded
    # in the sentence would concatenate into a fake number (e.g. "1515").
    if any(ch.isalpha() for ch in text):
        return None
    text = text.replace("\xa0", "").replace(" ", "").replace("\u2212", "-")
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


def tidy_istab(content: bytes, debug: bool = False) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Tidy a downloaded istab workbook. Returns (frame, report).

    The report's 'tidy_confidence' is 'yüksek' when every sheet was tidied
    and each carries period information, 'orta' when every sheet was tidied
    but some have no period (single-period/snapshot tables, check the
    table title for the reference period), 'kısmi' when some sheets failed.
    """
    head = content[:8]
    if head.startswith(b"%PDF"):
        raise TidyError("file is a PDF, not a spreadsheet; use tuik_download_table_file")
    if head.lstrip()[:1] == b"<":
        raise TidyError("file is HTML, not a spreadsheet (portal error page?)")
    try:
        book = pd.read_excel(io.BytesIO(content), sheet_name=None, header=None)
    except Exception as exc:  # noqa: BLE001
        raise TidyError(f"file is not a readable Excel workbook ({type(exc).__name__})") from exc
    if not book:
        raise TidyError("workbook has no sheets")

    frames: list[pd.DataFrame] = []
    notes: dict[str, str] = {}
    failures: dict[str, str] = {}
    debug_by_sheet: dict[str, Any] = {}
    for name, sheet in book.items():
        try:
            long, note, debug = tidy_sheet(sheet)
        except TidyError as exc:
            failures[name] = str(exc)
            continue
        if len(book) > 1:
            long.insert(0, "sayfa", name)
        frames.append(long)
        notes[name] = note
        debug_by_sheet[name] = debug

    if not frames:
        raise TidyError(
            "no sheet could be tidied: "
            + "; ".join(f"{k}: {v}" for k, v in failures.items())
        )

    merged = pd.concat(frames, ignore_index=True)
    if failures or len(frames) != len(book):
        confidence = "kısmi"
    elif any(note.startswith("dönem bilgisi yok") for note in notes.values()):
        confidence = "orta"
    else:
        confidence = "yüksek"
    report = {
        "n_sheets": len(book),
        "n_sheets_tidied": len(frames),
        "sheet_notes": notes,
        "sheet_failures": failures,
        "tidy_confidence": confidence,
    }
    if debug:
        report["debug_header"] = debug_by_sheet
    return merged, report
