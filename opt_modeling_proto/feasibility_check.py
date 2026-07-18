# -*- coding: utf-8 -*-
"""
feasibility_check.py

plan_from_orders.py를 실제로 돌리기 전에, 물리적으로 처음부터 불가능한
주문이 있는지 미리 진단한다. 순수 진단 스크립트라 실제로 뭔가를
바꾸거나 제외하지는 않는다 - 체크 로직 자체(order_feasibility.py의
[1]/[2]/[3]/[4])와 그 체크에 걸린 주문을 plan_from_orders.py가 실제로
어떻게 처리하는지(제외 vs 프로그램 종료)를 정확히 같은 규칙으로 다시
적용해서, "지금 돌리면 무슨 일이 일어날지"를 미리 보여준다:

  - 원래(ramp 반영 전) 납기가 계획기간을 넘었던 주문(31~35일차, 다음
    계획주기로 미룰 수 있음)이 [1]/[2]/[3] 중 하나라도 걸리면: "제외
    예정"으로 표시 - plan_from_orders.py를 돌리면 조용히 제외되고
    다음 계획주기로 미뤄진다.
  - 그 외(원래 납기가 이번 계획기간 안 - 이미 확정된 - 이거나 ASAP)인
    주문이 걸리면: "프로그램 종료 예정"으로 표시 - plan_from_orders.py를
    돌리면 CP-SAT을 돌리지도 않고 바로 종료된다(다음 계획주기로 미룰
    수 없는 확정 납기인데 물리적으로 불가능하다는 뜻이라, 원료/부자재
    입고일이나 납기 데이터 자체를 고쳐야 함).

[4](라인타입별 마감일 누적 병목)는 여러 주문이 얽힌 조합 문제라 "이
주문 하나가 걸렸다"로 표현할 수 없다 - 위 분류에는 안 들어가고 참고
정보로만 보여준다(plan_from_orders.py도 이건 실제로 안 본다 - CP-SAT이
직접 풀어야 알 수 있는 영역).

이 스크립트가 아무것도 못 찾았다고 해서 plan_from_orders.py가 반드시
FEASIBLE을 찾는다는 보장은 없다(여러 라인타입에 걸친 주문들끼리의
조합 경쟁 문제 등은 CP-SAT이 직접 풀어야 알 수 있다) - 다만 뭔가
찾았다면, 그 주문 때문에 time-limit을 아무리 늘려도 절대 FEASIBLE이
나올 수 없다는 것만은 확실하다.

사용법 (plan_from_orders.py와 거의 동일한 인자):
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
from order_feasibility import check_type_bottleneck, describe_failures


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
    # filter_and_attach_rates()가 deadline_day를 ramp로 덮어쓰기 전에
    # 원본을 스냅샷해둔다 - plan_from_orders.py의 enforce_feasibility()와
    # 똑같이 "이 주문이 원래 이번 계획기간 안이었는지(제외 예정 vs
    # 프로그램 종료 예정)"를 판단할 때 쓴다.
    raw_deadline_by_id = {o.order_id: o.deadline_day for o in raw_orders}

    # filter_and_attach_rates() 자체가 "최초 생산가능일이 계획기간을
    # 넘는" 주문을 걸러내면서 order_id별 이유를 이미 출력한다
    # (plan_from_orders.py와 완전히 같은 함수라서 여기서 따로 검사할
    # 필요가 없다 - 그 주문들은 자연히 아래 orders 목록에서 빠진다).
    orders, _filter_stats = pfo.filter_and_attach_rates(raw_orders, read_specs_from_excel=read_specs_from_excel)
    print(f"[정보] rate/투입인원 출처: {args.read_specs}")
    print(f"[정보] 검사 대상 주문 {len(orders)}건 (기준일 {reference_date}, 계획기간 {args.horizon_days}일)")

    lines = pfo.build_lines()
    count_by_type = {l.line_type_id: l.count for l in lines}
    closed_dates = [date.fromisoformat(s) for s in args.closed_date]
    closed_days = pfo.resolve_closed_days(
        reference_date, args.horizon_days, closed_dates, close_weekends=not args.no_weekend_closure,
    )

    failures_by_id = describe_failures(orders, count_by_type, closed_days, args.horizon_days)
    deferred: list[tuple] = []  # 원래 late-ramp 대상 - plan_from_orders.py가 조용히 제외할 예정
    blocking: list[tuple] = []  # 이번 계획기간 확정 - plan_from_orders.py가 종료될 예정
    for o in orders:
        failures = failures_by_id.get(o.order_id)
        if not failures:
            continue
        raw_deadline = raw_deadline_by_id.get(o.order_id)
        is_late_ramped = raw_deadline is not None and raw_deadline > args.horizon_days
        (deferred if is_late_ramped else blocking).append((o, failures, raw_deadline))

    print(
        f"\n[제외 예정] 원래 납기가 계획기간을 넘어(ramp 대상) [1]/[2]/[3] 중 하나라도 걸리는 주문: {len(deferred)}건"
    )
    if deferred:
        print("  -> plan_from_orders.py를 돌리면 이 주문들은 조용히 제외되고 다음 계획주기로 미뤄집니다.")
    for o, failures, raw_deadline in deferred:
        print(f"  {o.order_id} ({o.product_name}): {'; '.join(failures)} (원래 납기 {raw_deadline}일차)")

    print(
        f"\n[프로그램 종료 예정] 이번 계획기간 확정 납기(또는 ASAP)인데 [1]/[2]/[3] 중 하나라도 걸리는 주문: {len(blocking)}건"
    )
    if blocking:
        print("  -> plan_from_orders.py를 돌리면 CP-SAT을 돌리지도 않고 바로 종료됩니다 - 원료/부자재 입고일이나 납기를 먼저 확인하세요.")
    for o, failures, _raw_deadline in blocking:
        print(f"  {o.order_id} ({o.product_name}): {'; '.join(failures)}")

    bottleneck = check_type_bottleneck(orders, count_by_type, closed_days, args.horizon_days)
    print(
        f"\n[참고] 라인타입별 마감일 누적 병목(여러 주문의 조합 문제라 개별 제외 대상 아님, "
        f"그 라인타입 전용 주문만 집계): {len(bottleneck)}건"
    )
    for line_type, end, req_h, avail_h, o in bottleneck:
        print(
            f"  {line_type} @ {end}일차까지 누적: 필요 {req_h:,.1f}시간 vs 가용 {avail_h:,.1f}시간 "
            f"(마지막으로 걸린 주문: {o.order_id}, 마감={o.deadline_day})"
        )

    print()
    if not blocking:
        extra = f"({len(deferred)}건은 조용히 제외될 예정)" if deferred else ""
        print(f"[결론] plan_from_orders.py가 이 진단으로는 즉시 종료되지 않습니다. {extra}")
    else:
        print(f"[결론] {len(blocking)}건이 plan_from_orders.py 실행을 막습니다 - 위 목록을 먼저 해결하세요.")


if __name__ == "__main__":
    main()
