# -*- coding: utf-8 -*-
"""
order_feasibility.py

"물리적으로 처음부터 불가능한" 주문을 CP-SAT 없이 즉시(밀리초 단위로)
판별하는 체크 함수들. feasibility_check.py(진단 전용 - 이 체크들에
걸리는 주문을 보여주기만 하고 아무것도 안 바꿈)와 plan_from_orders.py
(실제로 이 체크에 걸리는 주문을 계획에서 빼거나, 뺄 수 없는 경우
프로그램을 바로 종료하는 데 씀) 둘 다 여기서 가져다 쓴다.

이 파일을 따로 뺀 이유: plan_from_orders.py가 feasibility_check.py를
직접 import하면 순환참조가 생긴다(feasibility_check.py가 이미
plan_from_orders를 import해서 엑셀을 읽으므로). 두 스크립트가 공통으로
쓰는 체크 로직만 이 파일로 분리하면 순환참조 없이 양쪽에서 쓸 수 있다.

체크 목록:
  [1] earliest_start_day > deadline_day
      (원료/부자재가 도착하기도 전에 납기가 지나가버림 - 생산 가능한
      날이 아예 없음)
  [2] [earliest_start_day, deadline_day] 구간이 전부 휴무일(주말/공휴일)
      (구간 자체는 역전이 아니지만 그 안에 근무일이 하루도 없음)
  [3] 그 구간의 근무일마다 호환 라인 전부(대수까지 반영)를 잔업 포함
      풀가동해도 필요수량을 못 채움(순수 라인 capa 부족)
  [4] 라인타입별 마감일 누적 병목 - 여러 주문이 같은(호환 라인타입이
      하나뿐인) 라인을 두고 겹치는 기간에 경쟁하는 경우. [1]~[3]과
      달리 특정 "이 주문 하나"의 문제가 아니라 여러 주문의 조합
      문제라서, 개별 주문을 빼거나 종료하는 판단에는 안 쓴다(진단
      전용 - feasibility_check.py에서만 보여줌).

모두 필요조건이지 충분조건은 아니다 - 이 체크들이 전부 통과했다고
plan_from_orders.py가 반드시 FEASIBLE을 찾는다는 보장은 없다(여러
라인타입에 걸친 주문들의 조합 경쟁 문제 등은 CP-SAT이 직접 풀어야
알 수 있음). 하지만 하나라도 걸리면, 그 주문 때문에 time-limit을
아무리 늘려도 절대 FEASIBLE이 나올 수 없다는 것만은 확실하다.
"""

from __future__ import annotations

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


def check_type_bottleneck(
    orders: list[Order],
    count_by_type: dict[str, int],
    closed_days: frozenset[int],
    horizon_days: int,
) -> list[tuple[str, int, float, float, Order]]:
    """[4] 라인타입별 마감일 누적 병목. [3]은 주문 하나하나를 "그 주문
    혼자 그 라인을 다 쓴다면"으로 독립적으로 체크해서, 여러 주문이 같은
    라인타입을 두고 겹치는 기간에 서로 경쟁하는 경우를 못 잡는다. 이
    체크는 "그 라인타입에서만 생산 가능한(다른 라인타입과 호환 안
    되는) 주문들"을 마감일 이른 순으로 모아서, 누적 필요시간이 누적
    가용시간(그 라인타입 총 대수 x 잔업포함 슬롯수 x 1일차부터 그
    마감일까지의 근무일수)을 넘는 지점이 있는지 확인한다 - 넘으면 그
    주문들을 전부 그 라인에 배정해도 물리적으로 마감을 못 맞춘다는
    뜻이라 확실히 infeasible이다.

    한계(필요조건이지 충분조건은 아님): (1) 호환 라인타입이 2개 이상인
    주문은 여러 라인 중 어디로 갈지 확정할 수 없어서 아예 제외한다 -
    그런 주문들끼리의 경쟁까지는 못 본다. (2) earliest_start_day를
    무시하고 1일차부터 가용하다고 가정한다(실제 가용시간은 이보다
    적을 수 있으므로 이 체크는 오히려 관대한 쪽으로 치우쳐 있다 -
    그런데도 걸린다면 실제로는 확실히 infeasible)."""
    bad = []
    by_type: dict[str, list[Order]] = {}
    for o in orders:
        if o.is_asap():
            continue
        compat = o.compatible_line_types()
        if len(compat) != 1:
            continue  # 여러 라인타입에 걸친 주문은 이 체크에서 제외(위 설명 참고)
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


def describe_failures(
    orders: list[Order],
    count_by_type: dict[str, int],
    closed_days: frozenset[int],
    horizon_days: int,
) -> dict[str, list[str]]:
    """orders 각각에 대해 [1]/[2]/[3] 중 걸리는 게 있으면 그 사람이 읽을
    설명 문자열 리스트를 order_id 기준으로 돌려준다(안 걸리면 그
    order_id는 결과 dict에 아예 없음). [4]는 여러 주문의 조합 문제라
    "이 주문이 걸렸다"는 형태로 표현이 안 되므로 여기 포함 안 됨 -
    필요하면 check_type_bottleneck을 따로 호출해서 보여줘야 한다."""
    inverted_ids = {o.order_id for o in check_inverted_window(orders)}
    closed_ids = {o.order_id for o in check_all_closed_window(orders, closed_days)}
    shortfall_by_id = {
        o.order_id: (rate_sum, open_days, max_prod)
        for o, rate_sum, open_days, max_prod in check_capacity_shortfall(orders, count_by_type, closed_days, horizon_days)
    }

    result: dict[str, list[str]] = {}
    for o in orders:
        failures = []
        if o.order_id in inverted_ids:
            failures.append(f"[1] 시작가능일(day {o.earliest_start_day}) > 마감일(day {o.deadline_day})")
        if o.order_id in closed_ids:
            failures.append(f"[2] 생산가능구간(day {o.earliest_start_day or 1}~{o.deadline_day})이 전부 휴무일")
        if o.order_id in shortfall_by_id:
            rate_sum, open_days, max_prod = shortfall_by_id[o.order_id]
            failures.append(
                f"[3] 필요수량({o.quantity:,.0f}) > 이론상 최대생산량({max_prod:,.0f}, "
                f"시간당capa={rate_sum:,.0f}, 가용일수={open_days})"
            )
        if failures:
            result[o.order_id] = failures
    return result
