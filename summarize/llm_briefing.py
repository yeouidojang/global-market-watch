"""
Claude API를 이용한 시황 브리핑 생성

사용법:
    python llm_briefing.py --session us
    python llm_briefing.py --session asia --date 2026-06-10 --no-save
"""

import os
import sys
import argparse
from datetime import date
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR.parent / ".env")
sys.path.insert(0, str(BASE_DIR))

import anthropic

from summarize.build_snapshot import build_snapshot, format_snapshot_text
from db.db_manager import DBManager

MODEL = "claude-opus-4-5"

SESSION_CONTEXT = {
    "asia":   "한국·일본·중국 중심 아시아 시장 마감 직후 브리핑입니다.",
    "europe": "유럽 시장 마감 직후 브리핑입니다.",
    "us":     "미국 시장 마감 직후 전일 글로벌 통합 브리핑입니다.",
}

SYSTEM_PROMPT = """당신은 주식 운용 팀을 위한 시황 브리핑 작성 전문가입니다.
데이터를 근거로 핵심 움직임과 운용 관점의 시사점을 간결하게 전달합니다.
이모지는 🚨(이상 변동 경고)만 사용합니다. 불필요한 수식어는 생략합니다.

출력 형식 규칙:
- 지수·종목·섹터·매크로 수치는 반드시 마크다운 표(| 컬럼 | 컬럼 |) 형식으로 작성합니다.
- 표 컬럼: 숫자는 우측 정렬(--:), 텍스트는 좌측 정렬(--).
- 각 섹션은 ## 헤더로 시작합니다. 섹션 사이에는 --- 구분선을 넣습니다.
- 핵심 요약·운용 포인트는 표 없이 문장으로 작성합니다."""


def _fmt_market_structure(session: str, stocks_data: dict) -> str:
    """시장 폭 + 전체 수급 요약 한 줄 텍스트 (LLM 컨텍스트 주입용)."""
    breadth     = stocks_data.get("breadth", {})
    market_flow = stocks_data.get("market_flow", {})
    parts = []

    if breadth:
        label = {"asia": "KOSPI+KOSDAQ", "europe": "DAX/FTSE/CAC", "us": "SPX"}.get(session, "SPX")
        up    = breadth.get("up", 0)
        down  = breadth.get("down", 0)
        total = breadth.get("total", 0)
        up_pct = breadth.get("up_pct")
        adr    = breadth.get("adr")
        w_chg  = breadth.get("weighted_chg")

        row = f"**{label} 시장 폭**  상승 {up}/{total}"
        if up_pct is not None:
            row += f"({up_pct:.1f}%)"
        row += f"  하락 {down}/{total}"
        if adr is not None:
            row += f"  ADR {adr:.2f}"
        if w_chg is not None:
            row += f"  거래대금가중등락 {w_chg:+.2f}%"
        parts.append(row)

    if session == "asia" and market_flow:
        fn = market_flow.get("foreign_net")
        it = market_flow.get("inst_net")
        fn_str = f"{fn//100_000_000:+,}억" if fn is not None else "-"
        it_str = f"{it//100_000_000:+,}억" if it is not None else "-"
        parts.append(f"**시장 전체 수급**  외국인 {fn_str}  기관 {it_str}")

    return "\n".join(parts)


def _fmt_stocks_asia(stocks_data: dict) -> str:
    """Asia 종목 테이블 텍스트 생성."""
    lines = []
    ms = _fmt_market_structure("asia", stocks_data)
    if ms:
        lines.append(ms)
        lines.append("")
    major = stocks_data.get("major", [])
    featured = stocks_data.get("featured", [])

    if major:
        lines.append("\n## KOSPI/KOSDAQ 주요 종목 (시총+거래대금 기준 TOP 10)")
        lines.append("| 종목 | 시장 | 종가 | 등락률 | 거래대금(억) |")
        lines.append("|------|------|-----:|-------:|-------------:|")
        for s in major:
            tv = f"{s['trade_val']//100_000_000:,}" if s.get("trade_val") else "-"
            lines.append(f"| {s['name']} | {s.get('market','')} | {s['close']:,} | {s['chg_pct']:+.2f}% | {tv} |")

    if featured:
        lines.append("\n## KOSPI/KOSDAQ 특징주 (등락률+거래량급증+수급 기준 TOP 10)")
        lines.append("| 종목 | 등락률 | 외인(억) | 기관(억) | 시그널 |")
        lines.append("|------|-------:|---------:|---------:|--------|")
        for s in featured:
            fn  = f"{s['foreign_net']//100_000_000:+,}" if s.get("foreign_net") is not None else "-"
            it  = f"{s['inst_net']//100_000_000:+,}"    if s.get("inst_net")    is not None else "-"
            lines.append(f"| {s['name']} | {s['chg_pct']:+.2f}% | {fn} | {it} | {s.get('signal','')} |")

    return "\n".join(lines)


