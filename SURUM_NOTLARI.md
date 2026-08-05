# Sürüm notları

## v0.32.0 — HMB: iki gerçek kaynak-veri hatası bulundu ve düzeltildi
Kullanıcının "eski yılları test etmeden 'bitti' demeyelim" ısrarı iki gerçek, ciddi hatayı ortaya çıkardı:

- **Birim yıla göre değişiyor.** 2010+ dosyalarında "(Bin TL)", 2004 dosyasında ise **"(Milyar TL.)"** yazıyor. Fark edilmeseydi 2004'ün rakamları sessizce 1 milyon kat küçük görünecekti. Artık her dosyanın kendi birim etiketi okunup "bin TL"ye normalize ediliyor; tanınmayan bir birim gelirse (`_birim_carpani`) sessizce geçmek yerine net hata veriyor.
- **2004 dosyasında bir sayfa adı bozuk** — "Mayıs" yerine "00 Merkez" yazıyor (HMB'nin kendi dosyasındaki bir hata). 12 ayın 11'i tanınıp tam olarak biri eksikse, tanınmayan sayfaya o eksik ay atanıyor (`_fixed_month_names`) — konumdan tahmin değil, "hangi ay hâlâ eksik" mantığıyla.
- **Ayrıca bulundu:** Ay adı yazımı yıllar arasında tutarsızdı (2004: "Ocak", 2010+: "OCAK") — hepsi artık kanonik başlık-harfli forma normalize ediliyor.
- **Türkçe `.upper()` tuzağına iki kez daha denk gelindi** (bugün TÜİK'te de görülmüştü): hem ay adı eşleştirmesinde hem birim etiketinde, Python'un "Nisan"/"Bin" gibi kelimeleri ASCII "I" ile büyütmesi (doğrusu noktalı "İ") sessiz eşleşme hatalarına yol açıyordu; ikisi de sabit yazım listeleriyle düzeltildi.
- Gerçek 2004 ve 2010 dosyalarıyla uçtan uca doğrulandı; 2024/2025/2026 regresyonu bozulmadı.

## v0.31.0 — HMB: 2004-2026 arası tüm yıllar
- `hmb_get_data` artık **23 yılın (2004-2026) tamamını** destekliyor. Klasör id'leri Claude for Chrome ile DevTools üzerinden tek tek tıklanıp doğrulandı, tahmin edilmedi.
- İlginç bulgu: 2020-2026 id'leri yıl arttıkça artıyor (beklenen), ama 2004-2019 tam tersi yönde artıyor (2019 en düşük id, 2004 en yüksek) -- bu 16 yılın muhtemelen tek bir toplu yükleme işleminde, geriye doğru sırayla eklendiğini gösteriyor. id'ler yıl başına sabit bir aralıkla artmadığı için (2026→2025→2024 farkları -96/-86) hiçbir yıl tahminle doldurulmadı.

## v0.30.0 — HMB: 2024-2025 eklendi, sütun kayması hatası düzeltildi
- `hmb_get_data` artık 2024, 2025 ve 2026'yı destekliyor (klasör id'leri: 4042/3946/3860 -- doğrulandı, tahmin edilmedi).
- **Gerçek bir hata bulundu ve düzeltildi:** "00-Merkez" (ulusal toplam) dosyasının sütun yapısı il dosyalarından (ör. Adana) farklı çıktı -- il dosyalarında fazladan boş bir ilk sütun var. Sabit sütun indeksi (`row[1]`, `row[2]`, `row[3]`) yerine, her sayfanın kendi başlık satırından "Tahakkuk"/"Tahsilat" sütunlarının gerçek konumu okunuyor artık; kategori sütunu da buna göre dinamik türetiliyor. Hem Adana (regresyon) hem yeni Merkez dosyaları doğru ayrıştırıldığı test edildi.
- **Not:** Klasör id'leri yıllar arasında sabit bir aralıkla artmıyor (2026:4042, 2025:3946 [-96], 2024:3860 [-86]) -- bu yüzden 2004-2023 için id tahmin edilmedi, her biri ayrıca doğrulanmadan eklenmeyecek.

## v0.29.1 (dokümantasyon)
- README artık dört kaynağı da (TÜİK, EVDS, BDDK, HMB) kapsıyor — önceden yalnızca TÜİK/EVDS odaklıydı, bugün eklenen 5 araç (`bddk_get_data`, `hmb_get_data`, `hmb_get_karsilastirma`) hiç belgelenmemişti. Araç tabloları, örnek kullanım, teknik notlar (BDDK'nın TLS sertifika durumu, HMB'nin dosya API'si) ve bilinen sınırlar (BDDK'da yalnızca FinTürk, HMB'de yalnızca 2026) eklendi.

## v0.29.0 — BDDK SSL sorunu doğru şekilde çözüldü (gevşetme yok)
- **Kök neden kesinleşti:** `openssl s_client` çıktısı gösterdi ki bddk.org.tr TLS el sıkışmasında yalnızca kendi (leaf) sertifikasını gönderiyor, onu imzalayan ara sertifikayı ("GlobalSign RSA OV SSL CA 2018") göndermiyor. Tarayıcılar eksik halkayı sertifikanın AIA alanındaki adresten indirip zinciri tamamlıyor; Python'un `ssl` modülü bunu yapmıyor.
- **Çözüm:** Eksik ara sertifika pakete gömüldü (`certs/globalsign_rsa_ov_ssl_ca_2018.pem`, kaynağı sertifikanın kendi AIA adresi). Artık `certifi` kök deposu + bu ara sertifika birleştirilip kullanılıyor — **doğrulama devre dışı bırakılmıyor**, sadece eksik halka tamamlanıyor. Zincir güvenilir GlobalSign Root CA - R3'e kadar gidiyor.
- **Doğrulama:** `openssl verify` ile, gömülü sertifikanın BDDK'nın canlı leaf sertifikasını certifi köklerine bağladığı teyit edildi (`leaf.pem: OK`). Birleşik paketin Python `ssl` tarafından da sorunsuz yüklendiği (122 kök) ve gerçek (editable olmayan) kurulumda .pem dosyasının pakete dahil edildiği ayrıca test edildi.
- Üç kademeli mantık: (1) normal doğrulama — BDDK zincirini düzeltirse otomatik buraya döner, (2) gömülü ara sertifikayla tam doğrulama, (3) yalnızca son çare olarak doğrulamasız, ve bu durumda çıktıda açık `uyari` alanı. Kademe 2 kullanıldığında çıktıda bilgilendirici bir `tls_notu` görünür.
- Yeni bağımlılık: `certifi` (zaten dolaylı olarak vardı, artık açıkça belirtiliyor).

## v0.28.0 — BDDK SSL sertifika sorunu için geri çekilme
- **Sorun:** Canlı testte `bddk_get_data`, BDDK sunucusuna bağlanırken `CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate` hatası verdi. Aynı hata iki bağımsız barındırılan sunucudan doğrulandı; aynı adres masaüstü tarayıcıda sorunsuz açılıyor. Sebep: bddk.org.tr'nin TLS zincirinde eksik bir ara sertifika var — tarayıcılar bunu kendileri tamamlıyor (AIA), Python'un `ssl` modülü tamamlamıyor.
- **Çözüm (katmanlı):** `bddk.py` artık ÖNCE normal sertifika doğrulamasıyla bağlanmayı deniyor; yalnızca bu spesifik sertifika hatası gelirse doğrulamayı gevşetip tekrar deniyor. Sessizce yapmıyor: `bddk_get_data` çıktısına, bağlantının doğrulanamadığını açıkça söyleyen bir `uyari` alanı ekleniyor.
- Bu projede emsali var: TCMB EVDS için de özel bir SSL ayarı (OP_LEGACY_SERVER_CONNECT) gerekmişti — TR kamu sunucularında TLS eksiklikleri yaygın.
- **Risk notu:** BDDK FinTürk kamuya açık toplu bankacılık istatistiği yayınlıyor ve hiçbir kimlik bilgisi gönderilmiyor; doğrulanmamış kanal bir gizlilik riski değil, yalnızca kamuya açık sayıların kaynağının kriptografik teyidi eksik demek. Temiz çözüm, eksik ara sertifikayı pakete gömmek — canlı sunucudan yakalandığında yapılabilir.

## v0.27.0 — HMB: Mahalli İdareler eklendi
- `hmb_get_karsilastirma`'ya 3 yeni tablo: `mahalli_gider`, `mahalli_gelir`, `mahalli_denge` — belediye ve il özel idarelerinin il bazında bütçesi, EVDS'de hiç olmayan bir kırılım. Klasör id'leri doğrulandı (4098/4099/4100).
- Mevcut crosstab ayrıştırıcı (`crosstab_xls_to_tidy_frame`) hiç değişiklik gerektirmeden bu yeni dosyaları da doğru işledi — Merkezi Yönetim için yazdığımız genel çözüm (tidy.py'nin başlık birleştirme mantığı) burada da geçerli oldu.
- Çapraz doğrulama: Ankara'nın Mahalli İdareler Gider dosyasındaki TOPLAM (46.259.392), Denge dosyasındaki "BÜTÇE GİDERLERİ" değeriyle birebir eşleşti.
- HMB artık iki yönetim seviyesini (merkezi + mahalli), 7 crosstab tablosunu ve il bazlı bütçe gelirini kapsıyor — 2026 yılı için.

## v0.26.0 — HMB crosstab ailesinin 4 tablosu da tamam
- Kullanıcı "Bütçe Gider Tabloları" (id=4044) ve "Bütçe Gelir Tabloları" (id=4045) klasör id'lerini de yakaladı. `hmb_get_karsilastirma` artık 4 tabloyu da destekliyor: `gider`, `gelir`, `vergi`, `denge`.
- "Gelir Tabloları" klasöründe (id=4045) tek değil **iki** dosya olduğu görüldü (Gelirleri Tahsilatı + Vergi Gelirleri Tahakkuk/Tahsilat) — `get_karsilastirma_data` artık dosya adı örüntüsüyle (`file_match`) doğru dosyayı seçiyor, tek-dosya varsayımı kaldırıldı.
- Gerçek dosya etiketleriyle 4 eşleştirme de test edildi, hepsi doğru dosyayı buldu. HMB'nin il bazında bütçe verisi artık kapsamlı: harcama, gelir, vergi ve gelir-gider karşılaştırması, hepsi il kırılımıyla.

## v0.25.0 — HMB: crosstab tablo ailesi (İller Bazında Karşılaştırma)
- **`hmb_get_karsilastirma` aracı eklendi.** "İller İtibarıyla Merkezi Yönetim Bütçe İstatistikleri" — `hmb_get_data`'dan farklı bir aile: il başına ayrı dosya yerine tek dosyada il satır / kategori sütun (crosstab). Şimdilik yalnızca "denge" (gelir/gider karşılaştırması) bağlandı — klasör id'si doğrulandı (4046).
- **Çok satırlı başlık hatası bulundu ve düzeltildi:** "Gider" dosyasında iki satırlı bir başlık var (grup başlığı "EKONOMİK SINIFLANDIRMA"/"FONKSİYONEL SINIFLANDIRMA" + altında gerçek kategori adları) — ilk yazımda grup başlığı gerçek başlık sanılıp yanlış/eksik sütunlar (2 yerine 20 kategori) üretiyordu. Bunu ayrı bir sezgiyle çözmek yerine bugün TÜİK istab dosyalarında zaten kanıtlanmış `tidy.find_header_block`/`_combine_header_rows`'u yeniden kullandık — artık 19 kategori doğru etiketle geliyor (ör. "EKONOMİK SINIFLANDIRMA — Pers. Giderleri").
- Çapraz doğrulama: Ankara'nın "Gider" dosyasındaki TOPLAM değeri (610.481.879), "Denge" dosyasındaki "Giderler" değeriyle birebir eşleşti.
- **Bilinen sınır:** "gider" ve "gelir" (vergi de gelir klasöründe) tabloları için klasör id'leri henüz yakalanmadı, sadece "denge" canlı çalışıyor.

## v0.24.0 — HMB canlı, ilk gerçek veri
- **`hmb_get_data` aracı eklendi.** muhasebat.hmb.gov.tr'nin JS arayüzünün kullandığı genel dosya listeleme API'si keşfedildi: `GET portal/v2/files?name=<klasör adı>&id=<klasör id>` — HTML gömülü JSON döndürüyor, doğrudan .xls indirme linkleri (gerçek, tahmin edilemeyen hash'leriyle) içeriyor.
- İl bazında genel bütçe geliri (Tahakkuk/Tahsilat, aylık) — EVDS'de olmayan coğrafi kırılım, il plaka kodlarıyla (00 Merkez, 01-81) erişilebiliyor.
- Gerçek indirilen dosyayla doğrulandı: Adana, Haziran 2026, Genel Bütçe Gelirleri Tahakkuk = 155.052.887 bin TL.
- Yeni bağımlılık: `python-calamine` — HMB'nin .xls dosyaları `xlrd`'nin standart okuyucusunu bozan bir kayıt içeriyor (OLE2 imzası geçerli, gerçek ikili format, ama xlrd'nin UTF-16 metin ayrıştırıcısı çöküyor); calamine bunu sorunsuz okuyor.
- **Bilinen sınır:** yalnızca 2026 yılının klasör id'si biliniyor (4042). Diğer yıllar ve diğer bütçe tabloları (Mahalli İdareler, Merkezi Yönetim, Konsolide) aynı API'yi kullanıyor ama ayrı id gerektiriyor, henüz keşfedilmedi.

## v0.23.0 — BDDK: Grup ve İl kodları tam
- `TARAF_CODES` eklendi: 7 Grup kırılımı (Sektör, Mevduat, Kalkınma ve Yatırım, Katılım, Yabancı, Kamu, Yerli Özel) — kullanıcı tarafından tek tek 10001-10007 karşılığıyla doğrulandı.
- `SEHIR_CODES` eklendi: 81 il + "YURT DIŞI", BDDK'nın kendi yanıtındaki tam yazılışla (Türkçe İ/I ayrımı korunarak) — tahmin edilmedi, gerçek veriden alındı.
- **Sessiz hata koruması:** `bddk_get_data` artık bilinmeyen bir grup kodu ya da yanlış yazılmış bir il adı (ör. Türkçe "İSTANBUL" yerine ASCII "ISTANBUL") verilirse anlaşılır bir hatayla durur. Önceden BDDK bu tür hatalı girdilerde sessizce o kaydı atlıyordu, kullanıcı fark etmeden eksik veri alabilirdi.
- Artık "sektör toplamı" yanında kamu/özel/yabancı banka ayrımıyla, ya da tek bir il için de sorgu yapılabiliyor — "her mümkün veri" hedefine yaklaşıldı.

## v0.22.2 — BDDK 7 tablonun tamamı doğrulandı
- Kullanıcı 5-7 numaralı tabloları (Oranlar, Şubeler/Nüfus, Altın) da FinTürk arayüzünde kontrol etti, sıralı örüntü (dropdown sırası = tabloNo) doğrulandı. `bddk_get_data`'nın 7 tablosu artık tahmin değil, tam doğrulanmış durumda.

## v0.22.1 — BDDK 7 tablo
- `bddk_get_data`, BDDK FinTürk'ün 7 "Bilgi" seçeneğinin tümüyle (Krediler, Mevduat, Bireysel Bankacılık, Seçilmiş Sektörel Krediler, Oranlar, Şubeler/Nüfus Dağılımı, Altın Kredileri/Mevduatı) çalışacak şekilde güncellendi. tabloNo 1-4 kullanıcı tarafından tek tek doğrulandı (sıralı örüntü: dropdown sırası = tabloNo); 5-7 bu doğrulanmış örüntüden çıkarımla dolduruldu, ayrıca teyit edilmedi.

## v0.22.0 — BDDK canlı, ilk gerçek veri
- **`bddk_get_data` aracı eklendi.** BDDK'nın FinTürk (İllere Göre) interaktif bültenindeki gerçek endpoint'e bağlandı: `POST bddk.org.tr/BultenFinturk/tr/Home/VeriGetir`, form-encoded gövde, jqGrid biçimli JSON yanıt (`colModels` + `data.rows[].cell`).
- İl bazında kredi verisi (81 il + yurt dışı, toplam/nakdi/takipteki/gayrinakdi krediler) — EVDS'de olmayan coğrafi kırılım, tam hedeflediğimiz boşluk.
- Gerçek yakalanan yanıtla doğrulandı: İstanbul toplam nakdi kredi = 9.267.774.609 (bin TL), kullanıcının ekran görüntüsüyle birebir eşleşiyor.
- Bilinen sınır: şimdilik yalnızca tabloNo=1 (Krediler) doğrulandı; "Bilgi" açılır menüsündeki diğer seçenekler (muhtemelen başka tabloNo değerleri) henüz keşfedilmedi. İl kodları da yalnızca "HEPSİ" ile test edildi.

## v0.21.0 — BDDK ve HMB iskeletleri
- `src/turkiye_veri_mcp/bddk.py` ve `hmb.py` eklendi — CONTRIBUTING.md şablonuna uygun iskelet (client sınıfı, `list_tables`/`get_data` imzaları, `NotImplementedError`). Henüz `server.py`'ye araç olarak bağlanmadı, henüz canlı veri çekmiyor.
- CONTRIBUTING.md sağlayıcı kuyruğu güncellendi: BDDK ve HMB "inşa halinde" işaretlendi, İşKUR/TOBB eklendi, Belediye/CKAN "araştırma bekliyor" statüsüne indirildi, BIST ve Findeks kapsam dışı olarak not edildi.
- Sıradaki adım: BDDK'nın FinTürk (İllere Göre) interaktif bülteninde DevTools Network yakalaması.

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
