# -*- coding: utf-8 -*-
"""
output/plot_day.py

line_schedule.csv에서 특정 하루만 뽑아서 gantt_overview.png(scheduling/
report.py의 plot_gantt)와 같은 스타일로(행=라인, 열=그 날의 10개
시간슬롯) 그린다. 그 날 하루 종일 대기(idle)만 한 라인은 애초에 행을
만들지 않는다(동일 라인이 많은 그룹에서 그 날 실제로 안 돌아간 나머지가
차트를 채우는 걸 방지).

line_schedule.csv 자체는 CP-SAT을 다시 돌리지 않아도 되는 순수 후처리
입력이지만, 각 블록에 필요 인원수를 같이 적으려면(각 라인이 각 제품을
만들 때 몇 명이 필요한지) 그 값을 알고 있는 Line/Order 데이터가 다시
필요하다 - output/_data_source.py를 통해 labor_utilization.py와 동일한
방식(--data/--real-plan/기본 내장예시)으로 가져온다.

사용법:
    python plot_day.py --day 5
    python plot_day.py --day 5 --real-plan --dir real_plan
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

from _data_source import add_source_args, build_workers_lookup, resolve_source

SLOT_LABELS = ["08-09", "09-10", "10-11", "11-12", "13-14", "14-15", "15-16", "16-17", "17-18", "18-19"]
SLOT_ORDER = {s: i for i, s in enumerate(SLOT_LABELS)}


def plot_day(
    schedule: pd.DataFrame,
    day: int,
    out_path: str,
    workers_lookup: dict[tuple[str, str], int] | None = None,
) -> int:
    """schedule(line_schedule.csv를 그대로 읽은 DataFrame)에서 day
    하루치만 뽑아 PNG로 저장한다. workers_lookup을 주면 생산(produce)
    블록마다 그 라인/주문 조합에 필요한 인원수를 셀 가운데 숫자로 적는다
    (없으면 숫자 없이 색상만 - _data_source.build_workers_lookup으로
    만든 {(line_id, order_id): 인원수} 딕셔너리를 그대로 넣으면 됨).
    반환값은 실제로 그려진(그 날 활동이 있었던) 라인 개수.

    색상/범례는 order_id(주문) 단위로 나눈다 - product_id는 진짜
    식별자가 아니라 제품 "이름"이라서, 품번이 아직 안 나온 "코드확인중"
    같은 placeholder를 여러 다른 주문이 같이 쓸 수 있다. product_id로
    색을 나누면 그런 서로 다른 주문들이 전부 같은 색 하나로 뭉개져서
    구분이 안 된다 - order_id 기준으로 나누면 항상 정확히 구분된다.
    범례에는 "생산: {product_id} [{order_id}]"처럼 사람이 읽을 이름과
    진짜 식별자를 같이 보여준다.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patheffects as pe
    import numpy as np
    from matplotlib.colors import BoundaryNorm, ListedColormap
    from matplotlib.patches import Patch

    plt.rcParams["font.family"] = "Malgun Gothic"
    plt.rcParams["axes.unicode_minus"] = False

    day_df = schedule[schedule["day"] == day]
    if day_df.empty:
        raise ValueError(f"day={day}에 해당하는 행이 없습니다(line_schedule.csv의 day 범위를 확인하세요).")

    # 라인별로 그 날의 행만 모으고, 하루 종일 idle뿐인 라인은 아예 제외.
    # groupby(sort=False)로 CSV에 나온 순서(=원래 라인 그룹 순서)를 유지한다.
    line_ids: list[str] = []
    per_line: dict[str, pd.DataFrame] = {}
    for lid, g in day_df.groupby("line_id", sort=False):
        if (g["activity"] == "idle").all():
            continue
        per_line[lid] = g.set_index("slot")
        line_ids.append(lid)
    if not line_ids:
        raise ValueError(f"day={day}에 활동 중인 라인이 하나도 없습니다.")

    # 실제로 "생산"으로 표시될 주문만 범례/색상에 넣는다(셋업도 order_id가
    # 채워져 있지만 셋업은 주문과 무관하게 회색 하나로만 표시하므로 제외).
    # order_id -> product_id(범례에 사람이 읽을 이름을 같이 보여주기 위함,
    # 색상/그룹 자체는 항상 order_id 기준).
    order_product: dict[str, str] = {}
    for lid, g in per_line.items():
        for slot in g.index:
            if g.loc[slot, "activity"] == "produce":
                order_product[g.loc[slot, "order_id"]] = g.loc[slot, "product_id"]
    order_ids = sorted(order_product)
    order_index = {oid: i + 2 for i, oid in enumerate(order_ids)}

    # tab20 하나로는 20색뿐이라 주문이 그보다 많으면 모자란다(scheduling/
    # report.py의 plot_gantt와 동일한 이유/해결책 - 60색까지 이어붙이고
    # 그래도 모자라면 순환).
    palette = (
        list(plt.get_cmap("tab20").colors)
        + list(plt.get_cmap("tab20b").colors)
        + list(plt.get_cmap("tab20c").colors)
    )
    order_colors = [palette[i % len(palette)] for i in range(len(order_ids))]
    base_colors = ["#f2f2f2", "#9e9e9e"] + order_colors  # 0=idle, 1=setup, 2..=주문별
    cmap = ListedColormap(base_colors)
    norm = BoundaryNorm(list(range(len(base_colors) + 1)), cmap.N)

    # (r,c) -> 그 칸에 적을 인원수 텍스트. produce 칸에서만 채워진다 -
    # setup은 라인 작업자 인원 수요가 0이라(별도 인력이 처리, scheduling/
    # solver.py 참고) 애초에 표시할 인원수 자체가 없다.
    worker_labels: dict[tuple[int, int], str] = {}
    missing_workers: set[tuple[str, str]] = set()

    grid = np.zeros((len(line_ids), len(SLOT_LABELS)), dtype=int)
    for r, lid in enumerate(line_ids):
        g = per_line[lid]
        for c, slot in enumerate(SLOT_LABELS):
            if slot not in g.index:
                continue
            activity = g.loc[slot, "activity"]
            if activity == "idle":
                grid[r, c] = 0
            elif activity == "setup":
                grid[r, c] = 1
            else:
                oid = g.loc[slot, "order_id"]
                grid[r, c] = order_index[oid]
                if workers_lookup is not None:
                    workers = workers_lookup.get((lid, oid))
                    if workers is None:
                        missing_workers.add((lid, oid))
                    else:
                        worker_labels[r, c] = str(workers)

    if missing_workers:
        print(
            f"[경고] workers 정보가 없는 (line_id, order_id) 조합 {len(missing_workers)}건은 "
            f"인원수를 표시하지 않습니다: {sorted(missing_workers)}",
            file=sys.stderr,
        )

    fig_w = max(6, len(SLOT_LABELS) * 0.8)
    fig_h = max(3, 0.5 * len(line_ids) + 1.5)
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    ax.imshow(grid, aspect="auto", cmap=cmap, norm=norm, interpolation="none")

    for (r, c), label in worker_labels.items():
        ax.text(
            c, r, label, ha="center", va="center", fontsize=8, color="black",
            path_effects=[pe.withStroke(linewidth=2.5, foreground="white")],
        )

    for c in range(len(SLOT_LABELS) + 1):
        ax.axvline(c - 0.5, color="white", linewidth=0.6)

    ax.set_xticks(range(len(SLOT_LABELS)))
    ax.set_xticklabels(SLOT_LABELS, rotation=45, ha="right")
    ax.set_yticks(range(len(line_ids)))
    ax.set_yticklabels(line_ids)
    ax.set_title(f"생산 스케줄 - {day}일차 (행: 라인, 열: 시간슬롯)")

    legend_items = [
        Patch(facecolor="#f2f2f2", edgecolor="gray", label="대기(idle)"),
        Patch(facecolor="#9e9e9e", label="셋업(setup)"),
    ]
    for oid in order_ids:
        legend_items.append(Patch(facecolor=base_colors[order_index[oid]], label=f"생산: {order_product[oid]} [{oid}]"))
    ax.legend(handles=legend_items, bbox_to_anchor=(1.02, 1), loc="upper left", fontsize=8)

    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return len(line_ids)


