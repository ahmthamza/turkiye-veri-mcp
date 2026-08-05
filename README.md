# Türkiye Veri MCP

Türkiye'nin dört resmi veri kaynağını tek [MCP (Model Context Protocol)](https://modelcontextprotocol.io) sunucusunda birleştirir:

- **TÜİK** — Türkiye İstatistik Kurumu'nun resmi veri portalı (veriportali.tuik.gov.tr) ve SDMX dataflow'ları (databrowser2.tuik.gov.tr) üzerinden 19 temadaki tüm tablolar: her kırılım, her dönem. Scraping yok.
- **TCMB EVDS** — Merkez Bankası Elektronik Veri Dağıtım Sistemi (v3): tüm kategoriler, veri grupları ve binlerce seri; frekans dönüşümü (günlükten yıllığa), agregasyon (avg/min/max/first/last/sum) ve formüller (yüzde değişim, yıllık değişim, fark, hareketli ortalama/toplam) dahil API'nin tüm özellikleri.
- **BDDK** — Bankacılık Düzenleme ve Denetleme Kurumu'nun FinTürk verisi: il bazında kredi/mevduat/bireysel bankacılık/oranlar/şube-nüfus/altın, 7 gösterge × 7 grup kırılımı (sektör, mevduat/katılım/kalkınma-yatırım bankaları, kamu/yerli özel/yabancı sermaye) × 81 il + yurt dışı. **EVDS'de bu il kırılımı yok.**
- **HMB** — Hazine ve Maliye Bakanlığı bütçe istatistikleri: il bazında genel bütçe geliri (aylık) ve il × kategori bütçe gider/gelir/vergi/denge karşılaştırması, hem merkezi yönetim hem mahalli idareler (belediyeler, il özel idareleri) için. **EVDS'de bu il kırılımı yok** — EVDS'nin kamu maliyesi kategorisi yalnızca ulusal toplam seriler veriyor.

**One MCP server for Turkish official data** — TUIK statistics, TCMB EVDS series, BDDK banking data and HMB budget statistics, all with province-level breakdowns EVDS doesn't have, plus tidy CSV export for research pipelines.

> **Sorumluluk reddi:** Bu proje TÜİK, TCMB, BDDK veya HMB ile bağlantılı, onlar tarafından onaylanmış veya desteklenen bir proje değildir. Akademik araştırma amaçlı bağımsız bir araçtır.

## Araçlar

**TÜİK** (anahtar gerektirmez):

| Araç | Ne yapar |
| --- | --- |
| `tuik_list_themes` | 19 istatistik temasını listeler |
| `tuik_list_tables` | Temadaki tabloları listeler (SDMX `dataflow` / indirilebilir `istab`) |
| `tuik_search_tables` | Tüm temalarda tablo adı araması |
| `tuik_describe_dataflow` | Boyutlar, kod listeleri, key şablonu |
| `tuik_get_data` | SDMX verisi çekip önizler |
| `tuik_download_data` | Tam veri setini tidy CSV olarak yazar |
| `tuik_download_table_file` | `istab` dosyalarını (Excel) indirir |
| `tuik_get_table_data` | `istab` Excel'ini **tidy uzun formata** çevirir (başlık/dipnot satırları, çok sayfalı dosyalar, çapraz tablolar) |
| `usage_stats` | Bu sunucu örneğinin ne kadar kullanıldığını gösterir (araç çağrısı sayısı) |

**EVDS** (ücretsiz `EVDS_API_KEY` gerekir):

