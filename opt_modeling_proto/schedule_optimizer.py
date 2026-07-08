# -*- coding: utf-8 -*-
"""
schedule_optimizer.py

30일 생산 스케줄링 최적화 프로토타입의 실행 진입점 (Google OR-Tools
CP-SAT 사용). 실제 모델/리포트/예시데이터 구현은 scheduling/ 패키지로
나눠져 있고, 이 파일은 CLI 인자 처리 + 실행 순서 조립만 담당하는 얇은
스크립트다.

  - scheduling/models.py       : 시간 구조 상수, Line/Order/ScheduleConfig/ScheduleResult
  - scheduling/solver.py       : CP-SAT 모델 구성 + 2단계(비용->연속성) 풀이
  - scheduling/report.py       : 콘솔 리포트, CSV 저장, 간트 차트 PNG
  - scheduling/example_data.py : 내장 예시 데이터, JSON 데이터 로더

문제 요약 (사용자가 준 스펙):
  - 30일 계획기간, 하루는 08-12시(4h) + 13-17시(4h) = 정규 8시간,
    17-19시(2h)는 잔업(overtime) 가능. 12-13시 점심시간은 작업 불가.
    시간은 1시간 단위로 이산화(하루 10개 슬롯).
  - 제품군은 마스크/용기/튜브 3종, 각 제품군에 여러 라인 타입이 있고
    같은 라인 타입이라도 동일 물리 라인이 여러 대일 수 있음(각각 독립
    라인으로 취급).
  - 각 주문(order)은 제품ID/제품군/필요수량/마감일/라인별 생산속도
    (호환 안 되면 0)/라인별 필요인원을 가짐.
  - 생산은 완전 선형(시간당 생산속도 x 생산시간 = 생산량, 셋업/기동손실 없음).
  - 라인이 제품을 바꿀 때는 반드시 1시간짜리 셋업이 필요(같은 제품 연속이면
    불필요). 단, 하루 일과 "안에서" 제품이 바뀔 때만 셋업 슬롯이 필요하고,
    날짜가 바뀌는 시점(전날 마지막 슬롯 -> 다음날 첫 슬롯)에 제품이 바뀌는
    건 전날 밤에 직원이 미리 셋업해둘 수 있다고 보고 셋업 슬롯이 필요 없다
    (사용자 확인 반영, scheduling/solver.py의 전이 제약 주석 참고). 또한
    하루 일과 중 셋업이 발생하면 그건 별도 인력이 처리하므로, 그 시간 동안
    해당 라인에 필요한 '라인 작업자(line worker)' 인원은 0이다(=셋업은
    일일 고용 인원 계산에 반영되지 않음).
  - 인원은 '일 단위 고용'. 하루 중 몇 시간을 일하든 하루치 정액 임금을
    받으며, 그날 고용해야 하는 인원수는 그날 어느 시점에서든 동시에
    필요한 최대 인원수 이상이어야 함.
  - 이미 고용된 인원이 잔업을 할 수 있고(하루 최대 2시간, 즉 17-19시
    슬롯 2개), 잔업 1시간은 정규 시급의 1.5배.
  - 목적함수: (일일 정액임금의 합) + (잔업수당의 합) 최소화, 모든 주문을
    마감일 전까지 완료.
  - (추가) 위 인건비가 동일하다면, 그 안에서 라인이 대기<->생산을
    왔다갔다하는 횟수와 셋업 횟수를 최소화해서 "사람이 한 라인에 최대한
    오래 붙어있는" 스케줄을 우선한다. 1단계에서 찾은 최소 비용을 그대로
    고정한 채 2단계로 다시 푸는 lexicographic(사전식) 최적화로 구현했다
    (scheduling/solver.py의 "2단계 풀이" 부분 참고). --no-continuity로 끌 수 있다.

이 스크립트에 내장된 예시 데이터(scheduling.example_data.build_example_instance)로
바로 실행해 볼 수 있고, --data 옵션으로 JSON 파일을 넣으면 실제 라인/주문
데이터로도 돌릴 수 있다.
"""

from __future__ import annotations

import argparse
import io
import os
import sys

# Windows 콘솔(cp949 등) 기본 인코딩에서는 한글 출력이 깨지므로, 표준 출력/
# 에러 스트림을 UTF-8로 강제로 다시 감싼다. (파일 저장은 to_csv에서 별도로
# utf-8-sig를 지정하므로 이 처리와 무관하게 항상 정상 저장된다.)
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from scheduling.example_data import build_example_instance, load_data_from_json
from scheduling.models import ScheduleConfig
from scheduling.report import plot_gantt, print_report, save_outputs
from scheduling.solver import build_and_solve


def main():
    parser = argparse.ArgumentParser(description="30일 생산 스케줄링 최적화 (CP-SAT)")
    parser.add_argument("--data", default=None, help="라인/주문 데이터를 담은 JSON 파일 경로 (없으면 내장 예시 데이터 사용)")
    parser.add_argument("--horizon-days", type=int, default=30)
    parser.add_argument("--daily-wage", type=float, default=200_000, help="1인 1일 고용 정액임금")
    parser.add_argument("--hourly-wage", type=float, default=None, help="잔업수당 계산용 시급 (미지정시 daily-wage/8)")
    parser.add_argument("--overtime-multiplier", type=float, default=1.5)
    parser.add_argument(
        "--backlog-cost", type=float, default=100.0,
        help="ASAP 주문(마감일 없음)의 하루당 미생산 1개당 지연비용 기본값(원). 주문별로 override 가능.",
    )
    parser.add_argument("--time-limit", type=float, default=60.0, help="1단계(인건비 최소화) CP-SAT 탐색 제한시간(초)")
    parser.add_argument(
        "--secondary-time-limit", type=float, default=60.0,
        help="2단계(연속성 최적화) CP-SAT 탐색 제한시간(초)",
    )
    parser.add_argument(
        "--no-continuity", action="store_true",
        help="2단계 연속성 최적화를 생략하고 1단계(순수 비용 최소화) 결과만 사용",
    )
    parser.add_argument("--no-plot", action="store_true", help="간트 차트 PNG 생성 생략")
    parser.add_argument(
        "--output-dir",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "output"),
    )
    args = parser.parse_args()

    if args.data:
        lines, orders = load_data_from_json(args.data)
        print(f"[정보] JSON 데이터 로드: {args.data} (라인 {len(lines)}개, 주문 {len(orders)}개)")
    else:
        lines, orders = build_example_instance()
        print(f"[정보] 내장 예시 데이터 사용 (라인 {len(lines)}개, 주문 {len(orders)}개)")

    config = ScheduleConfig(
        horizon_days=args.horizon_days,
        daily_wage=args.daily_wage,
        hourly_wage=args.hourly_wage,
        overtime_multiplier=args.overtime_multiplier,
        time_limit_seconds=args.time_limit,
        secondary_time_limit_seconds=args.secondary_time_limit,
        optimize_continuity=not args.no_continuity,
        default_backlog_cost_per_unit_per_day=args.backlog_cost,
    )
    print(
        f"[정보] 임금 설정: 일급 {config.daily_wage:,.0f} / 시급(잔업기준) "
        f"{config.resolved_hourly_wage():,.0f} / 잔업배수 {config.overtime_multiplier}x"
    )

    result = build_and_solve(lines, orders, config)
    print_report(result, orders)

    os.makedirs(args.output_dir, exist_ok=True)
    save_outputs(result, orders, args.output_dir)
    if not args.no_plot:
        plot_gantt(result, orders, config, args.output_dir)


if __name__ == "__main__":
    main()
