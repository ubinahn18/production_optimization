# -*- coding: utf-8 -*-
"""
remove_order.py

이미 저장된 계획(line_schedule.csv/daily_workforce.csv)에서 특정
order_id 하나를 통째로 제외한다 - 그 주문이 차지하던 produce/setup
슬롯을 전부 idle로 되돌리는 순수 후처리이고, CP-SAT 재최적화는 하지
않는다. plan_from_orders.py/plan_additional_order.py 등 기존 스크립트는
전혀 건드리지 않는다.

이 주문을 빼면 실제로 얼마나 절약되는지 보여주는 게 핵심 목적이라,
결과를 저장하는 것과 별개로 영향받은 날짜(고용인원/잔업인원/잔업시간/
인건비가 바뀐 날)마다 그 변화를 콘솔에 출력한다.

이 주문의 슬롯을 idle로 되돌리면, 그 앞뒤에 있던 다른 주문의 셋업이
이제는 불필요해질 수 있다(scheduling/solver.py와 같은 규칙 - 아래
prune_redundant_setups 참고). 그래서 이 주문이 있던 (라인, 날짜)마다
그 날 전체를 다시 훑어서, 각 셋업 슬롯의 "바로 앞 생산 블록"이 뭐였는지
확인한 뒤 필요 없어진 셋업은 idle로 같이 되돌린다.

사용법:
    python remove_order.py --order-id S2026034SA012_100
"""

from __future__ import annotations

import argparse
import io
import os
import sys
from datetime import date

# Windows 콘솔(cp949 등) 기본 인코딩에서는 한글 출력이 깨지므로 UTF-8로 강제.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # opt_modeling_proto/ 를 import 경로에 추가
import plan_from_orders as pfo
from scheduling.models import OVERTIME_LOCAL_SLOTS, SLOT_LABELS, SLOTS_PER_DAY
from scheduling.pooling import build_line_pools

SLOT_ORDER = {s: i for i, s in enumerate(SLOT_LABELS)}


def compute_demand(
    schedule_df: pd.DataFrame, order_by_id: dict, type_by_physical_line: dict, horizon_days: int
) -> dict[int, float]:
    """스케줄 전체에서 슬롯별 필요인원(생산 슬롯만, 그 주문의 라인타입별
    workers 기준)을 계산한다. 셋업 슬롯은 기존 관례대로 인원 수요 0."""
    demand: dict[int, float] = {}
    produce = schedule_df[schedule_df["activity"] == "produce"]
    for row in produce.itertuples(index=False):
        o = order_by_id.get(row.order_id)
        line_type = type_by_physical_line.get(row.line_id)
        workers = 0 if o is None else o.workers.get(line_type, 0)
        t = (row.day - 1) * SLOTS_PER_DAY + SLOT_ORDER[row.slot]
        demand[t] = demand.get(t, 0) + workers
    for day in range(1, horizon_days + 1):
        for local in range(SLOTS_PER_DAY):
            t = (day - 1) * SLOTS_PER_DAY + local
            demand.setdefault(t, 0)
    return demand


def prune_redundant_setups(schedule_df: pd.DataFrame, affected_line_days: set[tuple[str, int]]) -> pd.DataFrame:
    """order_id를 idle로 되돌린 뒤, 그로 인해 필요 없어진 "다른 주문"의
    셋업을 찾아서 같이 idle로 되돌린다.

    scheduling/solver.py의 셋업 규칙: 어떤 셋업(그 다음 슬롯 생산의
    직전 슬롯)이 실제로 필요한지는 "그 셋업 바로 앞에서 가장 가까운
    생산 블록이 무엇이었나"로 정해진다.
      - 그 지점까지 하루 동안 생산이 전혀 없었으면(day-start부터 계속
        idle, fresh 상태) -> 셋업 불필요(fresh->생산은 항상 무료).
      - 바로 앞 생산 블록이 이 셋업과 "같은 주문"이면(idle_nf_of로
        대기하다가 재개하는 것) -> 셋업 불필요.
      - 바로 앞 생산 블록이 "다른 주문"이면 -> 셋업 필요(유지).

    주문 하나를 제거해서 그 주문이 차지하던 슬롯이 idle이 되면, 이
    앞/뒤 관계가 바뀔 수 있다(예: 뒤 주문의 셋업이 사실 이 주문의 생산을
    거쳐서 왔던 건데, 이 주문이 사라지면서 그 앞이 fresh나 같은 주문으로
    바뀌는 경우). 그래서 이 주문이 있었던 (라인, 날짜)만 다시 훑는다 -
    그 외의 (라인, 날짜)는 이 주문과 무관하므로 셋업 필요 여부가 안
    바뀐다."""
    df = schedule_df.reset_index(drop=True)
    for line_id, day in affected_line_days:
        day_idx = df.index[(df["line_id"] == line_id) & (df["day"] == day)]
        if len(day_idx) == 0:
            continue
        cell_at: list[tuple[str, str] | None] = [None] * SLOTS_PER_DAY
        row_idx_by_local: dict[int, int] = {}
        for idx in day_idx:
            local = SLOT_ORDER[df.at[idx, "slot"]]
            cell_at[local] = (df.at[idx, "activity"], df.at[idx, "order_id"])
            row_idx_by_local[local] = idx

        for local in range(SLOTS_PER_DAY):
            cell = cell_at[local]
            if cell is None or cell[0] != "setup":
                continue
            back_order = cell[1]
            front_order = None
            for p in range(local - 1, -1, -1):
                prev_cell = cell_at[p]
                if prev_cell is not None and prev_cell[0] == "produce":
                    front_order = prev_cell[1]
                    break
            if front_order is None or front_order == back_order:
                idx = row_idx_by_local[local]
                df.loc[idx, ["activity", "product_id", "order_id"]] = ["idle", "", ""]
    return df


