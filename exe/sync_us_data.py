"""
US 스크리닝 데이터 동기화 — 미수집 구간을 스스로 찾아 채운다 (run_session·스케줄러와 독립)

  [가격] market_daily (session=us, category=stock) ← yfinance
         · 최근 1년 XNYS 개장일 중 커버리지 95% 미만인 날짜 → 그 날짜부터 최신 개장일까지 전 종목 수집
         · 최근 --refresh-days 개장일은 항상 다시 받는다 (마감 직후 거래량·배당조정 미확정 보정)
         · 이력에 구멍이 있거나 이력이 늦게 시작하는 종목 → 그 종목만 1년치 수집
         · 받아온 날짜라도 커버리지 95% 미만이면 저장하지 않고 "미확정"으로 남긴다 (다음 실행에 재시도)
  [EPS]  eps_cache ← LSEG TR.EPSMeanEstimate (Period=NTM, Sdate=금요일)
         · 주간 수집: 최근 1년 금요일 중 스냅샷이 없거나 커버리지 95% 미만인 날을 오래된 순으로 수집
         · 수집 후 1년치 스냅샷의 1W/1M/3M 변화율을 원본값 히스토리로 다시 계산한다
           (백필 순서 때문에 기준 스냅샷보다 먼저 계산돼 비어 있던 변화율도 여기서 채워진다)

유니버스는 DB spx_constituents 최신 스냅샷 (exe/collect_spx_constituents.py).

사용법:
    python exe/sync_us_data.py                      # 가격 + EPS
    python exe/sync_us_data.py --only price         # 가격만 (LSEG 불필요)
    python exe/sync_us_data.py --only eps
    python exe/sync_us_data.py --dry-run            # 수집 대상만 출력
    python exe/sync_us_data.py --full-price         # 1년치 가격 전체 재수집 (조정계수 일괄 정렬)
    python exe/sync_us_data.py --lookback-days 365 --refresh-days 3
"""

import argparse
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from db.db_manager import DBManager
from exe.collect_us_stocks import COVERAGE_MIN, fetch_ohlcv, load_spx_tickers

LOOKBACK_DAYS = 365
REFRESH_DAYS  = 3


def _sessions(start: str, end: str) -> list[str]:
    """start~end 중 이미 장이 끝난 XNYS 개장일."""
    import exchange_calendars as ec
    cal = ec.get_calendar("XNYS")
    days = cal.sessions_in_range(start, end)
    now = pd.Timestamp.now(tz="UTC")
    return [d.strftime("%Y-%m-%d") for d in days if cal.session_close(d) < now]


def _db_price_rows(start: str) -> pd.DataFrame:
    db = DBManager()
    conn = db._connect()
    try:
        return pd.read_sql_query(
            "SELECT date, name AS ticker FROM market_daily "
            "WHERE session='us' AND category='stock' AND close IS NOT NULL AND date >= ?",
            conn, params=[start],
        )
    finally:
        conn.close()


# ── 가격 ──────────────────────────────────────────────────────────────────────
def plan_price(tickers: list[str], start: str, refresh_days: int, full: bool) -> dict:
    today = date.today().strftime("%Y-%m-%d")
    sessions = _sessions(start, today)
    need = int(len(tickers) * COVERAGE_MIN)

    rows = _db_price_rows(start)
    rows = rows[rows["ticker"].isin(tickers)]
    counts = rows.groupby("date")["ticker"].nunique()
    missing_dates = [d for d in sessions if counts.get(d, 0) < need]

    # 전 종목 수집 구간: 가장 이른 미수집일 또는 최근 refresh 구간 ~ 최신 개장일
    refresh = sessions[-refresh_days:] if refresh_days > 0 else []
    if full:
        range_start = sessions[0]
    else:
        heads = [d for d in (missing_dates[:1] + refresh[:1]) if d]
        range_start = min(heads) if heads else None

    # 종목 단위 구멍: 첫 행 이후 빠진 개장일이 있거나, 이력이 구간 시작보다 늦게 시작하는 종목
    # (신규 상장·분사 종목은 매번 다시 받게 되지만 몇 종목뿐이라 비용이 작다)
    ticker_gaps = []
    if not full:
        # 날짜 단위로 이미 비어 있는 날은 전 종목 수집에서 채우므로 여기선 빼고 센다
        sess_idx = pd.Index([d for d in sessions if d not in set(missing_dates)])
        by_ticker = rows[rows["date"].isin(sess_idx)].groupby("ticker")["date"].agg(["min", "nunique"])
        for t in tickers:
            if t not in by_ticker.index:
                ticker_gaps.append(t)
                continue
            first, n = by_ticker.loc[t, "min"], by_ticker.loc[t, "nunique"]
            expected = int((sess_idx >= first).sum())
            if first > sess_idx[0] or n < expected:
                ticker_gaps.append(t)

    return {"sessions": sessions, "need": need, "missing_dates": missing_dates,
            "range_start": range_start, "ticker_gaps": ticker_gaps}


