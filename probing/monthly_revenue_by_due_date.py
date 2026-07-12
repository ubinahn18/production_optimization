# -*- coding: utf-8 -*-
"""
monthly_revenue_by_due_date.py

"수주진행현황" 엑셀의 '2026년07월' 시트(전체 미출고 수주 목록, 납기가
2026-07~10월에 걸쳐 있음)를 사용해서, 기준일 8/15, 9/15, 10/15 각각에 대해
"전달 15일부터 해당 달 15일까지" 납기인 건들의 수주금액 합계를 구한다.
경계일(각 달 15일)은 "전달" 쪽 구간에 포함시킨다: 구간 시작일(전달 15일)은
제외하고 구간 종료일(해당 달 15일)은 포함한다 (due > start, due <= end).
예) 기준일 2026-08-15 -> 2026-07-15 초과 ~ 2026-08-15 이하 납기 건 합산.
"""

import argparse
import io
import os
import sys

import pandas as pd

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

DEFAULT_EXCEL = r"C:\Users\USER\production_opt\수주진행현황(simulation_DATA_2).xlsx"
SHEET_NAME = "2026년07월"

DUE_COL = "납기\n(인폼 기준)"
AMOUNT_COL = "수주금액"

REFERENCE_DATES = ["2026-08-15", "2026-09-15", "2026-10-15"]


def load_data(excel_path):
    df = pd.read_excel(excel_path, sheet_name=SHEET_NAME, header=1)
    df[DUE_COL] = pd.to_datetime(df[DUE_COL], errors="coerce")
    return df


def summarize(df, reference_dates):
    rows = []
    for ref_str in reference_dates:
        end = pd.Timestamp(ref_str)
        start = end - pd.DateOffset(months=1)
        mask = (df[DUE_COL] > start) & (df[DUE_COL] <= end)
        subset = df.loc[mask]
        rows.append(
            {
                "기준일": end.date(),
                "기간": f"{start.date()} 초과 ~ {end.date()} 이하",
                "건수": len(subset),
                "수주금액합계": subset[AMOUNT_COL].sum(),
            }
        )
    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser(description="기준일별(전달15일~해당달15일) 수주금액 합계 집계")
    parser.add_argument("--excel", default=DEFAULT_EXCEL, help="입력 엑셀 파일 경로")
    parser.add_argument(
        "--reference-dates",
        nargs="+",
        default=REFERENCE_DATES,
        help="기준일 목록 (예: 2026-08-15 2026-09-15 2026-10-15)",
    )
    args = parser.parse_args()

    df = load_data(args.excel)
    result = summarize(df, args.reference_dates)

    with pd.option_context("display.float_format", lambda v: f"{v:,.0f}"):
        print(result.to_string(index=False))


if __name__ == "__main__":
    main()
