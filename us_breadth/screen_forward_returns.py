"""
스크리닝 편입 종목의 편입 이후 수익률 (5/10/20/60 거래일)

입력: sp500_screen_history_<시작>_<끝>.xlsx 의 '일별편입' 시트 (run_sp500_screen_history.py 산출물)
      + DB market_daily 종가
정의:
  · 진입 = 편입일(D) 종가, 청산 = D+h 거래일 종가.  h ∈ {5, 10, 20, 60}
  · 신규편입 = 연속편입일수 == 1 인 (D, 종목)  ← 기본 분석 단위
    전체편입 = 필터링종목에 든 모든 (D, 종목) (보유 중 매일 중복 집계)
  · 초과수익 = 종목 수익률 − 같은 D·같은 h 의 유니버스(현재 구성종목 503) 동일가중 평균 수익률
  · D+h 가 DB 최신일을 넘으면 해당 기간은 계산하지 않는다 (N에 미포함)

출력: sp500_screen_fwdret_<시작>_<끝>.xlsx
  요약     — 신규/전체 × 기간별 N·평균·중앙값·승률·초과수익·유니버스평균
  섹터별   — 신규편입 기준 섹터 × 기간 평균수익률·초과수익·N
  섹터별_전체편입 — 같은 표, 전체편입 기준
  이벤트   — (편입일, 종목)별 수익률 상세
  기준     — 정의

사용법:
    python us_breadth/screen_forward_returns.py                       # output 폴더의 최신 history 파일
    python us_breadth/screen_forward_returns.py --history <path.xlsx>
"""

import argparse
import sqlite3
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sp500_volume_screen as m

HORIZONS = [5, 10, 20, 60]


def load_closes() -> pd.DataFrame:
    from db.db_manager import DBManager
    tickers = DBManager().get_latest_spx_constituents()
    with sqlite3.connect(str(m.DB_PATH)) as conn:
        df = pd.read_sql_query(
            "SELECT date, name AS ticker, close FROM market_daily "
            "WHERE session='us' AND category='stock' AND close IS NOT NULL", conn)
    wide = df.pivot(index="date", columns="ticker", values="close").sort_index()
    wide = wide[[t for t in tickers if t in wide.columns]]
    # 부분 날짜(커버리지 95% 미만) 제거 — 스크리닝과 같은 기준
    return wide[wide.notna().sum(axis=1) >= len(tickers) * 0.95]


def forward_returns(closes: pd.DataFrame, dates: list[str]) -> dict[int, pd.DataFrame]:
    """{h: DataFrame(index=date, columns=ticker)} — D 종가 → D+h 종가 수익률(%)."""
    idx = {d: i for i, d in enumerate(closes.index)}
    out = {}
    for h in HORIZONS:
        rows = {}
        for d in dates:
            i = idx.get(d)
            if i is None or i + h >= len(closes):
                continue
            rows[d] = (closes.iloc[i + h] / closes.iloc[i] - 1) * 100
        out[h] = pd.DataFrame(rows).T
    return out


def summarize(ev: pd.DataFrame, by: list[str] | None = None) -> pd.DataFrame:
    recs = []
    groups = ev.groupby(by) if by else [((), ev)]
    for key, g in groups:
        key = key if isinstance(key, tuple) else (key,)
        for h in HORIZONS:
            r, x, u = g[f"수익률_{h}d"].dropna(), g[f"초과_{h}d"].dropna(), g[f"유니버스_{h}d"].dropna()
            rec = dict(zip(by or [], key))
            rec.update({"기간": f"{h}일", "N": len(r),
                        "평균수익률(%)": round(r.mean(), 2) if len(r) else None,
                        "중앙값(%)": round(r.median(), 2) if len(r) else None,
                        "승률(%)": round((r > 0).mean() * 100, 1) if len(r) else None,
                        "평균초과수익(%p)": round(x.mean(), 2) if len(x) else None,
                        "초과승률(%)": round((x > 0).mean() * 100, 1) if len(x) else None,
                        "유니버스평균(%)": round(u.mean(), 2) if len(u) else None})
            recs.append(rec)
    return pd.DataFrame(recs)


