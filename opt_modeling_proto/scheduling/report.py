# -*- coding: utf-8 -*-
"""
scheduling/report.py

ScheduleResult를 사람이 보는 형태로 바꾸는 부분: 콘솔 리포트 출력, CSV
저장, 간트 스타일 개요 차트(PNG) 생성. CP-SAT 관련 로직은 전혀 없고
순수하게 solver.build_and_solve()가 반환한 결과를 소비하기만 한다.
"""

from __future__ import annotations

import os

from .models import SLOTS_PER_DAY, Order, ScheduleConfig, ScheduleResult


def _compress_runs(entries: list[tuple[int, str, str, str]]) -> list[tuple[int, str, int, str, str, str]]:
    """(day, slot_label, activity, order_id) 슬롯별 리스트를, 연속으로 같은
    (activity, order_id)가 이어지는 구간을 하나로 뭉쳐서 (시작일, 시작슬롯,
    끝일, 끝슬롯, activity, order_id) 형태로 압축한다. 300줄짜리 슬롯
    로그를 사람이 읽을 수 있는 요약으로 바꾸기 위한 용도(요청받은 적은
    없지만, 콘솔에 300줄을 그대로 뿌리면 아무도 못 읽으므로 가독성을
    위해 추가). activity뿐 아니라 order_id도 같이 비교하는 이유: 서로
    다른 주문이 같은 product_id(예: "코드확인중" 같은 placeholder)를
    공유하면 activity 문자열만으로는 같은 구간인지 구분이 안 되므로,
    실제로는 다른 주문인데 하나로 잘못 뭉쳐질 수 있다.
    """
    if not entries:
        return []
    runs = []
    start_day, start_slot, cur_activity, cur_order = entries[0]
    prev_day, prev_slot = start_day, start_slot
    for day, slot_label, activity, order_id in entries[1:]:
        if activity != cur_activity or order_id != cur_order:
            runs.append((start_day, start_slot, prev_day, prev_slot, cur_activity, cur_order))
            start_day, start_slot, cur_activity, cur_order = day, slot_label, activity, order_id
        prev_day, prev_slot = day, slot_label
    runs.append((start_day, start_slot, prev_day, prev_slot, cur_activity, cur_order))
    return runs


def print_report(result: ScheduleResult, orders: list[Order]):
    print(f"[결과] solver 상태: {result.status_name}")
    if not result.is_feasible:
        print("[결과] 실행 가능한 스케줄을 찾지 못했습니다. 마감일/라인수/인원 데이터를 확인하세요.")
        return

    print(f"[결과] 총 비용: {result.total_cost:,.0f}  (실 인건비 {result.labor_cost:,.0f} + ASAP backlog 페널티 {result.backlog_cost:,.0f})")
    if result.continuity_score is not None:
        print(f"[결과] 연속성 점수(대기<->생산 전환 + 셋업 횟수, 작을수록 좋음): {result.continuity_score}")

    print("\n[결과] 주문별 이행 현황:")
    for o in orders:
        f = result.order_fulfillment[o.order_id]
        if f["deadline_day"] is None:
            # ASAP 주문: 하드 마감이 없으므로 지연 여부 대신 완료일(다 만든
            # 날짜, 아직 못 채웠으면 None)과 계획기간 마지막날 기준 잔여
            # backlog를 보여준다.
            deadline_label = "ASAP"
            if f["completion_day"] is not None:
                status = f"완료일 {f['completion_day']}일 [OK]"
            else:
                status = f"미완료, 잔여 backlog {f['final_backlog']:,} [!!]"
            print(
                f"  {o.order_id}({f['product_id']}): 필요 {f['required']:>8,} / 생산 {f['produced']:>8,} "
                f"| 마감일 {deadline_label:>4} | {status}"
            )
        else:
            met = "OK" if (f["completion_day"] is not None and f["completion_day"] <= f["deadline_day"]) else "!!지연"
            print(
                f"  {o.order_id}({f['product_id']}): 필요 {f['required']:>8,} / 생산 {f['produced']:>8,} "
                f"| 마감일 {f['deadline_day']:>2}일 | 완료일 {f['completion_day']}일 [{met}]"
            )

    print("\n[결과] 일별 고용인원 / 잔업인원 (앞 10일 예시):")
    for d in list(result.daily_workforce.keys())[:10]:
        ot = result.overtime_workers[d]
        print(f"  {d:>2}일차: 고용 {result.daily_workforce[d]:>3}명  (잔업 17-18: {ot['17-18']:>2}명, 18-19: {ot['18-19']:>2}명)")

    print("\n[결과] 라인별 활동 요약 (연속 구간 압축, 라인당 앞 8개 구간만 표시):")
    for lid, entries in result.line_activity.items():
        runs = _compress_runs(entries)
        print(f"  [{lid}]")
        for sd, ss, ed, es, activity, order_id in runs[:8]:
            suffix = f" ({order_id})" if order_id else ""
            print(f"    {sd}일 {ss} ~ {ed}일 {es} : {activity}{suffix}")
        if len(runs) > 8:
            print(f"    ... (총 {len(runs)}개 구간, 나머지는 CSV 참고)")


