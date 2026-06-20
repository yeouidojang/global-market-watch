"""
글로벌 시장 데이터 수집 (지수 + 매크로)

config/indices.yaml  → 지수 데이터 (LSEG)
config/macro.yaml    → FX·금리·원자재 (LSEG) + 변동성 (yfinance)

사용법:
    python collect_macro.py --session asia     # 아시아 지수 + 전체 매크로
    python collect_macro.py --session europe
    python collect_macro.py --session us
    python collect_macro.py --session all      # 전체
    python collect_macro.py --date 2026-06-09  # 특정일 (기본: 오늘)
"""

import ssl
import httpx

ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

_orig_client = httpx.Client.__init__
_orig_async  = httpx.AsyncClient.__init__

def _fix_proxy(kw):
    kw.setdefault('verify', False)
    # httpx 0.28+: proxies dict → proxy=httpx.Proxy(url)
    if 'proxies' in kw:
        proxies = kw.pop('proxies')
        if isinstance(proxies, dict):
            url = next((v for v in proxies.values() if v), None)
            if url:
                kw.setdefault('proxy', httpx.Proxy(url))
        elif proxies is not None:
            kw.setdefault('proxy', proxies)
    if 'proxy' in kw and isinstance(kw['proxy'], dict):
        url = next((v for v in kw['proxy'].values() if v), None)
        kw['proxy'] = httpx.Proxy(url) if url else None

def _patch_client(self, *a, **kw):
    _fix_proxy(kw)
    _orig_client(self, *a, **kw)

def _patch_async(self, *a, **kw):
    _fix_proxy(kw)
    _orig_async(self, *a, **kw)

httpx.Client.__init__      = _patch_client
httpx.AsyncClient.__init__ = _patch_async

import urllib3
urllib3.disable_warnings()

import os
import sys
import argparse
from datetime import date, timedelta
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

sys.path.insert(0, str(BASE_DIR))

import requests as _requests

import yaml
import pandas as pd
import yfinance as yf
import lseg.data as ld

from db.db_manager import DBManager


def load_yaml(name: str) -> dict:
    path = BASE_DIR / "config" / name
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def lseg_open():
    cfg_path = os.getenv("LSEG_CONFIG_PATH", str(Path.home() / "lseg-data.config.json"))
    ld.open_session(config_name=cfg_path)


def lseg_close():
    try:
        ld.close_session()
    except Exception:
        pass


# ------------------------------------------------------------------ #
#  pykrx — KRX 로그인 + 지수 수집
# ------------------------------------------------------------------ #
_krx_session = None

_KRX_LOGIN_PAGE = "https://data.krx.co.kr/contents/MDC/COMS/client/MDCCOMS001.cmd"
_KRX_LOGIN_JSP  = "https://data.krx.co.kr/contents/MDC/COMS/client/view/login.jsp?site=mdc"
_KRX_LOGIN_URL  = "https://data.krx.co.kr/contents/MDC/COMS/client/MDCCOMS001D1.cmd"
_KRX_UA         = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def _login_krx() -> _requests.Session:
    """KRX data.krx.co.kr 로그인 후 인증 세션 반환."""
    krx_id = os.environ.get("KRX_ID", "")
    krx_pw = os.environ.get("KRX_PW", "")
    if not (krx_id and krx_pw):
        raise RuntimeError("KRX_ID / KRX_PW 환경 변수가 설정되지 않았습니다.")

    s = _requests.Session()
    s.verify = False
    hdrs = {"User-Agent": _KRX_UA}
    s.get(_KRX_LOGIN_PAGE, headers=hdrs, timeout=15)
    s.get(_KRX_LOGIN_JSP, headers={**hdrs, "Referer": _KRX_LOGIN_PAGE}, timeout=15)

    payload = {"mbrNm": "", "telNo": "", "di": "", "certType": "",
               "mbrId": krx_id, "pw": krx_pw}
    login_hdrs = {**hdrs, "Referer": _KRX_LOGIN_PAGE}
    r = s.post(_KRX_LOGIN_URL, data=payload, headers=login_hdrs, timeout=15)
    code = r.json().get("_error_code", "")
    if code == "CD011":          # 중복 로그인 → skipDup
        payload["skipDup"] = "Y"
        r = s.post(_KRX_LOGIN_URL, data=payload, headers=login_hdrs, timeout=15)
        code = r.json().get("_error_code", "")
    if code != "CD001":
        raise RuntimeError(f"KRX 로그인 실패: {code}")
    return s


