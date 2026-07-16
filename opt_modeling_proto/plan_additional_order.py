# -*- coding: utf-8 -*-
"""
plan_additional_order.py

이미 plan_from_orders.py로 30일 계획을 짜서 output/real_plan/에
line_schedule.csv 등이 저장돼 있는 상태에서, 새로운 수주 1건이
--adding-date에 들어왔다고 가정하고 그 주문을 기존 계획에 "끼워넣는다".
plan_from_orders.py/scheduling/solver.py 등 기존 스크립트는 전혀
건드리지 않고, 이 파일 하나로 완결되는 별도의(더 단순한) CP-SAT
서브모델을 새로 짠다.

=== 알고리즘 (3단계, 뒤로 갈수록 기존 계획을 더 많이 건드림) ===

adding_day(=--adding-date를 reference_date 기준 day index로 환산한 값)
이후의 슬롯만 손댈 수 있다(그 이전은 이미 지나간 과거로 취급). 그 구간
안에서 각 물리 라인의 슬롯은 세 종류로 나뉜다:
  - 마감일 있는(ASAP 아닌) 주문이 차지한 슬롯: 항상 동결(절대 안 건드림) -
    이미 납기를 맞추기로 확정된 생산이라서.
  - ASAP 주문이 차지한 슬롯: 1차 시도에서는 동결, 2차 시도에서는 재배정
    대상(그 주문의 backlog가 늘어나는 대가로 새 주문에 자리를 내줄 수 있음).
  - idle 슬롯: 항상 재배정 대상.

  [0단계] 상한선 체크: ASAP 슬롯까지 전부 반납하고 잔업 포함 그 구간을
          꽉 채워도(설비 전환 손실은 무시한 순수 이론값) 신규 주문
          수량을 못 채우면, 여기서 바로 실패 처리하고 "그래도 최대
          이만큼은 가능하다"는 이론적 상한만 보여준다(CP-SAT 자체를
          돌리지 않음 - 물리적으로 불가능한 걸 오래 탐색할 필요가 없음).
  [1단계] idle 슬롯만으로 이론상 가능한지 먼저 계산해보고(같은 상한선
          체크, idle만 대상), 가능해 보이면 idle 슬롯만 변수로 하는
          CP-SAT을 실제로 풀어본다(ASAP 주문 스케줄은 전혀 안 건드림 -
          가장 덜 disruptive).
  [2단계] 1단계가 안 되면(이론상 부족하거나 실제 풀이가 infeasible이면)
          ASAP 슬롯까지 재배정 대상에 포함해서 다시 푼다 - 신규 주문이
          우선시되고 밀려난 ASAP 주문은 backlog 비용으로 처리된다.
  실패하면: 마지막으로 "수량 제약을 만족(>=quantity)" 대신 "최대한
          많이 생산"으로 목적함수를 바꿔서 한 번 더 풀어 실제 달성
          가능한 최대치를 계산해 보여준다.

=== 이 서브모델과 scheduling/solver.py의 차이(의도적으로 단순화한 부분) ===

solver.py는 "몇 대가 무슨 상태인가"를 풀 단위 집계 변수로 다뤄서(동일
라인 그룹의 symmetry 문제를 피함) 슬롯 단위로 완전히 유연하게 최적화한다.
이 스크립트는 그 정도로 정교할 필요가 없다(건드리는 슬롯 수 자체가
훨씬 적고, 대부분의 배정은 이미 확정돼 있어 symmetry 문제가 크지
않음) - 대신 물리 라인 하나하나를 직접 다루면서 다음처럼 단순화한다:

  - 같은 물리 라인에서 하루 동안 연속된 "재배정 가능 구간"(run)은
    통째로 한 주문에만 배정한다(구간 중간에 다시 다른 주문으로 바꾸는
    건 고려 안 함) - 그 구간이 그날의 첫 슬롯부터 시작하면 셋업 없이
    바로 생산 가능(solver.py의 day-boundary 무료 전환과 동일 규칙),
    아니면 무조건 첫 슬롯 1개를 셋업으로 쓴다(직전에 동결된 슬롯이
    공교롭게 같은 제품이었을 가능성은 안전하게 무시 - 실제보다 셋업을
    한 번 더 쓰는 쪽으로만 틀릴 수 있고, 그 반대(실제로 필요한 셋업을
    빠뜨림)로는 절대 틀리지 않는다).
  - 2단계 연속성 최적화(대기<->생산 전환 최소화)는 하지 않는다 - 애초에
    건드리는 슬롯이 적어서 실익이 작고, run 단위로 통째로 배정하는
    방식 자체가 어느 정도 연속성을 보장한다.
  - ASAP 주문의 backlog 비용은 solver.py처럼 날짜별 선형 목표진도가
    아니라 "이 구간이 끝나는 시점까지 남은 부족분 x 단가"로 단순화했다.

=== 사용법 ===
    python plan_additional_order.py --adding-date 2026-07-20

새 주문 정보(수량/납기/라인타입별 rate·인원 등)는 아래 NEW_ORDER
플레이스홀더를 직접 채워서 씀 - 엑셀에서 자동으로 읽어오지 않는다.
"""

from __future__ import annotations

import argparse
import io
import os
import sys
from dataclasses import dataclass
from datetime import date, timedelta

# Windows 콘솔(cp949 등) 기본 인코딩에서는 한글 출력이 깨지므로 UTF-8로 강제.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)

import pandas as pd
from ortools.sat.python import cp_model

import plan_from_orders as pfo
from scheduling.models import MONEY_SCALE, OVERTIME_LOCAL_SLOTS, SLOT_LABELS, SLOTS_PER_DAY
from scheduling.pooling import build_line_pools

# ======================================================================
# 플레이스홀더: 새로 들어온 수주 정보. 여기 직접 채워서 쓴다(엑셀 자동
# 반영 없음). rate_by_line_type/workers_by_line_type은
# plan_from_orders.py의 CATEGORY_LINE_SPECS와 같은 형식으로, 이 주문을
# 만들 수 있는 라인타입마다 시간당 생산량/필요인원을 적는다.
# ======================================================================
NEW_ORDER_ID = "PLACEHOLDER_ORDER_ID"
NEW_ORDER_PRODUCT_ID = "PLACEHOLDER_PRODUCT_ID"
NEW_ORDER_PRODUCT_NAME = "PLACEHOLDER_PRODUCT_NAME"
NEW_ORDER_VENDOR = "PLACEHOLDER_VENDOR"
NEW_ORDER_CATEGORY = "PLACEHOLDER_CATEGORY"  # 참고/표시용. rate/workers는 라인타입 기준으로 아래 직접 채우므로 이 값 자체가 스케줄링에 쓰이진 않음.
NEW_ORDER_QUANTITY = 80_000  # 필요 생산수량 - 직접 수정
NEW_ORDER_DEADLINE_DATE: date | None = date(2026, 7, 18)  # 예: date(2026, 8, 10). None이면 ASAP(하드 마감 없음, backlog 비용으로만 처리).
NEW_ORDER_EARLIEST_START_DATE: date | None = date(2026, 7, 15)  # 원료 등 때문에 이 날짜 전엔 생산 불가. None이면 제약 없음(즉시 가능).
NEW_ORDER_RATE_BY_LINE_TYPE: dict[str, float] = {
    "셀라인": 2220,
    "단발": 1080
}
NEW_ORDER_WORKERS_BY_LINE_TYPE: dict[str, int] = {
    "셀라인": 8,
    "단발": 6
}
NEW_ORDER_BACKLOG_COST_PER_UNIT_PER_DAY: float | None = None  # ASAP일 때만 의미 있음. None이면 --backlog-cost 기본값 사용.


@dataclass
class Candidate:
    """이 서브모델이 스케줄링 대상으로 고려하는 "제품" 하나(신규 주문
    또는 재배정 대상이 된 기존 ASAP 주문). order_id가 NEW_ORDER_ID이면
    신규 주문."""

    order_id: str
    product_id: str
    rate_by_type: dict[str, float]
    workers_by_type: dict[str, int]
    is_new: bool
    remaining_qty: float  # 신규 주문이면 quantity 그대로, 기존 ASAP면 quantity - adding_day 이전 기생산량
    backlog_rate: float
    earliest_day: int  # 이 구간에서 생산 가능한 최초 day(1-indexed, adding_day 이상으로 이미 클램프됨)
    deadline_day: int | None  # None이면 ASAP(하드 마감 없음)