def save_outputs(result: ScheduleResult, orders: list[Order], output_dir: str):
    os.makedirs(output_dir, exist_ok=True)
    if not result.is_feasible:
        return

    import pandas as pd

    # 1) 라인별 슬롯 단위 전체 스케줄 (가장 상세한 원본 데이터)
    #    order_id를 같이 저장하는 이유: 서로 다른 주문이 같은 product_id를
    #    공유할 수 있어서(예: 품번이 아직 안 나온 "코드확인중" 같은
    #    placeholder를 여러 실제 제품이 같이 씀), product_id만으로는
    #    어느 주문의 생산/셋업인지 구분이 안 될 수 있다.
    rows = []
    for lid, entries in result.line_activity.items():
        for day, slot_label, activity, order_id in entries:
            kind, _, product = activity.partition(":")
            rows.append({
                "line_id": lid, "day": day, "slot": slot_label, "activity": kind,
                "product_id": product, "order_id": order_id,
            })
    schedule_df = pd.DataFrame(rows)
    schedule_csv = os.path.join(output_dir, "line_schedule.csv")
    schedule_df.to_csv(schedule_csv, index=False, encoding="utf-8-sig")

    # 2) 라인별 압축된(연속 구간) 스케줄 - 사람이 보기 편한 버전
    run_rows = []
    for lid, entries in result.line_activity.items():
        for sd, ss, ed, es, activity, order_id in _compress_runs(entries):
            kind, _, product = activity.partition(":")
            run_rows.append(
                {"line_id": lid, "start_day": sd, "start_slot": ss, "end_day": ed, "end_slot": es,
                 "activity": kind, "product_id": product, "order_id": order_id}
            )
    runs_csv = os.path.join(output_dir, "line_schedule_compressed.csv")
    pd.DataFrame(run_rows).to_csv(runs_csv, index=False, encoding="utf-8-sig")

    # 3) 일별 인건비 요약
    daily_rows = []
    for d, workforce in result.daily_workforce.items():
        daily_rows.append(
            {"day": d, "workforce": workforce,
             "overtime_17_18": result.overtime_workers[d]["17-18"],
             "overtime_18_19": result.overtime_workers[d]["18-19"]}
        )
    daily_csv = os.path.join(output_dir, "daily_workforce.csv")
    pd.DataFrame(daily_rows).to_csv(daily_csv, index=False, encoding="utf-8-sig")

    # 4) 주문 이행 현황
    order_rows = []
    for o in orders:
        f = result.order_fulfillment[o.order_id]
        order_rows.append({"order_id": o.order_id, **f})
    orders_csv = os.path.join(output_dir, "order_fulfillment.csv")
    pd.DataFrame(order_rows).to_csv(orders_csv, index=False, encoding="utf-8-sig")

    print(f"\n[정보] CSV 저장 완료: {output_dir}")
    for p in [schedule_csv, runs_csv, daily_csv, orders_csv]:
        print(f"  - {p}")


