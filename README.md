# Türkiye Veri MCP

Türkiye'nin dört resmi veri kaynağını tek [MCP (Model Context Protocol)](https://modelcontextprotocol.io) sunucusunda birleştirir — 30 araç:

- **TÜİK** — Türkiye İstatistik Kurumu'nun veri portalı (veriportali.tuik.gov.tr) ve veri tarayıcısı (databrowser2.tuik.gov.tr): 19 tema, 515 SDMX veri akışı (boyut/kod filtresiyle, her kırılım ve dönem) ve 1.700'den fazla `istab` Excel tablosu.
- **TCMB EVDS** — Merkez Bankası Elektronik Veri Dağıtım Sistemi (v3): 154 kategori, 680 veri grubu, 54 binden fazla seri; frekans dönüşümü, agregasyon ve formüller dahil API'nin tüm özellikleri. 1000 satır sınırı otomatik sayfalanır.
- **BDDK** — Bankacılık Düzenleme ve Denetleme Kurumu'nun beş etkileşimli bülteni: FinTürk (il bazında, çeyreklik), Aylık Bülten (17 tablo, 2002'den bugüne), Haftalık Bülten, Günlük Bülten ve banka dışı mali kuruluşlar (finansal kiralama, faktoring, finansman, varlık yönetim).
- **HMB** — Hazine ve Maliye Bakanlığı Muhasebat istatistikleri: il bazında genel bütçe geliri (2004+), il × kategori bütçe tabloları (Konsolide 1990–2003, Merkezi Yönetim 2004+, Mahalli İdareler 2006+), vergi gelirlerinden aktarılan paylar ve sitenin yayımladığı diğer tüm dosyalar.

**One MCP server for Turkish official data** — TUIK statistics, TCMB EVDS series, BDDK banking bulletins and HMB budget statistics, with province-level breakdowns EVDS doesn't have, plus tidy CSV export for research pipelines.

> **Sorumluluk reddi:** Bu proje TÜİK, TCMB, BDDK veya HMB ile bağlantılı, onlar tarafından onaylanmış veya desteklenen bir proje değildir. Akademik araştırma amaçlı bağımsız bir araçtır.

Ortak ilke: araçlar doğrulayamadığı sayıyı sessizce döndürmez; ya kaynakla karşılaştırıp not düşer ya da açık bir hata verir. Her araç sohbette küçük bir önizleme gösterir; çoğu `output_path` ile tam tabloyu CSV olarak yazar.

## Araçlar

**TÜİK** (anahtar gerektirmez):

| Araç | Ne yapar |
| --- | --- |
| `tuik_list_themes` | 19 istatistik temasını listeler |
| `tuik_list_tables` | Temadaki tabloları listeler (`dataflow` / `istab` / `database`) |
| `tuik_search_tables` | Tüm temalarda arama: Türkçe karakter duyarsız (`isgucu` → İşgücü), çok kelimeli, `resource_type` filtresi |
| `tuik_describe_dataflow` | Veriyi indirmeden boyutları, verisi olan kodları (tr/en etiketli), dönem aralığını ve gözlem sayısını verir |
| `tuik_get_data` | Veri akışını önizler; `filters="SEX=2;..."`, `start`/`end` sunucuda uygulanır |
| `tuik_download_data` | Aynı seçimi tidy CSV olarak yazar |
| `tuik_download_table_file` | `istab` dosyasını (Excel) indirir |
| `tuik_get_table_data` | `istab` Excel'ini tidy uzun formata çevirir; `tidy_confidence` döndürür |
| `usage_stats` | Bu sunucu örneğinin araç çağrısı sayacı |

**EVDS** (ücretsiz `EVDS_API_KEY` gerekir):

| Araç | Ne yapar |
| --- | --- |
| `evds_categories` | Kategori ağacı (3 düzey) |
| `evds_datagroups` | Veri gruplarını listeler/arar (sayfalı; birim, kaynak, son güncelleme) |
| `evds_series_list` | Bir veri grubundaki serileri kodlarıyla listeler (sayfalı) |
| `evds_series_info` | Seri üst verisi: birim, frekans, ilk/son gözlem, varsayılan agregasyon, kaynak |
| `evds_search_series` | Seri adına/koduna göre arama (TR+EN, Türkçe karakter duyarsız; dizin arka planda kurulur) |
| `evds_get_datagroup_data` | Bir veri grubunun tüm serilerini tek seferde çeker |
| `evds_get_data` | Çoklu seri; frekans + agregasyon + formül, `last_n`, `format="long"`, `output_path` |
| `evds_download_data` | Aynı parametrelerle CSV yazar |

**BDDK** (anahtar gerektirmez):

