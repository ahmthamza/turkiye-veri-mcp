# istab Sonda Raporu — turkiye-veri-mcp (2026-08-02)

Örneklem: tema başına en fazla 8 dosya, toplam **139** istab dosyası indirilip açıldı.

| Sınıf | Adet | Pay | Anlamı |
| --- | ---: | ---: | --- |
| multiheader | 101 | %72.7 | aile başına tek kural ile parse edilir |
| crosstab | 36 | %25.9 | unpivot kuralı gerekir |
| nontabular | 2 | %1.4 | kapsam dışı (PDF/HTML/zip) |

**Şekilsel tavan:** 137/139 (%98.6 — indirilebilen dosyalar içinde) bilinen bir şablona uyuyor. Bu, dosyanın *şekline* bakan kaba bir ölçümdür; aşağıdaki 'Gerçek tidy başarısı' asıl cevaptır.

## Gerçek tidy başarısı (tuik_get_table_data ile)

İndirilen **139** dosyanın **132** tanesi (%95.0) `tuik_get_table_data` aracıyla bugün, kod değişikliği olmadan tidy uzun formata çevrildi. Bu şekilsel sınıflandırmadan farklı olarak gerçek aracın çalıştırılmasıyla ölçülmüştür.

Tidy edilemeyenler:

- Eğitim — İBBS 2. Düzey ve Eğitim Kurumlarına Göre Kursiyer Sayısı: no sheet could be tidied: 14_t21: no period columns/headers found, and no numeric value columns either
- İstihdam, İşsizlik ve Ücret — İşgücüne dahil olmama nedenleri: no sheet could be tidied: Tablo: no period columns/headers found, and no numeric value columns either
- Sağlık ve Sosyal Koruma — Bebeklerin anne sütü ile beslenme sürelerinin cinsiyete göre dağılımı: no sheet could be tidied: t19: no period columns/headers found, and no numeric value columns either
- Sağlık ve Sosyal Koruma — Kayıtlı olan engelli bireylerin kamu kurum ve kuruluşlarından beklentilerinin engel türüne göre dağılımı: no sheet could be tidied: 5: no period columns/headers found, and no numeric value columns either
- Sağlık ve Sosyal Koruma — Sosyal koruma kapsamında yardım ve maaş alan kişi sayısı: no sheet could be tidied: t5: no period columns/headers found, and no numeric value columns either
- Çok Boyutlu İstatistikler — İllere Göre Umut Düzeyi Haritası: file is not a readable Excel workbook (ValueError)
- Çok Boyutlu İstatistikler — İllere Göre Mutluluk Düzeyi Haritası: file is not a readable Excel workbook (ValueError)

## Tema bazında

| Tema | clean | multiheader | multisheet | crosstab | nontabular | error |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Adalet ve Seçim | 0 | 4 | 0 | 4 | 0 | 0 |
| Bilim, Teknoloji ve Bilgi Toplumu | 0 | 3 | 0 | 5 | 0 | 0 |
| Eğitim | 0 | 6 | 0 | 2 | 0 | 0 |
| Enerji | 0 | 3 | 0 | 0 | 0 | 0 |
| Fiyat İstatistikleri | 0 | 5 | 0 | 3 | 0 | 0 |
| Gelir, Tüketim ve Yoksulluk | 0 | 7 | 0 | 1 | 0 | 0 |
| İstihdam, İşsizlik ve Ücret | 0 | 7 | 0 | 1 | 0 | 0 |
| Kısa Dönemli Ekonomik Göstergeler | 0 | 5 | 0 | 3 | 0 | 0 |
| Kültür ve Spor | 0 | 6 | 0 | 2 | 0 | 0 |
| Nüfus ve Demografi | 0 | 6 | 0 | 2 | 0 | 0 |
| Sağlık ve Sosyal Koruma | 0 | 7 | 0 | 1 | 0 | 0 |
| Tarım | 0 | 7 | 0 | 1 | 0 | 0 |
| Turizm | 0 | 8 | 0 | 0 | 0 | 0 |
| Ulaştırma ve Haberleşme | 0 | 5 | 0 | 3 | 0 | 0 |
| Ulusal Hesaplar | 0 | 8 | 0 | 0 | 0 | 0 |
| Uluslararası Ticaret | 0 | 8 | 0 | 0 | 0 | 0 |
| Yapısal Ekonomik Göstergeler ve İş Demografisi | 0 | 3 | 0 | 5 | 0 | 0 |
| Çok Boyutlu İstatistikler | 0 | 3 | 0 | 3 | 2 | 0 |

## Sorunlu örnekler

- [nontabular/pdf] Çok Boyutlu İstatistikler — İllere Göre Umut Düzeyi Haritası
- [nontabular/pdf] Çok Boyutlu İstatistikler — İllere Göre Mutluluk Düzeyi Haritası
