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
  category가 {마스크, 튜브, 용기} 중 하나(또는 발주처+품명이 셀바이오
  예외에 해당) AND (deadline_day가 없음(ASAP)이거나 MAX_DEADLINE_DAY +
  LATE_RAMP_DAYS 이하).

납기 ramp(filter_and_attach_rates 참고): 납기가 너무 임박(1~5일)하면
수량의 20%만, 계획기간을 살짝 넘으면(31~35일) 80%를 30일차로 당겨서
반영한다 - 자세한 이유는 filter_and_attach_rates 문서 참고.
"""

from __future__ import annotations

import argparse
import io
import os
import sys
from datetime import date, timedelta

# Windows 콘솔(cp949 등) 기본 인코딩에서는 한글 출력이 깨지므로 UTF-8로 강제.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from data_pipeline.orders_from_excel import load_orders_from_excel
from scheduling.models import Line, Order, ScheduleConfig
from scheduling.report import plot_gantt, print_report, save_outputs
from scheduling.solver import build_and_solve

DEFAULT_EXCEL_PATH = r"C:\Users\ubina\Downloads\수주진행현황(simulation_DATA_1)1.xlsx"

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
    # 품명에 매수 표기(예: "10매")가 있고 단위가 "EA"인 마스크 주문 -
    # 데이터 로딩 단계(data_pipeline/orders_from_excel.py)에서 "마스크"
    # 대신 이 category로 분류된다. 로타리에서만 생산 가능 - 물리적으로는
    # 위 "마스크"의 로타리와 같은 설비다(build_lines()가 line_type_id
    # 기준으로 중복 제거하므로 물리 라인이 따로 늘어나지는 않음).
    "마스크_멀티시트": [
        {"line_type_id": "로타리", "count": 2, "workers": 7, "rate": 3000},
    ],
    "튜브": [
        {"line_type_id": "튜브라인", "count": 3, "workers": 10, "rate": 2100},
    ],
    # 발주처가 "셀바이오휴먼텍"이고 품명에 "사각패드"가 들어가는 제품
    # 전용 라인. 엑셀상 category(제품군)는 "용기"로 찍혀 있지만 실제로는
    # 이 전용 라인에서만 생산되므로, filter_and_attach_rates에서 category와
    # 무관하게 발주처+품명으로 먼저 걸러서 이 spec을 배정한다(아래
    # CELLBIO_VENDOR/CELLBIO_PRODUCT_KEYWORD 참고). 이 키 자체는
    # Order.category 값과 매칭시키기 위한 용도가 아니라, build_lines()가
    # 물리 라인을 만들 때 순회하는 대상일 뿐이다.
    "셀바이오_사각패드": [
        {"line_type_id": "셀바이오_라인", "count": 1, "workers": 12, "rate": 750},
    ],
}

# "셀바이오_사각패드" 예외 대상 판별 기준(발주처+품명 부분일치).
CELLBIO_VENDOR = "셀바이오휴먼텍"
CELLBIO_PRODUCT_KEYWORD = "사각패드"

MAX_DEADLINE_DAY = 30

# 납기 임박/과원거리 주문에 대한 수량-납기 완화("ramp") 구간. 그대로 두면
# 1~5일 남은 주문에 생산계획이 과하게 몰리므로, 그 몫의 80%는 "지난 계획
# 주기에 이미 생산해 뒀다"고 가정하고 20%만 이번 계획에 반영한다. 대칭으로
# 31~35일 남은 주문은 80%를 이번 계획의 마지막날(30일차)에 당겨서 미리
# 반영해 두면(20%는 이번엔 아예 빼고 다음 계획 주기의 "1~5일" 구간에서
# 마저 반영됨), 다음 계획을 짤 때도 초반 며칠에 과한 쏠림이 생기지 않는다.
EARLY_RAMP_DAYS = 5          # 납기 1~5일: 급함 완화 구간
EARLY_RAMP_FRACTION = 0.2    # 위 구간에 반영할 수량 비율(나머지 80%는 이미 생산했다고 가정)
LATE_RAMP_DAYS = 5           # 납기 31~35일: 당겨서 반영하는 구간
LATE_RAMP_FRACTION = 0.8     # 위 구간에 반영할 수량 비율(납기는 30일차로 당김)


def build_lines() -> list[Line]:
    """CATEGORY_LINE_SPECS에 정의된 라인 타입들을 Line 목록으로 만든다.

    대부분의 라인 타입은 카테고리 하나에만 등장하지만(셀라인/단발은
    용기 전용, 10열기는 마스크 전용, 튜브라인은 튜브 전용), "로타리"는
    "마스크"와 "마스크_멀티시트" 둘 다에 나온다 - 물리적으로 동일한
    설비가 주문에 따라 두 category 중 하나로 취급될 뿐이라서다. 그래서
    같은 line_type_id가 여러 category spec에 등장하면 Line은 딱 하나만
    만들고(Line 자체는 category를 갖지 않음 - 생산 가능 여부는 항상
    Order.rate로 판단하므로 어느 category에 등장했는지는 무관함,
    scheduling/models.py의 Line 참고), count가 서로 다르면 물리 대수가
    모호해지므로 에러를 낸다."""
    lines_by_type: dict[str, Line] = {}
    first_category_by_type: dict[str, str] = {}  # 에러 메시지에서 어느 category끼리 충돌했는지 보여주기 위한 용도
    for category, specs in CATEGORY_LINE_SPECS.items():
        for spec in specs:
            type_id = spec["line_type_id"]
            existing = lines_by_type.get(type_id)
            if existing is not None:
                if existing.count != spec["count"]:
                    raise ValueError(
                        f"라인 타입 {type_id!r}이 서로 다른 count로 중복 정의됨: "
                        f"{first_category_by_type[type_id]}={existing.count} vs {category}={spec['count']}"
                    )
                continue
            lines_by_type[type_id] = Line(line_type_id=type_id, count=spec["count"])
            first_category_by_type[type_id] = category
    return list(lines_by_type.values())


# 지금은 그냥 line_type_id 가 rate와 workers를 결정하도록 했으니까 모든 order에 대해서 category만 읽으면 자동으로 
# o.rate와 o.workers 딕셔너리가 채워짐. 나중에는 채우는 걸 따로 할 것임

def filter_and_attach_rates(orders: list[Order]) -> tuple[list[Order], dict[str, int]]:
    """category가 CATEGORY_LINE_SPECS에 없는 주문(마스크/튜브/용기/셀바이오
    예외가 아닌 벌크/파우치/샤쉐 등)과 납기가 계획기간을 너무 벗어난
    주문을 걸러내고, 남은 주문에는 그 제품군(또는 셀바이오 예외)의
    rate/workers를 채워 넣는다(orders_from_excel.py가 만드는 Order는
    rate/workers가 항상 빈 dict라서 여기서 처음 채워짐).

    셀바이오 예외: 발주처가 CELLBIO_VENDOR이고 품명에
    CELLBIO_PRODUCT_KEYWORD가 들어가면 category(엑셀상 "용기")와 무관하게
    "셀바이오_사각패드" spec(전용 라인 1대)을 배정한다.

    납기 ramp: 납기가 EARLY_RAMP_DAYS(1~5일차)면 수량의 EARLY_RAMP_FRACTION만
    반영하고(나머지는 지난 계획주기에 이미 생산했다고 가정), 납기가
    MAX_DEADLINE_DAY를 넘어 LATE_RAMP_DAYS 이내(31~35일차)면 수량의
    LATE_RAMP_FRACTION을 MAX_DEADLINE_DAY(30일차) 납기로 당겨서 반영한다.
    그보다 더 먼 납기는 이번 계획에서 제외한다. ramp로 수량이 반올림되어
    0 이하가 되면 그 주문은 제외한다(excluded_nonpositive_qty).

    earliest_start_day(부자재/원료 입고일로 정해지는 최초 생산가능일)가
    계획기간(MAX_DEADLINE_DAY)을 넘는 값이면 여기서 지워서(None) 제약을
    걸지 않는다 - orders_from_excel.py는 계획기간 길이를 모르므로 값을
    그대로(우리 horizon 밖일 수도 있는 채로) 넘겨주고, 실제 horizon
    길이를 아는 이 함수에서 최종 판단한다."""
    kept: list[Order] = []
    stats = {
        "total": len(orders),
        "excluded_category": 0,
        "excluded_deadline": 0,
        "excluded_nonpositive_qty": 0,
        "included": 0,
    }
    for o in orders:
        if o.vendor.strip() == CELLBIO_VENDOR and CELLBIO_PRODUCT_KEYWORD in o.product_name:
            specs = CATEGORY_LINE_SPECS["셀바이오_사각패드"]
        else:
            specs = CATEGORY_LINE_SPECS.get(o.category)
        if specs is None:
            stats["excluded_category"] += 1
            continue

        if o.deadline_day is not None:
            if o.deadline_day > MAX_DEADLINE_DAY + LATE_RAMP_DAYS:
                stats["excluded_deadline"] += 1
                continue
            if o.deadline_day <= EARLY_RAMP_DAYS:
                o.quantity = round(o.quantity * EARLY_RAMP_FRACTION)
            elif o.deadline_day > MAX_DEADLINE_DAY:
                o.quantity = round(o.quantity * LATE_RAMP_FRACTION)
                o.deadline_day = MAX_DEADLINE_DAY
            if o.quantity <= 0:
                stats["excluded_nonpositive_qty"] += 1
                continue

        if o.earliest_start_day is not None and o.earliest_start_day > MAX_DEADLINE_DAY:
            o.earliest_start_day = None
        o.rate = {s["line_type_id"]: s["rate"] for s in specs}
        o.workers = {s["line_type_id"]: s["workers"] for s in specs}
        kept.append(o)
    stats["included"] = len(kept)
    return kept, stats


def resolve_closed_days(
    reference_date: date,
    horizon_days: int,
    closed_dates: list[date],
    close_weekends: bool = True,
) -> frozenset[int]:
    """휴무일 실제 날짜 목록(closed_dates)과 주말 자동휴무 여부를 종합해서,
    reference_date를 1일차로 하는 horizon_days 범위 안의 day index 집합으로
    변환한다(ScheduleConfig.closed_days, deadline_day와 동일하게 1-based).
    horizon 밖 휴무일은 조용히 무시하고 경고만 출력한다."""
    closed_date_set = set(closed_dates)
    horizon_dates = {reference_date + timedelta(days=offset) for offset in range(horizon_days)}

    result = set()
    for offset in range(horizon_days):
        d = reference_date + timedelta(days=offset)
        is_weekend = close_weekends and d.weekday() >= 5  # 5=토, 6=일
        if is_weekend or d in closed_date_set:
            result.add(offset + 1)

    out_of_range = sorted(closed_date_set - horizon_dates)
    if out_of_range:
        print(f"[경고] 계획기간({reference_date} ~ {reference_date + timedelta(days=horizon_days - 1)}) 밖 휴무일 무시: {out_of_range}")
    return frozenset(result)


def main():
    parser = argparse.ArgumentParser(description="실제 수주 데이터 기반 생산 스케줄링 (CP-SAT)")
    parser.add_argument("--excel-path", default=DEFAULT_EXCEL_PATH, help="수주진행현황 엑셀 경로")
    parser.add_argument("--reference-date", default=None, help="기준일(YYYY-MM-DD, 생략하면 오늘)")
    parser.add_argument("--horizon-days", type=int, default=MAX_DEADLINE_DAY)
    parser.add_argument("--daily-wage", type=float, default=120_000, help="1인 1일 고용 정액임금")
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
    parser.add_argument(
        "--closed-date", action="append", default=[],
        help="생산 불가일(YYYY-MM-DD), 여러 번 지정 가능(공휴일 등)",
    )
    parser.add_argument(
        "--no-weekend-closure", action="store_true",
        help="주말도 근무일로 취급(기본은 주말 자동 휴무)",
    )
    args = parser.parse_args()

    reference_date = date.fromisoformat(args.reference_date) if args.reference_date else date.today()
    raw_orders = load_orders_from_excel(args.excel_path, reference_date=reference_date)

    orders, stats = filter_and_attach_rates(raw_orders)
    print(
        f"[정보] 엑셀 기반 주문 {stats['total']}건 -> "
        f"제품군(마스크/튜브/용기/셀바이오 아님) 제외 {stats['excluded_category']}건, "
        f"납기 {MAX_DEADLINE_DAY + LATE_RAMP_DAYS}일 초과 제외 {stats['excluded_deadline']}건, "
        f"ramp 반영 후 잔량0이하 제외 {stats['excluded_nonpositive_qty']}건 "
        f"-> 최종 {stats['included']}건"
    )
    if not orders:
        print("[정보] 스케줄링할 주문이 없습니다. 종료.")
        return

    lines = build_lines()
    print(f"[정보] 라인 {len(lines)}종 (물리 라인 총 {sum(l.count for l in lines)}대)")

    closed_dates = [date.fromisoformat(s) for s in args.closed_date]
    closed_days = resolve_closed_days(
        reference_date, args.horizon_days, closed_dates,
        close_weekends=not args.no_weekend_closure,
    )

    config = ScheduleConfig(
        horizon_days=args.horizon_days,
        daily_wage=args.daily_wage,
        hourly_wage=args.hourly_wage,
        overtime_multiplier=args.overtime_multiplier,
        time_limit_seconds=args.time_limit,
        secondary_time_limit_seconds=args.secondary_time_limit,
        optimize_continuity=not args.no_continuity,
        default_backlog_cost_per_unit_per_day=args.backlog_cost,
        closed_days=closed_days,
    )
    print(
        f"[정보] 임금 설정: 일급 {config.daily_wage:,.0f} / 시급(잔업기준) "
        f"{config.resolved_hourly_wage():,.0f} / 잔업배수 {config.overtime_multiplier}x"
    )
    print(f"[정보] 휴무일(day index): {sorted(config.closed_days)}")

    result = build_and_solve(lines, orders, config)
    print_report(result, orders)

    os.makedirs(args.output_dir, exist_ok=True)
    save_outputs(result, orders, args.output_dir)
    if not args.no_plot:
        plot_gantt(result, orders, config, args.output_dir)


if __name__ == "__main__":
    main()
