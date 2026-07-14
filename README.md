# ETF Monitor v2

국내주식형 ETF 주간/일간 수급·리밸런싱 모니터링 파이프라인.
v1(`../etf_monitor`) 대비 **일별(주 5회) 구성종목 스냅샷**을 사용해 주 내(intra-week) 리밸런싱까지 추적한다.

## 실행

```bash
PY="C:/Users/8521/AppData/Local/anaconda3/envs/meritzquant/python.exe"
$PY run_all.py          # parse_step1 → a1~a6 전체 (~40s)
# 개별 실행도 가능: $PY a3_rebalancing.py 등
```

## 원천 데이터 (`raw/`)

| 파일 | 내용 |
|---|---|
| `etf_search_etf_{base}.csv` | 주간 ETF 메타(wide, 5일). 유형·기초지수·시총·종가·수익률·NAV·괴리율·상장주식수·CU구성좌수·num_cu |
| `etf_search_run_{base}_{run}.csv` | 일별 1CU 구성종목(wide, 5열/ETF). run_date 5개/주 |

- `base_date` = 주간 유니버스 스크리닝일(금~목 daily tracking). `run_date` = 일별 구성종목일.
- 유니버스 = 최근 5일 평균 거래대금 10억↑ 국내주식형(일반/레버리지/인버스 1X·2X).
- 인코딩: 전부 utf-8-sig.

## 파이프라인

### Stage 1 — `parse_step1.py` (정규화)
| 산출물 | 내용 |
|---|---|
| `parsed/etf_meta_ts.csv` | (base_date, date, etf_code) 롱포맷 메타 + `cu_value = NAV×CU구성좌수` |
| `parsed/holdings_ts.csv` | (base, run_date, etf, 구성종목) 롱포맷 + `kind` |
| `parsed/etf_universe.csv` | base_date별 멤버십 + 유형 + 테마 |
| `config/theme_map.csv` | etf→theme 매핑 (**사용자 편집용**) |
| `config/theme_unclassified.csv` | 테마 미분류(기타) 목록 |

- `kind`: stock / futures / option / cash / swap / etf(재간접). 파생·현금은 `constituent_code`가 비어 있어 **종목명**으로 식별(KRX 명명 `F 202607`, `C 202607 1295.0`, 위클리 `C 2606W4`, 개별선물, 스왑).
- 테마는 etf명+기초지수명 키워드(`THEME_RULES`)로 추출. 애매한 종목은 `theme_map.csv`에서 직접 수정.

### Stage 2 — goal별 분석 (`out/`)
| goal | 스크립트 | 산출물 | 핵심 |
|---|---|---|---|
| 1 주간수익률 | `a1_weekly_returns.py` | `weekly_returns.csv` | 일별 단순평균·시총가중(전일 시총 가중) 수익률 · 전체/유형/테마 |
| 2 설정/환매 | `a2_cu_flow.py` | `cu_flow.csv` | `Δnum_cu`(설정+/환매−) + `flow_value=Δnum_cu×cu_value` |
| 3 리밸런싱 | `a3_rebalancing.py` | `rebalancing.csv`, `rebalancing_by_kind.csv` | 1CU 바스켓 편입/편출/변경(주식현물·선물·옵션) |
| 4 CU발 AUM | `a4_aum_from_cu.py` | `aum_from_cu_daily.csv`, `_weekly.csv` | 설정 자금유입액(일간/주간합계·순위) |
| 5 리밸ETF수 | `a5_rebal_summary.py` | `rebal_summary.csv`, `rebal_etf_daily.csv` | 일별 리밸런싱 ETF 수 + 대표목록(주식/선물/옵션 한정) |
| 6 유니버스흐름 | `a6_universe_flow.py` | `universe_flow.csv`, `universe_events.csv` | 편입/편출 + 유형별 AUM·순유입(편입/편출 event 제거) |

## 단위·규약
- 금액 계열(`cu_value`, `flow_value`, a6 `aum_total`/`flow_value`)은 **원(KRW)**. (`mktcap` 원천은 백만원 → a6에서 원으로 환산)
- 수익률(`ret`)은 **%**, `ret[D]` = 전일→당일 등락.
- `num_cu = 상장주식수 / CU구성좌수` (parse 시 원본과 정합성 검증: 오차 0).
- 전일/전주 기준은 해당 ETF가 관측된 직전 영업일(주 경계 포함).

## 검증 포인트 (품질 회귀 체크)
- parse: `num_cu` 정합성 100%, `kind` 미분류 0건.
- a3: `event×kind`에서 futures/option이 반드시 존재(파생은 종목명 키). cash는 `편입/편출` 0 (qty 없음 → 변경만).
- a6: `flow_value`와 `aum_total` 단위(원) 일치.

## 남은 튜닝
- `config/theme_map.csv` — 기타/애매 종목 수동 보정.
- `THEME_RULES`(`parse_step1.py`) — 규칙 보강 시 재실행.
