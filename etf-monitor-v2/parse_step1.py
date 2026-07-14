# -*- coding: utf-8 -*-
"""
parse_step1.py  —  etf_monitor_v2 Stage 1 정규화

raw/etf_search_etf_{base}.csv      주간 ETF 메타 패널(wide, 5일)
raw/etf_search_run_{base}_{run}.csv 일별 1CU 구성종목(wide, 5열/ETF)

    → parsed/etf_meta_ts.csv    (base_date, date, etf_code) 단위 롱포맷 메타
    → parsed/holdings_ts.csv    (base_date, run_date, etf_code, constituent) 롱포맷 구성종목
    → parsed/etf_universe.csv   base_date별 유니버스 멤버십(+유형+테마)
    → config/theme_map.csv      etf_code→theme 매핑(사용자 검토/수정용)

인코딩: 모든 raw 는 utf-8-sig.
"""
import csv
import os
import re
import glob
import sys

import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RAW_DIR = os.path.join(BASE_DIR, "raw")
PARSED_DIR = os.path.join(BASE_DIR, "parsed")
CONFIG_DIR = os.path.join(BASE_DIR, "config")

# etf 메타 wide 파일의 Account 아이템코드 → 표준 컬럼명
ITEM_MAP = {
    "W200300": "listing_date",  # 상장일
    "R102300": "delisting_date",# 상장폐지일
    "W202100": "etf_type",      # ETF레버리지,인버스구분 (일반/레버리지/인버스 1X/인버스 2X)
    "W200100": "base_index",    # ETF기초지수명
    "S102100": "mktcap",        # 시가총액 (Local mn)
    "S100100": "close",         # 종가
    "S100500": "ret",           # 수정주가수익률 (%)
    "W201100": "nav",           # ETF순자산가치(NAV)
    "W201200": "disparity",     # ETF괴리율 (%)
    "S101500": "shares_out",    # 상장주식수
    "W402600": "cu_size",       # CU구성좌수
}
# (date, item) 로 값이 붙는 5일 시계열 아이템
TS_ITEMS = {"mktcap", "close", "ret", "nav", "disparity", "shares_out", "num_cu"}
# base_date 스칼라(날짜 무관) 아이템
SCALAR_ITEMS = {"etf_type", "base_index", "cu_size", "listing_date", "delisting_date"}


# ─────────────────────────────────────────────────────────────
# 유틸
# ─────────────────────────────────────────────────────────────
def read_csv_rows(path):
    with open(path, encoding="utf-8-sig") as fh:
        return list(csv.reader(fh))


