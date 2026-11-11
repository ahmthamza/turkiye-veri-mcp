"""BDDK (Bankacılık Düzenleme ve Denetleme Kurumu) interactive bulletins.

Everything below was captured from the live site on 2026-11-11 (page HTML,
the page's own JavaScript, and live requests) -- not guessed.

Bulletins and endpoints
-----------------------
FinTürk - İllere Göre (quarterly, province level)
    page  GET  https://www.bddk.org.tr/BultenFinturk/tr
    data  POST /BultenFinturk/tr/Home/VeriGetir
          tabloNo=<1-7>&donem=<YYYY-M>&tarafList[i]=<code>&sehirList[i]=<name>
    The page JS always sends a concrete donem (from #ddlDonem) and the
    "all provinces" option is the literal value "HEPSİ" (dotted capital İ).
    ROOT CAUSE of the old HTTP 500: the client sent donem="" when no period
    was given and "HEPSI" (ASCII I) as the all-provinces value; the server
    answers both with an ASP.NET "Runtime Error" page (HTTP 500). Cookies,
    tokens, TLS, WAF and the page "priming" visit are all irrelevant: a cold
    POST with the right values returns 200 in ~0.2 s.

Aylık Bülten (monthly, by bank group)
    page  GET  https://www.bddk.org.tr/BultenAylik/tr
    data  POST /BultenAylik/tr/Home/BasitRaporGetir
          tabloNo=<1-17>&yil=<YYYY>&ay=<M>&paraBirimi=<TL|USD>&taraf=<code>(repeated)
    Returns {"success": true, "Json": {colNames, colModels, data.rows, caption,
    uyari, duyurular}} or {"success": false, "error": "<Turkish message>"} --
    the server's own message for an unpublished period / unsupported currency.

Haftalık Bülten (weekly, by bank group) -- server-side session state
    page  GET  https://www.bddk.org.tr/BultenHaftalik/tr
    table POST /BultenHaftalik/              tabloId=<289..297>   (+ anti-forgery token)
    group POST /BultenHaftalik/tr/Home/TarafSec       tarafKodu=<code>
    week  POST /BultenHaftalik/tr/Home/DonemDegistir  yil, donemId, para
    Each POST returns the full page; the table is parsed from #TabloExcel2
    (5-decimal values) and the row ids from #Tablo.
    series POST /BultenHaftalik/tr/Home/KiyaslamaJsonGetir
          dil=tr&tarih=DD.MM.YYYY&id=<row id>&parabirimi=TRY|USD&sutun=<1..n>&tarafKodu=<code>&gun=..
    -> {"Baslik", "XEkseni": [dates], "YEkseni": [values]}: always the 13
    weekly points ending at the last week <= tarih ("gun" is ignored).

Günlük Bülten (daily) -- only the latest published day is offered
    page  GET  https://www.bddk.org.tr/BultenGunluk/tr  (#excelExportTl / #excelExportUsd)
    series POST /BultenGunluk/tr/Home/KiyaslamaJsonGetir (needs the page's
          anti-forgery token + cookie): the last ~7 business days of one row.

Bank-group ("taraf") codes are NOT the same across bulletins (e.g. 10003 is
"Kalkınma ve Yatırım" in FinTürk/Haftalık but "Katılım" in Aylık), so each
bulletin's code->name map is read from that bulletin's own page and every
response is checked against the requested group name.

TLS: since 2026-10 BDDK serves a leaf issued by "GlobalSign GCC R46 OV TLS CA
2025" and sends the full chain; plain certifi verification works. The
bundled intermediate (certs/) is only extra insurance should the server drop
it again. Verification is never disabled.
"""

from __future__ import annotations

import datetime as _dt
import html as _html
import re
import ssl
import threading
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import certifi
import httpx
import pandas as pd

HOST = "https://www.bddk.org.tr"
FINTURK_PAGE = f"{HOST}/BultenFinturk/tr"
FINTURK_DATA = f"{HOST}/BultenFinturk/tr/Home/VeriGetir"
AYLIK_PAGE = f"{HOST}/BultenAylik/tr"
AYLIK_DATA = f"{HOST}/BultenAylik/tr/Home/BasitRaporGetir"
HAFTALIK_PAGE = f"{HOST}/BultenHaftalik/tr"
HAFTALIK_TABLO = f"{HOST}/BultenHaftalik/"
HAFTALIK_TARAF = f"{HOST}/BultenHaftalik/tr/Home/TarafSec"
HAFTALIK_DONEM = f"{HOST}/BultenHaftalik/tr/Home/DonemDegistir"
HAFTALIK_SERI = f"{HOST}/BultenHaftalik/tr/Home/KiyaslamaJsonGetir"
GUNLUK_PAGE = f"{HOST}/BultenGunluk/tr"
GUNLUK_SERI = f"{HOST}/BultenGunluk/tr/Home/KiyaslamaJsonGetir"

_INTERMEDIATES = ["globalsign_gcc_r46_ov_tls_ca_2025.pem"]

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "tr-TR,tr;q=0.9",
}
_XHR = {"X-Requested-With": "XMLHttpRequest"}

# Politeness / time limits
MIN_INTERVAL = 0.8          # seconds between two requests to bddk.org.tr
REQUEST_TIMEOUT = httpx.Timeout(20.0, connect=8.0)
TOTAL_BUDGET = 55.0         # seconds per tool call (MCP clients often time out at 60 s)
META_TTL = 3 * 3600.0       # landing-page metadata cache

# Range limits per call (keep a call well inside TOTAL_BUDGET)
MAX_FINTURK_PERIODS = 40
MAX_AYLIK_PERIODS = 36
MAX_HAFTALIK_SERI_WINDOWS = 16   # 13 weeks each -> ~4 years
MAX_HAFTALIK_TARAF = 7

AY_ADLARI = {
    "Ocak": 1, "Şubat": 2, "Mart": 3, "Nisan": 4, "Mayıs": 5, "Haziran": 6,
    "Temmuz": 7, "Ağustos": 8, "Eylül": 9, "Ekim": 10, "Kasım": 11, "Aralık": 12,
}

# Aylık Bülten: group codes that are not in the page's dropdown but that the
# live endpoint answers with a correctly-labelled group (enumerated
# 9998..10031 on 2026-11-11; 10011-10016 exist, everything else returns 0 rows).
# Bank counts add up: Katılım 10 = 4+3+3, Kalkınma ve Yatırım 21 = 14+4+3.
AYLIK_GIZLI_TARAF = {
    "10011": "Katılım-Yerli Özel",
    "10012": "Katılım-Kamu",
    "10013": "Katılım-Yabancı",
    "10014": "Kalkınma ve Yatırım-Yerli Özel",
    "10015": "Kalkınma ve Yatırım-Kamu",
    "10016": "Kalkınma ve Yatırım-Yabancı",
}

# FinTürk column units: the unit is in the table title (captured from the page).
FINTURK_ID_COLS = ("EftKodu", "Yil", "Ay", "Sehir", "Grup")

# First published period per FinTürk table where it is later than the
# period list's start (2007-12). Found live 2026-11-11 by bisection: table 7
# (gold) returns HTTP 500 for 2015-6 and data from 2015-9; tables 1-6 all
# return data for 2007-12.
FINTURK_TABLE_START = {7: "2015-9"}


class BddkError(RuntimeError):
    """Raised for BDDK data-access errors (message is user-facing)."""


# --------------------------------------------------------------------------
# HTTP layer: verified TLS, pacing, bounded time, one retry on transport errors
# --------------------------------------------------------------------------

_SSL_CTX: ssl.SSLContext | None = None
_pace_lock = threading.Lock()
_last_request = 0.0


def ssl_context() -> ssl.SSLContext:
    """certifi roots + bundled GlobalSign intermediate(s). Always verifying."""
    global _SSL_CTX
    if _SSL_CTX is None:
        ctx = ssl.create_default_context(cafile=certifi.where())
        cert_dir = Path(__file__).parent / "certs"
        for name in _INTERMEDIATES:
            path = cert_dir / name
            if path.exists():
                ctx.load_verify_locations(cadata=path.read_text())
        _SSL_CTX = ctx
    return _SSL_CTX


def _pace() -> None:
    global _last_request
    with _pace_lock:
        wait = MIN_INTERVAL - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()


class Session:
    """One httpx client + a deadline. Use as a context manager."""

    def __init__(self, budget: float = TOTAL_BUDGET) -> None:
        self.deadline = time.monotonic() + budget
        self.budget = budget
        self.n_requests = 0
        self.client = httpx.Client(
            headers=_HEADERS,
            timeout=REQUEST_TIMEOUT,
            verify=ssl_context(),
            follow_redirects=True,
        )

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc: object) -> None:
        self.client.close()

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(2):
            if self.remaining() < 3:
                raise BddkError(
                    f"BDDK isteği zaman bütçesi ({self.budget:.0f} sn) içinde "
                    "tamamlanamadı; dönem aralığını daraltın veya tekrar deneyin."
                )
            _pace()
            self.n_requests += 1
            try:
                return self.client.request(method, url, **kw)
            except ssl.SSLError as exc:
                raise _tls_error(exc) from exc
            except httpx.ConnectError as exc:
                if "CERTIFICATE_VERIFY" in str(exc).upper():
                    raise _tls_error(exc) from exc
                last_exc = exc
            except (httpx.TimeoutException, httpx.RemoteProtocolError, httpx.ReadError) as exc:
                last_exc = exc
            if attempt == 0:
                time.sleep(1.5)
        raise BddkError(
            f"BDDK sunucusuna ulaşılamadı ({type(last_exc).__name__}: {last_exc}). "
            "Site geçici olarak erişilemez olabilir; biraz sonra tekrar deneyin."
        )

    def get(self, url: str, **kw: Any) -> httpx.Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw: Any) -> httpx.Response:
        return self.request("POST", url, **kw)


