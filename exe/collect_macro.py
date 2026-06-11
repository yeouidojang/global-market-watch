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
load_dotenv(BASE_DIR.parent / ".env")

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
    ld.open_session(
        config_name=str(BASE_DIR.parent / "lseg-data.config.json")
    )


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
    import pykrx.website.comm.webio as _webio

    # pykrx webio.Post.read 를 인증 세션으로 교체
    s = _get_krx_session()
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
    - close: TRDPRC_1 → BID 순으로 폴백 (FX·금리·금 지원)
    반환: {ric: [{date, close, open, high, low, volume}, ...]}  ← 다중 날짜
    """
    result = {}
    start = (pd.Timestamp(target_date) - pd.Timedelta(days=7)).strftime("%Y-%m-%d")
    try:
        df = ld.get_history(
            universe=rics,
            fields=["TRDPRC_1", "OPEN_PRC", "HIGH_1", "LOW_1", "ACVOL_UNS", "BID", "ASK"],
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
            "close":  _coalesce(r, "TRDPRC_1", "BID"),
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
#  매크로 수집 (FX·금리·원자재·변동성) — 세션 무관, 항상 전체
# ------------------------------------------------------------------ #
def collect_macro(target_date: str, cfg: dict, db: DBManager) -> int:
    records = []

    # LSEG 항목
    lseg_items = []
    lseg_meta  = {}

    for cat in ("fx", "rates", "commodities"):
        for item in cfg.get(cat, []):
            if item.get("source") == "lseg":
                ric = item["ric"]
                lseg_items.append(ric)
                cat_map = {"fx": "fx", "rates": "rate", "commodities": "commodity"}
                lseg_meta[ric] = {"name": item["name"], "category": cat_map.get(cat, cat)}

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

    # yfinance 항목 (VIX 등)
    yf_items = []
    yf_meta  = {}
    for item in cfg.get("volatility", []):
        if item.get("source") == "yfinance":
            t = item["ticker"]
            yf_items.append(t)
            yf_meta[t] = {"name": item["name"], "category": "volatility"}

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

    return db.upsert_market_daily(records)


# ------------------------------------------------------------------ #
#  메인
# ------------------------------------------------------------------ #
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", default="all",
                        choices=["asia", "europe", "us", "all"])
    parser.add_argument("--date", default=None,
                        help="수집 기준일 YYYY-MM-DD (기본: 오늘)")
    args = parser.parse_args()

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