def to_num(v):
    """'284,947,000' / '299.00' / '' → float | None"""
    if v is None:
        return None
    s = str(v).strip().replace(",", "").replace('"', "")
    if s == "" or s in ("-", "N/A", "nan"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def clean(v):
    return str(v).strip() if v is not None else ""


# ─────────────────────────────────────────────────────────────
# etf 메타 파싱
# ─────────────────────────────────────────────────────────────
def parse_etf_file(path, base_date):
    """wide etf 메타 → 롱포맷 rows[dict]  (한 base_date)"""
    rows = read_csv_rows(path)
    # row0 Account(item code), row1 Period(date | num_cu_*), row4 Korean name header
    acc = rows[0]
    period = rows[1]
    ncol = max(len(acc), len(period))

    # 컬럼별 (item, date) 라벨 산출
    col_item = {}   # col -> std item name
    col_date = {}   # col -> yyyymmdd (시계열 아이템만)
    for c in range(2, ncol):
        a = clean(acc[c]) if c < len(acc) else ""
        p = clean(period[c]) if c < len(period) else ""
        if a in ITEM_MAP:
            it = ITEM_MAP[a]
            col_item[c] = it
            if it in TS_ITEMS and re.fullmatch(r"\d{8}", p):
                col_date[c] = p
        elif p.startswith("num_cu_"):
            col_item[c] = "num_cu"
            m = re.search(r"(\d{8})", p)
            if m:
                col_date[c] = m.group(1)

    dates = sorted(set(col_date.values()))

    out = []
    for r in range(5, len(rows)):
        row = rows[r]
        if not row:
            continue
        code = clean(row[0])
        name = clean(row[1]) if len(row) > 1 else ""
        if not code:
            continue
        scal = {}
        ts = {d: {} for d in dates}
        for c, it in col_item.items():
            val = row[c] if c < len(row) else ""
            if it in SCALAR_ITEMS:
                scal[it] = clean(val) if it in ("etf_type", "base_index", "listing_date", "delisting_date") else to_num(val)
            else:  # 시계열
                d = col_date.get(c)
                if d is not None:
                    ts[d][it] = to_num(val)
        for d in dates:
            rec = {
                "base_date": base_date,
                "date": d,
                "etf_code": code,
                "etf_name": name,
                "etf_type": (scal.get("etf_type") or "").strip(),
                "base_index": scal.get("base_index", ""),
                "cu_size": scal.get("cu_size"),
                "listing_date": scal.get("listing_date", ""),
                "delisting_date": scal.get("delisting_date", ""),
            }
            rec.update({k: ts[d].get(k) for k in
                        ("mktcap", "close", "ret", "nav", "disparity", "shares_out", "num_cu")})
            # 1CU 가치 = NAV × CU구성좌수 (자금흐름 산출 기준)
            nav, cu = rec["nav"], rec["cu_size"]
            rec["cu_value"] = (nav * cu) if (nav is not None and cu is not None) else None
            out.append(rec)
    return out


# ─────────────────────────────────────────────────────────────
# run 구성종목 파싱
# ─────────────────────────────────────────────────────────────
SWAP_KW = ("스왑", "swap", "SWAP")
FUT_KW = ("선물", "개별선물", "개별선")
OPT_KW = ("콜옵션", "풋옵션")
CASH_KW = ("원화현금", "설정현금", "현금", "예금", "미수", "미지급", "위탁증거금")
# KRX 파생 명명 규칙: 선물 "<기초> F 202607 ( 10)", 옵션 "<기초> C 202607 1295.0" / 위클리 "C 2606W4"
FUT_RE = re.compile(r"(^|\s)F\s?\d{6}(\s|\(|$)")
OPT_RE = re.compile(r"(^|\s)[CP]\s?(\d{6}|\d{4}W\d)(\s|$)")
CODE_RE = re.compile(r"A[0-9A-Z]{6}")   # A005930(주식) / A00591K(우선주) / A0192M0(신형 ETF) 공통
# ETF 브랜드(구성종목명 첫 토큰) — 재간접(ETF-of-ETF) 판별용
ETF_BRANDS = {
    "KODEX", "TIGER", "RISE", "KBSTAR", "ACE", "SOL", "PLUS", "KOACT", "HANARO",
    "KIWOOM", "ARIRANG", "KOSEF", "TIMEFOLIO", "WON", "BNK", "TIME", "MIDAS",
    "UNICORN", "HK", "FOCUS", "1Q", "KCGI", "VITA", "TREX", "KINDEX", "KTOP",
    "KOACT", "히어로즈", "파워", "마이다스",
}


def classify_kind(code, name, etf_codes=None):
    n = name or ""
    c = (code or "").strip()
    first = n.split()[0].upper() if n else ""
    is_code = bool(CODE_RE.fullmatch(c))
    # 브랜드 접두 or 유니버스 코드 → 재간접 ETF (이름에 '선물'이 있어도 ETF 우선)
    if is_code and (first in ETF_BRANDS or (etf_codes and c in etf_codes)):
        return "etf"
    if any(k in n for k in SWAP_KW):
        return "swap"
    if any(k in n for k in FUT_KW) or FUT_RE.search(n):
        return "futures"
    if any(k in n for k in OPT_KW) or OPT_RE.search(n):
        return "option"
    if any(k in n for k in CASH_KW):
        return "cash"
    if is_code:
        return "stock"
    return "other"


def parse_run_file(path, base_date, run_date, etf_codes=None):
    """wide 5열/ETF 구성종목 → 롱포맷 rows[dict]"""
    rows = read_csv_rows(path)
    # 헤더행(cell0=='코드') 탐색 → codes/names 는 그 위 2/1행
    hdr = None
    for i, row in enumerate(rows):
        if row and clean(row[0]) == "코드":
            hdr = i
            break
    if hdr is None or hdr < 2:
        print(f"    ! 헤더행(코드) 탐색 실패: {os.path.basename(path)}")
        return []
    code_row, name_row = rows[hdr - 2], rows[hdr - 1]
    ncol = len(code_row)

    out = []
    for c in range(0, ncol, 5):   # 5열 간격 ETF 블록
        etf_code = clean(code_row[c]) if c < len(code_row) else ""
        if not etf_code:
            continue
        etf_name = clean(name_row[c]) if c < len(name_row) else ""
        for r in range(hdr + 1, len(rows)):
            row = rows[r]
            if c >= len(row):
                continue
            ccode = clean(row[c])
            cname = clean(row[c + 1]) if c + 1 < len(row) else ""
            if ccode == "" and cname == "":
                continue
            qty = to_num(row[c + 2]) if c + 2 < len(row) else None
            amount = to_num(row[c + 3]) if c + 3 < len(row) else None
            weight = to_num(row[c + 4]) if c + 4 < len(row) else None
            out.append({
                "base_date": base_date,
                "run_date": run_date,
                "etf_code": etf_code,
                "etf_name": etf_name,
                "constituent_code": ccode,
                "constituent_name": cname,
                "kind": classify_kind(ccode, cname, etf_codes),
                "qty": qty,
                "amount": amount,
                "weight": weight,
            })
    return out


# ─────────────────────────────────────────────────────────────
# 테마 추출 (etf명 + 기초지수명 키워드)
# ─────────────────────────────────────────────────────────────
# 우선순위 순서대로 매칭(위쪽이 우선). 없으면 '기타'.
# 위에서부터 우선 매칭. 구체 테마 → 광의 시장대표 순.
THEME_RULES = [
    ("반도체",     ("반도체", "HBM", "파운드리", "메모리", "삼성전자", "SK하이닉스", "하이닉스", "소부장")),
    ("2차전지",    ("2차전지", "이차전지", "배터리", "전고체", "리튬", "음극재", "양극재")),
    ("바이오",     ("바이오", "제약", "헬스케어", "의료", "신약", "비만")),
    ("방산",       ("방산", "우주항공", "항공우주", "국방", "디펜스")),
    ("원자력",     ("원자력", "원전", "SMR")),
    ("조선",       ("조선",)),
    ("로봇",       ("로봇", "휴머노이드")),
    ("AI전력인프라", ("소버린AI", "AI전력", "AI인프라", "전력인프라", "전력설비", "전력기기",
                   "전력핵심", "네트워크인프라", "온디바이스", "AI테크", "코리아테크", "테크TOP",
                   "테크핵심", "CAPEX", "설비투자", "데이터센터")),
    ("친환경에너지", ("수소", "태양광", "ESS", "신재생", "전기&수소차", "풍력")),
    ("자동차",     ("자동차", "자율주행", "미래차", "전기차", "완성차", "모빌리티")),
    ("화장품",     ("화장품", "뷰티", "K-뷰티")),
    ("금융",       ("금융", "은행", "증권", "보험")),
    ("배당",       ("배당", "고배당", "위클리", "커버드콜")),
    ("엔터",       ("엔터", "미디어", "게임", "K-POP", "콘텐츠")),
    ("인터넷",     ("인터넷", "플랫폼", "소프트웨어")),
    ("소비재",     ("소비재", "필수소비", "리테일", "유통")),
    ("그룹지주",    ("그룹", "지주회사")),
    ("여행레저",    ("여행", "레저")),
    ("메타버스",    ("메타버스",)),
    ("ESG",       ("ESG", "사회책임")),
    ("팩터",       ("모멘텀", "성장주", "성장 지수", "우량", "밸류", "퀄리티", "가치", "밸류업", "저PBR")),
    ("원자재",     ("철강", "화학", "정유", "에너지", "소재")),
    ("코스피대표",  ("코스피", "KOSPI", "KTOP", "KRX100", "KRX 100", "KRX300", "KRX 300",
                  "대형주", "MSCI Korea", "코리아TOP10", "KTOP30", "코스피50", "코리아 소버린",
                  "TOP 5", "TOP10 지수", "TOP 10 지수")),
    ("코스닥대표",  ("코스닥",)),
]


def extract_theme(name, base_index):
    text = f"{name} {base_index}"
    for theme, kws in THEME_RULES:
        if any(k in text for k in kws):
            return theme
    return "기타"


# ─────────────────────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────────────────────
def main():
    os.makedirs(PARSED_DIR, exist_ok=True)
    os.makedirs(CONFIG_DIR, exist_ok=True)

    etf_files = sorted(glob.glob(os.path.join(RAW_DIR, "etf_search_etf_*.csv")))
    run_files = sorted(glob.glob(os.path.join(RAW_DIR, "etf_search_run_*.csv")))
    if not etf_files:
        print("raw/etf_search_etf_*.csv 없음"); sys.exit(1)

    # ── etf 메타 ─────────────────────────────
    meta_rows = []
    for f in etf_files:
        m = re.search(r"etf_search_etf_(\d{8})\.csv$", f)
        base = m.group(1)
        recs = parse_etf_file(f, base)
        meta_rows.extend(recs)
        print(f"[etf ] {base}: {len(set(r['etf_code'] for r in recs))} ETF, {len(recs)} rows")
    meta = pd.DataFrame(meta_rows)
    meta.to_csv(os.path.join(PARSED_DIR, "etf_meta_ts.csv"),
                index=False, encoding="utf-8-sig")
    etf_codes = set(meta["etf_code"].unique())

    # ── run 구성종목 ─────────────────────────
    hold_rows = []
    for f in run_files:
        m = re.search(r"etf_search_run_(\d{8})_(\d{8})\.csv$", f)
        base, run = m.group(1), m.group(2)
        recs = parse_run_file(f, base, run, etf_codes)
        hold_rows.extend(recs)
        print(f"[run ] {base}/{run}: {len(set(r['etf_code'] for r in recs))} ETF, {len(recs)} rows")
    hold = pd.DataFrame(hold_rows)
    hold.to_csv(os.path.join(PARSED_DIR, "holdings_ts.csv"),
                index=False, encoding="utf-8-sig")

    # ── 유니버스 + 테마 ──────────────────────
    uni = (meta.groupby(["base_date", "etf_code"], as_index=False)
               .agg(etf_name=("etf_name", "first"),
                    etf_type=("etf_type", "first"),
                    base_index=("base_index", "first"),
                    listing_date=("listing_date", "first"),
                    delisting_date=("delisting_date", "first")))
    uni["theme"] = uni.apply(lambda r: extract_theme(r["etf_name"], r["base_index"]), axis=1)
    # 상장일 이후, 상장폐지일 이전인 ETF만 편입
    uni = uni[
        (uni["listing_date"] == "") | (uni["listing_date"] <= uni["base_date"])
    ]
    uni = uni[
        (uni["delisting_date"] == "") | (uni["delisting_date"] >= uni["base_date"])
    ]
    uni.to_csv(os.path.join(PARSED_DIR, "etf_universe.csv"),
               index=False, encoding="utf-8-sig")

    # theme_map (코드 유니크, 사용자 검토용)
    tm = (uni.sort_values("base_date")
             .groupby("etf_code", as_index=False)
             .agg(etf_name=("etf_name", "last"),
                  base_index=("base_index", "last"),
                  etf_type=("etf_type", "last"),
                  theme=("theme", "last"))
             .sort_values("etf_code"))
    tm.to_csv(os.path.join(CONFIG_DIR, "theme_map.csv"),
              index=False, encoding="utf-8-sig")
    # 미분류(기타) ETF 목록 — 규칙 보강 검토용
    unc = tm[tm["theme"] == "기타"][["etf_code", "etf_name", "base_index"]]
    unc.to_csv(os.path.join(CONFIG_DIR, "theme_unclassified.csv"),
               index=False, encoding="utf-8-sig")

    # ── 검증 요약 ────────────────────────────
    print("\n===== 요약 =====")
    print(f"etf_meta_ts : {len(meta):,} rows | base {meta['base_date'].nunique()} | "
          f"dates {meta['date'].nunique()} | etf {meta['etf_code'].nunique()}")
    print(f"holdings_ts : {len(hold):,} rows | base {hold['base_date'].nunique()} | "
          f"run {hold['run_date'].nunique()}")
    print("  kind 분포:")
    for k, v in hold["kind"].value_counts().items():
        print(f"    {k:8} {v:,}")
    print(f"etf_universe: {len(uni):,} rows | 유형 분포:")
    for k, v in uni["etf_type"].replace("", "(빈값)").value_counts().items():
        print(f"    {k:10} {v:,}")
    print(f"theme_map   : {len(tm):,} ETF | 테마 분포:")
    for k, v in tm["theme"].value_counts().items():
        print(f"    {k:10} {v:,}")
    # 정합성: num_cu ?= shares_out / cu_size
    chk = meta.dropna(subset=["num_cu", "shares_out", "cu_size"]).copy()
    chk = chk[chk["cu_size"] > 0]
    chk["recalc"] = chk["shares_out"] / chk["cu_size"]
    bad = (chk["num_cu"] - chk["recalc"]).abs() > 1e-6
    print(f"num_cu 정합성: {(~bad).sum():,}/{len(chk):,} 일치 "
          f"(불일치 {bad.sum():,})")


if __name__ == "__main__":
    main()