def _get_krx_session() -> _requests.Session:
    global _krx_session
    if _krx_session is None:
        _krx_session = _login_krx()
    return _krx_session


def fetch_pykrx_close(items: list[dict], target_date: str) -> dict[str, dict]:
    """
    pykrx를 이용해 KRX 지수 OHLCV 수집.
    items: [{"name": "KOSPI", "krx_ticker": "1001"}, ...]
    반환: {name: {date, close, open, high, low, volume}}
    """
    try:
        import pykrx.website.comm.webio as _webio
    except Exception as e:
        print(f"  [pykrx IMPORT ERROR] {e}")
        return {}

    # pykrx webio.Post.read 를 인증 세션으로 교체
    try:
        s = _get_krx_session()
    except Exception as e:
        print(f"  [KRX SESSION ERROR] {e}")
        return {}
    _orig_read = _webio.Post.read

    def _authed_read(self, **params):
        hdrs = {
            "User-Agent": _KRX_UA,
            "Referer": "https://data.krx.co.kr/contents/MDC/MDI/outerLoader/index.cmd",
            "X-Requested-With": "XMLHttpRequest",
        }
        url = self.url.replace("http://", "https://")
        return s.post(url, headers=hdrs, data=params)

    _webio.Post.read = _authed_read

    from pykrx import stock

    start = (pd.Timestamp(target_date) - pd.Timedelta(days=7)).strftime("%Y%m%d")
    end   = pd.Timestamp(target_date).strftime("%Y%m%d")

    result = {}
    try:
        for item in items:
            name   = item["name"]
            ticker = item["krx_ticker"]
            try:
                df = stock.get_index_ohlcv_by_date(start, end, ticker)
                df = df.dropna(how="all")
                if df.empty:
                    continue
                r = df.iloc[-1]
                actual_date = df.index[-1].strftime("%Y-%m-%d")
                # pykrx 컬럼명: 시가 고가 저가 종가 거래량
                result[name] = {
                    "date":   actual_date,
                    "close":  float(r.get("종가", r.iloc[3])),
                    "open":   float(r.get("시가", r.iloc[0])),
                    "high":   float(r.get("고가", r.iloc[1])),
                    "low":    float(r.get("저가", r.iloc[2])),
                    "volume": float(r.get("거래량", r.iloc[4])),
                }
            except Exception as e:
                print(f"  [pykrx ERROR] {name}({ticker}): {e}")
    finally:
        _webio.Post.read = _orig_read  # 패치 복구

    return result


# ------------------------------------------------------------------ #
#  LSEG 단일 날짜 가격 수집
# ------------------------------------------------------------------ #
def _coalesce(row, *fields):
    """첫 번째로 유효한(not NA/None) 값 반환."""
    for f in fields:
        v = row.get(f)
        try:
            if v is not None and not pd.isna(v):
                return v
        except (TypeError, ValueError):
            pass
    return None


