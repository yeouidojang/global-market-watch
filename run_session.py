"""
수동 원샷 실행 — 특정 세션 전체 파이프라인 실행

사용법:
    python run_session.py --session asia
    python run_session.py --session global --date 2026-06-19
    python run_session.py --session global --no-notify   # Slack 발송 생략
    python run_session.py --session global --no-llm      # 브리핑 생략, 데이터 수집만
"""

import sys, io
if hasattr(sys.stdout, 'buffer') and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import ssl, httpx
ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE
_orig_c = httpx.Client.__init__
_orig_a = httpx.AsyncClient.__init__
def _fix_proxy(kw):
    kw.setdefault('verify', False)
    if 'proxies' in kw:
        proxies = kw.pop('proxies')
        if isinstance(proxies, dict):
            url = next((v for v in proxies.values() if v), None)
            if url:
                kw.setdefault('proxy', httpx.Proxy(url))
        elif proxies is not None:
            kw.setdefault('proxy', proxies)
    if 'proxy' in kw and isinstance(kw['proxy'], dict):
        url = next((v for v in kw['proxy'].values() if v), None)
        kw['proxy'] = httpx.Proxy(url) if url else None
def _pc(self, *a, **kw): _fix_proxy(kw); _orig_c(self, *a, **kw)
def _pa(self, *a, **kw): _fix_proxy(kw); _orig_a(self, *a, **kw)
httpx.Client.__init__ = _pc
httpx.AsyncClient.__init__ = _pa
import urllib3; urllib3.disable_warnings()

import sys
import time
import argparse
import pandas as pd
from datetime import date, timedelta, datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

KST = timezone(timedelta(hours=9))


def _prev_weekday(d: date) -> date:
    d -= timedelta(days=1)
    while d.weekday() >= 5:   # 5=토, 6=일
        d -= timedelta(days=1)
    return d


def _default_date(session: str) -> str:
    """세션별 기본 target_date 반환.

    us/europe/global 세션은 KST 당일 오전(< 12시)에 실행될 때
    전일 시장(= 전 영업일)을 분석 대상으로 삼는다.
    예) 2026-06-11 06:10 KST 실행 → US 시장 마감일 2026-06-10
    """
    now_kst = datetime.now(KST)
    today   = now_kst.date()
    if session in ("us", "europe", "global") and now_kst.hour < 12:
        return _prev_weekday(today).strftime("%Y-%m-%d")
    return today.strftime("%Y-%m-%d")

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
sys.path.insert(0, str(BASE_DIR))


class _OpsTracker:
    """파이프라인 실행 결과를 수집해 ops 채널 메시지로 포맷."""

    SESSION_ICON = {"asia": "🌏", "global": "🌐", "europe": "🇪🇺", "us": "🇺🇸"}

    def __init__(self, session: str, target_date: str):
        self.session    = session
        self.date       = target_date
        self._t0        = time.time()
        self._steps: list[tuple[str, str, str]] = []  # (icon, label, detail)

    # ── 단계 기록 ────────────────────────────────────────────
    def ok(self, label: str, detail: str = ""):
        self._steps.append(("✅", label, detail))

    def warn(self, label: str, detail: str = ""):
        self._steps.append(("⚠️", label, detail))

    def err(self, label: str, detail: str = ""):
        self._steps.append(("❌", label, detail))

    # ── 경과 시간 ─────────────────────────────────────────────
    def _elapsed(self) -> str:
        s = int(time.time() - self._t0)
        m, s = divmod(s, 60)
        return f"{m}분 {s}초" if m else f"{s}초"

    # ── 메시지 포맷 ──────────────────────────────────────────
    def _header(self, status: str) -> str:
        icon = self.SESSION_ICON.get(self.session, "📊")
        return (f"{status} {icon} *[{self.session.upper()}] "
                f"{self.date} 파이프라인* ({self._elapsed()})")

    def format_success(self) -> str:
        lines = [self._header("✅")]
        for ico, label, detail in self._steps:
            lines.append(f"  {ico} {label}" + (f"  _({detail})_" if detail else ""))
        return "\n".join(lines)

    def format_failure(self, exc: Exception, tb: str) -> str:
        lines = [self._header("❌")]
        for ico, label, detail in self._steps:
            lines.append(f"  {ico} {label}" + (f"  _({detail})_" if detail else ""))
        lines.append(f"\n`{type(exc).__name__}: {str(exc)[:300]}`")
        tail = tb.strip().splitlines()
        lines.append("```" + "\n".join(tail[-12:]) + "```")
        return "\n".join(lines)

    def format_data_error(self, block_errors: list[str]) -> str:
        """핵심 수집 블록 오류로 LLM 스킵됐을 때 포맷."""
        lines = [self._header("⚠️")]
        for ico, label, detail in self._steps:
            lines.append(f"  {ico} {label}" + (f"  _({detail})_" if detail else ""))
        lines.append(f"\n*데이터 수집 오류 — LLM 브리핑 스킵*")
        for e in block_errors:
            lines.append(f"  • {e}")
        return "\n".join(lines)

    # ── 발송 ─────────────────────────────────────────────────
    def send(self, text: str):
        try:
            from summarize.notify_slack import send_ops
            send_ops(text)
        except Exception as _e:
            print(f"[ops] 발송 실패: {_e}")


