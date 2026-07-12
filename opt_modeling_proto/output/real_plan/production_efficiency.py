# -*- coding: utf-8 -*-
"""
output/real_plan/production_efficiency.py

real_plan/line_schedule.csv + daily_workforce.csv에서 계획기간(기본 30일)
전체를 놓고 다음 지표를 계산한다:

    {(용기생산량 + 튜브생산량) * WEIGHT_CONTAINER_TUBE + 마스크생산량} / 총 작업인원수

  - 용기생산량/튜브생산량/마스크생산량: 그 category의 주문(_data_source.
    build_order_category_lookup으로 판단 - product_id가 아니라 order_id
    기준, "코드확인중" 같은 placeholder product_id의 category 혼동을
    피하기 위함. line_id 기준이 아닌 이유: "로타리"처럼 물리 라인 하나가
    "마스크"/"마스크_멀티시트" 여러 category를 겸할 수 있어서 라인
    기준으로는 더 이상 category가 명확하지 않음)에서 계획기간 전체
    동안 실제로 생산(produce)된 수량의 합. 슬롯 하나 = 1시간이므로
    슬롯당 rate(그 라인/주문의 시간당 생산량, _data_source.build_rate_lookup)
    그대로가 그 슬롯의 생산량.
  - 총 작업인원수: daily_workforce.csv의 workforce(그날 고용 인원수)를
    전체 날짜에 대해 더한 값(총 인원-일). 잔업(overtime)은 이미 고용된
    같은 인원이 추가로 일하는 시간이지 "추가 인원"이 아니므로 여기
    분모에는 더하지 않는다.

이 스크립트는 plan_from_orders.py의 실제 수주 데이터 전용이라(용기/튜브/
마스크라는 카테고리 이름 자체가 그 스크립트의 CATEGORY_LINE_SPECS에서
나옴), --real-plan 같은 소스 선택 플래그 없이 항상 엑셀 기반 real_plan
방식으로 동작한다. real_plan/ 폴더 안에 있으므로 기본적으로 이 폴더의
CSV를 그대로 읽는다.

사용법 (이 파일이 있는 output/real_plan/ 디렉터리에서):
    python production_efficiency.py
    python production_efficiency.py --reference-date 2026-07-09   # plan_from_orders.py를 돌릴 때 쓴 기준일과 맞춰야 함
"""

from __future__ import annotations

import argparse
import io
import os
import sys
from collections import defaultdict
from datetime import date

import pandas as pd

# Windows 콘솔(cp949 등) 기본 인코딩에서는 한글 출력이 깨지므로 UTF-8로 강제.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)

# 이 파일은 output/real_plan/ 안에 있으므로, opt_modeling_proto/(scheduling
# 패키지 + plan_from_orders.py)와 output/(_data_source.py) 둘 다 import
# 경로에 추가해야 한다.
_HERE = os.path.dirname(os.path.abspath(__file__))          # .../output/real_plan
_OUTPUT_DIR = os.path.dirname(_HERE)                          # .../output
_OPT_MODELING_DIR = os.path.dirname(_OUTPUT_DIR)              # .../opt_modeling_proto
sys.path.insert(0, _OUTPUT_DIR)
sys.path.insert(0, _OPT_MODELING_DIR)

import plan_from_orders as pfo
from _data_source import build_order_category_lookup, build_rate_lookup

WEIGHT_CONTAINER_TUBE = 5  # (용기+튜브) 생산량에 곱하는 가중치