def plot_gantt(result: ScheduleResult, orders: list[Order], config: ScheduleConfig, output_dir: str):
    """라인 x 시간슬롯 전체를 한 장의 히트맵으로 그려서(행=라인, 열=슬롯)
    대기/셋업/생산을 색으로 구분해 보여주는 간트 스타일 개요 차트.
    슬롯이 300개라 x축이 촘촘하므로, 하루 단위(10슬롯마다) 굵은 구분선만
    긋고 5일 간격으로 날짜 라벨을 단다.
    """
    if not result.is_feasible:
        return

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.colors import ListedColormap, BoundaryNorm
    from matplotlib.patches import Patch
    from matplotlib.lines import Line2D

    plt.rcParams["font.family"] = "Malgun Gothic"
    plt.rcParams["axes.unicode_minus"] = False

    # 계획기간 내내 단 한 번도 안 쓰인(전부 idle) 라인은 차트에서 뺀다 -
    # 동일 라인이 많은 그룹(예: 단발 19대)에서 실제로 쓰인 몇 대만 빼고
    # 나머지가 전부 회색 idle 줄로 차트를 채워서 정작 중요한 부분이
    # 안 보이게 되는 걸 막기 위함.
    line_ids = [
        lid for lid, entries in result.line_activity.items()
        if any(activity != "idle" for _, _, activity, _ in entries)
    ]
    T = config.horizon_days * SLOTS_PER_DAY
    # 색상/범례는 order_id(주문) 단위로 나눈다 - product_id는 진짜 식별자가
    # 아니라 제품 "이름"이라서 여러 주문이 같은 값을 공유할 수 있다(예:
    # 품번이 아직 안 나온 "코드확인중" 같은 placeholder). product_id로
    # 색을 나누면 서로 다른 주문들이 같은 색 하나로 뭉개진다.
    order_ids_present = sorted({o.order_id for o in orders})
    order_product = {o.order_id: o.product_id for o in orders}
    # 0=idle, 1=setup(공통 회색), 2..=주문별 생산 색상
    order_index = {oid: i + 2 for i, oid in enumerate(order_ids_present)}

    grid = np.zeros((len(line_ids), T), dtype=int)
    for r, lid in enumerate(line_ids):
        for t, (day, slot_label, activity, order_id) in enumerate(result.line_activity[lid]):
            if activity == "idle":
                grid[r, t] = 0
            elif activity.startswith("setup:"):
                grid[r, t] = 1
            else:
                grid[r, t] = order_index[order_id]

    # tab20 하나로는 20가지 색밖에 없어서, 주문 종류가 그보다 많으면(실제
    # 수주 데이터에서 흔함) 만들어지는 색상표가 order_ids_present 개수보다
    # 모자라 ListedColormap이 죽는다. tab20/tab20b/tab20c(총 60색)를
    # 이어붙이고, 그래도 모자라면(주문 60종 초과) 순환시켜서 항상
    # len(order_ids_present)개만큼의 색을 확보한다.
    n_colors = 2 + len(order_ids_present)
    palette = (
        list(plt.get_cmap("tab20").colors)
        + list(plt.get_cmap("tab20b").colors)
        + list(plt.get_cmap("tab20c").colors)
    )
    order_colors = [palette[i % len(palette)] for i in range(len(order_ids_present))]
    base_colors = ["#f2f2f2", "#9e9e9e"] + order_colors
    cmap = ListedColormap(base_colors)
    bounds = list(range(n_colors + 1))
    norm = BoundaryNorm(bounds, cmap.N)

    fig_w = max(12, T / 25)
    fig_h = max(3, 0.5 * len(line_ids) + 1.5)
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    ax.imshow(grid, aspect="auto", cmap=cmap, norm=norm, interpolation="none")

    for d in range(config.horizon_days + 1):
        ax.axvline(d * SLOTS_PER_DAY - 0.5, color="white", linewidth=0.6)

    # 주문 마감일을 빨간 점선으로 표시. 같은 날짜가 마감인 주문이 여러 개면
    # 점선 하나로 묶고 그 위에 해당 product_id들을 세로로 적어둔다.
    # ASAP 주문(deadline_day=None)은 하드 마감이 없으므로 여기서는 그린다.
    deadlines_by_day: dict[int, list[str]] = {}
    for o in orders:
        if o.deadline_day is None:
            continue
        deadlines_by_day.setdefault(o.deadline_day, []).append(o.product_id)
    for d, product_ids_due in deadlines_by_day.items():
        x = d * SLOTS_PER_DAY - 0.5  # 그 날짜의 마지막 슬롯 바로 뒤 경계선 = 마감 시점
        ax.axvline(x, color="red", linestyle="--", linewidth=1.3, alpha=0.9, zorder=5)
        ax.text(x, -0.6, ",".join(product_ids_due), color="red", fontsize=7,
                rotation=90, ha="right", va="bottom")
    ax.set_ylim(len(line_ids) - 0.5, -2.5)  # 위쪽에 마감일 라벨 적을 여백 확보

    tick_days = list(range(0, config.horizon_days, 5)) or [0]
    ax.set_xticks([d * SLOTS_PER_DAY + SLOTS_PER_DAY / 2 - 0.5 for d in tick_days])
    ax.set_xticklabels([f"{d + 1}일" for d in tick_days])
    ax.set_yticks(range(len(line_ids)))
    ax.set_yticklabels(line_ids)
    ax.set_title("생산 스케줄 개요 (행: 라인, 열: 시간슬롯 1~30일)")

    legend_items = [Patch(facecolor="#f2f2f2", edgecolor="gray", label="대기(idle)"),
                     Patch(facecolor="#9e9e9e", label="셋업(setup)")]
    for oid in order_ids_present:
        legend_items.append(Patch(facecolor=base_colors[order_index[oid]], label=f"생산: {order_product[oid]} [{oid}]"))
    legend_items.append(Line2D([0], [0], color="red", linestyle="--", linewidth=1.3, label="마감일"))
    ax.legend(handles=legend_items, bbox_to_anchor=(1.02, 1), loc="upper left", fontsize=8)

    fig.tight_layout()
    p = os.path.join(output_dir, "gantt_overview.png")
    fig.savefig(p, dpi=150)
    plt.close(fig)
    print(f"  - {p}")