def fetch_lseg_close(rics: list[str], target_date: str) -> dict[str, list[dict]]:
    """
    target_date 기준 최근 7일 OHLCV를 LSEG get_history로 수집.
    - close: YLDTOMAT(금리) → TRDPRC_1 → BID 순으로 폴백 (FX·금리·금 지원)
    반환: {ric: [{date, close, open, high, low, volume}, ...]}  ← 다중 날짜
    """
    result = {}
    start = (pd.Timestamp(target_date) - pd.Timedelta(days=7)).strftime("%Y-%m-%d")
    try:
        df = ld.get_history(
            universe=rics,
            fields=["YLDTOMAT", "TRDPRC_1", "OPEN_PRC", "HIGH_1", "LOW_1", "ACVOL_UNS", "BID", "ASK"],
            start=start,
            end=target_date,
        )
    except Exception as e:
        print(f"  [LSEG ERROR] {e}")
        return result

    if df is None or df.empty:
        return result

    def extract_row(r, actual_date):
        return {
            "date":   actual_date,
            "close":  _coalesce(r, "YLDTOMAT", "TRDPRC_1", "BID"),
            "open":   _coalesce(r, "OPEN_PRC", "ASK"),
            "high":   _coalesce(r, "HIGH_1"),
            "low":    _coalesce(r, "LOW_1"),
            "volume": _coalesce(r, "ACVOL_UNS"),
        }

    # MultiIndex 처리
    if isinstance(df.columns, pd.MultiIndex):
        for ric in rics:
            try:
                sub = df.xs(ric, axis=1, level=0).dropna(how="all")
                if not sub.empty:
                    result[ric] = [
                        extract_row(sub.iloc[i], sub.index[i].strftime("%Y-%m-%d"))
                        for i in range(len(sub))
                    ]
            except KeyError:
                pass
    else:
        # 단일 RIC
        sub = df.dropna(how="all")
        if rics and not sub.empty:
            result[rics[0]] = [
                extract_row(sub.iloc[i], sub.index[i].strftime("%Y-%m-%d"))
                for i in range(len(sub))
            ]
    return result


