# -*- coding: utf-8 -*-
"""
feasibility_check.py

plan_from_orders.py를 실제로 돌리기 전에, "물리적으로 처음부터 불가능한"
주문이 있는지 미리 걸러내는 진단 스크립트. CP-SAT 탐색이 필요 없이
데이터만 보고 즉시(밀리초 단위로) 판별 가능한 세 가지 케이스를 검사한다:

  [1] earliest_start_day > deadline_day
      (원료/부자재가 도착하기도 전에 납기가 지나가버림 - 생산 가능한
      날이 아예 없음)
  [2] [earliest_start_day, deadline_day] 구간이 전부 휴무일(주말/공휴일)
      (구간 자체는 역전이 아니지만 그 안에 근무일이 하루도 없음)
  [3] 그 구간의 근무일마다 호환 라인 전부(대수까지 반영)를 잔업 포함
      풀가동해도 필요수량을 못 채움(순수 라인 capa 부족)

세 가지 다 CP-SAT이 오래 탐색해서 겨우 알아내는 게 아니라 데이터만
보고 바로 계산되는 값이라, 이 중 하나라도 걸리면 plan_from_orders.py를
time-limit을 아무리 늘려도 그 주문 때문에 절대 FEASIBLE이 나올 수 없다.
그래서 실제 솔버를 돌리기 전에 먼저 걸러보는 용도 - 이 스크립트가
전부 0건이라고 해서 반드시 feasible이 보장되는 건 아니다(여러 주문이
같은 라인을 두고 경쟁하는 조합 문제 등은 여전히 솔버가 판단해야 함).

사용법 (이 파일이 있는 opt_modeling_proto/ 디렉터리에서, plan_from_orders.py와
거의 동일한 인자):
    python feasibility_check.py
    python feasibility_check.py --reference-date 2026-07-10 --closed-date 2026-08-15
"""

from __future__ import annotations

import argparse
import io
import sys
from datetime import date

# Windows 콘솔(cp949 등) 기본 인코딩에서는 한글 출력이 깨지므로 UTF-8로 강제.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)

import plan_from_orders as pfo
from scheduling.models import Order, SLOTS_PER_DAY


def check_inverted_window(orders: list[Order]) -> list[Order]:
    """[1] earliest_start_day > deadline_day인 주문 목록."""
    return [
        o for o in orders
        if o.earliest_start_day is not None and o.deadline_day is not None
        and o.earliest_start_day > o.deadline_day
    ]


def check_all_closed_window(orders: list[Order], closed_days: frozenset[int]) -> list[Order]:
    """[2] [earliest_start_day, deadline_day] 구간(이미 역전인 건 제외)이
    전부 휴무일인 주문 목록."""
    bad = []
    for o in orders:
        if o.deadline_day is None:
            continue
        start = o.earliest_start_day or 1
        end = o.deadline_day
        if start > end:
            continue  # [1]에서 이미 걸림 - 여기서 중복 보고하지 않음
        if all(d in closed_days for d in range(start, end + 1)):
            bad.append(o)
    return bad


def check_capacity_shortfall(
    orders: list[Order],
    count_by_type: dict[str, int],
    closed_days: frozenset[int],
    horizon_days: int,
) -> list[tuple[Order, int, int, int]]:
    """[3] 호환 라인 전부(물리 대수 반영)를 [earliest_start_day,
    deadline_day] 구간의 근무일마다 잔업 포함(SLOTS_PER_DAY시간) 풀가동해도
    필요수량을 못 채우는 주문 목록. (order, 시간당 총capa, 가용일수,
    이론상 최대생산량) 튜플로 반환."""
    bad = []
    for o in orders:
        if o.is_asap():
            continue
        compat = o.compatible_line_types()
        max_rate_sum = sum(count_by_type[lid] * o.rate[lid] for lid in compat)
        start = o.earliest_start_day or 1
        end = min(o.deadline_day, horizon_days)
        if start > end:
            continue  # [1]에서 이미 걸림
        open_days = [d for d in range(start, end + 1) if d not in closed_days]
        max_producible = max_rate_sum * SLOTS_PER_DAY * len(open_days)
        if max_producible < o.quantity:
            bad.append((o, max_rate_sum, len(open_days), max_producible))
    return bad


def main():
    parser = argparse.ArgumentParser(
        description="plan_from_orders.py를 돌리기 전에 물리적으로 처음부터 불가능한 주문이 있는지 미리 진단"
    )
    parser.add_argument("--excel-path", default=pfo.DEFAULT_EXCEL_PATH, help="수주진행현황 엑셀 경로")
    parser.add_argument("--reference-date", default=None, help="기준일(YYYY-MM-DD, 생략하면 오늘)")
    parser.add_argument("--horizon-days", type=int, default=pfo.MAX_DEADLINE_DAY)
    parser.add_argument(
        "--closed-date", action="append", default=[],
        help="생산 불가일(YYYY-MM-DD), 여러 번 지정 가능(공휴일 등) - plan_from_orders.py에 줄 값과 맞춰야 함",
    )
    parser.add_argument(
        "--no-weekend-closure", action="store_true",
        help="주말도 근무일로 취급(기본은 주말 자동 휴무)",
    )
    args = parser.parse_args()

    reference_date = date.fromisoformat(args.reference_date) if args.reference_date else date.today()
    raw_orders, _load_stats = pfo.load_orders_from_excel(args.excel_path, reference_date=reference_date)
    orders, _filter_stats = pfo.filter_and_attach_rates(raw_orders)
    print(f"[정보] 검사 대상 주문 {len(orders)}건 (기준일 {reference_date}, 계획기간 {args.horizon_days}일)")

    lines = pfo.build_lines()
    count_by_type = {l.line_type_id: l.count for l in lines}

    closed_dates = [date.fromisoformat(s) for s in args.closed_date]
    closed_days = pfo.resolve_closed_days(
        reference_date, args.horizon_days, closed_dates, close_weekends=not args.no_weekend_closure,
    )

    inverted = check_inverted_window(orders)
    all_closed = check_all_closed_window(orders, closed_days)
    shortfall = check_capacity_shortfall(orders, count_by_type, closed_days, args.horizon_days)

    print(f"\n[1] 시작가능일 > 마감일(역전): {len(inverted)}건")
    for o in inverted:
        print(f"  {o.order_id} ({o.product_name}): 시작가능일={o.earliest_start_day} 마감일={o.deadline_day}")

    print(f"\n[2] 생산가능구간이 전부 휴무일: {len(all_closed)}건")
    for o in all_closed:
        print(f"  {o.order_id} ({o.product_name}): 구간={o.earliest_start_day or 1}~{o.deadline_day}")

    print(f"\n[3] 필요수량이 이론상 최대생산량 초과(라인 풀가동 기준): {len(shortfall)}건")
    for o, rate_sum, open_days, max_prod in shortfall:
        print(
            f"  {o.order_id} ({o.category}): 필요 {o.quantity:,} vs 최대 {max_prod:,} "
            f"(시간당capa={rate_sum}, 가용일수={open_days}, 구간={o.earliest_start_day or 1}~{o.deadline_day})"
        )

    total_bad = len(inverted) + len(all_closed) + len(shortfall)
    print()
    if total_bad == 0:
        print("[결론] 문제 없음 - 이 3가지 원인으로는 infeasible이 나지 않습니다.")
    else:
        print(
            f"[결론] 총 {total_bad}건 발견 - plan_from_orders.py를 time-limit을 아무리 늘려도 "
            f"이 주문들 때문에 infeasible이 납니다."
        )


if __name__ == "__main__":
    main()
