# -*- coding: utf-8 -*-
"""
output/real_plan/prod_tendency/order_gantt.py

주문(order_id)별로, 그 주문이 계획기간 전체에 걸쳐 어느 라인에서 언제
생산/셋업됐는지를 gantt_overview.png와 같은 스타일(행=라인, 열=계획기간
전체 시간슬롯)로 그린다 - "gantt_overview에서 그 주문만 보이게 필터링한"
셈. 같은 라인을 다른 주문이 같이 쓰는 구간은 "다른 주문" 한 가지 색으로
뭉뚱그려 표시해서, 이 주문이 실제로 언제 그 라인을 점유했는지와 언제
비어 있었는지(idle)를 구분할 수 있게 한다.

--order-id를 생략하면(기본 동작) order_fulfillment.csv(없으면
line_schedule.csv)에 있는 전체 주문 각각에 대해 그림을 하나씩 만들어서
<out-dir>/order_gantts/ 폴더에 전부 저장한다. --order-id를 주면 그 주문
하나만 만든다(같은 폴더에 저장). 계획기간 내내 한 번도 생산되지 않은
주문은 (전체 모드에서는) 건너뛰고 콘솔에 알려준다.

line_schedule.csv만 있으면 그리는 자체는 되지만, order_fulfillment.csv가
같은 위치에 있으면 필요/생산 수량, 마감일/완료일도 같이 출력하고 마감일
표시선도 그려준다.

이 스크립트는 real_plan/ 바로 밑이 아니라 real_plan/prod_tendency/에
있다 - 그래서 입력(line_schedule.csv/order_fulfillment.csv)은 기본적으로
한 단계 위(real_plan/)에서 읽고, 결과(order_gantts/ 폴더)는 이 스크립트가
있는 prod_tendency/ 폴더 밑에 저장한다(--data-dir/--out-dir로 둘 다
바꿀 수 있음).

사용법 (이 파일이 있는 output/real_plan/prod_tendency/ 디렉터리에서):
    python order_gantt.py                        # 전체 주문 -> order_gantts/
    python order_gantt.py --order-id S2026034SA012_8
"""

from __future__ import annotations

import argparse
import io
import os
import re
import sys

import pandas as pd

# Windows 콘솔(cp949 등) 기본 인코딩에서는 한글 출력이 깨지므로 UTF-8로 강제.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)

_HERE = os.path.dirname(os.path.abspath(__file__))  # .../real_plan/prod_tendency
_DATA_DIR = os.path.dirname(_HERE)                   # .../real_plan (line_schedule.csv 등이 있는 곳)

SLOT_LABELS = ["08-09", "09-10", "10-11", "11-12", "13-14", "14-15", "15-16", "16-17", "17-18", "18-19"]
SLOT_ORDER = {s: i for i, s in enumerate(SLOT_LABELS)}

_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*]')


