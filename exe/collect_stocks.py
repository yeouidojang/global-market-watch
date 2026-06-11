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
        ohlcv = ohlcv.rename(columns={"종가": "close", "거래대금": "trade_val", "거래량": "volume"})

        # ── 2. 시가총액 + 상장주식수 ─────────────────────────────────
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
            featured.append({
                "ticker":   r["ticker"],
                "name":     r["name"],
                "market":   r["market"],
                "close":    int(r["close"]),
                "chg_pct":  round(float(r["chg_pct"]), 2),
                "trade_val": int(r["trade_val"]),
                "turnover": round(float(r["turnover"]), 3),
                "signal":   "+".join(signals) if signals else "변동",
            })

        return {"major": major, "featured": featured}

    except Exception as e:
        print(f"  [collect_stocks ERROR] {e}")
        return {"major": [], "featured": []}
    finally:
        _restore_webio(orig)


# ================================================================== #
#  EUROPE — STOXX600 섹터 + DAX/FTSE 주요 종목 (LSEG)
# ================================================================== #

def fetch_europe_stocks(target_date: str) -> dict:
    """
    유럽 섹터 성과 + DAX/FTSE/CAC 주요 종목 수집 (LSEG).

    Returns
    -------
    {
      "sectors":    [{"name", "close", "chg_pct"}, ...],
      "top_stocks": [{"ric", "name", "close", "chg_pct", "volume"}, ...]
    }
    """
    import lseg.data as ld
    import yaml

    cfg_path = BASE_DIR / "config" / "indices.yaml"
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    eu_cfg = cfg.get("europe", {})

    def _lseg_batch(rics: list[str]) -> dict[str, dict]:
        """RIC 리스트 → {ric: {close, chg_pct, volume}} 반환."""
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

    # ── 섹터 지수 수집 ───────────────────────────────────────────────
    sector_rics  = [s["ric"] for s in eu_cfg.get("sectors", [])]
    sector_names = {s["ric"]: s["name"] for s in eu_cfg.get("sectors", [])}
    print(f"    [europe sectors] {sector_rics}")
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
    # 등락률 내림차순 정렬
    sectors.sort(key=lambda x: (x["chg_pct"] or 0), reverse=True)

    # ── 주요 종목 수집 ───────────────────────────────────────────────
    stock_rics  = [s["ric"] for s in eu_cfg.get("top_stocks", [])]
    stock_names = {s["ric"]: s["name"] for s in eu_cfg.get("top_stocks", [])}
    print(f"    [europe stocks] {stock_rics}")
    stock_prices = _lseg_batch(stock_rics)

    top_stocks = []
    for ric, meta_name in stock_names.items():
        v = stock_prices.get(ric, {})
        if v.get("close") is not None:
            top_stocks.append({
                "ric":     ric,
                "name":    meta_name,
                "close":   round(float(v["close"]), 4),
                "chg_pct": round(float(v["chg_pct"]), 2) if v.get("chg_pct") is not None else None,
                "volume":  int(v["volume"]) if v.get("volume") is not None else None,
            })
    # 등락률 내림차순
    top_stocks.sort(key=lambda x: (x["chg_pct"] or 0), reverse=True)

    return {"sectors": sectors, "top_stocks": top_stocks}


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

# LSEG RIC suffix 제거 + 특수 케이스 처리
_RIC_SPECIALS = {
    "BRKb": "BRK-B", "BRKa": "BRK-A",
    "BFb":  "BF-B",  "BFa":  "BF-A",
    "WAT_z": None,   # 제외
}

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


