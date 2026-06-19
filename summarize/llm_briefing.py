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
load_dotenv(BASE_DIR / ".env")
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
    """Asia 종목 — 한국(Main) + 일본·중국·홍콩(Sub) 구조."""
    lines = []
    overseas = stocks_data.get("overseas_asia", {})
    mk_pairs = (("jp", "일본"), ("cn", "중국"), ("hk", "홍콩"))

    # ── 공통 row 포매터 ──────────────────────────────────────────────
    HDR = "| 분류 | Ticker | Name | Sector | 종가 | 1D% | 1W% | 1M% | 거래대금 | Chg% | 비고 |"
    SEP = "|------|--------|------|--------|-----:|----:|----:|----:|---------:|-----:|------|"

    def _r(v, fmt="+.2f"):
        return f"{v:{fmt}}%" if v is not None else "-"

    def _tv_kr(s):
        v = s.get("trade_val")
        return f"{v//100_000_000:,}억" if v else "-"

    def _tv_ov(s):
        v = s.get("trade_val_b")
        return f"{v:.1f}B" if v is not None else "-"

    def _row_kr(div, s):
        fn  = s.get("foreign_net")
        it  = s.get("inst_net")
        sig = s.get("signal", "")
        fn_s = f"외인{fn//100_000_000:+,}억" if fn is not None else ""
        it_s = f"기관{it//100_000_000:+,}억" if it is not None else ""
        note = " ".join(filter(None, [fn_s, it_s, sig]))
        return (f"| {div} | {s['ticker']} | {s['name'][:18]} | - | "
                f"{s['close']:,} | {_r(s.get('chg_pct'))} | {_r(s.get('ret_1w'))} | "
                f"{_r(s.get('ret_1m'))} | {_tv_kr(s)} | {_r(s.get('tv_chg'),'+.0f')} | {note} |")

    def _row_ov(div, s):
        sec = (s.get("sector") or s.get("market") or "-")[:14]
        return (f"| {div} | {s['ticker']} | {s['name'][:18]} | {sec} | "
                f"{s['close']:.2f} | {_r(s.get('chg_pct'))} | {_r(s.get('ret_1w'))} | "
                f"{_r(s.get('ret_1m'))} | {_tv_ov(s)} | {_r(s.get('tv_chg'),'+.0f')} |  |")

    # ════════════════════════════════════════
    # [Main] 한국 (KOSPI + KOSDAQ)
    # ════════════════════════════════════════
    ms = _fmt_market_structure("asia", stocks_data)
    if ms:
        lines.append(ms)
        lines.append("")

    kr_major: list[str] = []
    for s in stocks_data.get("major_kospi",  []): kr_major.append(_row_kr("KOSPI",  s))
    for s in stocks_data.get("major_kosdaq", []): kr_major.append(_row_kr("KOSDAQ", s))
    if not kr_major:
        for s in stocks_data.get("major", []): kr_major.append(_row_kr(s.get("market", "KR"), s))
    if kr_major:
        lines.append("\n## [한국] 시총+거래대금 상위 (KOSPI·KOSDAQ)")
        lines.append(HDR); lines.append(SEP); lines.extend(kr_major)

    kr_feat: list[str] = []
    for s in stocks_data.get("featured_kospi",  []): kr_feat.append(_row_kr("KOSPI",  s))
    for s in stocks_data.get("featured_kosdaq", []): kr_feat.append(_row_kr("KOSDAQ", s))
    if not kr_feat:
        for s in stocks_data.get("featured", []): kr_feat.append(_row_kr(s.get("market", "KR"), s))
    if kr_feat:
        lines.append("\n## [한국] 특징주 — 급등락+거래대금급증")
        lines.append(HDR); lines.append(SEP); lines.extend(kr_feat)

    # ════════════════════════════════════════
    # [Sub] 일본·중국·홍콩 — 섹션별 통합표
    # ════════════════════════════════════════

    # 시장 폭
    brd_rows: list[str] = []
    for mk, lbl in mk_pairs:
        b = overseas.get(mk, {}).get("breadth", {})
        if b:
            up_pct = f"{b['up_pct']:.1f}%" if b.get("up_pct") is not None else "-"
            adr    = f"{b['adr']:.2f}"      if b.get("adr")    is not None else "-"
            wch    = f"{b['weighted_chg']:+.2f}%" if b.get("weighted_chg") is not None else "-"
            brd_rows.append(
                f"| {lbl} | {b.get('up',0)}/{b.get('total',0)} | {up_pct} | {adr} | {wch} |")
    if brd_rows:
        lines.append("\n## [Sub] 시장 폭 — 일본·중국·홍콩")
        lines.append("| 시장 | 상승/전체 | 상승% | ADR | 거래대금가중등락 |")
        lines.append("|------|:---------:|------:|----:|----------------:|")
        lines.extend(brd_rows)

    # 시총 상위
    mc_rows: list[str] = []
    for mk, lbl in mk_pairs:
        for s in overseas.get(mk, {}).get("mktcap_top", []):
            mc_rows.append(_row_ov(lbl, s))
    if mc_rows:
        lines.append("\n## [Sub] 시총 상위 — 일본·중국·홍콩")
        lines.append(HDR); lines.append(SEP); lines.extend(mc_rows)

    # 거래대금 상위
    tv_rows: list[str] = []
    for mk, lbl in mk_pairs:
        for s in overseas.get(mk, {}).get("tradeval_top", []):
            tv_rows.append(_row_ov(lbl, s))
    if tv_rows:
        lines.append("\n## [Sub] 거래대금 상위 — 일본·중국·홍콩")
        lines.append(HDR); lines.append(SEP); lines.extend(tv_rows)

    # 특징주
    ov_feat: list[str] = []
    for mk, lbl in mk_pairs:
        for s in overseas.get(mk, {}).get("featured", []):
            ov_feat.append(_row_ov(lbl, s))
    if ov_feat:
        lines.append("\n## [Sub] 특징주 — 일본·중국·홍콩")
        lines.append(HDR); lines.append(SEP); lines.extend(ov_feat)

    # 섹터 분석
    sec_rows: list[str] = []
    for mk, lbl in mk_pairs:
        for s in overseas.get(mk, {}).get("sectors", [])[:5]:
            wm = f"{s['chg_wmean']:+.2f}%" if s.get("chg_wmean") is not None else "-"
            sec_rows.append(
                f"| {lbl} | {s['sector']} | {s['n']} | {s['chg_avg']:+.2f}% | {wm} |")
    if sec_rows:
        lines.append("\n## [Sub] 섹터 분석 — 일본·중국·홍콩")
        lines.append("| 시장 | 섹터 | 종목수 | 단순평균 | 시총가중 |")
        lines.append("|------|------|------:|---------:|---------:|")
        lines.extend(sec_rows)

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

    # 종목표 통합 포맷 (Asia overseas와 동일 구조, 분류=지수)
    HDR = "| 분류 | Ticker | Name | 종가 | 1D% | 1W% | 1M% | 거래대금(B) | 비고 |"
    SEP = "|------|--------|------|-----:|----:|----:|----:|-----------:|------|"

    def _r(v, fmt="+.2f"):
        return f"{v:{fmt}}%" if v is not None else "-"

    def _row_eu(s, note=""):
        idx = s.get("index") or s.get("market") or "-"
        dv  = f"{s['dollar_vol_b']:.2f}" if s.get("dollar_vol_b") else "-"
        return (f"| {idx} | {s['ticker']} | {s['name'][:20]} | "
                f"{s['close']:.2f} | {_r(s.get('chg_pct'))} | {_r(s.get('ret_1w'))} | "
                f"{_r(s.get('ret_1m'))} | {dv} | {note or s.get('signal', '')} |")

    if sectors:
        lines.append("\n**STOXX600 섹터 성과**")
        lines.append("| 섹터 | 종가 | 1D% |")
        lines.append("|------|-----:|----:|")
        for s in sectors:
            chg = f"{s['chg_pct']:+.2f}%" if s.get("chg_pct") is not None else "-"
            lines.append(f"| {s['name']} | {s['close']:,.2f} | {chg} |")

    if mktcap:
        lines.append("\n**시총 상위** (DAX·FTSE·CAC)")
        lines.append(HDR); lines.append(SEP)
        for s in mktcap:
            mc = f"시총 {s['mktcap_b']:.1f}B" if s.get("mktcap_b") else ""
            lines.append(_row_eu(s, mc))

    if tradeval:
        lines.append("\n**거래대금 상위** (현지통화 십억)")
        lines.append(HDR); lines.append(SEP)
        for s in tradeval:
            lines.append(_row_eu(s))

    if surge:
        lines.append("\n**거래대금 급증 특징주** (≥ 1.5x + |등락률| ≥ 2%)")
        lines.append(HDR); lines.append(SEP)
        for s in surge:
            sr = f"{s['surge_ratio']:.1f}x" if s.get("surge_ratio") else ""
            lines.append(_row_eu(s, sr))

    if top_stocks and not (mktcap or tradeval or surge):
        lines.append("\n**유럽 주요 종목** (DAX·FTSE·CAC 등락상위 15)")
        lines.append("| 지수 | Ticker | Name | 종가 | 등락률 |")
        lines.append("|------|--------|------|-----:|-------:|")
        for s in top_stocks[:15]:
            chg = f"{s['chg_pct']:+.2f}%" if s.get("chg_pct") is not None else "-"
            lines.append(f"| {s.get('index','')} | {s.get('ticker','-')} | "
                         f"{s['name']} | {s['close']:,.2f} | {chg} |")

    return "\n".join(lines)