@dataclass(eq=False)
class Run:
    """한 물리 라인에서, 특정 하루 안에 이어진 "재배정 가능한" 슬롯
    구간(idle, tier2에서는 ASAP 슬롯도 포함) 하나. eq=False로 둬서
    (기본 object identity 기반) 해시 가능하게 함 - solve_tier가 Run
    인스턴스를 dict 키로 쓴다.

    attached_to: 정규 시간 run과 잔업(OVERTIME_LOCAL_SLOTS) 구간이 연속으로
    이어져 있으면, detect_runs가 정규 부분(attached_to=None)과 잔업 부분을
    별도 Run으로 쪼갠다. 잔업 run은 attached_to가 가리키는 정규 run과
    "같은 후보"에게 배정될 때만 덤으로 선택할 수 있고(정규는 안 하고
    잔업만 하는 건 말이 안 되므로), 선택되면 그 자체로는 셋업이 전혀
    필요 없다(정규 run에서 바로 이어지는 생산이라서) - production_slots()가
    이 두 가지를 반영한다. solve_tier가 use_vars[ot] <= use_vars[reg]
    제약을 추가로 건다."""

    line_id: str
    line_type: str
    day: int
    slot_indices: list[int]  # 전역 슬롯 인덱스(0-indexed, t = (day-1)*SLOTS_PER_DAY + local)
    starts_at_day_start: bool
    attached_to: "Run | None" = None

    @property
    def length(self) -> int:
        return len(self.slot_indices)

    def production_slots(self) -> int:
        """이 run을 어떤 후보에게 배정했을 때, 그중 실제로 "생산"에 쓰이는
        슬롯 수(나머지는 셋업 1칸). attached_to가 있는(잔업 연장) run은
        정규 run에서 셋업 없이 바로 이어지는 것이므로 전체가 생산이다."""
        if self.attached_to is not None:
            return self.length
        return self.length - (0 if self.starts_at_day_start else 1)


# ----------------------------------------------------------------------
# 데이터 로딩
# ----------------------------------------------------------------------
def load_existing_state(dir_: str):
    """plan_from_orders.py가 이미 저장해둔 30일 계획을 그대로 읽어온다.
    line_schedule.csv: (line_id, day, slot) 단위로 activity(idle/setup/
    produce)/product_id/order_id를 담은 표 - 이 전체가 "지금까지 확정된
    스케줄"이고, 이 스크립트는 adding_day 이후 구간만 이 표를 바탕으로
    고쳐 쓴다. daily_workforce.csv: 날짜별 필요 인원(잔업 포함)인데,
    write_outputs에서 adding_day 이후 구간만 재계산해서 덮어쓴다."""
    schedule_df = pd.read_csv(os.path.join(dir_, "line_schedule.csv"))
    workforce_df = pd.read_csv(os.path.join(dir_, "daily_workforce.csv"))
    return schedule_df, workforce_df


def build_new_order_candidate(reference_date: date, adding_day: int, horizon_days: int, default_backlog_cost: float) -> Candidate:
    """스크립트 상단의 NEW_ORDER_* 플레이스홀더(날짜/수량/라인타입별
    rate·인원)를 이 스크립트 내부에서 쓰는 Candidate 객체 하나로 변환한다.

    날짜 -> day index 변환: plan_from_orders.py와 동일하게 reference_date를
    1일차로 삼아서, (날짜 - reference_date).days + 1로 계산한다(1-indexed).
    NEW_ORDER_DEADLINE_DATE가 있으면 반드시 [adding_day, horizon_days]
    범위 안에 있어야 함(범위 밖이면 애초에 이 계획기간 안에서 만족시킬
    방법이 없으므로 여기서 바로 에러를 내서 CP-SAT을 괜히 돌리지 않게 함).
    NEW_ORDER_EARLIEST_START_DATE는 adding_day보다 이르면 의미가 없으므로
    max(adding_day, ...)로 클램프한다(신규 수주 반영일 이전 슬롯은 애초에
    이 스크립트가 손대지 않는 "과거" 구간이라서)."""
    if not NEW_ORDER_RATE_BY_LINE_TYPE:
        raise ValueError("NEW_ORDER_RATE_BY_LINE_TYPE이 비어 있습니다 - 스크립트 상단 플레이스홀더를 채워주세요.")
    deadline_day = None
    if NEW_ORDER_DEADLINE_DATE is not None:
        deadline_day = (NEW_ORDER_DEADLINE_DATE - reference_date).days + 1
        if not (adding_day <= deadline_day <= horizon_days):
            raise ValueError(
                f"신규 주문 납기(day {deadline_day})가 [adding_day={adding_day}, horizon={horizon_days}] 범위 밖입니다."
            )
    earliest_day = adding_day
    if NEW_ORDER_EARLIEST_START_DATE is not None:
        earliest_day = max(adding_day, (NEW_ORDER_EARLIEST_START_DATE - reference_date).days + 1)
    return Candidate(
        order_id=NEW_ORDER_ID,
        product_id=NEW_ORDER_PRODUCT_ID,
        rate_by_type=dict(NEW_ORDER_RATE_BY_LINE_TYPE),
        workers_by_type=dict(NEW_ORDER_WORKERS_BY_LINE_TYPE),
        is_new=True,
        remaining_qty=NEW_ORDER_QUANTITY,
        backlog_rate=NEW_ORDER_BACKLOG_COST_PER_UNIT_PER_DAY or default_backlog_cost,
        earliest_day=earliest_day,
        deadline_day=deadline_day,
    )


def build_asap_candidates(
    schedule_df: pd.DataFrame,
    order_by_id: dict,
    adding_day: int,
    horizon_days: int,
    default_backlog_cost: float,
) -> dict[str, Candidate]:
    """adding_day 이후 구간에 실제로 슬롯을 차지하고 있는 ASAP 주문들만
    후보로 만든다(그 외 ASAP 주문은 이 구간과 무관하므로 대상에서 뺌).
    remaining_qty는 전체 quantity에서 adding_day 이전(과거, 이미 확정된)
    생산량을 뺀 값."""
    candidates: dict[str, Candidate] = {}
    produce_rows = schedule_df[schedule_df["activity"] == "produce"]
    for oid, group in produce_rows.groupby("order_id"):
        o = order_by_id.get(oid)
        if o is None or not o.is_asap():
            continue
        in_window = group[group["day"] >= adding_day]
        if in_window.empty:
            continue
        already_produced = 0.0
        for row in group[group["day"] < adding_day].itertuples(index=False):
            already_produced += o.rate.get(_line_type_of(row.line_id), 0.0)
        remaining = max(0.0, o.quantity - already_produced)
        candidates[oid] = Candidate(
            order_id=oid,
            product_id=o.product_id,
            rate_by_type=dict(o.rate),
            workers_by_type={k: int(v) for k, v in o.workers.items()},
            is_new=False,
            remaining_qty=remaining,
            backlog_rate=o.backlog_cost_per_unit_per_day or default_backlog_cost,
            earliest_day=adding_day,
            deadline_day=None,
        )
    return candidates


# _line_type_of는 main()에서 실제 매핑을 채운 뒤 모듈 전역으로 바꿔치기한다
# (build_asap_candidates가 위에서 참조하므로 이런 순서가 됨).
def _line_type_of(line_id: str) -> str:  # pragma: no cover - main()에서 재정의됨
    raise RuntimeError("_line_type_of가 초기화되지 않았습니다")


