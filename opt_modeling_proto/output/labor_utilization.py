# -*- coding: utf-8 -*-
"""
output/labor_utilization.py

line_schedule.csv + daily_workforce.csv를 읽어서 날짜별로
  실노동비율 = 실제 투입 인원-시간 / 가용 인원-시간
        = sum(라인별 produce 시간 x 그 라인에서 그 제품 생산에 필요한 인원수)
          / (그날 출근 인원수 x 정규 8시간 + 야근 인원수 x 야근시간)
를 계산한다. 야근 인원-시간은 daily_workforce.csv의 overtime_17_18 +
overtime_18_19(슬롯당 1시간이므로 인원수 = 인원-시간)로 구한다.

주의: setup 슬롯은 별도 인력이 처리해서 스케줄링 모델상 그 시간 동안
라인 작업자 수요가 0이므로(schedule_optimizer.py 주석 참고), 분자
계산에서 setup은 빼고 produce 시간만 카운트한다.

사용법 (이 파일이 있는 output/ 디렉터리에서, 또는 어디서든 --dir로 지정):
    python labor_utilization.py
    python labor_utilization.py --data ../real_data.json   # 내장 예시 대신 실제 데이터의 workers 값 사용시
    python labor_utilization.py --real-plan               # plan_from_orders.py로 만든 output/real_plan/ 결과 분석시
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

# opt_modeling_proto/ (이 파일의 상위 폴더)를 import 경로에 추가해서
# scheduling 패키지 및 plan_from_orders.py를 가져올 수 있게 한다.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scheduling.models import SLOTS_PER_DAY, OVERTIME_LOCAL_SLOTS
from _data_source import add_source_args, build_workers_lookup, resolve_source

REGULAR_HOURS_PER_DAY = SLOTS_PER_DAY - len(OVERTIME_LOCAL_SLOTS)  # 10 - 2 = 8


def main():
    parser = argparse.ArgumentParser(description="날짜별 실노동비율(실제 투입 인원-시간 / 가용 인원-시간) 계산")
    parser.add_argument("--dir", default=None,
                         help="line_schedule.csv / daily_workforce.csv가 있는 디렉터리 "
                              "(기본: --real-plan이면 output/real_plan, 아니면 output/)")
    add_source_args(parser)
    parser.add_argument("--out", default=None, help="결과 CSV 저장 경로 (기본: <dir>/labor_utilization.csv)")
    args = parser.parse_args()

    lines, orders, default_dir = resolve_source(args, os.path.dirname(os.path.abspath(__file__)))
    args.dir = args.dir or default_dir
    workers_lookup = build_workers_lookup(lines, orders)

    schedule_path = os.path.join(args.dir, "line_schedule.csv")
    workforce_path = os.path.join(args.dir, "daily_workforce.csv")
    out_path = args.out or os.path.join(args.dir, "labor_utilization.csv")

    schedule = pd.read_csv(schedule_path)
    workforce_df = pd.read_csv(workforce_path).set_index("day")
    workforce = workforce_df["workforce"]
    overtime_person_hours = workforce_df["overtime_17_18"] + workforce_df["overtime_18_19"]

    produce = schedule[schedule["activity"] == "produce"].copy()

    # order_id 기준으로 workers를 찾는다(product_id는 여러 주문이 공유할
    # 수 있는 "이름"일 뿐이라 식별자로 쓰면 안 됨 - output/_data_source.py
    # 참고).
    combos = set(map(tuple, produce[["line_id", "order_id"]].drop_duplicates().values))
    missing = sorted(combos - set(workers_lookup))
    if missing:
        print(f"[경고] workers 정보가 없는 (line_id, order_id) 조합 {len(missing)}건은 0명으로 취급합니다: {missing}",
              file=sys.stderr)

    produce["required_workers"] = [
        workers_lookup.get((lid, oid), 0) for lid, oid in zip(produce["line_id"], produce["order_id"])
    ]
    produce["person_hours"] = produce["required_workers"]  # 슬롯 1개 = 1시간 -> 행 1개당 1인시(person-hour)

    daily_required = produce.groupby("day")["person_hours"].sum()

    result = pd.DataFrame({"day": workforce.index})
    result["workforce"] = result["day"].map(workforce)
    result["overtime_person_hours"] = result["day"].map(overtime_person_hours)
    result["required_person_hours"] = result["day"].map(daily_required).fillna(0.0)
    result["available_person_hours"] = (
        result["workforce"] * REGULAR_HOURS_PER_DAY + result["overtime_person_hours"]
    )
    result["labor_utilization_ratio"] = (
        result["required_person_hours"] / result["available_person_hours"]
    ).round(4)

    result.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(result.to_string(index=False))
    print(f"\n[저장 완료] {out_path}")


if __name__ == "__main__":
    main()