| Araç | Ne yapar |
| --- | --- |
| `bddk_metadata` | Her bültenin tablo, grup (taraf) kodu, para birimi ve dönem listesini siteden canlı okur — önce bunu çağırın; grup kodları bültenden bültene farklıdır |
| `bddk_get_data` | FinTürk il bazında veri: 7 tablo × 7 grup × 81 il + yurt dışı, 2007-12'den bugüne çeyreklik; `donem_bitis` ile zaman serisi |
| `bddk_aylik_bulten` | Aylık Bülten'in 17 tablosu (bilanço, kâr-zarar, krediler, mevduat, sermaye yeterliliği, rasyolar…), TL/USD |
| `bddk_haftalik_bulten` | Haftalık Bülten'in 9 tablosu (2014'ten bugüne) |
| `bddk_haftalik_seri` | Haftalık Bülten'de tek kalemin zaman serisi |
| `bddk_gunluk_bulten` | Günlük Bülten'in son günü; tek kalemin son ~7 iş günü |
| `bddk_bdmk_bulten` | Finansal kiralama, faktoring, finansman ve varlık yönetim şirketleri |

**HMB** (anahtar gerektirmez):

| Araç | Ne yapar |
| --- | --- |
| `hmb_get_data` | İl bazında genel bütçe geliri (Tahakkuk/Tahsilat), aylık kümülatif, 2004'ten bugüne — `il_kodu`: "01"-"81", "00" = Merkez muhasebe birimleri (ulusal toplam değildir) |
| `hmb_get_karsilastirma` | İl × kategori bütçe tabloları: `gider`/`gelir`/`vergi`/`denge` ve `mahalli_*`; 1990'dan bugüne |
| `hmb_get_paylar` | Genel bütçe vergi gelirlerinden mahalli idare ve fonlara aktarılan paylar |
| `hmb_list_tables` / `hmb_list_files` | Muhasebat sitesindeki tüm istatistik bölümlerini, klasörleri ve dosyaları gezer |
| `hmb_get_file` | Herhangi bir HMB dosyası: tanınan düzen tidy tabloya çevrilir, tanınmayan gerekçesiyle reddedilir ve `ham_path` ile ham dosya olarak kaydedilir |

## Kurulum

