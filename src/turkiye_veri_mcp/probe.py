"""Probe TUIK 'istab' downloads to measure how many are machine-parseable.

The coverage audit counts istab tables; this probe opens a sample of them and
classifies their structure, so the "how much of the remaining 78.8% can we
actually tidy?" question is answered with measurements, not guesses.

    turkiye-veri-probe --per-theme 8        # writes ISTAB_PROBE.md

Classes:
  clean        single sheet, one header row -> parses today, no custom code
  multiheader  merged/multi-row header -> parses with a per-family rule
  multisheet   several sheets (often year-per-sheet) -> loop + same rule
  crosstab     header rows AND header columns (pivot) -> needs unpivot rule
  nontabular   PDF/zip/HTML or unreadable -> out of scope
  error        download or open failed -> report separately
"""

from __future__ import annotations

import argparse
import io
import random
import time
from collections import Counter
from datetime import date

import httpx

from turkiye_veri_mcp.portal import PORTAL_BASE, _UA
from turkiye_veri_mcp.portal import PortalClient
from turkiye_veri_mcp.tidy import TidyError, tidy_istab

_TIMEOUT = 90.0
_DOWNLOAD_HEADERS = {
    "User-Agent": _UA,
    "Accept": "*/*",
    "Referer": f"{PORTAL_BASE}/tr/statistical-themes",
}


def _classify(content: bytes, content_type: str) -> tuple[str, str]:
    """Return (class, note) for a downloaded istab payload."""
    head = content[:8]
    if b"%PDF" in head:
        return "nontabular", "pdf"
    if content_type and "html" in content_type and not head.startswith(b"PK"):
        return "nontabular", "html"

    try:
        import pandas as pd
    except ImportError:  # pragma: no cover
        return "error", "pandas missing"

    try:
        book = pd.read_excel(io.BytesIO(content), sheet_name=None, header=None)
    except Exception as exc:  # noqa: BLE001
        return "nontabular", f"not excel: {type(exc).__name__}"

    if not book:
        return "error", "empty workbook"

    sheets = list(book.values())
    frame = sheets[0]
    if frame.empty:
        return "error", "empty first sheet"

    def _is_year_label(value: object) -> bool:
        # Year headers (2020, 2021...) are labels, not observations.
        return (
            isinstance(value, (int, float))
            and float(value).is_integer()
            and 1900 <= float(value) <= 2100
        )

    # How many leading rows look like header/title material (mostly text)?
    header_rows = 0
    for _, row in frame.head(8).iterrows():
        values = [v for v in row.tolist() if v == v and str(v).strip()]
        if not values:
            header_rows += 1
            continue
        numeric = sum(
            1
            for v in values
            if isinstance(v, (int, float)) and not _is_year_label(v)
        )
        if numeric / len(values) < 0.3:
            header_rows += 1
        else:
            break

    # Does the body carry label columns on the left (crosstab shape)?
    body = frame.iloc[header_rows : header_rows + 20]
    label_cols = 0
    for column in body.columns[:4]:
        values = [v for v in body[column].tolist() if v == v]
        if not values:
            continue
        text = sum(1 for v in values if isinstance(v, str))
        if text / len(values) > 0.6:
            label_cols += 1
        else:
            break

    if len(book) > 1:
        return "multisheet", f"{len(book)} sheets, {header_rows} header rows"
    if label_cols >= 2 and header_rows >= 2:
        return "crosstab", f"{header_rows} header rows, {label_cols} label cols"
    if header_rows > 1:
        return "multiheader", f"{header_rows} header rows"
    return "clean", "single header row"


def probe(per_theme: int = 8, lang: str = "tr", seed: int = 0, delay: float = 0.5) -> dict:
    random.seed(seed)
    portal = PortalClient()
    results: list[dict] = []
    for theme in portal.list_themes(lang=lang):
        rows = portal.list_resources(theme["theme_id"], lang=lang, types=("istab",))
        if not rows:
            continue
        sample = random.sample(rows, min(per_theme, len(rows)))
        print(f"  {theme['theme_name']}: {len(sample)}/{len(rows)} örnek")
        for row in sample:
            try:
                with httpx.Client(
                    timeout=_TIMEOUT, follow_redirects=True, headers=_DOWNLOAD_HEADERS
                ) as client:
                    response = None
                    for attempt in range(3):
                        response = client.get(row["url"])
                        if response.status_code != 429:
                            break
                        time.sleep(delay * (2 ** (attempt + 2)))  # 429: back off harder
                    response.raise_for_status()
                kind, note = _classify(
                    response.content, response.headers.get("content-type", "")
                )
                try:
                    _, tidy_report = tidy_istab(response.content)
                    tidy_ok, tidy_note = True, tidy_report["tidy_confidence"]
                except TidyError as exc:
                    tidy_ok, tidy_note = False, str(exc)[:200]
            except Exception as exc:  # noqa: BLE001
                kind, note = "error", f"{type(exc).__name__}: {exc}"[:200]
                tidy_ok, tidy_note = None, ""
            results.append(
                {
                    "theme": theme["theme_name"],
                    "name": row["name"],
                    "url": row["url"],
                    "class": kind,
                    "note": note,
                    "tidy_ok": tidy_ok,
                    "tidy_note": tidy_note,
                }
            )
            time.sleep(delay)
    return {"results": results, "per_theme": per_theme}