def _fmt_stocks_us(stocks_data: dict) -> str:
    """US(Main) + Europe(Sub) 종목 데이터 — Asia 구조와 동일한 섹션 방식."""
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
    europe   = stocks_data.get("europe_stocks", {})

    def _r(v, fmt="+.2f"):
        return f"{v:{fmt}}%" if v is not None else "-"

    # 전일 아시아·유럽 브리핑 요약 (컨텍스트)
    if prior:
        lines.append("\n**당일 아시아·유럽 시황 요약**")
        for sess, text in prior.items():
            lines.append(f"\n[{sess.upper()}] {text[:300]}...")

    # SPX 섹터 ETF
    if sectors:
        lines.append("\n**SPX 섹터 ETF 성과**")
        lines.append("| 섹터 | ETF | 종가 | 1D% |")
        lines.append("|------|-----|-----:|----:|")
        for s in sectors:
            chg = f"{s['chg_pct']:+.2f}%" if s.get("chg_pct") is not None else "-"
            lines.append(f"| {s['name']} | {s['ticker']} | {s['close']:,.2f} | {chg} |")

    # ════════════════════════════════════════
    # [US Main]
    # ════════════════════════════════════════
    US_HDR = "| 분류 | Ticker | Name | Sector | 종가 | 1D% | 1W% | 1M% | 규모 | 비고 |"
    US_SEP = "|------|--------|------|--------|-----:|----:|----:|----:|-----:|------|"

    # 시총+거래대금 상위
    us_major: list[str] = []
    for s in mktcap[:10]:
        sec = (s.get("sector") or "-")[:16]
        mc  = f"{s['mktcap_b']:.0f}B" if s.get("mktcap_b") else "-"
        us_major.append(f"| 시총상위 | {s['ticker']} | {s.get('name', s['ticker'])[:18]} | {sec} | "
                        f"{s['close']:.2f} | {_r(s.get('chg_pct'))} | {_r(s.get('ret_1w'))} | "
                        f"{_r(s.get('ret_1m'))} | {mc} |  |")
    for s in tradeval[:10]:
        sec = (s.get("sector") or "-")[:16]
        dv  = f"{s['dollar_vol_b']:.1f}B" if s.get("dollar_vol_b") else "-"
        us_major.append(f"| 거래대금상위 | {s['ticker']} | {s.get('name', s['ticker'])[:18]} | {sec} | "
                        f"{s['close']:.2f} | {_r(s.get('chg_pct'))} | {_r(s.get('ret_1w'))} | "
                        f"{_r(s.get('ret_1m'))} | {dv} |  |")
    if us_major:
        lines.append("\n## [US] 시총+거래대금 상위")
        lines.append(US_HDR); lines.append(US_SEP); lines.extend(us_major)

    # 급등락+거래대금급증
    if surge:
        lines.append("\n## [US] 급등락+거래대금급증")
        lines.append(US_HDR); lines.append(US_SEP)
        for s in surge:
            sec = (s.get("sector") or "-")[:16]
            sr  = f"{s['surge_ratio']:.1f}x" if s.get("surge_ratio") else "-"
            lines.append(f"| 특징주 | {s['ticker']} | {s.get('name', s['ticker'])[:18]} | {sec} | "
                         f"{s['close']:.2f} | {_r(s.get('chg_pct'))} | {_r(s.get('ret_1w'))} | "
                         f"{_r(s.get('ret_1m'))} | {sr} | {s.get('signal','')} |")

    # EPS Revision
    if eps_rev:
        lines.append("\n## [US] EPS Revision")
        lines.append("| 분류 | Ticker | Name | Sector | 1D% | 1W% | 1M% | EPS변화(1W) | EPS변화(1M) | 비고 |")
        lines.append("|------|--------|------|--------|----:|----:|----:|------------:|------------:|------|")
        for s in eps_rev:
            chg = _r(s.get("chg_pct"))
            r1w = (_r(s.get("ret_1w")) if s.get("ret_1w") is not None else
                   (_r(s.get("return_7d")) if s.get("return_7d") is not None else "-"))
            r1m = _r(s.get("ret_1m"))
            e1w = f"{s['eps_chg_1w']:+.2f}%" if s.get("eps_chg_1w") is not None else "-"
            e1m = f"{s['eps_chg_1m']:+.2f}%" if s.get("eps_chg_1m") is not None else "-"
            sec = (s.get("sector") or "-")[:16]
            lines.append(f"| EPS | {s['ticker']} | {s.get('name', s['ticker'])[:18]} | {sec} | "
                         f"{chg} | {r1w} | {r1m} | {e1w} | {e1m} |  |")

    # ════════════════════════════════════════
    # [Europe Sub]
    # ════════════════════════════════════════
    if europe:
        eu_breadth  = europe.get("breadth", {})
        eu_sectors  = europe.get("sectors", [])
        eu_mktcap   = europe.get("mktcap_top", [])
        eu_tradeval = europe.get("tradeval_top", [])
        eu_surge    = europe.get("turnover_surge", [])

        EU_HDR = "| 분류 | Ticker | Name | 종가 | 1D% | 1W% | 1M% | 거래대금(B) | 비고 |"
        EU_SEP = "|------|--------|------|-----:|----:|----:|----:|-----------:|------|"

        def _row_eu(div, s, note=""):
            dv = f"{s['dollar_vol_b']:.2f}" if s.get("dollar_vol_b") else "-"
            return (f"| {div} | {s['ticker']} | {s['name'][:20]} | "
                    f"{s['close']:.2f} | {_r(s.get('chg_pct'))} | {_r(s.get('ret_1w'))} | "
                    f"{_r(s.get('ret_1m'))} | {dv} | {note or s.get('signal', '')} |")

        # 시장 폭
        if eu_breadth:
            up     = eu_breadth.get("up", 0)
            down   = eu_breadth.get("down", 0)
            total  = eu_breadth.get("total", 0)
            up_pct = f"{eu_breadth['up_pct']:.1f}%" if eu_breadth.get("up_pct") is not None else "-"
            adr    = f"{eu_breadth['adr']:.2f}"     if eu_breadth.get("adr")    is not None else "-"
            lines.append("\n## [Europe] 시장 폭 — DAX·FTSE100·CAC40")
            lines.append("| 시장 | 상승/전체 | 상승% | ADR |")
            lines.append("|------|:---------:|------:|----:|")
            lines.append(f"| DAX·FTSE·CAC | {up}/{total} | {up_pct} | {adr} |")

        # STOXX600 섹터 분석
        if eu_sectors:
            lines.append("\n## [Europe] STOXX600 섹터 분석")
            lines.append("| 섹터 | 종가 | 1D% |")
            lines.append("|------|-----:|----:|")
            for s in eu_sectors:
                chg = f"{s['chg_pct']:+.2f}%" if s.get("chg_pct") is not None else "-"
                lines.append(f"| {s['name']} | {s['close']:,.2f} | {chg} |")

        # 시총 상위
        if eu_mktcap:
            lines.append("\n## [Europe] 시총 상위 — DAX·FTSE100·CAC40")
            lines.append(EU_HDR); lines.append(EU_SEP)
            for s in eu_mktcap:
                idx = s.get("index") or s.get("market") or "-"
                mc  = f"시총{s['mktcap_b']:.1f}B" if s.get("mktcap_b") else ""
                lines.append(_row_eu(idx, s, mc))

        # 거래대금 상위
        if eu_tradeval:
            lines.append("\n## [Europe] 거래대금 상위 — DAX·FTSE100·CAC40")
            lines.append(EU_HDR); lines.append(EU_SEP)
            for s in eu_tradeval:
                idx = s.get("index") or s.get("market") or "-"
                lines.append(_row_eu(idx, s))

        # 급등락+거래대금급증
        if eu_surge:
            lines.append("\n## [Europe] 급등락+거래대금급증 — DAX·FTSE100·CAC40")
            lines.append(EU_HDR); lines.append(EU_SEP)
            for s in eu_surge:
                idx = s.get("index") or s.get("market") or "-"
                sr  = f"{s['surge_ratio']:.1f}x" if s.get("surge_ratio") else ""
                lines.append(_row_eu(idx, s, sr))

    return "\n".join(lines)


