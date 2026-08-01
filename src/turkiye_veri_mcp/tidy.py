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


def find_header_row(frame: pd.DataFrame, scan: int = 12) -> int:
    """Index of the row that most looks like a header.

    A header row carries labels and/or period markers but no free-standing
    observations, so any row holding a non-period number is treated as data.
    """
    best_index, best_score = None, -1.0
    for index in range(min(scan, len(frame))):
        row = frame.iloc[index]
        values = [v for v in row.tolist() if not _is_blank(v)]
        if len(values) < 2:
            continue
        periods = sum(1 for v in values if looks_like_period(v))
        observations = sum(
            1
            for v in values
            if not looks_like_period(v) and _numeric_value(v) is not None
        )
        if observations:
            continue  # data row, not a header
        labels = len(values) - periods
        score = periods * 2 + labels + len(values) / max(len(row), 1)
        if score > best_score:
            best_index, best_score = index, score
    return best_index if best_index is not None else 0


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


def tidy_sheet(raw: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    """Tidy one sheet. Returns (long DataFrame, confidence note)."""
    frame = raw.dropna(axis=0, how="all").dropna(axis=1, how="all")
    if frame.empty:
        raise TidyError("sheet is empty")

    header_index = find_header_row(frame)
    header = frame.iloc[header_index].tolist()
    body = frame.iloc[header_index + 1 :].copy()
    body = body[~body.apply(_is_footnote_row, axis=1)]
    if body.empty:
        raise TidyError("no data rows below the header")

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
    label_columns = [c for c in columns if c not in period_columns]
    section_id = ["_bolum"] if has_sections else []

    if period_columns and label_columns:
        long = body.melt(
            id_vars=label_columns + section_id,
            value_vars=period_columns,
            var_name="donem",
            value_name="deger",
        )
        note = f"crosstab unpivot ({len(label_columns)} etiket, {len(period_columns)} dönem)"
    elif period_columns:
        long = body.melt(
            id_vars=section_id or None,
            value_vars=period_columns,
            var_name="donem",
            value_name="deger",
        )
        note = "yalnızca dönem sütunları"
    else:
        # A period column instead of period headers (long already).
        period_like = [
            c for c in columns
            if _col(c).map(looks_like_period).mean() > 0.7
        ]
        if period_like:
            period_column = period_like[0]
            value_columns = [
                c for c in columns
                if c != period_column
                and pd.to_numeric(_col(c), errors="coerce").notna().mean() > 0.5
            ]
            if not value_columns:
                raise TidyError("period column found but no numeric value columns")
            id_columns = [c for c in columns if c not in value_columns and c != period_column]
            long = body.melt(
                id_vars=id_columns + section_id + [period_column],
                value_vars=value_columns,
                var_name="gosterge",
                value_name="deger",
            ).rename(columns={period_column: "donem"})
            note = "zaten uzun format (dönem sütunu)"
        else:
            # No period anywhere: a single-reference-period ("snapshot")
            # table -- category + value columns, no time dimension at all.
            value_columns = [
                c for c in columns
                if pd.to_numeric(_col(c), errors="coerce").notna().mean() > 0.5
            ]
            if not value_columns:
                raise TidyError(
                    "no period columns/headers found, and no numeric value "
                    "columns either"
                )
            id_columns = [c for c in columns if c not in value_columns]
            long = body.melt(
                id_vars=id_columns + section_id,
                value_vars=value_columns,
                var_name="gosterge",
                value_name="deger",
            )
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
    return long, note


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
    text = text.replace("\xa0", "").replace(" ", "")
    if "," in text and "." in text:                # 1.234,5 -> 1234.5
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    text = re.sub(r"[^0-9.\-]", "", text)
    return text or None


def tidy_istab(content: bytes) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Tidy a downloaded istab workbook. Returns (frame, report)."""
    try:
        book = pd.read_excel(io.BytesIO(content), sheet_name=None, header=None)
    except Exception as exc:  # noqa: BLE001
        raise TidyError(f"file is not a readable Excel workbook ({type(exc).__name__})") from exc
    if not book:
        raise TidyError("workbook has no sheets")

    frames: list[pd.DataFrame] = []
    notes: dict[str, str] = {}
    failures: dict[str, str] = {}
    for name, sheet in book.items():
        try:
            long, note = tidy_sheet(sheet)
        except TidyError as exc:
            failures[name] = str(exc)
            continue
        if len(book) > 1:
            long.insert(0, "sayfa", name)
        frames.append(long)
        notes[name] = note

    if not frames:
        raise TidyError(
            "no sheet could be tidied: "
            + "; ".join(f"{k}: {v}" for k, v in failures.items())
        )

    merged = pd.concat(frames, ignore_index=True)
    report = {
        "n_sheets": len(book),
        "n_sheets_tidied": len(frames),
        "sheet_notes": notes,
        "sheet_failures": failures,
        "tidy_confidence": "yüksek" if not failures and len(frames) == len(book) else "kısmi",
    }
    return merged, report
