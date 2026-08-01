# Sürüm notları

## v0.15.0
Sen istemeden, kodu tarayıp bulduğum iki ek hata:

- **tidy.py — başlıksız ilk gösterge bloğu:** Bir sayfada birden fazla gösterge varsa ama İLK göstergenin başlık satırı yoksa (yalnızca sonraki göstergeler birleştirilmiş hücre başlığıyla ayrılmışsa), o ilk bloğun satırları gösterge adı yerine gerçek `NaN` alıyordu. Artık başlığı olmayan bloklar orijinal sütun adını koruyor, veri kaybı yok.
- **evds.py — çoklu seri/uzun dönem birleştirmesinde sabit sütun adı varsayımı:** `evds_get_data`/`evds_download_data` çok seri veya uzun tarih aralığını otomatik parçalarken, parçaları tarihe göre birleştirmek için sütun adının `"Tarih"` veya `"YEARWEEK"` olduğunu varsayıyordu. Farklı bir isim gelirse (doğrulanmamış bir varsayımdı) sessizce yan yana yapıştırmaya düşüp, farklı tarih aralıklı serilerde **yanlış tarihe yanlış değer eşleştirebilirdi**. Artık sütun adı sabit değil, her seferinde dinamik tespit ediliyor; farklı uzunluktaki seriler doğru şekilde eksik (NaN) bırakılıyor, yanlış hizalanmıyor.

Sekiz regresyon testi (tidy.py) ve iki hedefli test (evds.py) ile doğrulandı.

## v0.14.0
- **Yeni özellik: çok göstergeli blok tespiti.** TÜİK'in bazı Excel dosyaları (ör. "Temel işgücü göstergeleri") tek sayfada birden fazla göstergeyi (işgücü sayısı, işsizlik oranı, istihdam oranı vb.) art arda bloklar halinde yayınlıyor; her blok birleştirilmiş bir hücreden gelen tek satırlık başlıkla ayrılıyor. `tuik_get_table_data` artık bu başlıkları tanıyıp `gosterge` sütununa doğru gösterge adını yazıyor — önceden bu sayılar tek bir isimsiz sütuna (`kolon_2`) düşüp gösterge kimliği kayboluyordu.
- Tespit kriteri sıkı tutuldu: yalnızca ilk sütunu dolu, geri kalanı tamamen boş olan satırlar "bölüm başlığı" sayılıyor (Excel'deki birleştirilmiş hücrelerin okunma şekli). Metni tüm sütunlara tekrarlayan artefakt satırlar (ör. "Yıllar - Years") yanlışlıkla bölüm sanılmıyor.
- Bölümsüz sıradan tablolarda davranış tamamen değişmedi (7 regresyon testi doğrulandı).

## v0.13.0
- **Hata düzeltmesi (hosted dağıtım):** `render.yaml`'daki sağlık kontrolü `/mcp` adresine düz GET atıyordu; MCP streamable-http protokolü oturumsuz GET'e 400 döndürdüğü için Render'ın sağlık kontrolü sürekli başarısız görünüyordu (loglarda tekrar eden 400 Bad Request). Artık MCP protokolünden bağımsız bir `/healthz` endpoint'i var, `render.yaml` oraya yönlendiriyor.

## v0.12.0
- `turkiye-veri-probe`: 429 (Too Many Requests) alındığında üstel geri çekilmeyle 3 deneme yapıyor. Gerçek kullanım (tek tablo indirme) hiç etkilenmiyordu; bu yalnızca sondanın toplu/hızlı indirme deseninde TÜİK'i hız sınırına takılmaktan kurtarıyor.
- 173 dosyalık ikinci canlı ölçüm: **%94,6 gerçek tidy başarısı**. Kalan 8 istisna (seyrek/isimsiz tablolar) ve 1 nontabular (PDF harita görseli) kabul edilebilir kapsam dışı durumlar.

## v0.11.0
- **Kritik hata düzeltmesi:** `tuik_get_table_data` bazı tablolarda tekrarlı sütun başlığı (ör. iki "Toplam" sütunu) olduğunda pandas Seri karşılaştırma hatasıyla çöküyordu. Bu, büyük örneklemli probe testinde bazı temaların (Turizm, Ulaştırma, Ulusal Hesaplar vb.) %100 hata vermesinin sebebiydi. Sütun adları artık otomatik benzersizleştiriliyor.
- **Kapsam genişletmesi:** Dönem/zaman boyutu içermeyen "tek dönemlik" (snapshot) tablolar — kategori + değer, yıl sütunu olmayan — artık tidy'leniyor. Önceden "no period columns found" hatasıyla reddediliyordu.

## v0.10.0
- `turkiye-veri-probe`: artık şekilsel sınıflandırmanın yanında gerçek `tuik_get_table_data` çağrısını da çalıştırıp asıl tidy başarı oranını ölçüyor ("Gerçek tidy başarısı" bölümü). İlk canlı ölçüm: 53 indirilebilen dosyanın hepsi tidy'lendi.

## v0.9.0
- **Hata düzeltmesi:** istab dosya indirmeleri (`tuik_download_table_file`, `tuik_get_table_data`, `turkiye-veri-probe`) artık tarayıcı benzeri User-Agent + Referer header'ı gönderiyor. Önceki sürümde çıplak istekler TÜİK sunucusu tarafından reddediliyor olabilirdi (probe'un ilk canlı çalıştırmasında tüm indirmeler başarısız oldu).
- `turkiye-veri-probe`: tüm indirmeler başarısız olsa bile artık çökmeden rapor üretiyor (önceki sürümde sıfıra bölme hatası vardı) ve her hatanın tam mesajını gösteriyor.