def build_prompt(session: str, snapshot_text: str, stocks_data: dict = None) -> str:
    context = SESSION_CONTEXT.get(session, "")
    stocks_data = stocks_data or {}

    if session == "asia":
        stocks_section = _fmt_stocks_asia(stocks_data)
        session_format = """
## Asia 시황 브리핑

**핵심 요약** (2~3문장)

---

## 주요 지수 동향
(한국·일본·중국·홍콩 — 종목명·종가·등락률을 마크다운 표로 작성)

---

## 매크로
(FX·금리·원자재·변동성을 아래 통합 표 형식으로 작성하세요.
`| 분류 | 지표 | 종가 | 1D% | 1W% | 1M% | 비고 |` — 비고에 핵심 원인·시사점 1줄)

---

## 종목 분석

### [한국 Main] KOSPI·KOSDAQ
아래 두 표가 제공됩니다: **[한국] 시총+거래대금 상위** / **[한국] 특징주**.
표 형식: `분류 | Ticker | Name | Sector | 종가 | 1D% | 1W% | 1M% | 거래대금 | Chg% | 비고`
**비고** 컬럼에 상승/하락 원인·코멘트를 1줄로 작성하세요 (외인·기관 수급 반드시 포함).
Chg%는 거래대금 전일대비 변화율입니다.
분석 관점:
  - KOSPI vs KOSDAQ 강세 시장·자금 집중 방향
  - 외인·기관 수급 동반 여부 (수급 있는 급등 vs 수급 없는 급등 구분)
  - 급등락+거래대금급증 특징주의 테마 (반도체·전기차·금융·에너지 등)

### [Sub] 일본·중국·홍콩
아래 섹션별 표가 제공됩니다: **시장 폭** / **시총 상위** / **거래대금 상위** / **특징주** / **섹터 분석**.
각 표에 일본→중국→홍콩 순으로 행이 정렬돼 있습니다.
국가별로 분리해 분석하고, 강세/약세 섹터·대형주 방향성을 서술하세요.
  - 각 국가의 시장 폭(상승비율·ADR)로 광범위 상승 vs 소수 집중 판단
  - 시총/거래대금 상위 대형주 수급·테마 식별
  - 특징주 급등락 배경 및 테마 (반도체·전기차·금융·부동산 등)
  - 해외 → 한국 전이 테마 명시적 연결

{stocks}

---

## 시사점 + 다음 세션 주목 포인트
(일본·중국·홍콩 흐름을 종합해 다음 세션(유럽·미국) 및 내일 한국 개장 시 주목할 포인트를 정리하세요:
  - **해외 → 한국 전이 테마**: 일본·중국·홍콩에서 강세/약세를 보인 섹터·테마와 KOSPI/KOSDAQ 연관 종목 연결
    (예: 일본 반도체 장비주 강세 → SK하이닉스·삼성전자·반도체 장비 중소형주 / 중국 전기차 부품 강세 → 2차전지·소재주 / 홍콩 금융 약세 → 은행·증권 비중 조절)
  - **매크로 경계 포인트**: FX·금리 방향이 KOSPI/KOSDAQ 외국인 수급에 주는 영향
  - **내일 한국 개장 주목 종목/업종** 3~5개를 bullet로 정리)
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
(FX·금리·원자재·변동성을 `| 분류 | 지표 | 종가 | 1D% | 1W% | 1M% | 비고 |` 통합 표로 작성. 비고에 핵심 원인 1줄)

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
        has_prior  = bool(stocks_data.get("prior_briefings"))
        has_europe = bool(stocks_data.get("europe_stocks"))
        session_format = f"""