# ----------------------------------------------------------------------
# Run(재배정 가능 구간) 탐지
# ----------------------------------------------------------------------
def detect_runs(
    schedule_df: pd.DataFrame,
    adding_day: int,
    horizon_days: int,
    closed_days: frozenset[int],
    order_by_id: dict,
    include_asap: bool,
) -> tuple[list[Run], dict[tuple[str, int, str], tuple[str, str]]]:
    """물리 라인 x 날짜별로, adding_day 이후 구간에서 재배정 가능한
    슬롯들이 이어진 구간(run)을 찾는다. include_asap=False면 idle
    슬롯만, True면 idle + ASAP 주문이 차지한 슬롯도 재배정 가능으로
    본다. 마감일 있는(ASAP 아닌) 주문 슬롯과 휴무일은 항상 제외.

    두 번째 반환값(synthetic_setups)은 (line_id, day, slot_label) ->
    (product_id, order_id, trigger_run) 매핑이다. "동결된 produce로 셋업
    없이 이어지던 idle 다리"의 맨 마지막 한 칸을 그 동결 주문의 셋업
    후보로 예약해두되(아래 설명 참고), 실제로 그 앞쪽(트리거) run이
    어떤 후보에게 배정됐을 때만 write_outputs가 이 칸을 setup으로
    채워 넣어야 한다 - 아무도 그 앞을 안 건드렸으면 원래대로 idle로
    남아야 한다(예: 다리가 하루의 첫 슬롯부터 시작하거나 1칸짜리라서
    애초에 아무 candidate도 그 앞부분을 쓸 수 없는 경우, 또는 CP-SAT이
    그냥 그 run을 선택하지 않은 경우 - 이런데도 무조건 setup으로
    표시하면 실제로 일어나지 않은 셋업을 지어내는 셈이 된다)."""
    slot_order = {s: i for i, s in enumerate(SLOT_LABELS)}
    runs: list[Run] = []
    synthetic_setups: dict[tuple[str, int, str], tuple[str, str, Run]] = {}

    window = schedule_df[schedule_df["day"] >= adding_day]
    for line_id, line_group in window.groupby("line_id"):
        by_day: dict[int, dict[int, tuple[str, str, str]]] = {}
        for row in line_group.itertuples(index=False):
            by_day.setdefault(row.day, {})[slot_order[row.slot]] = (
                row.activity, row.product_id, "" if pd.isna(row.order_id) else row.order_id
            )

        for day in range(adding_day, horizon_days + 1):
            if day in closed_days:
                continue
            slots = by_day.get(day, {})
            cell_at = [slots.get(local, ("idle", "", "")) for local in range(SLOTS_PER_DAY)]

            reassignable = [False] * SLOTS_PER_DAY
            for local in range(SLOTS_PER_DAY):
                activity, _product, oid = cell_at[local]
                if activity == "idle":
                    reassignable[local] = True
                    continue
                o = order_by_id.get(oid)
                if include_asap and o is not None and o.is_asap():
                    reassignable[local] = True

            # 동결된(재배정 대상 아닌) produce 슬롯으로 셋업 없이 이어지는
            # idle 구간은, 사실 완전히 논 게 아니라 그 주문 설정을 유지한
            # 채 대기(idle_nf_of)한 것이었다. 이 다리의 슬롯들은 그 자체로는
            # 그 주문의 생산량에 전혀 기여하지 않으므로(idle은 idle),
            # 다리 전체를 막을 필요는 없고 - 맨 마지막 한 칸만 "그 주문의
            # 셋업 후보"로 잠가서(reassignable=False) 다른 candidate에게
            # 단독으로 새어나가지 않게만 하고, 나머지 앞쪽 칸들은 안전하게
            # 다른 주문에 내줄 수 있다. 실제로 setup 라벨을 붙일지는 아래
            # 트리거 run 매칭 단계에서 결정한다.
            pending_reservations: list[tuple[int, str, str]] = []  # (prev_local, product, oid)
            for local in range(SLOTS_PER_DAY):
                activity, product, oid = cell_at[local]
                if activity != "produce" or reassignable[local]:
                    continue  # 재배정 대상인(동결 아닌) produce는 예약할 다리가 없음
                prev = local - 1
                if prev >= 0 and cell_at[prev][0] == "idle" and reassignable[prev]:
                    reassignable[prev] = False
                    pending_reservations.append((prev, product, oid))

            free_locals = [local for local in range(SLOTS_PER_DAY) if reassignable[local]]

            # 연속 구간으로 묶기
            day_runs: list[Run] = []
            run_start = None
            prev = None
            for local in free_locals + [None]:  # None: 끝 표시용 sentinel
                if local is not None and (prev is None or local == prev + 1):
                    if run_start is None:
                        run_start = local
                else:
                    if run_start is not None:
                        locals_in_run = list(range(run_start, prev + 1))
                        t0 = (day - 1) * SLOTS_PER_DAY
                        day_runs.extend(_split_at_overtime_boundary(line_id, _line_type_of(line_id), day, locals_in_run, t0))
                    run_start = local if local is not None else None
                prev = local
            runs.extend(day_runs)

            # 예약해둔 각 자리마다, 바로 그 앞에서 끝나는 run(트리거)이
            # 실제로 있는지 찾는다. 없으면(하루 첫 슬롯이라 앞이 아예
            # 없거나, 바로 앞 슬롯도 동결이라 재배정 가능한 run이 못
            # 만들어진 경우) 이 예약은 절대 발동될 수 없으므로 아예
            # synthetic_setups에 넣지 않는다 - write_outputs는 이런 칸을
            # (원본 그대로) idle로 둔다.
            t0 = (day - 1) * SLOTS_PER_DAY
            for prev_local, product, oid in pending_reservations:
                target_last_slot = t0 + prev_local - 1
                trigger_run = next((r for r in day_runs if r.slot_indices[-1] == target_last_slot), None)
                if trigger_run is not None:
                    synthetic_setups[line_id, day, SLOT_LABELS[prev_local]] = (product, oid, trigger_run)
    return runs, synthetic_setups


def _split_at_overtime_boundary(line_id: str, line_type: str, day: int, locals_in_run: list[int], t0: int) -> list[Run]:
    """연속 재배정 가능 구간(locals_in_run) 하나가 정규 시간과 잔업
    (OVERTIME_LOCAL_SLOTS) 경계를 걸치면, "정규 run"과 그에 딸린
    "잔업 run"으로 쪼갠다. 잔업은 그날 정규 시간에 이미 일하던 걸
    연장하는 개념이라, 정규 부분 없이 잔업 슬롯만 뚝 떨어져 있으면(즉
    바로 앞 정규 슬롯이 동결돼 있어서 이 구간에 못 낀 경우) 정규 없이
    잔업만 새로 시작하는 건 말이 안 되므로 그 잔업 슬롯은 아예 재배정
    대상에서 뺀다(반환값에 안 넣음). 정규 부분이 있으면 정규 run은
    평소처럼 만들고, 잔업 부분이 있으면 attached_to=정규run으로 별도
    run을 만들어서 solve_tier가 "정규를 고른 후보만 잔업도 고를 수
    있다"는 제약을 걸 수 있게 한다."""
    ot_start = OVERTIME_LOCAL_SLOTS[0]
    reg_locals = [l for l in locals_in_run if l < ot_start]
    ot_locals = [l for l in locals_in_run if l >= ot_start]
    if not reg_locals:
        return []
    reg_run = Run(
        line_id=line_id, line_type=line_type, day=day,
        slot_indices=[t0 + l for l in reg_locals], starts_at_day_start=(reg_locals[0] == 0),
    )
    made = [reg_run]
    if ot_locals:
        made.append(Run(
            line_id=line_id, line_type=line_type, day=day,
            slot_indices=[t0 + l for l in ot_locals], starts_at_day_start=False, attached_to=reg_run,
        ))
    return made


# ----------------------------------------------------------------------
# 상한선(이론값) 체크 - CP-SAT 없이 순수 계산
# ----------------------------------------------------------------------
def ceiling_capacity(runs: list[Run], new_order: Candidate) -> float:
    """new_order 입장에서, 주어진 run 목록(이미 idle-only 또는
    idle+ASAP로 필터된 것) 전체를 최대한 이 주문에만 쓴다고 가정했을 때
    이론상 최대 생산량. 셋업 손실은 무시(실제보다 낙관적인 상한)."""
    total = 0.0
    for r in runs:
        if r.day < new_order.earliest_day:
            continue
        if new_order.deadline_day is not None and r.day > new_order.deadline_day:
            continue
        rate = new_order.rate_by_type.get(r.line_type, 0.0)
        if rate <= 0:
            continue
        total += rate * r.length  # 셋업 슬롯 소모를 무시하고 구간 전체를 생산으로 침(상한값이므로 낙관적이어도 됨)
    return total


