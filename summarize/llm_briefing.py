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
이모지는 🚨(이상 변동 경고)만 사용합니다. 불필요한 수식어는 생략합니다."""


def _fmt_stocks_asia(stocks_data: dict) -> str:
    """Asia 종목 테이블 텍스트 생성."""
    lines = []
    major = stocks_data.get("major", [])
    featured = stocks_data.get("featured", [])

    if major:
        lines.append("\n**KOSPI/KOSDAQ 주요 종목** (시총+거래대금 기준 TOP 10)")
        lines.append("| 종목 | 시장 | 종가 | 등락률 | 거래대금(억) |")
        lines.append("|------|------|-----:|-------:|-------------:|")
        for s in major:
            tv = f"{s['trade_val']//100_000_000:,}" if s.get("trade_val") else "-"
            lines.append(f"| {s['name']} | {s.get('market','')} | {s['close']:,} | {s['chg_pct']:+.2f}% | {tv} |")

    if featured:
        lines.append("\n**KOSPI/KOSDAQ 특징주** (등락률+거래량급증 기준 TOP 10)")
        lines.append("| 종목 | 시장 | 종가 | 등락률 | 시그널 |")
        lines.append("|------|------|-----:|-------:|--------|")
        for s in featured:
            lines.append(f"| {s['name']} | {s.get('market','')} | {s['close']:,} | {s['chg_pct']:+.2f}% | {s.get('signal','')} |")

    return "\n".join(lines)


def _fmt_stocks_europe(stocks_data: dict) -> str:
    """Europe 섹터+종목 테이블 텍스트 생성."""
    lines = []
    sectors = stocks_data.get("sectors", [])
    top_stocks = stocks_data.get("top_stocks", [])

    if sectors:
        lines.append("\n**STOXX600 섹터 성과**")
        lines.append("| 섹터 | 종가 | 등락률 |")
        lines.append("|------|-----:|-------:|")
        for s in sectors:
            chg = f"{s['chg_pct']:+.2f}%" if s.get("chg_pct") is not None else "-"
            lines.append(f"| {s['name']} | {s['close']:,.2f} | {chg} |")

    if top_stocks:
        lines.append("\n**유럽 주요 종목** (DAX·FTSE·CAC 대형주)")
        lines.append("| 종목 | 종가 | 등락률 |")
        lines.append("|------|-----:|-------:|")
        for s in top_stocks[:10]:
            chg = f"{s['chg_pct']:+.2f}%" if s.get("chg_pct") is not None else "-"
            lines.append(f"| {s['name']} | {s['close']:,.2f} | {chg} |")

    return "\n".join(lines)


def _fmt_stocks_us(stocks_data: dict) -> str:
    """US 스크리닝 결과 테이블 텍스트 생성."""
    lines = []
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
        lines.append("\n**EPS 추정치 변화 (1W)**")
        lines.append("| 종목 | 종가 | 등락률 | EPS변화(1W) | 시그널 |")
        lines.append("|------|-----:|-------:|------------:|--------|")
        for s in eps_rev:
            chg = f"{s['chg_pct']:+.2f}%" if s.get("chg_pct") is not None else "-"
            eps = f"{s['eps_chg_1w']:+.1f}%" if s.get("eps_chg_1w") is not None else "-"
            lines.append(f"| {s['ticker']} | {s['close']:,.2f} | {chg} | {eps} | {s.get('signal','')} |")

    return "\n".join(lines)


def build_prompt(session: str, snapshot_text: str, stocks_data: dict = None) -> str:
    context = SESSION_CONTEXT.get(session, "")
    stocks_data = stocks_data or {}

    if session == "asia":
        stocks_section = _fmt_stocks_asia(stocks_data)
        session_format = """
## ASIA 시황 브리핑

**핵심 요약** (2~3문장)

**주요 지수 동향** (한국·일본·중국·홍콩)

**매크로 (FX·금리·원자재)**

{stocks}

**향후 주목 이벤트** (3일 이내 경제지표·이벤트)

**운용 포인트** (1~2문장, 한국 포지션 관점)
""".format(stocks=stocks_section)

    elif session == "europe":
        stocks_section = _fmt_stocks_europe(stocks_data)
        session_format = """
## EUROPE 시황 브리핑

**핵심 요약** (2~3문장)

**주요 지수 동향** (DAX·FTSE100·CAC40·EuroStoxx50)

**섹터 분석** (강세/약세 섹터 2~3개 집중)

**매크로 (EUR/USD·유로금리·에너지)**

{stocks}

**향후 주목 이벤트** (ECB·PMI·CPI 발표 등)

**운용 포인트** (1~2문장, 유럽 익스포저 관점)
""".format(stocks=stocks_section)

    else:  # us — 글로벌 통합 브리핑
        stocks_section = _fmt_stocks_us(stocks_data)
        has_prior = bool(stocks_data.get("prior_briefings"))
        session_format = f"""
## US / 글로벌 통합 시황 브리핑

**핵심 요약** (3~4문장, 글로벌 전체 흐름)

**미국 지수 동향** (SPX·NDX·DJIA·Russell2000 + 마감 추이)

**섹터 분석** (강세/약세 섹터 Top3 언급)

**매크로 (달러·금리·VIX·원자재)**

{{stocks}}

{'**아시아·유럽 당일 주요 이슈** (앞서 브리핑 기반)' if has_prior else ''}

**향후 주목 이벤트** (FOMC·고용·CPI 등)

**글로벌 운용 포인트** (2~3문장, 지역별 포지션 관점)
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

    print(f"[llm_briefing] 스냅샷 생성 중... (session={session}, date={today})")
    snapshot = build_snapshot(session, today, stocks_data=stocks_data)
    snapshot_text = format_snapshot_text(snapshot)

    print("[llm_briefing] Claude API 호출 중...")
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    has_stocks = bool(stocks_data and any(stocks_data.get(k) for k in ("major","featured","sectors","top_stocks")))
    message = client.messages.create(
        model=MODEL,
        max_tokens=2048,
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