def _run_collect_block(name: str, fn, *args, ops=None, ops_label: str = "", **kwargs):
    """데이터 수집 블록 단위 실행.

    Returns (result, error_str | None).
    오류 시 ops에 기록하고 None 반환 — 다음 블록은 계속 실행.
    """
    try:
        result = fn(*args, **kwargs)
        return result, None
    except Exception as e:
        err_msg = f"{type(e).__name__}: {str(e)[:160]}"
        label = ops_label or name
        if ops:
            ops.err(label, err_msg[:100])
        print(f"  [{name} ERROR] {e}")
        return None, f"[{name}] {err_msg}"


def _archive_briefing_text(session: str, target_date: str, briefing_id: int, content: str) -> Path:
    """브리핑 원문을 TXT 파일로 보관하고 저장 경로를 반환."""
    out_dir = BASE_DIR / "logs" / "briefings_txt" / session
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{target_date}_id{briefing_id}.txt"
    out_path.write_text(content or "", encoding="utf-8")
    return out_path


def run_session(session: str, target_date: str,
                skip_llm: bool = False, skip_notify: bool = False,
                ops: "_OpsTracker | None" = None):

    print(f"\n{'='*55}")
    print(f"  Global Market Watch  |  {session.upper()}  |  {target_date}")
    print(f"{'='*55}")

    # Step 1: 데이터 수집
    import yaml
    from exe.collect_macro import (
        load_yaml, lseg_open, lseg_close,
        collect_indices, collect_macro, DBManager
    )

    indices_cfg = load_yaml("indices.yaml")
    macro_cfg   = load_yaml("macro.yaml")
    db = DBManager()

    # ── [1/4] 데이터 수집 — 블록별 독립 실행 ─────────────────────
    print(f"\n[1/4] 데이터 수집 (session={session})")
    _core_errors: list[str] = []   # 핵심 블록(지수·매크로) 오류 → LLM 게이트

    # Block A: 지수 수집 (핵심)
    try:
        lseg_open()
        _lseg_ok = True
    except Exception as _e:
        _lseg_ok = False
        _core_errors.append(f"[LSEG 세션] {_e}")
        if ops: ops.err("[수집] LSEG 세션", str(_e)[:80])

    try:
        n_idx, _err = _run_collect_block(
            "지수", collect_indices, session, target_date, indices_cfg, db,
            ops=ops, ops_label="[수집] 지수"
        )
        if _err:
            _core_errors.append(_err)
        else:
            print(f"  지수 upserted: {n_idx}건")
            if ops: ops.ok("[수집] 지수", f"{n_idx}건")

        # Block B: 매크로 수집 (핵심)
        n_mac, _err = _run_collect_block(
            "매크로", collect_macro, target_date, macro_cfg, db,
            ops=ops, ops_label="[수집] 매크로"
        )
        if _err:
            _core_errors.append(_err)
        else:
            print(f"  매크로 upserted: {n_mac}건")
            if ops: ops.ok("[수집] 매크로", f"{n_mac}건")
    finally:
        if _lseg_ok:
            lseg_close()

    # Block C: SPX OHLCV (핵심 — US 종목 스크리닝 필수)
    if session == "us":
        print(f"\n[1/4-spx] SPX 전 종목 OHLCV 수집 (yfinance → us_stocks_daily)")
        _, _err = _run_collect_block(
            "SPX OHLCV", lambda: __import__("exe.collect_us_stocks", fromlist=["run"]).run(target_date, target_date),
            ops=ops, ops_label="[수집] SPX OHLCV"
        )
        if _err:
            _core_errors.append(_err)
        elif ops:
            ops.ok("[수집] SPX OHLCV")

    # ── 핵심 블록 오류 게이트 ────────────────────────────────────
    if _core_errors:
        _err_lines = "\n".join(f"  • {e}" for e in _core_errors)
        print(f"\n⚠️ 핵심 수집 블록 오류 {len(_core_errors)}개 — LLM 브리핑 스킵")
        print(_err_lines)
        if ops:
            ops.warn("[LLM] 브리핑", f"수집 오류로 스킵 ({len(_core_errors)}블록)")
            ops.send(ops.format_data_error(_core_errors))
        return

    # ── [2/4] 경제지표·어닝 캘린더 (보조 — 오류 시 warn, LLM 계속) ──
    if session == "us":
        print(f"\n[2/4] 경제지표 + 어닝 캘린더 수집 (이번주 월요일 ~ 다음주 금요일)")
        _mon = (pd.Timestamp(target_date) - pd.Timedelta(days=pd.Timestamp(target_date).weekday())).strftime("%Y-%m-%d")
        _fri = (pd.Timestamp(_mon) + pd.Timedelta(days=11)).strftime("%Y-%m-%d")

        _n_econ, _err = _run_collect_block(
            "경제지표", lambda: db.upsert_econ_event(
                __import__("exe.collect_econ_cal", fromlist=["collect_econ_calendar"]).collect_econ_calendar(from_date=_mon, to_date=_fri)
            ), ops=ops, ops_label="[수집] 경제지표"
        )
        if _err:
            if ops: ops.warn("[수집] 경제지표", "오류 — 스킵")
        else:
            print(f"  경제지표 upserted: {_n_econ}건")
            if ops: ops.ok("[수집] 경제지표", f"{_n_econ}건")

        _n_earn, _err = _run_collect_block(
            "어닝캘린더", lambda: db.upsert_earnings_event(
                __import__("exe.collect_earnings_cal", fromlist=["collect_earnings_calendar"]).collect_earnings_calendar(from_date=_mon, to_date=_fri, filter_spx=True)
            ), ops=ops, ops_label="[수집] 어닝"
        )
        if _err:
            if ops: ops.warn("[수집] 어닝", "오류 — 스킵")
        else:
            print(f"  어닝 upserted: {_n_earn}건")
            if ops: ops.ok("[수집] 어닝", f"{_n_earn}건")
    else:
        print(f"\n[2/4] 경제지표 캘린더 수집 (skip: {session} — global 파이프라인에서 일괄 수집)")

    # Step 3: LLM 브리핑
    if skip_llm:
        print(f"\n[3/4] LLM 브리핑 (skip: --no-llm)")
        if ops: ops.warn("[3] LLM 브리핑", "skip (--no-llm)")
        return

    # 한국 세션이면 주요 종목 / 특징주 수집
    stocks_data = {}
    if session == "asia":
        print(f"\n[3/4-pre] KOSPI/KOSDAQ 종목 수집")
        from exe.collect_stocks import fetch_top_stocks, fetch_asia_overseas_stocks
        stocks_data = fetch_top_stocks(target_date)
        _n_major = len(stocks_data.get('major', []))
        _n_feat  = len(stocks_data.get('featured', []))
        print(f"  주요종목 {_n_major}개  특징주 {_n_feat}개  "
              f"수급데이터 {len(stocks_data.get('investor_flow', []))}개")

        # 일본 / 중국 / 홍콩 — LSEG 구성종목 분석
        print(f"\n[3/4-pre2] 일본·중국·홍콩 구성종목 분석 (LSEG)")
        from exe.collect_macro import lseg_open as _lseg_open, lseg_close as _lseg_close
        _lseg_open()
        try:
            overseas = fetch_asia_overseas_stocks(target_date, n_major=8, n_featured=6)
        finally:
            _lseg_close()
        if overseas:
            stocks_data["overseas_asia"] = overseas
            for mk, d in overseas.items():
                print(f"  {mk.upper()} {d['name']}: major {len(d['major'])} / "
                      f"featured {len(d['featured'])} / sectors {len(d['sectors'])}")
        if ops: ops.ok("[3-pre] 종목 수집", f"주요 {_n_major}개, 특징 {_n_feat}개")

    elif session == "europe":
        print(f"\n[3/4-pre] 유럽 섹터 + 종목 수집")
        from exe.collect_stocks import fetch_europe_stocks
        from exe.collect_macro import lseg_open as _lseg_open, lseg_close as _lseg_close
        _lseg_open()
        try:
            stocks_data = fetch_europe_stocks(target_date)
        finally:
            _lseg_close()
        print(f"  섹터 {len(stocks_data.get('sectors', []))}개  "
              f"시총상위 {len(stocks_data.get('mktcap_top', []))}개  "
              f"거래대금상위 {len(stocks_data.get('tradeval_top', []))}개  "
              f"급증 {len(stocks_data.get('turnover_surge', []))}개")
        if ops: ops.ok("[3-pre] 유럽 종목", f"섹터 {len(stocks_data.get('sectors',[]))}개")

    elif session == "us":
        print(f"\n[3/4-pre] 미국 섹터ETF + 종목 수집")
        from exe.collect_stocks import fetch_us_stocks
        stocks_data = fetch_us_stocks(target_date)
        print(f"  섹터ETF {len(stocks_data.get('sectors', []))}개  "
              f"시총상위 {len(stocks_data.get('mktcap_top', []))}개  "
              f"거래대금상위 {len(stocks_data.get('tradeval_top', []))}개  "
              f"급증 {len(stocks_data.get('turnover_surge', []))}개  "
              f"EPS변화 {len(stocks_data.get('eps_revision', []))}개")
        # US 세션: 당일 europe 종목 데이터를 DB에서 불러와 Europe 독립 섹션에 활용
        e_stocks = db.get_stocks_daily(target_date, "europe")
        if e_stocks:
            stocks_data["europe_stocks"] = e_stocks
            print(f"  유럽 종목 데이터 로드: 섹터 {len(e_stocks.get('sectors', []))}개  "
                  f"시총상위 {len(e_stocks.get('mktcap_top', []))}개  "
                  f"급증 {len(e_stocks.get('turnover_surge', []))}개")
        if ops: ops.ok("[3-pre] US 종목", f"섹터ETF {len(stocks_data.get('sectors',[]))}개")

        # US 세션: 당일 asia/europe 브리핑 요약을 DB에서 읽어 추가 컨텍스트로 전달
        prior_briefings = {}
        for prior_session in ("asia", "europe"):
            row = db.get_latest_briefing(prior_session)
            if row and row.get("date") == target_date:
                prior_briefings[prior_session] = row["content"][:400]
        if prior_briefings:
            stocks_data["prior_briefings"] = prior_briefings
            print(f"  이전 브리핑 컨텍스트: {list(prior_briefings.keys())}")

    # 종목 데이터 DB 저장 (prior_briefings 제외)
    if stocks_data:
        save_stocks = {k: v for k, v in stocks_data.items() if k != "prior_briefings"}
        n_saved = db.upsert_stocks_daily(target_date, session, save_stocks)
        print(f"  [stocks_daily] DB 저장: {n_saved}건")

    print(f"\n[3/4] LLM 브리핑 생성")
    from summarize.llm_briefing import generate_briefing
    content = generate_briefing(session=session, target_date=target_date, save=True,
                                stocks_data=stocks_data)
    print(content[:300] + "..." if len(content) > 300 else content)
    # 토큰 수 DB에서 조회
    _briefing_row = db.get_latest_briefing(session)
    if _briefing_row and ops:
        _pt = _briefing_row.get("prompt_tokens", 0) or 0
        _ot = _briefing_row.get("output_tokens", 0) or 0
        ops.ok("[3] LLM 브리핑", f"{_pt:,}+{_ot:,} tokens")

    # Step 4: Slack 발송
    if skip_notify:
        print(f"\n[4/4] Slack 발송 (skip: --no-notify)")
        if ops: ops.warn("[4] Slack", "skip (--no-notify)")
        return

    print(f"\n[4/4] Slack + ClickUp 발송")
    from summarize.notify_slack import send_briefing
    latest = db.get_latest_briefing(session)
    if latest:
        if session == "asia":
            txt_path = _archive_briefing_text(
                session=session,
                target_date=target_date,
                briefing_id=latest["id"],
                content=latest.get("content", content),
            )
            print(f"  [txt 저장] {txt_path}")
        send_briefing(briefing_id=latest["id"], session=session, date=target_date)
        if ops: ops.ok("[4] Slack 브리핑 발송", f"id={latest['id']}")

    if session != "europe":
        try:
            from summarize.notify_clickup import send_briefing_to_docs
            send_briefing_to_docs(content=content, session=session, date=target_date)
            if ops: ops.ok("[4] ClickUp 발송")
        except Exception as _cu_e:
            print(f"  [ClickUp 발송 ERROR] {_cu_e}")
            if ops: ops.warn("[4] ClickUp", str(_cu_e)[:80])

    print(f"\n✅ 완료: {session.upper()} 세션 파이프라인")