def daily_breakdown(demand: dict[int, float], horizon_days: int) -> dict[int, dict]:
    """일별로 (정규 고용인원, 17-18/18-19 잔업인원, 잔업시간)을 뽑는다.
    잔업시간은 그 두 잔업 슬롯 중 실제로 인원>0인 슬롯 개수(0~2시간)."""
    ot_local = list(OVERTIME_LOCAL_SLOTS)
    out: dict[int, dict] = {}
    for day in range(1, horizon_days + 1):
        day_slots = [demand.get((day - 1) * SLOTS_PER_DAY + local, 0) for local in range(SLOTS_PER_DAY)]
        ot17 = day_slots[ot_local[0]] if len(day_slots) > ot_local[0] else 0
        ot18 = day_slots[ot_local[1]] if len(day_slots) > ot_local[1] else 0
        out[day] = {
            "workforce": max(day_slots) if day_slots else 0,
            "overtime_17_18": ot17,
            "overtime_18_19": ot18,
            "overtime_hours": (1 if ot17 > 0 else 0) + (1 if ot18 > 0 else 0),
        }
    return out


def daily_cost(d: dict, daily_wage: float, hourly_wage: float, overtime_multiplier: float) -> float:
    return d["workforce"] * daily_wage + (d["overtime_17_18"] + d["overtime_18_19"]) * hourly_wage * overtime_multiplier


