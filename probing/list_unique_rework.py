# -*- coding: utf-8 -*-
"""
list_unique_rework.py

records.csv에서 '리웍' 컬럼의 원본(raw) 텍스트값을 건수와 함께 전부 뽑아서
콘솔에 출력하고 CSV로도 저장한다. worker_per_process.py가 만든
records.csv를 입력으로 사용한다.
"""

import argparse
import io
import os
import sys

import pandas as pd

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

DEFAULT_RECORDS_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "records.csv")
DEFAULT_OUTPUT_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "unique_rework_raw.csv")


def main():
    parser = argparse.ArgumentParser(description="records.csv의 리웍 원본 텍스트 unique 목록 출력")
    parser.add_argument("--records-csv", default=DEFAULT_RECORDS_CSV, help="worker_per_process.py가 만든 records.csv 경로")
    parser.add_argument("--output-csv", default=DEFAULT_OUTPUT_CSV, help="결과를 저장할 CSV 경로")
    args = parser.parse_args()

    df = pd.read_csv(args.records_csv)
    counts = df["rework_raw"].value_counts()

    print(f"[정보] 고유 리웍 텍스트 {len(counts)}개 (전체 {len(df)}건)\n")
    for text, cnt in counts.items():
        print(f"{cnt:4d}건  {text!r}")

    counts.rename_axis("rework_raw").reset_index(name="count").to_csv(
        args.output_csv, index=False, encoding="utf-8-sig"
    )
    print(f"\n[정보] CSV 저장: {args.output_csv}")


if __name__ == "__main__":
    main()