## Global 시황 브리핑

**핵심 요약** (3~4문장, 미국·유럽·달러·원자재 핵심 드라이버 중심)

---

## 주요 지수 동향
(US와 Europe 지수를 한 표에 작성:
| 지역 | 지수 | 종가 | 1D% | 1W% | 1M% | 비고 |
|------|------|-----:|----:|----:|----:|------|
- US: SPX·NDX·DJIA·Russell2000
- Europe: DAX·FTSE100·CAC40·EuroStoxx50
비고에 당일 핵심 드라이버 1줄)

---

## 매크로
(`| 분류 | 지표 | 종가 | 1D% | 1W% | 1M% | 비고 |` 통합 표 형식으로 작성:
- FX: DXY 반드시 첫 행 → EUR/USD·USD/JPY·USD/CNY·USD/KRW 순. 비고에 강달러 전환 여부 1줄
- 금리: US 10Y·2Y·KR 3Y·JP 10Y
- 원자재: WTI·Brent·Gold — 비고에 원인 1줄씩
- 변동성: VIX·MOVE·VKOSPI)

---

## 종목 분석

### [US Main]
아래 표가 제공됩니다: **[US] 시총+거래대금 상위** / **[US] 급등락+거래대금급증** / **[US] EPS Revision**.
표 형식: `분류 | Ticker | Name | Sector | 종가 | 1D% | 1W% | 1M% | 규모 | 비고`
**비고** 컬럼에 상승/하락 원인·시그널·코멘트를 1줄로 작성하세요.
분석 관점:
  - 시총 상위 대형주 동향 및 거래 집중도
  - 거래대금 급증 특징주의 테마 (AI·반도체·전기차·바이오·은행·에너지 등)
  - EPS Revision: 상향/하향 배경과 주가 반응 1줄

