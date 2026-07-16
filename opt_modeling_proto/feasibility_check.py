# -*- coding: utf-8 -*-
"""
feasibility_check.py

plan_from_orders.py를 실제로 돌리기 전에, "물리적으로 처음부터 불가능한"
주문이 있는지 미리 걸러내는 진단 스크립트. CP-SAT 탐색이 필요 없이
데이터만 보고 즉시(밀리초 단위로) 판별 가능한 케이스들을 검사한다:

  [1] earliest_start_day > deadline_day
      (원료/부자재가 도착하기도 전에 납기가 지나가버림 - 생산 가능한
      날이 아예 없음). filter_and_attach_rates()가 조정한(ramp 반영,
      earliest_start_day가 계획기간을 넘으면 지워버림) 최종 값 기준.
  [1b] 위와 같은 모순인데, filter_and_attach_rates()가 earliest_start_day를
      "계획기간을 넘는다"는 이유로 지워버려서(제약을 안 거는 쪽으로
      처리) [1]에서는 안 보이게 된 경우. 이 스크립트가 raw(필터링 전)
      earliest_start_day를 따로 들고 있다가, 필터링 후 조정된
      deadline_day와 다시 비교해서 잡아낸다 - plan_from_orders.py의
      실제 동작은 안 건드리고(그 스크립트가 이 케이스를 어떻게 다루는지는
      별개 문제), 진단만 정확하게 하기 위함.
  [2] [earliest_start_day, deadline_day] 구간이 전부 휴무일(주말/공휴일)
      (구간 자체는 역전이 아니지만 그 안에 근무일이 하루도 없음)
  [3] 그 구간의 근무일마다 호환 라인 전부(대수까지 반영)를 잔업 포함
      풀가동해도 필요수량을 못 채움(순수 라인 capa 부족)
  [4] 라인타입별 마감일 누적 병목(아래 check_type_bottleneck 참고)

전부 CP-SAT이 오래 탐색해서 겨우 알아내는 게 아니라 데이터만 보고 바로
계산되는 값이라, 이 중 하나라도 걸리면 plan_from_orders.py를 time-limit을
아무리 늘려도 그 주문 때문에 절대 FEASIBLE이 나올 수 없다. 그래서 실제
솔버를 돌리기 전에 먼저 걸러보는 용도 - 이 스크립트가 전부 0건이라고
해서 반드시 feasible이 보장되는 건 아니다(여러 라인타입에 걸친 주문들의
조합 경쟁 문제 등은 여전히 솔버가 판단해야 함).

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


def check_earliest_start_after_ramped_deadline(
    orders: list[Order], raw_earliest_start_by_id: dict[str, int | None],
) -> list[tuple[Order, int]]:
    """[1b] plan_from_orders.py의 filter_and_attach_rates()는 두 가지를
    한 함수 안에서 순서대로 한다: (a) 납기가 31~35일차면 80%를 30일차로
    당기는 ramp, (b) earliest_start_day가 계획기간(30일)을 넘으면 그냥
    지워버림(None) - "제약을 걸지 않는다"는 의도지만, 만약 그 주문의
    deadline_day가 (a)에서 이미 30일차로 당겨진 상태라면 이건 "원료가
    입고되기도 전에 납기가 지나가버리는" 진짜 모순이다. 그런데 (b)가
    그 모순을 만드는 원인(earliest_start_day)을 지워버리므로, 필터링이
    끝난 뒤의 Order 객체만 보는 check_inverted_window([1])는 이 모순을
    영영 못 본다.

    이 함수는 필터링 *전* raw earliest_start_day(호출부가 미리 스냅샷
    떠둔 것)를 필터링 *후* deadline_day와 다시 비교해서 이 케이스를
    잡아낸다. plan_from_orders.py 자체는 전혀 안 건드린다 - 진단
    스크립트만 더 정확하게 보게 하는 것."""
    bad = []
    for o in orders:
        if o.deadline_day is None:
            continue
        raw_es = raw_earliest_start_by_id.get(o.order_id)
        if raw_es is not None and raw_es > o.deadline_day:
            bad.append((o, raw_es))
    return bad


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


def check_type_bottleneck(
    orders: list[Order],
    count_by_type: dict[str, int],
    closed_days: frozenset[int],
    horizon_days: int,
) -> list[tuple[str, int, float, float, Order]]:
    """[4] 라인타입별 마감일 누적 병목. 앞의 [3]은 주문 하나하나를
    "그 주문 혼자 그 라인을 다 쓴다면"으로 독립적으로 체크해서, 여러
    주문이 같은 라인타입을 두고 겹치는 기간에 서로 경쟁하는 경우를
    못 잡는다. 이 체크는 "그 라인타입에서만 생산 가능한(다른 라인타입과
    호환 안 되는) 주문들"을 마감일 이른 순으로 모아서, 누적 필요시간이
    누적 가용시간(그 라인타입 총 대수 x 잔업포함 슬롯수 x 1일차부터
    그 마감일까지의 근무일수)을 넘는 지점이 있는지 확인한다 - 넘으면
    그 주문들을 전부 그 라인에 배정해도 물리적으로 마감을 못 맞춘다는
    뜻이라 확실히 infeasible이다.

    한계(필요조건이지 충분조건은 아님): (1) 호환 라인타입이 2개
    이상인 주문은 여러 라인 중 어디로 갈지 확정할 수 없어서 아예
    제외한다 - 그런 주문들끼리의 경쟁까지는 못 본다. (2)
    earliest_start_day를 무시하고 1일차부터 가용하다고 가정한다(실제
    가용시간은 이보다 적을 수 있으므로 이 체크는 오히려 관대한 쪽으로
    치우쳐 있다 - 그런데도 걸린다면 실제로는 확실히 infeasible)."""
    bad = []
    by_type: dict[str, list[Order]] = {}
    for o in orders:
        if o.is_asap():
            continue
        compat = o.compatible_line_types()
        if len(compat) != 1:
            continue  # 여러 라인타입에 걸친 주문은 이 체크에서 제외(주석 참고)
        by_type.setdefault(compat[0], []).append(o)

    for line_type, type_orders in by_type.items():
        physical_count = count_by_type.get(line_type, 0)
        type_orders_sorted = sorted(type_orders, key=lambda o: min(o.deadline_day, horizon_days))
        cumulative_required_hours = 0.0
        for o in type_orders_sorted:
            end = min(o.deadline_day, horizon_days)
            rate = o.rate.get(line_type, 0)
            if rate <= 0:
                continue
            cumulative_required_hours += o.quantity / rate
            open_days_so_far = sum(1 for d in range(1, end + 1) if d not in closed_days)
            available_hours = physical_count * SLOTS_PER_DAY * open_days_so_far
            if cumulative_required_hours > available_hours:
                bad.append((line_type, end, cumulative_required_hours, available_hours, o))
    return bad


def main():
    parser = argparse.ArgumentParser(
        description="plan_from_orders.py를 돌리기 전에 물리적으로 처음부터 불가능한 주문이 있는지 미리 진단"
    )
    parser.add_argument("--excel-path", default=pfo.DEFAULT_EXCEL_PATH, help="수주진행현황 엑셀 경로")
    parser.add_argument("--reference-date", default=None, help="기준일(YYYY-MM-DD, 생략하면 오늘)")
    parser.add_argument(
        "--read-specs", choices=["category", "excel"], default="category",
        help="라인별 rate/투입인원을 어디서 가져올지 - plan_from_orders.py에 준 --read-specs와 "
             "반드시 맞춰야 한다(안 맞으면 실제 솔버가 보는 것과 다른 데이터로 진단하게 됨). "
             "category(기본): CATEGORY_LINE_SPECS. excel: 엑셀의 제품별 라인별 컬럼(T-AH).",
    )
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
    read_specs_from_excel = args.read_specs == "excel"
    raw_orders, _load_stats = pfo.load_orders_from_excel(
        args.excel_path, reference_date=reference_date, read_specs_from_excel=read_specs_from_excel,
    )
    # filter_and_attach_rates()가 earliest_start_day를 제자리에서(in-place)
    # 지워버리기 전에 원본 값을 먼저 스냅샷해둔다 - [1b]에서 씀.
    raw_earliest_start_by_id = {o.order_id: o.earliest_start_day for o in raw_orders}
    orders, filter_stats = pfo.filter_and_attach_rates(raw_orders, read_specs_from_excel=read_specs_from_excel)
    print(f"[정보] rate/투입인원 출처: {args.read_specs}")
    print(f"[정보] 검사 대상 주문 {len(orders)}건 (기준일 {reference_date}, 계획기간 {args.horizon_days}일)")
    if filter_stats["excluded_earliest_start_conflict"]:
        print(
            f"[정보] 첫 생산 가능일이 계획 기간보다 늦어서 filter_and_attach_rates가 이미 제외한 주문 "
            f"{filter_stats['excluded_earliest_start_conflict']}건 - 계획기간 안에서는 절대 생산 불가능. "
            f"아래 [1b]는 이제 이 경로로는 항상 0건이 나온다(이미 제외됐으니 볼 수가 없음) - "
            f"혹시 이 값이 필요조건을 놓치는 경우를 대비한 안전망으로 남겨둠."
        )

    lines = pfo.build_lines()
    count_by_type = {l.line_type_id: l.count for l in lines}

    closed_dates = [date.fromisoformat(s) for s in args.closed_date]
    closed_days = pfo.resolve_closed_days(
        reference_date, args.horizon_days, closed_dates, close_weekends=not args.no_weekend_closure,
    )

    inverted = check_inverted_window(orders)
    ramp_hidden = check_earliest_start_after_ramped_deadline(orders, raw_earliest_start_by_id)
    all_closed = check_all_closed_window(orders, closed_days)
    shortfall = check_capacity_shortfall(orders, count_by_type, closed_days, args.horizon_days)
    bottleneck = check_type_bottleneck(orders, count_by_type, closed_days, args.horizon_days)

    print(f"\n[1] 시작가능일 > 마감일(역전): {len(inverted)}건")
    for o in inverted:
        print(f"  {o.order_id} ({o.product_name}): 시작가능일={o.earliest_start_day} 마감일={o.deadline_day}")

    print(f"\n[1b] 첫 생산 가능일(원본)이 ramp로 당겨진 납기보다 늦음(filter_and_attach_rates가 제약을 지워서 [1]엔 안 보임): {len(ramp_hidden)}건")
    for o, raw_es in ramp_hidden:
        print(f"  {o.order_id} ({o.product_name}): 원본 첫 생산 가능일={raw_es}일차, ramp 반영 후 납기={o.deadline_day}일차")

    print(f"\n[2] 생산가능구간이 전부 휴무일: {len(all_closed)}건")
    for o in all_closed:
        print(f"  {o.order_id} ({o.product_name}): 구간={o.earliest_start_day or 1}~{o.deadline_day}")

    print(f"\n[3] 필요수량이 이론상 최대생산량 초과(라인 풀가동 기준): {len(shortfall)}건")
    for o, rate_sum, open_days, max_prod in shortfall:
        print(
            f"  {o.order_id} ({o.category}): 필요 {o.quantity:,} vs 최대 {max_prod:,} "
            f"(시간당capa={rate_sum}, 가용일수={open_days}, 구간={o.earliest_start_day or 1}~{o.deadline_day})"
        )

    print(f"\n[4] 라인타입별 마감일 누적 병목(그 라인타입 전용 주문만): {len(bottleneck)}건")
    for line_type, end, req_h, avail_h, o in bottleneck:
        print(
            f"  {line_type} @ {end}일차까지 누적: 필요 {req_h:,.1f}시간 vs 가용 {avail_h:,.1f}시간 "
            f"(마지막으로 걸린 주문: {o.order_id}, 마감={o.deadline_day})"
        )

    total_bad = len(inverted) + len(ramp_hidden) + len(all_closed) + len(shortfall) + len(bottleneck)
    print()
    if total_bad == 0:
        print("[결론] 문제 없음 - 이 원인들로는 infeasible이 나지 않습니다.")
    else:
        print(
            f"[결론] 총 {total_bad}건 발견 - plan_from_orders.py를 time-limit을 아무리 늘려도 "
            f"이 주문들 때문에 infeasible이 납니다."
        )


if __name__ == "__main__":
    main()