def _fmt_stocks_europe(stocks_data: dict) -> str:
    """Europe 섹터+종목 테이블 텍스트 생성."""
    lines = []
    ms = _fmt_market_structure("europe", stocks_data)
    if ms:
        lines.append(ms)
        lines.append("")

    sectors    = stocks_data.get("sectors", [])
    top_stocks = stocks_data.get("top_stocks", [])

    if sectors:
        lines.append("\n**STOXX600 섹터 성과**")
        lines.append("| 섹터 | 종가 | 등락률 |")
        lines.append("|------|-----:|-------:|")
        for s in sectors:
            chg = f"{s['chg_pct']:+.2f}%" if s.get("chg_pct") is not None else "-"
            lines.append(f"| {s['name']} | {s['close']:,.2f} | {chg} |")

    if top_stocks:
        lines.append("\n**유럽 주요 종목** (DAX·FTSE·CAC 등락상위 15)")
        lines.append("| 종목 | 지수 | 종가 | 등락률 |")
        lines.append("|------|------|-----:|-------:|")
        for s in top_stocks[:15]:
            chg = f"{s['chg_pct']:+.2f}%" if s.get("chg_pct") is not None else "-"
            idx = s.get("index", "")
            lines.append(f"| {s['name']} | {idx} | {s['close']:,.2f} | {chg} |")

    return "\n".join(lines)


def _fmt_stocks_us(stocks_data: dict) -> str:
    """US 스크리닝 결과 테이블 텍스트 생성."""
    lines = []
    ms = _fmt_market_structure("us", stocks_data)
    if ms:
        lines.append(ms)
        lines.append("")
    prior    = stocks_data.get("prior_briefings", {})
    sectors  = stocks_data.get("sectors", [])
    mktcap   = stocks_data.get("mktcap_top", [])
    tradeval = stocks_data.get("tradeval_top", [])
    surge    = stocks_data.get("turnover_surge", [])
    eps_rev  = stocks_data.get("eps_revision", [])

    if prior:
        lines.append("\n**당일 아시아·유럽 시황 요약**")
        for sess, text in prior.items():
            lines.append(f"\n[{sess.upper()}] {text[:300]}...")

    if sectors:
        lines.append("\n**SPX 섹터 ETF 성과**")
        lines.append("| 섹터 | ETF | 종가 | 등락률 |")
        lines.append("|------|-----|-----:|-------:|")
        for s in sectors:
            chg = f"{s['chg_pct']:+.2f}%" if s.get("chg_pct") is not None else "-"
            lines.append(f"| {s['name']} | {s['ticker']} | {s['close']:,.2f} | {chg} |")

    if mktcap:
        lines.append("\n**시총 상위** (Large Cap)")
        lines.append("| 종목 | 종가 | 등락률 | 시총($B) | EPS추정변화(1W) |")
        lines.append("|------|-----:|-------:|---------:|---------------:|")
        for s in mktcap[:10]:
            chg  = f"{s['chg_pct']:+.2f}%" if s.get("chg_pct") is not None else "-"
            cap  = f"{s['mktcap_b']:.0f}" if s.get("mktcap_b") else "-"
            eps  = f"{s['eps_chg_1w']:+.1f}%" if s.get("eps_chg_1w") is not None else "-"
            lines.append(f"| {s['ticker']} | {s['close']:,.2f} | {chg} | {cap} | {eps} |")

    if tradeval:
        lines.append("\n**거래대금 상위**")
        lines.append("| 종목 | 종가 | 등락률 | 거래대금($B) |")
        lines.append("|------|-----:|-------:|------------:|")
        for s in tradeval[:10]:
            chg = f"{s['chg_pct']:+.2f}%" if s.get("chg_pct") is not None else "-"
            dv  = f"{s['dollar_vol_b']:.1f}" if s.get("dollar_vol_b") else "-"
            lines.append(f"| {s['ticker']} | {s['close']:,.2f} | {chg} | {dv} |")

    if surge:
        lines.append("\n**거래대금 급증 특징주** (turnover surge ≥ 1.5x + |등락률| ≥ 2%)")
        lines.append("| 종목 | 종가 | 등락률 | 급증배수 | 시그널 |")
        lines.append("|------|-----:|-------:|---------:|--------|")
        for s in surge:
            chg = f"{s['chg_pct']:+.2f}%" if s.get("chg_pct") is not None else "-"
            sr  = f"{s['surge_ratio']:.1f}x" if s.get("surge_ratio") else "-"
            lines.append(f"| {s['ticker']} | {s['close']:,.2f} | {chg} | {sr} | {s.get('signal','')} |")

    if eps_rev:
        lines.append("\n**EPS 추정치 변화**")
        lines.append("| 종목 | 종가 | 당일등락 | EPS변화(1M) | EPS변화(1W) | 7일수익률 | 시그널 |")
        lines.append("|------|-----:|---------:|------------:|------------:|----------:|--------|")
        for s in eps_rev:
            chg   = f"{s['chg_pct']:+.2f}%"   if s.get("chg_pct")   is not None else "-"
            e1m   = f"{s['eps_chg_1m']:+.2f}%" if s.get("eps_chg_1m") is not None else "-"
            e1w   = f"{s['eps_chg_1w']:+.2f}%" if s.get("eps_chg_1w") is not None else "-"
            ret7  = f"{s['return_7d']:+.1f}%"  if s.get("return_7d") is not None else "-"
            lines.append(f"| {s['ticker']} | {s['close']:,.2f} | {chg} | {e1m} | {e1w} | {ret7} | {s.get('signal','')} |")

    return "\n".join(lines)


