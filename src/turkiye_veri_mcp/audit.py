"""Coverage audit: count what the sources publish vs what this MCP serves.

Run on a machine with internet access (and EVDS_API_KEY for the EVDS side):

    turkiye-veri-audit                # writes COVERAGE.md
    turkiye-veri-audit --out rapor.md

The report classifies TUIK portal content into three access tiers:

- Tier A (tam programatik): SDMX dataflows via TUIK DataBrowser2 -> tidy
  DataFrame/CSV, all dimensions and periods, filtered server-side.
- Tier B (dosya erisimi): 'istab' downloads -> file retrieved, tidy not
  guaranteed (Excel layouts vary).
- Tier C (listelenir, veri cekilmez): 'database' nodes -> legacy MEDAS
  (Biruni) databases; we expose their links but do not fetch from them:
  MEDAS is a stateful ZK 8.5 web application (all interaction is
  server-side component events over /medas/zkau, no data API) with a
  50,000-cell query cap, so it cannot be queried robustly.

EVDS coverage is by construction the full API surface (the EVDS website is
itself a client of the same API); the audit verifies this with live counts
of categories, datagroups and series.
"""

from __future__ import annotations

import argparse
from datetime import date

from turkiye_veri_mcp.evds import EvdsClient, EvdsError
from turkiye_veri_mcp.portal import PortalClient

_TUIK_TYPES = ("dataflow", "istab", "database", "press", "report")


def tuik_inventory(lang: str = "tr") -> dict:
    portal = PortalClient()
    themes = portal.list_themes(lang=lang)
    per_theme: list[dict] = []
    totals = {t: 0 for t in _TUIK_TYPES}
    for theme in themes:
        rows = portal.list_resources(theme["theme_id"], lang=lang, types=_TUIK_TYPES)
        counts = {t: 0 for t in _TUIK_TYPES}
        for row in rows:
            counts[row["type"]] += 1
            totals[row["type"]] += 1
        per_theme.append({**theme, **counts})
    return {"themes": per_theme, "totals": totals}


def evds_inventory(delay: float = 0.4, refresh: bool = False) -> dict:
    """Live EVDS counts. Series are counted from the series-search index
    (one serieList request per datagroup; resumable, failures retried once
    and listed), so an audit right after an index build costs 3 requests."""
    client = EvdsClient()
    categories = client.categories()
    datagroups = client.datagroups(None)
    per_category: dict[str, int] = {}
    for group in datagroups:
        key = str(group["category_id"])
        per_category[key] = per_category.get(key, 0) + 1
    series = client.build_series_index(progress=True, delay=delay, refresh=refresh)
    status = client.index_status()
    return {
        "n_categories": len(categories),
        "n_top_categories": sum(1 for c in categories if c.get("level") == 1),
        "n_datagroups": len(datagroups),
        "n_series": len(series),
        "n_series_unique": len({s["serie_code"] for s in series}),
        "datagroups_per_category": per_category,
        "serie_list_failures": status["index_failed_datagroups"],
        "index_age_days": status["index_age_days"],
    }


def render_report(tuik: dict | None, evds: dict | None, evds_error: str = "") -> str:
    lines: list[str] = [
        f"# Kapsam Raporu — turkiye-veri-mcp ({date.today().isoformat()})",
        "",
        "Erişim katmanları: **A** = tam programatik (tidy veri), "
        "**B** = dosya indirme (tidy garanti değil), "
        "**C** = listelenir ama veri çekilmez (MEDAS/Biruni).",
        "",
    ]

    if tuik:
        totals = tuik["totals"]
        n_all = sum(totals[t] for t in ("dataflow", "istab", "database"))
        lines += [
            "## TÜİK (veriportali.tuik.gov.tr)",
            "",
            f"Portalda sayılan veri kaynağı: **{n_all}** "
            f"(+ {totals['press']} haber bülteni, {totals['report']} rapor — veri değil, kapsam dışı)",
            "",
            f"- Tier A — SDMX dataflow: **{totals['dataflow']}** "
            f"(%{100 * totals['dataflow'] / n_all:.1f}) — tüm kırılımlar ve tüm dönem tidy erişilir "
            "(DataBrowser2; filtre/dönem sunucuda uygulanır)",
            f"- Tier B — istab dosya: **{totals['istab']}** "
            f"(%{100 * totals['istab'] / n_all:.1f}) — dosya indirilir",
            f"- Tier C — MEDAS/Biruni veritabanı: **{totals['database']}** "
            f"(%{100 * totals['database'] / n_all:.1f}) — yalnızca link listelenir "
            "(MEDAS/Biruni durum tutan ZK web arayüzü; veri API'si yok)",
            "",
            "| Tema | dataflow | istab | database |",
            "| --- | --- | --- | --- |",
        ]
        for theme in tuik["themes"]:
            lines.append(
                f"| {theme['theme_name']} | {theme['dataflow']} "
                f"| {theme['istab']} | {theme['database']} |"
            )
        lines.append("")

    lines.append("## TCMB EVDS (evds3.tcmb.gov.tr)")
    lines.append("")
    if evds:
        lines += [
            f"- Kategori: **{evds['n_categories']}** "
            f"({evds.get('n_top_categories', '?')} ana kategori, 3 seviyeli ağaç) — tümü erişilebilir",
            f"- Veri grubu: **{evds['n_datagroups']}** — tümü erişilebilir",
            f"- Seri: **{evds['n_series']}** "
            f"({evds.get('n_series_unique', evds['n_series'])} tekil kod) — tümü erişilebilir "
            "(8 frekans, 6 agregasyon, 8 dönüşüm formülü dahil; EVDS'nin 1000 satır sınırı "
            "sayfalanarak aşılır)",
        ]
        if evds["serie_list_failures"]:
            lines.append(
                f"- Seri listesi alınamayan veri grubu: "
                f"{len(evds['serie_list_failures'])} "
                f"({', '.join(evds['serie_list_failures'][:10])}...)"
            )
        lines += [
            "",
            "EVDS web sitesi aynı API'nin istemcisidir; API'nin sunduğu her seri "
            "bu MCP'den erişilebilir. Yukarıdaki sayılar canlı API'den sayılmıştır.",
        ]
    else:
        lines.append(f"EVDS envanteri alınamadı: {evds_error}")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="turkiye-veri-mcp kapsam denetimi")
    parser.add_argument("--out", default="COVERAGE.md")
    parser.add_argument("--lang", default="tr", choices=["tr", "en"])
    parser.add_argument("--skip-evds", action="store_true")
    parser.add_argument("--skip-tuik", action="store_true")
    args = parser.parse_args()

    tuik = None
    if not args.skip_tuik:
        print("TÜİK envanteri sayılıyor (19 tema)...")
        tuik = tuik_inventory(lang=args.lang)

    evds, evds_error = None, ""
    if not args.skip_evds:
        print("EVDS envanteri sayılıyor (veri grubu başına bir istek, sürebilir)...")
        try:
            evds = evds_inventory()
        except EvdsError as exc:
            evds_error = str(exc)

    report = render_report(tuik, evds, evds_error)
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(report)
    print(f"Rapor yazıldı: {args.out}")


if __name__ == "__main__":
    main()
