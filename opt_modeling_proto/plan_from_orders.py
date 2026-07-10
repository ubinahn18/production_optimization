# -*- coding: utf-8 -*-
"""
plan_from_orders.py

실제 수주 데이터(data_pipeline/orders_from_excel.py로 엑셀에서 읽어온
Order 목록)를 이용해 생산 스케줄링을 돌리는 실행 진입점.

schedule_optimizer.py가 내장 예시 데이터로 CP-SAT 모델을 시연하는
스크립트라면, 이 스크립트는 그 다음 단계 - 실제 수주 엑셀에서 읽은
주문으로 실제 스케줄을 뽑아본다. 다만 "주문마다 각 라인에서 rate/workers가
얼마인지" 계산하는 로직은 아직 없어서(다음 단계 예정), 지금은 "같은
제품군(category)이면 그 제품군이 쓸 수 있는 라인들의 rate/workers도
다 같다"는 단순화된 가정으로 대체한다 - CATEGORY_LINE_SPECS의 숫자는
실측값이 아니라 사용자가 알려준 대략적인 라인 스펙이다.

대상 주문 필터(orders_from_excel.py 자체의 납기/상태 필터 위에 추가로 적용):
  category가 {마스크, 튜브, 용기} 중 하나 AND (deadline_day가 없음(ASAP)
  이거나 MAX_DEADLINE_DAY 이하).
"""

from __future__ import annotations

import argparse
import io
import os
import sys
from datetime import date

# Windows 콘솔(cp949 등) 기본 인코딩에서는 한글 출력이 깨지므로 UTF-8로 강제.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from data_pipeline.orders_from_excel import load_orders_from_excel
from scheduling.models import Line, Order, ScheduleConfig
from scheduling.report import plot_gantt, print_report, save_outputs
from scheduling.solver import build_and_solve

DEFAULT_EXCEL_PATH = r"C:\Users\USER\Desktop\유빈_생산계획\수주진행현황(25년12월~) (20260708).xlsx"

# 제품군(category)별로 사용 가능한 라인 타입 + 그 라인에서 이 제품군을
# 생산할 때 필요한 인원(workers)/시간당 생산량(rate). "같은 제품군이면
# 전부 같은 값"이라는 단순화 가정 - 실제로는 제품(주문)마다 조금씩
# 다르지만 그 세부 계산은 다음 단계에서 대체될 예정.
CATEGORY_LINE_SPECS: dict[str, list[dict]] = {
    "용기": [
        {"line_type_id": "셀라인", "count": 3, "workers": 10, "rate": 2340},
        {"line_type_id": "단발", "count": 19, "workers": 6, "rate": 1140},
    ],
    "마스크": [
        {"line_type_id": "로타리", "count": 2, "workers": 7, "rate": 3000},
        {"line_type_id": "10열기", "count": 6, "workers": 11, "rate": 8750},
    ],
    "튜브": [
        {"line_type_id": "튜브라인", "count": 3, "workers": 10, "rate": 2100},
    ],
}

MAX_DEADLINE_DAY = 30


def build_lines() -> list[Line]:
    """CATEGORY_LINE_SPECS에 정의된 라인 타입들을 그대로 Line 목록으로
    만든다. 카테고리 간에 라인 타입이 겹치지 않으므로(셀라인/단발은
    용기 전용, 로타리/10열기는 마스크 전용, 튜브라인은 튜브 전용)
    line_type_id 충돌 걱정은 없다."""
    lines = []
    for category, specs in CATEGORY_LINE_SPECS.items():
        for spec in specs:
            lines.append(Line(line_type_id=spec["line_type_id"], category=category, count=spec["count"]))
    return lines


# 지금은 그냥 line_type_id 가 rate와 workers를 결정하도록 했으니까 모든 order에 대해서 category만 읽으면 자동으로 
# o.rate와 o.workers 딕셔너리가 채워짐. 나중에는 채우는 걸 따로 할 것임

