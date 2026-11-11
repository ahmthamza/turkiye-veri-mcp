# istab Sonda Raporu — turkiye-veri-mcp (2026-11-11)

Örneklem: tema başına en fazla 8 dosya, toplam **139** istab dosyası indirilip açıldı.

| Sınıf | Adet | Pay | Anlamı |
| --- | ---: | ---: | --- |
| multiheader | 109 | %78.4 | aile başına tek kural ile parse edilir |
| multisheet | 1 | %0.7 | döngü + aynı kural ile parse edilir |
| crosstab | 24 | %17.3 | unpivot kuralı gerekir |
| error | 5 | %3.6 | indirme/açma hatası — ayrıca incelenmeli |

**Şekilsel tavan:** 134/134 (%100.0 — indirilebilen dosyalar içinde) bilinen bir şablona uyuyor. Bu, dosyanın *şekline* bakan kaba bir ölçümdür; aşağıdaki 'Gerçek tidy başarısı' asıl cevaptır.

## Gerçek tidy başarısı (tuik_get_table_data ile)

İndirilen **134** dosyanın **131** tanesi (%97.8) `tuik_get_table_data` aracıyla tidy uzun formata çevrildi (güven: yüksek 80, orta 51). Kalanlar açık bir hata ile reddedildi (yanlış sayı döndürmek yerine). Bu, dosya şekline bakan sınıflandırmadan farklı olarak gerçek aracın çalıştırılmasıyla ölçülmüştür; sayıların doğruluğunu tek tek kanıtlamaz.

Tidy edilemeyenler:

- Eğitim — Cinsiyet, yaş grubu ve ayrıntılı nedenlere göre yaygın eğitime katılım: no sheet could be tidied: t3: value column 'Yaş grubu-Age group' holds only year numbers; the layout was not recognised safely
- Fiyat İstatistikleri — Hizmet Üretici Fiyat Endeksi- Kısım ve Bölümler (2017=100) CPA 2.1: no sheet could be tidied: 191_t: value column 'Services producer price index, 2017=100, by sections and div' holds only year numbers; the layout was not recognised safely
- Fiyat İstatistikleri — Yurt İçi Üretici Fiyat Endeksi- Kısım, Bölüm ve Gruplar (2003=100) CPA 2008: no sheet could be tidied: 18_t13: value column 'Domestic producer price index, 2003=100, historic series by ' holds only year numbers; the layout was not recognised safely

## Tema bazında

| Tema | clean | multiheader | multisheet | crosstab | nontabular | error |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Adalet ve Seçim | 0 | 3 | 0 | 0 | 0 | 5 |
| Bilim, Teknoloji ve Bilgi Toplumu | 0 | 6 | 0 | 2 | 0 | 0 |
| Eğitim | 0 | 7 | 0 | 1 | 0 | 0 |
| Enerji | 0 | 3 | 0 | 0 | 0 | 0 |
| Fiyat İstatistikleri | 0 | 5 | 0 | 3 | 0 | 0 |
| Gelir, Tüketim ve Yoksulluk | 0 | 7 | 0 | 1 | 0 | 0 |
| İstihdam, İşsizlik ve Ücret | 0 | 5 | 1 | 2 | 0 | 0 |
| Kısa Dönemli Ekonomik Göstergeler | 0 | 8 | 0 | 0 | 0 | 0 |
| Kültür ve Spor | 0 | 6 | 0 | 2 | 0 | 0 |
| Nüfus ve Demografi | 0 | 7 | 0 | 1 | 0 | 0 |
| Sağlık ve Sosyal Koruma | 0 | 7 | 0 | 1 | 0 | 0 |
| Tarım | 0 | 7 | 0 | 1 | 0 | 0 |
| Turizm | 0 | 8 | 0 | 0 | 0 | 0 |
| Ulaştırma ve Haberleşme | 0 | 6 | 0 | 2 | 0 | 0 |
| Ulusal Hesaplar | 0 | 8 | 0 | 0 | 0 | 0 |
| Uluslararası Ticaret | 0 | 7 | 0 | 1 | 0 | 0 |
| Yapısal Ekonomik Göstergeler ve İş Demografisi | 0 | 4 | 0 | 4 | 0 | 0 |
| Çok Boyutlu İstatistikler | 0 | 5 | 0 | 3 | 0 | 0 |

## Sorunlu örnekler

- [error/PortalError: TÜİK portalı indirme hız sınırına takıldı (5 sn'de bir dosya); birkaç saniye sonra tekrar deneyin.] Adalet ve Seçim — İl Genel Meclisi Üyeleri Seçimi Sonuçları (1963-1977)
- [error/PortalError: TÜİK portalı indirme hız sınırına takıldı (5 sn'de bir dosya); birkaç saniye sonra tekrar deneyin.] Adalet ve Seçim — Mahalli İdareler Seçimine Katılan Siyasi Partilerin ve Bağımsızların Aldıkları Oy Sayısı Ve Oranı 
- [error/PortalError: TÜİK portalı indirme hız sınırına takıldı (5 sn'de bir dosya); birkaç saniye sonra tekrar deneyin.] Adalet ve Seçim — Siyasi Parti ve Bağımsızların Kazandıkları Milletvekili Sayısı
- [error/PortalError: TÜİK portalı indirme hız sınırına takıldı (5 sn'de bir dosya); birkaç saniye sonra tekrar deneyin.] Adalet ve Seçim — Seçim Yılına Göre Kayıtlı Seçmen ve Oy Kullanan Seçmen Sayısı
- [error/PortalError: TÜİK portalı indirme hız sınırına takıldı (5 sn'de bir dosya); birkaç saniye sonra tekrar deneyin.] Adalet ve Seçim — İl Genel Meclisi Üyeleri Seçimi Sonuçları (2014)