| Araç | Ne yapar |
| --- | --- |
| `evds_categories` | Ana kategorileri listeler (kur, faiz, enflasyon, ödemeler dengesi…) |
| `evds_datagroups` | Veri gruplarını listeler/arar |
| `evds_series_list` | Bir veri grubundaki tüm serileri kodlarıyla listeler |
| `evds_search_series` | **Seri adına göre arama** (52 bin seri; ilk çağrıda indeks kurulur, sonra anında) |
| `evds_get_datagroup_data` | Bir veri grubunun **tüm serilerini tek seferde** çeker (otomatik parçalama) |
| `evds_get_data` | Çoklu seri; frekans + agregasyon + formül desteğiyle önizler (uzun/geniş sorgular otomatik parçalanır) |
| `evds_download_data` | Aynı parametrelerle tidy CSV yazar |

**BDDK** (anahtar gerektirmez):

| Araç | Ne yapar |
| --- | --- |
| `bddk_get_data` | FinTürk il bazında bankacılık verisi — 7 tablo (`tablo_no` 1-7: Krediler, Mevduat, Bireysel Bankacılık, Seçilmiş Sektörel Krediler, Oranlar, Şubeler/Nüfus, Altın) × 7 grup (`taraf_list`: Sektör, Mevduat, Kalkınma ve Yatırım, Katılım, Yabancı, Kamu, Yerli Özel) × il (`sehir_list`: tek il, birden çok il, ya da `HEPSI`) |

**HMB** (anahtar gerektirmez):

| Araç | Ne yapar |
| --- | --- |
| `hmb_get_data` | İl bazında genel bütçe geliri (Tahakkuk/Tahsilat), aylık — il plaka kodu (`il_kodu`: "00" Merkez/ulusal toplam, "01"-"81") |
| `hmb_get_karsilastirma` | İl × kategori bütçe crosstab tablosu (`tablo`): Merkezi Yönetim `gider`/`gelir`/`vergi`/`denge` (aylık kümülatif) veya Mahalli İdareler `mahalli_gider`/`mahalli_gelir`/`mahalli_denge` (üç aylık) |

## Kurulum