def run_global_pipeline(target_date: str,
                        skip_llm: bool = False, skip_notify: bool = False,
                        ops: "_OpsTracker | None" = None):
    """06:10 KST 통합 파이프라인 — Europe + US 데이터 수집/브리핑/Slack 단일 실행.

    중복 제거: LSEG 세션 1회, 매크로 1회, 경제지표 캘린더 1회.
    Europe 브리핑은 DB 저장만(Slack 미발송) → US 글로벌 통합 브리핑의
    prior_briefings 컨텍스트로 자동 흡수 → 최종 Slack 1건만 발송.
    """
    print(f"\n{'='*55}")
    print(f"  Global Market Watch  |  EUROPE+US 통합 파이프라인  |  {target_date}")
    print(f"{'='*55}")

    from exe.collect_macro import (
        load_yaml, lseg_open, lseg_close,
        collect_indices, collect_macro, DBManager
    )
    indices_cfg = load_yaml("indices.yaml")
    macro_cfg   = load_yaml("macro.yaml")
    db = DBManager()

    # Step 1: 데이터 수집 (Europe + US 지수, 매크로 — LSEG 단일 세션)
    # ── [1/5] 데이터 수집 — 블록별 독립 실행 ─────────────────────
    print(f"\n[1/5] 데이터 수집 (Europe + US 지수, 매크로)")
    _core_errors: list[str] = []

    try:
        lseg_open()
        _lseg_ok = True
    except Exception as _e:
        _lseg_ok = False
        _core_errors.append(f"[LSEG 세션] {_e}")
        if ops: ops.err("[수집] LSEG 세션", str(_e)[:80])

    try:
        # Block A: Europe 지수 (핵심)
        n_eu, _err = _run_collect_block(
            "EU 지수", collect_indices, "europe", target_date, indices_cfg, db,
            ops=ops, ops_label="[수집] EU 지수"
        )
        if _err:
            _core_errors.append(_err)
        else:
            print(f"  Europe 지수 upserted: {n_eu}건")
            if ops: ops.ok("[수집] EU 지수", f"{n_eu}건")

        # Block B: US 지수 (핵심)
        n_us, _err = _run_collect_block(
            "US 지수", collect_indices, "us", target_date, indices_cfg, db,
            ops=ops, ops_label="[수집] US 지수"
        )
        if _err:
            _core_errors.append(_err)
        else:
            print(f"  US 지수 upserted:     {n_us}건")
            if ops: ops.ok("[수집] US 지수", f"{n_us}건")

        # Block C: 매크로 (핵심)
        n_macro, _err = _run_collect_block(
            "매크로", collect_macro, target_date, macro_cfg, db,
            ops=ops, ops_label="[수집] 매크로"
        )
        if _err:
            _core_errors.append(_err)
        else:
            print(f"  매크로 upserted:      {n_macro}건")
            if ops: ops.ok("[수집] 매크로", f"{n_macro}건")
    finally:
        if _lseg_ok:
            lseg_close()

    # Block D: SPX OHLCV (핵심)
    print(f"\n[1/5-spx] SPX 전 종목 OHLCV 수집 (yfinance → us_stocks_daily)")
    _, _err = _run_collect_block(
        "SPX OHLCV", lambda: __import__("exe.collect_us_stocks", fromlist=["run"]).run(target_date, target_date),
        ops=ops, ops_label="[수집] SPX OHLCV"
    )
    if _err:
        _core_errors.append(_err)
    elif ops:
        ops.ok("[수집] SPX OHLCV")

    # ── 핵심 블록 오류 게이트 ────────────────────────────────────
    if _core_errors:
        _err_lines = "\n".join(f"  • {e}" for e in _core_errors)
        print(f"\n⚠️ 핵심 수집 블록 오류 {len(_core_errors)}개 — LLM 브리핑 스킵")
        print(_err_lines)
        if ops:
            ops.warn("[LLM] 브리핑", f"수집 오류로 스킵 ({len(_core_errors)}블록)")
            ops.send(ops.format_data_error(_core_errors))
        return

    # ── [2/5] 경제지표 + 어닝 캘린더 (보조) ──────────────────────
    print(f"\n[2/5] 경제지표 + SPX 어닝 캘린더 수집 (이번주 월요일 ~ 다음주 금요일)")
    _mon = (pd.Timestamp(target_date) - pd.Timedelta(days=pd.Timestamp(target_date).weekday())).strftime("%Y-%m-%d")
    _fri = (pd.Timestamp(_mon) + pd.Timedelta(days=11)).strftime("%Y-%m-%d")

    _n_econ, _err = _run_collect_block(
        "경제지표", lambda: db.upsert_econ_event(
            __import__("exe.collect_econ_cal", fromlist=["collect_econ_calendar"]).collect_econ_calendar(from_date=_mon, to_date=_fri)
        ), ops=ops, ops_label="[수집] 경제지표"
    )
    if _err:
        if ops: ops.warn("[수집] 경제지표", "오류 — 스킵")
    else:
        print(f"  경제지표 upserted: {_n_econ}건 ({_mon} ~ {_fri})")
        if ops: ops.ok("[수집] 경제지표", f"{_n_econ}건")

    _n_earn, _err = _run_collect_block(
        "어닝캘린더", lambda: db.upsert_earnings_event(
            __import__("exe.collect_earnings_cal", fromlist=["collect_earnings_calendar"]).collect_earnings_calendar(from_date=_mon, to_date=_fri, filter_spx=True)
        ), ops=ops, ops_label="[수집] 어닝"
    )
    if _err:
        if ops: ops.warn("[수집] 어닝", "오류 — 스킵")
    else:
        print(f"  어닝 upserted: {_n_earn}건 ({_mon} ~ {_fri})")
        if ops: ops.ok("[수집] 어닝", f"{_n_earn}건")

    if skip_llm:
        print(f"\n[3-5/5] LLM 브리핑 + Slack (skip: --no-llm)")
        if ops: ops.warn("[3-5] LLM·Slack", "skip (--no-llm)")
        print(f"\n✅ 완료: EUROPE+US 데이터 수집")
        return

    # Step 3: 종목 데이터 수집 (Europe + US)
    print(f"\n[3/5] Europe + US 종목 데이터 수집")
    from exe.collect_stocks import fetch_europe_stocks, fetch_us_stocks

    lseg_open()
    try:
        europe_data = fetch_europe_stocks(target_date)
    finally:
        lseg_close()
    if europe_data:
        print(f"  Europe — 섹터 {len(europe_data.get('sectors', []))}개  "
              f"시총상위 {len(europe_data.get('mktcap_top', []))}개  "
              f"거래대금상위 {len(europe_data.get('tradeval_top', []))}개  "
              f"급증 {len(europe_data.get('turnover_surge', []))}개")
        db.upsert_stocks_daily(target_date, "europe", europe_data)
        if ops: ops.ok("[3] Europe 종목", f"섹터 {len(europe_data.get('sectors',[]))}개")
    else:
        print(f"  Europe — 휴장일 스킵")
        if ops: ops.warn("[3] Europe 종목", "휴장일 스킵")

    us_data = fetch_us_stocks(target_date)
    if us_data:
        print(f"  US     — 섹터ETF {len(us_data.get('sectors', []))}개  "
              f"시총상위 {len(us_data.get('mktcap_top', []))}개  "
              f"거래대금상위 {len(us_data.get('tradeval_top', []))}개  "
              f"급증 {len(us_data.get('turnover_surge', []))}개  "
              f"EPS변화 {len(us_data.get('eps_revision', []))}개")
        if ops: ops.ok("[3] US 종목", f"섹터ETF {len(us_data.get('sectors',[]))}개")
    else:
        print(f"  US     — 휴장일 스킵")
        if ops: ops.warn("[3] US 종목", "휴장일 스킵")
    us_data["europe_stocks"] = europe_data
    db.upsert_stocks_daily(target_date, "us", us_data)

    # Step 4: 브리핑 생성 — Europe(컨텍스트용, DB만) → US(통합, Slack 대상)
    print(f"\n[4/5] LLM 브리핑 생성")
    from summarize.llm_briefing import generate_briefing

    print(f"  ▸ Europe 컨텍스트 브리핑 생성 (DB 저장만)")
    generate_briefing(session="europe", target_date=target_date,
                      save=True, stocks_data=europe_data)

    # 당일 asia/europe 브리핑을 US 통합 브리핑 컨텍스트로 주입
    prior_briefings = {}
    for prior_session in ("asia", "europe"):
        row = db.get_latest_briefing(prior_session)
        if row and row.get("date") == target_date:
            prior_briefings[prior_session] = row["content"][:400]
    if prior_briefings:
        us_data["prior_briefings"] = prior_briefings
        print(f"  ▸ 이전 브리핑 컨텍스트: {list(prior_briefings.keys())}")

    print(f"  ▸ Global 통합 브리핑 생성")
    us_content = generate_briefing(session="us", target_date=target_date,
                                   save=True, stocks_data=us_data,
                                   save_as="global")
    print(us_content[:300] + "..." if len(us_content) > 300 else us_content)
    _briefing_row = db.get_latest_briefing("global")
    if _briefing_row and ops:
        _pt = _briefing_row.get("prompt_tokens", 0) or 0
        _ot = _briefing_row.get("output_tokens", 0) or 0
        ops.ok("[4] LLM 브리핑", f"{_pt:,}+{_ot:,} tokens")

    # Step 5: Slack 발송 (Global 통합 브리핑 1건만)
    if skip_notify:
        print(f"\n[5/5] Slack 발송 (skip: --no-notify)")
        if ops: ops.warn("[5] Slack", "skip (--no-notify)")
        print(f"\n✅ 완료: GLOBAL 통합 파이프라인 (Slack 미발송)")
        return

    print(f"\n[5/5] Slack + ClickUp 발송 (Global 통합 브리핑 1건)")
    from summarize.notify_slack import send_briefing
    latest = db.get_latest_briefing("global")
    if latest:
        txt_path = _archive_briefing_text(
            session="global",
            target_date=target_date,
            briefing_id=latest["id"],
            content=latest.get("content", us_content),
        )
        print(f"  [txt 저장] {txt_path}")
        send_briefing(briefing_id=latest["id"], session="global", date=target_date)
        if ops: ops.ok("[5] Slack 브리핑 발송", f"id={latest['id']}")

    try:
        from summarize.notify_clickup import send_briefing_to_docs
        send_briefing_to_docs(content=us_content, session="global", date=target_date)
        if ops: ops.ok("[5] ClickUp 발송")
    except Exception as _cu_e:
        print(f"  [ClickUp 발송 ERROR] {_cu_e}")
        if ops: ops.warn("[5] ClickUp", str(_cu_e)[:80])

    print(f"\n✅ 완료: GLOBAL 통합 파이프라인")