def compute_efficiency(
    schedule: pd.DataFrame,
    daily_workforce: pd.DataFrame,
    rate_lookup: dict[tuple[str, str], float],
    order_category: dict[str, str],
    weight_container_tube: float = WEIGHT_CONTAINER_TUBE,
) -> dict:
    """전체 계획기간에 대해 카테고리별 총생산량 + 총 작업인원수 + 최종
    지표값을 딕셔너리로 반환한다."""
    produce = schedule[schedule["activity"] == "produce"].copy()

    missing_rate: set[tuple[str, str]] = set()
    missing_category: set[str] = set()

    def _rate(row) -> float:
        key = (row["line_id"], row["order_id"])
        val = rate_lookup.get(key)
        if val is None:
            missing_rate.add(key)
            return 0.0
        return val

    def _category(oid: str) -> str:
        cat = order_category.get(oid)
        if cat is None:
            missing_category.add(oid)
            return "(미상)"
        return cat

    produce["quantity"] = produce.apply(_rate, axis=1)  # 슬롯 1개 = 1시간 -> rate 그대로가 생산량
    produce["category"] = produce["order_id"].map(_category)

    if missing_rate:
        print(
            f"[경고] rate 정보가 없는 (line_id, order_id) 조합 {len(missing_rate)}건은 "
            f"0으로 취급합니다: {sorted(missing_rate)}",
            file=sys.stderr,
        )
    if missing_category:
        print(
            f"[경고] category 정보가 없는 order_id {len(missing_category)}건은 "
            f"'(미상)'으로 취급합니다: {sorted(missing_category)}",
            file=sys.stderr,
        )

    qty_by_category: dict[str, float] = defaultdict(float)
    for cat, total in produce.groupby("category")["quantity"].sum().items():
        qty_by_category[cat] = total

    container_qty = qty_by_category.get("용기", 0.0)
    tube_qty = qty_by_category.get("튜브", 0.0)
    # "마스크_멀티시트"(품명에 매수 표기가 있고 단위가 EA인 마스크 주문 -
    # data_pipeline/orders_from_excel.py 참고)도 물리적으로는 마스크
    # 생산이므로 마스크생산량에 합산한다.
    mask_sheet_qty = qty_by_category.get("마스크_멀티시트", 0.0)
    mask_qty = qty_by_category.get("마스크", 0.0) + mask_sheet_qty

    total_workforce = daily_workforce["workforce"].sum()

    numerator = (container_qty + tube_qty) * weight_container_tube + mask_qty
    efficiency = numerator / total_workforce if total_workforce else float("nan")

    return {
        "container_qty": container_qty,
        "tube_qty": tube_qty,
        "mask_qty": mask_qty,
        "mask_sheet_qty": mask_sheet_qty,  # 참고용: mask_qty 중 "마스크_멀티시트"에서 온 몫
        "other_qty": sum(v for k, v in qty_by_category.items() if k not in ("용기", "튜브", "마스크", "마스크_멀티시트")),
        "total_workforce": total_workforce,
        "numerator": numerator,
        "efficiency": efficiency,
    }


def main():
    parser = argparse.ArgumentParser(
        description="real_plan 계획기간 전체의 {(용기+튜브)생산량*WEIGHT_CONTAINER_TUBE + 마스크생산량} / 총작업인원수 계산"
    )
    parser.add_argument("--dir", default=_HERE, help="line_schedule.csv / daily_workforce.csv가 있는 디렉터리")
    parser.add_argument("--reference-date", default=None,
                         help="plan_from_orders.py를 돌릴 때 쓴 기준일(YYYY-MM-DD, 생략하면 오늘) - "
                              "rate/workers 값을 다시 만드는 데만 쓰이고 CP-SAT을 다시 돌리지는 않음")
    parser.add_argument("--excel-path", default=None, help="수주진행현황 엑셀 경로(생략하면 기본 경로)")
    parser.add_argument(
        "--weight-container-tube", type=float, default=WEIGHT_CONTAINER_TUBE,
        help=f"(용기+튜브) 생산량에 곱하는 가중치 (기본 {WEIGHT_CONTAINER_TUBE})",
    )
    args = parser.parse_args()

    ref = date.fromisoformat(args.reference_date) if args.reference_date else None
    excel_path = args.excel_path or pfo.DEFAULT_EXCEL_PATH
    raw_orders = pfo.load_orders_from_excel(excel_path, reference_date=ref, verbose=False)
    orders, _stats = pfo.filter_and_attach_rates(raw_orders)
    lines = pfo.build_lines()

    rate_lookup = build_rate_lookup(lines, orders)
    order_category = build_order_category_lookup(orders)

    schedule = pd.read_csv(os.path.join(args.dir, "line_schedule.csv"))
    daily_workforce = pd.read_csv(os.path.join(args.dir, "daily_workforce.csv"))

    r = compute_efficiency(schedule, daily_workforce, rate_lookup, order_category,
                            weight_container_tube=args.weight_container_tube)

    print(f"용기 생산량: {r['container_qty']:,.0f}개")
    print(f"튜브 생산량: {r['tube_qty']:,.0f}개")
    print(f"마스크 생산량: {r['mask_qty']:,.0f}개 (그 중 마스크_멀티시트: {r['mask_sheet_qty']:,.0f}개)")
    if r["other_qty"]:
        print(f"(기타 제품군 생산량: {r['other_qty']:,.0f}개 - 지표 계산에는 포함 안 됨)")
    print(f"총 작업인원수(인원-일): {r['total_workforce']:,.0f}명")
    print(f"분자 {{(용기+튜브)*{args.weight_container_tube}+마스크}}: {r['numerator']:,.1f}")
    print(f"\n지표값: {r['efficiency']:.4f}")


if __name__ == "__main__":
    main()