def build_prompt(session: str, snapshot_text: str, stocks_data: dict = None) -> str:
    context = SESSION_CONTEXT.get(session, "")
    stocks_data = stocks_data or {}

    if session == "asia":
        stocks_section = _fmt_stocks_asia(stocks_data)
        session_format = """
## ASIA 시황 브리핑

**핵심 요약** (2~3문장)

---

## 주요 지수 동향
(한국·일본·중국·홍콩 — 종목명·종가·등락률을 마크다운 표로 작성)

---

## 매크로
(FX·금리·원자재 항목·수치·등락률을 카테고리별 표로 작성)

---

## KOSPI/KOSDAQ 주요 종목 및 특징주
(아래 데이터를 표로 출력. 시장 폭과 전체 수급 방향성을 먼저 1문장으로 요약하고, 특징주는 외인/기관 수급 동반 급등 vs 수급 없는 단순 급등을 구분하여 서술)

{stocks}

---

## 향후 주목 이벤트
(3일 이내 경제지표·이벤트를 날짜·발표시각(ET)·지표명·기준기간·예상치 표로 작성)

---

## 운용 포인트
(1~2문장, 한국 포지션 관점)
""".format(stocks=stocks_section)

    elif session == "europe":
        stocks_section = _fmt_stocks_europe(stocks_data)
        session_format = """
## EUROPE 시황 브리핑

**핵심 요약** (2~3문장)

---

## 주요 지수 동향
(DAX·FTSE100·CAC40·EuroStoxx50 — 표로 작성)

---

## 섹터 분석
(강세/약세 섹터 — 표로 작성, 2~3개 집중. DAX/FTSE/CAC 시장 폭 데이터를 참고해 광범위 상승 vs 소수 집중 여부 1문장 분석)

---

## 매크로
(EUR/USD·유로금리·에너지 — 표로 작성)

---

{stocks}

---

## 향후 주목 이벤트
(ECB·PMI·CPI 발표 등 — 표로 작성)

---

## 운용 포인트
(1~2문장, 유럽 익스포저 관점)
""".format(stocks=stocks_section)

    else:  # us — 글로벌 통합 브리핑
        stocks_section = _fmt_stocks_us(stocks_data)
        has_prior = bool(stocks_data.get("prior_briefings"))
        session_format = f"""
## US / 글로벌 통합 시황 브리핑

**핵심 요약** (3~4문장, 글로벌 전체 흐름)

---

## 미국 지수 동향
(SPX·NDX·DJIA·Russell2000 — 표로 작성)

---

## 섹터 분석
(강세/약세 섹터 — 강세 Top3·약세 Top3를 표로 작성. SPX 시장 폭 데이터를 참고해 광범위 랠리 vs 소수 종목 집중 여부를 1문장 분석)

---

## SPX 섹터 ETF 성과
(스냅샷의 US_Tech·US_Finance·US_Energy 등 전체 섹터 ETF를 종가·등락률 포함해 표로 작성)

---

## 시총 상위 종목
(스냅샷의 Apple·Microsoft·Nvidia·Tesla 등 대형주를 종가·등락률 포함해 표로 작성)

---

## 매크로
(달러·금리·VIX·원자재 — 카테고리별 표로 작성)

---

{{stocks}}

---

{'## 아시아·유럽 당일 주요 이슈' if has_prior else ''}
{'(앞서 브리핑 요약 기반 — 표로 작성)' if has_prior else ''}

{'---' if has_prior else ''}

## 향후 주목 이벤트
(FOMC·고용·CPI 등 — 발표일·지표명·기준기간·예상치를 표로 작성)

---

## 글로벌 운용 포인트
(2~3문장, 지역별 포지션 관점)
""".format(stocks=stocks_section)

    return f"""{context}

아래는 오늘의 시장 데이터입니다.

{snapshot_text}

다음 형식으로 브리핑을 작성하세요.
데이터에 없는 항목은 생략하고, 실제 수치를 근거로 서술하세요.

{session_format}
"""


