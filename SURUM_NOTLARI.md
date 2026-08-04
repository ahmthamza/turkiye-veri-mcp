# Sürüm notları

## v0.20.2 (dokümantasyon)
- README'deki "paylaşılan kotayla bağlan" (kendi EVDS anahtarını girmeden, sunucu sahibinin anahtarını kullanarak) komutu, artık asıl önerdiğimiz ve Mac'te test edilen `claude mcp add-json` sözdizimiyle tutarlı hale getirildi (önceden eski `--transport http` biçimindeydi).

## v0.20.1 (dokümantasyon — son teyit)
Kurulumu her platformda gerçekten doğruladım (Claude Code Mac/Windows, Claude Desktop, Claude web), iki gerçek sorun buldum ve README'yi buna göre düzelttim:

- **claude.ai web, custom connector'larda özel HTTP header desteklemiyor** (Anthropic'in kendi deposunda açık özellik talebi: anthropics/claude-ai-mcp#10). Web kullanıcıları kendi EVDS anahtarını veremez, otomatik paylaşılan kotayı kullanır — bu artık README'de açıkça yazıyor.
- **`claude mcp add --transport http ... --header "..."` komutunda header'ın bazen sessizce kaydedilmediği bilinen bir Claude Code hatası var** (anthropics/claude-code#17069, hâlâ açık). Bunun yerine Claude Code kurulum talimatı artık `claude mcp add-json` kullanıyor — tek parça JSON aldığı için hem bu hatayı hem Windows'taki `--` ayrıştırma hatasını (#15077) aynı anda atlıyor, ve Mac/Windows'ta harfi harfine aynı komut çalışıyor (PowerShell'de tek tırnak da literal string).
- Claude Desktop için ayrı iki yol belgelendi: Custom Connector (kolay, paylaşılan kota) ve `mcp-remote` köprüsü (Node.js gerektirir, kendi anahtarınızla).

## v0.20.0 — hosted sunucuda kişi başına EVDS anahtarı
- **Sorun:** MCP'yi herkese duyurunca, hosted (Render) bağlantı üzerinden gelen tüm EVDS çağrıları sunucu sahibinin tek anahtarını/kotasını paylaşıyordu — biri yoğun kullanırsa herkesin kotası tükenebilirdi.
- **Çözüm:** EVDS araçları artık `X-Evds-Api-Key` HTTP header'ını okuyor. Header varsa o anahtarla (izole, kendi kotasıyla) istek atılıyor; header yoksa sunucunun kendi `EVDS_API_KEY` ortam değişkenine düşülüyor (stdio/yerel kullanımda zaten header kavramı yok, davranış değişmedi).
- Gerçek HTTP isteğiyle iki yönde de doğrulandı: header'lı çağrı gönderilen anahtarla TCMB'ye gerçekten istek attı (sahte anahtar için TCMB'den gerçek ret geldi, sessiz düşme olmadı); header'sız çağrı sunucunun kendi ortam değişkenine doğru şekilde düştü.
- README, Claude Code için `--header "X-Evds-Api-Key: ANAHTARINIZ"` eklenmiş kurulum komutunu birincil yöntem olarak gösteriyor.

## v0.19.1 (dokümantasyon)
- **Windows kurulum kolaylığı:** Claude Code'un yerel (stdio) kurulum komutu (`claude mcp add ... -- uvx --from ...`) PowerShell'de bilinen bir hatadan dolayı "unknown option" veriyor (anthropics/claude-code#15077, #7672, #3825). README artık Claude Code için önce **hosted HTTP bağlantısını** (`claude mcp add --transport http turkiye-veri https://turkiye-veri-mcp.onrender.com/mcp`) öneriyor — kurulum gerektirmiyor, bu hataya hiç uğramıyor, Windows'ta da sorunsuz. Kendi EVDS anahtarını kullanmak isteyenler için yerel kurulum ve Windows'a özel `.bat` sarmalayıcı çözümü de belgelendi.

## v0.19.0 — SDMX 401 kalıcı olarak çözüldü
- **Kök neden bulundu:** `nsiws.tuik.gov.tr` (eski SDMX 2.1 servisi) TÜİK tarafından kullanımdan kaldırılmış/kısıtlanmış görünüyor — kullanıcı, tarayıcının Network sekmesinde TÜİK'in kendi yeni web arayüzünün (`databrowser2.tuik.gov.tr`) farklı bir API kullandığını yakaladı.
- **sdmx.py tamamen bu yeni API'ye taşındı:** `GET .../structure` (varsayılan seçim + boyutlar) ve `POST .../data` (JSON-stat 2.0 formatında veri) ile çalışıyor. Yanıt `pyjstat` ile parse ediliyor (seyrek/eksik hücreleri doğru işliyor).
- Gerçek yakalanan TÜİK verisiyle doğrulandı: İstanbul 2007 nüfusu (12.573.836) doğru geldi, boyutlar (REF_AREA, FREQ, INDICATOR, İKAMET_YERİ, ADNKS_GÖSTERGE, TIME_PERIOD) doğru ayrıştı.
- Eski `nsiws` yolu kod içinde fallback olarak korunuyor (yeni API başarısız olursa denenir) — TÜİK eski servisi geri açarsa otomatik devreye girer, dar SDMX key (ör. "TR.TR100") desteği de o zaman geri gelir.
- **Bilinen sınır:** Yeni API'de şimdilik yalnızca `key="ALL"` (tam varsayılan seçim) destekleniyor; daraltılmış key istekleri legacy'ye düşüyor (o da şu an kapalı olduğu için başarısız olur). Sonucu kendiniz filtrelemeniz gerekebilir.
- Yeni bağımlılık: `pyjstat`.


## v0.18.1 (hata düzeltmesi)
- v0.18.0'daki oturum-çerezi denemesi canlıda "Cannot open a client instance more than once" hatasıyla çöküyordu — `_primed_client` içinde çerez almak için client zaten kullanılmışken `fetch_data`/`fetch_structure` onu tekrar `with` ile açmaya çalışıyordu. `with` yerine `try/finally` + `client.close()` kullanılarak düzeltildi. Bu bir kod hatasıydı, 401 teorisiyle ilgisi yok — asıl teori (oturum çerezi) hâlâ test edilmeyi bekliyor.

## v0.18.0 (denenmemiş — canlı test gerekiyor)
- **sdmx.py — 401 için ikinci deneme: oturum çerezi.** `tuikr` R paketinin kaynağı incelendiğinde, SDMX çağrısından önce hep portal (katalog) çağrısı yapıldığı ve R'nin HTTP istemcisinin çerezleri oturum boyunca taşıdığı görüldü. Bu, nsiws.tuik.gov.tr'nin de veriportali ile aynı oturum çerezini beklediği ihtimalini düşündürüyor. `sdmx.py` artık SDMX isteğinden önce `portal.py`'nin katalog sayfasını ziyaret edip aynı `httpx.Client` (ve çerezlerini) SDMX isteğinde de kullanıyor.
- **Doğrulanmadı:** Bu teori sandbox'ta test edilemedi (TÜİK'e ağ erişimi yok). Deploy sonrası `tuik_describe_dataflow` ile gerçek sonucu görmemiz gerekiyor — 401 hâlâ dönerse, sorun muhtemelen header/çerez dışı bir şey (IP kısıtlaması, dokümante edilmemiş anahtar gereksinimi vb.).

## v0.17.0
- **sdmx.py — eksik User-Agent düzeltmesi tamamlandı.** GitHub'daki repoda bu düzeltmenin daha önce planlandığı ama hiç uygulanmadığı tespit edildi (nsiws.tuik.gov.tr'ye giden isteklerde hâlâ 401 dönüyordu). Artık `portal.py`'deki tarayıcı User-Agent'ı SDMX isteklerine de ekleniyor.
- **tidy.py — çok satırlı başlık birleştirme tamamlandı.** GitHub'da başka bir oturumdan gelen kısmi bir çok-satırlı-header girişimi vardı (find_header_block/_combine_header_rows), ama iki gerçek hatası tespit edilip düzeltildi:
  1. `_clean_number`, uzun metin etiketlerindeki (ör. "15 ve daha yukarı yaştaki nüfus...") gömülü rakamları yanlışlıkla sayı sanıyordu (ör. "1515" — aylardır görülen gizemli değerin kaynağı buymuş). Artık harf içeren hücreler asla sayı sayılmıyor.
  2. `find_header_block`, tek hücreli dekoratif başlık satırlarını (ör. rapor başlığı) header bloğuna dahil edip dönem sütunu tespitini bozuyordu (crosstab tabloları kırılıyordu). Artık yalnızca gerçek çok hücreli header satırları dahil ediliyor.
- İşgücü tablosu artık doğru ayrışıyor: "İşgücü" göstergesi (2020: 30.735 bin) "15+ yaş nüfus" göstergesinden (2020: 62.579 bin) doğru şekilde ayrılıyor — önceki sürümlerde bu ikisi karışıyordu.
- 8 regresyon testi (7 eski + İşgücü senaryosu) doğrulandı.

## v0.16.0 (teşhis)
- İşgücü tablosunda gösterge blok tespiti canlıda tetiklenmedi — meğer bu dosya satır-bloklu değil, sütun-yığmalı yapıdaymış (birden fazla isimsiz gösterge sütunu yan yana). `tuik_get_table_data` çıktısına geçici bir `debug_header` alanı eklendi: tespit edilen başlık satırı, üstündeki/altındaki ham satırlar. Bu, gerçek yapıyı görüp doğru düzeltmeyi (çok satırlı/birleştirilmiş başlık birleştirme) yapmak için.

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