{'### [Europe Sub] DAX · FTSE100 · CAC40' if has_europe else ''}
{'아래 섹션별 표가 제공됩니다: **[Europe] 시장 폭** / **[Europe] STOXX600 섹터 분석** / **[Europe] 시총 상위** / **[Europe] 거래대금 상위** / **[Europe] 급등락+거래대금급증**.' if has_europe else ''}
{'종목표 형식: `분류(지수명) | Ticker | Name | 종가 | 1D% | 1W% | 1M% | 거래대금(B) | 비고`' if has_europe else ''}
{'분석 관점:' if has_europe else ''}
{'  - 시장 폭(상승비율·ADR)으로 DAX/FTSE/CAC 광범위 상승 vs 소수 집중 판단' if has_europe else ''}
{'  - STOXX600 섹터: 강세/약세 섹터 2~3개 집중 분석' if has_europe else ''}
{'  - 시총/거래대금 상위 대형주 수급·방향성 구분 (DAX/FTSE/CAC 시장별)' if has_europe else ''}
{'  - 급등락 특징주 테마 (자동차·럭셔리·에너지·금융·헬스케어 등) 및 미국·아시아 전이 가능성' if has_europe else ''}

{{stocks}}

---

## 경제지표 · 어닝 캘린더

이번주·다음주 시장에 영향을 줄 수 있는 주요 경제지표와 기업실적을 정리합니다.
스냅샷 데이터([주요 경제지표], [SPX 기업실적] 섹션)를 기반으로 아래 형식을 엄수하세요.

