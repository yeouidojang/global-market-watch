"""
DB에서 최신 데이터를 읽어 스냅샷(변동률·플래그) 생성

반환 구조:
  {
    "date": "2026-06-10",
    "session": "us",
    "indices": { "SP500": {"close": 5800, "chg_pct": 0.52, "flag": ""}, ... },
    "macro":   { "USD_KRW": {"close": 1380, "chg_pct": -0.1, "flag": ""}, ... },
    "econ_upcoming": [{"event_date":..., "indicator":..., "forecast":...}, ...]
  }
"""

import sys
from datetime import date, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import pandas as pd
from db.db_manager import DBManager


# 이상 변동 임계값
THRESHOLDS = {
    "index":      3.0,   # ±3% 이상이면 🚨
    "fx":         1.5,
    "rate":       0.15,  # 절대값 bp 단위가 아닌 % 기준
    "commodity":  3.0,
    "volatility": 10.0,
}


def compute_chg_pct(current: float, prev: float) -> float | None:
    if prev and prev != 0 and current is not None:
        return round((current - prev) / abs(prev) * 100, 2)
    return None


def flag(chg_pct: float | None, category: str) -> str:
    if chg_pct is None:
        return ""
    thresh = THRESHOLDS.get(category, 3.0)
    if chg_pct >= thresh:
        return "🚨+"
    if chg_pct <= -thresh:
        return "🚨-"
    return ""


def build_snapshot(session: str, target_date: str = None, stocks_data: dict = None) -> dict:
    """
    session  : asia | europe | us
    target_date : YYYY-MM-DD (기본: 오늘)
    """
    today = target_date or date.today().strftime("%Y-%m-%d")
    db    = DBManager()

    # 최근 35일 데이터 로드 (1D/1W/1M 변동률 계산용)
    all_names_df = db.get_latest_by_name()
    if all_names_df.empty:
        return {"date": today, "session": session, "indices": {}, "macro": {}, "econ_upcoming": []}

    all_names = all_names_df["name"].tolist()

    df = db.get_recent(all_names, n_days=35)
    if df.empty:
        return {"date": today, "session": session, "indices": {}, "macro": {}, "econ_upcoming": []}

    df = df.sort_values("date")

    pivot = df.pivot_table(index="date", columns="name", values="close", aggfunc="last")
    dates_sorted = sorted(pivot.index.tolist())

    def get_close(name, d):
        try:
            v = pivot.loc[d, name]
            return None if pd.isna(v) else float(v)
        except (KeyError, TypeError, ValueError):
            return None

    def latest_vals(name):
        """최근 non-null 값 목록 반환 — (date, close) 내림차순."""
        return [(d, get_close(name, d)) for d in reversed(dates_sorted)
                if get_close(name, d) is not None]

    result_indices = {}
    result_macro   = {}

    for _, row in all_names_df.iterrows():
        name = row["name"]
        cat  = row["category"]

        vals = latest_vals(name)
        if not vals:
            continue

        cur_date, cur_val   = vals[0]
        _, prev_val         = vals[1]  if len(vals) > 1  else (None, None)
        _, val_1w           = vals[5]  if len(vals) > 5  else (None, None)
        _, val_1m           = vals[21] if len(vals) > 21 else (None, None)

        chg    = compute_chg_pct(cur_val, prev_val)
        chg_1w = compute_chg_pct(cur_val, val_1w)
        chg_1m = compute_chg_pct(cur_val, val_1m)
        fl     = flag(chg, cat)

        entry = {
            "close":    round(cur_val, 4) if cur_val is not None else None,
            "prev":     round(prev_val, 4) if prev_val is not None else None,
            "date":     cur_date,
            "chg_pct":  chg,
            "chg_1w":   chg_1w,
            "chg_1m":   chg_1m,
            "flag":     fl,
            "category": cat,
        }

        if cat == "index":
            result_indices[name] = entry
        else:
            result_macro[name] = entry

    # 경제지표 / 어닝 캘린더 — US 세션 전용 (이번주 월요일 ~ 다음주 일요일)
    econ_upcoming: list[dict] = []
    earnings_upcoming: list[dict] = []
    if session == "us":
        today_dt = date.fromisoformat(today)
        week_start = today_dt - timedelta(days=today_dt.weekday())   # 이번주 월요일
        next_sunday = week_start + timedelta(days=13)                # 다음주 일요일
        span_days = (next_sunday - week_start).days                  # 13

        econ_df = db.get_upcoming_events(week_start.isoformat(), days=span_days)
        if not econ_df.empty:
            econ_upcoming = econ_df.to_dict("records")

        try:
            e_df = db.get_upcoming_earnings(week_start.isoformat(), days=span_days)
            # us_stocks_daily에 저장된 SPX 유니버스로 필터링
            if not e_df.empty:
                try:
                    conn = db._connect()
                    spx_rows = conn.execute(
                        "SELECT DISTINCT ticker FROM us_stocks_daily"
                    ).fetchall()
                    conn.close()
                    spx_set = {r[0] for r in spx_rows}
                    if spx_set:
                        e_df = e_df[e_df["symbol"].isin(spx_set)]
                except Exception:
                    pass
                earnings_upcoming = e_df.to_dict("records")
        except Exception:
            earnings_upcoming = []

    return {
        "date":              today,
        "session":           session,
        "indices":           result_indices,
        "macro":             result_macro,
        "econ_upcoming":     econ_upcoming,
        "earnings_upcoming": earnings_upcoming,
        "stocks":            stocks_data or {},
    }


