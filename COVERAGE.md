# Kapsam Raporu — turkiye-veri-mcp (2026-11-11)

Erişim katmanları: **A** = tam programatik (tidy veri), **B** = dosya indirme (tidy garanti değil), **C** = listelenir ama veri çekilmez (MEDAS/Biruni).

## TÜİK (veriportali.tuik.gov.tr)

Portalda sayılan veri kaynağı: **2361** (+ 170 haber bülteni, 55 rapor — veri değil, kapsam dışı)

- Tier A — SDMX dataflow: **515** (%21.8) — tüm kırılımlar ve tüm dönem tidy erişilir (DataBrowser2; filtre/dönem sunucuda uygulanır)
- Tier B — istab dosya: **1746** (%74.0) — dosya indirilir
- Tier C — MEDAS/Biruni veritabanı: **100** (%4.2) — yalnızca link listelenir (MEDAS/Biruni durum tutan ZK web arayüzü; veri API'si yok)

| Tema | dataflow | istab | database |
| --- | --- | --- | --- |
| Adalet ve Seçim | 0 | 25 | 6 |
| Bilim, Teknoloji ve Bilgi Toplumu | 42 | 50 | 1 |
| Çevre | 37 | 0 | 4 |
| Eğitim | 35 | 22 | 2 |
| Enerji | 2 | 3 | 1 |
| Fiyat İstatistikleri | 31 | 77 | 8 |
| Gelir, Tüketim ve Yoksulluk | 0 | 103 | 3 |
| İstihdam, İşsizlik ve Ücret | 31 | 245 | 8 |
| Kısa Dönemli Ekonomik Göstergeler | 29 | 71 | 16 |
| Kültür ve Spor | 12 | 38 | 4 |
| Nüfus ve Demografi | 152 | 587 | 20 |
| Sağlık ve Sosyal Koruma | 0 | 60 | 1 |
| Tarım | 12 | 117 | 9 |
| Turizm | 0 | 30 | 2 |
| Ulaştırma ve Haberleşme | 37 | 37 | 2 |
| Ulusal Hesaplar | 11 | 106 | 3 |
| Uluslararası Ticaret | 27 | 27 | 4 |
| Yapısal Ekonomik Göstergeler ve İş Demografisi | 56 | 114 | 4 |
| Çok Boyutlu İstatistikler | 1 | 34 | 2 |

## TCMB EVDS (evds3.tcmb.gov.tr)

- Kategori: **154** (11 üst düzey) — tümü erişilebilir
- Veri grubu: **680** — tümü erişilebilir
- Seri: **54.075** (hepsi benzersiz kod; seri listesi alınamayan veri grubu yok) — tümü erişilebilir

Sayılar 2026-11-11'de arama dizini kurulurken canlı API'den sayılmıştır. EVDS web sitesi aynı API'nin istemcisidir.

## BDDK (bddk.org.tr)

Beş etkileşimli bülten bağlı: FinTürk (7 tablo, çeyreklik, il bazında), Aylık Bülten (17 tablo), Haftalık Bülten (9 tablo), Günlük Bülten, banka dışı mali kuruluşlar (4 sektör). Bağlı olmayanlar: kredi kartı ücretleri bülteni, "Üç Aylık Temel Göstergeler".

## HMB (muhasebat.hmb.gov.tr)

Sitede sayılan: **2.369** klasör, **6.246** dosya (6.050 xls, 59 xlsx, 137 pdf). Tamamı `hmb_list_tables` / `hmb_list_files` / `hmb_get_file` ile erişilebilir (ham dosya); örneklenen 854 tablo türünden 298'i tidy tabloya çevrilir. İl bazlı aileler (`hmb_get_data`, `hmb_get_karsilastirma`, `hmb_get_paylar`) tüm yıllar için doğrulanmıştır.
