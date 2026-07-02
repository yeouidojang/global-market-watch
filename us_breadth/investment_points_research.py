"""
S&P 500 스크리닝 종목 투자포인트 자동 조사
────────────────────────────────────────────
  · sp500_screen_YYYYMMDD.xlsx (EPS_3달변화율 > 0 종목)을 로드
  · Claude API + web_search 도구로 종목당 투자포인트 자동 조사 (배치 5개)
  · 결과를 sp500_investment_points_YYYYMMDD.xlsx 로 저장 후 Slack 발송

출력 시트:
  투자포인트   — 전체 종목 (EPS_3달변화율 내림차순)
  섹터요약     — 섹터별 집계
  섹터테마     — 대표 테마 분류
"""

import json
import os
import re
import sys
import time
from pathlib import Path
from datetime import datetime

import anthropic
import pandas as pd
from dotenv import load_dotenv
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

BASE_DIR = Path("/home/quant/global-market-watch")
load_dotenv(BASE_DIR / ".env")
sys.path.insert(0, str(BASE_DIR))

OUT_DIR = Path("/home/quant/us-market-analysis/output")
TODAY   = datetime.today().strftime("%Y-%m-%d")

RESEARCH_MODEL  = "claude-haiku-4-5-20251001"
BATCH_SIZE      = 3       # 배치당 종목 수 (웹 검색 횟수 제한 고려)
MAX_SEARCH_USES = 6       # 배치당 최대 웹 검색 횟수 (종목당 2회)
RETRY_DELAY     = 5       # API 재시도 대기(초)

EPS_3M_THRESHOLD = 0.0    # EPS 3달 변화율 필터 기준


# ── 1. 스크리닝 데이터 로드 ───────────────────────────────────────────────────
def load_screened(date_str: str) -> pd.DataFrame:
    path = OUT_DIR / f"sp500_screen_{date_str.replace('-','')}.xlsx"
    if not path.exists():
        raise FileNotFoundError(f"스크리닝 파일 없음: {path}")
    df = pd.read_excel(str(path), sheet_name="필터링종목")
    df = df[df["EPS_3달변화율(%)"] > EPS_3M_THRESHOLD].copy()
    df = df.sort_values("EPS_3달변화율(%)", ascending=False).reset_index(drop=True)
    print(f"[로드] {len(df)}개 종목 (EPS 3달 > {EPS_3M_THRESHOLD}%)")
    return df


# ── 2. Claude API 배치 조사 ───────────────────────────────────────────────────
_RESEARCH_PROMPT = """다음 {n}개 미국 S&P 500 종목의 최신 투자포인트를 조사해줘.
웹 검색으로 2026년 최신 정보를 확인하고 아래 JSON 형식으로만 답해줘.

조사할 종목:
{stock_list}

규칙:
- 검색 정보가 부족하더라도 반드시 {n}개 종목 모두 포함한 JSON 배열을 반환해야 함
- 정보가 부족한 종목은 알고 있는 일반 지식으로 작성
- 코드블록(```), 설명 텍스트 없이 JSON 배열만 반환

반환 형식:
[
  {{
    "ticker": "티커",
    "EPS상향이유": "최근 EPS 추정치 상향 핵심 이유 (실적·가이던스·사업 모멘텀 등, 2~3문장)",
    "투자포인트": "① 포인트1 ② 포인트2 ③ 포인트3 (각 1~2문장)",
    "주요리스크": "핵심 리스크 1가지 (1~2문장)"
  }}
]"""