def format_snapshot_text(snapshot: dict) -> str:
    """스냅샷 딕셔너리를 사람이 읽기 좋은 텍스트로 변환 (LLM 프롬프트 입력용)."""
    lines = []
    lines.append(f"=== {snapshot['date']} {snapshot['session'].upper()} 세션 시장 데이터 ===\n")

    # 지수 — 통합 표 (지수 | 종가 | 1D% | 1W% | 1M%)
    today = snapshot["date"]
    if snapshot["indices"]:
        lines.append("[주요 지수]")
        lines.append("| 지수 | 종가 | 1D% | 1W% | 1M% |")
        lines.append("|------|-----:|----:|----:|----:|")
        for name, v in snapshot["indices"].items():
            close_s = f"{v['close']:.2f}" if v["close"] is not None else "N/A"
            d1 = f"{v['chg_pct']:+.2f}%" if v.get("chg_pct") is not None else "-"
            w1 = f"{v['chg_1w']:+.2f}%" if v.get("chg_1w") is not None else "-"
            m1 = f"{v['chg_1m']:+.2f}%" if v.get("chg_1m") is not None else "-"
            fl = v.get("flag", "")
            name_tag = f"{name}{fl}"
            if v.get("date") != today:
                name_tag = f"{name_tag} (전일:{v['date']})"
            lines.append(f"| {name_tag} | {close_s} | {d1} | {w1} | {m1} |")

    # 매크로 — 통합 표 (분류 | 지표 | 종가 | 1D% | 1W% | 1M%)
    cat_label = {"fx": "FX", "rate": "금리", "commodity": "원자재", "volatility": "변동성"}
    macro_rows = []
    for cat in ("fx", "rate", "commodity", "volatility"):
        items = {k: v for k, v in snapshot["macro"].items() if v["category"] == cat}
        for name, v in items.items():
            macro_rows.append((cat_label.get(cat, cat.upper()), name, v))

    if macro_rows:
        lines.append("\n[매크로]")
        lines.append("| 분류 | 지표 | 종가 | 1D% | 1W% | 1M% |")
        lines.append("|------|------|-----:|----:|----:|----:|")
        for label, name, v in macro_rows:
            close_s = f"{v['close']:.4f}" if v["close"] is not None else "N/A"
            d1 = f"{v['chg_pct']:+.2f}%" if v.get("chg_pct") is not None else "-"
            w1 = f"{v['chg_1w']:+.2f}%"  if v.get("chg_1w")  is not None else "-"
            m1 = f"{v['chg_1m']:+.2f}%"  if v.get("chg_1m")  is not None else "-"
            fl = v.get("flag", "")
            lines.append(f"| {label} | {name}{fl} | {close_s} | {d1} | {w1} | {m1} |")

    # 주요 종목 / 특징주
    stocks = snapshot.get("stocks", {})
    if stocks.get("major"):
        lines.append("\n[KOSPI/KOSDAQ 주요 종목 (시총+거래대금 기준)]")
        for s in stocks["major"]:
            chg = f"{s['chg_pct']:+.2f}%"
            val = f"{s['trade_val']/1e8:.0f}억"
            cap = f"{s['mktcap']/1e12:.1f}조"
            lines.append(f"  {s['name']:12s}  {s['market']:6s}  {s['close']:>8,}  {chg:>7s}  거래대금 {val}  시총 {cap}")

    if stocks.get("featured"):
        lines.append("\n[KOSPI/KOSDAQ 특징주 (등락률+거래량급증+수급)]")
        for s in stocks["featured"]:
            chg = f"{s['chg_pct']:+.2f}%"
            fn  = f"외인 {s['foreign_net']//100_000_000:+,}억" if s.get("foreign_net") is not None else "외인 -"
            it  = f"기관 {s['inst_net']//100_000_000:+,}억"    if s.get("inst_net")    is not None else "기관 -"
            lines.append(f"  {s['name']:12s}  {chg:>7s}  {fn}  {it}  [{s['signal']}]")

    # 경제지표 (US 전용 — 이번주 + 다음주, 마크다운 표)
    if snapshot["econ_upcoming"]:
        lines.append("\n[주요 경제지표 — 이번주·다음주]")
        lines.append("| 날짜 | 시간(ET) | 중요도 | 지표 | 기간 | 직전치 | 예상치 | 현재치 |")
        lines.append("|------|----------|--------|------|------|-------:|-------:|-------:|")
        imp_icon = {"high": "★★★", "medium": "★★☆", "low": "★☆☆"}
        for ev in sorted(snapshot["econ_upcoming"], key=lambda x: x["event_date"]):
            imp     = imp_icon.get(ev.get("importance", "medium"), "★★☆")
            t_str   = ev.get("event_time") or "-"
            period  = ev.get("period") or "-"
            prev    = f"{ev['previous']}" if ev.get("previous") is not None else "-"
            fc      = f"{ev['forecast']}" if ev.get("forecast")  is not None else "-"
            act     = f"**{ev['actual']}**" if ev.get("actual") is not None else "-"
            lines.append(
                f"| {ev['event_date']} | {t_str} | {imp} | {ev['indicator']} "
                f"| {period} | {prev} | {fc} | {act} |"
            )

    # 어닝 캘린더 (US 전용 — 이번주 + 다음주, 마크다운 표)
    earnings = snapshot.get("earnings_upcoming", [])
    if earnings:
        lines.append("\n[SPX 기업실적 — 이번주·다음주]")
        lines.append("| 날짜 | 종목 | 장전/후 | 분기 | EPS예상 | EPS실적 | 서프라이즈 |")
        lines.append("|------|------|---------|------|--------:|--------:|-----------:|")
        hour_label = {"bmo": "장전", "amc": "장후", "dmh": "장중"}
        for ev in sorted(earnings, key=lambda x: (x["event_date"], x.get("hour") or "")):
            hr   = hour_label.get((ev.get("hour") or "").lower(), "-")
            yq   = f"{ev.get('year') or '-'}Q{ev.get('quarter') or '-'}"
            _est = ev.get("eps_estimate")
            _act = ev.get("eps_actual")
            _sur = ev.get("surprise_pct")
            est  = f"{_est:+.2f}" if _est is not None and pd.notna(_est) else "-"
            act  = f"**{_act:+.2f}**" if _act is not None and pd.notna(_act) else "-"
            surp_str = f"**{_sur:+.1f}%**" if _sur is not None and pd.notna(_sur) else "-"
            lines.append(
                f"| {ev['event_date']} | {ev['symbol']} | {hr} | {yq} "
                f"| {est} | {act} | {surp_str} |"
            )

    return "\n".join(lines)


if __name__ == "__main__":
    import json
    snap = build_snapshot("us")
    print(format_snapshot_text(snap))