_ORDER = ["clean", "multiheader", "multisheet", "crosstab", "nontabular", "error"]

_MEANING = {
    "clean": "bugün olduğu gibi parse edilir",
    "multiheader": "aile başına tek kural ile parse edilir",
    "multisheet": "döngü + aynı kural ile parse edilir",
    "crosstab": "unpivot kuralı gerekir",
    "nontabular": "kapsam dışı (PDF/HTML/zip)",
    "error": "indirme/açma hatası — ayrıca incelenmeli",
}


def render(probe_result: dict) -> str:
    results = probe_result["results"]
    total = len(results)
    counts = Counter(r["class"] for r in results)
    per_theme_counts: dict[str, Counter] = {}
    for row in results:
        per_theme_counts.setdefault(row["theme"], Counter())[row["class"]] += 1

    reachable = total - counts["error"]
    tidyable = counts["clean"] + counts["multiheader"] + counts["multisheet"] + counts["crosstab"]

    lines = [
        f"# istab Sonda Raporu — turkiye-veri-mcp ({date.today().isoformat()})",
        "",
        f"Örneklem: tema başına en fazla {probe_result['per_theme']} dosya, "
        f"toplam **{total}** istab dosyası indirilip açıldı.",
        "",
        "| Sınıf | Adet | Pay | Anlamı |",
        "| --- | ---: | ---: | --- |",
    ]
    for kind in _ORDER:
        n = counts[kind]
        if not n:
            continue
        lines.append(f"| {kind} | {n} | %{100 * n / total:.1f} | {_MEANING[kind]} |")

    if reachable == 0:
        lines += [
            "",
            "**Hiçbir dosya indirilemedi** — bu bir kapsam ölçümü değil, bir "
            "erişim sorunudur (ağ engeli, User-Agent/Referer reddi, zaman "
            "aşımı vb.). Aşağıdaki 'Sorunlu örnekler' bölümündeki hata "
            "mesajlarına bakıp nedeni tespit edin; kapsam yüzdesi bu "
            "koşulda anlamlı değildir.",
        ]
    else:
        lines += [
            "",
            f"**Şekilsel tavan:** {tidyable}/{reachable} "
            f"(%{100 * tidyable / reachable:.1f} — indirilebilen dosyalar içinde) "
            "bilinen bir şablona uyuyor. Bu, dosyanın *şekline* bakan kaba bir "
            "ölçümdür; aşağıdaki 'Gerçek tidy başarısı' asıl cevaptır.",
        ]

    tidy_results = [r for r in results if r.get("tidy_ok") is not None]
    if tidy_results:
        tidy_success = sum(1 for r in tidy_results if r["tidy_ok"])
        n_tidy_total = len(tidy_results)
        lines += [
            "",
            "## Gerçek tidy başarısı (tuik_get_table_data ile)",
            "",
            f"İndirilen **{n_tidy_total}** dosyanın **{tidy_success}** tanesi "
            f"(%{100 * tidy_success / n_tidy_total:.1f}) `tuik_get_table_data` "
            "aracıyla bugün, kod değişikliği olmadan tidy uzun formata "
            "çevrildi. Bu şekilsel sınıflandırmadan farklı olarak gerçek "
            "aracın çalıştırılmasıyla ölçülmüştür.",
        ]
        failures = [r for r in tidy_results if not r["tidy_ok"]]
        if failures:
            lines += ["", "Tidy edilemeyenler:", ""]
            for row in failures[:25]:
                lines.append(f"- {row['theme']} — {row['name']}: {row['tidy_note']}")

    lines += [
        "",
        "## Tema bazında",
        "",
        "| Tema | " + " | ".join(_ORDER) + " |",
        "| --- | " + " | ".join("---:" for _ in _ORDER) + " |",
    ]
    for theme, counter in per_theme_counts.items():
        lines.append(
            f"| {theme} | " + " | ".join(str(counter[k]) for k in _ORDER) + " |"
        )

    problems = [r for r in results if r["class"] in ("error", "nontabular")]
    if problems:
        lines += ["", "## Sorunlu örnekler", ""]
        for row in problems[:25]:
            lines.append(f"- [{row['class']}/{row['note']}] {row['theme']} — {row['name']}")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="TÜİK istab parse edilebilirlik sondası")
    parser.add_argument("--per-theme", type=int, default=8)
    parser.add_argument("--out", default="ISTAB_PROBE.md")
    parser.add_argument("--lang", default="tr", choices=["tr", "en"])
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    print(f"istab örneklemi indiriliyor (tema başına {args.per_theme})...")
    result = probe(per_theme=args.per_theme, lang=args.lang, seed=args.seed)
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(render(result))
    print(f"Rapor yazıldı: {args.out}")


if __name__ == "__main__":
    main()
