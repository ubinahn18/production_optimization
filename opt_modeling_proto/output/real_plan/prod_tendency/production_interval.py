# -*- coding: utf-8 -*-
"""
output/real_plan/prod_tendency/production_interval.py

각 주문(order_id)이 계획기간 중 "처음 생산된 날"부터 "마지막으로 생산된
날"까지 며칠에 걸쳐 흩어져 있는지(production_interval_days = 마지막
생산일 - 첫 생산일, 하루만 만들고 끝났으면 0)를 line_schedule.csv에서
뽑아내고, 그 분포를 히스토그램(막대그래프: x축=간격 일수, y축=그 간격을
가진 주문 수)으로 그린다. first_production_day.py가 "언제 시작하는지"를
본다면, 이건 "한 번 시작한 주문이 끝날 때까지 며칠에 걸쳐 끊기는지"를
보는 셈이다 - 값이 크면 그 주문이 계획기간에 걸쳐 여러 날 나눠서
생산됐다는 뜻이고, 0/작으면 며칠 안에 몰아서 끝났다는 뜻이다(중간에
쉬는 날이 있어도 상관없이 시작~끝 날짜 차이만 본다).

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
    python production_interval.py
    python production_interval.py --data-dir ../other_plan --out interval.png
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


def compute_production_interval(schedule: pd.DataFrame) -> pd.DataFrame:
    """schedule(line_schedule.csv를 그대로 읽은 DataFrame)에서 order_id별
    첫 생산일(first_day)/마지막 생산일(last_day)/그 차이(interval_days)를
    뽑는다. product_id는 참고용으로 같이 붙인다. 생산 기록이 아예 없으면
    ValueError."""
    produce = schedule[schedule["activity"] == "produce"]
    if produce.empty:
        raise ValueError("line_schedule.csv에 생산(produce) 기록이 전혀 없습니다.")
    span = produce.groupby("order_id", as_index=False).agg(
        first_day=("day", "min"), last_day=("day", "max"), product_id=("product_id", "first")
    )
    span["interval_days"] = span["last_day"] - span["first_day"]
    return span.sort_values("order_id").reset_index(drop=True)


def plot_histogram(interval_df: pd.DataFrame, out_path: str, max_interval: int | None = None) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams["font.family"] = "Malgun Gothic"
    plt.rcParams["axes.unicode_minus"] = False

    max_x = max_interval if max_interval is not None else int(interval_df["interval_days"].max())
    counts = (
        interval_df["interval_days"].value_counts()
        .reindex(range(0, max_x + 1), fill_value=0)
        .sort_index()
    )

    fig, ax = plt.subplots(figsize=(max(8, max_x * 0.35), 5))
    ax.bar(counts.index, counts.values, color="#1565c0", edgecolor="white", width=0.85)
    ax.set_xlabel("생산 간격(마지막 생산일 - 첫 생산일, 일)")
    ax.set_ylabel("주문 수")
    ax.set_title("주문별 생산 간격 분포")
    tick_step = max(1, (max_x + 1) // 30)
    ax.set_xticks(range(0, max_x + 1, tick_step))
    ax.yaxis.get_major_locator().set_params(integer=True)
    ax.grid(axis="y", alpha=0.3)

    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description="주문별 생산 간격(마지막 생산일 - 첫 생산일) 히스토그램")
    parser.add_argument("--data-dir", default=_DATA_DIR, help="line_schedule.csv / order_fulfillment.csv가 있는 디렉터리")
    parser.add_argument("--out-dir", default=_HERE, help="결과(CSV/PNG)를 저장할 디렉터리")
    parser.add_argument("--out", default=None, help="저장할 PNG 경로 (기본: <out-dir>/production_interval_histogram.png)")
    parser.add_argument("--csv-out", default=None, help="주문별 생산 간격 CSV 저장 경로 (기본: <out-dir>/production_interval.csv)")
    parser.add_argument("--max-interval", type=int, default=None, help="히스토그램 x축 범위(기본: 실제 데이터의 최댓값까지만)")
    parser.add_argument(
        "--min-interval-print", type=int, default=5,
        help="이 값 이상인 주문을 [간격, 납기, 생산량]으로 콘솔에 나열(기본 5)",
    )
    args = parser.parse_args()

    schedule_path = os.path.join(args.data_dir, "line_schedule.csv")
    schedule = pd.read_csv(schedule_path)

    interval_df = compute_production_interval(schedule)

    fulfillment_path = os.path.join(args.data_dir, "order_fulfillment.csv")
    if os.path.exists(fulfillment_path):
        fulfillment = pd.read_csv(fulfillment_path)
        never_produced = fulfillment.loc[fulfillment["produced"] <= 0, ["order_id", "product_id", "required"]]
        if not never_produced.empty:
            print(f"[경고] 계획기간 내내 한 번도 생산되지 않은 주문 {len(never_produced)}건:", file=sys.stderr)
            print(never_produced.to_string(index=False), file=sys.stderr)
        interval_df = interval_df.merge(
            fulfillment[["order_id", "required", "produced", "deadline_day"]], on="order_id", how="left"
        )

    print("[정보] 생산 간격(일)별 주문 수:")
    print(interval_df["interval_days"].value_counts().sort_index().to_string())
    print(f"\n[정보] 평균 {interval_df['interval_days'].mean():.1f}일 / 중앙값 {interval_df['interval_days'].median():.1f}일 "
          f"/ 최댓값 {interval_df['interval_days'].max()}일")

    wide = interval_df[interval_df["interval_days"] >= args.min_interval_print].sort_values(
        "interval_days", ascending=False
    )
    if not wide.empty:
        print(f"\n[정보] 생산 간격 {args.min_interval_print}일 이상인 주문 {len(wide)}건 [간격, 납기, 생산량]:")
        has_fulfillment_cols = "deadline_day" in wide.columns and "produced" in wide.columns
        for row in wide.itertuples(index=False):
            deadline = row.deadline_day if has_fulfillment_cols else "?"
            produced = row.produced if has_fulfillment_cols else "?"
            print(f"  {row.order_id}: [{row.interval_days}, {deadline}, {produced}]")

    os.makedirs(args.out_dir, exist_ok=True)
    csv_out = args.csv_out or os.path.join(args.out_dir, "production_interval.csv")
    interval_df.to_csv(csv_out, index=False, encoding="utf-8-sig")
    print(f"\n[저장 완료] {csv_out}")

    out_path = args.out or os.path.join(args.out_dir, "production_interval_histogram.png")
    plot_histogram(interval_df, out_path, max_interval=args.max_interval)
    print(f"[저장 완료] {out_path}")


if __name__ == "__main__":
    main()