def sector_table(ev: pd.DataFrame) -> pd.DataFrame:
    s = summarize(ev, ["섹터"])
    wide = s.pivot(index="섹터", columns="기간", values=["평균수익률(%)", "평균초과수익(%p)", "N"])
    order = [f"{h}일" for h in HORIZONS]
    wide = wide.reindex(columns=pd.MultiIndex.from_product([["평균수익률(%)", "평균초과수익(%p)", "N"], order]))
    wide.columns = [f"{a}_{b}" for a, b in wide.columns]
    n_events = ev.groupby("섹터").size().rename("편입건수")
    return wide.join(n_events).sort_values("편입건수", ascending=False).reset_index()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--history")
    args = ap.parse_args()
    hist = Path(args.history) if args.history else max(m.OUT_DIR.glob("sp500_screen_history_*.xlsx"))
    daily = pd.read_excel(hist, sheet_name="일별편입")
    daily["date"] = daily["date"].astype(str)
    dates = sorted(daily["date"].unique())
    print(f"[입력] {hist.name} — {dates[0]} ~ {dates[-1]}, 편입 {len(daily)}건")

    closes = load_closes()
    closes.index = closes.index.astype(str)
    print(f"[가격] {closes.index[0]} ~ {closes.index[-1]} ({closes.shape[1]}종목)")
    fr = forward_returns(closes, dates)

    ev = daily[["date", "ticker", "종목명", "섹터", "연속편입일수", "순위", "종합점수"]].copy()
    for h in HORIZONS:
        f = fr[h]
        univ = f.mean(axis=1)
        ev[f"수익률_{h}d"] = [f.at[d, t] if d in f.index and t in f.columns else None
                             for d, t in zip(ev["date"], ev["ticker"])]
        ev[f"유니버스_{h}d"] = ev["date"].map(univ)
        ev[f"초과_{h}d"] = ev[f"수익률_{h}d"] - ev[f"유니버스_{h}d"]
    ev = ev.round(2)
    new = ev[ev["연속편입일수"] == 1]

    summ = pd.concat([summarize(new).assign(기준="신규편입"), summarize(ev).assign(기준="전체편입")])
    summ = summ[["기준"] + [c for c in summ.columns if c != "기준"]]
    sec_new, sec_all = sector_table(new), sector_table(ev)

    start, end = dates[0].replace("-", ""), dates[-1].replace("-", "")
    params = pd.DataFrame([
        {"항목": "입력", "값": hist.name},
        {"항목": "편입 기간", "값": f"{dates[0]} ~ {dates[-1]} ({len(dates)} 개장일)"},
        {"항목": "가격 최신일", "값": closes.index[-1]},
        {"항목": "수익률", "값": "편입일 종가 → h거래일 후 종가 (배당 미포함 조정종가, 증분수집 조정 오차 ~1% 가능)"},
        {"항목": "신규편입", "값": f"연속편입일수 = 1 인 건 ({len(new)}건)"},
        {"항목": "전체편입", "값": f"편입된 모든 날 ({len(ev)}건, 같은 종목 중복)"},
        {"항목": "초과수익", "값": "같은 날·같은 기간 유니버스(현재 S&P500 구성종목) 동일가중 평균 대비"},
        {"항목": "주의", "값": "D+h가 가격 최신일을 넘는 건은 제외 → 60일은 기간 초반 편입만 표본"},
        {"항목": "주의", "값": "진입을 편입일 종가로 가정 (실제 스크리닝은 다음날 아침 → 다음날 시가 진입이 현실적)"},
    ])

    out = m.OUT_DIR / f"sp500_screen_fwdret_{start}_{end}.xlsx"
    with pd.ExcelWriter(str(out), engine="openpyxl") as w:
        for name, df in [("요약", summ), ("섹터별", sec_new), ("섹터별_전체편입", sec_all),
                         ("이벤트", ev), ("기준", params)]:
            df.to_excel(w, sheet_name=name, index=False)
            m._col_width(w.sheets[name], df)
    print(f"[저장] {out}\n")
    pd.set_option("display.width", 200)
    print(summ.to_string(index=False))
    print("\n■ 섹터별 (신규편입)")
    print(sec_new.to_string(index=False))


if __name__ == "__main__":
    main()
