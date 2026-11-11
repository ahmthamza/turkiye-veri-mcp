# istab Ölçüm Raporu — turkiye-veri-mcp (2026-11-11)

Diskteki **1700** istab dosyası `tidy_istab` ile çevrimdışı ölçüldü (indirme denemesi başarısız olan: 4; katalogun geri kalanı için aşağıdaki 'Kapsam' bölümüne bakın).

## Sonuç dağılımı

| Sonuç | Adet | Pay |
| --- | ---: | ---: |
| yüksek | 459 | %27.0 |
| orta | 1152 | %67.8 |
| kısmi | 19 | %1.1 |
| reddedildi | 70 | %4.1 |

Güven düzeyleri: **yüksek** = düz ızgara; etiketler doğrudan başlık hücrelerinden (en fazla 2 başlık satırı), dönem tablonun içinden; **orta** = tüm sayfalar okundu ama dönem başlık/sayfa adı/katalogdan alındı ya da bulunamadı (`donem_kaynagi`) veya yapı çıkarımla kuruldu (`yapi_cikarimi`); **kısmi** = bazı sayfa/satır/sütunlar atlandı (listelenir); **reddedildi** = güvenle okunamadı, sayı döndürülmedi.

## Tasarım kümesi ve ayrılmış (held-out) küme

URL'nin özetine göre rastgele %15'lik bir küme (248 dosya) kurallar tasarlanırken hiç incelenmedi; sonuçlar ayrı raporlanır.

| Sonuç | Tasarım | Ayrılmış |
| --- | ---: | ---: |
| yüksek | 394 (%27.1) | 65 (%26.2) |
| orta | 985 (%67.8) | 167 (%67.3) |
| kısmi | 15 (%1.0) | 4 (%1.6) |
| reddedildi | 58 (%4.0) | 12 (%4.8) |

## Dönemin kaynağı (sayfa başına)

| Kaynak | Sayfa |
| --- | ---: |
| tablo | 1169 |
| başlık | 277 |
| yok | 114 |
| katalog yolu | 62 |
| katalog adı | 19 |
| sayfa adı | 1 |

## Ret nedenleri

| Neden | Adet |
| --- | ---: |
| labels do not tell values apart (…) | 57 |
| no sheet holds numeric data | 13 |

## Dosya türleri

| Tür | Adet |
| --- | ---: |
| xls | 1693 |
| xlsx | 7 |

## İndirilemeyenler

- Fiyat İstatistikleri — Üretici Fiyat Endeksi- NACE Rev.2 Kodlarının Eski Serilerdeki Karşılıkları: PortalError: TÜİK portalı 180 sn içinde dosyayı göndermedi.
- İstihdam, İşsizlik ve Ücret — Ekonomik Faaliyete Göre İş Kazası Geçiren veya İşe Bağlı Sağlık Sorununa Maruz Kalanlar: PortalError: TÜİK portalı 180 sn içinde dosyayı göndermedi.
- İstihdam, İşsizlik ve Ücret — Kendi hesabına veya işveren olarak çalışanların çalışmaya başlama nedeni: PortalError: TÜİK portalı 180 sn içinde dosyayı göndermedi.
- Nüfus ve Demografi — İl ve cinsiyete göre ortalama evlenme yaşı: PortalError: TÜİK portalı 180 sn içinde dosyayı göndermedi.

## Kapsam

Katalogdaki 1.746 istab bağlantısının **1.700**'ü diskte ölçüldü; kalan 46 dosya indirme
iş sahibinin isteğiyle durdurulduğu için **indirilmedi** (ölçüme dahil değil). Ölçüm
`tidy_istab`'ı çevrimdışı çalıştırır; canlı aracın aynı kodu kullanır.

## Önceki sürümle (v0.35, git HEAD) karşılaştırma — aynı 1.700 dosya

| Sonuç | Önceki (tümü) | Yeni (tümü) | Önceki tasarım / ayrılmış | Yeni tasarım / ayrılmış |
| --- | ---: | ---: | ---: | ---: |
| yüksek | 906 | 459 | 769 / 137 | 394 / 65 |
| orta | 758 | 1.152 | 654 / 104 | 985 / 167 |
| kısmi | — | 19 | — | 15 / 4 |
| reddedildi | 36 | 70 | 29 / 7 | 58 / 12 |

"yüksek" artık daha dar ve anlamlı: yalnızca düz ızgara (en fazla 2 başlık satırı,
bölüm hiyerarşisi/varsayım yok, dönem tablonun içinden). Çıkarımla kurulan her yapı
`orta`dır ve `yapi_cikarimi` alanında listelenir.

Değer düzeyinde (tasarım kümesi, iki sürümün de çevirdiği 1.384 dosya): 1.226 dosyada
değer kümesi aynı; 18 dosyada yalnızca yeni değer eklendi; 118 dosyada bazı değerler artık
verilmiyor — bunlar önceki sürümün veri sandığı başlık yılları, sütun/satır numaraları,
kod sütunları (CPA/ISCO/NACE), yıl bölüm satırları, dipnot satırları ve artık raporlanan
atlamalar (başlıksız sütun, kaynakta iki kez yazılmış satır). Ayrıca tüm sayfalar için
denetlendi: değer satırı × değer sütunundaki her sayı ya çıktıda ya da `atlananlar`'da.
Önceki sürümün çevirip yenisinin reddettiği 49 (+ayrılmış 9) dosyanın 47'si (+9) önceki
sürümde zaten **aynı etiketle iki farklı sayı** veriyordu (sessiz belirsizlik); kalan 2'si
sayısal veri içermeyen madde/kod listesidir (önceki sürüm kodları değer sanıyordu).

## Bağımsız değer izleme (value trace)

Dosya başına 4 rastgele çıktı değeri, ham dosyada (xlrd/openpyxl, birleştirilmiş hücreler
dahil; dönüştürücünün kodu kullanılmadan) kaynak hücresine kadar izlendi; her etiketin o
hücrenin satır/sütun/başlık bağlamında bulunması ve etiketlerin hücreyi tek başına
belirlemesi denetlendi.

| | Önceki sürüm | Yeni sürüm |
| --- | ---: | ---: |
| Tasarım: tam doğrulanan | 4.483 / 5.682 (%78,9) | 4.874 / 5.569 (%87,5) |
| Ayrılmış: tam doğrulanan | 772 / 960 (%80,4) | 817 / 943 (%86,6) |
| Tasarım: aynı etiket bağlamı birden çok farklı sayıyı kapsıyor | 846 | 323 |

Yeni sürümde doğrulanamayan değerlerin dökümü (tasarım 695, ayrılmış 126): kanıtlanmış
açıklamalar — kaynak satır gösterilerek doğrulanmış başlık/blok etiketi (107), toplam/üst
satır ya da başlıksız ilk blok olarak ayrışan (247+42), beyan edilen birim/TR-EN/sarılı
etiket birleştirmeleri (44+2), tek başlıksız değer sütunu yer tutucusu (43+11), izleyicinin
bilinen sınırları (sayı başlığı satırdaki veriyle eşleşiyor; matrisin iki ekseninde aynı adlar;
veri olmayan rakip hücre) (39+16), önceki yöntemle okunan sayfalar (49+18, `orta`, etiketleri
doğrulanmadı). **Açıklanamayan kalan: tasarım 167, ayrılmış 26 değer** (başlık kapsamı/yakın
başlık atlanmış olabilir, etiket kaynakta bulunamadı vb.); bunların **hiçbiri `yüksek`
dosyada değil** — hepsi `orta`/`kısmi` olarak ve `yapi_cikarimi` notuyla döner.