İki yol var: **hosted sunucuya bağlanmak** (kurulum yok, aşağıdaki ilk bölüm) ya da **kendi bilgisayarınızda çalıştırmak** (bunun için [uv](https://docs.astral.sh/uv/getting-started/installation/) gerekir — `curl -LsSf https://astral.sh/uv/install.sh | sh`). Anahtar gerektiren tek şey EVDS: [evds3.tcmb.gov.tr](https://evds3.tcmb.gov.tr)'den ücretsiz API anahtarı alın (Benim Sayfam → Kayıt → Profilim → API Key). TÜİK, BDDK ve HMB araçlarının hiçbiri anahtar istemez.

### En kolay yol — hazır hosted sunucuya bağlanmak

Kurulum gerektirmez, hiçbir platformda `uvx`/Python derdi yok. Sunucu zaten `https://turkiye-veri-mcp.onrender.com/mcp` adresinde çalışıyor. TÜİK araçları herkes için anahtarsız çalışır. EVDS araçları için üç platformun **desteği farklı** — aşağıda platform platform, hangisinin kendi anahtarınızı kullanmanıza izin verdiği net yazıyor.

#### Claude Code (Mac ve Windows) — kendi EVDS anahtarınızla

```bash
claude mcp add-json turkiye-veri '{"type":"http","url":"https://turkiye-veri-mcp.onrender.com/mcp","headers":{"X-Evds-Api-Key":"ANAHTARINIZ"}}'
```

Bu komut **hem Mac'te hem Windows'ta (PowerShell dahil) harfi harfine aynı şekilde çalışır** — `claude mcp add --transport http ... --header ...` biçimini kasıtlı kullanmadık, çünkü o komutta iki bilinen sorun var: Windows'ta `--` sonrası parametreleri yanlış ayrıştırma hatası ([anthropics/claude-code#15077](https://github.com/anthropics/claude-code/issues/15077)) ve header'ın bazen sessizce kaydedilmediği ayrı bir hata ([anthropics/claude-code#17069](https://github.com/anthropics/claude-code/issues/17069)). `add-json` tek parça JSON aldığı için ikisini de atlıyor.

Kurulumdan sonra doğrulayın:
```bash
claude mcp list
```
`turkiye-veri` karşısında `✔ Connected` görmelisiniz. Emin olamıyorsanız `~/.claude.json` (Windows'ta `%USERPROFILE%\.claude.json`) dosyasını açıp `headers` alanının gerçekten yazıldığını kontrol edin.

**Sunucu sahibinin paylaşılan kotasıyla bağlanmak için** (kendi anahtarınızı hiç girmeden, ör. bir arkadaşınız zaten kendi anahtarıyla hosted sunucuyu çalıştırıyorsa):

```bash
claude mcp add-json turkiye-veri '{"type":"http","url":"https://turkiye-veri-mcp.onrender.com/mcp"}'
```

Bu, Mac'te doğrulanan `add-json` komutuyla aynı sözdizimi — sadece `headers` alanı çıkarılmış hali (o kısım ayrıca çalıştırılıp test edilmedi ama aynı kalıp olduğu için çalışması beklenir).

#### Claude Desktop — paylaşılan kota (kolay) ya da kendi anahtarınız (ek araç gerekir)

`claude_desktop_config.json` dosyası uzak (URL) sunucuları doğrudan kabul etmiyor — Desktop'ta iki yol var:

**Kolay yol (paylaşılan kota):** Uygulama içinde **Settings → Connectors → Add custom connector** ile `https://turkiye-veri-mcp.onrender.com/mcp` adresini ekleyin. EVDS araçları sunucu sahibinin anahtarını kullanır.

**Kendi anahtarınızla (Node.js gerektirir):** `mcp-remote` köprüsüyle `claude_desktop_config.json`'a ekleyin:

```json
{
  "mcpServers": {
    "turkiye-veri": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://turkiye-veri-mcp.onrender.com/mcp", "--header", "X-Evds-Api-Key: ANAHTARINIZ"]
    }
  }
}
```

#### Claude web (claude.ai) — yalnızca paylaşılan kota

claude.ai'nin connector arayüzü şu anda **özel header desteklemiyor** (Anthropic'in kendi deposunda açık bir özellik talebi: [anthropics/claude-ai-mcp#10](https://github.com/anthropics/claude-ai-mcp/issues/10)). Yani web'den bağlananlar kendi EVDS anahtarını veremez, otomatik olarak sunucu sahibinin kotasını kullanır. **Settings → Connectors → Add custom connector** ile yalnızca URL'yi eklemeniz yeterli:

```
https://turkiye-veri-mcp.onrender.com/mcp
```

### Kendi bilgisayarınızda çalıştırmak (yerel kurulum)

Hosted sunucuyu kullanmak istemiyorsanız — ör. tamamen kendi EVDS kotanızı izole etmek için — `uvx` ile yerel kurulum hâlâ mümkün:

```bash
claude mcp add -e EVDS_API_KEY=ANAHTARINIZ turkiye-veri -- uvx --from git+https://github.com/ahmthamza/turkiye-veri-mcp turkiye-veri-mcp
```

Windows'ta bu komut `--` hatasına takılırsa, `.bat` sarmalayıcı çözümü:

```bat
@echo off
set EVDS_API_KEY=ANAHTARINIZ
uvx --from git+https://github.com/ahmthamza/turkiye-veri-mcp turkiye-veri-mcp
```

Dosyayı ör. `C:\turkiye-veri-mcp\run_turkiye.bat` olarak kaydedip:

```powershell
claude mcp add turkiye-veri C:\turkiye-veri-mcp\run_turkiye.bat
```

## Örnek kullanım

```
TÜİK'te işgücüyle ilgili hangi tablolar var? İl bazında olanı 2014'ten itibaren çek.
EVDS'den TÜFE'yi yıllık yüzde değişimle, aylık frekansta 2010'dan bugüne al ve tufe.csv olarak kaydet.
USD/TRY ile politika faizini aynı tabloda, aylık ortalama olarak indir.
BDDK'dan 2026-6 dönemi için İstanbul'daki kamu bankalarının kredi hacmini çek.
HMB'den Ankara'nın (il kodu 06) bu yılki bütçe gelirini aylık olarak göster.
İllere göre merkezi yönetim bütçe giderlerini (tablo: gider) karşılaştır, en yüksek 10 ili sırala.
```

## Teknik notlar

- TÜİK dataflow verisi `databrowser2.tuik.gov.tr`'nin JSON-stat 2.0 API'sinden gelir (eski SDMX servisi `nsiws.tuik.gov.tr` 2026-08 itibarıyla erişilemez durumda; kod önce yeni API'yi dener, olmazsa eskiye düşer).
- EVDS v3 API'si kullanılır (`evds3.tcmb.gov.tr`); anahtar HTTP header'ında gönderilir ve TCMB sunucusunun gerektirdiği legacy SSL ayarı otomatik uygulanır.
- BDDK verisi FinTürk'ün kendi arayüzünün kullandığı `POST bddk.org.tr/BultenFinturk/tr/Home/VeriGetir` (jqGrid formatı) üzerinden gelir. **BDDK sunucusu TLS el sıkışmasında eksik bir ara sertifika gönderiyor** (tarayıcılar bunu kendileri tamamlıyor, çoğu istemci tamamlamıyor); eksik ara sertifika (GlobalSign RSA OV SSL CA 2018) pakete gömülüp doğrulama tam olarak yapılıyor, gevşetilmiyor.
- HMB verisi, sitenin JS arayüzünün kullandığı genel bir dosya listeleme API'sinden (`GET muhasebat.hmb.gov.tr/portal/v2/files?name=...&id=...`) geliyor; dönen HTML içindeki gerçek `.xls` linkleri indirilip ayrıştırılıyor. HMB'nin bazı `.xls` dosyaları `xlrd` ile açılamıyor (bozuk bir kayıt); `python_calamine` kullanılıyor.
- Endpoint keşfinde Emrah Er'in [tuikr](https://github.com/emraher/tuikr) R paketinden ve Fatih Mete'nin [evds](https://github.com/fatihmete/evds) Python paketinden (her ikisi MIT) yararlanılmıştır. BDDK ve HMB endpoint'leri tarayıcı DevTools ile keşfedilmiştir, dokümante bir kaynakları yoktur.

## Lisans

MIT

## Bilinen sınırlar

- **TÜİK istab tidy'leme best-effort'tur.** `tuik_get_table_data` TÜİK'in yaygın şablonlarını çözer ve her çıktıda `tidy_confidence` döndürür; şablona uymayan tablolarda hata verip sizi ham dosyaya yönlendirir. Ne kadarının çözüldüğünü ölçmek için `turkiye-veri-probe` kullanın.
- **TÜİK SDMX dataflow'larında şimdilik yalnızca `key="ALL"` desteklenir** (yeni `databrowser2` API'sinde daraltılmış key henüz yok). Sonucu kendiniz filtreleyin.
- **MEDAS/Biruni veritabanları listelenir, veri çekilmez.** Portal ağacındaki `database` düğümleri link olarak döner.
- **TÜİK mikroverisi kapsam dışıdır** (kurumsal başvuruyla dağıtılır).
- **BDDK'da yalnızca FinTürk (İllere Göre) bağlı — diğer bültenler (Günlük/Haftalık/Aylık, Kredi Kartı Bilgileri) henüz eklenmedi.**
- **HMB'de yalnızca 2026 yılı çalışıyor.** Diğer yıllar (2004-2025) ve diğer tablolar (İller İtibarıyla Konsolide Bütçe İstatistikleri 1990-2003, Genel Bütçe Vergi Gelirlerinden Mahalli İdare ve Fonlara Aktarılan Paylar) aynı `portal/v2/files` API'sini kullanıyor ama her biri kendi klasör "id"sini gerektiriyor — henüz keşfedilmedi.
- **Hosted (paylaşılan) sunucuda EVDS anahtarı platforma göre değişir.** Claude Code'da `X-Evds-Api-Key` header'ıyla kendi anahtarınızı gönderebilirsiniz (bkz. Kurulum). Claude web (claude.ai) şu an custom connector'larda özel header desteklemiyor, bu yüzden web'den bağlananlar otomatik olarak sunucu sahibinin kotasını paylaşır. Bu paylaşım bir güvenlik riski değildir (EVDS zaten herkese açık veri sunar) ama yoğun/toplu sorgulardan kaçının — kota tükenirse sunucu sahibinin de erişimi kesintiye uğrar.
- **EVDS'de sunucu tarafı seri araması yoktur.** `evds_search_series` ilk çağrıda tüm veri gruplarını gezip yerel bir indeks kurar (birkaç dakika), sonrasında anında çalışır. İndeks 7 günden eskiyse kendini yeniler; hemen yenilemek için `refresh=True` kullanın.

## Kullanım istatistikleri

Anthropic, başkalarının senin custom connector'ına kaç kez bağlandığını sana göstermez — bu bilgi Anthropic tarafında kalır. Bunun yerine sunucu kendi araç-çağrısı sayacını tutar (kişi değil, çağrı sayar):

```
"TÜİK MCP'de kullanım istatistiklerine bak" (usage_stats aracını çağırır)
```

Ayrıca proxy olarak: **GitHub → repo → Insights → Traffic** (ziyaretçi/klon sayısı) ve **Render dashboard → Metrics** (HTTP istek sayısı) kabaca ilgiyi gösterir.

Önemli sınır: Render'ın ücretsiz katmanındaki disk kalıcı değildir, yeniden dağıtımda/yeniden başlatmada sayaç sıfırlanır. Kalıcı bir sayaç isterseniz Render'da ücretli kalıcı disk eklemek gerekir.

## Yeni veri ve yeni seriler

Kod hiçbir seri veya tablo listesi barındırmaz; kataloglar her seferinde kaynaktan sorgulanır. Bu nedenle:

- **Mevcut bir serinin yeni gözlemi** (ör. yeni ay TÜFE'si) bir sonraki çağrıda gelir.
- **Yeni eklenen seri/tablo/tema** kod değişikliği olmadan görünür; katalog önbellekleri en fazla 1 saatlik, EVDS arama indeksi en fazla 7 günlüktür.
- **Kod değişikliği yalnızca kaynak yapısını değiştirirse gerekir** — TÜİK portal adreslerini değiştirirse, bir dataflow kaldırılırsa ya da istab dosyaları yeni bir şablonla yayınlanmaya başlarsa. Bu durumda `tuik_get_table_data` sessizce yanlış veri döndürmez, hata verip ham dosyaya yönlendirir.

## Kapsam denetimi

Kaynaklarda ne olduğu ve MCP'nin ne kadarını sunduğu iddia değil, ölçümdür. Canlı sayım için:

```bash
turkiye-veri-audit            # COVERAGE.md üretir
```

Rapor, TÜİK portalındaki tüm kaynakları üç erişim katmanına ayırıp tema tema sayar (A: SDMX tidy, B: istab dosya, C: MEDAS/Biruni — yalnızca link) ve EVDS'nin kategori/veri grubu/seri toplamlarını canlı API'den doğrular. Portallar değiştikçe raporu yeniden üretin.

## istab sondası

istab katmanının ne kadarının tidy'lenebilir olduğunu ölçmek için:

```bash
turkiye-veri-probe --per-theme 8   # ISTAB_PROBE.md üretir
```

Tema başına örneklem indirip dosyaları yapılarına göre sınıflandırır (clean / multiheader / multisheet / crosstab / nontabular) ve parser yazıldığı takdirde ulaşılabilecek tavanı raporlar.