def filter_and_attach_rates(orders: list[Order]) -> tuple[list[Order], dict[str, int]]:
    """category가 CATEGORY_LINE_SPECS에 없는 주문(마스크/튜브/용기가
    아닌 벌크/파우치/샤쉐 등)과 납기가 MAX_DEADLINE_DAY를 넘는 주문을
    걸러내고, 남은 주문에는 그 제품군의 rate/workers를 채워 넣는다
    (orders_from_excel.py가 만드는 Order는 rate/workers가 항상 빈
    dict라서 여기서 처음 채워짐)."""
    kept: list[Order] = []
    stats = {"total": len(orders), "excluded_category": 0, "excluded_deadline": 0, "included": 0}
    for o in orders:
        specs = CATEGORY_LINE_SPECS.get(o.category)
        if specs is None:
            stats["excluded_category"] += 1
            continue
        if o.deadline_day is not None and o.deadline_day > MAX_DEADLINE_DAY:
            stats["excluded_deadline"] += 1
            continue
        o.rate = {s["line_type_id"]: s["rate"] for s in specs}
        o.workers = {s["line_type_id"]: s["workers"] for s in specs}
        kept.append(o)
    stats["included"] = len(kept)
    return kept, stats


def main():
    parser = argparse.ArgumentParser(description="실제 수주 데이터 기반 생산 스케줄링 (CP-SAT)")
    parser.add_argument("--excel-path", default=DEFAULT_EXCEL_PATH, help="수주진행현황 엑셀 경로")
    parser.add_argument("--reference-date", default=None, help="기준일(YYYY-MM-DD, 생략하면 오늘)")
    parser.add_argument("--horizon-days", type=int, default=MAX_DEADLINE_DAY)
    parser.add_argument("--daily-wage", type=float, default=200_000, help="1인 1일 고용 정액임금")
    parser.add_argument("--hourly-wage", type=float, default=None, help="잔업수당 계산용 시급 (미지정시 daily-wage/8)")
    parser.add_argument("--overtime-multiplier", type=float, default=1.5)
    parser.add_argument(
        "--backlog-cost", type=float, default=100.0,
        help="ASAP 주문(마감일 없음)의 하루당 미생산 1개당 지연비용 기본값(원). 주문별로 override 가능.",
    )
    parser.add_argument("--time-limit", type=float, default=60.0, help="1단계(인건비 최소화) CP-SAT 탐색 제한시간(초)")
    parser.add_argument(
        "--secondary-time-limit", type=float, default=60.0,
        help="2단계(연속성 최적화) CP-SAT 탐색 제한시간(초)",
    )
    parser.add_argument(
        "--no-continuity", action="store_true",
        help="2단계 연속성 최적화를 생략하고 1단계(순수 비용 최소화) 결과만 사용",
    )
    parser.add_argument("--no-plot", action="store_true", help="간트 차트 PNG 생성 생략")
    parser.add_argument(
        "--output-dir",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "real_plan"),
    )
    args = parser.parse_args()

    reference_date = date.fromisoformat(args.reference_date) if args.reference_date else None
    raw_orders = load_orders_from_excel(args.excel_path, reference_date=reference_date)

    orders, stats = filter_and_attach_rates(raw_orders)
    print(
        f"[정보] 엑셀 기반 주문 {stats['total']}건 -> "
        f"제품군(마스크/튜브/용기 아님) 제외 {stats['excluded_category']}건, "
        f"납기 {MAX_DEADLINE_DAY}일 초과 제외 {stats['excluded_deadline']}건 "
        f"-> 최종 {stats['included']}건"
    )
    if not orders:
        print("[정보] 스케줄링할 주문이 없습니다. 종료.")
        return

    lines = build_lines()
    print(f"[정보] 라인 {len(lines)}종 (물리 라인 총 {sum(l.count for l in lines)}대)")

    config = ScheduleConfig(
        horizon_days=args.horizon_days,
        daily_wage=args.daily_wage,
        hourly_wage=args.hourly_wage,
        overtime_multiplier=args.overtime_multiplier,
        time_limit_seconds=args.time_limit,
        secondary_time_limit_seconds=args.secondary_time_limit,
        optimize_continuity=not args.no_continuity,
        default_backlog_cost_per_unit_per_day=args.backlog_cost,
    )
    print(
        f"[정보] 임금 설정: 일급 {config.daily_wage:,.0f} / 시급(잔업기준) "
        f"{config.resolved_hourly_wage():,.0f} / 잔업배수 {config.overtime_multiplier}x"
    )

    result = build_and_solve(lines, orders, config)
    print_report(result, orders)

    os.makedirs(args.output_dir, exist_ok=True)
    save_outputs(result, orders, args.output_dir)
    if not args.no_plot:
        plot_gantt(result, orders, config, args.output_dir)


if __name__ == "__main__":
    main()
