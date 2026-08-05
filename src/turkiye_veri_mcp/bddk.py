"""Client for BDDK (Bankacılık Düzenleme ve Denetleme Kurumu) FinTürk data.

Endpoint discovered via browser DevTools (2026-08), FinTürk - İllere Göre
interactive bulletin (bddk.org.tr/BultenFinturk):

  POST https://www.bddk.org.tr/BultenFinturk/tr/Home/VeriGetir
  Content-Type: application/x-www-form-urlencoded (jQuery-style form data)
  Body: tabloNo=<int>&donem=<YYYY-M>&tarafList[0]=<code>&sehirList[0]=<code|HEPSI>
        (tarafList/sehirList are repeatable for multi-select: [0], [1], ...)

Response is jqGrid-shaped JSON, verified against a real capture (tabloNo=1,
donem=2026-6, tarafList=[10001], sehirList=[HEPSI], 81 rows):

  {"success": true, "Json": {
      "colNames": [...Turkish display labels...],
      "colModels": [{"name": "EftKodu", ...}, {"name": "Yil", ...}, ...],
      "data": {"page": "1", "total": "1", "records": "1",
                "rows": [{"cell": [10001, 2026, 6, "ADANA", "SEKTÖR",
                                    631565779, 605673070, 25892709, 180480299]}, ...]},
      "uyari": null
  }}

Column identity comes from colModels' "name" field, in order; each row's
"cell" array is positional and must be zipped against that same order.

STATUS: tabloNo=1 (Krediler, Bin TL) verified end-to-end with a real
response. Not yet confirmed: the full range of tabloNo (other "Bilgi"
dropdown options -- likely more tables exist, same endpoint), taraf codes
beyond 10001 ("SEKTÖR" -- Fonksiyon/Sahiplik Grubu breakdowns mentioned on
the BDDK site probably have their own codes), individual city codes (only
"HEPSI" tried), or whether "donem" values outside the current one work
without a prior page load (untested cookie/session requirement).
"""

from __future__ import annotations

from typing import Any

import httpx
import pandas as pd

BASE = "https://www.bddk.org.tr/BultenFinturk"
DATA_URL = f"{BASE}/tr/Home/VeriGetir"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": f"{BASE}/tr",
    "X-Requested-With": "XMLHttpRequest",
}

# Bilinen tabloNo -> ne olduğu. Tümü doğrulandı (2026-08-04): 1-4 tek tek
# gerçek yanıtla kontrol edildi, 5-7 kullanıcı tarafından FinTürk arayüzünde
# ayrıca kontrol edilip aynı sıralı örüntüyü (dropdown sırası = tabloNo)
# doğruladı.
KNOWN_TABLES = {
    1: "Krediler (Bin TL)",
    2: "Mevduat (Bin TL)",
    3: "Bireysel Bankacılık (Bin TL)",
    4: "Seçilmiş Sektörel Krediler (Bin TL)",
    5: "Oranlar (%)",
    6: "Şubeler (Adet) ve Nüfusa Göre Dağılım (TL)",
    7: "Altın Kredileri ve Altın Mevduatı (Bin TL)",
}

# Bilinen taraf (Grup) kodları -> ne olduğu. Kullanıcı FinTürk arayüzünde
# dropdown sırasını (SEKTÖR, MEVDUAT, KALKINMA VE YATIRIM, KATILIM,
# YABANCI, KAMU, YERLİ ÖZEL) tek tek 10001-10007 karşılığıyla doğruladı
# (2026-08-04) -- aynı sıralı örüntü tabloNo'da da görülmüştü.
TARAF_CODES = {
    "10001": "SEKTÖR",
    "10002": "MEVDUAT",
    "10003": "KALKINMA VE YATIRIM",
    "10004": "KATILIM",
    "10005": "YABANCI",
    "10006": "KAMU",
    "10007": "YERLİ ÖZEL",
}

