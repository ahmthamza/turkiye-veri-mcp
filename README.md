# Türkiye Veri MCP

Türkiye'nin iki temel resmi veri kaynağını tek [MCP (Model Context Protocol)](https://modelcontextprotocol.io) sunucusunda birleştirir:

- **TÜİK** — Türkiye İstatistik Kurumu'nun resmi veri portalı (veriportali.tuik.gov.tr) ve SDMX 2.1 web servisi (nsiws.tuik.gov.tr) üzerinden 19 temadaki tüm tablolar: her kırılım, her dönem. Scraping yok.
- **TCMB EVDS** — Merkez Bankası Elektronik Veri Dağıtım Sistemi (v3): tüm kategoriler, veri grupları ve binlerce seri; frekans dönüşümü (günlükten yıllığa), agregasyon (avg/min/max/first/last/sum) ve formüller (yüzde değişim, yıllık değişim, fark, hareketli ortalama/toplam) dahil API'nin tüm özellikleri.

**One MCP server for Turkish official data** — TUIK statistics via SDMX and TCMB EVDS series with full frequency/aggregation/formula support, plus tidy CSV export for research pipelines.

> **Sorumluluk reddi:** Bu proje TÜİK veya TCMB ile bağlantılı, onlar tarafından onaylanmış veya desteklenen bir proje değildir. Akademik araştırma amaçlı bağımsız bir araçtır.

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

## Kurulum

İki yol var: **hosted sunucuya bağlanmak** (kurulum yok, aşağıdaki ilk bölüm) ya da **kendi bilgisayarınızda çalıştırmak** (bunun için [uv](https://docs.astral.sh/uv/getting-started/installation/) gerekir — `curl -LsSf https://astral.sh/uv/install.sh | sh`). Her iki yolda da EVDS araçları için [evds3.tcmb.gov.tr](https://evds3.tcmb.gov.tr)'den ücretsiz API anahtarı gerekir (Benim Sayfam → Kayıt → Profilim → API Key); TÜİK araçları anahtar gerektirmez.

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

EVDS anahtarınız yoksa header kısmını boş `{}` bırakıp sadece TÜİK araçlarını kullanabilirsiniz, ya da hiç anahtar vermeden bağlanıp EVDS araçlarında sunucu sahibinin paylaşılan kotasını kullanabilirsiniz:
```bash
claude mcp add --transport http turkiye-veri https://turkiye-veri-mcp.onrender.com/mcp
```

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
```

## Teknik notlar

- TÜİK verisi standart SDMX 2.1 REST'ten gelir; sunucu önce SDMX-CSV ister, servis reddederse SDMX-ML GenericData'yı ayrıştırır. Bir dataflow `key="ALL"` ile tüm kırılımları ve tüm dönemi verir; büyük dataflow'larda `tuik_describe_dataflow` ile daraltılmış key kurun.
- EVDS v3 API'si kullanılır (`evds3.tcmb.gov.tr`); anahtar HTTP header'ında gönderilir ve TCMB sunucusunun gerektirdiği legacy SSL ayarı otomatik uygulanır.
- Endpoint keşfinde Emrah Er'in [tuikr](https://github.com/emraher/tuikr) R paketinden ve Fatih Mete'nin [evds](https://github.com/fatihmete/evds) Python paketinden (her ikisi MIT) yararlanılmıştır.

## Lisans

MIT

## Bilinen sınırlar

- **TÜİK istab tidy'leme best-effort'tur.** `tuik_get_table_data` TÜİK'in yaygın şablonlarını çözer ve her çıktıda `tidy_confidence` döndürür; şablona uymayan tablolarda hata verip sizi ham dosyaya yönlendirir. Ne kadarının çözüldüğünü ölçmek için `turkiye-veri-probe` kullanın.
- **MEDAS/Biruni veritabanları listelenir, veri çekilmez.** Portal ağacındaki `database` düğümleri link olarak döner.
- **TÜİK mikroverisi kapsam dışıdır** (kurumsal başvuruyla dağıtılır).
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
