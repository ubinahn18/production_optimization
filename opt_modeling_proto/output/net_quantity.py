# -*- coding: utf-8 -*-
"""
output/net_quantity.py

line_schedule.csv에서 특정 하루만 뽑아서(plot_day.py처럼 --day로 날짜를
고른다) 그 날 제품군(category)별 실제 생산량을 계산한다.

생산량 계산: 그 날의 produce 슬롯마다 "그 라인에서 그 주문 생산시
시간당 생산량"(rate, _data_source.build_rate_lookup으로 Line/Order
데이터에서 order_id 기준으로 가져옴)을 더한다 - 슬롯 하나 = 1시간이므로
슬롯당 rate 값 그대로가 그 슬롯의 생산량이다. 그 다음 그 슬롯을 만든
라인의 category(_data_source.build_line_category_lookup)로 묶어서
제품군별 합계를 낸다.

주의: 여기서 쓰는 식별자는 전부 order_id다(product_id가 아님).
product_id는 진짜 식별자가 아니라 제품 "이름"이라, 여러 주문이 같은
값을 공유할 수 있다(예: 품번이 아직 안 나온 "코드확인중" 같은
placeholder를 서로 다른 실제 제품 여러 개가 같이 씀) - product_id로
rate를 찾거나 결과를 묶으면 서로 다른 주문이 하나로 뭉개진다(실제로
이 때문에 잘못된 결과가 나온 적 있음). product_id는 결과 테이블에
사람이 읽기 편하라고 참고용으로만 같이 보여준다. category도 같은
이유로 product_id가 아니라 line_id로 판단한다(라인은 category 전용이라
항상 명확함).

카테고리 이름은 하드코딩하지 않고 실제 라인 데이터에 있는 값을 그대로
쓴다 - --real-plan(엑셀 기반)에서는 "마스크"/"용기"/"튜브"가 나오고,
내장 예시 데이터에서는 "mask"/"container"/"tube"가 나온다.

사용법:
    python net_quantity.py --day 5
    python net_quantity.py --day 5 --real-plan --reference-date 2026-07-09
"""

from __future__ import annotations

import argparse
import io
import os
import sys

import pandas as pd

# Windows 콘솔(cp949 등) 기본 인코딩에서는 한글 출력이 깨지므로 UTF-8로 강제.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# opt_modeling_proto/ (이 파일의 상위 폴더)를 import 경로에 추가해서
# scheduling 패키지 및 plan_from_orders.py를 가져올 수 있게 한다.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _data_source import add_source_args, build_line_category_lookup, build_rate_lookup, resolve_source


def compute_net_quantity(
    schedule: pd.DataFrame,
    day: int,
    rate_lookup: dict[tuple[str, str], float],
    line_category: dict[str, str],
) -> pd.DataFrame:
    """day 하루치 produce 슬롯들을 rate_lookup으로 수량 환산해서
    (category, order_id)별 합계 DataFrame(product_id는 참고용 컬럼으로
    같이 붙음, quantity 컬럼 포함)을 반환한다. category는 그 슬롯을 만든
    line_id 기준으로 정한다(위 모듈 docstring 참고). rate/category 정보가
    없는 조합은 각각 0/"(미상)"으로 취급하고 경고를 남긴다(조용히
    빠뜨리지 않음)."""
    day_df = schedule[(schedule["day"] == day) & (schedule["activity"] == "produce")].copy()
    if day_df.empty:
        raise ValueError(f"day={day}에 생산(produce) 기록이 없습니다.")

    missing_rate: set[tuple[str, str]] = set()
    missing_category: set[str] = set()

    def _rate(row) -> float:
        key = (row["line_id"], row["order_id"])
        val = rate_lookup.get(key)
        if val is None:
            missing_rate.add(key)
            return 0.0
        return val

    def _category(lid: str) -> str:
        cat = line_category.get(lid)
        if cat is None:
            missing_category.add(lid)
            return "(미상)"
        return cat

    # 슬롯 하나 = 1시간이므로, 그 슬롯의 생산량은 시간당 생산량(rate) 그대로.
    day_df["quantity"] = day_df.apply(_rate, axis=1)
    day_df["category"] = day_df["line_id"].map(_category)

    if missing_rate:
        print(
            f"[경고] rate 정보가 없는 (line_id, order_id) 조합 {len(missing_rate)}건은 "
            f"0으로 취급합니다: {sorted(missing_rate)}",
            file=sys.stderr,
        )
    if missing_category:
        print(
            f"[경고] category 정보가 없는 line_id {len(missing_category)}건은 "
            f"'(미상)'으로 취급합니다: {sorted(missing_category)}",
            file=sys.stderr,
        )

    # order_id가 진짜 그룹 키. product_id는 order_id마다 값이 하나로
    # 고정돼 있으므로(주문 하나 = 제품 하나) 같이 묶어도 그룹 자체는
    # 안 바뀌고, 결과 테이블에 참고용 제품명으로 남는다.
    by_order = day_df.groupby(["category", "order_id", "product_id"], as_index=False)["quantity"].sum()
    return by_order.sort_values(["category", "order_id"]).reset_index(drop=True)


def main():
    parser = argparse.ArgumentParser(description="특정 날짜의 제품군(category)별 생산량 계산")
    parser.add_argument("--dir", default=None, help="line_schedule.csv가 있는 디렉터리")
    parser.add_argument("--day", type=int, required=True, help="계산할 날짜(1-indexed)")
    parser.add_argument("--out", default=None, help="결과 CSV 저장 경로 (기본: <dir>/net_quantity_day<N>.csv)")
    add_source_args(parser)
    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    lines, orders, default_dir = resolve_source(args, script_dir)
    args.dir = args.dir or default_dir

    rate_lookup = build_rate_lookup(lines, orders)
    line_category = build_line_category_lookup(lines, orders)

    schedule_path = os.path.join(args.dir, "line_schedule.csv")
    schedule = pd.read_csv(schedule_path)

    by_order = compute_net_quantity(schedule, args.day, rate_lookup, line_category)
    by_category = (
        by_order.groupby("category", as_index=False)["quantity"]
        .sum()
        .sort_values("quantity", ascending=False)
    )

    print(f"[정보] {args.day}일차 제품군별 생산량:")
    for _, row in by_category.iterrows():
        print(f"  {row['category']}: {row['quantity']:,.0f}개")

    print("\n[정보] 주문별 상세:")
    print(by_order.to_string(index=False))

    out_path = args.out or os.path.join(args.dir, f"net_quantity_day{args.day}.csv")
    by_order.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"\n[저장 완료] {out_path}")


if __name__ == "__main__":
    main()
