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
- 핵심 요약·운용 포인트는 표 없이 문장으로 작성합니다.

시간 순서 엄수:
- us/global 세션 브리핑의 market_date(target_date)는 미국 시장 마감일(한국시간 다음날 새벽 종료)입니다.
- prior_briefings에 포함된 asia/europe 브리핑은 market_date 당일의 아시아·유럽 마감 기록입니다.
  (아시아 → 유럽 → 미국 순으로 마감 → 이 순서로만 참고)
- 아직 발생하지 않은 미래 세션(예: 오늘 오후 아시아 마감)은 절대 언급하거나 예측하지 않습니다."""


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

    # Asia: 시장별 폭/수급 보조 라인
    if session == "asia":
        for mk_key, mk_label in (("kospi", "KOSPI"), ("kosdaq", "KOSDAQ")):
            b  = stocks_data.get(f"breadth_{mk_key}", {})
            mf = stocks_data.get(f"market_flow_{mk_key}", {})
            if not b and not mf:
                continue
            sub = [f"**{mk_label}**"]
            if b:
                up, dn, tot = b.get("up", 0), b.get("down", 0), b.get("total", 0)
                up_pct, adr, wch = b.get("up_pct"), b.get("adr"), b.get("weighted_chg")
                seg = f"폭 {up}/{tot}"
                if up_pct is not None: seg += f"({up_pct:.1f}%)"
                seg += f" vs {dn}/{tot}"
                if adr is not None: seg += f"  ADR {adr:.2f}"
                if wch is not None: seg += f"  가중등락 {wch:+.2f}%"
                sub.append(seg)
            if mf:
                fn, it = mf.get("foreign_net"), mf.get("inst_net")
                fn_str = f"{fn//100_000_000:+,}억" if fn is not None else "-"
                it_str = f"{it//100_000_000:+,}억" if it is not None else "-"
                sub.append(f"수급 외인 {fn_str} / 기관 {it_str}")
            parts.append("  ".join(sub))

    return "\n".join(parts)


def _fmt_stocks_asia(stocks_data: dict) -> str:
    """Asia 종목 테이블 텍스트 — KOSPI/KOSDAQ 분리 출력."""
    lines = []
    ms = _fmt_market_structure("asia", stocks_data)
    if ms:
        lines.append(ms)
        lines.append("")

    def _table_major(rows: list[dict]) -> list[str]:
        out = ["| 종목 | 종가 | 등락률 | 거래대금(억) | 시총(조) |",
               "|------|-----:|-------:|-------------:|---------:|"]
        for s in rows:
            tv = f"{s['trade_val']//100_000_000:,}" if s.get("trade_val") else "-"
            mc = f"{s['mktcap']/1e12:.1f}" if s.get("mktcap") else "-"
            out.append(f"| {s['name']} | {s['close']:,} | {s['chg_pct']:+.2f}% | {tv} | {mc} |")
        return out

    def _table_featured(rows: list[dict]) -> list[str]:
        out = ["| 종목 | 종가 | 등락률 | 외인(억) | 기관(억) | 시그널 |",
               "|------|-----:|-------:|---------:|---------:|--------|"]
        for s in rows:
            fn  = f"{s['foreign_net']//100_000_000:+,}" if s.get("foreign_net") is not None else "-"
            it  = f"{s['inst_net']//100_000_000:+,}"    if s.get("inst_net")    is not None else "-"
            out.append(f"| {s['name']} | {s['close']:,} | {s['chg_pct']:+.2f}% | {fn} | {it} | {s.get('signal','')} |")
        return out

    # KOSPI 블록
    major_k  = stocks_data.get("major_kospi",    [])
    feat_k   = stocks_data.get("featured_kospi", [])
    if major_k or feat_k:
        lines.append("\n## [KOSPI] 주요 종목 (시총+거래대금 TOP 10)")
        if major_k:
            lines.extend(_table_major(major_k))
        if feat_k:
            lines.append("\n**[KOSPI] 특징주 (급등락+거래량급증+수급 TOP 10)**")
            lines.extend(_table_featured(feat_k))

    # KOSDAQ 블록
    major_q  = stocks_data.get("major_kosdaq",    [])
    feat_q   = stocks_data.get("featured_kosdaq", [])
    if major_q or feat_q:
        lines.append("\n## [KOSDAQ] 주요 종목 (시총+거래대금 TOP 10)")
        if major_q:
            lines.extend(_table_major(major_q))
        if feat_q:
            lines.append("\n**[KOSDAQ] 특징주 (급등락+거래량급증+수급 TOP 10)**")
            lines.extend(_table_featured(feat_q))

    # 시장별 키가 없으면 기존 통합 출력으로 폴백
    if not (major_k or feat_k or major_q or feat_q):
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

    # ── 일본·중국·홍콩 구성종목 블록 ────────────────────────────
    overseas = stocks_data.get("overseas_asia", {})
    market_order = [
        ("jp", "🇯🇵 일본 Nikkei225"),
        ("cn", "🇨🇳 중국 CSI300"),
        ("hk", "🇭🇰 홍콩 HSI"),
    ]
    for mk, label in market_order:
        d = overseas.get(mk)
        if not d:
            continue
        b = d.get("breadth", {})
        lines.append(f"\n## {label}")
        if b:
            up, dn, tot = b.get("up", 0), b.get("down", 0), b.get("total", 0)
            up_pct, adr = b.get("up_pct"), b.get("adr")
            seg = f"**시장 폭** 상승 {up}/{tot}"
            if up_pct is not None: seg += f"({up_pct:.1f}%)"
            seg += f"  하락 {dn}/{tot}"
            if adr is not None: seg += f"  ADR {adr:.2f}"
            lines.append(seg)

        major_o = d.get("major", [])
        if major_o:
            lines.append("\n**주요 종목 (시총+거래대금 TOP 8)**")
            lines.append("| 종목 | 섹터 | 종가 | 등락률 | 시총(B) | 거래대금(B) |")
            lines.append("|------|------|-----:|-------:|--------:|------------:|")
            for s in major_o:
                mc = f"{s['mktcap_b']:,.1f}" if s.get("mktcap_b") is not None else "-"
                tv = f"{s['trade_val_b']:,.2f}" if s.get("trade_val_b") is not None else "-"
                sec = (s.get("sector") or "-")[:14]
                lines.append(f"| {s['name'][:22]} | {sec} | {s['close']:,.2f} | {s['chg_pct']:+.2f}% | {mc} | {tv} |")

        feat_o = d.get("featured", [])
        if feat_o:
            lines.append("\n**특징주 (급등락 ±3% or 거래량 ≥1.5x)**")
            lines.append("| 종목 | 섹터 | 종가 | 등락률 | 시그널 |")
            lines.append("|------|------|-----:|-------:|--------|")
            for s in feat_o:
                sec = (s.get("sector") or "-")[:14]
                lines.append(f"| {s['name'][:22]} | {sec} | {s['close']:,.2f} | {s['chg_pct']:+.2f}% | {s.get('signal','')} |")

        sectors_o = d.get("sectors", [])
        if sectors_o:
            lines.append("\n**섹터 분석 (시총가중 등락률 절대값 상위 5)**")
            lines.append("| 섹터 | 종목수 | 단순평균 | 시총가중 | 섹터시총(B) |")
            lines.append("|------|------:|---------:|---------:|------------:|")
            for s in sectors_o[:5]:
                wm = f"{s['chg_wmean']:+.2f}%" if s.get("chg_wmean") is not None else "-"
                mc = f"{s['mktcap_b']:,.0f}" if s.get("mktcap_b") is not None else "-"
                lines.append(f"| {s['sector']} | {s['n']} | {s['chg_avg']:+.2f}% | {wm} | {mc} |")

    return "\n".join(lines)


def _fmt_stocks_europe(stocks_data: dict) -> str:
    """Europe 섹터+종목 테이블 텍스트 생성."""
    lines = []
    ms = _fmt_market_structure("europe", stocks_data)
    if ms:
        lines.append(ms)
        lines.append("")

    sectors    = stocks_data.get("sectors", [])
    mktcap     = stocks_data.get("mktcap_top", [])
    tradeval   = stocks_data.get("tradeval_top", [])
    surge      = stocks_data.get("turnover_surge", [])
    top_stocks = stocks_data.get("top_stocks", [])

    if sectors:
        lines.append("\n**STOXX600 섹터 성과**")
        lines.append("| 섹터 | 종가 | 등락률 |")
        lines.append("|------|-----:|-------:|")
        for s in sectors:
            chg = f"{s['chg_pct']:+.2f}%" if s.get("chg_pct") is not None else "-"
            lines.append(f"| {s['name']} | {s['close']:,.2f} | {chg} |")

    if mktcap:
        lines.append("\n**시총 상위** (DAX·FTSE·CAC)")
        lines.append("| 종목 | 지수 | 통화 | 종가 | 등락률 | 시총($B 환산) |")
        lines.append("|------|------|------|-----:|-------:|------------:|")
        for s in mktcap:
            chg = f"{s['chg_pct']:+.2f}%" if s.get("chg_pct") is not None else "-"
            cap = f"{s['mktcap_b']:.1f}" if s.get("mktcap_b") else "-"
            lines.append(f"| {s['name']} | {s.get('index','')} | {s.get('ccy','')} | "
                         f"{s['close']:,.2f} | {chg} | {cap} |")

    if tradeval:
        lines.append("\n**거래대금 상위** (현지통화 십억)")
        lines.append("| 종목 | 지수 | 통화 | 종가 | 등락률 | 거래대금(B) |")
        lines.append("|------|------|------|-----:|-------:|----------:|")
        for s in tradeval:
            chg = f"{s['chg_pct']:+.2f}%" if s.get("chg_pct") is not None else "-"
            dv  = f"{s['dollar_vol_b']:.2f}" if s.get("dollar_vol_b") else "-"
            lines.append(f"| {s['name']} | {s.get('index','')} | {s.get('ccy','')} | "
                         f"{s['close']:,.2f} | {chg} | {dv} |")

    if surge:
        lines.append("\n**거래대금 급증 특징주** (≥ 1.5x + |등락률| ≥ 2%)")
        lines.append("| 종목 | 지수 | 종가 | 등락률 | 급증배수 | 시그널 |")
        lines.append("|------|------|-----:|-------:|---------:|--------|")
        for s in surge:
            chg = f"{s['chg_pct']:+.2f}%" if s.get("chg_pct") is not None else "-"
            sr  = f"{s['surge_ratio']:.1f}x" if s.get("surge_ratio") else "-"
            lines.append(f"| {s['name']} | {s.get('index','')} | {s['close']:,.2f} | "
                         f"{chg} | {sr} | {s.get('signal','')} |")

    if top_stocks and not (mktcap or tradeval or surge):
        # 신규 스크리닝이 모두 비었을 때 호환성 fallback
        lines.append("\n**유럽 주요 종목** (DAX·FTSE·CAC 등락상위 15)")
        lines.append("| 종목 | 지수 | 종가 | 등락률 |")
        lines.append("|------|------|-----:|-------:|")
        for s in top_stocks[:15]:
            chg = f"{s['chg_pct']:+.2f}%" if s.get("chg_pct") is not None else "-"
            lines.append(f"| {s['name']} | {s.get('index','')} | {s['close']:,.2f} | {chg} |")

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

    # ── 유럽 종목 데이터 (Europe 독립 섹션용) ──────────────────────────
    europe = stocks_data.get("europe_stocks", {})
    if europe:
        lines.append("\n**── 유럽 시장 데이터 (DAX·FTSE·CAC 세션 수집값) ──**")
        eu_text = _fmt_stocks_europe(europe)
        if eu_text.strip():
            lines.append(eu_text)

    if sectors:
        lines.append("\n**SPX 섹터 ETF 성과**")
        lines.append("| 섹터 | ETF | 종가 | 등락률 |")
        lines.append("|------|-----|-----:|-------:|")
        for s in sectors:
            chg = f"{s['chg_pct']:+.2f}%" if s.get("chg_pct") is not None else "-"
            lines.append(f"| {s['name']} | {s['ticker']} | {s['close']:,.2f} | {chg} |")

    if mktcap:
        lines.append("\n**시총 상위** (Large Cap)")
        lines.append("| 종목 | 종가 | 등락률 | 거래대금($B) | 시총($B) | EPS추정변화(1W) |")
        lines.append("|------|-----:|-------:|------------:|---------:|---------------:|")
        for s in mktcap[:10]:
            chg  = f"{s['chg_pct']:+.2f}%" if s.get("chg_pct") is not None else "-"
            cap  = f"{s['mktcap_b']:.0f}" if s.get("mktcap_b") else "-"
            eps  = f"{s['eps_chg_1w']:+.1f}%" if s.get("eps_chg_1w") is not None else "-"
            dv   = f"{s['dollar_vol_b']:.1f}" if s.get("dollar_vol_b") else "-"
            lines.append(f"| {s['ticker']} | {s['close']:,.2f} | {chg} | {dv} | {cap} | {eps} |")

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

## KOSPI / KOSDAQ 종목 분석
(아래 데이터는 **KOSPI**와 **KOSDAQ**이 각각 분리되어 제공됩니다.
각 시장별로 주요 종목(시총+거래대금)과 특징주(급등락+거래량급증+수급)를 별도 표로 정리하고,
시장별 폭·수급 데이터를 활용해 다음을 분석하세요:
  - KOSPI vs KOSDAQ 어느 쪽이 강세이고 자금이 집중되는지
  - 각 시장의 외인/기관 수급 방향 차이
  - 특징주가 수급 동반 급등인지 vs 수급 없는 단순 급등인지 시장별 구분)

{stocks}

---

## 일본·중국·홍콩 시장 상세 (Nikkei225·CSI300·HSI 구성종목)
(아래 데이터를 표로 출력하고, 시장별로 다음을 분석하세요:
  - 강세/약세 섹터 (시총가중 등락률 기준)
  - 시총 상위 대형주의 방향성과 거래대금 집중도
  - 급등/급락+거래량 급증 특징주의 테마 (반도체·전기차·금융·에너지 등)
  - **한국 시장에의 의의**: 일본·중국·홍콩에서 강세를 보인 섹터·테마가 KOSPI/KOSDAQ의 어떤 종목·업종에 영향을 줄지 명시적으로 연결
    (예: 일본 반도체 장비주 강세 → 한국 SK하이닉스·삼성전자·반도체 장비 중소형주 / 중국 전기차 부품 강세 → 한국 2차전지·소재주 / 홍콩 금융 약세 → 한국 은행·증권 비중 조절))

---

## 운용 포인트
(2~3문장, 한국 포지션 관점 — KOSPI/KOSDAQ 어느 쪽에 무게중심을 둘지, 일본·중국·홍콩 강세 테마와 연결한 한국 종목/업종 비중 조절 의견 포함)
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

## 유럽 종목 분석 — 한국·글로벌 시장에의 의의
(아래 관점으로 분석하세요:
  - 시총 상위 / 거래대금 상위 대형주의 방향성과 거래 집중도
  - 거래대금 급증 특징주의 테마 (반도체·자동차·금융·헬스케어·에너지 등)
  - **한국 시장에의 의의**: 유럽에서 강세/약세를 보인 섹터·테마가 KOSPI/KOSDAQ의 어떤 종목·업종에 영향을 줄지 명시적으로 연결
    (예: 유럽 반도체 강세(ASML·Infineon) → 한국 반도체 장비주 / 유럽 자동차 강세(BMW·Mercedes) → 한국 현대차·기아·자동차부품 / 유럽 헬스케어 약세(Novartis·AstraZeneca) → 한국 바이오·제약 / 유럽 에너지(Shell·BP) → 한국 정유·SK이노 / 유럽 럭셔리(LVMH 가정) → 한국 화장품·의류)
  - **글로벌 매크로 의의**: 유럽 은행·금융주 흐름이 ECB 정책·금리 기대에 주는 시사점)

---

## 운용 포인트
(2~3문장, 한국 관점에서 유럽 강세 테마와 연결한 KOSPI/KOSDAQ 업종 비중 조절 의견 포함)
""".format(stocks=stocks_section)

    else:  # us — 글로벌 통합 브리핑
        stocks_section = _fmt_stocks_us(stocks_data)
        has_prior = bool(stocks_data.get("prior_briefings"))
        has_europe = bool(stocks_data.get("europe_stocks"))
        session_format = f"""
## US / 글로벌 통합 시황 브리핑

**핵심 요약** (3~4문장, 글로벌 전체 흐름 — 미국·유럽·원자재·달러 핵심 드라이버 명시)

---

## 핵심 매크로 드라이버 (매일 고정)
(당일 시장 전체를 움직인 핵심 변수 3개를 아래 형식으로 작성:
| 드라이버 | 방향 | 시사점 |
|---------|------|--------|
| 예: CPI 4.2% 상회 | 금리인상 기대↑ | 기술주 조정, 실질금리↑로 금 하락 |
데이터가 충분치 않으면 확실한 것만 1~2개 작성)

---

## 미국 지수 동향
(SPX·NDX·DJIA·Russell2000 — 표로 작성)

---

## 시장 폭 (Market Breadth)
(stocks 데이터의 breadth 정보를 아래 표로 반드시 작성:
| 지수 | 상승비율 | ADR | 거래대금가중 등락 |
|------|---------|-----|----------------|
| SPX  | X.X% (상승N/전체M) | X.XX | +X.XX% |
breadth 데이터가 없을 때만 이 섹션을 생략)

---

## 섹터 분석
(강세/약세 섹터 — 강세 Top3·약세 Top3를 표로 작성. 시장 폭 데이터와 결합해 "광범위 랠리 vs 소수 집중" 여부를 1문장 분석)

---

## SPX 섹터 ETF 성과
(스냅샷의 US_Tech·US_Finance·US_Energy 등 전체 섹터 ETF를 종가·등락률 포함해 표로 작성)

---

## 시총 상위 종목
(stocks의 mktcap_top 데이터 — 종목·종가·등락률·거래대금($B)·시총($B)·EPS추정변화를 표로 작성. 거래대금 컬럼 반드시 포함)

---

## 매크로
(카테고리별 표로 작성:
- FX: DXY 반드시 첫 행에 포함 후 EUR/USD·USD/JPY·USD/CNY·USD/KRW 순. DXY 방향으로 강달러 전환 여부 1문장 코멘트.
- 금리: US 10Y·2Y·KR 3Y·JP 10Y
- 원자재: WTI·Brent·Gold — 원인(이란 제재, CPI, 달러) 비고 컬럼에 1줄씩 추가
- 변동성: VIX·MOVE·VKOSPI)

---

{'## 유럽 시장 동향' if has_europe else ''}
{'(stocks의 "유럽 시장 데이터" 섹션을 활용해 아래를 작성:' if has_europe else ''}
{'  - DAX·FTSE100·CAC40·EuroStoxx50 지수 동향 표 (snapshot에서)' if has_europe else ''}
{'  - STOXX600 섹터 강약 표 (강세 Top3·약세 Top3)' if has_europe else ''}
{'  - 시총상위·거래대금 급증 특징주 표 (종목·지수·종가·등락률·급증배수·시그널)' if has_europe else ''}
{'  - 유럽 섹터와 한국 연관 테마 명시:' if has_europe else ''}
{'    (예: ASML 강세 → 한국 반도체 장비주 / Siemens Energy 급락 → 국내 전력기기 약세 / 자동차 → 현대차·기아))' if has_europe else ''}

{'---' if has_europe else ''}

{{stocks}}

---

## 미국 종목 분석 — 한국·글로벌 시장에의 의의
(아래 관점으로 분석하세요:
  - SPX 섹터 ETF 강세/약세 상위 (Tech·Finance·Energy·Healthcare·Industrials·Consumer 등)
  - 시총 상위 / 거래대금 상위 대형주 동향과 거래 집중도
  - 거래대금 급증 특징주의 테마 (AI·반도체·전기차·바이오·은행·에너지 등)
  - EPS 추정치 변화 종목의 펀더멘털 시사점
  - **한국 시장에의 의의**: 미국에서 강세/약세를 보인 섹터·테마가 KOSPI/KOSDAQ의 어떤 종목·업종에 영향을 줄지 명시적으로 연결
    (예: 미국 반도체 강세(Nvidia·AMD·Micron) → 한국 SK하이닉스·삼성전자·반도체 장비주 / 미국 전기차·배터리(Tesla) → 한국 2차전지·소재(에코프로비엠·LG에너지솔루션) / 미국 빅테크(Apple·MSFT) → 한국 부품주·LG디스플레이 / 미국 은행 강세(JPM·BAC) → 한국 은행·보험 / 미국 에너지 강세 → 한국 정유·SK이노 / 미국 헬스케어 → 한국 바이오)
  - **글로벌 매크로 의의**: 시장 폭·VIX·DXY 흐름이 신흥국·한국 자금 흐름에 주는 시사점)

---

{'## 아시아·유럽 당일 주요 이슈' if has_prior else ''}
{'(앞서 브리핑 요약 기반 — 지역별 지수·주요 이슈를 표로 작성)' if has_prior else ''}

{'---' if has_prior else ''}

## 주요 경제지표 — 이번주·다음주
(FOMC·고용·CPI·PMI 등 — 이번주 월요일부터 다음주 일요일까지의 주요 지표를 발표일·지표명·기준기간·예상치·실제치를 표로 작성. 이미 발표된 항목은 실제치를 함께 표기하고 서프라이즈 여부를 1문장 코멘트)

---

## SPX 기업실적 — 이번주·다음주
(SPX 구성종목 어닝 — 발표일·티커·시간대(BMO/AMC)·EPS 예상/실제·서프라이즈(%)를 표로 작성. 이미 발표된 종목은 결과를 포함하고, 향후 주목할 대형주 1~2개를 1문장 코멘트)

---

## 글로벌 운용 포인트
(2~3문장, 한국 포지션 관점 — 미국·유럽·아시아 흐름을 종합한 KOSPI/KOSDAQ 업종/종목 비중 조절 의견 포함)
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
            # US 세션: europe_stocks 복원 (Europe 독립 섹션용)
            if not stocks_data.get("europe_stocks"):
                e_stocks = DBManager().get_stocks_daily(today, "europe")
                if e_stocks:
                    stocks_data["europe_stocks"] = e_stocks
                    print(f"[llm_briefing] europe_stocks DB에서 로드")

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

    has_stocks = bool(stocks_data and any(stocks_data.get(k) for k in
                      ("major", "featured", "sectors", "top_stocks",
                       "mktcap_top", "tradeval_top", "turnover_surge", "europe_stocks")))
    # US 세션은 Europe 독립 섹션 추가로 출력이 길어지므로 max_tokens 확장
    max_tok = 8000 if session == "us" else 6000
    message = client.messages.create(
        model=MODEL,
        max_tokens=max_tok,
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