def main():
    parser = argparse.ArgumentParser(description="line_schedule.csv에서 하루만 뽑아 간트 차트로 그리기")
    parser.add_argument("--dir", default=None, help="line_schedule.csv가 있는 디렉터리")
    parser.add_argument("--day", type=int, required=True, help="그릴 날짜(1-indexed)")
    parser.add_argument("--out", default=None, help="저장할 PNG 경로 (기본: <dir>/gantt_day<N>.png)")
    parser.add_argument(
        "--no-workers", action="store_true",
        help="블록에 필요 인원수를 적지 않음(기본은 표시함)",
    )
    add_source_args(parser)
    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    lines, orders, default_dir = resolve_source(args, script_dir)
    args.dir = args.dir or default_dir
    workers_lookup = None if args.no_workers else build_workers_lookup(lines, orders)

    schedule_path = os.path.join(args.dir, "line_schedule.csv")
    schedule = pd.read_csv(schedule_path)
    out_path = args.out or os.path.join(args.dir, f"gantt_day{args.day}.png")

    n_lines = plot_day(schedule, args.day, out_path, workers_lookup=workers_lookup)
    print(f"[정보] {args.day}일차 - 활동 있는 라인 {n_lines}개 표시")
    print(f"[저장 완료] {out_path}")


if __name__ == "__main__":
    main()