def sync_price(tickers: list[str], start: str, refresh_days: int, full: bool, dry_run: bool) -> dict:
    p = plan_price(tickers, start, refresh_days, full)
    sessions, need = p["sessions"], p["need"]
    last = sessions[-1]
    print(f"[가격] 유니버스 {len(tickers)}개 | 개장일 {len(sessions)}일 ({sessions[0]} ~ {last})")
    print(f"  미수집·부분 날짜 {len(p['missing_dates'])}일: {', '.join(p['missing_dates'][:10])}"
          f"{' …' if len(p['missing_dates']) > 10 else ''}")
    print(f"  전 종목 수집 구간: {p['range_start'] or '-'} ~ {last}")
    print(f"  이력 부족 종목 {len(p['ticker_gaps'])}개: {', '.join(p['ticker_gaps'][:15])}"
          f"{' …' if len(p['ticker_gaps']) > 15 else ''}")
    if dry_run:
        return {"saved": 0, "pending": []}

    yf_end = (pd.Timestamp(last) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    session_set = set(sessions)
    records, pending = [], []

    if p["range_start"]:
        recs = [r for r in fetch_ohlcv(tickers, start=p["range_start"], end=yf_end) if r["date"] in session_set]
        cnt = pd.Series([r["date"] for r in recs], dtype=object).value_counts()
        pending = [d for d in sessions if d >= p["range_start"] and cnt.get(d, 0) < need]
        records += [r for r in recs if r["date"] not in pending]
        if pending:
            detail = ", ".join(f"{d}({cnt.get(d, 0)}/{len(tickers)})" for d in pending)
            print(f"  [미확정] 커버리지 {COVERAGE_MIN:.0%} 미만 → 저장 안 함, 다음 실행에 재시도: {detail}")

    if p["ticker_gaps"]:
        recs = fetch_ohlcv(p["ticker_gaps"], start=sessions[0], end=yf_end)
        records += [r for r in recs if r["date"] in session_set and r["date"] not in pending]

    saved = DBManager().upsert_market_daily(records) if records else 0
    print(f"  → {len(records)}건 upsert ({saved})")
    return {"saved": saved, "pending": pending}


# ── EPS ───────────────────────────────────────────────────────────────────────
def _eps_counts(start: str) -> pd.Series:
    db = DBManager()
    conn = db._connect()
    try:
        df = pd.read_sql_query(
            "SELECT fetched_date, COUNT(*) n FROM eps_cache "
            "WHERE eps_mean_est IS NOT NULL AND fetched_date >= ? GROUP BY fetched_date",
            conn, params=[start],
        )
    finally:
        conn.close()
    return df.set_index("fetched_date")["n"]


def plan_eps(tickers: list[str], start: str) -> dict:
    # 오늘 이전(당일 제외) 금요일만 — 금요일 당일엔 뉴욕 장이 아직 안 끝났다
    fridays = [d.strftime("%Y-%m-%d")
               for d in pd.date_range(start, date.today() - timedelta(days=1), freq="W-FRI")]
    need = int(len(tickers) * COVERAGE_MIN)
    counts = _eps_counts(start)
    missing = [d for d in fridays if counts.get(d, 0) < need]
    return {"fridays": fridays, "need": need, "missing": missing}


def recompute_eps_changes(start: str) -> int:
    """start 이후 모든 스냅샷의 1W/1M/3M 변화율을 원본값 히스토리로 재계산 (오래된 순)."""
    from exe.collect_stocks import calc_eps_changes
    db = DBManager()
    dates = sorted(_eps_counts(start).index)
    updated = 0
    for d in dates:
        est = db.get_eps_estimates_at(d)
        updated += db.update_eps_changes(d, calc_eps_changes(d, est))
    return updated


def sync_eps(tickers: list[str], start: str, dry_run: bool) -> dict:
    p = plan_eps(tickers, start)
    print(f"[EPS] 금요일 {len(p['fridays'])}개 ({p['fridays'][0]} ~ {p['fridays'][-1]}) | "
          f"미수집 {len(p['missing'])}개: {', '.join(p['missing'])}")
    if dry_run:
        return {"collected": [], "failed": []}

    collected, failed = [], []
    if p["missing"]:
        import lseg.data as ld
        from exe.collect_stocks import _lseg_screener, calc_eps_changes
        cfg_path = os.getenv("LSEG_CONFIG_PATH", str(Path.home() / "lseg-data.config.json"))
        ld.open_session(config_name=cfg_path)
        try:
            for d in p["missing"]:
                t0 = time.time()
                # 시총 조회는 필요 없으므로 rics_yf=[] — EPS만 전 유니버스 대상으로 받는다
                meta = _lseg_screener([], fetch_eps=True, target_date=d, eps_universe=tickers)
                est = {t: m["eps_mean_est"] for t, m in meta.items()
                       if t in tickers and m.get("eps_mean_est") is not None}
                if len(est) < p["need"]:
                    print(f"  {d}: {len(est)}/{len(tickers)} — 커버리지 부족, 저장 안 함")
                    failed.append(d)
                    continue
                chg = calc_eps_changes(d, est)
                DBManager().upsert_eps_cache([
                    {"ticker": t, "eps_mean_est": v, "fetched_date": d, **chg.get(t, {})}
                    for t, v in est.items()
                ])
                collected.append(d)
                print(f"  {d}: {len(est)}/{len(tickers)} 저장 ({time.time() - t0:.0f}s)")
        finally:
            try:
                ld.close_session()
            except Exception:
                pass

    n = recompute_eps_changes(start)
    print(f"  변화율 재계산: {n}행")
    return {"collected": collected, "failed": failed}


def main():
    ap = argparse.ArgumentParser(description="US 스크리닝 데이터 미수집 구간 동기화")
    ap.add_argument("--only", choices=["price", "eps"])
    ap.add_argument("--lookback-days", type=int, default=LOOKBACK_DAYS)
    ap.add_argument("--refresh-days", type=int, default=REFRESH_DAYS)
    ap.add_argument("--full-price", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    start = (date.today() - timedelta(days=args.lookback_days)).strftime("%Y-%m-%d")
    tickers = load_spx_tickers()
    print(f"[sync_us_data] 기준 시작일 {start} (최근 {args.lookback_days}일)\n")

    errors = []
    if args.only in (None, "price"):
        try:
            sync_price(tickers, start, args.refresh_days, args.full_price, args.dry_run)
        except Exception as e:
            errors.append(f"가격: {e}")
            print(f"  [가격 오류] {e}")
        print()
    if args.only in (None, "eps"):
        try:
            r = sync_eps(tickers, start, args.dry_run)
            if r["failed"]:
                errors.append(f"EPS 커버리지 부족: {', '.join(r['failed'])}")
        except Exception as e:
            errors.append(f"EPS: {e}")
            print(f"  [EPS 오류] {e}")

    if errors:
        print("\n[sync_us_data] 실패 항목: " + " | ".join(errors))
        sys.exit(1)
    print("\n[sync_us_data] 완료")


if __name__ == "__main__":
    main()