def main():
    parser = argparse.ArgumentParser(description="이미 짜둔 계획에서 특정 주문 1건을 빼고, 그로 인한 인원/인건비 절감을 보여주기")
    parser.add_argument("--order-id", required=True, help="계획에서 제외할 주문 order_id")
    parser.add_argument("--excel-path", default=pfo.DEFAULT_EXCEL_PATH, help="주문 라인타입별 투입인원을 다시 찾을 때 쓸 수주진행현황 엑셀 경로")
    parser.add_argument("--reference-date", default=None, help="기존 계획을 만들 때 쓴 기준일(YYYY-MM-DD, 생략하면 오늘) - plan_from_orders.py와 맞춰야 함")
    parser.add_argument("--horizon-days", type=int, default=pfo.MAX_DEADLINE_DAY)
    parser.add_argument(
        "--dir",
        default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output", "real_plan"),
        help="기존 line_schedule.csv 등이 있는 디렉터리",
    )
    parser.add_argument("--out-dir", default=None, help="결과 저장 폴더 (기본: <dir>/remove_order)")
    parser.add_argument("--daily-wage", type=float, default=120_000)
    parser.add_argument("--hourly-wage", type=float, default=None)
    parser.add_argument("--overtime-multiplier", type=float, default=1.5)
    args = parser.parse_args()

    reference_date = date.fromisoformat(args.reference_date) if args.reference_date else date.today()
    hourly_wage = args.hourly_wage if args.hourly_wage is not None else args.daily_wage / 8.0

    schedule_path = os.path.join(args.dir, "line_schedule.csv")
    schedule_df = pd.read_csv(schedule_path)

    if args.order_id not in set(schedule_df["order_id"].dropna().unique()):
        raise SystemExit(f"[오류] order_id={args.order_id!r}가 line_schedule.csv에 없습니다(오타를 확인하세요).")

    raw_orders, _load_stats = pfo.load_orders_from_excel(args.excel_path, reference_date=reference_date)
    orders, _filter_stats = pfo.filter_and_attach_rates(raw_orders)
    order_by_id = {o.order_id: o for o in orders}
    if args.order_id not in order_by_id:
        print(
            f"[경고] order_id={args.order_id!r}를 지금 엑셀에서 다시 찾을 수 없습니다(엑셀이 그 사이 바뀌었을 수 있음) - "
            f"이 주문의 라인타입별 투입인원 정보를 모르므로 인원/인건비 절감분이 실제보다 적게(0으로) 계산될 수 있습니다."
        )

    lines = pfo.build_lines()
    pools = build_line_pools(lines, orders)
    type_by_physical_line: dict[str, str] = {}
    for line, pool in zip(lines, pools):
        for pid in pool.line_ids:
            type_by_physical_line[pid] = line.line_type_id

    removed_rows = schedule_df[schedule_df["order_id"] == args.order_id]
    affected_lines = sorted(removed_rows["line_id"].unique())
    print(f"[정보] {args.order_id} 제거 - 영향받는 라인 {len(affected_lines)}개: {affected_lines}")

    schedule_after = schedule_df.copy()
    mask = schedule_after["order_id"] == args.order_id
    schedule_after.loc[mask, ["activity", "product_id", "order_id"]] = ["idle", "", ""]

    affected_line_days = set(zip(removed_rows["line_id"], removed_rows["day"]))
    schedule_after = prune_redundant_setups(schedule_after, affected_line_days)

    demand_before = compute_demand(schedule_df, order_by_id, type_by_physical_line, args.horizon_days)
    demand_after = compute_demand(schedule_after, order_by_id, type_by_physical_line, args.horizon_days)
    stats_before = daily_breakdown(demand_before, args.horizon_days)
    stats_after = daily_breakdown(demand_after, args.horizon_days)

    print(f"\n=== {args.order_id} 제거로 영향받은 날짜별 인원/인건비 변화 (1일차~{args.horizon_days}일차) ===")
    any_changed = False
    for day in range(1, args.horizon_days + 1):
        b, a = stats_before[day], stats_after[day]
        if b == a:
            continue
        any_changed = True
        cost_b = daily_cost(b, args.daily_wage, hourly_wage, args.overtime_multiplier)
        cost_a = daily_cost(a, args.daily_wage, hourly_wage, args.overtime_multiplier)
        print(
            f"  {day}일차: 고용인원 {b['workforce']:.0f} -> {a['workforce']:.0f}(-{b['workforce'] - a['workforce']:.0f}), "
            f"잔업인원(17-18) {b['overtime_17_18']:.0f} -> {a['overtime_17_18']:.0f}, "
            f"잔업인원(18-19) {b['overtime_18_19']:.0f} -> {a['overtime_18_19']:.0f}, "
            f"잔업시간 {b['overtime_hours']}시간 -> {a['overtime_hours']}시간, "
            f"인건비 {cost_b:,.0f} -> {cost_a:,.0f}(-{cost_b - cost_a:,.0f})"
        )
    if not any_changed:
        print("  (인원/인건비에 변화가 없습니다 - 이 주문의 생산 슬롯이 그 날의 최대 동시인원에 영향을 안 준 것으로 보입니다)")

    total_cost_before = sum(daily_cost(stats_before[d], args.daily_wage, hourly_wage, args.overtime_multiplier) for d in stats_before)
    total_cost_after = sum(daily_cost(stats_after[d], args.daily_wage, hourly_wage, args.overtime_multiplier) for d in stats_after)
    print(
        f"\n총 인건비(일급+잔업수당 포함, 1일차~{args.horizon_days}일차): "
        f"기존 {total_cost_before:,.0f} -> 신규 {total_cost_after:,.0f} (절감분 {total_cost_before - total_cost_after:,.0f})"
    )

    out_dir = args.out_dir or os.path.join(args.dir, "remove_order")
    os.makedirs(out_dir, exist_ok=True)

    schedule_out_path = os.path.join(out_dir, "line_schedule.csv")
    schedule_after.to_csv(schedule_out_path, index=False, encoding="utf-8-sig")

    workforce_rows = [
        {"day": day, "workforce": stats_after[day]["workforce"],
         "overtime_17_18": stats_after[day]["overtime_17_18"], "overtime_18_19": stats_after[day]["overtime_18_19"]}
        for day in range(1, args.horizon_days + 1)
    ]
    workforce_out_path = os.path.join(out_dir, "daily_workforce.csv")
    pd.DataFrame(workforce_rows).to_csv(workforce_out_path, index=False, encoding="utf-8-sig")

    # order_fulfillment.csv가 있으면 이 주문 행을 빼서 같이 저장한다(order_gantt.py 등이 참고).
    fulfillment_path_in = os.path.join(args.dir, "order_fulfillment.csv")
    if os.path.exists(fulfillment_path_in):
        fulfillment = pd.read_csv(fulfillment_path_in)
        fulfillment = fulfillment[fulfillment["order_id"] != args.order_id]
        fulfillment_out_path = os.path.join(out_dir, "order_fulfillment.csv")
        fulfillment.to_csv(fulfillment_out_path, index=False, encoding="utf-8-sig")
        print(f"[저장 완료] {fulfillment_out_path}")

    print(f"[저장 완료] {schedule_out_path}")
    print(f"[저장 완료] {workforce_out_path}")
    print(
        f"\n[안내] order_gantt.py 등으로 이 결과를 보려면 --data-dir로 이 폴더를 지정하세요, 예:\n"
        f"  python order_gantt.py --data-dir {out_dir}"
    )


if __name__ == "__main__":
    main()