**이번주·다음주 주목 포인트** (2~3문장 코멘트: 시장 영향력 큰 이벤트 중심, 이미 발표된 결과의 시사점 포함)

---

### 경제지표

아래 표를 스냅샷 데이터 그대로 재현하세요. 현재치(이미 발표된 경우만 **굵게**), 미발표는 "-".
직전치·예상치·현재치는 숫자만, 단위 변환 없이 원본 그대로.

| 날짜 | 시간(ET) | 중요도 | 지표 | 기간 | 직전치 | 예상치 | 현재치 |
|------|----------|--------|------|------|-------:|-------:|-------:|
(스냅샷 [주요 경제지표] 행을 그대로 삽입)

---

### 어닝 캘린더

아래 표를 스냅샷 데이터 그대로 재현하세요. 실적 발표 완료 종목은 EPS실적·서프라이즈 **굵게**.

| 날짜 | 종목 | 장전/후 | 분기 | EPS예상 | EPS실적 | 서프라이즈 |
|------|------|---------|------|--------:|--------:|-----------:|
(스냅샷 [SPX 기업실적] 행을 그대로 삽입)

---

## 시사점 + 다음 세션 주목 포인트
(미국·유럽 흐름을 종합해 내일 아시아 개장 시 주목할 포인트를 정리하세요:
  - **미국 → 아시아 전이 테마**: 미국 반도체·AI·전기차·에너지 섹터와 한국·일본·중국 연관 종목 구체적으로 연결
  - **유럽 → 아시아 전이 테마**: ASML·자동차·럭셔리·에너지 등 유럽 섹터와 한국·일본 연관 종목 연결
  - **매크로 경계 포인트**: DXY·VIX·금리 방향이 아시아 외국인 수급에 주는 영향
  - **내일 아시아 세션 주목 종목/업종** 3~5개를 bullet로 정리)