def _extract_json(text: str) -> list:
    """Claude 응답에서 JSON 배열 추출. 부분 응답도 최대한 복구."""
    text = text.strip()
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```\s*$", "", text, flags=re.MULTILINE)

    # 완전한 JSON 배열 시도
    start = text.find("[")
    end   = text.rfind("]") + 1
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError:
            pass

    # 개별 JSON 객체 추출 (배열이 깨진 경우)
    results = []
    for m in re.finditer(r'\{[^{}]*"ticker"\s*:\s*"(\w+)"[^{}]*\}', text, re.DOTALL):
        try:
            obj = json.loads(m.group(0))
            results.append(obj)
        except json.JSONDecodeError:
            pass
    if results:
        return results

    raise ValueError(f"JSON 배열을 찾을 수 없음: {text[:300]}")


def _research_batch(
    client: anthropic.Anthropic,
    batch: list[dict],
) -> list[dict]:
    """배치(최대 BATCH_SIZE개) 종목 투자포인트 조사."""
    stock_list = "\n".join(
        f"- {s['ticker']} ({s['name']}, {s['sector']}, EPS 3달 {s['eps_3m']:+.2f}%)"
        for s in batch
    )
    prompt = _RESEARCH_PROMPT.format(n=len(batch), stock_list=stock_list)

    for attempt in range(3):
        try:
            msg = client.beta.messages.create(
                model=RESEARCH_MODEL,
                max_tokens=2000,
                betas=["web-search-2025-03-05"],
                tools=[{
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": MAX_SEARCH_USES,
                }],
                messages=[{"role": "user", "content": prompt}],
            )
            # 텍스트 블록만 추출
            text_parts = [b.text for b in msg.content if hasattr(b, "text")]
            full_text  = "\n".join(text_parts)
            results    = _extract_json(full_text)
            # ticker 기준으로 매핑 검증
            found = {r["ticker"] for r in results}
            expected = {s["ticker"] for s in batch}
            missing = expected - found
            if missing:
                print(f"  [WARN] 응답 누락 ticker: {missing}")
            return results

        except (json.JSONDecodeError, ValueError) as e:
            print(f"  [파싱 오류 attempt={attempt+1}] {e}")
            if attempt < 2:
                time.sleep(RETRY_DELAY)
        except anthropic.APIError as e:
            print(f"  [API 오류 attempt={attempt+1}] {e}")
            if attempt < 2:
                time.sleep(RETRY_DELAY * 2)

    # 실패 시 빈 결과 반환
    return [
        {"ticker": s["ticker"], "EPS상향이유": "조사 실패", "투자포인트": "-", "주요리스크": "-"}
        for s in batch
    ]


def research_all(df: pd.DataFrame) -> pd.DataFrame:
    """전체 종목 투자포인트 조사 (배치 처리)."""
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    batches = []
    stocks  = [
        {
            "ticker":  row["ticker"],
            "name":    row.get("종목명", ""),
            "sector":  row.get("섹터", ""),
            "eps_3m":  row.get("EPS_3달변화율(%)", 0),
        }
        for _, row in df.iterrows()
    ]

    for i in range(0, len(stocks), BATCH_SIZE):
        batches.append(stocks[i: i + BATCH_SIZE])

    all_results: list[dict] = []
    for idx, batch in enumerate(batches, 1):
        tickers = [s["ticker"] for s in batch]
        print(f"  배치 {idx}/{len(batches)}: {tickers}")
        results = _research_batch(client, batch)
        all_results.extend(results)
        if idx < len(batches):
            time.sleep(1)   # API 레이트 리밋 방지

    return pd.DataFrame(all_results)


# ── 3. 섹터 요약 ─────────────────────────────────────────────────────────────
def build_sector_summary(df: pd.DataFrame) -> pd.DataFrame:
    summary = df.groupby("섹터").agg(
        종목수              = ("ticker", "count"),
        EPS_3달평균        = ("EPS_3달변화율(%)", "mean"),
        EPS_1달평균        = ("EPS_1달변화율(%)", "mean"),
        _1주수익률평균     = ("1주_수익률(%)", "mean"),
        MA20이격도평균     = ("MA20_이격도(%)", "mean"),
        거래대금비율평균   = ("거래대금_비율", "mean"),
    ).round(2).reset_index()
    summary.rename(columns={"_1주수익률평균": "1주수익률평균"}, inplace=True)
    return summary.sort_values("EPS_3달평균", ascending=False)


def build_sector_themes(df: pd.DataFrame) -> pd.DataFrame:
    """섹터/테마 분류 자동 생성."""
    theme_map = {
        "Energy":                    ("정제·에너지 마진 사이클",     "정제 마진 강세 및 LNG·천연가스 인프라 수요 급증 수혜"),
        "Utilities":                 ("AI 데이터센터 전력 인프라",  "AI 서버팜 전력 수요 구조적 성장으로 전력 인프라 중장기 수혜"),
        "Financials":                ("금융 수익성 회복",           "보험 손해율 개선·NII 회복·수수료 수익 다각화로 이익 반등"),
        "Industrials":               ("산업·방산·항공 가이던스 상향","실적 서프라이즈 + 연간 가이던스 상향으로 EPS 추정치 개선 모멘텀"),
        "Health Care":               ("헬스케어·생명과학 재건",     "의료비 관리 개선·전문약 유통 확대·바이오프로세싱 사이클 반등"),
        "Information Technology":   ("IT·방산 디지털 성장",         "방산·우주·산업 자동화 디지털 장비 수주 급증"),
        "Consumer Discretionary":   ("소비재 브랜드 가격전가력",    "관세 역풍에도 프리미엄 브랜드 파워로 마진 방어"),
        "Consumer Staples":         ("방어적 소비재·고배당",        "필수 소비재 수요 안정성 + 가격 인상력 + 배당 성장"),
        "Real Estate":              ("리츠·임대 자산 회복",         "임대율·임대료 개선 및 AI 데이터센터 인접 자산 리레이팅"),
        "Materials":                ("소재·원자재 사이클 수혜",     "글로벌 인프라 투자 확대 및 원자재 수요 회복"),
        "Communication Services":   ("플랫폼·통신 성장",           "디지털 광고·스트리밍·데이터 수익 확대"),
    }

    rows = []
    for sector, (theme, desc) in theme_map.items():
        sub = df[df["섹터"] == sector]
        if sub.empty:
            continue
        tickers = ", ".join(sub["ticker"].tolist())
        rows.append({
            "주요테마":  theme,
            "섹터":      sector,
            "종목":      tickers,
            "종목수":    len(sub),
            "설명":      desc,
        })
    return pd.DataFrame(rows)


# ── 4. Excel 저장 ─────────────────────────────────────────────────────────────
def _col_width(ws, df: pd.DataFrame):
    for i, col in enumerate(df.columns, 1):
        try:
            w = max(df[col].astype(str).map(len).max(), len(str(col))) + 2
        except Exception:
            w = len(str(col)) + 2
        ws.column_dimensions[get_column_letter(i)].width = min(max(w, 10), 60)


def _style_header(ws, fill_hex="1F4E79"):
    fill  = PatternFill("solid", fgColor=fill_hex)
    font  = Font(color="FFFFFF", bold=True)
    align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for cell in ws[1]:
        cell.fill = fill
        cell.font = font
        cell.alignment = align
    ws.row_dimensions[1].height = 30


def _wrap_cols(ws, df: pd.DataFrame, col_names: list[str], width: int = 55):
    for col_name in col_names:
        if col_name not in df.columns:
            continue
        idx = df.columns.tolist().index(col_name) + 1
        ws.column_dimensions[get_column_letter(idx)].width = width
        for row in ws.iter_rows(min_row=2, min_col=idx, max_col=idx):
            for cell in row:
                cell.alignment = Alignment(wrap_text=True, vertical="top")


def save_excel(
    merged:         pd.DataFrame,
    sector_summary: pd.DataFrame,
    sector_themes:  pd.DataFrame,
    date_str:       str,
) -> Path:
    fname = f"sp500_investment_points_{date_str.replace('-','')}.xlsx"
    fpath = OUT_DIR / fname

    display_cols = [
        "ticker", "종목명", "섹터", "세부업종",
        "현재가($)", "거래대금_비율",
        "최근5일_평균거래대금($M)", "직전4주_평균거래대금($M)",
        "1일_수익률(%)", "1주_수익률(%)", "1개월_수익률(%)", "YTD_수익률(%)", "MA20_이격도(%)",
        "EPS_1주변화율(%)", "EPS_1달변화율(%)", "EPS_3달변화율(%)",
        "종합점수",
        "EPS상향이유", "투자포인트", "주요리스크",
    ]
    display_cols = [c for c in display_cols if c in merged.columns]
    out = merged[display_cols]

    with pd.ExcelWriter(str(fpath), engine="openpyxl") as writer:
        out.to_excel(writer, sheet_name="투자포인트", index=False)
        ws1 = writer.sheets["투자포인트"]
        _col_width(ws1, out)
        _style_header(ws1)
        _wrap_cols(ws1, out, ["EPS상향이유", "투자포인트", "주요리스크"])
        ws1.freeze_panes = "E2"

        sector_summary.to_excel(writer, sheet_name="섹터요약", index=False)
        ws2 = writer.sheets["섹터요약"]
        _col_width(ws2, sector_summary)
        _style_header(ws2, "2E75B6")

        sector_themes.to_excel(writer, sheet_name="섹터테마", index=False)
        ws3 = writer.sheets["섹터테마"]
        _col_width(ws3, sector_themes)
        _style_header(ws3, "375623")
        _wrap_cols(ws3, sector_themes, ["종목", "설명"], width=45)

    print(f"[저장] {fpath}  ({len(out)}개 종목)")
    return fpath


# ── 5. Slack 발송 ─────────────────────────────────────────────────────────────
def send_slack(fpath: Path, merged: pd.DataFrame, sector_summary: pd.DataFrame, date_str: str):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from notify_slack_file import upload_file

    top3 = sector_summary.head(3)
    sector_lines = " | ".join(
        f"{row['섹터']}({int(row['종목수'])}개, EPS 3달 평균 +{row['EPS_3달평균']:.1f}%)"
        for _, row in top3.iterrows()
    )

    comment = (
        f"*S&P 500 스크리닝 — EPS 상향 종목 투자포인트* ({date_str})\n"
        f"3M EPS 상승률 > 0 조건 통과: *{len(merged)}개 종목*\n"
        f"상위 섹터: {sector_lines}\n"
        f"📊 시트: ① 투자포인트 ② 섹터요약 ③ 섹터테마"
    )
    upload_file(
        file_path=fpath,
        title=f"S&P500_투자포인트_{date_str}",
        comment=comment,
    )


# ── MAIN ──────────────────────────────────────────────────────────────────────
def run(date_str: str = TODAY) -> Path:
    global TODAY
    TODAY = date_str
    print(f"\n{'='*60}")
    print(f"  S&P500 투자포인트 자동 조사")
    print(f"  기준일: {date_str}")
    print(f"{'='*60}\n")

    print("▶ 스크리닝 데이터 로드")
    df = load_screened(date_str)

    print(f"\n▶ Claude API 투자포인트 조사 ({len(df)}개 종목, 배치={BATCH_SIZE})")
    research_df = research_all(df)

    print("\n▶ 데이터 병합")
    merged = df.merge(research_df, on="ticker", how="left")

    print("\n▶ 섹터 집계")
    sector_summary = build_sector_summary(merged)
    sector_themes  = build_sector_themes(merged)
    print(f"  섹터: {len(sector_summary)}개  테마: {len(sector_themes)}개")

    print("\n▶ Excel 저장")
    fpath = save_excel(merged, sector_summary, sector_themes, date_str)

    print("\n▶ Slack 발송")
    send_slack(fpath, merged, sector_summary, date_str)

    return fpath
