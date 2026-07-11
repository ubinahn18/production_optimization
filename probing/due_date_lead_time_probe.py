# -*- coding: utf-8 -*-
"""
due_date_lead_time_probe.py

"수주진행현황" 엑셀의 '2026년07월' 시트만 사용해서
1) 납기일자(납기\n(인폼 기준)) 분포를 타임라인 위에 tick/점으로 표시
2) (부자재입고예정일 - 납기일) 분포
3) (원료입고예정일 - 납기일) 분포
를 그려서 PNG로 저장한다.
"""

import argparse
import io
import os
import sys

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

plt.rcParams["font.family"] = "Malgun Gothic"
plt.rcParams["axes.unicode_minus"] = False

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

DEFAULT_EXCEL = r"C:\Users\ubina\Downloads\수주진행현황(simulation_DATA_1)1.xlsx"
DEFAULT_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
SHEET_NAME = "2026년07월"

DUE_COL = "납기\n(인폼 기준)"
SUBMAT_COL = "부자재입고예정일"
MATERIAL_COL = "원료입고예정일"

# reference palette (dataviz skill) -- single-series blue on a light surface
SURFACE = "#fcfcfb"
INK_PRIMARY = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRIDLINE = "#e1e0d9"
BASELINE = "#c3c2b7"
SERIES_BLUE = "#2a78d6"


def load_data(excel_path):
    df = pd.read_excel(excel_path, sheet_name=SHEET_NAME, header=1)
    due = pd.to_datetime(df[DUE_COL], errors="coerce")
    submat = pd.to_datetime(df[SUBMAT_COL], errors="coerce")
    material = pd.to_datetime(df[MATERIAL_COL], errors="coerce")
    return due, submat, material


def style_axes(ax):
    ax.set_facecolor(SURFACE)
    for spine in ("top", "right", "left"):
        ax.spines[spine].set_visible(False)
    ax.spines["bottom"].set_color(BASELINE)
    ax.tick_params(colors=INK_MUTED, labelsize=9)
    ax.yaxis.grid(True, color=GRIDLINE, linewidth=0.8)
    ax.set_axisbelow(True)


def plot_due_date_timeline(due, output_path):
    due = due.dropna()
    days = due.dt.floor("D")
    counts = days.value_counts().sort_index()

    fig, ax = plt.subplots(figsize=(12, 4.5))
    fig.patch.set_facecolor(SURFACE)
    style_axes(ax)

    # Wilkinson-style dot/tick stack: each record is one tick, stacked
    # vertically when several fall on the same day, so the stack height
    # itself reads as the distribution.
    for day, cnt in counts.items():
        ax.vlines(
            x=[day] * cnt,
            ymin=np.arange(cnt),
            ymax=np.arange(cnt) + 0.7,
            color=SERIES_BLUE,
            linewidth=2.2,
        )

    ax.set_ylim(0, counts.max() + 1)
    ax.set_ylabel("건수 (같은 날짜 누적)", color=INK_SECONDARY, fontsize=10)
    ax.set_title(
        f"납기일자 분포 (2026년07월 시트, n={len(due)})",
        color=INK_PRIMARY,
        fontsize=13,
        loc="left",
        pad=12,
    )
    fig.autofmt_xdate(rotation=30)
    fig.tight_layout()
    fig.savefig(output_path, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    print(f"[저장] {output_path}")


def plot_lead_time_distribution(diff_days, title, output_path):
    diff_days = diff_days.dropna()
    lo, hi = int(diff_days.min()), int(diff_days.max())
    bins = np.arange(lo - 0.5, hi + 1.5, 1)

    fig, (ax_hist, ax_rug) = plt.subplots(
        2, 1, figsize=(9, 5), sharex=True, gridspec_kw={"height_ratios": [5, 1], "hspace": 0.08}
    )
    fig.patch.set_facecolor(SURFACE)
    style_axes(ax_hist)

    ax_hist.hist(diff_days, bins=bins, color=SERIES_BLUE, edgecolor=SURFACE, linewidth=0.5)
    median = diff_days.median()
    ax_hist.axvline(median, color=INK_SECONDARY, linewidth=1.2, linestyle="--")
    ax_hist.text(
        median,
        ax_hist.get_ylim()[1] * 0.95,
        f" 중앙값 {median:.0f}일",
        color=INK_SECONDARY,
        fontsize=9,
        va="top",
    )
    ax_hist.set_ylabel("건수", color=INK_SECONDARY, fontsize=10)
    ax_hist.set_title(f"{title} (n={len(diff_days)})", color=INK_PRIMARY, fontsize=13, loc="left", pad=12)

    # rug of individual data points along the bottom
    ax_rug.set_facecolor(SURFACE)
    for spine in ax_rug.spines.values():
        spine.set_visible(False)
    ax_rug.set_yticks([])
    ax_rug.tick_params(colors=INK_MUTED, labelsize=9)
    ax_rug.vlines(diff_days, 0, 1, color=SERIES_BLUE, linewidth=0.8, alpha=0.5)
    ax_rug.set_xlabel("입고예정일 - 납기일 (일)", color=INK_SECONDARY, fontsize=10)

    fig.savefig(output_path, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    print(f"[저장] {output_path}")


def main():
    parser = argparse.ArgumentParser(description="납기일 타임라인 및 입고예정일 리드타임 분포 시각화")
    parser.add_argument("--excel", default=DEFAULT_EXCEL, help="입력 엑셀 파일 경로")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="PNG 저장 폴더")
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    due, submat, material = load_data(args.excel)

    plot_due_date_timeline(due, os.path.join(args.output_dir, "due_date_timeline.png"))

    submat_diff = (submat - due).dt.days
    plot_lead_time_distribution(
        submat_diff,
        "부자재입고예정일 - 납기일 분포",
        os.path.join(args.output_dir, "submaterial_lead_time_dist.png"),
    )

    material_diff = (material - due).dt.days
    plot_lead_time_distribution(
        material_diff,
        "원료입고예정일 - 납기일 분포",
        os.path.join(args.output_dir, "material_lead_time_dist.png"),
    )


if __name__ == "__main__":
    main()