## v0.7.0
- Kataloglar artık zaman aşımına bağlı: TÜİK tema ağacı ve EVDS veri grubu listesi en fazla 1 saat önbellekte kalıyor, uzun süre çalışan sunucu yeni yayınlanan tabloları kaçırmıyor.
- EVDS seri arama indeksi 7 günden eskiyse kendini otomatik yeniliyor; `evds_search_series(refresh=True)` ile anında yenilenebiliyor. Çıktıda `index_age_days` dönüyor.

## v0.6.0
- HTTP transport (`--transport http`): sunucu internete açık bir adreste çalışabiliyor, claude.ai custom connector olarak eklenebiliyor.
- Dockerfile, render.yaml, .gitignore — GitHub + hosted dağıtım için hazır.

## v0.5.0
- `tuik_get_table_data`: istab Excel'lerini tidy uzun formata çeviren yeni araç (başlık/dipnot satırları, çok sayfalı dosyalar, çapraz tablolar, TR sayı formatı, "-"/".." eksik hücreleri). Her çıktıda `tidy_confidence`.
- `evds_search_series`: 52 bin serinin adında arama; ilk çağrıda yerel indeks kurulur (`~/.cache/turkiye-veri-mcp/`), sonrasında anında.
- `evds_get_datagroup_data`: bir veri grubunun tüm serilerini tek çağrıda çeker.
- `evds_get_data` / `evds_download_data` artık otomatik parçalıyor (seri başına 8'li gruplar, 10 yıllık pencereler) — uzun ve geniş sorgular sessizce kesilmiyor.
- CONTRIBUTING.md: yeni sağlayıcı ekleme rehberi ve modül şablonu.
- README'ye "Bilinen sınırlar" bölümü.

## v0.4.0
- `turkiye-veri-probe`: istab dosyalarının parse edilebilirliğini ölçen sonda aracı.

## v0.3.0
- `turkiye-veri-audit`: kaynak bazlı kapsam raporu (COVERAGE.md).
- `tuik_list_tables` artık MEDAS/Biruni veritabanlarını da listeliyor.

## v0.2.0
- TÜİK ve EVDS tek sunucuda birleştirildi.

## v0.1.0
- TÜİK (veriportali + SDMX) ilk sürüm.

## v0.8.0
- `usage_stats` aracı: bu sunucu örneğinin araç-çağrısı sayacı (kişi sayısı değil, aktivite göstergesi). Kişi başına bağlantı verisi Anthropic tarafında olduğundan bu, sahibe açık en yakın alternatif.
