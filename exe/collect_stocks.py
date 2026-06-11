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
    KOSPI/KOSDAQ 주요 종목 / 특징주 수집 (시장별 분리 분석).

    Parameters
    ----------
    target_date  : YYYY-MM-DD
    min_trade_val: 최소 거래대금 필터 (기본 50억)
    n_major      : **시장별** 주요 종목 수 (KOSPI N + KOSDAQ N)
    n_featured   : **시장별** 특징주 수 (KOSPI N + KOSDAQ N)

    Returns
    -------
    {
      # 시장별 분리 키 (LLM 렌더링용)
      "major_kospi": [...], "major_kosdaq": [...],
      "featured_kospi": [...], "featured_kosdaq": [...],
      "breadth_kospi": {...}, "breadth_kosdaq": {...},
      "market_flow_kospi": {...}, "market_flow_kosdaq": {...},

      # 통합 키 (DB 저장·기존 호환)
      "major": [...], "featured": [...], "investor_flow": [...],
      "breadth": {...}, "market_flow": {...},
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

        # ── 시장 폭 헬퍼 ─────────────────────────────────────────────
        def _compute_breadth(df_market: pd.DataFrame) -> dict:
            v = df_market[df_market["close"] > 0]
            up    = int((v["chg_pct"] > 0).sum())
            down  = int((v["chg_pct"] < 0).sum())
            flat  = int((v["chg_pct"] == 0).sum())
            total = up + down + flat
            tv    = float(v["trade_val"].sum())
            return {
                "up": up, "down": down, "flat": flat, "total": total,
                "up_pct":       round(up / total * 100, 1) if total > 0 else None,
                "adr":          round(up / down, 2)         if down > 0 else None,
                "weighted_chg": round((v["chg_pct"] * v["trade_val"]).sum() / tv, 2)
                                if tv > 0 else None,
            }

        valid = ohlcv[ohlcv["close"] > 0].copy()
        breadth        = _compute_breadth(valid)
        breadth_kospi  = _compute_breadth(valid[valid["market"] == "KOSPI"])
        breadth_kosdaq = _compute_breadth(valid[valid["market"] == "KOSDAQ"])

        # ── 2. 투자자별 순매수 (외국인·기관, 전 종목) ────────────────
        inv_map = {}      # 전체: {ticker: {"foreign_net": int, "inst_net": int}}
        inv_by_market = {"KOSPI": {}, "KOSDAQ": {}}
        try:
            def _fetch_inv(market: str, investor: str) -> dict[str, int]:
                """ticker → 순매수거래대금 dict."""
                try:
                    df = stock.get_market_net_purchases_of_equities_by_ticker(
                        date_str, date_str, market, investor
                    )
                except Exception as _me:
                    print(f"  [pykrx investor {market}/{investor} ERROR] {_me}")
                    return {}
                if df is None or df.empty:
                    return {}
                cols = df.columns.tolist()
                net_col = next((c for c in cols if "순매수거래대금" in c), None) \
                       or next((c for c in cols if "순매수" in c and "대금" in c), None)
                if not net_col:
                    print(f"  [pykrx investor {market}/{investor}] net 컬럼 없음: {cols[:8]}")
                    return {}
                out = {}
                for ticker, row in df.iterrows():
                    v = row[net_col]
                    if pd.notna(v):
                        out[str(ticker)] = int(v)
                return out

            for market in ("KOSPI", "KOSDAQ"):
                f_map = _fetch_inv(market, "외국인")
                i_map = _fetch_inv(market, "기관합계")
                tickers = set(f_map.keys()) | set(i_map.keys())
                for t in tickers:
                    rec = {
                        "foreign_net": f_map.get(t),
                        "inst_net":    i_map.get(t),
                    }
                    inv_map[t] = rec
                    inv_by_market[market][t] = rec
            if inv_map:
                n_f = sum(1 for v in inv_map.values() if v["foreign_net"] is not None)
                n_i = sum(1 for v in inv_map.values() if v["inst_net"]    is not None)
                print(f"  [asia investor] 수급 조회 완료: {len(inv_map)}개 종목 "
                      f"(외인 {n_f}개, 기관 {n_i}개)  "
                      f"KOSPI {len(inv_by_market['KOSPI'])} / KOSDAQ {len(inv_by_market['KOSDAQ'])}")
        except Exception as _ie:
            print(f"  [asia investor ERROR] {_ie}")

        # ── 시장 전체 수급 집계 ───────────────────────────────────────
        def _flow_sum(mp: dict) -> dict:
            if not mp:
                return {}
            fn = [v["foreign_net"] for v in mp.values() if v.get("foreign_net") is not None]
            it = [v["inst_net"]    for v in mp.values() if v.get("inst_net")    is not None]
            return {
                "foreign_net": sum(fn) if fn else None,
                "inst_net":    sum(it) if it else None,
                "n_stocks":    len(mp),
            }
        market_flow        = _flow_sum(inv_map)
        market_flow_kospi  = _flow_sum(inv_by_market["KOSPI"])
        market_flow_kosdaq = _flow_sum(inv_by_market["KOSDAQ"])

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

        # ── 4. 병합 + 필터 ───────────────────────────────────────────
        merged = ohlcv.merge(cap, on="ticker", how="left")
        merged = merged[merged["trade_val"] >= min_trade_val].copy()
        merged = merged[merged["close"] > 0].copy()

        # ── 5. 시장별 분석 함수 ──────────────────────────────────────
        def _analyze_market(m_df: pd.DataFrame, market_name: str) -> tuple[list, list]:
            """단일 시장에 대해 주요 종목 + 특징주 산출."""
            if m_df.empty:
                return [], []

            # 종목명 조회 (상위 후보만)
            top_tickers = set(
                m_df.nlargest(n_major * 3, "mktcap")["ticker"].tolist()
                + m_df.nlargest(n_major * 3, "trade_val")["ticker"].tolist()
                + m_df.nlargest(n_featured * 3, "chg_pct")["ticker"].tolist()
                + m_df.nsmallest(n_featured * 3, "chg_pct")["ticker"].tolist()
            )
            name_map_local = {}
            for t in top_tickers:
                try:
                    name_map_local[t] = stock.get_market_ticker_name(t)
                except Exception:
                    name_map_local[t] = t
            m_df = m_df.copy()
            m_df["name"] = m_df["ticker"].map(lambda t: name_map_local.get(t, t))
            m_df["turnover"] = m_df.apply(
                lambda r: r["volume"] / r["shares"] * 100 if r["shares"] > 0 else 0, axis=1
            )

            # 주요 종목: 시총순위 + 거래대금순위 합산
            m_df["rank_cap"]    = m_df["mktcap"].rank(ascending=False)
            m_df["rank_trade"]  = m_df["trade_val"].rank(ascending=False)
            m_df["major_score"] = m_df["rank_cap"] + m_df["rank_trade"]
            major_df = m_df.nsmallest(n_major, "major_score")

            major_list = []
            for _, r in major_df.iterrows():
                major_list.append({
                    "ticker":    r["ticker"],
                    "name":      r["name"],
                    "market":    market_name,
                    "close":     int(r["close"]),
                    "chg_pct":   round(float(r["chg_pct"]), 2),
                    "trade_val": int(r["trade_val"]),
                    "mktcap":    int(r["mktcap"]),
                })

            # 특징주: |등락률| + 회전율 합산
            m_df["abs_chg"]      = m_df["chg_pct"].abs()
            m_df["rank_abs_chg"] = m_df["abs_chg"].rank(ascending=False)
            m_df["rank_turn"]    = m_df["turnover"].rank(ascending=False)
            m_df["feat_score"]   = m_df["rank_abs_chg"] + m_df["rank_turn"]

            turn_threshold = m_df["turnover"].quantile(0.80)
            candidates = m_df[
                (m_df["abs_chg"] >= 3.0) | (m_df["turnover"] >= turn_threshold)
            ]
            feat_df = candidates.nsmallest(n_featured, "feat_score")

            featured_list = []
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
                if   fn is not None and fn > 0: signals.append("외인매수")
                elif fn is not None and fn < 0: signals.append("외인매도")
                if   it is not None and it > 0: signals.append("기관매수")
                elif it is not None and it < 0: signals.append("기관매도")
                featured_list.append({
                    "ticker":      r["ticker"],
                    "name":        r["name"],
                    "market":      market_name,
                    "close":       int(r["close"]),
                    "chg_pct":     round(float(r["chg_pct"]), 2),
                    "trade_val":   int(r["trade_val"]),
                    "turnover":    round(float(r["turnover"]), 3),
                    "foreign_net": fn,
                    "inst_net":    it,
                    "signal":      "+".join(signals) if signals else "변동",
                })
            return major_list, featured_list

        major_kospi,    featured_kospi    = _analyze_market(
            merged[merged["market"] == "KOSPI"],  "KOSPI")
        major_kosdaq,   featured_kosdaq   = _analyze_market(
            merged[merged["market"] == "KOSDAQ"], "KOSDAQ")

        # 통합 키 (DB 저장·기존 호환)
        major    = major_kospi    + major_kosdaq
        featured = featured_kospi + featured_kosdaq

        # ── investor_flow: 전 종목 외인/기관 순매수 (분석용) ─────────
        # 한 번에 ticker별 OHLCV 조회를 위한 인덱스
        ohlcv_idx = ohlcv.set_index("ticker")
        investor_flow = []
        for ticker, inv in inv_map.items():
            row = ohlcv_idx.loc[ticker] if ticker in ohlcv_idx.index else None
            if row is None:
                continue
            # 동일 ticker 중복 인덱스 방지
            if isinstance(row, pd.DataFrame):
                row = row.iloc[0]
            investor_flow.append({
                "ticker":      ticker,
                "name":        row.get("name") if "name" in row else ticker,
                "market":      row.get("market"),
                "close":       int(row["close"]) if pd.notna(row.get("close")) else None,
                "chg_pct":     round(float(row["chg_pct"]), 2) if pd.notna(row.get("chg_pct")) else None,
                "foreign_net": inv.get("foreign_net"),
                "inst_net":    inv.get("inst_net"),
            })

        return {
            # 시장별 분리
            "major_kospi":        major_kospi,
            "major_kosdaq":       major_kosdaq,
            "featured_kospi":     featured_kospi,
            "featured_kosdaq":    featured_kosdaq,
            "breadth_kospi":      breadth_kospi,
            "breadth_kosdaq":     breadth_kosdaq,
            "market_flow_kospi":  market_flow_kospi,
            "market_flow_kosdaq": market_flow_kosdaq,
            # 통합 (DB·기존 호환)
            "major":         major,
            "featured":      featured,
            "investor_flow": investor_flow,
            "breadth":       breadth,
            "market_flow":   market_flow,
        }

    except Exception as e:
        print(f"  [collect_stocks ERROR] {e}")
        return {"major": [], "featured": []}
    finally:
        _restore_webio(orig)


# ================================================================== #
#  ASIA OVERSEAS — Nikkei225 / CSI300 / HSI 구성종목 분석 (LSEG)
#
#  각 시장에 대해:
#    - 시총+거래대금 상위 N개 (major)
#    - 등락률 ±3% 이상 or 거래량 5일평균 대비 1.5배 이상 (featured)
#    - GICS 섹터별 평균 등락률 + 시총가중 등락률 (sectors)
# ================================================================== #

ASIA_OVERSEAS_MARKETS = [
    {"key": "jp", "name": "Nikkei225", "chain": "0#.N225",  "currency": "JPY"},
    {"key": "cn", "name": "CSI300",    "chain": "0#.CSI300", "currency": "CNY"},
    {"key": "hk", "name": "HSI",       "chain": "0#.HSI",    "currency": "HKD"},
]


def _fetch_asia_market(chain: str, target_date: str,
                       market_key: str = "",
                       n_major: int = 8, n_featured: int = 6,
                       lookback_days: int = 10) -> dict:
    """
    LSEG ChainRIC으로 단일 시장 구성종목 분석 + market_daily 저장.

    Parameters
    ----------
    chain        : LSEG Chain RIC (예: 0#.N225)
    target_date  : 분석 기준일 YYYY-MM-DD
    market_key   : jp | cn | hk (market_daily category 접두어용)
    """
    import lseg.data as ld

    # ── 1. 종목 메타 + 당일 스냅샷 (시총·종가·거래량·섹터) ──────────
    try:
        meta = ld.get_data(
            universe=chain,
            fields=[
                "TR.RIC", "TR.CompanyName",
                "TR.CompanyMarketCap",
                "TR.PriceClose",
                "TR.Volume",
                "TR.GICSSector",
            ],
        )
    except Exception as e:
        print(f"    [{chain} meta ERROR] {e}")
        return {}
    if meta is None or meta.empty:
        return {}

    meta.columns = [str(c).strip() for c in meta.columns]
    meta = meta.rename(columns={
        "Instrument":         "ticker",
        "RIC":                "ric",
        "Company Name":       "name",
        "Company Market Cap": "mktcap",
        "Price Close":        "close",
        "Volume":             "volume",
        "GICS Sector Name":   "sector",
    })
    # 유효 행만
    meta["close"]  = pd.to_numeric(meta["close"],  errors="coerce")
    meta["volume"] = pd.to_numeric(meta["volume"], errors="coerce")
    meta["mktcap"] = pd.to_numeric(meta["mktcap"], errors="coerce")
    meta = meta[meta["close"] > 0].copy()
    if meta.empty:
        return {}

    tickers = meta["ticker"].astype(str).tolist()

    # ── 2. lookback OHLCV (등락률·거래량 surge 계산 + market_daily 저장) ─
    try:
        hist = ld.get_data(
            universe=tickers,
            fields=[
                "TR.PriceOpen", "TR.PriceHigh", "TR.PriceLow",
                "TR.PriceClose", "TR.Volume", "TR.PriceClose.date",
            ],
            parameters={"SDate": f"-{lookback_days}D", "EDate": "0D", "Frq": "D"},
        )
    except Exception as e:
        print(f"    [{chain} hist ERROR] {e}")
        hist = pd.DataFrame()

    chg_map: dict[str, float] = {}
    surge_map: dict[str, float] = {}
    md_records: list[dict] = []
    if hist is not None and not hist.empty:
        hist.columns = [str(c).strip() for c in hist.columns]
        hist = hist.rename(columns={
            "Instrument":  "ticker",
            "Price Open":  "open",
            "Price High":  "high",
            "Price Low":   "low",
            "Price Close": "close",
            "Volume":      "volume",
            "Date":        "date",
        })
        for col in ("open", "high", "low", "close", "volume"):
            if col in hist.columns:
                hist[col] = pd.to_numeric(hist[col], errors="coerce")
        hist = hist.dropna(subset=["close", "date"])
        hist = hist.sort_values(["ticker", "date"])

        # market_daily 저장용 레코드 (전 종목 × 전 거래일)
        for _, row in hist.iterrows():
            close = row.get("close")
            if pd.isna(close) or close <= 0:
                continue
            date_str = str(row["date"])[:10]
            md_records.append({
                "date":     date_str,
                "session":  "asia",
                "category": f"{market_key}_stock" if market_key else "overseas_stock",
                "name":     str(row["ticker"]),
                "close":    float(close),
                "open":     float(row["open"])   if pd.notna(row.get("open"))   else None,
                "high":     float(row["high"])   if pd.notna(row.get("high"))   else None,
                "low":      float(row["low"])    if pd.notna(row.get("low"))    else None,
                "volume":   float(row["volume"]) if pd.notna(row.get("volume")) else None,
            })

        # 분석용 등락률 / surge_ratio
        for t, g in hist.groupby("ticker"):
            g = g[g["close"] > 0]
            if len(g) >= 2:
                prev_close = float(g["close"].iloc[-2])
                cur_close  = float(g["close"].iloc[-1])
                if prev_close > 0:
                    chg_map[str(t)] = round((cur_close - prev_close) / prev_close * 100, 2)
            vols = g["volume"].dropna()
            if len(vols) >= 4:
                today_vol = float(vols.iloc[-1])
                past_avg  = float(vols.iloc[:-1].tail(5).mean())
                if past_avg > 0:
                    surge_map[str(t)] = round(today_vol / past_avg, 2)

    # market_daily 저장
    if md_records:
        try:
            from db.db_manager import DBManager as _DBM
            n_saved = _DBM().upsert_market_daily(md_records)
            print(f"    [{market_key or chain} market_daily] 저장 {n_saved}건 "
                  f"({len(set(r['name'] for r in md_records))}개 종목 × "
                  f"{len(set(r['date'] for r in md_records))}일)")
        except Exception as _e:
            print(f"    [{market_key or chain} market_daily ERROR] {_e}")

    # 메타에 lookback 결과 병합
    meta["chg_pct"]     = meta["ticker"].astype(str).map(chg_map)
    meta["surge_ratio"] = meta["ticker"].astype(str).map(surge_map)
    meta["trade_val"]   = meta["close"] * meta["volume"]

    # 등락률을 못 받은 종목은 분석 불가 → 제외
    valid = meta.dropna(subset=["chg_pct"]).copy()
    if valid.empty:
        return {}

    # ── 3. 시장 폭 ─────────────────────────────────────────────────
    up   = int((valid["chg_pct"] > 0).sum())
    down = int((valid["chg_pct"] < 0).sum())
    flat = int((valid["chg_pct"] == 0).sum())
    tot  = up + down + flat
    breadth = {
        "up": up, "down": down, "flat": flat, "total": tot,
        "up_pct": round(up / tot * 100, 1) if tot > 0 else None,
        "adr":    round(up / down, 2)      if down > 0 else None,
    }

    # ── 4. major: 시총순위 + 거래대금순위 합산 ─────────────────────
    valid["rank_cap"]    = valid["mktcap"].rank(ascending=False)
    valid["rank_trade"]  = valid["trade_val"].rank(ascending=False)
    valid["major_score"] = valid["rank_cap"] + valid["rank_trade"]
    major_df = valid.nsmallest(n_major, "major_score")

    def _row_to_major(r) -> dict:
        return {
            "ticker":     str(r["ticker"]),
            "name":       str(r.get("name") or r["ticker"])[:40],
            "sector":     str(r.get("sector") or "-"),
            "close":      round(float(r["close"]), 2),
            "chg_pct":    float(r["chg_pct"]),
            "mktcap_b":   round(float(r["mktcap"]) / 1e9, 2) if pd.notna(r.get("mktcap")) else None,
            "trade_val_b": round(float(r["trade_val"]) / 1e9, 2) if pd.notna(r.get("trade_val")) else None,
        }

    major_list = [_row_to_major(r) for _, r in major_df.iterrows()]

    # ── 5. featured: |등락률|≥3% or 거래량 surge≥1.5 ───────────────
    valid["abs_chg"] = valid["chg_pct"].abs()
    feat_mask = (valid["abs_chg"] >= 3.0) | (valid["surge_ratio"].fillna(0) >= 1.5)
    cand = valid[feat_mask].copy()
    # 스코어: |등락률|순위 + surge_ratio순위 (낮을수록 우수)
    cand["rank_chg"]   = cand["abs_chg"].rank(ascending=False)
    cand["rank_surge"] = cand["surge_ratio"].fillna(0).rank(ascending=False)
    cand["feat_score"] = cand["rank_chg"] + cand["rank_surge"]
    feat_df = cand.nsmallest(n_featured, "feat_score")

    def _row_to_featured(r) -> dict:
        signals = []
        if r["chg_pct"] >=  3.0: signals.append("급등")
        elif r["chg_pct"] <= -3.0: signals.append("급락")
        sr = r.get("surge_ratio")
        if pd.notna(sr) and sr >= 1.5:
            signals.append(f"거래량{sr:.1f}x")
        return {
            "ticker":      str(r["ticker"]),
            "name":        str(r.get("name") or r["ticker"])[:40],
            "sector":      str(r.get("sector") or "-"),
            "close":       round(float(r["close"]), 2),
            "chg_pct":     float(r["chg_pct"]),
            "surge_ratio": float(sr) if pd.notna(sr) else None,
            "trade_val_b": round(float(r["trade_val"]) / 1e9, 2) if pd.notna(r.get("trade_val")) else None,
            "signal":      "+".join(signals) if signals else "변동",
        }

    featured_list = [_row_to_featured(r) for _, r in feat_df.iterrows()]

    # ── 6. 섹터 분석: 단순평균 + 시총가중평균 ──────────────────────
    sectors = []
    sec_g = valid[valid["sector"].notna() & (valid["sector"] != "-")].copy()
    if not sec_g.empty:
        sec_g["cap"] = sec_g["mktcap"].fillna(0)
        for sec_name, g in sec_g.groupby("sector"):
            total_cap = float(g["cap"].sum())
            wmean = float((g["chg_pct"] * g["cap"]).sum() / total_cap) if total_cap > 0 else None
            sectors.append({
                "sector":    str(sec_name),
                "n":         int(len(g)),
                "chg_avg":   round(float(g["chg_pct"].mean()), 2),
                "chg_wmean": round(wmean, 2) if wmean is not None else None,
                "mktcap_b":  round(total_cap / 1e9, 1),
            })
        # 시총가중 등락률 절대값 큰 순
        sectors.sort(key=lambda s: abs(s.get("chg_wmean") or s["chg_avg"]), reverse=True)

    return {
        "major":    major_list,
        "featured": featured_list,
        "sectors":  sectors,
        "breadth":  breadth,
    }


def fetch_asia_overseas_stocks(target_date: str,
                               n_major: int = 8,
                               n_featured: int = 6) -> dict:
    """일본·중국·홍콩 시장의 시총+거래대금 상위, 특징주, 섹터 분석.

    Returns
    -------
    {
      "jp": {"name": "Nikkei225", "major": [...], "featured": [...], "sectors": [...], "breadth": {...}},
      "cn": {...},
      "hk": {...},
    }
    """
    result: dict = {}
    for mkt in ASIA_OVERSEAS_MARKETS:
        print(f"  [asia overseas] {mkt['name']} ({mkt['chain']}) 분석 중...")
        try:
            data = _fetch_asia_market(
                mkt["chain"], target_date,
                market_key=mkt["key"],
                n_major=n_major, n_featured=n_featured,
            )
            if data:
                data["name"]     = mkt["name"]
                data["currency"] = mkt["currency"]
                result[mkt["key"]] = data
                print(f"    {mkt['name']}: major {len(data['major'])} / "
                      f"featured {len(data['featured'])} / sectors {len(data['sectors'])}  "
                      f"폭 {data['breadth'].get('up_pct')}%")
            else:
                print(f"    {mkt['name']}: 데이터 없음")
        except Exception as e:
            print(f"    [{mkt['name']} ERROR] {e}")
    return result


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
        close_v = v.get("close")
        if close_v is None or pd.isna(close_v):
            continue
        chg_v = v.get("chg_pct")
        sectors.append({
            "ric":     ric,
            "name":    meta_name,
            "close":   round(float(close_v), 2),
            "chg_pct": round(float(chg_v), 2) if (chg_v is not None and not pd.isna(chg_v)) else None,
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
    # 통화 매핑: yfinance suffix → 현지통화
    def _ccy_of(t: str) -> str:
        if t.endswith(".L"):  return "GBp"   # London은 pence 단위
        if t.endswith(".DE"): return "EUR"
        if t.endswith(".PA"): return "EUR"
        if t.endswith(".AS"): return "EUR"
        if t.endswith(".MI"): return "EUR"
        if t.endswith(".SW"): return "CHF"
        return "EUR"

    for ticker, df in stock_data.items():
        if len(df) < 2:
            continue
        close = float(df["Close"].iloc[-1])
        prev  = float(df["Close"].iloc[-2])
        if close <= 0 or prev <= 0:
            continue
        vol = float(df["Volume"].iloc[-1]) if "Volume" in df and pd.notna(df["Volume"].iloc[-1]) else None

        # 거래대금 (현지통화 millions) + surge_ratio (오늘 / 과거 5일 평균)
        trade_val_local_m = None
        surge_ratio = None
        if vol and vol > 0:
            trade_val_local_m = (close * vol) / 1e6
            hist_dvol = (df["Close"] * df["Volume"]).iloc[:-1].dropna()
            if len(hist_dvol) >= 2:
                avg = float(hist_dvol.tail(5).mean())
                if avg > 0:
                    surge_ratio = round(close * vol / avg, 2)

        stats[ticker] = {
            "name":         yf_names.get(ticker, ticker),
            "index":        yf_index.get(ticker, ""),
            "ccy":          _ccy_of(ticker),
            "close":        round(close, 2),
            "chg_pct":      round((close - prev) / abs(prev) * 100, 2),
            "volume":       int(vol) if vol else None,
            "trade_val_lm": round(trade_val_local_m, 2) if trade_val_local_m else None,
            "surge_ratio":  surge_ratio,
        }

    # ── 시총 조회 (LSEG TR.CompanyMarketCap, USD billion) ───────────
    mktcap_b_map: dict[str, float] = {}
    if stats:
        try:
            mc_part = ld.get_data(
                universe=list(stats.keys()),
                fields=["TR.CompanyMarketCap"],
            )
            if mc_part is not None and not mc_part.empty:
                for _, row in mc_part.iterrows():
                    tk = str(row.get("Instrument", "")).strip()
                    cap = row.get("Company Market Cap")
                    if tk and pd.notna(cap) and cap:
                        # LSEG는 현지통화 단위 — USD 환산은 생략하고 십억 단위로
                        mktcap_b_map[tk] = round(float(cap) / 1e9, 2)
        except Exception as _e:
            print(f"    [europe mktcap LSEG ERROR] {_e}")

    for tk, s in stats.items():
        s["mktcap_b"] = mktcap_b_map.get(tk)

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

    # |등락률| 상위 15개 → top_stocks (호환성 유지)
    top_stocks = [
        {"ticker": t, "name": s["name"], "index": s["index"],
         "close": s["close"], "chg_pct": s["chg_pct"]}
        for t, s in sorted(stats.items(), key=lambda x: abs(x[1]["chg_pct"]), reverse=True)[:15]
    ]

    # 시총 상위 10
    mktcap_top = [
        {"ticker": t, "name": s["name"], "index": s["index"], "ccy": s["ccy"],
         "close": s["close"], "chg_pct": s["chg_pct"],
         "mktcap_b": s.get("mktcap_b"),
         "dollar_vol_b": (s.get("trade_val_lm") / 1000) if s.get("trade_val_lm") else None,
         "signal": "시총상위"}
        for t, s in sorted(
            ((k, v) for k, v in stats.items() if v.get("mktcap_b") is not None),
            key=lambda x: x[1]["mktcap_b"], reverse=True
        )[:10]
    ]

    # 거래대금 상위 10
    tradeval_top = [
        {"ticker": t, "name": s["name"], "index": s["index"], "ccy": s["ccy"],
         "close": s["close"], "chg_pct": s["chg_pct"],
         "mktcap_b": s.get("mktcap_b"),
         "dollar_vol_b": (s.get("trade_val_lm") / 1000) if s.get("trade_val_lm") else None,
         "signal": "거래대금상위"}
        for t, s in sorted(
            ((k, v) for k, v in stats.items() if v.get("trade_val_lm") is not None),
            key=lambda x: x[1]["trade_val_lm"], reverse=True
        )[:10]
    ]

    # 거래대금 급증 + 급등락 (surge_ratio ≥ 1.5 & |chg| ≥ 2%)
    surge_cands = [
        (t, s) for t, s in stats.items()
        if s.get("surge_ratio") and s["surge_ratio"] >= 1.5
        and abs(s["chg_pct"]) >= 2.0
    ]
    surge_cands.sort(key=lambda x: x[1]["surge_ratio"], reverse=True)
    turnover_surge = []
    for t, s in surge_cands[:10]:
        signals = []
        if s["chg_pct"] >= 3:    signals.append("급등")
        elif s["chg_pct"] <= -3: signals.append("급락")
        signals.append(f"거래대금{s['surge_ratio']:.1f}x")
        turnover_surge.append({
            "ticker": t, "name": s["name"], "index": s["index"], "ccy": s["ccy"],
            "close": s["close"], "chg_pct": s["chg_pct"],
            "surge_ratio": s["surge_ratio"],
            "dollar_vol_b": (s.get("trade_val_lm") / 1000) if s.get("trade_val_lm") else None,
            "signal": "+".join(signals),
        })

    print(f"    [europe screening] 시총상위 {len(mktcap_top)} | 거래대금상위 {len(tradeval_top)} | "
          f"급증 {len(turnover_surge)}")

    # ── 3. market_daily 저장 ────────────────────────────────────────
    try:
        from db.db_manager import DBManager as _DBM
        md_records = []
        # 섹터 지수 (LSEG)
        for ric, v in sector_prices.items():
            close_v = v.get("close")
            if close_v is None or pd.isna(close_v):
                continue
            vol_v = v.get("volume")
            md_records.append({
                "date": target_date, "session": "europe", "category": "sector",
                "name": ric, "close": float(close_v), "open": None,
                "high": None, "low": None,
                "volume": float(vol_v) if (vol_v is not None and not pd.isna(vol_v)) else None,
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

    return {"sectors": sectors, "top_stocks": top_stocks, "breadth": eu_breadth,
            "mktcap_top": mktcap_top, "tradeval_top": tradeval_top,
            "turnover_surge": turnover_surge}


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


