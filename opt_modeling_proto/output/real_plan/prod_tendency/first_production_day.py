# -*- coding: utf-8 -*-
"""
output/real_plan/prod_tendency/first_production_day.py

각 주문(order_id)이 계획기간 중 처음 생산(produce)된 날짜("첫 생산
일자")를 line_schedule.csv에서 뽑아내고, 그 분포를 히스토그램(막대그래프:
x축=일차, y축=그 날 첫 생산을 시작한 주문 수)으로 그린다.

order_fulfillment.csv가 (기본적으로) 같은 위치에서 찾아지면(plan_from_orders.py가
항상 같이 만듦) 그걸로 "이번 계획에 포함된 전체 주문 목록"을 파악해서,
계획기간 내내 단 한 번도 생산되지 않은 주문(전량 미생산)을 콘솔에 따로
알려준다 - line_schedule.csv에는 애초에 그 주문의 행 자체가 안 남으므로
line_schedule.csv만 봐서는 이런 주문이 존재하는지 알 수 없다. 없으면 이
경고는 생략하고 히스토그램/CSV만 만든다.

이 스크립트는 real_plan/ 바로 밑이 아니라 real_plan/prod_tendency/에
있다 - 그래서 입력(line_schedule.csv/order_fulfillment.csv)은 기본적으로
한 단계 위(real_plan/)에서 읽고, 결과(CSV/PNG)는 이 스크립트가 있는
prod_tendency/ 폴더 자체에 저장한다(--data-dir/--out-dir로 둘 다
바꿀 수 있음).

사용법 (이 파일이 있는 output/real_plan/prod_tendency/ 디렉터리에서):
    python first_production_day.py
    python first_production_day.py --data-dir ../other_plan --out first_day.png
"""

from __future__ import annotations

import argparse
import io
import os
import sys

import pandas as pd

# Windows 콘솔(cp949 등) 기본 인코딩에서는 한글 출력이 깨지므로 UTF-8로 강제.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)

_HERE = os.path.dirname(os.path.abspath(__file__))  # .../real_plan/prod_tendency
_DATA_DIR = os.path.dirname(_HERE)                   # .../real_plan (line_schedule.csv 등이 있는 곳)


def compute_first_production_day(schedule: pd.DataFrame) -> pd.DataFrame:
    """schedule(line_schedule.csv를 그대로 읽은 DataFrame)에서 order_id별
    첫 생산일(day)을 뽑는다. product_id는 참고용으로 같이 붙인다
    (order_id, product_id, first_day 컬럼, order_id 기준 정렬). 생산
    기록이 아예 없으면 ValueError."""
    produce = schedule[schedule["activity"] == "produce"]
    if produce.empty:
        raise ValueError("line_schedule.csv에 생산(produce) 기록이 전혀 없습니다.")
    first = produce.groupby("order_id", as_index=False).agg(
        first_day=("day", "min"), product_id=("product_id", "first")
    )
    return first.sort_values("order_id").reset_index(drop=True)


def plot_histogram(first_day_df: pd.DataFrame, out_path: str, horizon_days: int | None = None) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams["font.family"] = "Malgun Gothic"
    plt.rcParams["axes.unicode_minus"] = False

    max_day = horizon_days or int(first_day_df["first_day"].max())
    counts = (
        first_day_df["first_day"].value_counts()
        .reindex(range(1, max_day + 1), fill_value=0)
        .sort_index()
    )

    fig, ax = plt.subplots(figsize=(max(8, max_day * 0.35), 5))
    ax.bar(counts.index, counts.values, color="#2e7d32", edgecolor="white", width=0.85)
    ax.set_xlabel("첫 생산 일자")
    ax.set_ylabel("주문 수")
    ax.set_title("주문별 첫 생산 일자 분포")
    tick_step = max(1, max_day // 30)
    ax.set_xticks(range(1, max_day + 1, tick_step))
    ax.yaxis.get_major_locator().set_params(integer=True)
    ax.grid(axis="y", alpha=0.3)

    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description="주문별 첫 생산 일자 히스토그램")
    parser.add_argument("--data-dir", default=_DATA_DIR, help="line_schedule.csv / order_fulfillment.csv가 있는 디렉터리")
    parser.add_argument("--out-dir", default=_HERE, help="결과(CSV/PNG)를 저장할 디렉터리")
    parser.add_argument("--out", default=None, help="저장할 PNG 경로 (기본: <out-dir>/first_production_day_histogram.png)")
    parser.add_argument("--csv-out", default=None, help="주문별 첫 생산일 CSV 저장 경로 (기본: <out-dir>/first_production_day.csv)")
    parser.add_argument("--horizon-days", type=int, default=None, help="히스토그램 x축 범위(기본: 실제 데이터의 최댓값까지만)")
    args = parser.parse_args()

    schedule_path = os.path.join(args.data_dir, "line_schedule.csv")
    schedule = pd.read_csv(schedule_path)

    first_day_df = compute_first_production_day(schedule)

    fulfillment_path = os.path.join(args.data_dir, "order_fulfillment.csv")
    if os.path.exists(fulfillment_path):
        fulfillment = pd.read_csv(fulfillment_path)
        never_produced = fulfillment.loc[fulfillment["produced"] <= 0, ["order_id", "product_id", "required"]]
        if not never_produced.empty:
            print(f"[경고] 계획기간 내내 한 번도 생산되지 않은 주문 {len(never_produced)}건:", file=sys.stderr)
            print(never_produced.to_string(index=False), file=sys.stderr)
        first_day_df = first_day_df.merge(
            fulfillment[["order_id", "required", "produced", "deadline_day"]], on="order_id", how="left"
        )

    print("[정보] 첫 생산 일자별 주문 수:")
    print(first_day_df["first_day"].value_counts().sort_index().to_string())

    os.makedirs(args.out_dir, exist_ok=True)
    csv_out = args.csv_out or os.path.join(args.out_dir, "first_production_day.csv")
    first_day_df.to_csv(csv_out, index=False, encoding="utf-8-sig")
    print(f"\n[저장 완료] {csv_out}")

    out_path = args.out or os.path.join(args.out_dir, "first_production_day_histogram.png")
    plot_histogram(first_day_df, out_path, horizon_days=args.horizon_days)
    print(f"[저장 완료] {out_path}")


if __name__ == "__main__":
    main()