def _tls_error(exc: Exception) -> BddkError:
    return BddkError(
        "BDDK sunucusunun TLS sertifikası doğrulanamadı "
        f"({exc}). Doğrulama kapatılmadı; veri alınmadı. Sertifika zinciri "
        "değişmiş olabilir -- certs/README.md'ye bakın."
    )


def _check_html(resp: httpx.Response, what: str) -> str:
    if resp.status_code != 200:
        raise BddkError(f"BDDK {what} HTTP {resp.status_code} döndürdü.")
    return resp.text


# --------------------------------------------------------------------------
# Small HTML helpers (stdlib only)
# --------------------------------------------------------------------------

class _TableGrabber(HTMLParser):
    """Collect rows of <table id=...> as lists of cells {text, attrs, th}."""

    def __init__(self, table_id: str) -> None:
        super().__init__(convert_charrefs=True)
        self.table_id = table_id
        self.depth = 0          # nesting depth inside the target table
        self.rows: list[list[dict[str, Any]]] = []
        self.row_attrs: list[dict[str, str]] = []
        self._cell: dict[str, Any] | None = None
        self._row: list[dict[str, Any]] | None = None
        self.found = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag == "table":
            if self.depth == 0 and a.get("id") == self.table_id and not self.found:
                self.depth = 1
                self.found = True
                return
            if self.depth:
                self.depth += 1
            return
        if not self.depth:
            return
        if tag == "tr":
            self._row = []
            self.rows.append(self._row)
            self.row_attrs.append(a)
        elif tag in ("td", "th") and self._row is not None:
            self._cell = {"text": "", "attrs": a, "th": tag == "th", "italic": False}
            self._row.append(self._cell)
        elif tag == "span" and self._cell is not None and "italic" in a.get("style", ""):
            self._cell["italic"] = True
        elif tag == "br" and self._cell is not None:
            self._cell["text"] += " "

    def handle_endtag(self, tag: str) -> None:
        if not self.depth:
            return
        if tag == "table":
            self.depth -= 1
        elif tag in ("td", "th"):
            self._cell = None
        elif tag == "tr":
            self._row = None

    def handle_data(self, data: str) -> None:
        if self.depth and self._cell is not None:
            self._cell["text"] += data


def _clean(text: str) -> str:
    text = text.replace("​", "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def parse_table(page: str, table_id: str) -> list[list[dict[str, Any]]]:
    g = _TableGrabber(table_id)
    g.feed(page)
    if not g.found:
        raise BddkError(f"BDDK sayfasında '{table_id}' tablosu bulunamadı (sayfa yapısı değişmiş olabilir).")
    for row in g.rows:
        for cell in row:
            cell["text"] = _clean(cell["text"])
    return g.rows


def parse_number(text: str) -> float | None:
    """Turkish-formatted number ('18.535.605,59', '62186,36', '-1.234') -> float."""
    t = _clean(text).replace(" ", "")
    if t in ("", "-", "—"):
        return None
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    elif t.count(".") >= 1 and re.fullmatch(r"-?\d{1,3}(\.\d{3})+", t):
        t = t.replace(".", "")
    try:
        return float(t)
    except ValueError:
        raise BddkError(f"Sayıya çevrilemeyen değer: {text!r}") from None


def parse_number_or_none(text: str) -> float | None:
    try:
        return parse_number(text)
    except BddkError:
        return None


def _select_options(page: str, select_id: str) -> list[dict[str, Any]]:
    m = re.search(rf'<select[^>]*id="{re.escape(select_id)}"[^>]*>(.*?)</select>', page, flags=re.S)
    if not m:
        raise BddkError(f"BDDK sayfasında '{select_id}' seçim kutusu bulunamadı (sayfa yapısı değişmiş olabilir).")
    out = []
    for attrs, text in re.findall(r"<option([^>]*)>(.*?)</option>", m.group(1), flags=re.S):
        v = re.search(r'value="([^"]*)"', attrs)
        cls = re.search(r'class="([^"]*)"', attrs)
        out.append({
            "value": _html.unescape(v.group(1)) if v else _clean(_html.unescape(text)),
            "text": _clean(_html.unescape(re.sub(r"<[^>]+>", "", text))),
            "selected": "selected" in attrs,
            "class": cls.group(1) if cls else "",
        })
    return out


_TR_FOLD = str.maketrans("İıŞşĞğÜüÖöÇçÂâÎî", "IiSsGgUuOoCcAaIi")


def tr_fold(text: str) -> str:
    """ASCII-folded upper-case key for tolerant Turkish name matching."""
    return _clean(text).translate(_TR_FOLD).upper()


def _turkish_upper(text: str) -> str:
    return text.replace("i", "İ").replace("ı", "I").upper()


# --------------------------------------------------------------------------
# Metadata cache
# --------------------------------------------------------------------------

_meta_cache: dict[str, tuple[float, Any]] = {}
_meta_lock = threading.Lock()


def _cached(key: str, loader, session: Session | None = None):
    with _meta_lock:
        hit = _meta_cache.get(key)
        if hit and time.monotonic() - hit[0] < META_TTL:
            return hit[1]
    if session is None:
        with Session() as s:
            value = loader(s)
    else:
        value = loader(session)
    with _meta_lock:
        _meta_cache[key] = (time.monotonic(), value)
    return value


def clear_cache() -> None:
    with _meta_lock:
        _meta_cache.clear()


def _bad_choice(kind: str, given: Any, valid: list[str], limit: int = 40) -> BddkError:
    shown = ", ".join(valid[:limit]) + (" ..." if len(valid) > limit else "")
    return BddkError(f"Geçersiz {kind}: {given!r}. Geçerli değerler: {shown}")


# ==========================================================================
# FinTürk (province level, quarterly)
# ==========================================================================

@dataclass
class FinturkMeta:
    tables: dict[int, str]
    periods: list[str]                 # newest first, e.g. "2026-9"
    taraf: dict[str, str]              # code -> name
    sehir: list[str]                   # includes "HEPSİ" first


def parse_finturk_page(page: str) -> FinturkMeta:
    tables = {int(o["value"]): o["text"] for o in _select_options(page, "ddlTablo")}
    periods = [o["value"] for o in _select_options(page, "ddlDonem")]
    taraf = {o["value"]: o["text"] for o in _select_options(page, "ddlTaraf")}
    sehir = [o["value"] for o in _select_options(page, "ddlSehir")]
    if not (tables and periods and taraf and sehir):
        raise BddkError("FinTürk sayfasından seçenek listeleri okunamadı.")
    return FinturkMeta(tables, periods, taraf, sehir)


def finturk_meta(session: Session | None = None) -> FinturkMeta:
    return _cached(
        "finturk",
        lambda s: parse_finturk_page(_check_html(s.get(FINTURK_PAGE), "FinTürk sayfası")),
        session,
    )


def _period_key(p: str) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d{4})\s*[-/.]\s*(\d{1,2})\s*", p)
    if not m:
        raise BddkError(f"Dönem 'YYYY-M' biçiminde olmalı (ör. 2026-9 veya 2026-09): {p!r}")
    return int(m.group(1)), int(m.group(2))


def _resolve_periods(
    available: list[str], donem: str, donem_bitis: str, kind: str, limit: int
) -> list[str]:
    """available: newest first. Returns chosen periods oldest -> newest."""
    keyed = {(_period_key(p)): p for p in available}
    first, last = available[-1], available[0]
    if not donem and not donem_bitis:
        return [available[0]]

    def pick(p: str) -> tuple[int, int]:
        k = _period_key(p)
        if k not in keyed:
            raise BddkError(
                f"{kind} için {p!r} dönemi yayımlanmamış/yok. Mevcut aralık: "
                f"{first} .. {last} (en son yayımlanan: {last}). "
                f"Son dönemler: {', '.join(available[:8])}"
            )
        return k

    start = pick(donem) if donem else _period_key(first)
    end = pick(donem_bitis) if donem_bitis else (start if donem else _period_key(last))
    if start > end:
        start, end = end, start
    chosen = sorted(k for k in keyed if start <= k <= end)
    if len(chosen) > limit:
        raise BddkError(
            f"İstenen aralık {len(chosen)} dönem içeriyor; tek çağrıda en fazla {limit} "
            "dönem alınır (BDDK'ya nazik davranmak ve zaman aşımını önlemek için). "
            "Aralığı bölerek birkaç çağrı yapın."
        )
    return [keyed[k] for k in chosen]


def _resolve_sehir(meta: FinturkMeta, sehir_list: list[str] | None) -> list[str]:
    if not sehir_list:
        return ["HEPSİ"]
    by_fold: dict[str, list[str]] = {}
    for s in meta.sehir:
        by_fold.setdefault(tr_fold(s), []).append(s)
    out = []
    for raw in sehir_list:
        exact = _turkish_upper(_clean(raw))
        if exact in meta.sehir:
            out.append(exact)
            continue
        cands = by_fold.get(tr_fold(raw), [])
        if len(cands) == 1:
            out.append(cands[0])
            continue
        raise _bad_choice("şehir", raw, meta.sehir, limit=90)
    if "HEPSİ" in out and len(out) > 1:
        raise BddkError("'HEPSİ' diğer şehirlerle birlikte seçilemez; ya HEPSİ ya da il adları verin.")
    return out


