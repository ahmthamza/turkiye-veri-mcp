# Kapsam Raporu — turkiye-veri-mcp (2026-08-02)

Erişim katmanları: **A** = tam programatik (tidy veri), **B** = dosya indirme (tidy garanti değil), **C** = listelenir ama veri çekilmez (MEDAS/Biruni).

## TÜİK (veriportali.tuik.gov.tr)

Portalda sayılan veri kaynağı: **2206** (+ 169 haber bülteni, 55 rapor — veri değil, kapsam dışı)

- Tier A — SDMX dataflow: **366** (%16.6) — tüm kırılımlar ve tüm dönem tidy erişilir
- Tier B — istab dosya: **1739** (%78.8) — dosya indirilir
- Tier C — MEDAS/Biruni veritabanı: **101** (%4.6) — yalnızca link listelenir

| Tema | dataflow | istab | database |
| --- | --- | --- | --- |
| Adalet ve Seçim | 0 | 25 | 6 |
| Bilim, Teknoloji ve Bilgi Toplumu | 37 | 50 | 1 |
| Çevre | 37 | 0 | 4 |
| Eğitim | 23 | 22 | 2 |
| Enerji | 0 | 3 | 1 |
| Fiyat İstatistikleri | 20 | 77 | 8 |
| Gelir, Tüketim ve Yoksulluk | 0 | 103 | 3 |
| İstihdam, İşsizlik ve Ücret | 13 | 244 | 8 |
| Kısa Dönemli Ekonomik Göstergeler | 29 | 71 | 16 |
| Kültür ve Spor | 15 | 38 | 4 |
| Nüfus ve Demografi | 87 | 587 | 20 |
| Sağlık ve Sosyal Koruma | 0 | 60 | 1 |
| Tarım | 8 | 117 | 9 |
| Turizm | 0 | 30 | 2 |
| Ulaştırma ve Haberleşme | 37 | 37 | 2 |
| Ulusal Hesaplar | 11 | 100 | 4 |
| Uluslararası Ticaret | 27 | 27 | 4 |
| Yapısal Ekonomik Göstergeler ve İş Demografisi | 22 | 114 | 4 |
| Çok Boyutlu İstatistikler | 0 | 34 | 2 |

## TCMB EVDS (evds3.tcmb.gov.tr)

EVDS envanteri alınamadı: EVDS_API_KEY is not set. Get a free key at https://evds3.tcmb.gov.tr (BENIM SAYFAM -> Kayit -> Profilim -> API Key) and set it as the EVDS_API_KEY environment variable in your MCP config.
