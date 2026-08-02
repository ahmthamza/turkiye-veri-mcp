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

Ön koşullar: [uv](https://docs.astral.sh/uv/getting-started/installation/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`) ve EVDS araçları için [evds3.tcmb.gov.tr](https://evds3.tcmb.gov.tr)'den ücretsiz API anahtarı (Benim Sayfam → Kayıt → Profilim → API Key). Anahtar girmezseniz TÜİK araçları yine çalışır; EVDS araçları anahtar isteyen açıklayıcı bir mesaj döner.

### Claude Code

```bash
claude mcp add -e EVDS_API_KEY=ANAHTARINIZ turkiye-veri -- uvx --from git+[https://github.com/ahmthamza/turkiye-veri-mcp](https://github.com/ahmthamza/turkiye-veri-mcp) turkiye-veri-mcp
```

### Claude Desktop

**Settings → Developer → Edit Config** ile `claude_desktop_config.json` dosyasına:

```json
{
  "mcpServers": {
    "turkiye-veri": {
      "command": "uvx",
      "args": ["--from", "git+[https://github.com/ahmthamza/turkiye-veri-mcp](https://github.com/ahmthamza/turkiye-veri-mcp)", "turkiye-veri-mcp"],
      "env": { "EVDS_API_KEY": "ANAHTARINIZ" }
    }
  }
}
```

### Claude web (claude.ai) — Hosted Kurulum (Herkes İçin)

Claude Web (claude.ai), bilgisayarınızdaki yerel sunuculara doğrudan bağlanamaz; internete açık bir sunucu adresine (URL) ihtiyaç duyar. Bu projeyi kendi adınıza, ücretsiz ve güvenli bir şekilde saniyeler içinde buluta kurmak için aşağıdaki butona tıklayabilirsiniz:

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/ahmthamza/turkiye-veri-mcp)

**Nasıl Kurulur?**
1. Yukarıdaki butona tıklayın (ücretsiz bir Render hesabı açmanız gerekebilir).
2. Kurulum ekranında sistem size güvenliğiniz için **`EVDS_API_KEY`** değerinizi soracaktır. Buraya [evds3.tcmb.gov.tr](https://evds3.tcmb.gov.tr) adresinden aldığınız kendi ücretsiz Merkez Bankası API anahtarınızı girin ve "Apply" diyerek kurulumu başlatın.
3. Birkaç dakika içinde kurulum bitecek ve Render size özel bir web adresi verecektir (örn: `https://turkiye-veri-abc.onrender.com`).
4. Bu adresin sonuna `/mcp` ekleyerek (örn: `https://turkiye-veri-abc.onrender.com/mcp`) kopyalayın.
5. **claude.ai** sayfasına gidin, sağ üstten **Settings → Connectors → Add custom connector** menüsüne tıklayın ve kopyaladığınız adresi yapıştırın.

*Not: Bu yöntem sayesinde herkes sunucuyu kendi API anahtarı ve kotasıyla çalıştırır. Anahtarınız kodun içinde yer almaz, tamamen şifrelenmiş olarak sizin Render hesabınızda saklanır.*

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