# ------------------------------------------------------------------ #
#  yfinance 수집 (VIX, MOVE 등)
# ------------------------------------------------------------------ #
def fetch_yf_close(tickers: list[str], target_date: str) -> dict[str, list[dict]]:
    """
    target_date 기준 최근 7일 OHLCV를 yfinance로 수집.
    반환: {ticker: [{date, close, open, high, low, volume}, ...]}  ← 다중 날짜
    """
    result = {}
    start = (pd.Timestamp(target_date) - pd.Timedelta(days=7)).strftime("%Y-%m-%d")
    end   = (pd.Timestamp(target_date) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    def _safe_float(val):
        try:
            return None if pd.isna(val) else float(val)
        except (TypeError, ValueError):
            return None

    for ticker in tickers:
        try:
            df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
            if df.empty:
                continue
            # MultiIndex columns(yfinance ≥0.2): flatten
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            rows = []
            for dt, r in df.iterrows():
                rows.append({
                    "date":   dt.strftime("%Y-%m-%d"),
                    "close":  _safe_float(r.get("Close")),
                    "open":   _safe_float(r.get("Open")),
                    "high":   _safe_float(r.get("High")),
                    "low":    _safe_float(r.get("Low")),
                    "volume": _safe_float(r.get("Volume")),
                })
            result[ticker] = rows
        except Exception as e:
            print(f"  [yfinance ERROR] {ticker}: {e}")
    return result


# ------------------------------------------------------------------ #
#  세션별 지수 수집
# ------------------------------------------------------------------ #
def collect_indices(session: str, target_date: str, cfg: dict, db: DBManager) -> int:
    records = []

    if session == "asia":
        regions = [
            ("korea",  cfg["asia"]["korea"]),
            ("japan",  cfg["asia"]["japan"]),
            ("china",  cfg["asia"]["china"]),
        ]
    elif session == "europe":
        regions = [("europe", cfg["europe"].get("indices", cfg["europe"]))]
    elif session == "us":
        regions = [("us", cfg["us"].get("indices", []))]
    else:
        regions = []

    for region_name, items in regions:
        # source별 분리
        pykrx_items = [i for i in items if i.get("source") == "pykrx"]
        yf_items    = [i for i in items if i.get("source") == "yfinance"]
        lseg_items  = [i for i in items if i.get("source") not in ("pykrx", "yfinance")]

        # pykrx 수집
        if pykrx_items:
            print(f"  [{region_name}/pykrx] {[i['name'] for i in pykrx_items]}")
            prices = fetch_pykrx_close(pykrx_items, target_date)
            for name, vals in prices.items():
                rec_date = vals.pop("date", target_date)
                records.append({
                    "date":     rec_date,
                    "session":  session,
                    "category": "index",
                    "name":     name,
                    **vals,
                })

        # yfinance 수집 (다중 날짜)
        if yf_items:
            tickers   = [i["ticker"] for i in yf_items]
            yf_names  = {i["ticker"]: i["name"] for i in yf_items}
            print(f"  [{region_name}/yfinance] {tickers}")
            prices = fetch_yf_close(tickers, target_date)
            for ticker, rows in prices.items():
                for row in rows:
                    records.append({
                        "date":     row.get("date", target_date),
                        "session":  session,
                        "category": "index",
                        "name":     yf_names.get(ticker, ticker),
                        "close":    row.get("close"),
                        "open":     row.get("open"),
                        "high":     row.get("high"),
                        "low":      row.get("low"),
                        "volume":   row.get("volume"),
                    })

        # LSEG 수집 (다중 날짜) — 아직 LSEG ric이 남아있는 항목용
        if lseg_items:
            rics  = [i["ric"] for i in lseg_items]
            names = {i["ric"]: i["name"] for i in lseg_items}
            print(f"  [{region_name}/lseg] {rics}")
            prices = fetch_lseg_close(rics, target_date)
            for ric, rows in prices.items():
                for row in rows:
                    rec_date = row.get("date", target_date)
                    records.append({
                        "date":     rec_date,
                        "session":  session,
                        "category": "index",
                        "name":     names.get(ric, ric),
                        "close":    row.get("close"),
                        "open":     row.get("open"),
                        "high":     row.get("high"),
                        "low":      row.get("low"),
                        "volume":   row.get("volume"),
                    })

    # US 섹터 ETF + 대형주 (yfinance, 다중 날짜)
    if session == "us":
        yf_items = cfg["us"].get("sector_etfs", []) + cfg["us"].get("top_stocks", [])
        if yf_items:
            yf_tickers = [i["ticker"] for i in yf_items]
            yf_names   = {i["ticker"]: i["name"] for i in yf_items}
            print(f"  [us/yfinance] {yf_tickers}")
            prices = fetch_yf_close(yf_tickers, target_date)
            for ticker, rows in prices.items():
                for row in rows:
                    records.append({
                        "date":     row.get("date", target_date),
                        "session":  session,
                        "category": "index",
                        "name":     yf_names.get(ticker, ticker),
                        "close":    row.get("close"),
                        "open":     row.get("open"),
                        "high":     row.get("high"),
                        "low":      row.get("low"),
                        "volume":   row.get("volume"),
                    })

    return db.upsert_market_daily(records)


# ------------------------------------------------------------------ #
#  BoK ECOS API — 한국 금리 수집
# ------------------------------------------------------------------ #
BOK_API_KEY  = os.getenv("ECOS_API_KEY", "sample")
BOK_BASE_URL = "https://ecos.bok.or.kr/api/StatisticSearch"

def fetch_bok_rates(stat_code: str, item_code: str,
                    start_date: str, end_date: str) -> dict[str, float]:
    """BoK ECOS 일별 금리 수집 → {YYYY-MM-DD: yield} 반환."""
    start = start_date.replace("-", "")
    end   = end_date.replace("-", "")
    url   = f"{BOK_BASE_URL}/{BOK_API_KEY}/json/kr/1/100/{stat_code}/D/{start}/{end}/{item_code}"
    try:
        import requests as _req
        resp = _req.get(url, timeout=15, verify=False)
        resp.raise_for_status()
        data = resp.json()
        rows = data.get("StatisticSearch", {}).get("row", [])
        result = {}
        for r in rows:
            raw = r.get("DATA_VALUE", "")
            if raw and raw.strip():
                t = r["TIME"]
                dt = f"{t[:4]}-{t[4:6]}-{t[6:8]}"
                result[dt] = float(raw)
        return result
    except Exception as e:
        print(f"  [BoK ECOS ERROR] {e}")
        return {}


# ------------------------------------------------------------------ #
#  매크로 수집 (FX·금리·원자재·변동성) — 세션 무관, 항상 전체
# ------------------------------------------------------------------ #
def collect_macro(target_date: str, cfg: dict, db: DBManager) -> int:
    records = []

    # LSEG 항목 (fx, rates 중 source=lseg, commodities)
    lseg_items = []
    lseg_meta  = {}
    bok_items  = []   # source=bok 인 rate 항목

    cat_map = {"fx": "fx", "rates": "rate", "commodities": "commodity"}
    for cat in ("fx", "rates", "commodities"):
        for item in cfg.get(cat, []):
            src = item.get("source", "lseg")
            if src == "lseg":
                ric = item["ric"]
                lseg_items.append(ric)
                lseg_meta[ric] = {"name": item["name"], "category": cat_map.get(cat, cat)}
            elif src == "bok" and cat == "rates":
                bok_items.append(item)

    if lseg_items:
        print(f"  [macro LSEG] {lseg_items}")
        prices = fetch_lseg_close(lseg_items, target_date)
        for ric, rows in prices.items():
            meta = lseg_meta[ric]
            for row in rows:
                records.append({
                    "date":     row.get("date", target_date),
                    "session":  "macro",
                    "category": meta["category"],
                    "name":     meta["name"],
                    "close":    row.get("close"),
                    "open":     row.get("open"),
                    "high":     row.get("high"),
                    "low":      row.get("low"),
                    "volume":   row.get("volume"),
                })

    # BoK ECOS 항목 (국고채 금리 등)
    if bok_items:
        start7 = (pd.Timestamp(target_date) - pd.Timedelta(days=9)).strftime("%Y-%m-%d")
        for item in bok_items:
            name = item["name"]
            print(f"  [macro BoK] {name} ({item['stat_code']}/{item['item_code']})")
            yields = fetch_bok_rates(item["stat_code"], item["item_code"], start7, target_date)
            for dt, yld in sorted(yields.items()):
                records.append({
                    "date": dt, "session": "macro", "category": "rate",
                    "name": name, "close": yld,
                    "open": None, "high": None, "low": None, "volume": None,
                })

    # yfinance 항목 (VIX, DXY 등 — volatility 외 fx/rates/commodities도 포함)
    yf_items = []
    yf_meta  = {}
    for item in cfg.get("volatility", []):
        if item.get("source") == "yfinance":
            t = item["ticker"]
            yf_items.append(t)
            yf_meta[t] = {"name": item["name"], "category": "volatility"}
    for cat, db_cat in cat_map.items():
        for item in cfg.get(cat, []):
            if item.get("source") == "yfinance":
                t = item["ticker"]
                yf_items.append(t)
                yf_meta[t] = {"name": item["name"], "category": db_cat}

    if yf_items:
        print(f"  [macro yfinance] {yf_items}")
        prices = fetch_yf_close(yf_items, target_date)
        for ticker, rows in prices.items():
            meta = yf_meta[ticker]
            for row in rows:
                records.append({
                    "date":     row.get("date", target_date),
                    "session":  "macro",
                    "category": meta["category"],
                    "name":     meta["name"],
                    "close":    row.get("close"),
                    "open":     row.get("open"),
                    "high":     row.get("high"),
                    "low":      row.get("low"),
                    "volume":   row.get("volume"),
                })

    # KRX 항목 (VKOSPI 등)
    krx_items = [i for i in cfg.get("volatility", []) if i.get("source") == "krx"]
    for item in krx_items:
        name = item["name"]
        kind = item.get("kind", "vkospi")
        if kind == "vkospi":
            rows = fetch_krx_vkospi(target_date)
        else:
            print(f"  [macro KRX] 알 수 없는 kind={kind} (skip)")
            continue
        if rows:
            print(f"  [macro KRX] {name}: {len(rows)}건")
        for row in rows:
            records.append({
                "date":     row.get("date", target_date),
                "session":  "macro",
                "category": "volatility",
                "name":     name,
                "close":    row.get("close"),
                "open":     row.get("open"),
                "high":     row.get("high"),
                "low":      row.get("low"),
                "volume":   row.get("volume"),
            })

    return db.upsert_market_daily(records)


# ------------------------------------------------------------------ #
#  KRX 정보데이터시스템 직접 호출 (VKOSPI 등 일반 인덱스 외 시계열)
# ------------------------------------------------------------------ #
_KRX_MDC_URL = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"

# KRX MDCSTAT의 V-KOSPI 200 시계열 (idxIndMidclssCd=03, indIdx2=300)
_KRX_VKOSPI_PARAMS = {
    "bld":               "dbms/MDC/STAT/standard/MDCSTAT00301",
    "idxIndMidclssCd":   "03",
    "tboxindIdx_finder_equidx0_0": "V-KOSPI 200",
    "indIdx":            "1",
    "indIdx2":           "300",
    "codeNmindIdx_finder_equidx0_0": "V-KOSPI 200",
    "param1indIdx_finder_equidx0_0": "",
    "share":             "2",
    "money":             "3",
    "csvxls_isNo":       "false",
}


def fetch_krx_vkospi(target_date: str, lookback_days: int = 7) -> list[dict]:
    """
    KRX 정보데이터시스템에서 V-KOSPI 200 OHLC 시계열 수집.

    Returns
    -------
    list[dict]  [{date, close, open, high, low, volume}]
    """
    try:
        s = _get_krx_session()
    except Exception as e:
        print(f"  [KRX VKOSPI SESSION ERROR] {e}")
        return []
    end = pd.Timestamp(target_date).strftime("%Y%m%d")
    start = (pd.Timestamp(target_date) - pd.Timedelta(days=lookback_days)).strftime("%Y%m%d")

    params = dict(_KRX_VKOSPI_PARAMS)
    params["strtDd"] = start
    params["endDd"]  = end

    hdrs = {
        "User-Agent": _KRX_UA,
        "Referer":    "https://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd",
        "X-Requested-With": "XMLHttpRequest",
    }
    try:
        r = s.post(_KRX_MDC_URL, data=params, headers=hdrs, timeout=15)
        r.raise_for_status()
        rows = r.json().get("output", [])
    except Exception as e:
        print(f"  [KRX VKOSPI ERROR] {e}")
        return []

    def _f(v):
        if v in (None, "", "-"):
            return None
        try:
            return float(str(v).replace(",", ""))
        except (TypeError, ValueError):
            return None

    out = []
    for row in rows:
        d = row.get("TRD_DD", "").replace("/", "-")
        if not d:
            continue
        out.append({
            "date":   d,
            "close":  _f(row.get("CLSPRC_IDX")),
            "open":   _f(row.get("OPNPRC_IDX")),
            "high":   _f(row.get("HGPRC_IDX")),
            "low":    _f(row.get("LWPRC_IDX")),
            "volume": None,  # VKOSPI는 거래량 없음
        })
    return out


# ------------------------------------------------------------------ #
#  메인
# ------------------------------------------------------------------ #
#  히스토리 초기화 (1회)
# ------------------------------------------------------------------ #
def initialize_history_batch(days_back: int = 180):
    """
    과거 N일의 지수 + 매크로 데이터를 배치 수집.
    DB에 이미 있는 날짜는 skip하고, 없는 날짜만 수집.
    
    args:
        days_back: 과거 몇 일을 수집할지 (기본: 180일)
    """
    print(f"\n[initialize_history_batch] 과거 {days_back}일 히스토리 수집 시작...")
    
    indices_cfg = load_yaml("indices.yaml")
    macro_cfg   = load_yaml("macro.yaml")
    db = DBManager()
    
    today = date.today()
    start_date = today - timedelta(days=days_back)
    
    # 수집 대상 날짜 생성 (평일만, 거래일 기준)
    dates_to_collect = []
    current = start_date
    while current < today:
        if current.weekday() < 5:  # 월-금
            dates_to_collect.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)
    
    print(f"  수집 대상: {len(dates_to_collect)} 영업일")
    
    lseg_open()
    try:
        total_indices = 0
        total_macro = 0
        skipped_dates = 0
        
        for idx, target_date in enumerate(dates_to_collect, 1):
            # DB에 이미 해당 날짜의 지수 데이터가 있으면 skip
            existing = db.get_by_date(target_date)
            if not existing.empty and len(existing) > 10:  # 충분한 데이터가 있으면 skip
                skipped_dates += 1
                if idx % 20 == 0:
                    print(f"  진행 중... {idx}/{len(dates_to_collect)} (skip: {skipped_dates})")
                continue
            
            # 지수 수집
            for session in ["asia", "europe", "us"]:
                n = collect_indices(session, target_date, indices_cfg, db)
                total_indices += n
            
            # 매크로 수집 (1회/날짜)
            n = collect_macro(target_date, macro_cfg, db)
            total_macro += n
            
            if idx % 20 == 0:
                print(f"  진행 중... {idx}/{len(dates_to_collect)} (indices: {total_indices}, macro: {total_macro})")
        
        print(f"\n[initialize_history_batch] 완료")
        print(f"  총 지수 레코드: {total_indices}")
        print(f"  총 매크로 레코드: {total_macro}")
        print(f"  스킵된 날짜: {skipped_dates}")
        return True
        
    except Exception as e:
        print(f"[initialize_history_batch] 오류: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        lseg_close()


# ------------------------------------------------------------------ #
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", default="all",
                        choices=["asia", "europe", "us", "all"])
    parser.add_argument("--date", default=None,
                        help="수집 기준일 YYYY-MM-DD (기본: 오늘)")
    parser.add_argument("--init-history", action="store_true",
                        help="과거 180일 히스토리 1회 초기화 (시작 시 1회만 실행)")
    parser.add_argument("--days-back", type=int, default=180,
                        help="히스토리 초기화 기간 (일, 기본: 180)")
    args = parser.parse_args()

    # 히스토리 초기화 (--init-history 플래그)
    if args.init_history:
        success = initialize_history_batch(days_back=args.days_back)
        if not success:
            print("[main] 히스토리 초기화 실패")
            sys.exit(1)
        return

    target_date = args.date or date.today().strftime("%Y-%m-%d")
    print(f"[collect_macro] session={args.session}  date={target_date}")

    indices_cfg = load_yaml("indices.yaml")
    macro_cfg   = load_yaml("macro.yaml")
    db = DBManager()

    lseg_open()
    try:
        # 지수
        sessions = ["asia", "europe", "us"] if args.session == "all" else [args.session]
        for s in sessions:
            n = collect_indices(s, target_date, indices_cfg, db)
            print(f"  [{s} indices] upserted {n} rows")

        # 매크로 (세션 무관 1회)
        n = collect_macro(target_date, macro_cfg, db)
        print(f"  [macro] upserted {n} rows")

    finally:
        lseg_close()

    print("[collect_macro] 완료")


if __name__ == "__main__":
    main()