def _resolve_taraf(valid: dict[str, str], taraf_list: list[str] | None, default: str = "10001") -> list[str]:
    taraf_list = [str(t).strip() for t in (taraf_list or [default])]
    out = []
    for t in taraf_list:
        if t in valid:
            out.append(t)
            continue
        by_name = [c for c, n in valid.items() if tr_fold(n) == tr_fold(t)]
        if len(by_name) == 1:
            out.append(by_name[0])
            continue
        raise _bad_choice("taraf (grup) kodu", t, [f"{c}={n}" for c, n in valid.items()])
    return list(dict.fromkeys(out))


def jqgrid_to_frame(json_body: dict[str, Any]) -> pd.DataFrame:
    """jqGrid body (colModels + data.rows[].cell) -> DataFrame (column names from colModels)."""
    col_models = json_body.get("colModels") or []
    columns = [c["name"] for c in col_models]
    if not columns:
        raise BddkError("Yanıtta 'colModels' yok; sütun adları çözülemedi.")
    rows_raw = (json_body.get("data") or {}).get("rows")
    if rows_raw is None:
        raise BddkError("Yanıtta 'data.rows' yok.")
    records = [row["cell"] for row in rows_raw]
    bad = [r for r in records if len(r) != len(columns)]
    if bad:
        raise BddkError(f"Yanıttaki satır uzunluğu ({len(bad[0])}) sütun sayısıyla ({len(columns)}) uyuşmuyor.")
    return pd.DataFrame(records, columns=columns)


def finturk_frame(json_body: dict[str, Any]) -> tuple[pd.DataFrame, dict[str, str]]:
    """FinTürk VeriGetir 'Json' -> (frame, {column: Turkish label}); value columns numeric."""
    frame = jqgrid_to_frame(json_body)
    labels = dict(zip([c["name"] for c in json_body["colModels"]], json_body.get("colNames") or []))
    for col in frame.columns:
        if col not in ("Sehir", "Grup"):
            converted = pd.to_numeric(frame[col], errors="coerce")
            lost = converted.isna() & frame[col].notna() & (frame[col].astype(str).str.strip() != "")
            if lost.any():
                raise BddkError(f"FinTürk '{col}' sütununda sayıya çevrilemeyen değer: {frame[col][lost].iloc[0]!r}")
            frame[col] = converted
    return frame, labels