def generate_briefing(session: str, target_date: str = None,
                      save: bool = True, stocks_data: dict = None) -> str:
    today = target_date or date.today().strftime("%Y-%m-%d")

    # stocks_data 없으면 DB에서 로드 (브리핑 재생성 시 재수집 불필요)
    if not stocks_data:
        db_stocks = DBManager().get_stocks_daily(today, session)
        if db_stocks:
            stocks_data = db_stocks
            print(f"[llm_briefing] stocks_data DB에서 로드 ({session}/{today})")
        # US 세션: prior_briefings 복원
        if session == "us" and stocks_data:
            prior_briefings = {}
            for prior_session in ("asia", "europe"):
                row = DBManager().get_latest_briefing(prior_session)
                if row and row.get("date") == today:
                    prior_briefings[prior_session] = row["content"][:400]
            if prior_briefings:
                stocks_data["prior_briefings"] = prior_briefings

    # 시장 폭/전체 수급: DB fallback (재생성 시 재계산)
    if stocks_data and not stocks_data.get("breadth"):
        b = DBManager().get_market_breadth(today, session)
        if b:
            stocks_data["breadth"] = b
    if session == "asia" and stocks_data and not stocks_data.get("market_flow"):
        mf = DBManager().get_market_flow(today)
        if mf:
            stocks_data["market_flow"] = mf

    print(f"[llm_briefing] 스냅샷 생성 중... (session={session}, date={today})")
    snapshot = build_snapshot(session, today, stocks_data=stocks_data)
    snapshot_text = format_snapshot_text(snapshot)

    print("[llm_briefing] Claude API 호출 중...")
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    has_stocks = bool(stocks_data and any(stocks_data.get(k) for k in ("major","featured","sectors","top_stocks")))
    message = client.messages.create(
        model=MODEL,
        max_tokens=3000,
        system=SYSTEM_PROMPT,
        messages=[
            {"role": "user", "content": build_prompt(session, snapshot_text, stocks_data=stocks_data if has_stocks else {})}
        ],
    )

    content       = message.content[0].text
    prompt_tokens = message.usage.input_tokens
    output_tokens = message.usage.output_tokens

    print(f"[llm_briefing] 완료: {prompt_tokens}+{output_tokens} tokens")

    if save:
        db = DBManager()
        row_id = db.save_briefing(
            date=today,
            session=session,
            content=content,
            model=MODEL,
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
        )
        print(f"[llm_briefing] DB 저장: id={row_id}")

    return content


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True,
                        choices=["asia", "europe", "us"])
    parser.add_argument("--date", default=None)
    parser.add_argument("--no-save", action="store_true",
                        help="DB 저장 없이 터미널 출력만")
    args = parser.parse_args()

    content = generate_briefing(
        session=args.session,
        target_date=args.date,
        save=not args.no_save,
    )
    print("\n" + "=" * 60)
    print(content)


if __name__ == "__main__":
    main()