# ----------------------------------------------------------------------
# CP-SAT 서브모델
# ----------------------------------------------------------------------
def solve_tier(
    runs: list[Run],
    candidates: list[Candidate],
    baseline_demand: dict[int, float],
    daily_wage: float,
    hourly_wage: float,
    overtime_multiplier: float,
    time_limit_seconds: float,
    require_new_order_full_qty: bool,
):
    """runs(이 tier에서 재배정 가능한 구간들)와 candidates(이 tier에서
    스케줄링 대상인 신규 주문 + 재배정된 ASAP 주문들)로 작은 CP-SAT을
    풀어서 (status_name, is_optimal_or_feasible, per-run 배정 dict,
    candidate별 생산량 dict)를 돌려준다.

    require_new_order_full_qty=True면 신규 주문 quantity를 하드 제약으로
    걸고(비용 최소화), False면 하드 제약 없이 신규 주문 생산량을 최대화
    한다(0단계/1단계가 실패했을 때 "그래도 최대 얼마나 가능한지"
    계산하는 용도)."""
    model = cp_model.CpModel()
    new_order = next(c for c in candidates if c.is_new)

    # 어느 candidate도 실제로 쓸 수 없는 run(어떤 후보도 그 line_type의
    # rate가 없거나, 신규 주문 전용 run인데 신규 주문의 earliest/deadline
    # 범위 밖이거나, 셋업 1슬롯도 못 넣을 만큼 짧은 경우)은 모델에서 아예
    # 제외해서 변수 수를 줄인다.
    usable_runs = []
    for r in runs:
        for c in candidates:
            rate = c.rate_by_type.get(r.line_type, 0.0)
            if rate <= 0:
                continue
            if r.attached_to is None and not r.starts_at_day_start and r.length <= 1:
                continue  # 셋업 1슬롯도 못 넣는 구간 - 사실상 못 씀(잔업 연장 run은 셋업이 아예 없으니 예외)
            if c.is_new and (r.day < c.earliest_day or (c.deadline_day is not None and r.day > c.deadline_day)):
                continue
            usable_runs.append(r)
            break

    use_vars: dict[tuple[int, str], cp_model.IntVar] = {}
    run_candidates: dict[int, list[str]] = {}
    for i, r in enumerate(usable_runs):
        options = []
        for c in candidates:
            rate = c.rate_by_type.get(r.line_type, 0.0)
            if rate <= 0:
                continue
            if r.attached_to is None and not r.starts_at_day_start and r.length <= 1:
                continue
            if c.is_new and (r.day < c.earliest_day or (c.deadline_day is not None and r.day > c.deadline_day)):
                continue
            options.append(c.order_id)
        if not options:
            continue
        run_candidates[i] = options
        for oid in options:
            use_vars[i, oid] = model.NewBoolVar(f"use[{i},{oid}]")
        model.Add(sum(use_vars[i, oid] for oid in options) <= 1)

    # 잔업 연장 run(attached_to가 있는 run)은 "그 정규 run을 고른 후보만
    # 덤으로 고를 수 있다"로 묶는다 - 정규는 안 하고 잔업만 하는 배정은
    # 말이 안 되므로 막는다. usable_runs 인덱스를 identity로
    # 찾기 위해 id() 기반 매핑을 쓴다(Run은 eq=False라 값 비교가 안 됨).
    run_pos_by_id = {id(r): i for i, r in enumerate(usable_runs)}
    for i, r in enumerate(usable_runs):
        if r.attached_to is None or i not in run_candidates:
            continue
        reg_idx = run_pos_by_id.get(id(r.attached_to))
        for oid in run_candidates[i]:
            if reg_idx is not None and oid in run_candidates.get(reg_idx, []):
                model.Add(use_vars[i, oid] <= use_vars[reg_idx, oid])
            else:
                # 정규 run이 필터링돼서 아예 없거나, 이 후보가 정규 run에서는
                # 옵션이 아닌 경우(이론상 거의 없음) - 안전하게 잔업만
                # 단독으로 쓰는 걸 막는다.
                model.Add(use_vars[i, oid] == 0)

    # 후보별 총 생산량
    produced_expr: dict[str, list] = {c.order_id: [] for c in candidates}
    for i, r in enumerate(usable_runs):
        if i not in run_candidates:
            continue
        for oid in run_candidates[i]:
            c = next(cc for cc in candidates if cc.order_id == oid)
            rate = c.rate_by_type[r.line_type]
            qty = int(round(rate * r.production_slots()))
            produced_expr[oid].append(qty * use_vars[i, oid])

    produced_total: dict[str, cp_model.LinearExpr] = {
        oid: (sum(terms) if terms else 0) for oid, terms in produced_expr.items()
    }

    if require_new_order_full_qty:
        model.Add(produced_total[new_order.order_id] >= int(round(new_order.remaining_qty)))
    # else: 최대화 모드 - 하드 제약 없이 신규 주문 생산량 자체를 목적함수로 최대화(아래 Maximize 참고)

    # 재배정된 ASAP 후보들의 backlog(부족분) 비용
    backlog_cost_terms = []
    for c in candidates:
        if c.is_new:
            continue
        shortfall = model.NewIntVar(0, int(round(c.remaining_qty)) + 1, f"shortfall[{c.order_id}]")
        model.Add(shortfall >= int(round(c.remaining_qty)) - produced_total[c.order_id])
        backlog_cost_terms.append((shortfall, c.backlog_rate))

    # ---- 인원 수요/인건비 ----
    # baseline_demand: 이 tier에서 재배정 대상이 아닌(=동결된) 슬롯들이
    # 이미 차지하고 있는 인원 수요(main()의 compute_baseline_demand가 미리
    # 계산해서 넘겨줌). 여기에 이번에 새로 배정되는 run들의 "생산" 슬롯이
    # 요구하는 추가 인원을 더해서, 슬롯별 총 인원 수요(demand[t])를 만든다.
    all_slots = sorted(baseline_demand.keys())
    days = sorted({(t // SLOTS_PER_DAY) + 1 for t in all_slots})
    slot_extra_terms: dict[int, list] = {t: [] for t in all_slots}
    for i, r in enumerate(usable_runs):
        if i not in run_candidates:
            continue
        prod_slots = r.production_slots()
        if prod_slots <= 0:
            continue
        # 셋업 슬롯을 뺀 "생산" 슬롯에만 인원 수요가 발생(셋업은 별도 인력).
        prod_slot_indices = r.slot_indices[r.length - prod_slots:]
        for oid in run_candidates[i]:
            c = next(cc for cc in candidates if cc.order_id == oid)
            workers = c.workers_by_type.get(r.line_type, 0)
            if workers <= 0:
                continue
            # use_vars[i, oid]가 1이면(=이 run을 이 후보에게 배정하면) 그
            # run의 생산 슬롯마다 workers명이 추가로 필요해진다. use_vars가
            # 0이면 이 항은 0이 되므로, "배정됐을 때만" 수요에 반영된다.
            for t in prod_slot_indices:
                slot_extra_terms[t].append(workers * use_vars[i, oid])

    daily_wage_scaled = int(round(daily_wage * MONEY_SCALE))
    ot_wage_scaled = int(round(hourly_wage * overtime_multiplier * MONEY_SCALE))
    ot_local = set(OVERTIME_LOCAL_SLOTS)

    # 인건비 = (하루 중 최대 동시 투입인원) x 일급 [정규 인력 계약이 그 날
    # 최대 동시 투입인원 기준으로 이뤄지므로] + (잔업 슬롯 인원) x 시급 x
    # 잔업배율 [잔업은 슬롯별로 실제 투입된 사람 수만큼만 추가 지급].
    # scheduling/solver.py의 인건비 모델과 동일한 규칙.
    labor_cost_terms = []
    total_demand_var: dict[int, cp_model.IntVar] = {}
    max_possible = int(sum(c.workers_by_type.get(r.line_type, 0) for r in usable_runs for c in candidates) + 1
                        + max(baseline_demand.values(), default=0))
    for t in all_slots:
        extra = slot_extra_terms.get(t, [])
        tv = model.NewIntVar(0, max_possible, f"demand[{t}]")
        model.Add(tv == int(round(baseline_demand.get(t, 0))) + (sum(extra) if extra else 0))
        total_demand_var[t] = tv
        if (t % SLOTS_PER_DAY) in ot_local:
            labor_cost_terms.append(tv * ot_wage_scaled)  # 잔업 슬롯(17-18/18-19)은 슬롯별로 별도 시급 비용

    for d in days:
        day_slots = [total_demand_var[t] for t in all_slots if (t // SLOTS_PER_DAY) + 1 == d]
        dv = model.NewIntVar(0, max_possible, f"dailyWorkforce[{d}]")
        model.AddMaxEquality(dv, day_slots)  # 그 날 슬롯들 중 최댓값 = 그 날 필요한 정규 인원수
        labor_cost_terms.append(dv * daily_wage_scaled)

    objective_terms = list(labor_cost_terms)
    for shortfall, rate in backlog_cost_terms:
        objective_terms.append(shortfall * int(round(rate * MONEY_SCALE)))

    if require_new_order_full_qty:
        model.Minimize(sum(objective_terms))
    else:
        # 신규 주문 생산량 최대화가 최우선(하드 제약이 없으므로), 그 다음이
        # 비용 최소화 - 큰 배율로 우선순위를 분리(신규 생산량 1단위가
        # 인건비/backlog 전체 예산보다 항상 더 중요하도록).
        priority_scale = 10 ** 9
        model.Maximize(produced_total[new_order.order_id] * priority_scale - sum(objective_terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    solver.parameters.num_search_workers = 8
    solver.parameters.random_seed = 42
    status = solver.Solve(model)
    status_name = solver.StatusName(status)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return status_name, False, {}, {}

    assignment: dict[int, str | None] = {}
    for i, r in enumerate(usable_runs):
        if i not in run_candidates:
            continue
        chosen = None
        for oid in run_candidates[i]:
            if solver.Value(use_vars[i, oid]):
                chosen = oid
                break
        assignment[i] = chosen

    produced_values = {oid: solver.Value(produced_total[oid]) if not isinstance(produced_total[oid], int) else produced_total[oid]
                        for oid in produced_total}

    return status_name, True, {usable_runs[i]: oid for i, oid in assignment.items() if oid is not None}, produced_values


# ----------------------------------------------------------------------
# 메인
# ----------------------------------------------------------------------
def main():
    """전체 파이프라인 진입점. 순서대로:
      1) CLI 인자 파싱, --adding-date를 day index(adding_day)로 환산.
      2) 엑셀에서 주문 목록을 다시 읽어서 order_by_id를 만든다(이미
         line_schedule.csv에 반영된 배정을 "누가 만든 어떤 주문인지"로
         해석하려면 원본 주문 정보 - rate/workers/deadline 등 - 가 다시
         필요하기 때문. plan_from_orders.py를 다시 실행하는 게 아니라
         순수 조회용으로만 씀).
      3) 물리 라인 -> 라인타입 매핑(type_by_physical_line)을 만들고
         _line_type_of를 그 매핑을 참조하는 클로저로 바꿔치기한다
         (모듈 로드 시점엔 아직 이 매핑이 없어서 함수 정의 시점에는
         플레이스홀더로 남겨뒀던 것).
      4) 기존 line_schedule.csv/daily_workforce.csv를 로드.
      5) NEW_ORDER_* 플레이스홀더로 신규 주문 Candidate를 만든다.
      6) [0/1/2단계] 알고리즘 실행(모듈 docstring 참고) - 성공하면
         write_outputs로 결과 저장, 실패하면 이유를 출력하고 조용히 종료
         (return만 하고 별도 exit code는 안 씀 - 대화형으로 재시도하는
         워크플로라 스크립트 자체의 실패로 취급하지 않음).
    """
    global _line_type_of

    parser = argparse.ArgumentParser(description="이미 짜둔 계획에 신규 수주 1건을 끼워넣기")
    parser.add_argument("--excel-path", default=pfo.DEFAULT_EXCEL_PATH, help="기존 계획을 만들 때 쓴 수주진행현황 엑셀 경로")
    parser.add_argument("--reference-date", default=None, help="기존 계획을 만들 때 쓴 기준일(YYYY-MM-DD, 생략하면 오늘) - plan_from_orders.py와 맞춰야 함")
    parser.add_argument("--adding-date", required=True, help="신규 수주가 들어온 날짜(YYYY-MM-DD) - 이 날짜부터 재배정 가능")
    parser.add_argument("--horizon-days", type=int, default=pfo.MAX_DEADLINE_DAY)
    parser.add_argument("--dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "real_plan"),
                         help="기존 line_schedule.csv 등이 있는 디렉터리")
    parser.add_argument("--out-dir", default=None, help="결과 저장 폴더 (기본: <dir>/plan_additional_order)")
    parser.add_argument("--daily-wage", type=float, default=120_000)
    parser.add_argument("--hourly-wage", type=float, default=None)
    parser.add_argument("--overtime-multiplier", type=float, default=1.5)
    parser.add_argument("--backlog-cost", type=float, default=100.0, help="ASAP 주문 기본 backlog 단가(원/개/일) - plan_from_orders.py --backlog-cost와 맞추는 걸 권장")
    parser.add_argument("--closed-date", action="append", default=[])
    parser.add_argument("--no-weekend-closure", action="store_true")
    parser.add_argument("--time-limit", type=float, default=60.0, help="tier별 CP-SAT 탐색 제한시간(초)")
    args = parser.parse_args()

    reference_date = date.fromisoformat(args.reference_date) if args.reference_date else date.today()
    adding_date = date.fromisoformat(args.adding_date)
    adding_day = (adding_date - reference_date).days + 1
    if not (1 <= adding_day <= args.horizon_days):
        raise SystemExit(f"[오류] --adding-date가 계획기간(1~{args.horizon_days}) 밖입니다(day {adding_day}).")

    hourly_wage = args.hourly_wage if args.hourly_wage is not None else args.daily_wage / 8.0

    print(f"[정보] 기준일={reference_date}, 신규 수주 반영일={adding_date}(day {adding_day}), 계획기간={args.horizon_days}일")

    raw_orders, _load_stats = pfo.load_orders_from_excel(args.excel_path, reference_date=reference_date)
    orders, _filter_stats = pfo.filter_and_attach_rates(raw_orders)
    order_by_id = {o.order_id: o for o in orders}

    lines = pfo.build_lines()
    pools = build_line_pools(lines, orders)
    type_by_physical_line: dict[str, str] = {}
    for line, pool in zip(lines, pools):
        for pid in pool.line_ids:
            type_by_physical_line[pid] = line.line_type_id

    def _line_type_of_impl(line_id: str) -> str:
        return type_by_physical_line[line_id]
    _line_type_of = _line_type_of_impl  # noqa: F811 - build_asap_candidates/detect_runs가 참조하는 모듈 전역을 여기서 채워넣음

    closed_dates = [date.fromisoformat(s) for s in args.closed_date]
    closed_days = pfo.resolve_closed_days(
        reference_date, args.horizon_days, closed_dates, close_weekends=not args.no_weekend_closure,
    )

    schedule_df, workforce_df = load_existing_state(args.dir)

    unknown_order_ids = sorted(set(schedule_df["order_id"].dropna().unique()) - set(order_by_id))
    if unknown_order_ids:
        print(
            f"[경고] line_schedule.csv에는 있지만 지금 엑셀에서 다시 찾을 수 없는 주문 {len(unknown_order_ids)}건 "
            f"(엑셀이 그 사이 바뀌었을 수 있음) - 안전하게 '동결'로 취급합니다: {unknown_order_ids[:10]}"
            + ("..." if len(unknown_order_ids) > 10 else "")
        )

    new_order = build_new_order_candidate(reference_date, adding_day, args.horizon_days, args.backlog_cost)
    print(f"[정보] 신규 주문: {new_order.order_id}, 필요수량={new_order.remaining_qty:,.0f}, "
          f"납기={'ASAP' if new_order.deadline_day is None else f'day {new_order.deadline_day}'}, "
          f"호환 라인타입={list(new_order.rate_by_type)}")

    # ---- baseline: adding_day 이후, "동결된"(항상 얼어있는 = 마감일 있는 주문) 슬롯들의 인원 수요 ----
    # solve_tier는 이 baseline 위에 새로 배정되는 run들의 인원 수요만
    # 얹어서 슬롯별/일별 총 인원을 계산한다(동결된 슬롯은 run으로 취급되지
    # 않으므로 solve_tier가 알 방법이 없어서, 미리 계산해 넘겨줘야 함).
    # unfreeze_asap=False(tier1): 마감일 있는 주문 + ASAP 주문까지 전부
    #   baseline에 포함(둘 다 이번 tier에서는 안 건드리는 대상이므로).
    # unfreeze_asap=True(tier2): ASAP 주문은 baseline에서 빼고, 대신
    #   build_asap_candidates로 만든 candidate로 재배정 대상에 넘긴다
    #   (baseline에 포함 + candidate로도 넘기면 이중 계산이 되므로 주의).
    def compute_baseline_demand(unfreeze_asap: bool) -> dict[int, float]:
        slot_order = {s: i for i, s in enumerate(SLOT_LABELS)}
        demand: dict[int, float] = {}
        window = schedule_df[(schedule_df["day"] >= adding_day) & (schedule_df["activity"] == "produce")]
        for row in window.itertuples(index=False):
            oid = row.order_id
            o = order_by_id.get(oid)
            if o is None:
                is_asap = False  # 모르는 주문은 안전하게 동결(=baseline에 포함)
            else:
                is_asap = o.is_asap()
            if unfreeze_asap and is_asap:
                continue  # tier2에서는 ASAP 슬롯을 baseline에서 빼고 재배정 대상으로 넘김
            workers = 0 if o is None else o.workers.get(type_by_physical_line[row.line_id], 0)
            t = (row.day - 1) * SLOTS_PER_DAY + slot_order[row.slot]
            demand[t] = demand.get(t, 0) + workers
        # 재배정 대상이 아닌 슬롯도 0으로 채워서 daily max 계산이 정확하게 되도록.
        for day in range(adding_day, args.horizon_days + 1):
            for local in range(SLOTS_PER_DAY):
                t = (day - 1) * SLOTS_PER_DAY + local
                demand.setdefault(t, 0)
        return demand

    # ---- [0단계] 상한선 체크: idle + ASAP 반납 + 잔업 풀가동 ----
    runs_tier2_all, synthetic_setups_tier2 = detect_runs(
        schedule_df, adding_day, args.horizon_days, closed_days, order_by_id, include_asap=True
    )
    ceiling = ceiling_capacity(runs_tier2_all, new_order)
    print(f"\n[0단계] 상한선 체크(idle+ASAP 반납+잔업 풀가동, 셋업 손실 무시): 이론상 최대 {ceiling:,.0f}개")
    if ceiling < new_order.remaining_qty:
        print(f"[결과] 실패 - 이론상 최대치({ceiling:,.0f}개)가 필요수량({new_order.remaining_qty:,.0f}개)보다 적습니다.")
        print("[결과] 물리적으로 불가능합니다(ASAP를 전부 포기하고 잔업까지 풀가동해도 못 채움). CP-SAT을 돌리지 않고 종료합니다.")
        return

    # ---- [1단계] idle-only ----
    runs_tier1, synthetic_setups_tier1 = detect_runs(
        schedule_df, adding_day, args.horizon_days, closed_days, order_by_id, include_asap=False
    )
    idle_ceiling = ceiling_capacity(runs_tier1, new_order)
    print(f"\n[1단계] idle 슬롯만 사용 시 이론상 최대: {idle_ceiling:,.0f}개")

    baseline_tier1 = compute_baseline_demand(unfreeze_asap=False)
    tier1_result = None
    if idle_ceiling >= new_order.remaining_qty:
        print("[1단계] 이론상 가능 - idle 슬롯만으로 CP-SAT 최적화 시도")
        status_name, ok, assignment, produced = solve_tier(
            runs_tier1, [new_order], baseline_tier1, args.daily_wage, hourly_wage, args.overtime_multiplier,
            args.time_limit, require_new_order_full_qty=True,
        )
        print(f"[1단계] solver 상태: {status_name}")
        if ok:
            tier1_result = (assignment, produced, [new_order], baseline_tier1, synthetic_setups_tier1)
            print(f"[결과] 1단계 성공 - 기존 ASAP 주문 스케줄은 전혀 안 건드리고 신규 주문을 idle 슬롯에 다 끼워넣었습니다.")
    else:
        print("[1단계] 이론상으로도 idle만으로는 부족 - 2단계로 넘어감")

    final = None
    if tier1_result is not None:
        final = ("tier1", *tier1_result)
    else:
        # ---- [2단계] idle + ASAP 반납 ----
        print("\n[2단계] ASAP 주문 슬롯까지 재배정 대상에 포함해서 재시도")
        asap_candidates = build_asap_candidates(schedule_df, order_by_id, adding_day, args.horizon_days, args.backlog_cost)
        print(f"[2단계] 재배정 대상 ASAP 주문 {len(asap_candidates)}건: {list(asap_candidates)}")
        candidates2 = [new_order] + list(asap_candidates.values())
        baseline_tier2 = compute_baseline_demand(unfreeze_asap=True)

        status_name, ok, assignment, produced = solve_tier(
            runs_tier2_all, candidates2, baseline_tier2, args.daily_wage, hourly_wage, args.overtime_multiplier,
            args.time_limit, require_new_order_full_qty=True,
        )
        print(f"[2단계] solver 상태: {status_name}")
        if ok:
            final = ("tier2", assignment, produced, candidates2, baseline_tier2, synthetic_setups_tier2)
            print("[결과] 2단계 성공 - 일부 ASAP 주문 생산이 밀려나고(backlog 증가) 신규 주문이 반영됐습니다.")
        else:
            print("[결과] 2단계도 CP-SAT 자체는 실패(0단계 이론상 상한은 통과했지만 셋업/구간 제약 때문에 실제로는 못 채움).")
            print("[결과] 최대 달성 가능한 생산량을 다시 계산합니다(수량 하드 제약 없이 최대화)...")
            status_name2, ok2, assignment2, produced2 = solve_tier(
                runs_tier2_all, candidates2, baseline_tier2, args.daily_wage, hourly_wage, args.overtime_multiplier,
                args.time_limit, require_new_order_full_qty=False,
            )
            if ok2:
                achieved = produced2.get(new_order.order_id, 0)
                print(f"[결과] 실패 - 실제로 달성 가능한 최대 생산량은 {achieved:,.0f}개 입니다(필요 {new_order.remaining_qty:,.0f}개).")
            else:
                print(f"[결과] 실패 - 최대화 재시도도 solver 상태 {status_name2}로 끝났습니다(시간제한을 늘려보세요: --time-limit).")
            return

    tier_name, assignment, produced, candidates_used, baseline, synthetic_setups = final
    write_outputs(args, schedule_df, workforce_df, order_by_id, new_order, tier_name, assignment, produced,
                  candidates_used, baseline, adding_day, type_by_physical_line, synthetic_setups,
                  daily_wage=args.daily_wage, hourly_wage=hourly_wage, overtime_multiplier=args.overtime_multiplier)


def plot_updated_gantt(schedule_df: pd.DataFrame, orders_for_plot: list, horizon_days: int, output_path: str) -> None:
    """갱신된 전체 스케줄(schedule_df, line_schedule.csv와 동일한 형식)을
    scheduling/report.py의 plot_gantt와 같은 스타일(행=라인, 열=계획기간
    전체 시간슬롯, 주문별 색상 + 마감일 빨간 점선)로 그린다. orders_for_plot의
    각 원소는 order_id/product_id/deadline_day 속성만 있으면 되므로
    scheduling.models.Order든 이 파일의 Candidate든 섞어서 넣어도 된다
    (신규 주문은 excel에 없어서 Order가 아니라 Candidate로 넘어옴)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.colors import BoundaryNorm, ListedColormap
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch

    plt.rcParams["font.family"] = "Malgun Gothic"
    plt.rcParams["axes.unicode_minus"] = False

    slot_order = {s: i for i, s in enumerate(SLOT_LABELS)}
    T = horizon_days * SLOTS_PER_DAY

    line_ids: list[str] = []
    per_line: dict[str, dict[int, tuple[str, str]]] = {}
    for line_id, g in schedule_df.groupby("line_id", sort=False):
        cells: dict[int, tuple[str, str]] = {}
        any_active = False
        for row in g.itertuples(index=False):
            t = (row.day - 1) * SLOTS_PER_DAY + slot_order[row.slot]
            oid = "" if pd.isna(row.order_id) else row.order_id
            cells[t] = (row.activity, oid)
            if row.activity != "idle":
                any_active = True
        if any_active:
            line_ids.append(line_id)
            per_line[line_id] = cells
    line_ids.sort()  # 스크립트마다 라인 순서가 제각각이면 비교하기 어려우므로 가나다순으로 통일

    order_ids_present = sorted({o.order_id for o in orders_for_plot})
    order_product = {o.order_id: o.product_id for o in orders_for_plot}
    order_index = {oid: i + 2 for i, oid in enumerate(order_ids_present)}

    palette = (
        list(plt.get_cmap("tab20").colors)
        + list(plt.get_cmap("tab20b").colors)
        + list(plt.get_cmap("tab20c").colors)
    )
    order_colors = [palette[i % len(palette)] for i in range(len(order_ids_present))]
    base_colors = ["#f2f2f2", "#9e9e9e"] + order_colors
    cmap = ListedColormap(base_colors)
    norm = BoundaryNorm(list(range(len(base_colors) + 1)), cmap.N)

    grid = np.zeros((len(line_ids), T), dtype=int)
    for r, lid in enumerate(line_ids):
        cells = per_line[lid]
        for t in range(T):
            activity, oid = cells.get(t, ("idle", ""))
            if activity == "idle":
                grid[r, t] = 0
            elif activity == "setup":
                grid[r, t] = 1
            else:
                grid[r, t] = order_index.get(oid, 1)

    # 마감일 라벨 개수 제한 + 여백을 라인 수와 무관하게 고정으로 확보
    # (scheduling/report.py의 plot_gantt와 동일한 이유 - 라벨이 길어지면
    # tight_layout이 실제 차트 영역을 짓눌러버리는 문제가 있었음).
    DEADLINE_LABEL_MARGIN_INCHES = 2.5
    MAX_DEADLINE_LABEL_ITEMS = 4
    fig_w = max(12, T / 25)
    fig_h = max(3, 0.5 * len(line_ids) + 1.5) + DEADLINE_LABEL_MARGIN_INCHES
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    ax.imshow(grid, aspect="auto", cmap=cmap, norm=norm, interpolation="none")

    for d in range(horizon_days + 1):
        ax.axvline(d * SLOTS_PER_DAY - 0.5, color="white", linewidth=0.6)

    deadlines_by_day: dict[int, list[str]] = {}
    for o in orders_for_plot:
        if o.deadline_day is None:
            continue
        deadlines_by_day.setdefault(o.deadline_day, []).append(o.product_id)
    for d, product_ids_due in deadlines_by_day.items():
        x = d * SLOTS_PER_DAY - 0.5
        ax.axvline(x, color="red", linestyle="--", linewidth=1.3, alpha=0.9, zorder=5)
        if len(product_ids_due) > MAX_DEADLINE_LABEL_ITEMS:
            shown = product_ids_due[:MAX_DEADLINE_LABEL_ITEMS]
            label = ",".join(shown) + f"+{len(product_ids_due) - MAX_DEADLINE_LABEL_ITEMS}개"
        else:
            label = ",".join(product_ids_due)
        ax.text(x, -0.6, label, color="red", fontsize=7, rotation=90, ha="right", va="bottom")
    ax.set_ylim(len(line_ids) - 0.5, -2.5)

    tick_days = list(range(0, horizon_days, 5)) or [0]
    ax.set_xticks([d * SLOTS_PER_DAY + SLOTS_PER_DAY / 2 - 0.5 for d in tick_days])
    ax.set_xticklabels([f"{d + 1}일" for d in tick_days])
    ax.set_yticks(range(len(line_ids)))
    ax.set_yticklabels(line_ids)
    ax.set_title("생산 스케줄 개요 - 신규 주문 반영 후 (행: 라인, 열: 시간슬롯)")

    legend_items = [
        Patch(facecolor="#f2f2f2", edgecolor="gray", label="대기(idle)"),
        Patch(facecolor="#9e9e9e", label="셋업(setup)"),
    ]
    for oid in order_ids_present:
        legend_items.append(Patch(facecolor=base_colors[order_index[oid]], label=f"생산: {order_product[oid]} [{oid}]"))
    legend_items.append(Line2D([0], [0], color="red", linestyle="--", linewidth=1.3, label="마감일"))
    ax.legend(handles=legend_items, bbox_to_anchor=(1.02, 1), loc="upper left", fontsize=8)

    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def compute_completion_day(
    schedule_df: pd.DataFrame, order_id: str, rate_by_type: dict[str, float], type_by_physical_line: dict[str, str],
    required_qty: float,
) -> int | None:
    """updated_schedule 전체(과거+갱신된 구간 다 포함)를 날짜순으로 훑어서
    누적 생산량이 required_qty에 처음 도달하는 날(day)을 찾는다 - 못
    채웠으면 None. order_fulfillment.csv의 completion_day 칼럼을 다시
    계산하는 용도(order_gantt.py 등이 이 칼럼으로 완료일을 표시함)."""
    produce = schedule_df[(schedule_df["order_id"] == order_id) & (schedule_df["activity"] == "produce")]
    if produce.empty:
        return None
    qty_by_day: dict[int, float] = {}
    for row in produce.itertuples(index=False):
        line_type = type_by_physical_line.get(row.line_id)
        rate = rate_by_type.get(line_type, 0)
        qty_by_day[row.day] = qty_by_day.get(row.day, 0) + rate
    cumulative = 0.0
    for day in sorted(qty_by_day):
        cumulative += qty_by_day[day]
        if cumulative >= required_qty:
            return day
    return None


def write_outputs(
    args, schedule_df, workforce_df, order_by_id, new_order, tier_name, assignment, produced,
    candidates_used, baseline, adding_day, type_by_physical_line, synthetic_setups,
    *, daily_wage, hourly_wage, overtime_multiplier,
):
    """solve_tier가 찾은 배정(assignment: Run -> order_id)을 실제
    line_schedule.csv 형식의 행으로 풀어서 저장하고, 거기서 파생되는
    나머지 산출물(daily_workforce.csv/order_fulfillment.csv/
    gantt_added.png)까지 순서대로 만든다. 전부 out_dir(기본
    args.dir/plan_additional_order/)에 같이 저장된다.

    핵심은 "adding_day 이후 스케줄 재구성" 단계(아래 new_rows 루프)로,
    각 (line_id, day, slot)마다 우선순위를 이렇게 둔다:
      1) 이번에 새로 배정된 슬롯(reassigned_slots) - CP-SAT이 고른 것.
      2) synthetic_setups로 예약된 슬롯 - detect_runs가 찾아낸
         idle_nf_of 연속성 브릿지의 마지막 칸으로, 새 주문에게 안 내주고
         원래(동결된) 주문의 setup으로 명시해야 그 주문의 이어지는
         produce가 셋업 없이 재개되는 버그를 막을 수 있다.
      3) 그 외에는 원본(original_lookup) 그대로 - 단, 이번 tier에서
         재배정 대상이었던 주문(touched_order_ids)인데 이번엔 어떤
         run으로도 안 뽑혔으면 idle로 되돌린다(자리를 내줬지만 실제로는
         안 쓰인 슬롯이므로).
    """
    out_dir = args.out_dir or os.path.join(args.dir, "plan_additional_order")
    os.makedirs(out_dir, exist_ok=True)

    candidate_by_id = {c.order_id: c for c in candidates_used}

    # ---- 갱신된 line_schedule.csv 만들기 ----
    # 1) adding_day 이전은 그대로(이미 지나간 것으로 취급, 절대 안 건드림).
    before = schedule_df[schedule_df["day"] < adding_day].copy()

    # 2) adding_day 이후: run으로 새로 배정된 슬롯은 그 배정을 쓰고, 그
    #    외의 모든 슬롯은 "원래 뭐였는지"를 보고 판단한다 - 이번 tier에서
    #    재배정 대상이었던 주문(touched_order_ids)의 슬롯인데 이번엔
    #    아무 run도 못 받았으면 idle로 되돌아가고(자리를 내줬는데 실제로는
    #    안 쓰인 경우), 그 외(마감일 있는 주문, 또는 이번 tier에서 건드리지
    #    않은 ASAP 주문, 원래부터 idle)는 원본 그대로 유지한다.
    touched_order_ids = {c.order_id for c in candidates_used if not c.is_new}
    slot_order = {s: i for i, s in enumerate(SLOT_LABELS)}

    original_lookup: dict[tuple[str, int, str], tuple[str, str, str]] = {}
    for row in schedule_df[schedule_df["day"] >= adding_day].itertuples(index=False):
        original_lookup[row.line_id, row.day, row.slot] = (
            row.activity, row.product_id, "" if pd.isna(row.order_id) else row.order_id
        )

    reassigned_slots: dict[tuple[str, int, str], tuple[str, str, str]] = {}  # (line_id, day, slot) -> (activity, product_id, order_id)
    for run, oid in assignment.items():
        c = candidate_by_id[oid]
        prod_slots = run.production_slots()
        setup_slot_indices = run.slot_indices[: run.length - prod_slots]
        prod_slot_indices = run.slot_indices[run.length - prod_slots:]
        for t in setup_slot_indices:
            local = t % SLOTS_PER_DAY
            reassigned_slots[run.line_id, run.day, SLOT_LABELS[local]] = ("setup", c.product_id, oid)
        for t in prod_slot_indices:
            local = t % SLOTS_PER_DAY
            reassigned_slots[run.line_id, run.day, SLOT_LABELS[local]] = ("produce", c.product_id, oid)

    all_lines = schedule_df["line_id"].unique()
    new_rows = []
    for line_id in all_lines:
        for day in range(adding_day, args.horizon_days + 1):
            for slot in SLOT_LABELS:
                key = (line_id, day, slot)
                if key in reassigned_slots:
                    activity, product_id, order_id = reassigned_slots[key]
                elif key in synthetic_setups and synthetic_setups[key][2] in assignment:
                    # idle_nf_of 연속성 브릿지의 마지막 슬롯: 바로 앞의 트리거
                    # run이 실제로 어떤 candidate에게 배정됐을 때만(=그 candidate가
                    # 실제로 이 라인의 설정을 바꿔놨을 때만) 원래 주문을 위한
                    # setup으로 채운다. 트리거 run이 배정 안 됐으면(아무도 그 앞을
                    # 안 건드렸으면) 실제로는 아무 셋업도 일어나지 않은 것이므로
                    # else 분기로 내려가 원본 그대로(=idle) 유지한다.
                    product_id, order_id, _trigger_run = synthetic_setups[key]
                    activity = "setup"
                else:
                    orig_activity, orig_product, orig_order = original_lookup[key]
                    if orig_activity != "idle" and orig_order in touched_order_ids:
                        activity, product_id, order_id = "idle", "", ""  # 자리를 내줬지만 실제로는 안 쓰인 슬롯
                    else:
                        activity, product_id, order_id = orig_activity, orig_product, orig_order
                new_rows.append({"line_id": line_id, "day": day, "slot": slot, "activity": activity,
                                  "product_id": product_id, "order_id": order_id})

    updated_after = pd.DataFrame(new_rows)
    updated_after["_slot_order"] = updated_after["slot"].map(slot_order)
    updated_schedule = pd.concat([before, updated_after], ignore_index=True)
    updated_schedule["_slot_order"] = updated_schedule["_slot_order"].fillna(
        updated_schedule["slot"].map(slot_order)
    )
    updated_schedule = updated_schedule.sort_values(["line_id", "day", "_slot_order"]).drop(columns=["_slot_order"])

    schedule_path = os.path.join(out_dir, "line_schedule.csv")
    updated_schedule.to_csv(schedule_path, index=False, encoding="utf-8-sig")

    # ---- 갱신된 daily_workforce.csv (adding_day 이후만 재계산) ----
    # solve_tier 안에서 CP-SAT 변수로 계산했던 슬롯별/일별 인원 수요를
    # 여기서 파이썬으로 그대로 재현한다(baseline + 이번에 배정된 run들의
    # 생산 슬롯 인원). synthetic_setups로 예약된 슬롯은 setup이라
    # 인원 수요가 0이므로 여기서 따로 처리할 필요가 없다.
    demand_by_slot = dict(baseline)
    for run, oid in assignment.items():
        c = candidate_by_id[oid]
        prod_slots = run.production_slots()
        workers = c.workers_by_type.get(run.line_type, 0)
        if workers <= 0 or prod_slots <= 0:
            continue
        for t in run.slot_indices[run.length - prod_slots:]:
            demand_by_slot[t] = demand_by_slot.get(t, 0) + workers

    workforce_rows = []
    for day in range(adding_day, args.horizon_days + 1):
        day_slots = [demand_by_slot.get((day - 1) * SLOTS_PER_DAY + local, 0) for local in range(SLOTS_PER_DAY)]
        ot_local = list(OVERTIME_LOCAL_SLOTS)
        workforce_rows.append({
            "day": day,
            "workforce": max(day_slots) if day_slots else 0,
            "overtime_17_18": day_slots[ot_local[0]] if len(day_slots) > ot_local[0] else 0,
            "overtime_18_19": day_slots[ot_local[1]] if len(day_slots) > ot_local[1] else 0,
        })
    updated_workforce_after = pd.DataFrame(workforce_rows)
    workforce_before = workforce_df[workforce_df["day"] < adding_day]
    updated_workforce = pd.concat([workforce_before, updated_workforce_after], ignore_index=True).sort_values("day")
    workforce_path = os.path.join(out_dir, "daily_workforce.csv")
    updated_workforce.to_csv(workforce_path, index=False, encoding="utf-8-sig")

    # ---- 갱신된 order_fulfillment.csv ----
    # order_gantt.py 등 output/real_plan/prod_tendency/의 분석 스크립트들이
    # "이번 계획에 포함된 전체 주문 목록 + 납기/완료일"을 여기서 읽으므로
    # (order_fulfillment.csv가 없으면 line_schedule.csv에 등장하는 order_id로만
    # 대체하는데, 그러면 납기/완료일 정보 없이 그림만 그려짐), 원본을 베이스로
    # 재배정된 ASAP 주문의 produced/completion_day/final_backlog만 갱신하고
    # 신규 주문 행을 새로 추가한다. 원본이 없으면(한 번도 저장 안 됐으면)
    # 신규 주문 행 하나만 담아서 새로 만든다.
    fulfillment_path_in = os.path.join(args.dir, "order_fulfillment.csv")
    if os.path.exists(fulfillment_path_in):
        fulfillment = pd.read_csv(fulfillment_path_in).set_index("order_id")
    else:
        fulfillment = pd.DataFrame(
            columns=["product_id", "required", "produced", "deadline_day", "completion_day", "final_backlog"]
        ).rename_axis("order_id")

    for c in candidates_used:
        if c.is_new:
            continue
        o = order_by_id.get(c.order_id)
        if o is None:
            continue  # 엑셀에서 다시 못 찾은 주문(이미 위에서 경고 남김) - 원본 행 그대로 둠
        already_produced_before = max(0.0, o.quantity - c.remaining_qty)
        total_produced = already_produced_before + produced.get(c.order_id, 0)
        completion_day = compute_completion_day(updated_schedule, c.order_id, o.rate, type_by_physical_line, o.quantity)
        final_backlog = max(0.0, o.quantity - total_produced)
        fulfillment.loc[c.order_id, ["product_id", "required", "produced", "deadline_day", "completion_day", "final_backlog"]] = [
            o.product_id, o.quantity, total_produced, None, completion_day, final_backlog,
        ]

    new_order_produced = produced.get(new_order.order_id, 0)
    new_order_completion_day = compute_completion_day(
        updated_schedule, new_order.order_id, new_order.rate_by_type, type_by_physical_line, new_order.remaining_qty
    )
    new_order_final_backlog = max(0.0, new_order.remaining_qty - new_order_produced) if new_order.deadline_day is None else None
    fulfillment.loc[new_order.order_id, ["product_id", "required", "produced", "deadline_day", "completion_day", "final_backlog"]] = [
        NEW_ORDER_PRODUCT_ID, new_order.remaining_qty, new_order_produced, new_order.deadline_day,
        new_order_completion_day, new_order_final_backlog,
    ]

    fulfillment_path_out = os.path.join(out_dir, "order_fulfillment.csv")
    fulfillment.reset_index().to_csv(fulfillment_path_out, index=False, encoding="utf-8-sig")

    # ---- 콘솔 요약 ----
    print(f"\n=== 결과 요약 ({tier_name}) ===")
    print(f"신규 주문 {new_order.order_id}: 생산 {produced.get(new_order.order_id, 0):,.0f} / 필요 {new_order.remaining_qty:,.0f}")
    for c in candidates_used:
        if c.is_new:
            continue
        prod = produced.get(c.order_id, 0)
        print(f"  재배정된 ASAP {c.order_id}: 이 구간 생산 {prod:,.0f} / 남은필요 {c.remaining_qty:,.0f} "
              f"(부족분 {max(0, c.remaining_qty - prod):,.0f})")

    old_daily = {}
    for day in range(adding_day, args.horizon_days + 1):
        day_slots = [baseline.get((day - 1) * SLOTS_PER_DAY + local, 0) for local in range(SLOTS_PER_DAY)]
        old_daily[day] = max(day_slots) if day_slots else 0
    new_daily = {r["day"]: r["workforce"] for r in workforce_rows}
    old_cost = sum(old_daily[d] * daily_wage for d in old_daily)
    new_cost = sum(new_daily[d] * daily_wage for d in new_daily)
    print(f"\n인건비(정규 일급 기준, {adding_day}일차~{args.horizon_days}일차 구간): "
          f"기존 {old_cost:,.0f} -> 신규 {new_cost:,.0f} (증가분 {new_cost - old_cost:,.0f})")

    print(f"\n[저장 완료] {schedule_path}")
    print(f"[저장 완료] {workforce_path}")
    print(f"[저장 완료] {fulfillment_path_out}")

    # ---- gantt_added.png: line_schedule.csv 등 나머지 결과물과 같이
    # out_dir(plan_additional_order/)에 저장한다.
    orders_for_plot = list(order_by_id.values()) + [new_order]
    gantt_path = os.path.join(out_dir, "gantt_added.png")
    plot_updated_gantt(updated_schedule, orders_for_plot, args.horizon_days, gantt_path)
    print(f"[저장 완료] {gantt_path}")

    # order_gantt.py 등 prod_tendency/의 분석 스크립트들은 기본적으로
    # output/real_plan/(원본)을 보므로, out_dir(plan_additional_order/)을
    # 보게 하려면 --data-dir을 꼭 지정해야 한다 - 매번 까먹기 쉬워서
    # 바로 복사해 쓸 수 있는 명령어를 알려준다.
    print(
        f"\n[안내] order_gantt.py 등으로 이 결과를 보려면 --data-dir로 이 폴더를 지정하세요, 예:\n"
        f"  python order_gantt.py --order-id {new_order.order_id} --data-dir {out_dir}"
    )


if __name__ == "__main__":
    main()