# Geçerli 'sehirList' değerleri -- BDDK'nın KENDİ yanıtındaki "Sehir"
# alanından birebir alındı (2026-08-04, tabloNo=1 yanıtı), tahmin/üretim
# DEĞİL. Türkçe büyük harf kuralına dikkat: İ (noktalı) ve I (noktasız)
# BDDK'nın kendi yazdığı gibi korunmuştur -- "İSTANBUL" ASCII "ISTANBUL"
# değildir, sehirList'e yanlış varyant gönderilirse BDDK'nın API'si o ili
# sessizce atlar (hata vermez), bu yüzden bu liste elle üretilmedi.
SEHIR_CODES = [
    "ADANA", "ADIYAMAN", "AFYONKARAHİSAR", "AĞRI", "AKSARAY", "AMASYA",
    "ANKARA", "ANTALYA", "ARDAHAN", "ARTVİN", "AYDIN", "BALIKESİR",
    "BARTIN", "BATMAN", "BAYBURT", "BİLECİK", "BİNGÖL", "BİTLİS", "BOLU",
    "BURDUR", "BURSA", "ÇANAKKALE", "ÇANKIRI", "ÇORUM", "DENİZLİ",
    "DİYARBAKIR", "DÜZCE", "EDİRNE", "ELAZIĞ", "ERZİNCAN", "ERZURUM",
    "ESKİŞEHİR", "GAZİANTEP", "GİRESUN", "GÜMÜŞHANE", "HAKKARİ", "HATAY",
    "IĞDIR", "ISPARTA", "İSTANBUL", "İZMİR", "KAHRAMANMARAŞ", "KARABÜK",
    "KARAMAN", "KARS", "KASTAMONU", "KAYSERİ", "KIRIKKALE", "KIRKLARELİ",
    "KIRŞEHİR", "KİLİS", "KOCAELİ", "KONYA", "KÜTAHYA", "MALATYA",
    "MANİSA", "MARDİN", "MERSİN", "MUĞLA", "MUŞ", "NEVŞEHİR", "NİĞDE",
    "ORDU", "OSMANİYE", "RİZE", "SAKARYA", "SAMSUN", "SİİRT", "SİNOP",
    "SİVAS", "ŞANLIURFA", "ŞIRNAK", "TEKİRDAĞ", "TOKAT", "TRABZON",
    "TUNCELİ", "UŞAK", "VAN", "YALOVA", "YOZGAT", "YURT DIŞI", "ZONGULDAK",
]  # 81 il + "YURT DIŞI" = 82 kayıt; "HEPSİ" ile tek seferde hepsi de gelir.


class BddkError(RuntimeError):
    """Raised for BDDK data-access errors."""


class BddkClient:
    def __init__(self, timeout: float = 60.0) -> None:
        self._timeout = timeout

    def get_data(
        self,
        tablo_no: int = 1,
        donem: str = "",
        taraf_list: list[str] | None = None,
        sehir_list: list[str] | None = None,
    ) -> dict[str, Any]:
        """Ham VeriGetir yanıtındaki 'Json' gövdesini döndürür.

        Args:
            tablo_no: "Bilgi" seçimine karşılık gelen tablo numarası.
            donem: "YYYY-M" biçiminde dönem, ör. "2026-6".
            taraf_list: "Grup" seçimleri (varsayılan ["10001"] = Sektör).
            sehir_list: Şehir seçimleri (varsayılan ["HEPSI"]).
        """
        taraf_list = taraf_list or ["10001"]
        sehir_list = sehir_list or ["HEPSI"]

        bad_taraf = [t for t in taraf_list if t not in TARAF_CODES]
        if bad_taraf:
            raise ValueError(
                f"Bilinmeyen taraf (Grup) kodu: {bad_taraf}. "
                f"Geçerli kodlar: {list(TARAF_CODES)}"
            )
        # "HEPSI" özel bir seçim; onun dışındakiler BDDK'nın kendi il
        # adı listesiyle (Türkçe İ/I ayrımı dahil) eşleşmeli -- yoksa
        # BDDK hata vermeden o ili sessizce atlar.
        bad_sehir = [s for s in sehir_list if s != "HEPSI" and s not in SEHIR_CODES]
        if bad_sehir:
            raise ValueError(
                f"Bilinmeyen şehir adı: {bad_sehir}. Türkçe büyük harf "
                "kuralına dikkat edin (İSTANBUL, IĞDIR gibi -- ASCII 'I' "
                "değil). Geçerli adlar için SEHIR_CODES listesine bakın."
            )

        form: dict[str, str] = {"tabloNo": str(tablo_no), "donem": donem}
        for i, value in enumerate(taraf_list):
            form[f"tarafList[{i}]"] = value
        for i, value in enumerate(sehir_list):
            form[f"sehirList[{i}]"] = value

        with httpx.Client(timeout=self._timeout, headers=_HEADERS, follow_redirects=True) as client:
            response = client.post(DATA_URL, data=form)
        if response.status_code != 200:
            raise BddkError(f"BDDK VeriGetir returned HTTP {response.status_code}")
        payload = response.json()
        if not payload.get("success"):
            raise BddkError(f"BDDK VeriGetir reported failure: {payload}")
        return payload["Json"]

    def get_dataframe(self, **kwargs: Any) -> pd.DataFrame:
        """get_data'nın sonucunu tidy bir DataFrame'e çevirir."""
        return jqgrid_to_frame(self.get_data(**kwargs))


def jqgrid_to_frame(json_body: dict[str, Any]) -> pd.DataFrame:
    """BDDK'nın jqGrid biçimli yanıtını (colModels + data.rows[].cell) DataFrame'e çevirir."""
    col_models = json_body.get("colModels") or []
    columns = [c["name"] for c in col_models]
    if not columns:
        raise BddkError("Response'ta 'colModels' bulunamadı, sütun adları çözülemedi.")

    rows_raw = (json_body.get("data") or {}).get("rows")
    if rows_raw is None:
        raise BddkError("Response'ta 'data.rows' bulunamadı.")

    records = [row["cell"] for row in rows_raw]
    frame = pd.DataFrame(records, columns=columns)
    for col in frame.columns:
        if col not in ("Sehir", "Grup"):
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
    return frame
