# -*- coding: utf-8 -*-
"""
write_report_from_output.py

이미 plan_from_orders.py를 돌려서 output/real_plan/에 결과(line_schedule.csv,
daily_workforce.csv, order_fulfillment.csv)가 저장돼 있을 때, CP-SAT을
다시 돌리지 않고 그 결과만 가지고 plan_report.xlsx만 다시 만든다 - CP-SAT
재탐색에 몇 분씩 걸리는데 리포트 서식만 고쳤거나(plan_report.py 수정)
파일을 엑셀에서 열어둔 채로 저장에 실패했을 때, 처음부터 다시 도는
것보다 훨씬 빠르다.

주의 두 가지:
  1) 인건비 계산에 쓰이는 임금 설정(--daily-wage/--hourly-wage/
     --overtime-multiplier)은 CSV에 저장돼 있지 않으므로, plan_from_orders.py를
     돌릴 때 썼던 것과 같은 값을 여기서도 다시 줘야 "총 인건비"가
     정확하다(다르게 주면 그 항목만 틀리고 나머지는 정확함).
  2) 발주처/제품명/제품군은 엑셀을 다시 읽어서 얻는다(CSV엔 없음) - 이
     엑셀 파일이 plan_from_orders.py를 돌렸을 때와 그 사이에 바뀌었으면
     order_fulfillment.csv의 일부 주문을 못 찾을 수 있고, 그런 주문은
     경고를 남기고 리포트에서 빠진다.

사용법 (이 파일이 있는 opt_modeling_proto/ 디렉터리에서, plan_from_orders.py를
돌릴 때 쓴 것과 같은 --reference-date/--excel-path/임금 옵션으로):
    python write_report_from_output.py
    python write_report_from_output.py --reference-date 2026-07-10 --daily-wage 120000
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

import plan_from_orders as pfo
from plan_report import write_plan_report_excel
from scheduling.models import ScheduleConfig, ScheduleResult


def _load_result_from_csv(
    output_dir: str, daily_wage: float, hourly_wage: float | None, overtime_multiplier: float
) -> ScheduleResult:
    """line_schedule.csv/daily_workforce.csv/order_fulfillment.csv를 읽어서
    write_plan_report_excel이 필요로 하는 필드만 채운 ScheduleResult를
    다시 만든다. continuity_score/backlog_cost/storage_cost/total_cost는
    CSV에 없어서 None으로 둔다(plan_report.xlsx는 이 값들을 안 씀)."""
    schedule = pd.read_csv(os.path.join(output_dir, "line_schedule.csv"))
    workforce_df = pd.read_csv(os.path.join(output_dir, "daily_workforce.csv"))
    fulfillment_df = pd.read_csv(os.path.join(output_dir, "order_fulfillment.csv"))

    # activity/product_id 두 컬럼을 원래 형태("produce:P1"/"setup:P1"/"idle")로
    # 다시 합친다 - scheduling/report.py의 save_outputs가 반대로 쪼갠 것과
    # 정확히 대칭.
    line_activity: dict[str, list[tuple[int, str, str, str]]] = {}
    for row in schedule.itertuples(index=False):
        activity = row.activity if row.activity == "idle" else f"{row.activity}:{row.product_id}"
        order_id = "" if pd.isna(row.order_id) else row.order_id
        line_activity.setdefault(row.line_id, []).append((int(row.day), row.slot, activity, order_id))

    daily_workforce = {int(r.day): int(r.workforce) for r in workforce_df.itertuples(index=False)}
    overtime_workers = {
        int(r.day): {"17-18": int(r.overtime_17_18), "18-19": int(r.overtime_18_19)}
        for r in workforce_df.itertuples(index=False)
    }

    order_fulfillment = {}
    for r in fulfillment_df.itertuples(index=False):
        order_fulfillment[r.order_id] = {
            "product_id": r.product_id,
            "required": r.required,
            "produced": r.produced,
            "deadline_day": None if pd.isna(r.deadline_day) else int(r.deadline_day),
            "completion_day": None if pd.isna(r.completion_day) else int(r.completion_day),
            "final_backlog": None if pd.isna(r.final_backlog) else r.final_backlog,
        }

    resolved_hourly = hourly_wage if hourly_wage is not None else daily_wage / 8.0
    labor_cost = sum(
        daily_workforce[d] * daily_wage
        + (overtime_workers[d]["17-18"] + overtime_workers[d]["18-19"]) * resolved_hourly * overtime_multiplier
        for d in daily_workforce
    )

    return ScheduleResult(
        status_name="(output CSV에서 재구성 - CP-SAT 재탐색 안 함)",
        is_feasible=True,
        total_cost=None,
        daily_workforce=daily_workforce,
        overtime_workers=overtime_workers,
        line_activity=line_activity,
        order_fulfillment=order_fulfillment,
        labor_cost=labor_cost,
    )


def main():
    parser = argparse.ArgumentParser(
        description="CP-SAT을 다시 돌리지 않고, 이미 저장된 output/real_plan/ 결과로부터 plan_report.xlsx만 재생성"
    )
    parser.add_argument("--excel-path", default=pfo.DEFAULT_EXCEL_PATH, help="수주진행현황 엑셀 경로")
    parser.add_argument(
        "--reference-date", default=None,
        help="plan_from_orders.py를 돌릴 때 쓴 기준일(YYYY-MM-DD, 생략하면 오늘) - 반드시 그때와 맞춰야 함",
    )
    parser.add_argument("--horizon-days", type=int, default=pfo.MAX_DEADLINE_DAY)
    parser.add_argument("--daily-wage", type=float, default=120_000, help="plan_from_orders.py를 돌릴 때 쓴 값과 맞춰야 함")
    parser.add_argument("--hourly-wage", type=float, default=None)
    parser.add_argument("--overtime-multiplier", type=float, default=1.5)
    parser.add_argument(
        "--dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "real_plan"),
        help="line_schedule.csv 등이 있는 디렉터리",
    )
    args = parser.parse_args()

    reference_date = date.fromisoformat(args.reference_date) if args.reference_date else date.today()
    raw_orders, load_stats = pfo.load_orders_from_excel(args.excel_path, reference_date=reference_date)
    orders, filter_stats = pfo.filter_and_attach_rates(raw_orders)

    result = _load_result_from_csv(args.dir, args.daily_wage, args.hourly_wage, args.overtime_multiplier)

    orders_by_id = {o.order_id: o for o in orders}
    missing = sorted(set(result.order_fulfillment) - set(orders_by_id))
    if missing:
        print(
            f"[경고] order_fulfillment.csv에는 있지만 지금 엑셀에서 다시 찾을 수 없는 주문 {len(missing)}건 "
            f"(엑셀이 그 사이 바뀌었을 수 있음) - 리포트에서 제외합니다: {missing[:10]}"
            + ("..." if len(missing) > 10 else "")
        )
    matched_orders = [o for o in orders if o.order_id in result.order_fulfillment]

    lines = pfo.build_lines()
    config = ScheduleConfig(
        horizon_days=args.horizon_days, daily_wage=args.daily_wage,
        hourly_wage=args.hourly_wage, overtime_multiplier=args.overtime_multiplier,
    )

    write_plan_report_excel(
        result, matched_orders, lines, config, reference_date, load_stats, filter_stats,
        os.path.join(args.dir, "plan_report.xlsx"),
        deadline_window_days=pfo.MAX_DEADLINE_DAY + pfo.LATE_RAMP_DAYS,
    )


if __name__ == "__main__":
    main()
