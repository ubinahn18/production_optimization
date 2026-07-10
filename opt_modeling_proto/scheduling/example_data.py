# -*- coding: utf-8 -*-
"""
scheduling/example_data.py

내장 예시 인스턴스(build_example_instance)와, 실제 데이터를 --data JSON
파일로 넘길 때 쓰는 로더(load_data_from_json).
"""

from __future__ import annotations

import json

from .models import Line, Order


def build_example_instance() -> tuple[list[Line], list[Order]]:
    """마스크/용기/튜브 3개 제품군에 걸친 작은 예시 인스턴스.
    line_mask_3와 line_container_1은 물리 설비가 2대씩(count=2)이라
    '동일 라인타입의 여러 대 = 독립 라인' 요구사항을 보여준다. 동일
    라인타입 내 물리 라인들은 rate/workers가 항상 같으므로, 주문의
    rate/workers 딕셔너리에도 line_type_id당 값을 한 번만 적으면
    된다 - count대만큼 중복 입력할 필요가 없다(scheduling/models.py의
    Line.count, scheduling/pooling.py의 build_line_pools 참고).
    실제 데이터로 돌리려면 --data로 이런 구조의 JSON을 넣으면 된다
    (load_data_from_json 참고).
    """
    lines = [
        Line(line_type_id="line_mask_1", category="mask", count=1),       # 로타리형: 속도 빠름, 인원 多
        Line(line_type_id="line_mask_2", category="mask", count=1),       # 단발형: 속도 느림, 인원 少
        Line(line_type_id="line_mask_3", category="mask", count=2),       # 셀라인형: 동일 설비 2대
        Line(line_type_id="line_container_1", category="container", count=2),
        Line(line_type_id="line_tube_1", category="tube", count=1),
    ]

    orders = [
        Order(
            order_id="O1", product_id="MASK_A", category="mask",
            quantity=50_000, deadline_day=10,
            rate={"line_mask_1": 800, "line_mask_3": 600},
            workers={"line_mask_1": 6, "line_mask_3": 4},
        ),
        Order(
            order_id="O2", product_id="MASK_B", category="mask",
            quantity=30_000, deadline_day=15,
            rate={"line_mask_2": 500},
            workers={"line_mask_2": 3},
        ),
        Order(
            order_id="O3", product_id="MASK_C", category="mask",
            quantity=80_000, deadline_day=25,
            rate={"line_mask_1": 800, "line_mask_3": 600},
            workers={"line_mask_1": 6, "line_mask_3": 4},
        ),
        Order(
            order_id="O4", product_id="CONTAINER_A", category="container",
            quantity=40_000, deadline_day=12,
            rate={"line_container_1": 700},
            workers={"line_container_1": 5},
        ),
        Order(
            order_id="O5", product_id="CONTAINER_B", category="container",
            quantity=20_000, deadline_day=20,
            rate={"line_container_1": 700},
            workers={"line_container_1": 5},
        ),
        Order(
            order_id="O6", product_id="TUBE_A", category="tube",
            quantity=15_000, deadline_day=8,
            rate={"line_tube_1": 400},
            workers={"line_tube_1": 4},
        ),
        # ASAP 주문 예시: deadline_day를 아예 안 주면(None) 하드 마감 없이
        # backlog 비용으로만 다뤄진다 (scheduling/solver.py 참고).
        Order(
            order_id="O7", product_id="MASK_D", category="mask",
            quantity=60_000, deadline_day=None,
            rate={"line_mask_1": 800, "line_mask_3": 600},
            workers={"line_mask_1": 6, "line_mask_3": 4},
        ),
    ]
    return lines, orders


def load_data_from_json(path: str) -> tuple[list[Line], list[Order]]:
    """--data로 넘긴 JSON 파일을 읽어 Line/Order 목록으로 변환한다.

    JSON 형식:
      {
        "lines": [{"line_type_id": "...", "category": "mask", "count": 2}, ...],
        "orders": [
          {"order_id": "...", "product_id": "...", "category": "mask",
           "quantity": 10000, "deadline_day": 12,
           "rate": {"line_type_id": 시간당수량, ...},
           "workers": {"line_type_id": 필요인원, ...}},
          ...
        ]
      }

    "count"는 생략하면 1(라인 1대)로 취급된다. 동일 물리 라인이 여러 대면
    rate/workers는 그 line_type_id에 대해 한 번만 적으면 되고(count대 전부에
    동일하게 적용됨), count대만큼 값을 중복 입력할 필요는 없다 - 물리
    라인별 라벨은 solver가 내부적으로 생성한다(scheduling/pooling.py의
    build_line_pools 참고).

    "deadline_day"를 생략하거나 null로 주면 ASAP 주문(마감 없음)으로
    처리된다 - 하드 마감 대신 하루당 backlog 비용으로 다뤄진다
    (scheduling/models.py의 Order, scheduling/solver.py의 backlog 섹션
    참고). 필요하면 주문마다 "backlog_cost_per_unit_per_day"를 같이 줘서
    전역 기본값(--backlog-cost)을 개별적으로 덮어쓸 수 있다.
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    lines = [Line(**l) for l in data["lines"]]
    orders = [Order(**o) for o in data["orders"]]
    return lines, orders