def _check_and_init_history():
    """DB 히스토리 상태 확인 및 필요 시 초기화 안내."""
    from db.db_manager import DBManager
    import pandas as pd
    
    db = DBManager()
    conn = db._connect()
    try:
        result = conn.execute(
            """SELECT COUNT(*), MIN(date), MAX(date) 
               FROM market_daily WHERE category='index'"""
        ).fetchone()
    finally:
        conn.close()
    
    if not result or result[0] == 0:
        print("\n" + "="*60)
        print("[경고] DB에 지수 히스토리 데이터가 없습니다.")
        print("="*60)
        print("\n다음 명령어로 180일 히스토리를 초기화하세요 (1회만 실행):")
        print("  python exe/collect_macro.py --init-history")
        print("\n이 과정에는 수 분이 소요될 수 있습니다.")
        print("초기화 후 다시 run_session.py를 실행해주세요.\n")
        return False
    
    count, min_date, max_date = result
    date_range = (pd.Timestamp(max_date) - pd.Timestamp(min_date)).days
    
    if date_range < 25:  # 25영업일 미만 (약 1개월)
        print(f"\n[경고] 지수 히스토리가 부족합니다: {count}개 레코드, 범위 {min_date}~{max_date}")
        print("→ 180일 히스토리 초기화를 권장합니다: python exe/collect_macro.py --init-history\n")
        return False
    
    print(f"[정보] DB 지수 데이터: {count}개 레코드, 범위 {min_date}~{max_date} ({date_range}일)")
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="global-market-watch",
        description="Global Market Watch 파이프라인 (수집 → 브리핑 → Slack)",
    )
    parser.add_argument("--session", default=None,
                        choices=["asia", "europe", "us", "global", "all"],
                        help="실행 세션. 플래그(--global 등)로 대체 가능")
    # 플래그 스타일 세션 선택 (예: global-market-watch --global --date 2026-06-18)
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument("--asia",   dest="session_flag", action="store_const", const="asia")
    grp.add_argument("--global", dest="session_flag", action="store_const", const="global",
                     help="Europe+US 통합 파이프라인 (06:10 스케줄)")
    grp.add_argument("--all",    dest="session_flag", action="store_const", const="all")
    parser.add_argument("--date", default=None,
                        help="기준일 YYYY-MM-DD (기본: 오늘)")
    parser.add_argument("--no-llm",    action="store_true")
    parser.add_argument("--no-notify", action="store_true")
    args = parser.parse_args(argv)

    session = args.session or args.session_flag
    if not session:
        parser.error("세션을 지정하세요 (예: --global 또는 --session global)")
    args.session = session

    # 히스토리 체크
    if not _check_and_init_history():
        sys.exit(1)

    target_date = args.date or _default_date(args.session)

    ops = _OpsTracker(session=args.session, target_date=target_date)

    try:
        if args.session == "global":
            run_global_pipeline(
                target_date=target_date,
                skip_llm=args.no_llm,
                skip_notify=args.no_notify,
                ops=ops,
            )
        elif args.session == "all":
            # 호환 유지: asia 후 global 통합 실행
            ops_asia   = _OpsTracker(session="asia",   target_date=target_date)
            ops_global = _OpsTracker(session="global", target_date=target_date)
            run_session(session="asia", target_date=target_date,
                        skip_llm=args.no_llm, skip_notify=args.no_notify, ops=ops_asia)
            ops_asia.send(ops_asia.format_success())
            run_global_pipeline(target_date=target_date,
                                skip_llm=args.no_llm, skip_notify=args.no_notify, ops=ops_global)
            ops_global.send(ops_global.format_success())
            return
        else:
            run_session(
                session=args.session,
                target_date=target_date,
                skip_llm=args.no_llm,
                skip_notify=args.no_notify,
                ops=ops,
            )

        ops.send(ops.format_success())

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        ops.err("예외 발생", f"{type(e).__name__}: {str(e)[:150]}")
        ops.send(ops.format_failure(e, tb))
        raise


# 콘솔 스크립트 entry point 별칭
cli = main


if __name__ == "__main__":
    main()
