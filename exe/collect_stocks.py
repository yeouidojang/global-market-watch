"""
KOSPI+KOSDAQ 주요 종목 및 특징주 수집 (pykrx)

주요 종목: 시총순위 + 거래대금순위 합산 스코어 TOP N
특징주:    거래대금 필터 통과 종목 중 |등락률| + 거래량회전율 합산 스코어 TOP N
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR.parent / ".env")
sys.path.insert(0, str(BASE_DIR))

import pandas as pd

from exe.collect_macro import _get_krx_session, _KRX_UA
import pykrx.website.comm.webio as _webio


def _patch_webio():
    """pykrx webio를 KRX 인증 세션으로 교체."""
    s = _get_krx_session()
    orig = _webio.Post.read

    def _authed(self, **params):
        hdrs = {
            "User-Agent": _KRX_UA,
            "Referer": "https://data.krx.co.kr/contents/MDC/MDI/outerLoader/index.cmd",
            "X-Requested-With": "XMLHttpRequest",
        }
        return s.post(self.url.replace("http://", "https://"), headers=hdrs, data=params)

    _webio.Post.read = _authed
    return orig


def _restore_webio(orig):
    _webio.Post.read = orig


def fetch_top_stocks(
    target_date: str,
    min_trade_val: int = 5_000_000_000,   # 50억 원
    n_major: int = 10,
    n_featured: int = 10,
) -> dict:
    """
    KOSPI+KOSDAQ 주요 종목 / 특징주 수집.

    Parameters
    ----------
    target_date  : YYYY-MM-DD
    min_trade_val: 최소 거래대금 필터 (기본 50억)
    n_major      : 주요 종목 수
    n_featured   : 특징주 수

    Returns
    -------
    {
      "major":    [{"ticker", "name", "close", "chg_pct", "trade_val", "mktcap"}, ...],
      "featured": [{"ticker", "name", "close", "chg_pct", "trade_val", "turnover", "signal"}, ...]
    }
    """
    orig = _patch_webio()
    try:
        from pykrx import stock

        date_str = target_date.replace("-", "")

        # ── 1. OHLCV (등락률·거래대금 포함) ──────────────────────────
        dfs = []
        for market in ("KOSPI", "KOSDAQ"):
            df = stock.get_market_ohlcv_by_ticker(date_str, market=market)
            if df.empty:
                continue
            df["market"] = market
            dfs.append(df)
        if not dfs:
            return {"major": [], "featured": []}
        ohlcv = pd.concat(dfs)
        ohlcv.index.name = "ticker"
        ohlcv = ohlcv.reset_index()
        ohlcv.columns = [c if c != "등락률" else "chg_pct" for c in ohlcv.columns]
        ohlcv = ohlcv.rename(columns={
            "종가": "close", "시가": "open", "고가": "high", "저가": "low",
            "거래대금": "trade_val", "거래량": "volume",
        })

        # ── 전 종목 OHLCV → market_daily 저장 ────────────────────────
        try:
            from db.db_manager import DBManager as _DBM
            md_records = [
                {
                    "date": target_date, "session": "asia", "category": "stock",
                    "name": row["ticker"],
                    "close":  row.get("close"),  "open": row.get("open"),
                    "high":   row.get("high"),   "low":  row.get("low"),
                    "volume": row.get("volume"),
                }
                for _, row in ohlcv.iterrows()
                if row.get("close") and row.get("close") > 0
            ]
            n = _DBM().upsert_market_daily(md_records)
            print(f"  [asia stocks] market_daily 저장: {n}건 ({len(md_records)}개 종목)")
        except Exception as _e:
            print(f"  [asia stocks market_daily ERROR] {_e}")

        # ── 시장 폭 계산 ─────────────────────────────────────────────
        valid  = ohlcv[ohlcv["close"] > 0].copy()
        up     = int((valid["chg_pct"] > 0).sum())
        down   = int((valid["chg_pct"] < 0).sum())
        flat   = int((valid["chg_pct"] == 0).sum())
        total  = up + down + flat
        tv_sum = float(valid["trade_val"].sum())
        breadth = {
            "up": up, "down": down, "flat": flat, "total": total,
            "up_pct":       round(up / total * 100, 1) if total > 0 else None,
            "adr":          round(up / down, 2)         if down > 0 else None,
            "weighted_chg": round(
                (valid["chg_pct"] * valid["trade_val"]).sum() / tv_sum, 2
            ) if tv_sum > 0 else None,
        }

        # ── 2. 투자자별 순매수 (외국인·기관, 전 종목) ────────────────
        inv_map = {}   # {ticker: {"foreign_net": int, "inst_net": int}}
        try:
            inv_dfs = []
            for market in ("KOSPI", "KOSDAQ"):
                try:
                    df_inv = stock.get_market_net_purchases_of_equities_by_ticker(
                        date_str, date_str, market
                    )
                    if df_inv is not None and not df_inv.empty and len(df_inv.columns) > 0:
                        df_inv.index.name = "ticker"
                        df_inv = df_inv.reset_index()
                        df_inv["_market"] = market
                        inv_dfs.append(df_inv)
                except Exception as _me:
                    print(f"  [pykrx investor {market} ERROR] {_me}")
            if inv_dfs:
                inv_all = pd.concat(inv_dfs, ignore_index=True)
                # 컬럼명 동적 탐지: 외국인/외국인합계, 기관합계/기관
                cols = inv_all.columns.tolist()
                foreign_col = next((c for c in cols if "외국인" in c), None)
                inst_col    = next((c for c in cols if "기관" in c and "합계" in c), None) \
                           or next((c for c in cols if "기관" in c), None)
                if foreign_col and inst_col:
                    for _, row in inv_all.iterrows():
                        t = row["ticker"]
                        inv_map[t] = {
                            "foreign_net": int(row[foreign_col]) if pd.notna(row[foreign_col]) else None,
                            "inst_net":    int(row[inst_col])    if pd.notna(row[inst_col])    else None,
                        }
                    print(f"  [asia investor] 수급 조회 완료: {len(inv_map)}개 종목 "
                          f"(외인={foreign_col}, 기관={inst_col})")
                else:
                    print(f"  [asia investor] 컬럼 탐지 실패: {cols[:10]}")
        except Exception as _ie:
            print(f"  [asia investor ERROR] {_ie}")

        # ── 시장 전체 수급 집계 ───────────────────────────────────────
        market_flow: dict = {}
        if inv_map:
            fn_list = [v["foreign_net"] for v in inv_map.values() if v.get("foreign_net") is not None]
            it_list = [v["inst_net"]    for v in inv_map.values() if v.get("inst_net")    is not None]
            market_flow = {
                "foreign_net": sum(fn_list) if fn_list else None,
                "inst_net":    sum(it_list) if it_list else None,
                "n_stocks":    len(inv_map),
            }

        # ── 3. 시가총액 + 상장주식수 ─────────────────────────────────
        caps = []
        for market in ("KOSPI", "KOSDAQ"):
            df = stock.get_market_cap_by_ticker(date_str, market=market)
            if df.empty:
                continue
            caps.append(df)
        cap = pd.concat(caps)
        cap.index.name = "ticker"
        cap = cap.reset_index()[["ticker", "시가총액", "상장주식수"]]
        cap = cap.rename(columns={"시가총액": "mktcap", "상장주식수": "shares"})

        # ── 3. 병합 + 필터 ───────────────────────────────────────────
        merged = ohlcv.merge(cap, on="ticker", how="left")
        merged = merged[merged["trade_val"] >= min_trade_val].copy()
        merged = merged[merged["close"] > 0].copy()

        # 종목명 조회 (상위 후보만)
        top_tickers = set(
            merged.nlargest(n_major * 3, "mktcap")["ticker"].tolist()
            + merged.nlargest(n_major * 3, "trade_val")["ticker"].tolist()
            + merged.nlargest(n_featured * 3, "chg_pct")["ticker"].tolist()
            + merged.nsmallest(n_featured * 3, "chg_pct")["ticker"].tolist()
        )
        name_map = {}
        for t in top_tickers:
            try:
                name_map[t] = stock.get_market_ticker_name(t)
            except Exception:
                name_map[t] = t
        merged["name"] = merged["ticker"].map(lambda t: name_map.get(t, t))

        # 거래량 회전율 (클수록 거래량 급증)
        merged["turnover"] = merged.apply(
            lambda r: r["volume"] / r["shares"] * 100 if r["shares"] > 0 else 0, axis=1
        )

        # ── 4. 주요 종목: 시총순위 + 거래대금순위 합산 ───────────────
        merged["rank_cap"]   = merged["mktcap"].rank(ascending=False)
        merged["rank_trade"] = merged["trade_val"].rank(ascending=False)
        merged["major_score"] = merged["rank_cap"] + merged["rank_trade"]
        major_df = merged.nsmallest(n_major, "major_score")

        major = []
        for _, r in major_df.iterrows():
            major.append({
                "ticker":    r["ticker"],
                "name":      r["name"],
                "market":    r["market"],
                "close":     int(r["close"]),
                "chg_pct":   round(float(r["chg_pct"]), 2),
                "trade_val": int(r["trade_val"]),
                "mktcap":    int(r["mktcap"]),
            })

        # ── 5. 특징주: |등락률| + 회전율 합산 스코어 ─────────────────
        merged["abs_chg"]      = merged["chg_pct"].abs()
        merged["rank_abs_chg"] = merged["abs_chg"].rank(ascending=False)
        merged["rank_turn"]    = merged["turnover"].rank(ascending=False)
        merged["feat_score"]   = merged["rank_abs_chg"] + merged["rank_turn"]

        # 등락률 ±3% 이상이거나 회전율 상위 20% 종목만 특징주 후보
        turn_threshold = merged["turnover"].quantile(0.80)
        candidates = merged[
            (merged["abs_chg"] >= 3.0) | (merged["turnover"] >= turn_threshold)
        ]
        feat_df = candidates.nsmallest(n_featured, "feat_score")

        featured = []
        for _, r in feat_df.iterrows():
            signals = []
            if r["chg_pct"] >= 3.0:
                signals.append("급등")
            elif r["chg_pct"] <= -3.0:
                signals.append("급락")
            if r["turnover"] >= turn_threshold:
                signals.append("거래량급증")
            inv = inv_map.get(r["ticker"], {})
            fn, it = inv.get("foreign_net"), inv.get("inst_net")
            if fn is not None and fn > 0:   signals.append("외인매수")
            elif fn is not None and fn < 0: signals.append("외인매도")
            if it is not None and it > 0:   signals.append("기관매수")
            elif it is not None and it < 0: signals.append("기관매도")
            featured.append({
                "ticker":      r["ticker"],
                "name":        r["name"],
                "market":      r["market"],
                "close":       int(r["close"]),
                "chg_pct":     round(float(r["chg_pct"]), 2),
                "trade_val":   int(r["trade_val"]),
                "turnover":    round(float(r["turnover"]), 3),
                "foreign_net": fn,
                "inst_net":    it,
                "signal":      "+".join(signals) if signals else "변동",
            })

        # ── investor_flow: 전 종목 외인/기관 순매수 (분석용) ─────────
        investor_flow = []
        for ticker, inv in inv_map.items():
            row_ohlcv = ohlcv[ohlcv["ticker"] == ticker]
            investor_flow.append({
                "ticker":      ticker,
                "name":        name_map.get(ticker, ticker),
                "market":      row_ohlcv["market"].iloc[0] if not row_ohlcv.empty else None,
                "close":       int(row_ohlcv["close"].iloc[0]) if not row_ohlcv.empty else None,
                "chg_pct":     round(float(row_ohlcv["chg_pct"].iloc[0]), 2) if not row_ohlcv.empty else None,
                "foreign_net": inv.get("foreign_net"),
                "inst_net":    inv.get("inst_net"),
            })

        return {"major": major, "featured": featured, "investor_flow": investor_flow,
                "breadth": breadth, "market_flow": market_flow}

    except Exception as e:
        print(f"  [collect_stocks ERROR] {e}")
        return {"major": [], "featured": []}
    finally:
        _restore_webio(orig)


# ================================================================== #
#  EUROPE — STOXX600 섹터(LSEG) + DAX40·FTSE30·CAC40 종목(yfinance)
# ================================================================== #

def fetch_europe_stocks(target_date: str) -> dict:
    """
    유럽 섹터 성과(LSEG) + DAX40·FTSE30·CAC40 구성종목(yfinance) 수집.

    Returns
    -------
    {
      "sectors":    [{"name", "close", "chg_pct"}, ...],
      "top_stocks": [{"ticker", "name", "index", "close", "chg_pct"}, ...],  # |등락률| 상위 15
      "breadth":    {"up", "down", "flat", "total", "up_pct", "adr", "weighted_chg"},
    }
    """
    import lseg.data as ld
    import yaml

    cfg_path = BASE_DIR / "config" / "indices.yaml"
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    eu_cfg = cfg.get("europe", {})

    today_end  = (pd.Timestamp(target_date) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    hist_start = (pd.Timestamp(target_date) - pd.Timedelta(days=9)).strftime("%Y-%m-%d")

    def _lseg_batch(rics: list[str]) -> dict[str, dict]:
        """LSEG RIC 리스트 → {ric: {close, chg_pct, volume}}."""
        if not rics:
            return {}
        try:
            df = ld.get_history(
                universe=rics,
                fields=["TRDPRC_1", "PCTCHNG", "ACVOL_UNS"],
                start=target_date,
                end=target_date,
            )
        except Exception as e:
            print(f"    [LSEG batch ERROR] {e}")
            return {}
        if df is None or df.empty:
            return {}

        result = {}
        if isinstance(df.columns, pd.MultiIndex):
            for ric in rics:
                try:
                    row = df.xs(ric, axis=1, level=0)
                    if not row.empty:
                        r = row.iloc[-1]
                        result[ric] = {
                            "close":   r.get("TRDPRC_1"),
                            "chg_pct": r.get("PCTCHNG"),
                            "volume":  r.get("ACVOL_UNS"),
                        }
                except KeyError:
                    pass
        else:
            if rics and not df.empty:
                r = df.iloc[-1]
                result[rics[0]] = {
                    "close":   r.get("TRDPRC_1"),
                    "chg_pct": r.get("PCTCHNG"),
                    "volume":  r.get("ACVOL_UNS"),
                }
        return result

    # ── 1. STOXX600 섹터 지수 (LSEG, 변경 없음) ─────────────────────
    sector_rics  = [s["ric"] for s in eu_cfg.get("sectors", [])]
    sector_names = {s["ric"]: s["name"] for s in eu_cfg.get("sectors", [])}
    print(f"    [europe sectors LSEG] {sector_rics}")
    sector_prices = _lseg_batch(sector_rics)

    sectors = []
    for ric, meta_name in sector_names.items():
        v = sector_prices.get(ric, {})
        if v.get("close") is not None:
            sectors.append({
                "ric":     ric,
                "name":    meta_name,
                "close":   round(float(v["close"]), 2),
                "chg_pct": round(float(v["chg_pct"]), 2) if v.get("chg_pct") is not None else None,
            })
    sectors.sort(key=lambda x: (x["chg_pct"] or 0), reverse=True)

    # ── 2. DAX40·FTSE30·CAC40 구성종목 (yfinance) ───────────────────
    stock_universe = eu_cfg.get("stock_universe", [])
    yf_tickers  = [s["ticker"] for s in stock_universe]
    yf_names    = {s["ticker"]: s["name"]  for s in stock_universe}
    yf_index    = {s["ticker"]: s.get("index", "") for s in stock_universe}

    print(f"    [europe stocks yfinance] {len(yf_tickers)}개 종목 다운로드 중...")
    stock_data = _yf_multiday(yf_tickers, hist_start, today_end)
    print(f"    [europe stocks yfinance] 수신: {len(stock_data)}개")

    # 종목별 지표 계산
    stats: dict[str, dict] = {}
    for ticker, df in stock_data.items():
        if len(df) < 2:
            continue
        close = float(df["Close"].iloc[-1])
        prev  = float(df["Close"].iloc[-2])
        if close <= 0 or prev <= 0:
            continue
        vol = float(df["Volume"].iloc[-1]) if "Volume" in df and pd.notna(df["Volume"].iloc[-1]) else None
        stats[ticker] = {
            "name":    yf_names.get(ticker, ticker),
            "index":   yf_index.get(ticker, ""),
            "close":   round(close, 2),
            "chg_pct": round((close - prev) / abs(prev) * 100, 2),
            "volume":  int(vol) if vol else None,
        }

    # 시장 폭 계산
    if stats:
        _up   = sum(1 for s in stats.values() if s["chg_pct"] > 0)
        _down = sum(1 for s in stats.values() if s["chg_pct"] < 0)
        _flat = sum(1 for s in stats.values() if s["chg_pct"] == 0)
        _total = _up + _down + _flat
        eu_breadth = {
            "up": _up, "down": _down, "flat": _flat, "total": _total,
            "up_pct": round(_up / _total * 100, 1) if _total > 0 else None,
            "adr":    round(_up / _down, 2)         if _down > 0 else None,
            "weighted_chg": None,   # 통화 단위 혼재로 가중평균 생략
        }
    else:
        eu_breadth = {}

    # |등락률| 상위 15개 → top_stocks
    top_stocks = [
        {"ticker": t, "name": s["name"], "index": s["index"],
         "close": s["close"], "chg_pct": s["chg_pct"]}
        for t, s in sorted(stats.items(), key=lambda x: abs(x[1]["chg_pct"]), reverse=True)[:15]
    ]

    # ── 3. market_daily 저장 ────────────────────────────────────────
    try:
        from db.db_manager import DBManager as _DBM
        md_records = []
        # 섹터 지수 (LSEG)
        for ric, v in sector_prices.items():
            if v.get("close") is not None:
                md_records.append({
                    "date": target_date, "session": "europe", "category": "sector",
                    "name": ric, "close": v.get("close"), "open": None,
                    "high": None, "low": None, "volume": v.get("volume"),
                })
        # 구성종목 전체 × 전일치 OHLCV (yfinance)
        for ticker, df in stock_data.items():
            for dt_idx, row in df.iterrows():
                date_str = str(dt_idx.date()) if hasattr(dt_idx, "date") else str(dt_idx)[:10]
                md_records.append({
                    "date": date_str, "session": "europe", "category": "stock",
                    "name": ticker,
                    "close":  float(row["Close"])  if pd.notna(row.get("Close"))  else None,
                    "open":   float(row["Open"])   if pd.notna(row.get("Open"))   else None,
                    "high":   float(row["High"])   if pd.notna(row.get("High"))   else None,
                    "low":    float(row["Low"])    if pd.notna(row.get("Low"))    else None,
                    "volume": float(row["Volume"]) if pd.notna(row.get("Volume")) else None,
                })
        n = _DBM().upsert_market_daily(md_records)
        print(f"  [europe stocks] market_daily 저장: {n}건 "
              f"({len(sector_prices)}섹터 + {len(stock_data)}종목×{len(md_records)//max(len(stock_data)+len(sector_prices),1)}일)")
    except Exception as _e:
        print(f"  [europe stocks market_daily ERROR] {_e}")

    return {"sectors": sectors, "top_stocks": top_stocks, "breadth": eu_breadth}


# ================================================================== #
#  US — 섹터 ETF + SPX 스크리닝 (yfinance + LSEG)
#
#  스크리닝 4종:
#    mktcap_top    : 시총 상위 (LSEG TR.CompanyMarketCap)
#    tradeval_top  : 거래대금 상위 (close × volume)
#    turnover_surge: 거래대금 급증 (오늘/5일평균 >= 1.5) + |등락률| >= 2%
#    eps_revision  : EPS 추정치 1W 변화 (LSEG TR.MeanPctChg)
#
#  시총 필터: min_mktcap_b ($B) 이상 종목만 (소형주 제외)
# ================================================================== #

SPX_CSV_PATH = BASE_DIR.parent / "spx_constituents_prices_2026-02-12.csv"
EPS_CACHE_DAYS = 7

# LSEG RIC suffix 제거 + 특수 케이스 처리
_RIC_SPECIALS = {
    "BRKb": "BRK-B", "BRKa": "BRK-A",
    "BFb":  "BF-B",  "BFa":  "BF-A",
    "WAT_z": None,   # 제외
}

def _load_eps_cache() -> dict:
    """DB에서 EPS 캐시 로드. {ticker: {eps_chg_1w, fetched_date}}"""
    try:
        from db.db_manager import DBManager
        return DBManager().get_eps_cache()
    except Exception:
        return {}


def _save_eps_cache(records: list[dict]):
    """DB에 EPS 캐시 저장. records: [{ticker, eps_chg_1w, fetched_date}]"""
    try:
        from db.db_manager import DBManager
        DBManager().upsert_eps_cache(records)
    except Exception as e:
        print(f"    [eps cache save ERROR] {e}")


def _is_eps_cache_fresh(cache: dict, target_date: str) -> bool:
    """캐시가 EPS_CACHE_DAYS 이내인지 확인."""
    if not cache:
        return False
    sample = next(iter(cache.values()), {})
    fetched = sample.get("fetched_date", "")
    if not fetched:
        return False
    try:
        delta = (pd.Timestamp(target_date) - pd.Timestamp(fetched)).days
        return delta < EPS_CACHE_DAYS
    except Exception:
        return False


def _ric_to_yf(ric: str) -> str | None:
    base = ric.rsplit(".", 1)[0] if "." in ric else ric
    if base in _RIC_SPECIALS:
        return _RIC_SPECIALS[base]
    return base


def _load_spx_universe() -> list[str]:
    """SPX 구성종목 yfinance ticker 목록 (CSV 헤더 기반)."""
    if not SPX_CSV_PATH.exists():
        return []
    cols = pd.read_csv(SPX_CSV_PATH, nrows=0).columns.tolist()
    tickers = []
    for c in cols:
        if c.lower() == "date":
            continue
        yf_t = _ric_to_yf(c)
        if yf_t:
            tickers.append(yf_t)
    return tickers


def _yf_multiday(tickers: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
    """yfinance 배치 다운로드 → {ticker: DataFrame(OHLCV)}."""
    import yfinance as yf
    if not tickers:
        return {}
    try:
        raw = yf.download(
            tickers, start=start, end=end,
            progress=False, auto_adjust=True, group_by="ticker",
        )
    except Exception as e:
        print(f"    [yf multiday ERROR] {e}")
        return {}
    if raw is None or raw.empty:
        return {}

    result = {}
    single = len(tickers) == 1
    for t in tickers:
        try:
            df = raw if single else raw[t]
            df = df.dropna(how="all")
            if not df.empty:
                result[t] = df
        except Exception:
            pass
    return result


def _lseg_screener(rics_yf: list[str], fetch_eps: bool = True,
                   target_date: str = None,
                   eps_universe: list[str] = None) -> dict[str, dict]:
    """
    LSEG get_data로 시총 + EPS 추정치 변화 수집.
    - TR.CompanyMarketCap : billion USD 단위 (mktcap_rics 대상)
    - TR.MeanPctChg WP=30d: 30일 EPS 변화율 핵심 (eps_universe 전체)
    - TR.MeanPctChg WP=7d : 7일 EPS 변화율 서브  (eps_universe 전체)

    Parameters
    ----------
    rics_yf     : 시총 조회 대상 (거래대금 상위 100)
    fetch_eps   : False이면 EPS 조회 생략 (캐시 유효 시)
    target_date : Sdate 기준일 (YYYY-MM-DD)
    eps_universe: EPS 조회 대상 (None이면 rics_yf와 동일). 캐시 만료 시 전체 SPX 유니버스 전달
    """
    import lseg.data as ld
    import time

    mktcap_cands = rics_yf[:100]
    eps_cands    = eps_universe if eps_universe is not None else mktcap_cands

    # result는 mktcap + eps 전체 합집합
    all_tickers = list(dict.fromkeys(mktcap_cands + eps_cands))
    result: dict[str, dict] = {t: {"mktcap_b": None, "eps_chg_1m": None, "eps_chg_1w": None}
                               for t in all_tickers}
    session_opened = False

    try:
        try:
            state = ld.session.get_default().open_state.name
        except Exception:
            state = "Closed"

        if state != "Opened":
            ld.open_session(
                config_name=str(BASE_DIR.parent / "lseg-data.config.json")
            )
            session_opened = True

        # ── 시총 배치 수집 (거래대금 상위 100, batch_size=50) ────────────
        mktcap_batch = 50
        for i in range(0, len(mktcap_cands), mktcap_batch):
            batch = mktcap_cands[i:i + mktcap_batch]
            try:
                part = ld.get_data(universe=batch, fields=["TR.CompanyMarketCap"])
                if part is not None and not part.empty:
                    for _, row in part.iterrows():
                        ticker = str(row.get("Instrument", "")).strip()
                        mktcap = row.get("Company Market Cap")
                        if ticker and pd.notna(mktcap) and mktcap:
                            result.setdefault(ticker, {})["mktcap_b"] = round(float(mktcap), 2)
            except Exception as be:
                print(f"    [LSEG mktcap BATCH ERROR i={i}] {be}")

        # ── EPS 추정치 배치 수집 (전체 eps_universe, batch_size=20, retry×2) ─
        if fetch_eps:
            print(f"    [LSEG eps] {len(eps_cands)}개 종목 1M+1W EPS 조회 중...")
            eps_params_list = [
                ("1m", {"EstimateMeasure": "EPS", "Period": "NTM", "WP": "30d",
                        **({"Sdate": target_date} if target_date else {})}),
                ("1w", {"EstimateMeasure": "EPS", "Period": "NTM", "WP": "7d",
                        **({"Sdate": target_date} if target_date else {})}),
            ]
            eps_batch = 20
            for label, params in eps_params_list:
                field_key = f"eps_chg_{label}"
                for i in range(0, len(eps_cands), eps_batch):
                    batch = eps_cands[i:i + eps_batch]
                    for attempt in range(2):
                        try:
                            part = ld.get_data(
                                universe=batch,
                                fields="TR.MeanPctChg",
                                parameters=params,
                            )
                            if part is not None and not part.empty:
                                for _, row in part.iterrows():
                                    ticker = str(row.get("Instrument", "")).strip()
                                    val = row.get("Mean Estimate Pct Change")
                                    if ticker and pd.notna(val) and val is not None:
                                        result.setdefault(ticker, {})[field_key] = round(float(val), 2)
                            break
                        except Exception as be:
                            print(f"    [LSEG eps_{label} BATCH ERROR i={i} attempt={attempt+1}] {be}")
                            if attempt == 0:
                                time.sleep(3)

    except Exception as e:
        print(f"    [LSEG screener ERROR] {e}")
    finally:
        if session_opened:
            try:
                ld.close_session()
            except Exception:
                pass
    return result


def fetch_us_stocks(
    target_date:   str,
    min_mktcap_b:  float = 5.0,
    surge_thresh:  float = 1.5,
    n_top:         int   = 15,
) -> dict:
    """
    미국 특징주 멀티-시그널 스크리닝

    Returns
    -------
    {
      "sectors":       [섹터ETF 성과],
      "mktcap_top":    [시총 상위],
      "tradeval_top":  [거래대금 상위],
      "turnover_surge":[거래대금 급증 + 급등락],
      "eps_revision":  [EPS 추정치 변화],
    }
    """
    import yfinance as yf
    import yaml

    cfg_path = BASE_DIR / "config" / "indices.yaml"
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    us_cfg = cfg.get("us", {})

    today_end = (pd.Timestamp(target_date) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    hist_start = (pd.Timestamp(target_date) - pd.Timedelta(days=9)).strftime("%Y-%m-%d")  # 9일 = 약 5거래일 확보

    # ── 1. 섹터 ETF ─────────────────────────────────────────────────
    etf_tickers = [s["ticker"] for s in us_cfg.get("sector_etfs", [])]
    etf_names   = {s["ticker"]: s["name"] for s in us_cfg.get("sector_etfs", [])}
    print(f"    [us sector ETFs] {etf_tickers}")

    etf_data = _yf_multiday(etf_tickers, hist_start, today_end)
    sectors = []
    for ticker, meta_name in etf_names.items():
        df = etf_data.get(ticker)
        if df is None or len(df) < 1:
            continue
        close = float(df["Close"].iloc[-1])
        chg_pct = None
        if len(df) >= 2:
            prev = float(df["Close"].iloc[-2])
            chg_pct = round((close - prev) / abs(prev) * 100, 2) if prev else None
        sectors.append({"ticker": ticker, "name": meta_name,
                        "close": round(close, 2), "chg_pct": chg_pct})
    sectors.sort(key=lambda x: (x["chg_pct"] or 0), reverse=True)

    # ── 2. SPX 유니버스 로드 + 5일 데이터 다운로드 ──────────────────
    universe = _load_spx_universe()
    if not universe:
        universe = [s["ticker"] for s in us_cfg.get("top_stocks", [])]
        print(f"    [us universe] SPX CSV 없음 → config 종목 {len(universe)}개 사용")
    else:
        print(f"    [us universe] SPX CSV 로드: {len(universe)}개 종목")

    print(f"    [us 5d download] {len(universe)}개 × 5일 ...")
    stock_data = _yf_multiday(universe, hist_start, today_end)
    print(f"    [us 5d download] 수신: {len(stock_data)}개")

    # ── 전 종목 × 전일치 OHLCV → market_daily 저장 ──────────────────
    try:
        from db.db_manager import DBManager as _DBM
        md_records = []
        for ticker, df in stock_data.items():
            for dt_idx, row in df.iterrows():
                date_str = str(dt_idx.date()) if hasattr(dt_idx, "date") else str(dt_idx)[:10]
                md_records.append({
                    "date": date_str, "session": "us", "category": "stock",
                    "name": ticker,
                    "close":  float(row["Close"])  if pd.notna(row.get("Close"))  else None,
                    "open":   float(row["Open"])   if pd.notna(row.get("Open"))   else None,
                    "high":   float(row["High"])   if pd.notna(row.get("High"))   else None,
                    "low":    float(row["Low"])    if pd.notna(row.get("Low"))    else None,
                    "volume": float(row["Volume"]) if pd.notna(row.get("Volume")) else None,
                })
        n = _DBM().upsert_market_daily(md_records)
        print(f"    [us stocks] market_daily 저장: {n}건 ({len(stock_data)}개 종목 × {len(md_records)//max(len(stock_data),1)}일)")
    except Exception as _e:
        print(f"    [us stocks market_daily ERROR] {_e}")

    # ── 3. 종목별 지표 계산 ──────────────────────────────────────────
    stats = {}
    for ticker, df in stock_data.items():
        if len(df) < 2:
            continue
        close  = float(df["Close"].iloc[-1])
        volume = float(df["Volume"].iloc[-1])
        prev   = float(df["Close"].iloc[-2])
        if close <= 0 or prev <= 0:
            continue

        chg_pct    = round((close - prev) / abs(prev) * 100, 2)
        dollar_vol = close * volume

        # 4~5일 평균 거래대금 (오늘 제외)
        hist_dvol  = (df["Close"] * df["Volume"]).iloc[:-1]
        avg_dvol   = float(hist_dvol.mean()) if len(hist_dvol) > 0 else 0
        surge_ratio = round(dollar_vol / avg_dvol, 2) if avg_dvol > 0 else 0

        stats[ticker] = {
            "close":       round(close, 2),
            "chg_pct":     chg_pct,
            "volume":      int(volume),
            "dollar_vol":  dollar_vol,
            "surge_ratio": surge_ratio,
        }

    # ── 시장 폭 계산 (SPX) ───────────────────────────────────────────
    if stats:
        _up   = sum(1 for s in stats.values() if s["chg_pct"] > 0)
        _down = sum(1 for s in stats.values() if s["chg_pct"] < 0)
        _flat = sum(1 for s in stats.values() if s["chg_pct"] == 0)
        _total = _up + _down + _flat
        _dv_sum = sum(s["dollar_vol"] for s in stats.values())
        us_breadth = {
            "up": _up, "down": _down, "flat": _flat, "total": _total,
            "up_pct":       round(_up / _total * 100, 1) if _total > 0 else None,
            "adr":          round(_up / _down, 2)         if _down > 0 else None,
            "weighted_chg": round(
                sum(s["chg_pct"] * s["dollar_vol"] for s in stats.values()) / _dv_sum, 2
            ) if _dv_sum > 0 else None,
        }
    else:
        us_breadth = {}

    if not stats:
        return {"sectors": sectors, "mktcap_top": [], "tradeval_top": [],
                "turnover_surge": [], "eps_revision": [], "breadth": {}}

    # ── 4. 시총 필터를 위한 LSEG 조회 (상위 후보 100개) ─────────────
    # 거래대금 상위 100개만 LSEG 조회 (전체 조회는 부하 과다)
    cands_by_dvol = sorted(stats.keys(), key=lambda t: stats[t]["dollar_vol"], reverse=True)[:100]

    # EPS 주간 캐시 확인: 7일 이내 캐시가 있으면 LSEG EPS 호출 생략
    eps_cache = _load_eps_cache()
    cache_fresh = _is_eps_cache_fresh(eps_cache, target_date)
    if cache_fresh:
        fetched_date = next(iter(eps_cache.values()), {}).get("fetched_date", "?")
        print(f"    [eps cache] 캐시 사용 (수집일: {fetched_date}, {len(eps_cache)}개 종목)")
    else:
        print(f"    [eps cache] 만료/없음 → LSEG EPS 재수집")

    # 캐시 만료 시 EPS는 전체 SPX 유니버스로 확장, 시총은 top 100 유지
    eps_univ = universe if not cache_fresh else None
    print(f"    [us LSEG screener] 시총 상위{len(cands_by_dvol)}개 / "
          f"EPS {'전체 ' + str(len(eps_univ)) + '개' if eps_univ else '캐시'} 조회 중...")
    lseg_meta = _lseg_screener(cands_by_dvol, fetch_eps=not cache_fresh,
                               target_date=target_date, eps_universe=eps_univ)

    # 캐시에서 EPS 데이터 보완
    if cache_fresh:
        for ticker in cands_by_dvol:
            if ticker in eps_cache:
                lseg_meta.setdefault(ticker, {})["eps_chg_1m"] = eps_cache[ticker].get("eps_chg_1m")
                lseg_meta.setdefault(ticker, {})["eps_chg_1w"] = eps_cache[ticker].get("eps_chg_1w")
    else:
        # 새로 수집된 EPS 전체를 DB 캐시에 저장
        new_records = [
            {"ticker": t, "eps_chg_1m": meta.get("eps_chg_1m"),
             "eps_chg_1w": meta.get("eps_chg_1w"), "fetched_date": target_date}
            for t, meta in lseg_meta.items()
            if meta.get("eps_chg_1m") is not None or meta.get("eps_chg_1w") is not None
        ]
        if new_records:
            _save_eps_cache(new_records)
            print(f"    [eps cache] {len(new_records)}개 종목 DB 저장 (전체 유니버스)")

    def _entry(ticker: str, signal: str = "") -> dict:
        s = stats[ticker]
        m = lseg_meta.get(ticker, {})
        return {
            "ticker":       ticker,
            "close":        s["close"],
            "chg_pct":      s["chg_pct"],
            "dollar_vol_b": round(s["dollar_vol"] / 1e9, 2),
            "surge_ratio":  s["surge_ratio"],
            "mktcap_b":     m.get("mktcap_b"),
            "eps_chg_1m":   m.get("eps_chg_1m"),   # 핵심
            "eps_chg_1w":   m.get("eps_chg_1w"),   # 서브
            "signal":       signal,
        }

    def _passes_mktcap(ticker: str) -> bool:
        m = lseg_meta.get(ticker, {})
        cap = m.get("mktcap_b")
        return cap is None or cap >= min_mktcap_b  # 시총 미조회 시 통과

    # ── 5. 시총 상위 ─────────────────────────────────────────────────
    mktcap_sorted = sorted(
        [t for t in cands_by_dvol if lseg_meta.get(t, {}).get("mktcap_b")],
        key=lambda t: lseg_meta[t]["mktcap_b"], reverse=True
    )
    mktcap_top = [_entry(t, "시총상위") for t in mktcap_sorted[:n_top]]

    # ── 6. 거래대금 상위 ─────────────────────────────────────────────
    dvol_sorted = sorted(
        [t for t in stats if _passes_mktcap(t)],
        key=lambda t: stats[t]["dollar_vol"], reverse=True
    )
    tradeval_top = [_entry(t, "거래대금상위") for t in dvol_sorted[:n_top]]

    # ── 7. 거래대금 급증 + 급등락 (turnover_surge) ───────────────────
    surge_cands = [
        t for t in stats
        if stats[t]["surge_ratio"] >= surge_thresh
        and abs(stats[t]["chg_pct"]) >= 2.0
        and _passes_mktcap(t)
    ]
    surge_cands.sort(key=lambda t: stats[t]["surge_ratio"], reverse=True)

    turnover_surge = []
    for t in surge_cands[:n_top]:
        s = stats[t]
        signals = []
        if s["chg_pct"] >= 3:   signals.append("급등")
        elif s["chg_pct"] <= -3: signals.append("급락")
        signals.append(f"거래대금{s['surge_ratio']:.1f}x")
        e = _entry(t, "+".join(signals))
        turnover_surge.append(e)

    # ── 8. EPS 추정치 변화 — 1M 핵심, 1W 서브 ───────────────────────
    # 당일 가격 데이터가 있는 종목 중 EPS 변화 있는 전체 후보
    eps_cands = [
        t for t in lseg_meta
        if lseg_meta[t].get("eps_chg_1m") is not None
        and t in stats          # 당일 가격 데이터 있어야 함
        and _passes_mktcap(t)
    ]
    eps_up   = sorted([t for t in eps_cands if lseg_meta[t]["eps_chg_1m"] > 0],
                      key=lambda t: lseg_meta[t]["eps_chg_1m"], reverse=True)
    eps_down = sorted([t for t in eps_cands if lseg_meta[t]["eps_chg_1m"] < 0],
                      key=lambda t: lseg_meta[t]["eps_chg_1m"])

    eps_revision = []
    for t in (eps_up[:5] + eps_down[:5]):
        chg_1m = lseg_meta[t]["eps_chg_1m"]
        e = _entry(t, f"EPS{'상향' if chg_1m > 0 else '하향'}1M{chg_1m:+.1f}%")
        # 7일 누적 수익률
        df_t = stock_data.get(t)
        if df_t is not None and len(df_t) >= 2:
            close_today = float(df_t["Close"].iloc[-1])
            close_start = float(df_t["Close"].iloc[0])
            e["return_7d"] = round((close_today - close_start) / abs(close_start) * 100, 2) if close_start else None
        else:
            e["return_7d"] = None
        eps_revision.append(e)

    print(f"    [us screening] 시총상위 {len(mktcap_top)} | 거래대금상위 {len(tradeval_top)} | "
          f"급증 {len(turnover_surge)} | EPS {len(eps_revision)}")

    return {
        "sectors":        sectors,
        "mktcap_top":     mktcap_top,
        "tradeval_top":   tradeval_top,
        "turnover_surge": turnover_surge,
        "eps_revision":   eps_revision,
        "breadth":        us_breadth,
    }


