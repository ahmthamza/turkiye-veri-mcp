# Katkı Rehberi

Bu proje, Türkiye'nin resmi veri sağlayıcılarını tek MCP sunucusunda toplamayı hedefliyor. Yeni bir sağlayıcı eklemek çekirdeği değiştirmeyi gerektirmez: bir modül yazıp araçlarını kaydetmek yeterlidir.

## Mimari

```
src/turkiye_veri_mcp/
  portal.py   # TÜİK katalog istemcisi
  sdmx.py     # TÜİK SDMX istemcisi
  tidy.py     # istab Excel -> tidy long format
  evds.py     # TCMB EVDS istemcisi
  server.py   # araç tanımları (tuik_*, evds_*)
  audit.py    # kapsam raporu
  probe.py    # istab parse edilebilirlik sondası
```

Her sağlayıcı kendi modülünde durur; `server.py` yalnızca ince bir araç katmanıdır.

## Yeni sağlayıcı ekleme

1. **Erişim yolunu tespit edin.** Sırayla arayın: resmi API → SDMX/CKAN gibi standart servis → sayfaların beslendiği JSON backend → yapılandırılmış dosya (Excel/CSV) indirme. Tarayıcının Network sekmesi bu keşfin en hızlı yoludur. HTML scraping son çaredir; kırılgandır ve bakım yükü yaratır.

2. **Modülü yazın:** `src/turkiye_veri_mcp/<saglayici>.py`. Bir istemci sınıfı ya da fonksiyonlar; ham veriyi `pandas.DataFrame` olarak döndürsün. Ağ çağrıları `httpx`, hatalar için modüle özel bir `<Saglayici>Error`.

3. **Araçları kaydedin:** `server.py` içinde `<saglayici>_*` önekiyle. Asgari set:
   - `<saglayici>_list_*` — keşif (kategori/tablo listesi)
   - `<saglayici>_search_*` — isimle arama
   - `<saglayici>_get_data` — sohbet içi önizleme
   - `<saglayici>_download_data` — diske tidy CSV

4. **Anahtar gerekiyorsa** ortam değişkeni kullanın (`<SAGLAYICI>_API_KEY`) ve anahtar yokken ne yapılacağını söyleyen açıklayıcı bir hata döndürün. Anahtarı asla kod içine yazmayın.

5. **Testleri ekleyin.** Canlı API'ye bağlı test yazmayın; sentetik yanıtlarla ayrıştırma ve parametre mantığını test edin (mevcut modüllerdeki desene bakın).

6. **Kapsamı ölçün.** Sağlayıcının yayınladığı toplam ile modülün eriştiğini sayan bir bölüm `audit.py`'ye eklenmeli. Bu projede kapsam iddia değil ölçümdür.

## Araç tasarım ilkeleri

- **Keşiften veriye giden yol net olsun.** Bir LLM, kod bilmeden liste → arama → tanım → veri sırasını izleyebilmeli.
- **Önizleme ile indirmeyi ayırın.** Sohbete büyük veri basmayın; analiz için `download_*` araçları diske yazsın.
- **Docstring'ler araç açıklamasıdır.** Parametreleri ve kabul edilen değerleri örnekle yazın; LLM yalnızca bunu görür.
- **Kaynağın sınırını gizlemeyin.** Bir katman eksikse (tidy edilemeyen dosya, erişilemeyen veritabanı) bunu çıktıda söyleyin; sessizce boş dönmeyin.

## Sağlayıcı modülü şablonu

```python
"""Client for <SAĞLAYICI> (<adres>).

Erişim yolu: <API / SDMX / JSON backend / dosya indirme>
Anahtar: <gerekli mi, nereden alınır>
"""

from __future__ import annotations

import httpx
import pandas as pd

BASE = "https://..."


class SaglayiciError(RuntimeError):
    """Raised for <SAĞLAYICI> API errors."""


class SaglayiciClient:
    def __init__(self, timeout: float = 120.0) -> None:
        self._timeout = timeout

    def list_tables(self) -> list[dict]:
        """Katalog: erişilebilir tabloların listesi."""
        raise NotImplementedError

    def get_data(self, table_id: str, start: str = "", end: str = "") -> pd.DataFrame:
        """Tek tabloyu tidy DataFrame olarak döndürür."""
        raise NotImplementedError
```

## Sıradaki sağlayıcılar

Öncelik sırası (katkıya açık):

1. **BDDK** — bankacılık: haftalık kredi verileri, sektör bilançosu, FinTürk il bazında. Bülten uygulamaları JavaScript tabanlı; JSON backend keşfi gerekiyor.
2. **HMB** — kamu maliyesi: merkezi yönetim bütçe gerçekleşmeleri, borç istatistikleri. Excel yayınları; `tidy.py` yeniden kullanılabilir.
3. **EPİAŞ Şeffaflık** — enerji piyasası; dokümante REST API'si var.
4. **Belediye açık veri portalları** — CKAN standardı, tek modül birden çok belediyeyi kapsayabilir.

## Lisans

Katkılar MIT lisansı altında kabul edilir.
