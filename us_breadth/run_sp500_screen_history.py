"""
S&P 500 스크리닝 기간 일괄 실행 + 연속편입일수 요약
  · 기간 내 모든 개장일(DB 기준)에 대해 sp500_volume_screen.run() → 일별 sp500_screen_YYYYMMDD.xlsx
    (필터링종목 시트에 '연속편입일수' 포함, 각 날짜는 그 날 이하 데이터만 사용)
  · 요약: sp500_screen_history_<시작>_<끝>.xlsx
      일별편입   — 날짜 × 편입종목 (연속편입일수, 종합점수, 순위)
      종목요약   — 종목별 편입일수, 최장연속, 최근 기준 연속, 첫/마지막 편입일
      매트릭스   — 종목 × 날짜, 셀 = 그날의 연속편입일수 (미편입 빈칸)
      기준       — 기간·파라미터
  · Slack 업로드 안 함

사용법:
    python us_breadth/run_sp500_screen_history.py                          # 최근 3개월
    python us_breadth/run_sp500_screen_history.py --start 2026-06-30 --end 2026-09-29
"""

import argparse
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sp500_volume_screen as m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--months", type=int, default=3)
    args = ap.parse_args()

    meta = m.fetch_sp500_meta()
    tickers = list(meta.keys())

    # 끝 날짜 기본값: DB의 최신 완전 개장일
    probe = m.load_ohlcv(tickers, args.end or "9999-12-31", n_days=1)
    end = args.end or probe["date"].max().strftime("%Y-%m-%d")
    start = args.start or (pd.Timestamp(end) - pd.DateOffset(months=args.months) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    # 기간 거래일 + 지표창 + 연속일 역산 여유분을 한 번에 로드
    span = len(pd.bdate_range(start, end)) + m.LOAD_DAYS + m.STREAK_MAX + 5
    ohlcv_all = m.load_ohlcv(tickers, end, n_days=span)
    eps_hist = m.load_eps_history(end)
    dates = [d.strftime("%Y-%m-%d") for d in sorted(ohlcv_all["date"].unique())
             if pd.Timestamp(start) <= d <= pd.Timestamp(end)]
    print(f"[history] {start} ~ {end} | 개장일 {len(dates)}일\n")

    cache: dict = {}
    rows = []
    t0 = time.time()
    for d in dates:
        screened, path = m.run(d, meta=meta, ohlcv_all=ohlcv_all, eps_hist=eps_hist,
                               streak_cache=cache, quiet=True)
        cache[pd.Timestamp(d)] = set(screened["ticker"]) if not screened.empty else set()
        eps_date = m.eps_as_of(eps_hist, d)["fetched_date"].max()
        top = ", ".join(screened["ticker"].head(5)) if not screened.empty else "-"
        print(f"  {d}  통과 {len(screened):3d}  EPS {eps_date}  상위: {top}")
        for rank, r in enumerate(screened.itertuples(index=False), 1):
            rows.append({"date": d, "순위": rank, "ticker": r.ticker, "종목명": r.종목명,
                         "섹터": r.섹터, "연속편입일수": r.연속편입일수, "종합점수": r.종합점수,
                         "EPS수집일": eps_date})
    print(f"\n일별 실행 완료 ({time.time() - t0:.0f}s)")

    daily = pd.DataFrame(rows)
    mat = daily.pivot(index="ticker", columns="date", values="연속편입일수").reindex(columns=dates)

    # 종목별 최장 연속: 기간 내 연속편입일수의 최댓값 (기간 시작 전부터 이어진 연속도 포함)
    last = dates[-1]
    summ = daily.groupby("ticker").agg(
        종목명=("종목명", "first"), 섹터=("섹터", "first"),
        편입일수=("date", "count"), 최장연속=("연속편입일수", "max"),
        첫편입=("date", "min"), 마지막편입=("date", "max"),
    )
    summ["최근기준_연속"] = mat[last].reindex(summ.index).fillna(0).astype(int)
    summ["편입비율(%)"] = (summ["편입일수"] / len(dates) * 100).round(1)
    summ = summ.sort_values(["최근기준_연속", "최장연속", "편입일수"], ascending=False).reset_index()

    counts = daily.groupby("date").size().reindex(dates, fill_value=0)
    params = pd.DataFrame([
        {"항목": "기간", "값": f"{start} ~ {end} ({len(dates)} 개장일)"},
        {"항목": "연속편입일수", "값": f"그 날 포함, 필터링종목에 연속으로 든 거래일 수. 기간 시작 전 이력도 역산(상한 {m.STREAK_MAX})"},
        {"항목": "데이터 기준", "값": "각 날짜는 그 날 이하 가격·EPS만 사용 (EPS = 그 날 이하 최신 금요일 스냅샷)"},
        {"항목": "유니버스", "값": "현재 spx_constituents 스냅샷을 전 기간에 적용 (생존편향 있음)"},
        {"항목": "일평균 통과 종목", "값": round(counts.mean(), 1)},
        {"항목": "통과 종목 최소/최대", "값": f"{counts.min()} / {counts.max()}"},
        {"항목": "기간 중 1회 이상 편입 종목", "값": len(summ)},
    ])

    out = m.OUT_DIR / f"sp500_screen_history_{start.replace('-', '')}_{end.replace('-', '')}.xlsx"
    with pd.ExcelWriter(str(out), engine="openpyxl") as w:
        daily.to_excel(w, sheet_name="일별편입", index=False)
        summ.to_excel(w, sheet_name="종목요약", index=False)
        mat.loc[summ["ticker"]].reset_index().to_excel(w, sheet_name="매트릭스", index=False)
        params.to_excel(w, sheet_name="기준", index=False)
        for sheet, df in [("일별편입", daily), ("종목요약", summ), ("기준", params)]:
            m._col_width(w.sheets[sheet], df)
        w.sheets["매트릭스"].freeze_panes = "B2"
    print(f"[저장] {out}")
    print("\n■ 최근 기준 연속편입 상위")
    print(summ.head(15).to_string(index=False))


if __name__ == "__main__":
    main()