def _lseg_screener(rics_yf: list[str]) -> dict[str, dict]:
    """
    LSEG get_data로 시총 + EPS 추정치 1W 변화 수집.
    - TR.CompanyMarketCap: billion USD 단위로 반환
    - TR.MeanPctChg + parameters: Mean Estimate Pct Change 컬럼으로 반환
    """
    import lseg.data as ld

    candidates = rics_yf[:100]
    result: dict[str, dict] = {t: {"mktcap_b": None, "eps_chg_1w": None} for t in candidates}
    session_opened = False

    try:
        # 세션 상태 직접 확인
        try:
            state = ld.session.get_default().open_state.name  # "Opened" if active
        except Exception:
            state = "Closed"

        if state != "Opened":
            ld.open_session(
                config_name=str(BASE_DIR.parent / "lseg-data.config.json")
            )
            session_opened = True

        batch_size = 50

        # ── 시총 배치 수집 ──────────────────────────────
        for i in range(0, len(candidates), batch_size):
            batch = candidates[i:i + batch_size]
            try:
                part = ld.get_data(
                    universe=batch,
                    fields=["TR.CompanyMarketCap"],
                )
                if part is not None and not part.empty:
                    for _, row in part.iterrows():
                        ticker = str(row.get("Instrument", "")).strip()
                        mktcap = row.get("Company Market Cap")
                        if ticker and pd.notna(mktcap) and mktcap:
                            result.setdefault(ticker, {})["mktcap_b"] = round(float(mktcap), 2)
            except Exception as be:
                print(f"    [LSEG mktcap BATCH ERROR i={i}] {be}")

        # ── EPS 추정치 변화 배치 수집 ───────────────────
        for i in range(0, len(candidates), batch_size):
            batch = candidates[i:i + batch_size]
            try:
                part = ld.get_data(
                    universe=batch,
                    fields="TR.MeanPctChg",
                    parameters={"EstimateMeasure": "EPS", "Period": "NTM", "WP": "7d"},
                )
                if part is not None and not part.empty:
                    for _, row in part.iterrows():
                        ticker = str(row.get("Instrument", "")).strip()
                        eps = row.get("Mean Estimate Pct Change")
                        if ticker and pd.notna(eps) and eps is not None:
                            result.setdefault(ticker, {})["eps_chg_1w"] = round(float(eps), 2)
            except Exception as be:
                print(f"    [LSEG eps BATCH ERROR i={i}] {be}")

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

    if not stats:
        return {"sectors": sectors, "mktcap_top": [], "tradeval_top": [],
                "turnover_surge": [], "eps_revision": []}

    # ── 4. 시총 필터를 위한 LSEG 조회 (상위 후보 100개) ─────────────
    # 거래대금 상위 100개만 LSEG 조회 (전체 조회는 부하 과다)
    cands_by_dvol = sorted(stats.keys(), key=lambda t: stats[t]["dollar_vol"], reverse=True)[:100]
    print(f"    [us LSEG screener] 상위 {len(cands_by_dvol)}개 시총+EPS 조회 중...")
    lseg_meta = _lseg_screener(cands_by_dvol)

    def _entry(ticker: str, signal: str = "") -> dict:
        s = stats[ticker]
        m = lseg_meta.get(ticker, {})
        return {
            "ticker":      ticker,
            "close":       s["close"],
            "chg_pct":     s["chg_pct"],
            "dollar_vol_b": round(s["dollar_vol"] / 1e9, 2),
            "surge_ratio": s["surge_ratio"],
            "mktcap_b":    m.get("mktcap_b"),
            "eps_chg_1w":  m.get("eps_chg_1w"),
            "signal":      signal,
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

    # ── 8. EPS 추정치 변화 (1W) ──────────────────────────────────────
    eps_cands = [
        t for t in cands_by_dvol
        if lseg_meta.get(t, {}).get("eps_chg_1w") is not None
        and _passes_mktcap(t)
    ]
    eps_up   = sorted([t for t in eps_cands if lseg_meta[t]["eps_chg_1w"] > 0],
                      key=lambda t: lseg_meta[t]["eps_chg_1w"], reverse=True)
    eps_down = sorted([t for t in eps_cands if lseg_meta[t]["eps_chg_1w"] < 0],
                      key=lambda t: lseg_meta[t]["eps_chg_1w"])

    eps_revision = []
    for t in (eps_up[:5] + eps_down[:5]):
        chg = lseg_meta[t]["eps_chg_1w"]
        e = _entry(t, f"EPS추정{'상향' if chg > 0 else '하향'}{chg:+.1f}%")
        eps_revision.append(e)

    print(f"    [us screening] 시총상위 {len(mktcap_top)} | 거래대금상위 {len(tradeval_top)} | "
          f"급증 {len(turnover_surge)} | EPS {len(eps_revision)}")

    return {
        "sectors":       sectors,
        "mktcap_top":    mktcap_top,
        "tradeval_top":  tradeval_top,
        "turnover_surge": turnover_surge,
        "eps_revision":  eps_revision,
    }