""".format(stocks=stocks_section)

    return f"""{context}

아래는 오늘의 시장 데이터입니다.

{snapshot_text}

다음 형식으로 브리핑을 작성하세요.
데이터에 없는 항목은 생략하고, 실제 수치를 근거로 서술하세요.

{session_format}
"""


def _all_chg_zero(stocks: list) -> bool:
    """종목 리스트의 등락률(chg_pct)이 전부 0 또는 None인지 판정."""
    if not stocks:
        return False
    return all(s.get("chg_pct") is None or float(s.get("chg_pct")) == 0.0
               for s in stocks)


def _validate_stocks_data(session: str, target_date: str, stocks_data: dict) -> None:
    """Claude API 호출 전 종목 데이터 무결성 검증 (전 세션·전 시장).

    각 시장의 개별주 그룹이 DB에서 비어있거나 등락률이 전부 0/None인
    (수집 누락·date-fix 실패 등으로 '숫자가 안 나온') 경우 브리핑 생성을
    중단하고 에러를 발생시킨다. 잘못된 데이터로 LLM 토큰을 낭비하지 않기 위함.
    """
    sd = stocks_data or {}
    errors: list[str] = []

    def _check(label: str, stocks: list, require_nonempty: bool = True) -> None:
        if not stocks:
            if require_nonempty:
                errors.append(f"{label}: 종목 데이터 없음")
            return
        if _all_chg_zero(stocks):
            errors.append(
                f"{label}: {len(stocks)}종목 모두 등락률 0/None "
                f"(수집 누락·date-fix 실패 의심)")

    if session == "asia":
        # 한국 (KOSPI + KOSDAQ)
        kr_major = ((sd.get("major") or [])
                    + (sd.get("major_kospi") or [])
                    + (sd.get("major_kosdaq") or []))
        kr_feat  = ((sd.get("featured") or [])
                    + (sd.get("featured_kospi") or [])
                    + (sd.get("featured_kosdaq") or []))
        _check("한국 주요종목(KOSPI·KOSDAQ)", kr_major)
        _check("한국 특징주", kr_feat, require_nonempty=False)
        # 일본·중국·홍콩
        overseas = sd.get("overseas_asia", {})
        for mk, label in (("jp", "일본(Nikkei225)"),
                          ("cn", "중국(CSI300)"),
                          ("hk", "홍콩(HSI)")):
            mk_data = overseas.get(mk, {})
            stocks: list = []
            for sub in ("major", "mktcap_top", "tradeval_top", "featured"):
                stocks.extend(mk_data.get(sub, []) or [])
            _check(label, stocks)

    elif session == "europe":
        eu: list = []
        for sub in ("mktcap_top", "tradeval_top", "top_stocks"):
            eu.extend(sd.get(sub, []) or [])
        _check("유럽 종목(DAX·FTSE·CAC)", eu)
        _check("유럽 거래대금 급증주", sd.get("turnover_surge") or [],
               require_nonempty=False)

    elif session == "us":
        us: list = []
        for sub in ("mktcap_top", "tradeval_top", "top_stocks"):
            us.extend(sd.get(sub, []) or [])
        _check("미국 종목(SPX·NDX)", us)
        _check("미국 거래대금 급증주", sd.get("turnover_surge") or [],
               require_nonempty=False)
        # Europe Sub (US 세션 내 유럽 독립 섹션)
        eu_sub = sd.get("europe_stocks", {})
        eu_stocks: list = []
        for sub in ("mktcap_top", "tradeval_top", "top_stocks"):
            eu_stocks.extend(eu_sub.get(sub, []) or [])
        _check("유럽 종목(US세션 Sub)", eu_stocks, require_nonempty=False)

    if errors:
        detail = " | ".join(errors)
        raise ValueError(
            f"[{session} 종목 데이터 검증 실패] {target_date} DB 데이터 비정상 → "
            f"Claude 브리핑 생성 중단: {detail}")


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

    # Claude API 호출 전 DB 데이터 무결성 검증 (전 세션·전 시장 종목 데이터)
    _validate_stocks_data(session, today, stocks_data)

    print("[llm_briefing] Claude API 호출 중...")
    # Prevent indefinite blocking on network/model latency.
    timeout_secs = float(os.getenv("ANTHROPIC_TIMEOUT_SECS", "180"))
    max_retries = int(os.getenv("ANTHROPIC_MAX_RETRIES", "1"))
    client = anthropic.Anthropic(
        api_key=os.getenv("ANTHROPIC_API_KEY"),
        timeout=timeout_secs,
        max_retries=max_retries,
    )

    has_stocks = bool(stocks_data and any(stocks_data.get(k) for k in
                      ("major", "featured", "sectors", "top_stocks",
                       "mktcap_top", "tradeval_top", "turnover_surge", "europe_stocks")))
    # 운영 브리핑 길이 증가에 맞춰 세션 공통 출력 상한을 확장.
    max_tok = 10000
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