İki yol var: **hosted sunucuya bağlanmak** (kurulum yok, aşağıdaki ilk bölüm) ya da **kendi bilgisayarınızda çalıştırmak** (bunun için [uv](https://docs.astral.sh/uv/getting-started/installation/) gerekir — `curl -LsSf https://astral.sh/uv/install.sh | sh`). Anahtar gerektiren tek şey EVDS: [evds3.tcmb.gov.tr](https://evds3.tcmb.gov.tr)'den ücretsiz API anahtarı alın (Benim Sayfam → Kayıt → Profilim → API Key). TÜİK, BDDK ve HMB araçlarının hiçbiri anahtar istemez.

### En kolay yol — hazır hosted sunucuya bağlanmak

Kurulum gerektirmez, hiçbir platformda `uvx`/Python derdi yok. Sunucu zaten `https://turkiye-veri-mcp.onrender.com/mcp` adresinde çalışıyor. TÜİK, BDDK ve HMB araçları herkes için anahtarsız çalışır. EVDS araçları için üç platformun **desteği farklı** — aşağıda platform platform, hangisinin kendi anahtarınızı kullanmanıza izin verdiği net yazıyor.

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
TÜİK'te aylık işgücü veri akışını bul, kadınlar için işsizlik oranını 2014'ten itibaren çek.
EVDS'den USD/TRY'nin son 30 gözlemini getir; TÜFE'yi yıllık yüzde değişimle 2010'dan bugüne tufe.csv olarak kaydet.
BDDK'dan İstanbul'daki kamu bankalarının kredi hacmini 2020-3'ten 2026-9'a çeyreklik seri olarak çek.
BDDK Aylık Bülten'den sektörün takipteki alacaklarını son 24 ay için getir.
HMB'den Ankara'nın (il kodu 06) bu yılki bütçe gelirini aylık olarak göster.
1998 yılı için illere göre konsolide bütçe giderlerini karşılaştır, en yüksek 10 ili sırala.
```

## Teknik notlar

- **TÜİK:** veri akışları `databrowser2.tuik.gov.tr`'nin (TÜİK'in kendi veri tarayıcısının arka ucu) yapı, kod listesi ve SDMX-CSV indirme uçlarından gelir; eski `nsiws.tuik.gov.tr` servisi (HTTP 401) kullanılmaz. Her indirme TÜİK'in bildirdiği gözlem sayısıyla karşılaştırılır. Portal `istab` indirmelerini istemci başına 5 saniyede bir dosyayla sınırlar; araçlar bekler ve "lütfen bekleyiniz" sayfasını dosya diye kaydetmez.
- **EVDS:** v3 API (`evds3.tcmb.gov.tr`); anahtar HTTP başlığında gönderilir, TCMB'nin gerektirdiği legacy SSL ayarı otomatik uygulanır. EVDS her yanıtı 1000 satırla sınırlar ve en yeni 1000'i sessizce döndürür; istemci geriye doğru sayfalayıp birleştirir.
- **BDDK:** bültenlerin kendi arayüzlerinin kullandığı uçlar. Tablo, grup, dönem ve para birimi listeleri sayfalardan canlı okunur; gelen her yanıt istenen dönem/grup/tabloyla karşılaştırılır. TLS doğrulaması hiçbir koşulda kapatılmaz. İstekler arası 0,8 sn bekleme, çağrı başına 55 sn üst sınır.
- **HMB:** `muhasebat.hmb.gov.tr/portal/v2` (sayfa ağacı + dosya listeleme). Klasör kimlikleri çalışma anında siteden keşfedilir; yeni yıllar kod değişikliği olmadan görünür. Birim etiketi olmayan dosyalar yalnızca kardeş bir tabloyla doğrulanabiliyorsa kabul edilir.
- **Hata mesajları:** MCP SDK 2.x, `ToolError` olmayan istisnaların metnini gizler; tüm araç hataları `ToolError`'a çevrilerek kullanıcıya iletilir.
- **Büyük veri:** barındırılan küçük sunucularda bellek için `TUIK_MAX_OBSERVATIONS` ortam değişkeniyle TÜİK indirme sınırı düşürülebilir (varsayılan 2 milyon gözlem; `render.yaml`'da 300 bin).
- Endpoint keşfinde Emrah Er'in [tuikr](https://github.com/emraher/tuikr) R paketinden ve Fatih Mete'nin [evds](https://github.com/fatihmete/evds) Python paketinden (her ikisi MIT) yararlanılmıştır. TÜİK DataBrowser2, BDDK ve HMB uçları tarayıcı DevTools ile keşfedilmiştir; dokümante bir kaynakları yoktur ve kurumlar arayüzlerini değiştirirse bozulabilir.

## Testler

```bash
uv pip install pytest
pytest -m "not live"     # çevrimdışı birim testleri
pytest                    # + canlı testler (internet; EVDS için EVDS_API_KEY)
```

## Lisans

MIT

## Bilinen sınırlar

- **TÜİK:** katalogdaki 515 veri akışının 508'i canlı doğrulandı; `DF_UGOC_T1`–`T6` ve `T8` için TÜİK sunucusunda veri yok. Önizleme 300 bin, indirme 2 milyon gözlemle sınırlı.
- **TÜİK istab tidy'leme best-effort'tur.** Bağımsız 134 dosyalık örneklemde 131 tablo dönüştürüldü (80 yüksek, 51 orta güven), 3'ü açık hatayla reddedildi. "orta" = tabloda dönem bilgisi yok, başlığa bakın. TR/EN etiketlerin alt alta yazıldığı tablolarda bir seri iki etiketle bölünebilir; bazı değer sütunları etiket sanılıp atlanabilir (veri eksik kalır, yanlış olmaz). Ham dosya her zaman `tuik_download_table_file` ile alınabilir.
- **MEDAS/Biruni veritabanları (100 kayıt) yalnızca bağlantı olarak listelenir:** durum tutan bir web arayüzü, veri API'si yok.
- **TÜİK mikroverisi kapsam dışıdır** (kurumsal başvuruyla dağıtılır).
- **EVDS:** sunucu tarafı seri araması ve "son N gözlem" ucu yoktur; ikisi de istemci tarafında yapılır. `evds_search_series` dizini ilk kullanımda arka planda kurulur (yaklaşık 8 dakika), 7 günde bir yenilenir; dizin hazır değilken eşleşen veri grupları döner.
- **BDDK:** Günlük Bülten'in geçmişi yayımlanmıyor (yalnızca son gün ve kalem başına son ~7 iş günü). Kredi kartı ücretleri bülteni (banka bazında ücret bilgisi) ve "Üç Aylık Temel Göstergeler" bağlı değil. FinTürk altın tablosu 2015-9'da başlar. Aylık Bülten'de 6 tablo yalnızca TL'dir.
- **HMB:** `hmb_get_file` ile örneklenen 854 tablo türünden 298'i tabloya çevriliyor; kalanlar (muhasebe bazlı tablolar, GFS dosyaları, birçok ulusal Mahalli İdare/SGK tablosu, PDF bültenler) yalnızca ham dosya olarak iner. Mahalli İdareler il tabloları 2022 için birim etiketi olmadığından reddedilir. İl dosyalarının toplamı ile Merkezi Yönetim il tablosu 2006'dan itibaren %0,3–1,3 farklıdır (HMB'nin iki ayrı yayını).
- **Hosted (paylaşılan) sunucuda EVDS anahtarı platforma göre değişir.** Claude Code'da `X-Evds-Api-Key` başlığıyla kendi anahtarınızı gönderebilirsiniz (bkz. Kurulum). Claude web (claude.ai) özel başlık desteklemediği için web'den bağlananlar sunucu sahibinin kotasını paylaşır; yoğun/toplu sorgulardan kaçının.
- **Hosted sunucuda `output_path` sunucunun diskine yazar**, sizin bilgisayarınıza değil; dosya çıktısı için yerel kurulum kullanın.

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
