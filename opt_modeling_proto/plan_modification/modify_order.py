# -*- coding: utf-8 -*-
"""
modify_order.py

이미 짜둔 계획에서 특정 주문 하나의 수량/납기/earliest-start만 바꾸고
싶을 때 쓴다. 내부적으로는:
  1) remove_order.py로 그 주문을 계획에서 통째로 뺀다(_removed_stage/
     임시 폴더에 저장).
  2) plan_additional_order.py로 "같은 order_id"를, 안 바뀐 속성
     (호환 라인타입/라인타입별 rate·workers/product_id/vendor/category)은
     원래 값 그대로 유지한 채, 새 수량/납기/earliest-start로 다시
     끼워넣는다(--out-dir에 저장).

remove_order.py/plan_additional_order.py 둘 다 전혀 건드리지 않고 그대로
불러와서 쓴다. 다만 그 둘의 콘솔 로그(제거 단계/재투입 단계 각각의
중간 진행상황)는 여기서는 굳이 안 보여주고, 대신 "제거+재투입을 합쳐서
결과적으로" 바뀐 것만 보여준다:
  - 영향받은 날짜별 고용인원/잔업인원/잔업시간/인건비 변화(원래 계획 vs
    최종 계획을 직접 비교 - remove_order.py의 compute_demand/
    daily_breakdown/daily_cost를 그대로 재사용).
  - 이 주문의 원래 생산계획과 바뀐 생산계획을 위아래로 나란히 그린 PNG
    하나(order_gantt.py의 plot_order_comparison).

두 하위 스크립트 중 하나라도 실패하면(제거 대상 order_id를 못 찾음,
재투입이 물리적으로 불가능함 등) 그 단계의 실제 로그를 그대로 보여주고
중단한다 - 조용히 실패한 것처럼 넘어가지 않는다.

플래그로 안 준 값은 원래 주문 값을 그대로 쓴다:
  --quantity: 안 주면 원래 주문의 quantity.
  --deadline-date: 안 주면 원래 주문의 마감일(ASAP 주문이었으면 계속 ASAP).
  --earliest-start-date: 안 주면 원래 주문의 earliest-start(없었으면 계속 없음).

호환 라인타입/라인타입별 rate·workers/product_id/vendor/category는
플래그로 바꿀 수 없다 - "이 속성들은 전부 그대로"라는 게 이 스크립트의
전제라서, 항상 원래 주문(엑셀) 값을 그대로 복사해서 쓴다.

사용법:
    python modify_order.py --order-id SOZ20251200010 --deadline-date 2026-08-05
    python modify_order.py --order-id SOZ20251200010 --quantity 50000 --earliest-start-date 2026-08-01
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
from datetime import date, timedelta

# Windows 콘솔(cp949 등) 기본 인코딩에서는 한글 출력이 깨지므로 UTF-8로 강제.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # opt_modeling_proto/ 를 import 경로에 추가
import plan_from_orders as pfo
from scheduling.pooling import build_line_pools

_ORDER_GANTT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output", "real_plan", "prod_tendency"
)
sys.path.insert(0, _ORDER_GANTT_DIR)
import order_gantt  # noqa: E402 - prod_tendency/order_gantt.py의 plot_order_comparison을 재사용

# remove_order.py/plan_additional_order.py는 이 파일과 같은 폴더(plan_modification/)에
# 있으므로 스크립트 자기 자신의 디렉터리가 이미 sys.path에 잡혀 있어(python이
# 직접 실행 시 자동으로 넣어줌) 별도 경로 조작 없이 바로 import된다.
import remove_order as ro
import plan_additional_order as pao


def _run_suppressed(main_fn, label: str) -> str:
    """main_fn()을 호출하되 그 안의 콘솔 출력(중간 진행상황)은 버퍼에
    가둬서 화면에 안 보이게 하고, 버퍼 내용을 돌려준다(실패 시 진단용으로
    출력하기 위함). main_fn이 SystemExit을 던지면(치명적 오류) 지금까지
    가둬둔 로그를 먼저 보여주고 다시 던진다."""
    print(f"[{label}] 진행 중...")
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            main_fn()
    except SystemExit as e:
        sys.stdout.write(buf.getvalue())
        raise SystemExit(f"[오류] {label} 단계 실패: {e}")
    return buf.getvalue()


def main():
    parser = argparse.ArgumentParser(description="이미 짜둔 계획에서 주문 1건의 수량/납기/earliest-start를 바꾸기(remove_order + plan_additional_order 순차 실행)")
    parser.add_argument("--order-id", required=True, help="수정할 주문 order_id")
    parser.add_argument("--quantity", type=int, default=None, help="새 필요 생산수량 - 안 주면 원래 주문의 quantity 유지")
    parser.add_argument("--deadline-date", default=None, help="새 납기(YYYY-MM-DD) - 안 주면 원래 주문의 납기 유지(ASAP였으면 계속 ASAP)")
    parser.add_argument("--earliest-start-date", default=None, help="새 earliest-start(YYYY-MM-DD) - 안 주면 원래 주문의 earliest-start 유지")
    parser.add_argument("--excel-path", default=pfo.DEFAULT_EXCEL_PATH, help="원래 주문의 호환 라인타입/rate/workers 등을 찾을 때 쓸 수주진행현황 엑셀 경로")
    parser.add_argument("--reference-date", default=None, help="기존 계획을 만들 때 쓴 기준일(YYYY-MM-DD, 생략하면 오늘) - plan_from_orders.py와 맞춰야 함")
    parser.add_argument("--horizon-days", type=int, default=pfo.MAX_DEADLINE_DAY)
    parser.add_argument("--dir", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output", "real_plan"),
                         help="기존 line_schedule.csv 등이 있는 디렉터리")
    parser.add_argument("--out-dir", default=None, help="최종 결과 저장 폴더 (기본: <dir>/modify_order)")
    parser.add_argument("--daily-wage", type=float, default=120_000)
    parser.add_argument("--hourly-wage", type=float, default=None)
    parser.add_argument("--overtime-multiplier", type=float, default=1.5)
    parser.add_argument("--backlog-cost", type=float, default=100.0, help="ASAP 주문 기본 backlog 단가(원/개/일)")
    parser.add_argument("--closed-date", action="append", default=[])
    parser.add_argument("--no-weekend-closure", action="store_true")
    parser.add_argument("--time-limit", type=float, default=60.0, help="plan_additional_order.py의 tier별 CP-SAT 탐색 제한시간(초)")
    args = parser.parse_args()

    reference_date = date.fromisoformat(args.reference_date) if args.reference_date else date.today()
    hourly_wage = args.hourly_wage if args.hourly_wage is not None else args.daily_wage / 8.0

    raw_orders, _load_stats = pfo.load_orders_from_excel(args.excel_path, reference_date=reference_date)
    orders, _filter_stats = pfo.filter_and_attach_rates(raw_orders)
    order_by_id = {o.order_id: o for o in orders}
    original = order_by_id.get(args.order_id)
    if original is None:
        raise SystemExit(
            f"[오류] order_id={args.order_id!r}를 지금 엑셀에서 찾을 수 없습니다 - "
            f"호환 라인타입/rate/workers 등을 원래 주문에서 그대로 가져와야 하는데 그 원본을 못 찾았습니다."
        )

    new_quantity = args.quantity if args.quantity is not None else original.quantity

    if args.deadline_date is not None:
        new_deadline_date: date | None = date.fromisoformat(args.deadline_date)
    elif original.deadline_day is not None:
        new_deadline_date = reference_date + timedelta(days=original.deadline_day - 1)
    else:
        new_deadline_date = None  # 원래도 ASAP였음 - 계속 ASAP

    if args.earliest_start_date is not None:
        new_earliest_start_date: date | None = date.fromisoformat(args.earliest_start_date)
    elif original.earliest_start_day is not None:
        new_earliest_start_date = reference_date + timedelta(days=original.earliest_start_day - 1)
    else:
        new_earliest_start_date = None

    new_deadline_day = (new_deadline_date - reference_date).days + 1 if new_deadline_date is not None else None

    print(
        f"[정보] {args.order_id} 수정: 수량 {original.quantity:,.0f} -> {new_quantity:,.0f}, "
        f"납기 {original.deadline_day if original.deadline_day is not None else 'ASAP'} -> "
        f"{new_deadline_date if new_deadline_date is not None else 'ASAP'}, "
        f"earliest-start {original.earliest_start_day if original.earliest_start_day is not None else '(없음)'} -> "
        f"{new_earliest_start_date if new_earliest_start_date is not None else '(없음)'}"
    )
    print(f"[정보] 호환 라인타입/rate/workers/product_id/vendor/category는 원래 주문 값 그대로 유지: {list(original.rate)}")

    out_dir = args.out_dir or os.path.join(args.dir, "modify_order")
    removed_stage_dir = os.path.join(out_dir, "_removed_stage")

    # ---- 1단계: remove_order.py로 이 주문을 통째로 제거 ----
    sys.argv = [
        "remove_order.py",
        "--order-id", args.order_id,
        "--excel-path", args.excel_path,
        "--reference-date", reference_date.isoformat(),
        "--horizon-days", str(args.horizon_days),
        "--dir", args.dir,
        "--out-dir", removed_stage_dir,
        "--daily-wage", str(args.daily_wage),
        "--hourly-wage", str(hourly_wage),
        "--overtime-multiplier", str(args.overtime_multiplier),
    ]
    captured_remove = _run_suppressed(ro.main, "1/2 제거")
    if not os.path.exists(os.path.join(removed_stage_dir, "line_schedule.csv")):
        print(captured_remove)
        raise SystemExit("[오류] 제거 단계에서 결과가 저장되지 않았습니다(위 로그 참고).")

    # ---- 2단계: plan_additional_order.py로 같은 order_id를 새 수량/납기/earliest-start로 재투입 ----
    pao.NEW_ORDER_ID = args.order_id
    pao.NEW_ORDER_PRODUCT_ID = original.product_id
    pao.NEW_ORDER_PRODUCT_NAME = original.product_name
    pao.NEW_ORDER_VENDOR = original.vendor
    pao.NEW_ORDER_CATEGORY = original.category
    pao.NEW_ORDER_QUANTITY = new_quantity
    pao.NEW_ORDER_DEADLINE_DATE = new_deadline_date
    pao.NEW_ORDER_EARLIEST_START_DATE = new_earliest_start_date
    pao.NEW_ORDER_RATE_BY_LINE_TYPE = dict(original.rate)
    pao.NEW_ORDER_WORKERS_BY_LINE_TYPE = dict(original.workers)
    pao.NEW_ORDER_BACKLOG_COST_PER_UNIT_PER_DAY = original.backlog_cost_per_unit_per_day

    sys.argv = [
        "plan_additional_order.py",
        "--excel-path", args.excel_path,
        "--reference-date", reference_date.isoformat(),
        "--adding-date", reference_date.isoformat(),  # 제거로 비워진 슬롯 전체를 재배정 대상으로 고려
        "--horizon-days", str(args.horizon_days),
        "--dir", removed_stage_dir,
        "--out-dir", out_dir,
        "--daily-wage", str(args.daily_wage),
        "--hourly-wage", str(hourly_wage),
        "--overtime-multiplier", str(args.overtime_multiplier),
        "--backlog-cost", str(args.backlog_cost),
        "--time-limit", str(args.time_limit),
    ]
    for d in args.closed_date:
        sys.argv += ["--closed-date", d]
    if args.no_weekend_closure:
        sys.argv += ["--no-weekend-closure"]
    captured_add = _run_suppressed(pao.main, "2/2 재투입")
    if not os.path.exists(os.path.join(out_dir, "line_schedule.csv")):
        print(captured_add)
        raise SystemExit(
            f"[오류] 재투입 단계에서 실패했습니다(위 로그 참고) - "
            f"{args.order_id}가 새 조건(수량 {new_quantity:,.0f}, 납기 {new_deadline_date})으로 "
            f"다시 들어갈 자리를 못 찾았을 수 있습니다."
        )

    # ---- 결과적으로 바뀐 것: 원래 계획(before) vs 최종 계획(after) 직접 비교 ----
    lines = pfo.build_lines()
    pools = build_line_pools(lines, orders)
    type_by_physical_line: dict[str, str] = {}
    for line, pool in zip(lines, pools):
        for pid in pool.line_ids:
            type_by_physical_line[pid] = line.line_type_id

    before_schedule = pd.read_csv(os.path.join(args.dir, "line_schedule.csv"))
    after_schedule = pd.read_csv(os.path.join(out_dir, "line_schedule.csv"))

    demand_before = ro.compute_demand(before_schedule, order_by_id, type_by_physical_line, args.horizon_days)
    demand_after = ro.compute_demand(after_schedule, order_by_id, type_by_physical_line, args.horizon_days)
    stats_before = ro.daily_breakdown(demand_before, args.horizon_days)
    stats_after = ro.daily_breakdown(demand_after, args.horizon_days)

    print(f"\n=== {args.order_id} 수정으로 결과적으로 영향받은 날짜별 인원/인건비 변화 (1일차~{args.horizon_days}일차) ===")
    any_changed = False
    for day in range(1, args.horizon_days + 1):
        b, a = stats_before[day], stats_after[day]
        if b == a:
            continue
        any_changed = True
        cost_b = ro.daily_cost(b, args.daily_wage, hourly_wage, args.overtime_multiplier)
        cost_a = ro.daily_cost(a, args.daily_wage, hourly_wage, args.overtime_multiplier)
        print(
            f"  {day}일차: 고용인원 {b['workforce']:.0f} -> {a['workforce']:.0f}({a['workforce'] - b['workforce']:+.0f}), "
            f"잔업인원(17-18) {b['overtime_17_18']:.0f} -> {a['overtime_17_18']:.0f}, "
            f"잔업인원(18-19) {b['overtime_18_19']:.0f} -> {a['overtime_18_19']:.0f}, "
            f"잔업시간 {b['overtime_hours']}시간 -> {a['overtime_hours']}시간, "
            f"인건비 {cost_b:,.0f} -> {cost_a:,.0f}({cost_a - cost_b:+,.0f})"
        )
    if not any_changed:
        print("  (인원/인건비에 변화가 없습니다)")

    total_cost_before = sum(ro.daily_cost(stats_before[d], args.daily_wage, hourly_wage, args.overtime_multiplier) for d in stats_before)
    total_cost_after = sum(ro.daily_cost(stats_after[d], args.daily_wage, hourly_wage, args.overtime_multiplier) for d in stats_after)
    print(
        f"\n총 인건비(일급+잔업수당 포함, 1일차~{args.horizon_days}일차): "
        f"기존 {total_cost_before:,.0f} -> 신규 {total_cost_after:,.0f} ({total_cost_after - total_cost_before:+,.0f})"
    )

    # ---- 이 주문의 변경 전/후 생산계획을 위아래로 이어붙인 gantt PNG ----
    compare_path = os.path.join(out_dir, f"gantt_compare_{args.order_id}.png")
    order_gantt.plot_order_comparison(
        before_schedule, after_schedule, args.order_id, compare_path,
        before_deadline_day=original.deadline_day, after_deadline_day=new_deadline_day,
        before_label="변경 전", after_label="변경 후",
    )
    print(f"\n[저장 완료] {os.path.join(out_dir, 'line_schedule.csv')}")
    print(f"[저장 완료] {os.path.join(out_dir, 'daily_workforce.csv')}")
    print(f"[저장 완료] {os.path.join(out_dir, 'order_fulfillment.csv')}")
    print(f"[저장 완료] {compare_path}")


if __name__ == "__main__":
    main()
