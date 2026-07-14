# -*- coding: utf-8 -*-
"""
goal 6 — ETF 유니버스 편입/편출 + AUM 현황·유입/유출(유형별 분해)
  · 주간(base_date) 유니버스 멤버십 변화: 편입/편출
  · 유형별(일반/레버리지/인버스1X/2X) AUM 현황(=시가총액 합, 주말 기준)
  · 유입/유출 = Σ [cu_value × Δnum_cu]  — 편입/편출 event 영향 제거(연속 멤버만)
출력:
  out/universe_flow.csv    (base_prev→base) × etf_type 요약
  out/universe_events.csv  편입/편출 ETF 목록
주말 스냅샷 = 각 base_date의 최종 관측일(max date) 기준.
"""
import pandas as pd
from _common import load_meta, load_universe, save


def main(out_dir=None):
    m = load_meta()
    uni = load_universe()[["base_date", "etf_code", "theme"]]

    # base별 최종 관측일 스냅샷
    idx = m.groupby(["base_date", "etf_code"])["date"].idxmax()
    snap = m.loc[idx, ["base_date", "etf_code", "etf_name", "etf_type",
                       "num_cu", "cu_value", "mktcap", "date"]].copy()
    snap["mktcap"] = snap["mktcap"] * 1e6   # Local mn(백만원) → 원, flow_value(원)와 단위 통일
    snap["etf_type"] = snap["etf_type"].replace("", "(빈값)")
    snap = snap.merge(uni, on=["base_date", "etf_code"], how="left")

    bases = sorted(snap["base_date"].unique())
    by = {b: snap[snap["base_date"] == b].set_index("etf_code") for b in bases}

    flow_rows, event_rows = [], []
    for i in range(1, len(bases)):
        bp, bc = bases[i - 1], bases[i]
        A, B = by[bp], by[bc]
        setA, setB = set(A.index), set(B.index)
        cont = setA & setB
        ins, outs = setB - setA, setA - setB

        # 편입/편출 목록
        for c in sorted(ins):
            r = B.loc[c]
            event_rows.append(dict(base_prev=bp, base=bc, event="편입",
                                   etf_code=c, etf_name=r.etf_name,
                                   etf_type=r.etf_type, theme=r.theme,
                                   mktcap=r.mktcap))
        for c in sorted(outs):
            r = A.loc[c]
            event_rows.append(dict(base_prev=bp, base=bc, event="편출",
                                   etf_code=c, etf_name=r.etf_name,
                                   etf_type=r.etf_type, theme=r.theme,
                                   mktcap=r.mktcap))

        # 연속 멤버 유형별 event-adjusted flow
        cd = pd.DataFrame({
            "etf_code": list(cont),
            "etf_type": [B.loc[c, "etf_type"] for c in cont],
            "flow": [B.loc[c, "cu_value"] * (B.loc[c, "num_cu"] - A.loc[c, "num_cu"])
                     for c in cont],
            "mktcap_b": [B.loc[c, "mktcap"] for c in cont],
        })
        cont_by_type = cd.groupby("etf_type").agg(
            n_cont=("etf_code", "nunique"),
            flow_value=("flow", "sum")).reset_index()

        # 유형별 총 AUM(현황, base B 전체 멤버) + 편입/편출 집계
        aum_b = B.groupby("etf_type")["mktcap"].sum().rename("aum_total")
        in_by = B.loc[list(ins)].groupby("etf_type")["mktcap"].agg(["size", "sum"]) \
            .rename(columns={"size": "n_in", "sum": "in_aum"}) if ins else None
        out_by = A.loc[list(outs)].groupby("etf_type")["mktcap"].agg(["size", "sum"]) \
            .rename(columns={"size": "n_out", "sum": "out_aum"}) if outs else None

        types = sorted(set(cont_by_type["etf_type"]) | set(aum_b.index)
                       | set(B["etf_type"]) | set(A["etf_type"]))
        for t in types:
            row = dict(base_prev=bp, base=bc, etf_type=t)
            ct = cont_by_type[cont_by_type["etf_type"] == t]
            row["n_cont"] = int(ct["n_cont"].iloc[0]) if len(ct) else 0
            row["flow_value"] = float(ct["flow_value"].iloc[0]) if len(ct) else 0.0
            row["aum_total"] = float(aum_b.get(t, 0.0))
            row["n_in"] = int(in_by["n_in"].get(t, 0)) if in_by is not None else 0
            row["in_aum"] = float(in_by["in_aum"].get(t, 0.0)) if in_by is not None else 0.0
            row["n_out"] = int(out_by["n_out"].get(t, 0)) if out_by is not None else 0
            row["out_aum"] = float(out_by["out_aum"].get(t, 0.0)) if out_by is not None else 0.0
            flow_rows.append(row)

    flow = pd.DataFrame(flow_rows).sort_values(["base", "etf_type"])
    save(flow, "universe_flow.csv", out_dir)
    events = pd.DataFrame(event_rows).sort_values(["base", "event", "etf_type", "etf_code"])
    save(events, "universe_events.csv", out_dir)

    # 콘솔 요약
    print("  [유형별 event-adjusted 순유입(억원)]")
    for base, g in flow.groupby("base"):
        parts = " | ".join(f"{r.etf_type}:{r.flow_value/1e8:+,.0f}" for r in g.itertuples())
        print(f"    →{base}: {parts}")


if __name__ == "__main__":
    main()