def plot_order(
    schedule: pd.DataFrame,
    order_id: str,
    out_path: str,
    deadline_day: float | None = None,
) -> int:
    """schedule(line_schedule.csv를 그대로 읽은 DataFrame)에서 order_id가
    한 번이라도 등장하는 라인만 골라, 계획기간 전체를 행=라인/열=시간슬롯
    그리드로 그려 out_path에 저장한다. 반환값은 표시된 라인 개수.
    order_id가 아예 등장하지 않으면 ValueError(오타 확인용).

    deadline_day를 주면(order_fulfillment.csv의 deadline_day, ASAP 주문이라
    None/NaN이면 생략) scheduling/report.py의 plot_gantt와 같은 방식으로
    그 날짜 끝에 빨간 점선을 그어 마감 시점을 표시한다."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.colors import BoundaryNorm, ListedColormap
    from matplotlib.patches import Patch

    plt.rcParams["font.family"] = "Malgun Gothic"
    plt.rcParams["axes.unicode_minus"] = False

    relevant_lines = sorted(schedule.loc[schedule["order_id"] == order_id, "line_id"].unique())
    if not relevant_lines:
        raise ValueError(
            f"order_id={order_id!r}가 line_schedule.csv에 없습니다(오타이거나 이번 계획에서 생산되지 않았을 수 있습니다)."
        )

    days = sorted(schedule["day"].unique())
    day_index = {d: i for i, d in enumerate(days)}
    T = len(days) * len(SLOT_LABELS)

    # 0=idle, 1=다른 주문 작업 중, 2=셋업(이 주문), 3=생산(이 주문)
    base_colors = ["#f2f2f2", "#c9c9c9", "#f0ad4e", "#2e7d32"]
    cmap = ListedColormap(base_colors)
    norm = BoundaryNorm(list(range(len(base_colors) + 1)), cmap.N)

    line_index = {lid: r for r, lid in enumerate(relevant_lines)}
    grid = np.zeros((len(relevant_lines), T), dtype=int)
    sub = schedule[schedule["line_id"].isin(relevant_lines)]
    for row in sub.itertuples(index=False):
        r = line_index[row.line_id]
        c = day_index[row.day] * len(SLOT_LABELS) + SLOT_ORDER[row.slot]
        if row.activity == "idle":
            grid[r, c] = 0
        elif row.order_id == order_id:
            grid[r, c] = 3 if row.activity == "produce" else 2
        else:
            grid[r, c] = 1

    fig_w = max(12, T / 25)
    fig_h = max(3, 0.5 * len(relevant_lines) + 1.5)
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    ax.imshow(grid, aspect="auto", cmap=cmap, norm=norm, interpolation="none")

    for d in range(len(days) + 1):
        ax.axvline(d * len(SLOT_LABELS) - 0.5, color="white", linewidth=0.6)

    tick_idx = list(range(0, len(days), 5)) or [0]
    ax.set_xticks([i * len(SLOT_LABELS) + len(SLOT_LABELS) / 2 - 0.5 for i in tick_idx])
    ax.set_xticklabels([f"{days[i]}일" for i in tick_idx])
    ax.set_yticks(range(len(relevant_lines)))
    ax.set_yticklabels(relevant_lines)
    ax.set_title(f"주문 [{order_id}] 생산 스케줄 (행: 라인, 열: 시간슬롯)")

    has_deadline = deadline_day is not None and not pd.isna(deadline_day) and int(deadline_day) in day_index
    if has_deadline:
        d = int(deadline_day)
        x = day_index[d] * len(SLOT_LABELS) + len(SLOT_LABELS) - 0.5
        ax.axvline(x, color="red", linestyle="--", linewidth=1.5, alpha=0.9, zorder=5)
        ax.text(x, -0.6, f"마감 {d}일", color="red", fontsize=8, rotation=90, ha="right", va="bottom")
        ax.set_ylim(len(relevant_lines) - 0.5, -2.5)  # 위쪽에 마감일 라벨 적을 여백 확보

    legend_items = [
        Patch(facecolor=base_colors[0], edgecolor="gray", label="대기(idle)"),
        Patch(facecolor=base_colors[1], label="다른 주문 작업 중"),
        Patch(facecolor=base_colors[2], label="셋업(이 주문)"),
        Patch(facecolor=base_colors[3], label=f"생산: {order_id}"),
    ]
    if has_deadline:
        from matplotlib.lines import Line2D
        legend_items.append(Line2D([0], [0], color="red", linestyle="--", linewidth=1.5, label="마감일"))
    ax.legend(handles=legend_items, bbox_to_anchor=(1.02, 1), loc="upper left", fontsize=8)

    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return len(relevant_lines)


def _resolve_order_ids(order_id_arg: str | None, schedule: pd.DataFrame, fulfillment: pd.DataFrame | None) -> list[str]:
    """--order-id가 있으면 그 하나만, 없으면 이번 계획의 전체 주문 목록을
    돌려준다. order_fulfillment.csv(fulfillment)가 있으면 그게 "생산량
    0인 주문까지 포함한 진짜 전체 목록"이라 우선 쓰고(line_schedule.csv에는
    아예 생산 안 된 주문의 행 자체가 안 남으므로), 없으면 line_schedule.csv에
    등장하는 order_id로 대신한다."""
    if order_id_arg:
        return [order_id_arg]
    if fulfillment is not None:
        return list(fulfillment.index)
    return sorted(schedule["order_id"].dropna().unique())


def main():
    parser = argparse.ArgumentParser(
        description="주문(order_id) 생산 스케줄 그리기. --order-id를 생략하면 전체 주문 각각에 대해 그림을 만든다."
    )
    parser.add_argument("--order-id", default=None, help="이 주문 하나만 그리기(생략하면 전체 주문 각각에 대해 그림)")
    parser.add_argument("--data-dir", default=_DATA_DIR, help="line_schedule.csv / order_fulfillment.csv가 있는 디렉터리")
    parser.add_argument("--out-dir", default=None, help="PNG를 저장할 폴더 (기본: <이 스크립트 위치>/order_gantts)")
    args = parser.parse_args()

    schedule_path = os.path.join(args.data_dir, "line_schedule.csv")
    schedule = pd.read_csv(schedule_path)

    fulfillment = None
    fulfillment_path = os.path.join(args.data_dir, "order_fulfillment.csv")
    if os.path.exists(fulfillment_path):
        fulfillment = pd.read_csv(fulfillment_path).set_index("order_id")
    elif args.order_id is None:
        print("[경고] order_fulfillment.csv가 없어서 line_schedule.csv에 등장하는 주문만 대상으로 합니다"
              "(생산량 0인 주문은 애초에 목록에서 빠질 수 있습니다).", file=sys.stderr)

    order_ids = _resolve_order_ids(args.order_id, schedule, fulfillment)
    if args.order_id and fulfillment is not None and args.order_id not in fulfillment.index:
        print(f"[경고] order_fulfillment.csv에 order_id={args.order_id!r}가 없습니다(오타 확인).", file=sys.stderr)

    out_dir = args.out_dir or os.path.join(_HERE, "order_gantts")
    os.makedirs(out_dir, exist_ok=True)
    if len(order_ids) > 1:
        print(f"[정보] 전체 주문 {len(order_ids)}건에 대해 그림을 생성합니다 -> {out_dir}")

    saved, skipped = 0, []
    for oid in order_ids:
        deadline_day = None
        if fulfillment is not None and oid in fulfillment.index:
            f = fulfillment.loc[oid]
            deadline_day = f["deadline_day"]
            print(
                f"[정보] {oid} ({f['product_id']}): 필요 {f['required']:,.0f} / 생산 {f['produced']:,.0f} "
                f"| 마감일 {f['deadline_day']} | 완료일 {f['completion_day']}"
            )

        safe_name = _INVALID_FILENAME_CHARS.sub("_", oid)
        out_path = os.path.join(out_dir, f"gantt_order_{safe_name}.png")
        try:
            n_lines = plot_order(schedule, oid, out_path, deadline_day=deadline_day)
        except ValueError as e:
            skipped.append(oid)
            print(f"[건너뜀] {e}", file=sys.stderr)
            continue
        saved += 1
        print(f"[저장 완료] {oid} - 관련 라인 {n_lines}개 -> {out_path}")

    if len(order_ids) > 1:
        print(f"\n[정보] 총 {len(order_ids)}건 중 {saved}건 저장, {len(skipped)}건 건너뜀(생산 기록 없음)")


if __name__ == "__main__":
    main()
