# -*- coding: utf-8 -*-
"""
schedule_optimizer.py

30일 생산 스케줄링 최적화 프로토타입 (Google OR-Tools CP-SAT 사용).

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
    (사용자 확인 반영, build_and_solve()의 전이 제약 주석 참고). 또한 하루
    일과 중 셋업이 발생하면 그건 별도 인력이 처리하므로, 그 시간 동안 해당
    라인에 필요한 '라인 작업자(line worker)' 인원은 0이다(=셋업은 일일
    고용 인원 계산에 반영되지 않음).
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
    (build_and_solve()의 "2단계 풀이" 부분 참고). --no-continuity로 끌 수 있다.

모델링 방식: 시간을 300개(=30일 x 10슬롯)의 이산 슬롯으로 보고, 각
(라인, 슬롯) 쌍마다 '이 슬롯에 이 라인이 뭘 하고 있는가'(대기/셋업(어떤
제품)/생산(어떤 제품))를 불리언 변수로 표현하는 CP-SAT(제약 충족/정수계획)
모델이다. 자세한 변수 설계는 build_and_solve()의 주석을 참고.

이 스크립트에 내장된 예시 데이터(build_example_instance())로 바로 실행해
볼 수 있고, --data 옵션으로 JSON 파일을 넣으면 실제 라인/주문 데이터로도
돌릴 수 있다.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from dataclasses import dataclass, field

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from ortools.sat.python import cp_model

# ----------------------------------------------------------------------
# 시간 구조 상수
# ----------------------------------------------------------------------
# 하루 10개 슬롯: 08-12시(4개) + 13-17시(4개, 이상 정규 8시간) + 17-19시(2개, 잔업)
SLOT_LABELS = ["08-09", "09-10", "10-11", "11-12", "13-14", "14-15", "15-16", "16-17", "17-18", "18-19"]
SLOTS_PER_DAY = len(SLOT_LABELS)  # 10
OVERTIME_LOCAL_SLOTS = [8, 9]  # SLOT_LABELS 안에서 '17-18', '18-19'의 인덱스

# CP-SAT은 정수만 다루기 때문에, 원 단위 금액(특히 잔업배수 1.5x처럼 소수가
# 낀 값)을 정수로 안전하게 다루기 위해 내부적으로 곱해두는 배율. 목적함수
# 값을 보고할 때는 이 배율로 다시 나눠서 원래 금액 단위로 돌려놓는다.
MONEY_SCALE = 100


# ----------------------------------------------------------------------
# 입력 데이터 구조
# ----------------------------------------------------------------------
@dataclass
class Line:
    """물리적으로 독립된 생산라인 하나. 같은 라인 타입의 동일 설비가 여러
    대 있으면(예: line_mask_3가 4대) 이 클래스 인스턴스를 4개 만들어서
    각각 독립 라인으로 취급한다 (expand_line_types() 참고).
    """

    line_id: str
    category: str  # "mask" / "container" / "tube" - 참고/표시용. 실제 생산
                    # 가능 여부는 Order.rate에 그 라인이 등록되어 있고 값이
                    # 0보다 큰지로 판단하므로, category가 달라도 rate만
                    # 맞으면 모델상으로는 생산 가능하다.


@dataclass
class Order:
    """생산 주문 하나 = 스케줄링 대상 제품 하나.

    이 프로토타입에서는 '제품 1개 = 주문 1개'로 단순화했다. 만약 실제로
    같은 물리적 제품을 마감일이 다른 여러 주문으로 나눠 받는 경우라면,
    product_id는 같게 두고 order_id만 다르게 줘서 별도 Order로 등록하면
    된다(스케줄링 로직 상으로는 각 주문을 별개 제품처럼 다뤄도 무방하다).
    """

    order_id: str
    product_id: str
    category: str
    quantity: int          # 필요 총 생산수량
    deadline_day: int      # 1-indexed. 이 날짜의 마지막 슬롯(18-19시)까지 완료되어야 함.
    rate: dict = field(default_factory=dict)     # {line_id: 시간당 생산수량}. 없거나 0이면 그 라인에서 생산 불가.
    workers: dict = field(default_factory=dict)  # {line_id: 그 라인에서 이 제품 생산/셋업에 필요한 인원}

    def compatible_lines(self) -> list[str]:
        return [lid for lid, r in self.rate.items() if r and r > 0]


@dataclass
class ScheduleConfig:
    horizon_days: int = 30
    daily_wage: float = 200_000       # 인원 1명을 하루 고용할 때 지급하는 정액 임금(원)
    hourly_wage: float | None = None  # None이면 daily_wage / 8 (정규 8시간 기준 시급으로 역산)
    overtime_multiplier: float = 1.5
    time_limit_seconds: float = 60.0
    num_search_workers: int = 8       # CP-SAT 병렬 탐색 스레드 수
    random_seed: int = 42             # 고정해두면 같은 입력/설정에서 항상 같은 해가 나와 비교/재현이 쉬움
    log_progress: bool = False

    # 2단계(lexicographic) 최적화 설정. 1단계에서 찾은 최소 인건비를 그대로
    # 유지한 채(=인건비를 한 푼도 더 쓰지 않는 선에서), 라인이 대기<->생산을
    # 왔다갔다하는 전환횟수(각 1점) + 셋업 횟수(각 2점, 대기 왕복과 동급으로
    # 취급)의 합("연속성 점수")을 2단계에서 추가로 최소화한다. 즉 "총비용이
    # 같다면 사람이 한 라인에 최대한 오래 붙어있는 스케줄을 선호한다"는
    # 요청을 정확히 타이브레이크로 구현한 것.
    optimize_continuity: bool = True
    secondary_time_limit_seconds: float = 60.0

    def resolved_hourly_wage(self) -> float:
        return self.hourly_wage if self.hourly_wage is not None else self.daily_wage / 8.0


def expand_line_types(spec: list[tuple[str, str, int]]) -> list[Line]:
    """(line_type_id, category, 대수) 목록을 물리적으로 독립된 Line 객체
    목록으로 펼친다. 대수가 1이면 line_type_id를 그대로 쓰고, 2대 이상이면
    'line_type_id_1', 'line_type_id_2', ... 로 각각 별도 라인을 만든다.

    예) ("line_mask_3", "mask", 4) -> line_mask_3_1 ~ line_mask_3_4 (4개의
    독립 라인). 문제 설명의 "각 물리 설비는 독립된 생산라인으로 취급한다"는
    요구사항을 그대로 구현한 것.
    """
    lines: list[Line] = []
    for type_id, category, count in spec:
        if count <= 1:
            lines.append(Line(line_id=type_id, category=category))
        else:
            for i in range(1, count + 1):
                lines.append(Line(line_id=f"{type_id}_{i}", category=category))
    return lines


# ----------------------------------------------------------------------
# 결과 구조
# ----------------------------------------------------------------------
@dataclass
class ScheduleResult:
    status_name: str
    is_feasible: bool
    total_cost: float | None
    daily_workforce: dict            # {day(1-idx): 그날 고용 인원수}
    overtime_workers: dict           # {day(1-idx): {"17-18": 인원, "18-19": 인원}}
    line_activity: dict              # {line_id: [(day, slot_label, activity) ...]} (activity: "idle" / "setup:<product_id>" / "produce:<product_id>")
    order_fulfillment: dict          # {order_id: {"required":.., "produced":.., "deadline_day":.., "completion_day":.. or None}}
    continuity_score: int | None = None  # 2단계 목적함수 값(대기<->생산 전환횟수 + 셋업횟수). 작을수록 라인이 안 끊기고 오래 이어짐.


# ----------------------------------------------------------------------
# 모델 빌드 + 풀이
# ----------------------------------------------------------------------
def build_and_solve(lines: list[Line], orders: list[Order], config: ScheduleConfig) -> ScheduleResult:
    horizon_days = config.horizon_days
    T = horizon_days * SLOTS_PER_DAY

    for o in orders:
        if not (1 <= o.deadline_day <= horizon_days):
            raise ValueError(f"주문 {o.order_id}의 마감일({o.deadline_day})이 계획기간(1~{horizon_days})을 벗어났습니다.")

    model = cp_model.CpModel()

    line_ids = [l.line_id for l in lines]
    # 라인별로 그 라인에서 생산 가능한(rate>0) 주문 목록을 미리 뽑아둔다.
    # 이후 모든 루프에서 '호환되는 조합'에 대해서만 변수를 만들어서 불필요한
    # 변수 폭증을 막는다(라인 하나가 모든 제품을 만들 수 있는 게 아니므로).
    compat_orders_by_line: dict[str, list[Order]] = {
        lid: [o for o in orders if lid in o.compatible_lines()] for lid in line_ids
    }

    # ------------------------------------------------------------------
    # 변수 1: 슬롯별 활동(activity) - 대기 / 셋업(어느 제품) / 생산(어느 제품)
    #   각 (line, t) 슬롯은 셋 중 정확히 하나의 활동만 가능("라인은 시간당
    #   하나의 활동만 수행" 제약).
    # ------------------------------------------------------------------
    is_idle: dict[tuple[str, int], cp_model.IntVar] = {}
    is_setup: dict[tuple[str, int, str], cp_model.IntVar] = {}   # key: (line_id, t, order_id)
    is_prod: dict[tuple[str, int, str], cp_model.IntVar] = {}    # key: (line_id, t, order_id)

    # 변수 2: 슬롯 종료 시점 기준 '이 라인이 어느 제품으로 셋업된 상태인가'
    #   (setup/production이 아니라 idle을 지나도 마지막으로 셋업한 제품
    #   정보는 유지된다고 가정 - 즉 idle 후 같은 제품을 재개해도 셋업 불필요.
    #   이건 문제 설명에 명시돼 있진 않지만, "같은 제품이 이어지면 셋업 불요"
    #   라는 규칙을 가장 자연스럽게 확장한 가정이다).
    configured: dict[tuple[str, int, str], cp_model.IntVar] = {}
    configured_none: dict[tuple[str, int], cp_model.IntVar] = {}  # 아직 어떤 제품으로도 셋업된 적 없음(계획기간 시작 시 초기상태)

    # 생성되는 모든 불리언 결정변수를 한 군데 모아둔다. 2단계(연속성) 풀이를
    # 시작할 때 "1단계 해를 그대로 힌트로 준다"용으로 쓴다 (아래 build_and_solve
    # 하단의 model.AddHint 호출 참고).
    all_bool_vars: list[cp_model.IntVar] = []

    for lid in line_ids:
        compat_orders = compat_orders_by_line[lid]
        deadline_slot = {
            o.order_id: (o.deadline_day - 1) * SLOTS_PER_DAY + (SLOTS_PER_DAY - 1) for o in compat_orders
        }
        for t in range(T):
            idle_v = model.NewBoolVar(f"idle[{lid},{t}]")
            is_idle[lid, t] = idle_v
            activity_terms = [idle_v]
            all_bool_vars.append(idle_v)

            cnone = model.NewBoolVar(f"cfgNone[{lid},{t}]")
            configured_none[lid, t] = cnone
            cfg_terms = [cnone]
            all_bool_vars.append(cnone)

            for o in compat_orders:
                oid = o.order_id
                su = model.NewBoolVar(f"setup[{lid},{t},{oid}]")
                pr = model.NewBoolVar(f"prod[{lid},{t},{oid}]")
                is_setup[lid, t, oid] = su
                is_prod[lid, t, oid] = pr
                activity_terms += [su, pr]
                all_bool_vars += [su, pr]

                cfg = model.NewBoolVar(f"cfg[{lid},{t},{oid}]")
                configured[lid, t, oid] = cfg
                cfg_terms.append(cfg)
                all_bool_vars.append(cfg)

                # 그 주문의 마감일이 지난 슬롯에서는 그 제품에 대한
                # 생산/셋업을 아예 금지한다. (마감 이후 생산은 그 주문
                # 완료에 기여할 수 없으므로 비용만 낭비 - 탐색공간도 줄여줌)
                if t > deadline_slot[oid]:
                    model.Add(su == 0)
                    model.Add(pr == 0)

                # 그 날의 첫 슬롯(08-09시)에서는 셋업이 의미가 없다 - 제품이
                # 바뀌는 게 필요했다면 전날 밤에 이미 끝났을 일이기 때문
                # (아래 전이 제약 참고). 그래서 이 슬롯의 셋업 변수는 아예
                # 0으로 고정해 탐색공간에서 제외한다.
                #
                # 그 날의 마지막 슬롯(18-19시)에서의 셋업도 마찬가지로 항상
                # 손해다 - 그 시점에 제품을 바꿔봤자 그날은 더 생산할 시간이
                # 없고, 다음날 아침(day-start)은 어차피 셋업 없이 아무
                # 제품이나 바로 시작할 수 있으므로 굳이 전날 밤에 미리
                # 셋업할 이유가 없다(오히려 셋업을 다음날 새벽으로 미루면
                # 공짜다). 그래서 이 슬롯도 셋업을 아예 금지한다 - 셋업이
                # 공짜(인원 0)라서 솔버가 아무 의미 없이 야근시간에 셋업을
                # '채워넣는' 걸 막기 위한 하드 제약.
                local = t % SLOTS_PER_DAY
                if local == 0 or local == SLOTS_PER_DAY - 1:
                    model.Add(su == 0)

            # 슬롯 하나 = 대기/셋업(제품중 하나)/생산(제품중 하나) 중 정확히 1개
            model.Add(sum(activity_terms) == 1)
            # 셋업 상태도 '미설정' 또는 '특정 제품 1개로 설정' 중 정확히 1개
            model.Add(sum(cfg_terms) == 1)

    # ------------------------------------------------------------------
    # 전이(transition) 제약: 활동에 따라 '셋업 상태'가 어떻게 바뀌는지 정의.
    #
    #   핵심 규칙(사용자 확인 반영): 하루 일과(같은 날의 슬롯끼리) 안에서
    #   제품이 바뀔 때만 셋업 슬롯이 필요하다. 날짜가 바뀌는 시점(전날
    #   마지막 슬롯 -> 다음날 첫 슬롯)에 제품이 바뀌는 건 '전날 밤에 직원이
    #   미리 셋업해둘 수 있다'고 보고 셋업 슬롯 없이 곧바로 생산을 허용한다.
    #   그래서 각 라인/슬롯을 "그 날의 첫 슬롯(day-start)"과 "그 날의
    #   나머지 슬롯"으로 나눠서 서로 다른 전이 규칙을 적용한다.
    #
    #   [그 날의 나머지 슬롯 (local slot 1~9)] - 기존과 동일:
    #       * 생산(제품 p) 중이면: 직전 슬롯에 이미 p로 셋업되어 있어야
    #         하고("같은 제품 연속이면 셋업 불필요"), 이번 슬롯도 p로 유지.
    #       * 셋업(제품 p) 중이면: 이번 슬롯은 p로 새로 셋업됨.
    #       * 대기(idle) 중이면: 직전 슬롯의 셋업 상태를 그대로 유지.
    #
    #   [그 날의 첫 슬롯 (local slot 0, 매일 08-09시. 계획 첫날도 포함 -
    #     계획 시작 전날 밤에도 미리 셋업해둘 수 있다고 봄)]:
    #       * 생산(제품 p)을 곧바로 시작해도 됨 - 전날 밤 셋업 완료로 간주
    #         하여 직전 날 마지막 상태가 무엇이었는지는 확인하지 않는다.
    #       * 이 슬롯에서의 셋업(is_setup)은 의미가 없다(전날 밤에 이미
    #         끝났을 일이므로) - 애초에 변수를 만들지 않고 0으로 고정해서
    #         탐색공간에서 제외한다(아래 변수 생성부에서 처리).
    #       * 대기 중이면: 이전 날 마지막 상태를 그대로 유지(그날 첫
    #         슬롯에 아무 변경도 없었다는 뜻이므로).
    #
    #   두 경우 모두 '지금 이 활동이면 이 상태가 된다'는 정방향 함의만
    #   걸어주면 충분하다 - 각 t에서 '셋업 상태는 정확히 하나'라는 제약이
    #   이미 있어서, 특정 상태로 강제되는 순간 다른 상태들은 자동으로
    #   배제되기 때문이다(그래서 반대 방향 제약은 따로 필요 없음).
    # ------------------------------------------------------------------
    for lid in line_ids:
        compat_orders = compat_orders_by_line[lid]
        for t in range(T):
            is_day_start = (t % SLOTS_PER_DAY == 0)

            if is_day_start:
                for o in compat_orders:
                    oid = o.order_id
                    pr = is_prod[lid, t, oid]
                    cfg = configured[lid, t, oid]
                    # 전날 밤 셋업 완료로 간주 -> 직전 상태와 무관하게 곧바로 생산 가능.
                    model.Add(cfg == 1).OnlyEnforceIf(pr)
                if t == 0:
                    # 계획 첫날 이전엔 참조할 '전날'이 없으므로, 대기 중이면
                    # 아직 아무 것도 정해지지 않은 '미설정' 상태로 취급한다.
                    model.Add(configured_none[lid, 0] == is_idle[lid, 0])
                else:
                    idle_v = is_idle[lid, t]
                    model.Add(configured_none[lid, t] == configured_none[lid, t - 1]).OnlyEnforceIf(idle_v)
                    for o in compat_orders:
                        oid = o.order_id
                        model.Add(configured[lid, t, oid] == configured[lid, t - 1, oid]).OnlyEnforceIf(idle_v)
            else:
                for o in compat_orders:
                    oid = o.order_id
                    pr = is_prod[lid, t, oid]
                    su = is_setup[lid, t, oid]
                    cfg = configured[lid, t, oid]
                    prev_cfg = configured[lid, t - 1, oid]

                    # 생산하려면 직전 슬롯에 이미 이 제품으로 셋업되어 있어야 함.
                    model.Add(prev_cfg == 1).OnlyEnforceIf(pr)
                    # 생산 중이거나 방금 셋업했다면 -> 이번 슬롯 종료 시점엔 이 제품으로 설정된 상태.
                    model.Add(cfg == 1).OnlyEnforceIf(pr)
                    model.Add(cfg == 1).OnlyEnforceIf(su)

                    # (성능 최적화용 중복 제약) 이미 이 제품으로 설정돼 있는데
                    # 같은 제품으로 또 셋업하는 건 비용만 들고 아무 의미가
                    # 없으므로 아예 금지해서 탐색공간을 줄인다.
                    model.Add(su + prev_cfg <= 1)

                    # 핵심 제약: 셋업은 '실제로 그 제품을 생산하기 직전에만'
                    # 할 수 있다 - 셋업(oid) 다음 슬롯은 반드시 그 oid를
                    # 생산해야 한다(같은 제품이어야 함, 다른 제품 셋업이나
                    # 대기로 이어질 수 없음). 셋업이 인원 0이라 공짜라는
                    # 이유로 솔버가 "셋업 2연속"이나 "쓰지도 않을 제품으로
                    # 셋업" 같은 무의미한 조합을 만들지 못하게 막는 하드
                    # 제약이다. day-end 슬롯은 위에서 이미 셋업 자체를
                    # 금지했으므로(su=0 고정) t+1을 참조해도 되는 경우에만
                    # (=계획기간 전체의 마지막 슬롯이 아닐 때만) 건다 -
                    # 마지막 슬롯이 day-end라 su가 어차피 0으로 고정돼
                    # 있으므로 건너뛰어도 결과는 동일하다.
                    if t + 1 < T:
                        model.Add(is_prod[lid, t + 1, oid] == 1).OnlyEnforceIf(su)

                # 대기 중이면 직전 슬롯의 셋업 상태 전체를 그대로 유지.
                idle_v = is_idle[lid, t]
                model.Add(configured_none[lid, t] == configured_none[lid, t - 1]).OnlyEnforceIf(idle_v)
                for o in compat_orders:
                    oid = o.order_id
                    model.Add(configured[lid, t, oid] == configured[lid, t - 1, oid]).OnlyEnforceIf(idle_v)

    # ------------------------------------------------------------------
    # 생산량 집계 + 마감일 내 수량 충족 제약.
    #   각 슬롯 생산은 "완전 선형"이라 그 슬롯 동안 rate[line]만큼 정확히
    #   생산된다(시작손실/비선형 없음 가정을 그대로 반영).
    # ------------------------------------------------------------------
    produced_qty: dict[str, cp_model.IntVar] = {}
    for o in orders:
        oid = o.order_id
        compat = o.compatible_lines()
        if not compat:
            raise ValueError(f"주문 {oid}({o.product_id})를 생산할 수 있는 라인이 없습니다(rate가 전부 0/미지정).")

        deadline_slot = (o.deadline_day - 1) * SLOTS_PER_DAY + (SLOTS_PER_DAY - 1)
        eligible_slots = min(deadline_slot + 1, T)
        max_rate_sum = sum(int(round(o.rate[lid])) for lid in compat)
        upper_bound = max_rate_sum * eligible_slots + 1  # 이론상 최대 생산량(모든 호환라인이 마감일까지 이 제품만 생산) + 여유 1

        terms = []
        for lid in compat:
            rate_val = int(round(o.rate[lid]))
            if rate_val <= 0:
                continue
            for t in range(eligible_slots):
                terms.append((rate_val, is_prod[lid, t, oid]))

        pv = model.NewIntVar(0, max(upper_bound, o.quantity), f"produced[{oid}]")
        model.Add(pv == sum(coef * var for coef, var in terms))
        model.Add(pv >= o.quantity)  # 마감일 전까지 필요수량 이상 생산(시간 슬롯 단위라 약간의 초과생산은 허용)
        produced_qty[oid] = pv

    # ------------------------------------------------------------------
    # 인원 수요 집계: 슬롯별로 '그 순간 라인들에 배치되어 있어야 하는
    # 인원 합계'.
    #
    #   생산 중인 라인만 라인 작업자(line worker) 인원을 필요로 한다.
    #   셋업(하루 일과 중 제품이 바뀌어서 발생하는, 위 전이 제약 참고)은
    #   별도 인력이 처리하는 작업이라 그 시간 동안 이 라인에 투입되는
    #   생산 인원수는 0으로 본다 - 그래서 아래 집계에서 is_setup은 아예
    #   빼고 is_prod만 인원 수요에 반영한다. (참고: 야간 셋업은 애초에
    #   셋업 슬롯 자체가 생기지 않으므로 - 위 전이 제약 참고 - 별도 처리가
    #   필요 없다.)
    # ------------------------------------------------------------------
    max_possible_workers = sum(
        o.workers.get(lid, 0) for lid in line_ids for o in compat_orders_by_line[lid]
    )
    max_possible_workers = max(max_possible_workers, 1)

    total_workers_var: dict[int, cp_model.IntVar] = {}
    for t in range(T):
        terms = []
        for lid in line_ids:
            for o in compat_orders_by_line[lid]:
                oid = o.order_id
                w = int(o.workers.get(lid, 0))
                if w <= 0:
                    continue
                terms.append((w, is_prod[lid, t, oid]))
        tv = model.NewIntVar(0, max_possible_workers, f"totalWorkers[{t}]")
        model.Add(tv == sum(coef * var for coef, var in terms))
        total_workers_var[t] = tv

    # ------------------------------------------------------------------
    # 일별 고용 인원("그날 슬롯들 중 동시 필요인원의 최댓값") + 잔업 인원.
    # ------------------------------------------------------------------
    daily_workforce_var: dict[int, cp_model.IntVar] = {}
    for d in range(horizon_days):
        day_slots = [total_workers_var[d * SLOTS_PER_DAY + s] for s in range(SLOTS_PER_DAY)]
        dv = model.NewIntVar(0, max_possible_workers, f"dailyWorkforce[{d}]")
        model.AddMaxEquality(dv, day_slots)
        daily_workforce_var[d] = dv

    # ------------------------------------------------------------------
    # 목적함수: 일일 정액임금 합 + 잔업수당 합 (최소화).
    #   금액은 MONEY_SCALE 배율로 정수화해서 다루고, 최종 리포트에서 다시
    #   나눠서 원래 단위로 보여준다.
    # ------------------------------------------------------------------
    daily_wage_scaled = int(round(config.daily_wage * MONEY_SCALE))
    ot_hour_wage_scaled = int(round(config.resolved_hourly_wage() * config.overtime_multiplier * MONEY_SCALE))

    objective_terms = []
    for d in range(horizon_days):
        objective_terms.append(daily_workforce_var[d] * daily_wage_scaled)
        for s in OVERTIME_LOCAL_SLOTS:
            objective_terms.append(total_workers_var[d * SLOTS_PER_DAY + s] * ot_hour_wage_scaled)
    model.Minimize(sum(objective_terms))

    def snapshot(solver: cp_model.CpSolver) -> dict:
        """방금 성공적으로 풀린(OPTIMAL/FEASIBLE) solver 상태에서 필요한
        모든 변수값을 순수 파이썬 dict/int로 복사해둔다.

        왜 필요한가: CP-SAT는 Solve()가 OPTIMAL/FEASIBLE을 반환했을 때만
        solver.Value()로 해를 읽을 수 있다. 이 스크립트는 2단계(연속성)
        풀이를 추가로 돌리는데, 만약 2단계가 시간 안에 어떤 해도 못 찾으면
        (UNKNOWN) solver 내부엔 더 이상 유효한 해가 없다. 이 상태에서
        solver.Value()를 계속 호출하면 값이 뒤죽박죽 나오는 정도가 아니라
        네이티브(C++) 레벨에서 크래시(세그폴트)가 날 수 있다는 걸 실제로
        확인했다. 그래서 매 성공적인 Solve() 직후에 필요한 값을 전부
        일반 파이썬 객체로 복사해두고, 이후 로직은 절대 solver.Value()를
        다시 부르지 않고 이 스냅샷만 사용한다.
        """
        return {
            "is_idle": {k: solver.Value(v) for k, v in is_idle.items()},
            "is_setup": {k: solver.Value(v) for k, v in is_setup.items()},
            "is_prod": {k: solver.Value(v) for k, v in is_prod.items()},
            "produced_qty": {k: solver.Value(v) for k, v in produced_qty.items()},
            "total_workers": {t: solver.Value(v) for t, v in total_workers_var.items()},
            "daily_workforce": {d: solver.Value(v) for d, v in daily_workforce_var.items()},
        }

    # ------------------------------------------------------------------
    # 1단계 풀이: 인건비 최소화.
    # ------------------------------------------------------------------
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = config.time_limit_seconds
    solver.parameters.num_search_workers = config.num_search_workers
    solver.parameters.log_search_progress = config.log_progress
    solver.parameters.random_seed = config.random_seed  # 재현성: 같은 입력이면 같은 탐색 경로로 같은 해가 나오게.
    status = solver.Solve(model)
    status_name = solver.StatusName(status)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return ScheduleResult(
            status_name=status_name,
            is_feasible=False,
            total_cost=None,
            daily_workforce={},
            overtime_workers={},
            line_activity={},
            order_fulfillment={},
        )

    best_cost_scaled = int(round(solver.ObjectiveValue()))
    best_snapshot = snapshot(solver)  # 1단계 해를 즉시 안전하게 복사해둔다 (아래 참고 이유).
    all_bool_vals = [solver.Value(v) for v in all_bool_vars]  # 2단계 힌트용 - 역시 1단계 직후(안전할 때) 미리 복사.
    continuity_score = None

    # ------------------------------------------------------------------
    # 2단계 풀이(연속성 최적화, config.optimize_continuity=True일 때만):
    #   1단계에서 찾은 인건비를 '그대로 유지'하는 제약을 걸고(비용을 한 푼도
    #   더 쓰지 않음), 그 안에서 각 라인이 대기<->생산을 왔다갔다하는
    #   전환 횟수 + 셋업 횟수의 합("연속성 점수")을 추가로 최소화한다.
    #   즉 "비용이 같다면 사람이 한 라인에 최대한 오래 붙어있는 스케줄을
    #   우선한다"는 타이브레이크를, 실제 비용을 절대 희생하지 않는 방식으로
    #   정확히 구현한 것(순수 lexicographic 2단계 최적화).
    #   - 대기<->생산 전환: sw >= |idle[t]-idle[t-1]| 형태의 부등식 두 개만
    #     걸어두면, 목적함수가 sw를 최소화하려 하므로 자동으로 정확한
    #     XOR 값(0 또는 1)으로 수렴한다(등호 제약을 따로 안 걸어도 됨).
    #     대기 상태 하나를 거쳐서 원래 하던 일로 돌아오면(생산->대기->생산)
    #     '들어감'과 '나옴' 두 번의 전환이 잡혀서 2점이 된다.
    #   - 셋업 횟수: is_setup 변수를 전부 더한 값 = 전체 기간 동안 발생한
    #     제품 전환(셋업) 총 횟수. 셋업(Y) 다음 슬롯은 항상 그 제품(Y)
    #     생산으로 이어지도록 하드 제약을 걸어뒀기 때문에(위 전이 제약
    #     참고), 이 값은 정확히 '실제로 발생한 제품 전환 이벤트 수'와
    #     같다. 다만 대기<->생산 전환처럼 셋업도 "생산 중이던 라인이
    #     하던 일을 멈추고, 다른 걸 준비해서, 다시 생산으로 돌아오는"
    #     동일한 성격의 사건이므로(대기가 '들어감+나옴' 2점인 것과
    #     대칭적으로), 셋업 1회도 2점으로 계산한다(가중치 2배). 그래서
    #     생산(A)->대기->생산(A)(2점)와 생산(A)->셋업(B)->생산(B)(2점)가
    #     이제 동등하게 취급되고, 대기 없이 곧바로 셋업으로 넘어가는 쪽이
    #     여전히 더 싸다(대기까지 거치면 셋업 2점 + 대기 2점 = 4점).
    #   - AddHint로 1단계 해를 2단계 탐색의 출발점으로 알려준다. 1단계 해는
    #     이미 '비용 <= best_cost_scaled' 제약을 등호로 만족하므로 2단계
    #     모델에서도 즉시 실행가능(feasible)하다 - 그래서 힌트를 주면 거의
    #     항상 몇 초 안에 최소 하나의 해(=1단계 해 자체)를 확보하고 시작하게
    #     되어, 시간 안에 해를 하나도 못 찾는 상황(위에서 크래시로 이어졌던
    #     그 상황)을 사실상 방지한다.
    # ------------------------------------------------------------------
    if config.optimize_continuity:
        model.Add(sum(objective_terms) <= best_cost_scaled)

        switch_terms = []
        for lid in line_ids:
            for t in range(1, T):
                sw = model.NewBoolVar(f"idleSwitch[{lid},{t}]")
                model.Add(sw >= is_idle[lid, t] - is_idle[lid, t - 1])
                model.Add(sw >= is_idle[lid, t - 1] - is_idle[lid, t])
                switch_terms.append(sw)
        setup_terms = list(is_setup.values())
        model.Minimize(sum(switch_terms) + 2 * sum(setup_terms))

        for var, val in zip(all_bool_vars, all_bool_vals):
            model.AddHint(var, val)

        solver.parameters.max_time_in_seconds = config.secondary_time_limit_seconds
        status2 = solver.Solve(model)

        if status2 in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            continuity_score = int(round(solver.ObjectiveValue()))
            status_name = f"{status_name} + 2단계:{solver.StatusName(status2)}"
            best_snapshot = snapshot(solver)  # 2단계 해로 교체 (이때부터는 이 해로 최종 결과를 만든다).
        else:
            print("[경고] 2단계(연속성) 최적화가 시간 내에 해를 못 찾아 1단계 결과를 그대로 사용합니다.")

    # ------------------------------------------------------------------
    # 결과 추출 (전부 best_snapshot에서만 읽는다 - solver.Value()를 여기서
    # 다시 호출하지 않는다. 위 snapshot()의 이유 설명 참고).
    # ------------------------------------------------------------------
    total_cost_scaled = sum(
        best_snapshot["daily_workforce"][d] * daily_wage_scaled for d in range(horizon_days)
    ) + sum(
        best_snapshot["total_workers"][d * SLOTS_PER_DAY + s] * ot_hour_wage_scaled
        for d in range(horizon_days)
        for s in OVERTIME_LOCAL_SLOTS
    )
    total_cost = total_cost_scaled / MONEY_SCALE

    daily_workforce = {d + 1: best_snapshot["daily_workforce"][d] for d in range(horizon_days)}
    overtime_workers = {
        d + 1: {
            "17-18": best_snapshot["total_workers"][d * SLOTS_PER_DAY + OVERTIME_LOCAL_SLOTS[0]],
            "18-19": best_snapshot["total_workers"][d * SLOTS_PER_DAY + OVERTIME_LOCAL_SLOTS[1]],
        }
        for d in range(horizon_days)
    }

    line_activity: dict[str, list[tuple[int, str, str]]] = {}
    for lid in line_ids:
        compat_orders = compat_orders_by_line[lid]
        entries = []
        for t in range(T):
            day = t // SLOTS_PER_DAY + 1
            slot_label = SLOT_LABELS[t % SLOTS_PER_DAY]
            activity = "idle"
            for o in compat_orders:
                oid = o.order_id
                if best_snapshot["is_prod"][lid, t, oid] == 1:
                    activity = f"produce:{o.product_id}"
                    break
                if best_snapshot["is_setup"][lid, t, oid] == 1:
                    activity = f"setup:{o.product_id}"
                    break
            entries.append((day, slot_label, activity))
        line_activity[lid] = entries

    order_fulfillment = {}
    for o in orders:
        oid = o.order_id
        compat = o.compatible_lines()
        # 마감일 완료 여부와 별개로, '언제 누적 생산량이 필요수량에 처음
        # 도달했는지'(완료일)를 슬롯 단위로 다시 훑어서 계산한다.
        cumulative = 0
        completion_day = None
        for t in range(T):
            for lid in compat:
                if best_snapshot["is_prod"][lid, t, oid] == 1:
                    cumulative += int(round(o.rate[lid]))
            if completion_day is None and cumulative >= o.quantity:
                completion_day = t // SLOTS_PER_DAY + 1
        order_fulfillment[oid] = {
            "product_id": o.product_id,
            "required": o.quantity,
            "produced": best_snapshot["produced_qty"][oid],
            "deadline_day": o.deadline_day,
            "completion_day": completion_day,
        }

    return ScheduleResult(
        status_name=status_name,
        is_feasible=True,
        total_cost=total_cost,
        daily_workforce=daily_workforce,
        overtime_workers=overtime_workers,
        line_activity=line_activity,
        order_fulfillment=order_fulfillment,
        continuity_score=continuity_score,
    )


# ----------------------------------------------------------------------
# 결과 리포트 (콘솔 출력 + CSV 저장 + 간트 차트 PNG)
# ----------------------------------------------------------------------
def _compress_runs(entries: list[tuple[int, str, str]]) -> list[tuple[int, str, int, str, str]]:
    """(day, slot_label, activity) 슬롯별 리스트를, 연속으로 같은 activity가
    이어지는 구간을 하나로 뭉쳐서 (시작일, 시작슬롯, 끝일, 끝슬롯, activity)
    형태로 압축한다. 300줄짜리 슬롯 로그를 사람이 읽을 수 있는 요약으로
    바꾸기 위한 용도(요청받은 적은 없지만, 콘솔에 300줄을 그대로 뿌리면
    아무도 못 읽으므로 가독성을 위해 추가).
    """
    if not entries:
        return []
    runs = []
    start_day, start_slot, cur_activity = entries[0]
    prev_day, prev_slot = start_day, start_slot
    for day, slot_label, activity in entries[1:]:
        if activity != cur_activity:
            runs.append((start_day, start_slot, prev_day, prev_slot, cur_activity))
            start_day, start_slot, cur_activity = day, slot_label, activity
        prev_day, prev_slot = day, slot_label
    runs.append((start_day, start_slot, prev_day, prev_slot, cur_activity))
    return runs


def print_report(result: ScheduleResult, orders: list[Order]):
    print(f"[결과] solver 상태: {result.status_name}")
    if not result.is_feasible:
        print("[결과] 실행 가능한 스케줄을 찾지 못했습니다. 마감일/라인수/인원 데이터를 확인하세요.")
        return

    print(f"[결과] 총 인건비: {result.total_cost:,.0f}")
    if result.continuity_score is not None:
        print(f"[결과] 연속성 점수(대기<->생산 전환 + 셋업 횟수, 작을수록 좋음): {result.continuity_score}")

    print("\n[결과] 주문별 이행 현황:")
    for o in orders:
        f = result.order_fulfillment[o.order_id]
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
        for sd, ss, ed, es, activity in runs[:8]:
            print(f"    {sd}일 {ss} ~ {ed}일 {es} : {activity}")
        if len(runs) > 8:
            print(f"    ... (총 {len(runs)}개 구간, 나머지는 CSV 참고)")


def save_outputs(result: ScheduleResult, orders: list[Order], output_dir: str):
    os.makedirs(output_dir, exist_ok=True)
    if not result.is_feasible:
        return

    import pandas as pd

    # 1) 라인별 슬롯 단위 전체 스케줄 (가장 상세한 원본 데이터)
    rows = []
    for lid, entries in result.line_activity.items():
        for day, slot_label, activity in entries:
            kind, _, product = activity.partition(":")
            rows.append({"line_id": lid, "day": day, "slot": slot_label, "activity": kind, "product_id": product})
    schedule_df = pd.DataFrame(rows)
    schedule_csv = os.path.join(output_dir, "line_schedule.csv")
    schedule_df.to_csv(schedule_csv, index=False, encoding="utf-8-sig")

    # 2) 라인별 압축된(연속 구간) 스케줄 - 사람이 보기 편한 버전
    run_rows = []
    for lid, entries in result.line_activity.items():
        for sd, ss, ed, es, activity in _compress_runs(entries):
            kind, _, product = activity.partition(":")
            run_rows.append(
                {"line_id": lid, "start_day": sd, "start_slot": ss, "end_day": ed, "end_slot": es,
                 "activity": kind, "product_id": product}
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

    line_ids = list(result.line_activity.keys())
    T = config.horizon_days * SLOTS_PER_DAY
    product_ids = sorted({o.product_id for o in orders})
    # 0=idle, 1=setup(공통 회색), 2..=제품별 생산 색상
    product_index = {pid: i + 2 for i, pid in enumerate(product_ids)}

    grid = np.zeros((len(line_ids), T), dtype=int)
    for r, lid in enumerate(line_ids):
        for t, (day, slot_label, activity) in enumerate(result.line_activity[lid]):
            if activity == "idle":
                grid[r, t] = 0
            elif activity.startswith("setup:"):
                grid[r, t] = 1
            else:
                pid = activity.split(":", 1)[1]
                grid[r, t] = product_index[pid]

    n_colors = 2 + len(product_ids)
    base_colors = ["#f2f2f2", "#9e9e9e"] + list(plt.get_cmap("tab20").colors[: len(product_ids)])
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
    deadlines_by_day: dict[int, list[str]] = {}
    for o in orders:
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
    for pid in product_ids:
        legend_items.append(Patch(facecolor=base_colors[product_index[pid]], label=f"생산: {pid}"))
    legend_items.append(Line2D([0], [0], color="red", linestyle="--", linewidth=1.3, label="마감일"))
    ax.legend(handles=legend_items, bbox_to_anchor=(1.02, 1), loc="upper left", fontsize=8)

    fig.tight_layout()
    p = os.path.join(output_dir, "gantt_overview.png")
    fig.savefig(p, dpi=150)
    plt.close(fig)
    print(f"  - {p}")


# ----------------------------------------------------------------------
# 예시(demo) 데이터
# ----------------------------------------------------------------------
def build_example_instance() -> tuple[list[Line], list[Order]]:
    """마스크/용기/튜브 3개 제품군에 걸친 작은 예시 인스턴스.
    line_mask_3만 물리 설비 2대(line_mask_3_1, line_mask_3_2)로 구성해서
    '동일 라인타입의 여러 대 = 독립 라인' 요구사항을 보여준다.
    실제 데이터로 돌리려면 --data로 이런 구조의 JSON을 넣으면 된다
    (main()의 load_data_from_json 참고).
    """
    lines = expand_line_types(
        [
            ("line_mask_1", "mask", 1),       # 로타리형: 속도 빠름, 인원 多
            ("line_mask_2", "mask", 1),       # 단발형: 속도 느림, 인원 少
            ("line_mask_3", "mask", 2),       # 셀라인형: 동일 설비 2대
            ("line_container_1", "container", 2),
            ("line_tube_1", "tube", 1),
        ]
    )

    orders = [
        Order(
            order_id="O1", product_id="MASK_A", category="mask",
            quantity=50_000, deadline_day=10,
            rate={"line_mask_1": 800, "line_mask_3_1": 600, "line_mask_3_2": 600},
            workers={"line_mask_1": 6, "line_mask_3_1": 4, "line_mask_3_2": 4},
        ),
        Order(
            order_id="O2", product_id="MASK_B", category="mask",
            quantity=30_000, deadline_day=15,
            rate={"line_mask_2": 500},
            workers={"line_mask_2": 3},
        ),
        Order(
            order_id="O3", product_id="MASK_C", category="mask",
            quantity=80_000, deadline_day=25,
            rate={"line_mask_1": 800, "line_mask_3_1": 600, "line_mask_3_2": 600},
            workers={"line_mask_1": 6, "line_mask_3_1": 4, "line_mask_3_2": 4},
        ),
        Order(
            order_id="O4", product_id="CONTAINER_A", category="container",
            quantity=40_000, deadline_day=12,
            rate={"line_container_1_1": 700, "line_container_1_2": 700},
            workers={"line_container_1_1": 5, "line_container_1_2": 5},
        ),
        Order(
            order_id="O5", product_id="CONTAINER_B", category="container",
            quantity=20_000, deadline_day=20,
            rate={"line_container_1_1": 700, "line_container_1_2": 700},
            workers={"line_container_1_1": 5, "line_container_1_2": 5},
        ),
        Order(
            order_id="O6", product_id="TUBE_A", category="tube",
            quantity=15_000, deadline_day=8,
            rate={"line_tube_1": 400},
            workers={"line_tube_1": 4},
        ),
    ]
    return lines, orders


def load_data_from_json(path: str) -> tuple[list[Line], list[Order]]:
    """--data로 넘긴 JSON 파일을 읽어 Line/Order 목록으로 변환한다.

    JSON 형식:
      {
        "lines": [{"line_id": "...", "category": "mask"}, ...],
        "orders": [
          {"order_id": "...", "product_id": "...", "category": "mask",
           "quantity": 10000, "deadline_day": 12,
           "rate": {"line_id": 시간당수량, ...},
           "workers": {"line_id": 필요인원, ...}},
          ...
        ]
      }
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    lines = [Line(**l) for l in data["lines"]]
    orders = [Order(**o) for o in data["orders"]]
    return lines, orders


# ----------------------------------------------------------------------
# 진입점
# ----------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="30일 생산 스케줄링 최적화 (CP-SAT)")
    parser.add_argument("--data", default=None, help="라인/주문 데이터를 담은 JSON 파일 경로 (없으면 내장 예시 데이터 사용)")
    parser.add_argument("--horizon-days", type=int, default=30)
    parser.add_argument("--daily-wage", type=float, default=200_000, help="1인 1일 고용 정액임금")
    parser.add_argument("--hourly-wage", type=float, default=None, help="잔업수당 계산용 시급 (미지정시 daily-wage/8)")
    parser.add_argument("--overtime-multiplier", type=float, default=1.5)
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