class BddkClient:
    """FinTürk client (name kept for backwards compatibility)."""

    def __init__(self, budget: float = TOTAL_BUDGET) -> None:
        self._budget = budget

    def get_dataframe(
        self,
        tablo_no: int = 1,
        donem: str = "",
        taraf_list: list[str] | None = None,
        sehir_list: list[str] | None = None,
        donem_bitis: str = "",
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        with Session(self._budget) as s:
            meta = finturk_meta(s)
            if int(tablo_no) not in meta.tables:
                raise _bad_choice("tablo_no", tablo_no, [f"{k}={v}" for k, v in meta.tables.items()])
            periods = _resolve_periods(meta.periods, donem, donem_bitis, "FinTürk (çeyreklik; ay 3, 6, 9, 12)", MAX_FINTURK_PERIODS)
            tarafs = _resolve_taraf(meta.taraf, taraf_list)
            sehirs = _resolve_sehir(meta, sehir_list)
            start = FINTURK_TABLE_START.get(int(tablo_no))
            if start and _period_key(periods[0]) < _period_key(start):
                raise BddkError(
                    f"FinTürk tablo {tablo_no} ({meta.tables[int(tablo_no)]}) {start} döneminden itibaren "
                    f"yayımlanıyor; daha eski dönem istendi ({periods[0]})."
                )
            frames, labels, warnings = [], {}, []
            for p in periods:
                form = {"tabloNo": str(int(tablo_no)), "donem": p}
                for i, v in enumerate(tarafs):
                    form[f"tarafList[{i}]"] = v
                for i, v in enumerate(sehirs):
                    form[f"sehirList[{i}]"] = v
                resp = s.post(FINTURK_DATA, data=form, headers=_XHR)
                if resp.status_code != 200:
                    raise BddkError(
                        f"BDDK FinTürk sunucusu HTTP {resp.status_code} döndürdü (tablo {tablo_no}, dönem {p}). "
                        "Parametreler sitenin kendi listeleriyle doğrulandığı için bu genellikle o tablonun o "
                        "dönemde bulunmadığı ya da sunucu tarafı bir hata olduğu anlamına gelir."
                    )
                payload = resp.json()
                if not payload.get("success"):
                    raise BddkError(f"FinTürk hata döndürdü: {payload.get('error') or payload}")
                frame, labels = finturk_frame(payload["Json"])
                warnings += _check_finturk(frame, p, tarafs, sehirs, meta)
                frames.append(frame)
        frame = pd.concat(frames, ignore_index=True)
        info = {
            "tablo": meta.tables[int(tablo_no)],
            "birim": _finturk_unit(meta.tables[int(tablo_no)]),
            "donemler": periods,
            "sutun_etiketleri": labels,
            "taraf": {t: meta.taraf[t] for t in tarafs},
            "en_son_donem": meta.periods[0],
        }
        if warnings:
            info["uyari"] = sorted(set(warnings))
        return frame, info


def _finturk_unit(table_name: str) -> str:
    m = re.findall(r"\(([^)]*)\)", table_name)
    if table_name.startswith("Şubeler"):
        return "Şube sayıları adet; nüfusa göre dağılım sütunları TL (kişi başı)."
    return m[-1] if m else ""


def _check_finturk(frame: pd.DataFrame, donem: str, tarafs: list[str], sehirs: list[str],
                   meta: FinturkMeta) -> list[str]:
    """Raise on anything inconsistent with the request; return notes for
    legitimately absent rows (e.g. development/investment banks hold no
    deposits, so table 2 has no 10003 rows; a group may have no branch in a
    province). Province names were validated against BDDK's own list first,
    so an absent province means 'no data', not a typo."""
    if frame.empty:
        raise BddkError(f"FinTürk {donem} dönemi için istenen grup/il seçiminde hiç veri yok.")
    y, m = _period_key(donem)
    if not ((frame["Yil"] == y) & (frame["Ay"] == m)).all():
        raise BddkError(f"FinTürk yanıtındaki dönem ({frame['Yil'].iloc[0]}-{frame['Ay'].iloc[0]}) istenen {donem} ile uyuşmuyor.")
    got_codes = {str(int(c)) for c in frame["EftKodu"].unique()}
    extra = got_codes - set(tarafs)
    if extra:
        raise BddkError(f"FinTürk yanıtında istenmeyen grup kodları var: {sorted(extra)}")
    notes = []
    for code in tarafs:
        if code not in got_codes:
            notes.append(f"{donem}: {code} {meta.taraf[code]} için bu tabloda veri yok (BDDK satır döndürmedi).")
            continue
        names = set(frame.loc[frame["EftKodu"] == int(code), "Grup"])
        if {tr_fold(n) for n in names} != {tr_fold(meta.taraf[code])}:
            raise BddkError(f"FinTürk grup adı uyuşmuyor: kod {code} için {names} geldi, beklenen {meta.taraf[code]}.")
    if sehirs != ["HEPSİ"]:
        bad = set(frame["Sehir"]) - set(sehirs)
        if bad:
            raise BddkError(f"FinTürk yanıtında istenmeyen iller var: {sorted(bad)}")
        for code in tarafs:
            have = set(frame.loc[frame["EftKodu"] == int(code), "Sehir"])
            missing = [c for c in sehirs if c not in have]
            if code in got_codes and missing:
                notes.append(f"{donem}: {meta.taraf[code]} için şu illerde veri yok: {', '.join(missing)}")
    return notes


# ==========================================================================
# Aylık Bülten (monthly, bank groups)
# ==========================================================================

@dataclass
class AylikMeta:
    tables: dict[int, str]
    years: list[int]
    latest: tuple[int, int]            # (year, month) preselected on the page = latest published
    taraf: dict[str, str]              # dropdown codes
    para: list[str]
    taraf_gizli: dict[str, str] = field(default_factory=lambda: dict(AYLIK_GIZLI_TARAF))

    @property
    def all_taraf(self) -> dict[str, str]:
        return {**self.taraf, **self.taraf_gizli}


def parse_aylik_page(page: str) -> AylikMeta:
    tables = {int(o["value"]): o["text"] for o in _select_options(page, "TabloListesi")}
    yil_opts = _select_options(page, "ddlYil")
    ay_opts = _select_options(page, "ddlAy")
    years = [int(o["value"]) for o in yil_opts]
    sel_y = next((int(o["value"]) for o in yil_opts if o["selected"]), years[0])
    sel_m = next((int(o["value"]) for o in ay_opts if o["selected"]), None)
    if sel_m is None:
        raise BddkError("Aylık Bülten sayfasında seçili (en son) ay okunamadı.")
    taraf = {o["value"]: o["text"] for o in _select_options(page, "ddlTaraf")}
    para = [o["value"] for o in _select_options(page, "ddlParaBirimi")]
    return AylikMeta(tables, years, (sel_y, sel_m), taraf, para)


def aylik_meta(session: Session | None = None) -> AylikMeta:
    return _cached(
        "aylik",
        lambda s: parse_aylik_page(_check_html(s.get(AYLIK_PAGE), "Aylık Bülten sayfası")),
        session,
    )


def _aylik_periods(meta: AylikMeta, donem: str, donem_bitis: str) -> list[tuple[int, int]]:
    latest = meta.latest
    first = (min(meta.years), 1)
    if not donem and not donem_bitis:
        return [latest]
    start = _period_key(donem) if donem else first
    end = _period_key(donem_bitis) if donem_bitis else (start if donem else latest)
    for k in (start, end):
        if not 1 <= k[1] <= 12:
            raise BddkError(f"Ay 1-12 arasında olmalı: {k}")
        if k > latest:
            raise BddkError(
                f"Aylık Bülten'de {k[0]}-{k[1]:02d} henüz yayımlanmadı. En son yayımlanan dönem: "
                f"{latest[0]}-{latest[1]:02d}."
            )
    if start > end:
        start, end = end, start
    out, (y, m) = [], start
    while (y, m) <= end:
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    if len(out) > MAX_AYLIK_PERIODS:
        raise BddkError(
            f"İstenen aralık {len(out)} ay içeriyor; tek çağrıda en fazla {MAX_AYLIK_PERIODS} ay alınır. "
            "Aralığı bölerek birkaç çağrı yapın."
        )
    return out


_UNIT_RE = re.compile(r"\(([^()]*(?:TL|\$|USD)[^()]*)\)")


def aylik_frame(json_body: dict[str, Any]) -> pd.DataFrame:
    """BasitRaporGetir 'Json' -> tidy-ish wide frame (one row per item and group)."""
    frame = jqgrid_to_frame(json_body)
    labels = dict(zip([c["name"] for c in json_body["colModels"]], json_body.get("colNames") or []))
    rename = {"BankaAdi": "taraf", "BasitSira": "sira", "Ad": "kalem", "BasitFont": "bicim"}
    value_cols = [c for c in frame.columns if c not in rename]
    for col in value_cols:
        conv = pd.to_numeric(frame[col], errors="coerce")
        lost = conv.isna() & frame[col].notna() & (frame[col].astype(str).str.strip() != "")
        if lost.any():
            raise BddkError(f"Aylık '{col}' sütununda sayıya çevrilemeyen değer: {frame[col][lost].iloc[0]!r}")
        frame[col] = conv
    frame = frame.rename(columns=rename)
    frame = frame.rename(columns={c: (labels.get(c) or c) for c in value_cols})
    frame["kalem"] = frame["kalem"].map(_clean)
    return frame


def aylik_unit(caption: str) -> str:
    m = _UNIT_RE.search(caption or "")
    if m:
        return m.group(1).strip()
    if caption.startswith("Rasyolar") or "Rasyo" in caption:
        return "% (oran; birim kalem adında)"
    if caption.startswith("Diğer Bilgiler"):
        return "adet"
    return "kalem adında belirtilen birim"


class AylikClient:
    def __init__(self, budget: float = TOTAL_BUDGET) -> None:
        self._budget = budget

    def get_dataframe(
        self,
        tablo_no: int = 1,
        donem: str = "",
        donem_bitis: str = "",
        taraf_list: list[str] | None = None,
        para_birimi: str = "TL",
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        para = (para_birimi or "TL").strip().upper()
        with Session(self._budget) as s:
            meta = aylik_meta(s)
            if int(tablo_no) not in meta.tables:
                raise _bad_choice("tablo_no", tablo_no, [f"{k}={v}" for k, v in meta.tables.items()])
            if para not in meta.para:
                raise _bad_choice("para_birimi", para_birimi, meta.para)
            valid = meta.all_taraf
            tarafs = _resolve_taraf(valid, taraf_list)
            periods = _aylik_periods(meta, donem, donem_bitis)
            frames, caption, uyari = [], "", ""
            for y, m in periods:
                data = {"tabloNo": str(int(tablo_no)), "yil": str(y), "ay": str(m),
                        "paraBirimi": para, "taraf": tarafs}
                resp = s.post(AYLIK_DATA, data=data, headers=_XHR)
                try:
                    payload = resp.json()
                except ValueError:
                    raise BddkError(
                        f"Aylık Bülten HTTP {resp.status_code} döndürdü (JSON değil) -- tablo {tablo_no}, {y}-{m:02d}."
                    ) from None
                if not payload.get("success"):
                    raise BddkError(f"BDDK Aylık Bülten ({y}-{m:02d}): {_clean(str(payload.get('error')))}")
                body = payload["Json"]
                caption = body.get("caption") or ""
                uyari = body.get("uyari") or uyari
                cap_period = re.search(r"Dönem:\s*(\d{4})/(\d{1,2})", caption)
                # Some tables (Rasyolar, Diğer Bilgiler, ...) have no period in
                # the caption; when it is there it must match the request.
                if cap_period and (int(cap_period.group(1)), int(cap_period.group(2))) != (y, m):
                    raise BddkError(f"Aylık Bülten yanıtının dönemi ({caption!r}) istenen {y}-{m:02d} ile uyuşmuyor.")
                frame = aylik_frame(body)
                if frame.empty:
                    raise BddkError(f"Aylık Bülten {y}-{m:02d} için boş yanıt döndü.")
                got = {tr_fold(n) for n in frame["taraf"].unique()}
                want = {tr_fold(valid[t]) for t in tarafs}
                if got != want:
                    raise BddkError(
                        f"Aylık Bülten grup adları uyuşmuyor: gelen {sorted(frame['taraf'].unique())}, "
                        f"beklenen {[valid[t] for t in tarafs]}."
                    )
                name_to_code = {tr_fold(valid[t]): t for t in tarafs}
                frame.insert(0, "donem", f"{y}-{m:02d}")
                frame.insert(1, "taraf_kodu", frame["taraf"].map(lambda n: name_to_code[tr_fold(n)]))
                frames.append(frame)
        frame = pd.concat(frames, ignore_index=True)
        info = {
            "tablo": meta.tables[int(tablo_no)],
            "baslik": re.sub(r",\s*Dönem:.*$", "", caption),
            "birim": aylik_unit(caption),
            "para_birimi": para,
            "donemler": [f"{y}-{m:02d}" for y, m in periods],
            "taraf": {t: valid[t] for t in tarafs},
            "en_son_donem": f"{meta.latest[0]}-{meta.latest[1]:02d}",
            "dipnot": _clean(re.sub(r"<[^>]+>", " ", _html.unescape(uyari)))[:1500],
        }
        return frame, info


_SUTUNLAR = {1: "TP", 2: "YP", 3: "Toplam"}


def sutun_index(sutun: int | str) -> tuple[int, str]:
    """'TP'/'YP'/'TOPLAM' or 1/2/3 -> (index, title tag used in series 'Baslik')."""
    key = str(sutun).strip().upper()
    for i, name in _SUTUNLAR.items():
        if key in (str(i), name.upper()):
            return i, name
    raise _bad_choice("sutun", sutun, ["TP", "YP", "TOPLAM"])


def _check_series_title(baslik: str, para: str, sutun_ad: str, taraf_ad: str | None) -> None:
    """Series titles look like 'Toplam Krediler (2+10) (TRY) [Toplam] [Sektör]'."""
    problems = []
    if f"({'TRY' if para == 'TL' else 'USD'})" not in baslik:
        problems.append("para birimi")
    if f"[{sutun_ad}]" not in baslik:
        problems.append("sütun")
    if taraf_ad is not None and f"[{tr_fold(taraf_ad)}]" not in tr_fold(baslik):
        problems.append("taraf")
    if problems:
        raise BddkError(f"Seri başlığı ({baslik!r}) isteğe uymuyor ({', '.join(problems)}); veri döndürülmedi.")


# ==========================================================================
# Haftalık Bülten (weekly, bank groups; session-stateful pages)
# ==========================================================================

@dataclass
class HaftaDonem:
    donem_id: str
    tarih: _dt.date
    etiket: str
    yil: int


@dataclass
class HaftalikMeta:
    tables: dict[str, str]             # tabloId -> name
    taraf: dict[str, str]
    periods: list[HaftaDonem]          # newest first
    para: list[str]


def _parse_hafta_label(label: str, year: int) -> _dt.date:
    m = re.match(r"\s*([A-Za-zÇĞİÖŞÜçğıöşü]+)\s*/\s*(\d{1,2})", label)
    if not m or m.group(1) not in AY_ADLARI:
        raise BddkError(f"Haftalık dönem etiketi çözülemedi: {label!r}")
    return _dt.date(year, AY_ADLARI[m.group(1)], int(m.group(2)))


def parse_haftalik_page(page: str) -> HaftalikMeta:
    tables = {
        tid: _clean(_html.unescape(name))
        for tid, name in re.findall(
            r"TabloDegistir\('(\d+)'\)[^>]*>\s*<span class=\"text\">(.*?)</span>", page, flags=re.S
        )
    }
    taraf = {
        code: _clean(_html.unescape(name))
        for code, name in re.findall(
            r"TarafDegistir\('(\d+)'\)[^>]*>\s*<span class=\"text\">(.*?)</span>", page, flags=re.S
        )
    }
    periods = []
    for o in _select_options(page, "Donem"):
        ym = re.search(r"Yil-(\d{4})", o["class"])
        if not ym:
            continue
        year = int(ym.group(1))
        periods.append(HaftaDonem(o["value"], _parse_hafta_label(o["text"], year), o["text"], year))
    para = [o["value"] for o in _select_options(page, "ParaBirimi")]
    if not (tables and taraf and periods):
        raise BddkError("Haftalık Bülten sayfasından tablo/taraf/dönem listeleri okunamadı.")
    periods.sort(key=lambda d: d.tarih, reverse=True)
    return HaftalikMeta(tables, taraf, periods, para)


def haftalik_meta(session: Session | None = None) -> HaftalikMeta:
    return _cached(
        "haftalik",
        lambda s: parse_haftalik_page(_check_html(s.get(HAFTALIK_PAGE), "Haftalık Bülten sayfası")),
        session,
    )


def _form_token(page: str, action_fragment: str) -> str:
    for m in re.finditer(r'<form action="([^"]*)"[^>]*>(.*?)</form>', page, flags=re.S):
        if m.group(1).rstrip("/").endswith(action_fragment.rstrip("/")):
            t = re.search(r'name="__RequestVerificationToken" type="hidden" value="([^"]+)"', m.group(2))
            if t:
                return t.group(1)
    t = re.search(r'name="__RequestVerificationToken" type="hidden" value="([^"]+)"', page)
    if not t:
        raise BddkError("BDDK sayfasında güvenlik belirteci (__RequestVerificationToken) bulunamadı.")
    return t.group(1)


def _parse_tr_long_date(text: str) -> _dt.date | None:
    m = re.search(r"(\d{1,2})\s+([A-Za-zÇĞİÖŞÜçğıöşü]+)\s+(\d{4})", text)
    if not m or m.group(2) not in AY_ADLARI:
        return None
    return _dt.date(int(m.group(3)), AY_ADLARI[m.group(2)], int(m.group(1)))


def parse_haftalik_table(page: str) -> dict[str, Any]:
    """Parse the currently shown weekly table (#TabloExcel2 values, #Tablo row ids)."""
    rows = parse_table(page, "TabloExcel2")
    if not rows or len(rows[0]) < 3:
        raise BddkError("Haftalık tablo başlığı okunamadı.")
    head = rows[0]
    title = head[1]["text"]
    m = re.match(r"(.*?)\s*/\s*(.*?)\s*\((.*?)\)\s*\((.*?)\)\s*$", title)
    if not m:
        raise BddkError(f"Haftalık tablo başlığı beklenmeyen biçimde: {title!r}")
    taraf_name, table_name, date_text, unit = (x.strip() for x in m.groups())
    tarih = _parse_tr_long_date(date_text)
    value_cols = [c["text"] for c in head[2:]]

    ids: dict[str, list[str | None]] = {}
    try:
        for r in parse_table(page, "Tablo"):
            if r and not r[0]["th"] and r[0]["text"]:
                ids[r[0]["text"]] = [
                    (re.search(r"ShowModalGraph\('([^']+)'", c["attrs"].get("onclick", "")) or [None, None])[1]
                    for c in r[2:]
                ]
    except BddkError:
        pass

    records = []
    for r in rows[1:]:
        if len(r) < 2 + len(value_cols):
            continue
        sira = r[0]["text"]
        rid = next((i for i in ids.get(sira, []) if i), None)
        rec = {"sira": int(sira) if sira.isdigit() else sira, "kalem": r[1]["text"], "kalem_id": rid}
        for col, cell in zip(value_cols, r[2:]):
            rec[col] = parse_number(cell["text"])
        records.append(rec)
    if not records:
        raise BddkError("Haftalık tabloda veri satırı bulunamadı.")
    return {
        "taraf": taraf_name, "tablo": table_name, "tarih": tarih, "tarih_metni": date_text,
        "birim": unit, "sutunlar": value_cols, "frame": pd.DataFrame(records),
    }


class HaftalikClient:
    def __init__(self, budget: float = TOTAL_BUDGET) -> None:
        self._budget = budget

    @staticmethod
    def _pick_period(meta: HaftalikMeta, donem: str) -> HaftaDonem:
        if not donem:
            return meta.periods[0]
        d = donem.strip()
        for p in meta.periods:
            if p.donem_id == d:
                return p
        try:
            want = _dt.date.fromisoformat(d)
        except ValueError:
            raise BddkError(f"Haftalık dönem 'YYYY-AA-GG' tarihi olmalı (ör. 2026-10-30): {donem!r}") from None
        for p in meta.periods:
            if p.tarih == want:
                return p
        near = sorted(meta.periods, key=lambda p: abs((p.tarih - want).days))[:4]
        raise BddkError(
            f"{want.isoformat()} tarihli haftalık bülten yok. En yakın yayımlanmış haftalar: "
            f"{', '.join(p.tarih.isoformat() for p in near)}. Mevcut aralık: "
            f"{meta.periods[-1].tarih.isoformat()} .. {meta.periods[0].tarih.isoformat()}."
        )

    def get_table(
        self,
        tablo_id: str | int = "",
        donem: str = "",
        taraf_list: list[str] | None = None,
        para_birimi: str = "TL",
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        para = (para_birimi or "TL").strip().upper()
        with Session(self._budget) as s:
            page = _check_html(s.get(HAFTALIK_PAGE), "Haftalık Bülten sayfası")
            meta = parse_haftalik_page(page)
            with _meta_lock:
                _meta_cache["haftalik"] = (time.monotonic(), meta)
            tid = str(tablo_id or next(iter(meta.tables))).strip()
            if tid not in meta.tables:
                raise _bad_choice("tablo_id", tablo_id, [f"{k}={v}" for k, v in meta.tables.items()])
            if para not in meta.para:
                raise _bad_choice("para_birimi", para_birimi, meta.para)
            tarafs = _resolve_taraf(meta.taraf, taraf_list)
            if len(tarafs) > MAX_HAFTALIK_TARAF:
                raise BddkError(f"Tek çağrıda en fazla {MAX_HAFTALIK_TARAF} taraf.")
            period = self._pick_period(meta, donem)

            page = _check_html(
                s.post(HAFTALIK_TABLO, data={"__RequestVerificationToken": _form_token(page, "/BultenHaftalik/"),
                                             "tabloId": tid}), "Haftalık tablo seçimi")
            page = _check_html(
                s.post(HAFTALIK_DONEM, data={"__RequestVerificationToken": _form_token(page, "DonemDegistir"),
                                             "yil": str(period.yil), "donemId": period.donem_id, "para": para}),
                "Haftalık dönem seçimi")
            frames = []
            info_rows = []
            for t in tarafs:
                page = _check_html(
                    s.post(HAFTALIK_TARAF, data={"__RequestVerificationToken": _form_token(page, "TarafSec"),
                                                 "tarafKodu": t}), "Haftalık taraf seçimi")
                parsed = parse_haftalik_table(page)
                self._verify(parsed, meta, tid, t, period, para)
                f = parsed["frame"]
                f.insert(0, "tarih", period.tarih.isoformat())
                f.insert(1, "taraf_kodu", t)
                f.insert(2, "taraf", meta.taraf[t])
                frames.append(f)
                info_rows.append(parsed)
        frame = pd.concat(frames, ignore_index=True)
        info = {
            "tablo_id": tid,
            "tablo": meta.tables[tid],
            "tarih": period.tarih.isoformat(),
            "hafta": period.etiket,
            "birim": info_rows[0]["birim"],
            "para_birimi": para,
            "taraf": {t: meta.taraf[t] for t in tarafs},
            "en_son_hafta": meta.periods[0].tarih.isoformat(),
            "sutunlar": info_rows[0]["sutunlar"],
        }
        return frame, info

    @staticmethod
    def _verify(parsed: dict[str, Any], meta: HaftalikMeta, tid: str, taraf: str, period: HaftaDonem, para: str) -> None:
        problems = []
        if tr_fold(parsed["taraf"]) != tr_fold(meta.taraf[taraf]):
            problems.append(f"taraf {parsed['taraf']!r} != {meta.taraf[taraf]!r}")
        if tr_fold(parsed["tablo"]) != tr_fold(meta.tables[tid]):
            problems.append(f"tablo {parsed['tablo']!r} != {meta.tables[tid]!r}")
        if parsed["tarih"] != period.tarih:
            problems.append(f"tarih {parsed['tarih_metni']!r} != {period.tarih.isoformat()}")
        unit_ccy = "USD" if "USD" in parsed["birim"].upper() else "TL"
        if unit_ccy != para:
            problems.append(f"birim {parsed['birim']!r} != {para}")
        if problems:
            raise BddkError(
                "Haftalık Bülten sunucusu istenenden farklı bir tablo döndürdü (oturum durumu karıştı): "
                + "; ".join(problems) + ". Tekrar deneyin."
            )

    def get_series(
        self,
        kalem_id: str,
        sutun: int | str = "TOPLAM",
        taraf: str = "10001",
        para_birimi: str = "TL",
        baslangic: str = "",
        bitis: str = "",
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        sutun_no, sutun_ad = sutun_index(sutun)
        para = (para_birimi or "TL").strip().upper()
        if para not in ("TL", "USD"):
            raise _bad_choice("para_birimi", para_birimi, ["TL", "USD"])
        if not re.fullmatch(r"\d+(\.\d+)+", (kalem_id or "").strip()):
            raise BddkError(
                f"kalem_id {kalem_id!r} geçersiz. Değerleri bddk_haftalik_bulten çıktısındaki 'kalem_id' "
                "sütunundan alın (ör. '1.0.1' = Toplam Krediler)."
            )
        with Session(self._budget) as s:
            meta = haftalik_meta(s)
            t = _resolve_taraf(meta.taraf, [taraf])[0]
            end = _dt.date.fromisoformat(bitis) if bitis else meta.periods[0].tarih
            start = _dt.date.fromisoformat(baslangic) if baslangic else end - _dt.timedelta(weeks=12)
            if start > end:
                start, end = end, start
            first_avail = meta.periods[-1].tarih
            if end < first_avail:
                raise BddkError(f"Haftalık seri {first_avail.isoformat()} tarihinden başlar.")
            n_windows = (end - max(start, first_avail)).days // 91 + 1
            if n_windows > MAX_HAFTALIK_SERI_WINDOWS:
                raise BddkError(
                    f"İstenen aralık ~{n_windows * 13} hafta; tek çağrıda en fazla "
                    f"{MAX_HAFTALIK_SERI_WINDOWS * 13} hafta (~{MAX_HAFTALIK_SERI_WINDOWS // 4} yıl) alınır."
                )
            points: dict[_dt.date, float] = {}
            baslik = ""
            cursor = end
            while cursor >= start:
                resp = s.post(HAFTALIK_SERI, data={
                    "dil": "tr", "tarih": cursor.strftime("%d.%m.%Y"), "id": kalem_id.strip(),
                    "parabirimi": "TRY" if para == "TL" else "USD", "sutun": str(sutun_no),
                    "tarafKodu": t, "gun": "90",
                })
                if resp.status_code != 200:
                    raise BddkError(f"Haftalık seri isteği HTTP {resp.status_code} döndürdü.")
                body = resp.json()
                xs, ys = body.get("XEkseni") or [], body.get("YEkseni") or []
                if not xs:
                    raise BddkError(
                        f"kalem_id {kalem_id!r} / sütun {sutun} için seri boş döndü (bu kombinasyon yok). "
                        "Geçerli id'ler bddk_haftalik_bulten çıktısındaki 'kalem_id' sütunundadır."
                    )
                baslik = body.get("Baslik") or baslik
                dates = [_dt.datetime.strptime(x, "%d.%m.%Y").date() for x in xs]
                for d, v in zip(dates, ys):
                    points[d] = float(v)
                if dates[0] <= start or len(dates) < 13:
                    break
                cursor = dates[0] - _dt.timedelta(days=1)
            _check_series_title(baslik, para, sutun_ad, meta.taraf[t])
        frame = pd.DataFrame(sorted((d, v) for d, v in points.items() if start <= d <= end), columns=["tarih", "deger"])
        frame["tarih"] = frame["tarih"].map(lambda d: d.isoformat())
        info = {
            "baslik": baslik,
            "birim": f"Milyon {'TL' if para == 'TL' else 'USD'}",
            "taraf": meta.taraf[t],
            "kalem_id": kalem_id,
            "sutun": sutun_ad,
            "baslangic": start.isoformat(),
            "bitis": end.isoformat(),
        }
        return frame, info


# ==========================================================================
# Günlük Bülten (daily; latest day only)
# ==========================================================================

def _gunluk_ids(page: str) -> dict[str, list[str]]:
    """Display tables: row name -> [row ids] (TRY onclicks; same ids are used for USD)."""
    ids: dict[str, list[str]] = {}
    for block in re.findall(r"<tr[^>]*>(.*?)</tr>", page, flags=re.S):
        m = re.search(r"ShowModalGraph\('([^']+)','TRY'", block)
        if not m:
            continue
        first_td = re.search(r"<td[^>]*>(.*?)</td>", block, flags=re.S)
        name = _clean(_html.unescape(re.sub(r"<[^>]+>", " ", first_td.group(1)))) if first_td else ""
        ids.setdefault(tr_fold(name.lstrip("-").strip()), []).append(m.group(1))
    return ids


def parse_gunluk_page(page: str) -> dict[str, Any]:
    """Latest-day Günlük Bülten: values from #excelExportTl/#excelExportUsd (2 decimals)."""
    id_by_name = _gunluk_ids(page)
    out: dict[str, Any] = {}
    for ccy, tid in (("TL", "excelExportTl"), ("USD", "excelExportUsd")):
        rows = parse_table(page, tid)
        if not rows or len(rows[0]) < 2:
            raise BddkError(f"Günlük Bülten {ccy} tablosu boş.")
        title = rows[0][0]["text"]
        m = re.search(r"\((Milyon [A-Z]+), Tarih:\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\)", title)
        if not m:
            raise BddkError(f"Günlük Bülten başlığı okunamadı: {title!r}")
        if (ccy == "TL") != ("TL" in m.group(1)):
            raise BddkError(f"Günlük Bülten {ccy} tablosunun birimi beklenmedik: {m.group(1)}")
        tarih = _dt.date(int(m.group(4)), int(m.group(3)), int(m.group(2)))
        value_cols = [c["text"] for c in rows[0][1:]]
        section, sub, records, used = "", "", [], {}
        for r in rows[1:]:
            texts = [c["text"] for c in r]
            nonempty = [t for t in texts if t]
            if len(nonempty) == 1 and not any(parse_number_or_none(t) is not None for t in nonempty):
                # heading row: upper-case = main section (AKTİFLER, PASİFLER,
                # BİLANÇO DIŞI KALEMLER); otherwise a sub heading (Vadeye Göre)
                if nonempty[0] == _turkish_upper(nonempty[0]):
                    section, sub = nonempty[0], ""
                else:
                    sub = nonempty[0]
                continue
            if len(texts) < 1 + len(value_cols):
                continue
            vals = texts[-len(value_cols):]
            name = next((t for t in texts[: -len(value_cols)] if t), "")
            if not name.startswith("-"):
                sub = ""   # a top-level item ends the previous sub heading
            key = tr_fold(name.lstrip("-").strip())
            cand = id_by_name.get(key, [])
            k = used.get(key, 0)
            used[key] = k + 1
            rec = {"bolum": section, "alt_bolum": sub, "kalem": name,
                   "kalem_id": cand[k] if k < len(cand) else None}
            for c, v in zip(value_cols, vals):
                rec[c] = parse_number(v)
            records.append(rec)
        if not records:
            raise BddkError("Günlük Bülten tablosunda veri satırı bulunamadı.")
        out[ccy] = {"tarih": tarih, "birim": m.group(1), "sutunlar": value_cols,
                    "frame": pd.DataFrame(records)}
    if out["TL"]["tarih"] != out["USD"]["tarih"]:
        raise BddkError("Günlük Bülten TL ve USD tablolarının tarihi uyuşmuyor.")
    return out


class GunlukClient:
    def __init__(self, budget: float = TOTAL_BUDGET) -> None:
        self._budget = budget

    def get_table(self, para_birimi: str = "TL") -> tuple[pd.DataFrame, dict[str, Any]]:
        para = (para_birimi or "TL").strip().upper()
        if para not in ("TL", "USD"):
            raise _bad_choice("para_birimi", para_birimi, ["TL", "USD"])
        with Session(self._budget) as s:
            page = _check_html(s.get(GUNLUK_PAGE), "Günlük Bülten sayfası")
        parsed = parse_gunluk_page(page)[para]
        frame = parsed["frame"]
        frame.insert(0, "tarih", parsed["tarih"].isoformat())
        return frame, {"tarih": parsed["tarih"].isoformat(), "birim": parsed["birim"],
                       "para_birimi": para, "sutunlar": parsed["sutunlar"]}

    def get_series(self, kalem_id: str, sutun: int | str = "TOPLAM", para_birimi: str = "TL") -> tuple[pd.DataFrame, dict[str, Any]]:
        sutun_no, sutun_ad = sutun_index(sutun)
        para = (para_birimi or "TL").strip().upper()
        if para not in ("TL", "USD"):
            raise _bad_choice("para_birimi", para_birimi, ["TL", "USD"])
        if not re.fullmatch(r"\d+(\.\d+)+", (kalem_id or "").strip()):
            raise BddkError(f"kalem_id {kalem_id!r} geçersiz; bddk_gunluk_bulten çıktısındaki 'kalem_id' sütununu kullanın.")
        with Session(self._budget) as s:
            page = _check_html(s.get(GUNLUK_PAGE), "Günlük Bülten sayfası")
            token = _form_token(page, "")
            resp = s.post(GUNLUK_SERI, data={
                "__RequestVerificationToken": token, "id": kalem_id.strip(),
                "parabirimi": "TRY" if para == "TL" else "USD", "form": "", "sutun": str(sutun_no),
            })
        if resp.status_code != 200:
            raise BddkError(f"Günlük seri isteği HTTP {resp.status_code} döndürdü.")
        body = resp.json()
        xs, ys = body.get("XEkseni") or [], body.get("YEkseni") or []
        if not xs:
            raise BddkError(f"kalem_id {kalem_id!r} / sütun {sutun_ad} için günlük seri boş döndü (bu kalem için grafik yok).")
        baslik = body.get("Baslik") or ""
        _check_series_title(baslik, para, sutun_ad, None)
        dates = [_dt.datetime.strptime(x, "%d.%m.%Y").date().isoformat() for x in xs]
        frame = pd.DataFrame({"tarih": dates, "deger": [float(v) for v in ys]})
        return frame, {"baslik": baslik, "birim": f"Milyon {para}", "kalem_id": kalem_id, "sutun": sutun_ad}


# ==========================================================================
# Banka Dışı Mali Kuruluşlar Aylık Bülten (BultenAylikBdmk): leasing,
# factoring, financing and asset-management companies (sector totals)
# ==========================================================================

BDMK_SEKTORLER = {
    # key: (page path, form action, display name) -- all four captured from the
    # live menu/forms on 2026-11-11; the root page is Finansal Kiralama.
    "finansal_kiralama": ("/BultenAylikBdmk/tr/", "FinansalKiralamaBasitRaporGetir", "Finansal Kiralama"),
    "faktoring": ("/BultenAylikBdmk/tr/Gosterim/Faktoring", "FaktoringBasitRaporGetir", "Faktoring"),
    "finansman": ("/BultenAylikBdmk/tr/Gosterim/FinansmanSirketleri", "FinansmanSirketleriBasitRaporGetir", "Finansman Şirketleri"),
    "varlik_yonetim": ("/BultenAylikBdmk/tr/Gosterim/VYS", "VYSBasitRaporGetir", "Varlık Yönetim Şirketleri"),
}


@dataclass
class BdmkMeta:
    sektor: str
    tables: dict[str, str]
    years: list[int]
    latest: tuple[int, int]
    months_latest_year: list[int]

    @property
    def quarterly(self) -> bool:
        return set(self.months_latest_year) <= {3, 6, 9, 12}


def _resolve_bdmk_sektor(sektor: str) -> str:
    key = tr_fold(sektor or "").lower().replace(" ", "_").replace("-", "_")
    aliases = {"leasing": "finansal_kiralama", "kiralama": "finansal_kiralama",
               "finansman_sirketleri": "finansman", "vys": "varlik_yonetim",
               "varlik_yonetim_sirketleri": "varlik_yonetim"}
    key = aliases.get(key, key)
    if key not in BDMK_SEKTORLER:
        raise _bad_choice("sektor", sektor, list(BDMK_SEKTORLER))
    return key


def parse_bdmk_page(page: str, sektor: str) -> BdmkMeta:
    tables = {
        tid: _clean(_html.unescape(name))
        for tid, name in re.findall(
            r"RaporOlusturButonunaBasildi\('(\d+)'\)[^>]*>\s*<span class=\"text\">(.*?)</span>", page, flags=re.S
        )
    }
    yil = _select_options(page, "ddlYil")
    ay = _select_options(page, "ddlAy")
    years = [int(o["value"]) for o in yil]
    sel_y = next((int(o["value"]) for o in yil if o["selected"]), years[0])
    sel_m = next((int(o["value"]) for o in ay if o["selected"]), None)
    if not tables or sel_m is None:
        raise BddkError(f"BDMK ({sektor}) sayfasından tablo/dönem listesi okunamadı.")
    return BdmkMeta(sektor, tables, years, (sel_y, sel_m), [int(o["value"]) for o in ay])


def parse_bdmk_table(page: str) -> dict[str, Any]:
    rows = parse_table(page, "TabloBasitGosterim")
    if not rows:
        raise BddkError("BDMK tablosu boş.")
    head = [c["text"] for c in rows[0]]
    m = re.search(r"Dönem:\s*(\d{4})/(\d{1,2}),\s*Birim:\s*(.+)$", head[1] if len(head) > 1 else "")
    if not m:
        raise BddkError(f"BDMK tablo başlığı okunamadı: {head!r}")
    value_cols = head[2:]
    records = []
    for r in rows[1:]:
        texts = [c["text"] for c in r]
        if len(texts) < 2 + len(value_cols):
            continue
        rec = {"sira": int(texts[0]) if texts[0].isdigit() else texts[0], "kalem": texts[1]}
        for col, v in zip(value_cols, texts[2:]):
            rec[col] = parse_number(v)
        records.append(rec)
    return {"donem": (int(m.group(1)), int(m.group(2))), "birim": _clean(m.group(3)),
            "sutunlar": value_cols, "frame": pd.DataFrame(records)}


class BdmkClient:
    def __init__(self, budget: float = TOTAL_BUDGET) -> None:
        self._budget = budget

    def get_dataframe(self, sektor: str = "finansal_kiralama", tablo_no: str | int = "1",
                      donem: str = "", donem_bitis: str = "") -> tuple[pd.DataFrame, dict[str, Any]]:
        key = _resolve_bdmk_sektor(sektor)
        path, action, name = BDMK_SEKTORLER[key]
        with Session(self._budget) as s:
            page = _check_html(s.get(HOST + path), f"BDMK {name} sayfası")
            meta = parse_bdmk_page(page, key)
            tid = str(tablo_no).strip()
            if tid not in meta.tables:
                raise _bad_choice("tablo_no", tablo_no, [f"{k}={v}" for k, v in meta.tables.items()])
            periods = self._periods(meta, donem, donem_bitis)
            frames, unit, cols = [], "", []
            for y, mth in periods:
                resp = s.post(f"{HOST}/BultenAylikBdmk/tr/Gosterim/{action}", data={
                    "__RequestVerificationToken": _form_token(page, action),
                    "tabloNo": tid, "yil": str(y), "ay": str(mth), "paraBirimi": "",
                })
                page2 = _check_html(resp, f"BDMK {name} tablo isteği")
                parsed = parse_bdmk_table(page2)
                if parsed["donem"] != (y, mth):
                    raise BddkError(f"BDMK yanıtının dönemi {parsed['donem']} istenen {y}-{mth:02d} ile uyuşmuyor.")
                if parsed["frame"].empty:
                    raise BddkError(
                        f"BDMK {name} '{meta.tables[tid]}' tablosu için {y}-{mth:02d} döneminde veri yok "
                        f"(yayımlanmamış ya da bu tablo o dönemde yok). En son yayımlanan dönem: "
                        f"{meta.latest[0]}-{meta.latest[1]:02d}; ilk yıl: {min(meta.years)}."
                    )
                f = parsed["frame"]
                f.insert(0, "donem", f"{y}-{mth:02d}")
                frames.append(f)
                unit, cols = parsed["birim"], parsed["sutunlar"]
                page = page2 if "__RequestVerificationToken" in page2 else page
        frame = pd.concat(frames, ignore_index=True)
        return frame, {
            "sektor": name, "tablo": meta.tables[tid], "birim": unit, "sutunlar": cols,
            "donemler": [f"{y}-{m:02d}" for y, m in periods],
            "en_son_donem": f"{meta.latest[0]}-{meta.latest[1]:02d}",
            "siklik": "çeyreklik" if meta.quarterly else "aylık",
        }

    @staticmethod
    def _periods(meta: BdmkMeta, donem: str, donem_bitis: str) -> list[tuple[int, int]]:
        latest = meta.latest
        if not donem and not donem_bitis:
            return [latest]
        start = _period_key(donem) if donem else (min(meta.years), 1)
        end = _period_key(donem_bitis) if donem_bitis else (start if donem else latest)
        for k in (start, end):
            if not 1 <= k[1] <= 12:
                raise BddkError(f"Ay 1-12 arasında olmalı: {k}")
            if k > latest:
                raise BddkError(f"{k[0]}-{k[1]:02d} henüz yayımlanmadı. En son yayımlanan dönem: {latest[0]}-{latest[1]:02d}.")
            if k[0] < min(meta.years):
                raise BddkError(f"Bu bülten {min(meta.years)} yılından başlar.")
        if start > end:
            start, end = end, start
        out, (y, m) = [], start
        while (y, m) <= end:
            if not meta.quarterly or m in (3, 6, 9, 12):
                out.append((y, m))
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        if not out:
            raise BddkError("Seçilen aralıkta yayımlanan dönem yok (bu bülten çeyreklik: ay 3, 6, 9, 12).")
        if len(out) > MAX_AYLIK_PERIODS:
            raise BddkError(f"İstenen aralık {len(out)} dönem; tek çağrıda en fazla {MAX_AYLIK_PERIODS}. Aralığı bölün.")
        return out


def bdmk_meta(sektor: str) -> BdmkMeta:
    key = _resolve_bdmk_sektor(sektor)
    path, _action, _name = BDMK_SEKTORLER[key]
    return _cached(f"bdmk:{key}", lambda s: parse_bdmk_page(_check_html(s.get(HOST + path), "BDMK sayfası"), key))


# ==========================================================================
# Metadata summary for the listing tool
# ==========================================================================

def metadata(bulten: str, n_periods: int = 12) -> dict[str, Any]:
    b = tr_fold(bulten or "").lower()
    if b in ("finturk", "fintürk", "il", "iller"):
        m = finturk_meta()
        return {
            "bulten": "FinTürk - İllere Göre (çeyreklik, il bazında)",
            "arac": "bddk_get_data",
            "tablolar": {str(k): v for k, v in m.tables.items()},
            "taraf_kodlari": m.taraf,
            "donem_bicimi": "YYYY-M (yalnız çeyrek sonları: 3, 6, 9, 12)",
            "en_son_donem": m.periods[0],
            "ilk_donem": m.periods[-1],
            "tablo_ilk_donem_istisnalari": {str(k): v for k, v in FINTURK_TABLE_START.items()},
            "not": "Kalkınma ve yatırım bankaları mevduat toplamaz: tablo 2'de 10003 satırı gelmez (uyarı olarak bildirilir).",
            "son_donemler": m.periods[:n_periods],
            "n_donem": len(m.periods),
            "sehirler": m.sehir,
        }
    if b in ("aylik",):
        m = aylik_meta()
        return {
            "bulten": "Aylık Bülten (banka grupları, aylık)",
            "arac": "bddk_aylik_bulten",
            "tablolar": {str(k): v for k, v in m.tables.items()},
            "taraf_kodlari": m.taraf,
            "gizli_taraf_kodlari": {k: v + " (arayüz listesinde yok; canlı doğrulandı)" for k, v in m.taraf_gizli.items()},
            "para_birimleri": m.para,
            "para_notu": "11-13 ve 15-17 numaralı tablolar yalnız TL sunuluyor (BDDK hata mesajı döner).",
            "donem_bicimi": "YYYY-MM",
            "en_son_donem": f"{m.latest[0]}-{m.latest[1]:02d}",
            "ilk_donem": f"{min(m.years)}-12 (çoğu tablo; bazı tablolarda daha geç -- BDDK açık hata mesajı verir)",
        }
    if b in ("haftalik",):
        m = haftalik_meta()
        return {
            "bulten": "Haftalık Bülten (banka grupları, haftalık)",
            "arac": "bddk_haftalik_bulten (tablo), bddk_haftalik_seri (tek kalem zaman serisi)",
            "tablolar": m.tables,
            "taraf_kodlari": m.taraf,
            "para_birimleri": m.para,
            "donem_bicimi": "YYYY-AA-GG (haftanın veri tarihi; genelde Cuma)",
            "en_son_hafta": m.periods[0].tarih.isoformat(),
            "ilk_hafta": m.periods[-1].tarih.isoformat(),
            "son_haftalar": [f"{p.tarih.isoformat()} ({p.etiket})" for p in m.periods[:n_periods]],
            "n_hafta": len(m.periods),
        }
    if b in ("gunluk",):
        return {
            "bulten": "Günlük Bülten (sektör toplamı, günlük)",
            "arac": "bddk_gunluk_bulten",
            "not": "BDDK yalnız en son günü yayımlıyor; kalem_id ile son ~7 iş gününün serisi alınabilir.",
            "para_birimleri": ["TL", "USD"],
        }
    if b in ("bdmk", "banka_disi", "banka disi"):
        out = {"bulten": "Banka Dışı Mali Kuruluşlar Aylık Bülteni (sektör toplamları)",
               "arac": "bddk_bdmk_bulten", "donem_bicimi": "YYYY-MM", "sektorler": {}}
        for key in BDMK_SEKTORLER:
            m = bdmk_meta(key)
            out["sektorler"][key] = {
                "ad": BDMK_SEKTORLER[key][2], "tablolar": m.tables,
                "en_son_donem": f"{m.latest[0]}-{m.latest[1]:02d}", "ilk_yil": min(m.years),
                "siklik": "çeyreklik" if m.quarterly else "aylık",
            }
        return out
    raise _bad_choice("bulten", bulten, ["finturk", "aylik", "haftalik", "gunluk", "bdmk"])


# ==========================================================================
# MCP tool bodies (server.py keeps only thin @app.tool wrappers)
# ==========================================================================

def _tool_error(exc: Exception) -> Exception:
    """MCP SDK 2.x hides the text of non-ToolError exceptions; convert ours."""
    try:
        from mcp.server.mcpserver.exceptions import ToolError
    except ImportError:  # MCP SDK 1.x
        from mcp.server.fastmcp.exceptions import ToolError
    return ToolError(str(exc))


def _run(fn, *args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        return fn(*args, **kwargs)
    except (BddkError, ValueError, OSError) as exc:
        raise _tool_error(exc) from exc


def _payload(frame: pd.DataFrame, info: dict[str, Any], output_path: str, max_rows: int,
             kaynak: str) -> dict[str, Any]:
    max_rows = max(0, int(max_rows))
    out: dict[str, Any] = {**info, "kaynak": kaynak, "n_rows": int(frame.shape[0]),
                           "columns": list(frame.columns)}
    out["preview_csv"] = frame.head(max_rows).to_csv(index=False)
    if frame.shape[0] > max_rows:
        out["preview_note"] = (
            f"Önizleme ilk {max_rows} satır; tamamı için output_path verin veya max_rows'u artırın."
        )
    if output_path:
        path = Path(output_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(path, index=False)
        out["written"] = str(path.resolve())
    return out


def tool_finturk(tablo_no: int = 1, donem: str = "", donem_bitis: str = "",
                 taraf_list: list[str] | None = None, sehir_list: list[str] | None = None,
                 output_path: str = "", max_rows: int = 90) -> dict[str, Any]:
    def go():
        frame, info = BddkClient().get_dataframe(
            tablo_no=tablo_no, donem=donem, donem_bitis=donem_bitis,
            taraf_list=taraf_list, sehir_list=sehir_list)
        info["birim_notu"] = (
            "Tutarlar tablo adındaki birimde (çoğunlukla Bin TL); 'Oranlar (%)' tablosu yüzde. "
            "Sütun adlarının Türkçe karşılıkları 'sutun_etiketleri' alanında. "
            "EftKodu = taraf (grup) kodu, Yil/Ay = dönem."
        )
        return _payload(frame, info, output_path, max_rows, FINTURK_PAGE)
    return _run(go)


def tool_aylik(tablo_no: int = 1, donem: str = "", donem_bitis: str = "",
               taraf_list: list[str] | None = None, para_birimi: str = "TL",
               output_path: str = "", max_rows: int = 80) -> dict[str, Any]:
    def go():
        frame, info = AylikClient().get_dataframe(
            tablo_no=tablo_no, donem=donem, donem_bitis=donem_bitis,
            taraf_list=taraf_list, para_birimi=para_birimi)
        return _payload(frame, info, output_path, max_rows, AYLIK_PAGE)
    return _run(go)


def tool_haftalik(tablo_id: str = "", donem: str = "", taraf_list: list[str] | None = None,
                  para_birimi: str = "TL", output_path: str = "", max_rows: int = 60) -> dict[str, Any]:
    def go():
        frame, info = HaftalikClient().get_table(
            tablo_id=tablo_id, donem=donem, taraf_list=taraf_list, para_birimi=para_birimi)
        info["kalem_id_notu"] = "kalem_id değerleriyle bddk_haftalik_seri çağrılarak haftalık zaman serisi alınır."
        return _payload(frame, info, output_path, max_rows, HAFTALIK_PAGE)
    return _run(go)


def tool_haftalik_seri(kalem_id: str, sutun: str = "TOPLAM", taraf: str = "10001",
                       para_birimi: str = "TL", baslangic: str = "", bitis: str = "",
                       output_path: str = "", max_rows: int = 120) -> dict[str, Any]:
    def go():
        frame, info = HaftalikClient().get_series(
            kalem_id=kalem_id, sutun=sutun, taraf=taraf, para_birimi=para_birimi,
            baslangic=baslangic, bitis=bitis)
        return _payload(frame, info, output_path, max_rows, HAFTALIK_PAGE)
    return _run(go)


def tool_gunluk(para_birimi: str = "TL", kalem_id: str = "", sutun: str = "TOPLAM",
                output_path: str = "", max_rows: int = 60) -> dict[str, Any]:
    def go():
        client = GunlukClient()
        if kalem_id:
            frame, info = client.get_series(kalem_id=kalem_id, sutun=sutun, para_birimi=para_birimi)
        else:
            frame, info = client.get_table(para_birimi=para_birimi)
            info["not"] = ("BDDK yalnız en son günü yayımlıyor. Son ~7 iş gününün serisi için "
                           "kalem_id (bu çıktıdaki sütun) ile tekrar çağırın.")
        return _payload(frame, info, output_path, max_rows, GUNLUK_PAGE)
    return _run(go)


def tool_bdmk(sektor: str = "finansal_kiralama", tablo_no: str = "1", donem: str = "",
              donem_bitis: str = "", output_path: str = "", max_rows: int = 80) -> dict[str, Any]:
    def go():
        frame, info = BdmkClient().get_dataframe(sektor=sektor, tablo_no=tablo_no,
                                                 donem=donem, donem_bitis=donem_bitis)
        return _payload(frame, info, output_path, max_rows, f"{HOST}/BultenAylikBdmk/tr/")
    return _run(go)


def tool_metadata(bulten: str = "", n_periods: int = 12) -> dict[str, Any]:
    if not bulten:
        return {
            "bultenler": {
                "finturk": "İl bazında çeyreklik bankacılık verisi -> bddk_get_data",
                "aylik": "Aylık Bülten: banka grupları, 17 tablo (bilanço, kâr-zarar, krediler, mevduat, "
                         "sermaye yeterliliği, rasyolar ...) -> bddk_aylik_bulten",
                "haftalik": "Haftalık Bülten: banka grupları, 9 tablo -> bddk_haftalik_bulten / bddk_haftalik_seri",
                "gunluk": "Günlük Bülten: sektör özet bilançosu, yalnız son gün -> bddk_gunluk_bulten",
                "bdmk": "Banka dışı mali kuruluşlar (finansal kiralama, faktoring, finansman, varlık yönetim) "
                        "-> bddk_bdmk_bulten",
            },
            "not": "Ayrıntı için bulten parametresini verin (ör. bulten='aylik'). Taraf (grup) kodları "
                   "bültenden bültene FARKLIDIR; her bültenin kendi listesini kullanın.",
        }
    return _run(metadata, bulten, n_periods)
