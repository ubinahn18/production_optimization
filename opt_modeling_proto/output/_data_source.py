# -*- coding: utf-8 -*-
"""
output/_data_source.py

output/ 아래 스크립트들(labor_utilization.py, plot_day.py 등)이 공통으로
쓰는 "lines/orders를 어디서 가져올지" 선택 로직. 세 가지 소스를 지원한다:
  --real-plan : plan_from_orders.py와 동일한 방식(엑셀 수주 + 제품군별
                라인 스펙)으로 다시 만듦 - output/real_plan/ 결과를 다룰 때.
  --data      : JSON 파일(scheduling.example_data.load_data_from_json)
  (기본)      : 내장 예시 데이터(scheduling.example_data.build_example_instance)
각 소스는 --dir을 안 줬을 때 쓸 기본 디렉터리도 같이 정해준다.

이 파일을 쓰는 스크립트는 자신이 sys.path에 opt_modeling_proto/를 먼저
추가해둔 뒤에 이 모듈을 import해야 한다(scheduling 패키지 의존).
"""

from __future__ import annotations

import os
from datetime import date

from scheduling.example_data import build_example_instance, load_data_from_json
from scheduling.models import Line, Order
from scheduling.pooling import build_line_pools


def add_source_args(parser) -> None:
    parser.add_argument("--data", default=None,
                         help="라인/주문 데이터 JSON 경로 (없으면 내장 예시 데이터 사용)")
    parser.add_argument(
        "--real-plan", action="store_true",
        help="plan_from_orders.py와 동일한 방식(엑셀 수주 + 제품군별 라인 스펙)으로 "
             "lines/orders를 다시 만든다 - output/real_plan/ 결과를 다룰 때 사용 "
             "(그 스크립트를 돌릴 때 쓴 --reference-date/--excel-path와 맞춰줘야 함).",
    )
    parser.add_argument("--reference-date", default=None, help="--real-plan용 기준일(YYYY-MM-DD, 생략하면 오늘)")
    parser.add_argument("--excel-path", default=None, help="--real-plan용 수주진행현황 엑셀 경로(생략하면 기본 경로)")


def resolve_source(args, script_dir: str) -> tuple[list[Line], list[Order], str]:
    """(lines, orders, default_dir)를 반환한다. default_dir은 args.dir이
    비어 있을 때 쓸 디렉터리(--real-plan이면 output/real_plan, 아니면
    script_dir 그대로)."""
    if args.real_plan:
        import plan_from_orders as pfo

        ref = date.fromisoformat(args.reference_date) if args.reference_date else None
        excel_path = args.excel_path or pfo.DEFAULT_EXCEL_PATH
        raw_orders, _load_stats = pfo.load_orders_from_excel(excel_path, reference_date=ref, verbose=False)
        orders, _stats = pfo.filter_and_attach_rates(raw_orders)
        lines = pfo.build_lines()
        default_dir = os.path.join(script_dir, "real_plan")
    elif args.data:
        lines, orders = load_data_from_json(args.data)
        default_dir = script_dir
    else:
        lines, orders = build_example_instance()
        default_dir = script_dir
    return lines, orders, default_dir


def _build_field_lookup(lines, orders, field: str) -> dict[tuple[str, str], float]:
    """LinePool의 rate 또는 workers 딕셔너리(이미 order_id로 키가 잡혀
    있음)를, line_schedule.csv에 실제로 찍히는 (물리 라인 라벨, order_id)
    키로 펼쳐서 돌려준다. 물리 라인 라벨은 count>1이면 "{line_type_id}_1"..
    "{line_type_id}_{count}"(scheduling/pooling.py의 build_line_pools 참고).

    product_id가 아니라 order_id로 키를 잡는 이유: product_id는 진짜
    식별자가 아니라 제품 "이름"이라 여러 주문이 같은 값을 공유할 수
    있다(예: 품번이 아직 안 나온 "코드확인중" 같은 placeholder를 서로
    다른 실제 제품 여러 개가 같이 씀). order_id만이 항상 고유하다.
    """
    lookup: dict[tuple[str, str], float] = {}
    for pool in build_line_pools(lines, orders):
        source = getattr(pool, field)
        for order_id, value in source.items():
            for line_id in pool.line_ids:
                lookup[(line_id, order_id)] = value
    return lookup


def build_workers_lookup(lines, orders) -> dict[tuple[str, str], int]:
    """{(line_id, order_id): 그 라인에서 이 주문 생산에 필요한 인원수}"""
    return _build_field_lookup(lines, orders, "workers")


def build_rate_lookup(lines, orders) -> dict[tuple[str, str], float]:
    """{(line_id, order_id): 그 라인에서 이 주문을 생산할 때 시간당 생산량}
    (슬롯 하나 = 1시간이므로, produce 슬롯 하나의 생산량은 이 값 그대로다.)"""
    return _build_field_lookup(lines, orders, "rate")


def build_order_category_lookup(orders) -> dict[str, str]:
    """{order_id: 그 주문의 category}.

    product_id 기준으로 category를 매기면 안 되는 이유: "코드확인중"/
    "코드생성전" 같은 플레이스홀더 product_id는 서로 다른 제품군의 여러
    실제 제품이 같은 문자열을 공유해서, product_id -> category 매핑
    자체가 애매하다(실제로 이 문제로 잘못된 결과가 나온 적 있음).

    예전엔 물리 line_id 기준으로 매겼었다(라인은 카테고리 전용이라
    명확하다는 가정) - 하지만 plan_from_orders.py의 CATEGORY_LINE_SPECS에
    "마스크_멀티시트"가 추가되면서 "로타리" 물리 라인 하나가 "마스크"와
    "마스크_멀티시트" 두 category의 주문을 둘 다 생산할 수 있게 됐다.
    즉 "라인은 카테고리 전용"이라는 전제 자체가 깨졌으므로(build_lines()가
    로타리에 표시용 category를 "마스크" 하나로 고정해버리면, 실제로는
    로타리에서 만든 "마스크_멀티시트" 생산량까지 전부 "마스크"로 잘못
    집계된다), line_id 기준 매핑은 더 이상 안전하지 않다. order_id는
    애초에 유일하고 category도 주문마다 명확히 정해져 있으므로(라인이
    몇 종류의 category를 겸하든 상관없이) 이 문제 자체가 생기지 않는다.
    """
    return {o.order_id: o.category for o in orders}
