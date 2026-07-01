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

MODEL = "claude-opus-4-8"

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
            row += f"  ADR {adr:.1f}%"
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
                if adr is not None: seg += f"  ADR {adr:.1f}%"
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

    # ── row 포매터 ───────────────────────────────────────────────────
    # 한국: 시총 컬럼 추가 (거래대금 앞)
    KR_HDR = "| 분류 | Ticker | Name | 종가 | 1D% | 1W% | 1M% | 시총 | 거래대금 | TV5D% | 비고 |"
    KR_SEP = "|------|--------|------|-----:|----:|----:|----:|------:|---------:|------:|------|"
    # 해외 major: 시총+거래대금 통합표
    OV_MAJ_HDR = "| 분류 | Ticker | Name | Sector | 종가 | 1D% | 1W% | 1M% | 시총(B) | 거래대금(B) | TV5D% | 비고 |"
    OV_MAJ_SEP = "|------|--------|------|--------|-----:|----:|----:|----:|--------:|-----------:|------:|------|"
    # 해외 featured: TV5D% 포함
    OV_FEA_HDR = "| 분류 | Ticker | Name | Sector | 종가 | 1D% | 1W% | 1M% | 거래대금(B) | TV5D% | 비고 |"
    OV_FEA_SEP = "|------|--------|------|--------|-----:|----:|----:|----:|-----------:|------:|------|"

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
        mc_v = s.get("mktcap")
        cap  = f"{mc_v/1e12:.1f}조" if mc_v and mc_v >= 1e12 else (f"{mc_v//100_000_000:,}억" if mc_v else "-")
        # TV5D% = 이전 5D 평균 거래대금 대비 변화율
        return (f"| {div} | {s['ticker']} | {s['name'][:12]} | "
                f"{s['close']:,} | {_r(s.get('chg_pct'))} | {_r(s.get('ret_1w'))} | "
                f"{_r(s.get('ret_1m'))} | {cap} | {_tv_kr(s)} | {_r(s.get('tv_chg'),'+.0f')} | {note} |")

    def _row_ov_major(div, s):
        sec = (s.get("sector") or s.get("market") or "-")[:14]
        mc  = f"{s['mktcap_b']:.1f}B" if s.get("mktcap_b") is not None else "-"
        return (f"| {div} | {s['ticker']} | {s['name'][:14]} | {sec} | "
                f"{s['close']:.2f} | {_r(s.get('chg_pct'))} | {_r(s.get('ret_1w'))} | "
                f"{_r(s.get('ret_1m'))} | {mc} | {_tv_ov(s)} | {_r(s.get('tv_chg'),'+.0f')} |  |")

    def _row_ov_feat(div, s):
        sec = (s.get("sector") or s.get("market") or "-")[:14]
        return (f"| {div} | {s['ticker']} | {s['name'][:14]} | {sec} | "
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
        lines.append(KR_HDR); lines.append(KR_SEP); lines.extend(kr_major)

    kr_feat: list[str] = []
    for s in stocks_data.get("featured_kospi",  []): kr_feat.append(_row_kr("KOSPI",  s))
    for s in stocks_data.get("featured_kosdaq", []): kr_feat.append(_row_kr("KOSDAQ", s))
    if not kr_feat:
        for s in stocks_data.get("featured", []): kr_feat.append(_row_kr(s.get("market", "KR"), s))
    if kr_feat:
        lines.append("\n## [한국] 특징주 — 급등락+거래대금급증")
        lines.append(KR_HDR); lines.append(KR_SEP); lines.extend(kr_feat)

    # ════════════════════════════════════════
    # [Sub] 일본·중국·홍콩 — 섹션별 통합표
    # ════════════════════════════════════════

    # 시장 폭
    brd_rows: list[str] = []
    for mk, lbl in mk_pairs:
        b = overseas.get(mk, {}).get("breadth", {})
        if b:
            up_pct = f"{b['up_pct']:.1f}%" if b.get("up_pct") is not None else "-"
            adr    = f"{b['adr']:.1f}%"      if b.get("adr")    is not None else "-"
            wch    = f"{b['weighted_chg']:+.2f}%" if b.get("weighted_chg") is not None else "-"
            brd_rows.append(
                f"| {lbl} | {b.get('up',0)}/{b.get('total',0)} | {up_pct} | {adr} | {wch} |")
    if brd_rows:
        lines.append("\n## [Sub] 시장 폭 — 일본·중국·홍콩")
        lines.append("| 시장 | 상승/전체 | 상승% | ADR(20일) | 거래대금가중등락 |")
        lines.append("|------|:---------:|------:|----:|----------------:|")
        lines.extend(brd_rows)

    # 시총+거래대금 상위 합산 (major 리스트 — 시총순위+거래대금순위 합산 스코어)
    maj_rows: list[str] = []
    for mk, lbl in mk_pairs:
        for s in overseas.get(mk, {}).get("major", []):
            maj_rows.append(_row_ov_major(lbl, s))
    if maj_rows:
        lines.append("\n## [Sub] 시총+거래대금 상위 — 일본·중국·홍콩")
        lines.append(OV_MAJ_HDR); lines.append(OV_MAJ_SEP); lines.extend(maj_rows)

    # 특징주 (TV5D% 포함)
    ov_feat: list[str] = []
    for mk, lbl in mk_pairs:
        for s in overseas.get(mk, {}).get("featured", []):
            ov_feat.append(_row_ov_feat(lbl, s))
    if ov_feat:
        lines.append("\n## [Sub] 특징주 — 일본·중국·홍콩")
        lines.append(OV_FEA_HDR); lines.append(OV_FEA_SEP); lines.extend(ov_feat)

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
        idx  = s.get("index") or s.get("market") or "-"
        dv   = f"{s['dollar_vol_b']:.2f}" if s.get("dollar_vol_b") else "-"
        cl   = s.get("close")
        cl_s = f"{cl:.2f}" if cl is not None else "-"
        return (f"| {idx} | {s['ticker']} | {s.get('name', s['ticker'])[:20]} | "
                f"{cl_s} | {_r(s.get('chg_pct'))} | {_r(s.get('ret_1w'))} | "
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
        lines.append("| 섹터 | ETF | 종가 | 1D% | 1W% | 1M% |")
        lines.append("|------|-----|-----:|----:|----:|----:|")
        for s in sectors:
            chg = f"{s['chg_pct']:+.2f}%" if s.get("chg_pct") is not None else "-"
            r1w = f"{s['ret_1w']:+.2f}%" if s.get("ret_1w") is not None else "-"
            r1m = f"{s['ret_1m']:+.2f}%" if s.get("ret_1m") is not None else "-"
            lines.append(f"| {s['name']} | {s['ticker']} | {s['close']:,.2f} | {chg} | {r1w} | {r1m} |")

    # ════════════════════════════════════════
    # [US Main]
    # ════════════════════════════════════════
    US_HDR = "| 분류 | Ticker | Name | 종가 | 1D% | 1W% | 1M% | 시총(B) | 거래대금(B) | 비고 |"
    US_SEP = "|------|--------|------|-----:|----:|----:|----:|--------:|-----------:|------|"
    SG_HDR = "| 분류 | Ticker | Name | 종가 | 1D% | 1W% | 1M% | 시총(B) | 거래대금(B) | 5일평균(B) | 변화율(%) | 비고 |"
    SG_SEP = "|------|--------|------|-----:|----:|----:|----:|--------:|-----------:|-----------:|----------:|------|"

    # 시총+거래대금 상위
    us_major: list[str] = []
    for s in mktcap[:10]:
        mc  = f"{s['mktcap_b']:.0f}" if s.get("mktcap_b") else "-"
        dv  = f"{s['dollar_vol_b']:.1f}" if s.get("dollar_vol_b") else "-"
        us_major.append(f"| 시총상위 | {s['ticker']} | {s.get('name', s['ticker'])[:18]} | "
                        f"{s['close']:.2f} | {_r(s.get('chg_pct'))} | {_r(s.get('ret_1w'))} | "
                        f"{_r(s.get('ret_1m'))} | {mc} | {dv} |  |")
    for s in tradeval[:10]:
        mc  = f"{s['mktcap_b']:.0f}" if s.get("mktcap_b") else "-"
        dv  = f"{s['dollar_vol_b']:.1f}" if s.get("dollar_vol_b") else "-"
        us_major.append(f"| 거래대금상위 | {s['ticker']} | {s.get('name', s['ticker'])[:18]} | "
                        f"{s['close']:.2f} | {_r(s.get('chg_pct'))} | {_r(s.get('ret_1w'))} | "
                        f"{_r(s.get('ret_1m'))} | {mc} | {dv} |  |")
    if us_major:
        lines.append("\n## [US] 시총+거래대금 상위")
        lines.append(US_HDR); lines.append(US_SEP); lines.extend(us_major)

    # 급등락+거래대금급증
    if surge:
        lines.append("\n## [US] 급등락+거래대금급증")
        lines.append(SG_HDR); lines.append(SG_SEP)
        for s in surge[:10]:
            mc   = f"{s['mktcap_b']:.0f}" if s.get("mktcap_b") else "-"
            dv   = f"{s['dollar_vol_b']:.2f}" if s.get("dollar_vol_b") else "-"
            avg5 = f"{s['avg_dvol_b']:.2f}" if s.get("avg_dvol_b") else "-"
            tvc  = f"{s['tv_chg_pct']:+.1f}" if s.get("tv_chg_pct") is not None else "-"
            lines.append(f"| 특징주 | {s['ticker']} | {s.get('name', s['ticker'])[:18]} | "
                         f"{s['close']:.2f} | {_r(s.get('chg_pct'))} | {_r(s.get('ret_1w'))} | "
                         f"{_r(s.get('ret_1m'))} | {mc} | {dv} | {avg5} | {tvc} |  |")

    # EPS Revision
    if eps_rev:
        lines.append("\n## [US] EPS Revision")
        lines.append("| 분류 | Ticker | Name | 1D% | 1W% | 1M% | EPS변화(1M) | EPS변화(3M) | 비고 |")
        lines.append("|------|--------|------|----:|----:|----:|------------:|------------:|------|")
        for s in eps_rev:
            chg = _r(s.get("chg_pct"))
            r1w = (_r(s.get("ret_1w")) if s.get("ret_1w") is not None else
                   (_r(s.get("return_7d")) if s.get("return_7d") is not None else "-"))
            r1m = _r(s.get("ret_1m"))
            e1m = f"{s['eps_chg_1m']:+.2f}%" if s.get("eps_chg_1m") is not None else "-"
            e3m = f"{s['eps_chg_3m']:+.2f}%" if s.get("eps_chg_3m") is not None else "-"
            lines.append(f"| EPS | {s['ticker']} | {s.get('name', s['ticker'])[:18]} | "
                         f"{chg} | {r1w} | {r1m} | {e1m} | {e3m} |  |")

    # ════════════════════════════════════════
    # [Sub] DAX·FTSE·CAC
    # ════════════════════════════════════════
    if europe:
        eu_breadth  = europe.get("breadth", {})
        eu_sectors  = europe.get("sectors", [])
        eu_mktcap   = europe.get("mktcap_top", [])
        eu_tradeval = europe.get("tradeval_top", [])
        eu_surge    = europe.get("turnover_surge", [])

        EU_HDR = "| 분류(지수) | Ticker | Name | 종가 | 1D% | 1W% | 1M% | 시총(B) | 거래대금(B) | 5D평균(B) | 변화율(%) | 비고 |"
        EU_SEP = "|-----------|--------|------|-----:|----:|----:|----:|--------:|-----------:|----------:|----------:|------|"

        def _row_eu(div, s, note=""):
            mc   = f"{s['mktcap_b']:.1f}"    if s.get("mktcap_b")    else "-"
            dv   = f"{s['dollar_vol_b']:.2f}" if s.get("dollar_vol_b") else "-"
            avg5 = f"{s['avg_dvol_b']:.2f}"   if s.get("avg_dvol_b")   else "-"
            tvc  = f"{s['tv_chg_pct']:+.1f}"  if s.get("tv_chg_pct") is not None else "-"
            cl   = s.get("close")
            cl_s = f"{cl:.2f}" if cl is not None else "-"
            return (f"| {div} | {s['ticker']} | {s.get('name', s['ticker'])[:20]} | "
                    f"{cl_s} | {_r(s.get('chg_pct'))} | {_r(s.get('ret_1w'))} | "
                    f"{_r(s.get('ret_1m'))} | {mc} | {dv} | {avg5} | {tvc} | {note or s.get('signal', '')} |")

        # 시장 폭 (전체 유럽 통합)
        if eu_breadth:
            up     = eu_breadth.get("up", 0)
            down   = eu_breadth.get("down", 0)
            total  = eu_breadth.get("total", 0)
            up_pct = f"{eu_breadth['up_pct']:.1f}%" if eu_breadth.get("up_pct") is not None else "-"
            adr    = f"{eu_breadth['adr']:.1f}%"     if eu_breadth.get("adr")    is not None else "-"
            lines.append("\n## [Sub] 시장 폭 — DAX·FTSE100·CAC40")
            lines.append("| 시장 | 상승/전체 | 상승% | ADR(20일) |")
            lines.append("|------|:---------:|------:|----:|")
            lines.append(f"| DAX·FTSE·CAC (합산) | {up}/{total} | {up_pct} | {adr} |")

        # STOXX600 섹터 분석
        if eu_sectors:
            lines.append("\n## [Sub] STOXX600 섹터 분석")
            lines.append("| 섹터 | 종가 | 1D% | 1W% | 1M% |")
            lines.append("|------|-----:|----:|----:|----:|")
            for s in eu_sectors:
                chg = f"{s['chg_pct']:+.2f}%" if s.get("chg_pct") is not None else "-"
                r1w = f"{s['ret_1w']:+.2f}%" if s.get("ret_1w") is not None else "-"
                r1m = f"{s['ret_1m']:+.2f}%" if s.get("ret_1m") is not None else "-"
                lines.append(f"| {s['name']} | {s['close']:,.2f} | {chg} | {r1w} | {r1m} |")

        # 시총·거래대금 상위 합산 (중복 제거, signal=시총상위/거래대금상위 → 비고)
        if eu_mktcap or eu_tradeval:
            lines.append("\n## [Sub] 시총·거래대금 상위 — DAX·FTSE100·CAC40")
            lines.append(EU_HDR); lines.append(EU_SEP)
            _seen_eu: set = set()
            for s in (eu_mktcap + eu_tradeval):
                if s["ticker"] not in _seen_eu:
                    idx = s.get("index") or s.get("market") or "-"
                    lines.append(_row_eu(idx, s))
                    _seen_eu.add(s["ticker"])

        # 급등락+거래대금급증 (비고는 LLM이 채움)
        if eu_surge:
            lines.append("\n## [Sub] 급등락+거래대금급증 — DAX·FTSE100·CAC40")
            lines.append(EU_HDR); lines.append(EU_SEP)
            for s in eu_surge:
                idx = s.get("index") or s.get("market") or "-"
                lines.append(_row_eu(idx, {**s, "signal": ""}, ""))

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
⚠️ 스냅샷 [매크로] 섹션의 표를 **그대로 재현**하고 **비고** 컬럼에만 핵심 원인·시사점을 1줄 추가하세요.
헤더·컬럼 수·순서·수치·단위를 절대 변경하지 마세요 (금리는 스냅샷에 bp로 이미 표기됨).

---

## 종목 분석

⚠️ **표 재현 규칙 (반드시 준수)**: 아래 데이터 섹션에 완성된 마크다운 표가 제공됩니다.
각 표를 한 글자도 바꾸지 말고 그대로 복사하고, **비고** 컬럼에만 1줄 코멘트를 추가하세요.
필수 컬럼 체크리스트 (누락·추가·이름 변경 절대 금지):
- [한국] 시총+거래대금 상위·특징주: `분류|Ticker|Name|종가|1D%|1W%|1M%|시총|거래대금|TV5D%|비고`
- [해외 Sub] 시총+거래대금 상위: `분류|Ticker|Name|Sector|종가|1D%|1W%|1M%|시총(B)|거래대금(B)|TV5D%|비고`
- [해외 Sub] 특징주: `분류|Ticker|Name|Sector|종가|1D%|1W%|1M%|거래대금(B)|TV5D%|비고`

{stocks}

### 종목 분석 관점
**[한국 Main]** 비고에 외인·기관 수급 반드시 포함 / KOSPI+KOSDAQ 시장 폭(ADR ≥120% 과매수·강세 / 80~120% 중립 / <80% 과매도·약세) / KOSPI vs KOSDAQ 자금 방향 / 급등락 특징주 테마

**[Sub 일본·중국·홍콩]** 국가별 분리 분석 / 각국 시장 폭(ADR 기준 동일) / 시총·거래대금 상위 수급·테마 / 특징주 급등락 배경 / 해외 → 한국 전이 테마 명시

---

## 글로벌 세션(유럽·미국) 시사점
(아시아 시장 흐름을 종합해 당일 유럽·미국 세션에서 주목할 포인트를 정리하세요:
  - **아시아 → 유럽 전이 테마**: 한국·일본에서 강세/약세를 보인 섹터·테마와 유럽 연관 종목 연결
    (예: 반도체·자동차 방향성 → ASML·LVMH·BMW / 에너지 흐름 → Shell·TotalEnergies)
  - **아시아 → 미국 전이 테마**: KOSPI/KOSDAQ·Nikkei 흐름과 미국 빅테크·반도체·소재 연관 종목 연결
    (예: SK하이닉스·삼성전자 방향성 → NVDA·AMAT / 2차전지 강세 → TSLA·리튬 공급망)
  - **매크로 경계 포인트**: FX(원/달러·엔/달러)·금리 방향이 미국 위험자산 수급에 주는 영향
  - **오늘 유럽·미국 세션 주목 종목/업종** 3~5개를 bullet로 정리)
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
(강세/약세 섹터 — 표로 작성, 2~3개 집중. DAX/FTSE/CAC 시장 폭 데이터를 참고해 광범위 상승 vs 소수 집중 여부 1문장 분석.
 ADR 해석: ≥120% 과매수/광범위 강세 / 80~120% 중립 / <80% 과매도/광범위 약세)

---

## 매크로
⚠️ 스냅샷 [매크로] 섹션의 표를 **그대로 재현**하고 **비고** 컬럼에만 핵심 원인 1줄 추가.
헤더·컬럼 수·순서·수치·단위를 절대 변경하지 마세요 (금리는 스냅샷에 bp로 이미 표기됨).

---

⚠️ **표 재현 규칙 (반드시 준수)**: 아래 데이터 섹션에 완성된 마크다운 표가 제공됩니다.
각 표를 한 글자도 바꾸지 말고 그대로 복사하고, **비고** 컬럼에만 1줄 코멘트를 추가하세요.
필수 컬럼 체크리스트 (누락·추가·이름 변경 절대 금지):
- 유럽 시총·거래대금·급등락: `분류(지수)|Ticker|Name|종가|1D%|1W%|1M%|시총(B)|거래대금(B)|5D평균(B)|변화율(%)|비고`

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
⚠️ 스냅샷 [매크로] 섹션의 표를 **그대로 재현**하고 **비고** 컬럼에만 핵심 원인 1줄 추가.
헤더·컬럼 수·순서·수치·단위를 절대 변경하지 마세요 (금리는 스냅샷에 bp로 이미 표기됨).
DXY를 FX 첫 행에 배치하고, FX→금리→원자재→변동성 순서를 유지하세요.

---

## 종목 분석

⚠️ **표 재현 규칙 (반드시 준수)**: 아래 데이터 섹션에 완성된 마크다운 표가 제공됩니다.
각 표를 한 글자도 바꾸지 말고 그대로 복사하고, **비고** 컬럼에만 1줄 코멘트를 추가하세요.
필수 컬럼 체크리스트 (누락·추가·이름 변경 절대 금지):
- SPX 섹터 ETF: `섹터|ETF|종가|1D%|1W%|1M%` (비고 없음)
- STOXX600 섹터: `섹터|종가|1D%|1W%|1M%` (비고 없음)
- [US] 시총+거래대금 상위: `분류|Ticker|Name|종가|1D%|1W%|1M%|시총(B)|거래대금(B)|비고`
- [US] 급등락+거래대금급증: `분류|Ticker|Name|종가|1D%|1W%|1M%|시총(B)|거래대금(B)|5일평균(B)|변화율(%)|비고`
- [US] EPS Revision: `분류|Ticker|Name|1D%|1W%|1M%|EPS변화(1M)|EPS변화(3M)|비고`
- [Sub] 시총·거래대금 상위(합산): `분류(지수)|Ticker|Name|종가|1D%|1W%|1M%|시총(B)|거래대금(B)|5D평균(B)|변화율(%)|비고`
- [Sub] 급등락+거래대금급증: `분류(지수)|Ticker|Name|종가|1D%|1W%|1M%|시총(B)|거래대금(B)|5D평균(B)|변화율(%)|비고`

{{stocks}}

### 종목 분석 관점
**[US Main]** S&P500 vs Nasdaq 자금 집중 방향 / 섹터 ETF 방어주 vs 성장주 흐름 / 거래대금 급증 특징주 테마 (AI·반도체·전기차·바이오·은행) / EPS Revision 상향·하향 배경과 주가 반응

{'**[Sub]** 시장 폭(상승비율·ADR 20일)으로 광범위 vs 소수 집중 판단 (ADR ≥120% 과매수·강세 / 80~120% 중립 / <80% 과매도·약세) / STOXX600 강세·약세 섹터 2~3개 집중 / DAX·FTSE·CAC 지수별 수급·방향성 / 급등락 특징주 테마 및 미국·아시아 전이 가능성' if has_europe else ''}

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

## 아시아 세션 시사점
(미국·유럽 흐름을 종합해 내일 아시아 개장 시 주목할 포인트를 정리하세요:
  - **미국 → 아시아 전이 테마**: 미국 반도체·AI·전기차·에너지 섹터와 한국·일본·중국 연관 종목 구체적으로 연결
    (예: NVDA·AMAT 방향성 → SK하이닉스·삼성전자·반도체 장비 중소형주 / TSLA·리튬 → 2차전지·소재주)
  - **유럽 → 아시아 전이 테마**: ASML·자동차·럭셔리·에너지 등 유럽 섹터와 한국·일본 연관 종목 연결
    (예: 유럽 자동차 약세 → 현대차·기아·자동차 부품주 / ASML 흐름 → 반도체 장비 섹터)
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

    이상 감지 시 Slack 경고를 보내고 계속 진행한다 (브리핑은 중단하지 않음).
    휴장일인 마켓은 데이터가 비어있어도 검증을 스킵한다.
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
        # 한국 (KOSPI + KOSDAQ) — XKRX 휴장일이면 스킵
        try:
            from db.db_manager import DBManager as _DBMKR
            _kr_holiday = _DBMKR().is_market_holiday(target_date, "kr")
        except Exception:
            _kr_holiday = False
        if _kr_holiday:
            print(f"[INFO][validation] 한국(KOSPI·KOSDAQ): 휴장일 ({target_date}) → 검증 스킵")
        else:
            kr_major = ((sd.get("major") or [])
                        + (sd.get("major_kospi") or [])
                        + (sd.get("major_kosdaq") or []))
            kr_feat  = ((sd.get("featured") or [])
                        + (sd.get("featured_kospi") or [])
                        + (sd.get("featured_kosdaq") or []))
            _check("한국 주요종목(KOSPI·KOSDAQ)", kr_major)
            _check("한국 특징주", kr_feat, require_nonempty=False)
        # 일본·중국·홍콩 — 휴장일 스킵 또는 전종목 0%는 경고만
        overseas = sd.get("overseas_asia", {})
        for mk, label in (("jp", "일본(Nikkei225)"),
                          ("cn", "중국(CSI300)"),
                          ("hk", "홍콩(HSI)")):
            mk_data = overseas.get(mk, {})
            stocks: list = []
            for sub in ("major", "mktcap_top", "tradeval_top", "featured"):
                stocks.extend(mk_data.get(sub, []) or [])
            # 데이터 없음 = 휴장일 스킵이거나 수집 실패 → DB로 구분
            if not stocks:
                try:
                    from db.db_manager import DBManager as _DBMV
                    if _DBMV().is_market_holiday(target_date, mk):
                        print(f"[INFO][validation] {label}: 휴장일 ({target_date}) → 검증 스킵")
                        continue
                except Exception:
                    pass
                errors.append(f"{label}: 종목 데이터 없음 (수집 누락 의심)")
            elif _all_chg_zero(stocks):
                print(f"[WARN][validation] {label}: {len(stocks)}종목 모두 등락률 0/None "
                      f"(휴장일 또는 date-fix 실패 의심)")

    elif session == "europe":
        eu: list = []
        for sub in ("mktcap_top", "tradeval_top", "top_stocks"):
            eu.extend(sd.get(sub, []) or [])
        if not eu:
            try:
                from db.db_manager import DBManager as _DBMEU
                if _DBMEU().is_market_holiday(target_date, "eu"):
                    print(f"[INFO][validation] 유럽(DAX·FTSE·CAC): 휴장일 ({target_date}) → 검증 스킵")
                else:
                    errors.append("유럽 종목(DAX·FTSE·CAC): 종목 데이터 없음 (수집 누락 의심)")
            except Exception:
                errors.append("유럽 종목(DAX·FTSE·CAC): 종목 데이터 없음 (수집 누락 의심)")
        else:
            _check("유럽 종목(DAX·FTSE·CAC)", eu)
        _check("유럽 거래대금 급증주", sd.get("turnover_surge") or [],
               require_nonempty=False)

    elif session == "us":
        us: list = []
        for sub in ("mktcap_top", "tradeval_top", "top_stocks"):
            us.extend(sd.get(sub, []) or [])
        if not us:
            try:
                from db.db_manager import DBManager as _DBMUS
                if _DBMUS().is_market_holiday(target_date, "us"):
                    print(f"[INFO][validation] 미국(SPX·NDX): 휴장일 ({target_date}) → 검증 스킵")
                else:
                    errors.append("미국 종목(SPX·NDX): 종목 데이터 없음 (수집 누락 의심)")
            except Exception:
                errors.append("미국 종목(SPX·NDX): 종목 데이터 없음 (수집 누락 의심)")
        else:
            _check("미국 종목(SPX·NDX)", us)
        _check("미국 거래대금 급증주", sd.get("turnover_surge") or [],
               require_nonempty=False)
        # Europe Sub (US 세션 내 유럽 독립 섹션) — 유럽 휴장이면 없어도 무방
        eu_sub = sd.get("europe_stocks", {})
        eu_stocks: list = []
        for sub in ("mktcap_top", "tradeval_top", "top_stocks"):
            eu_stocks.extend(eu_sub.get(sub, []) or [])
        _check("유럽 종목(US세션 Sub)", eu_stocks, require_nonempty=False)

    if errors:
        detail = " | ".join(errors)
        msg = (f"⚠️ *[{session.upper()} 종목 데이터 이상]* {target_date}\n"
               f"{detail}\n브리핑은 가용 데이터로 계속 생성합니다.")
        print(f"[WARN][validation] {msg}")
        try:
            from summarize.notify_slack import send_text
            send_text(msg)
        except Exception:
            pass


def generate_briefing(session: str, target_date: str = None,
                      save: bool = True, stocks_data: dict = None,
                      save_as: str = None) -> str:
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
                       "mktcap_top", "tradeval_top", "turnover_surge", "europe_stocks",
                       "overseas_asia", "major_kospi", "major_kosdaq")))
    # Asia: 한국+해외 3개국 전체 종목 테이블로 출력이 크므로 별도 확장.
    max_tok = 16000 if session == "asia" else 12000
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
    stop_reason   = message.stop_reason

    print(f"[llm_briefing] 완료: {prompt_tokens}+{output_tokens} tokens  stop={stop_reason}")
    if stop_reason == "max_tokens":
        warn_msg = (f"⚠️ *[{session.upper()} 브리핑 토큰 한도 초과]* {today}\n"
                    f"출력이 {output_tokens}/{max_tok} 토큰에서 잘렸습니다. max_tokens 증가 필요.")
        print(f"[llm_briefing][WARN] {warn_msg}")
        try:
            from summarize.notify_slack import send_ops
            send_ops(warn_msg)
        except Exception:
            pass

    if save:
        db = DBManager()
        row_id = db.save_briefing(
            date=today,
            session=save_as or session,
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
