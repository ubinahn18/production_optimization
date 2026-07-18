# -*- coding: utf-8 -*-
"""
scheduling/models.py

시간 구조 상수 + 입력 데이터(Line, Order) / 설정(ScheduleConfig) /
결과(ScheduleResult) 데이터클래스. 이 모듈은 CP-SAT나 다른 무거운
의존성 없이 순수 파이썬만 쓰므로, solver.py나 report.py 어디서든
가볍게 import해서 쓸 수 있다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

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
    """생산라인 "타입" 하나 - line_type_id는 물리적으로 유일한 라인을
    가리키는 게 아니라 "이런 종류의 설비"를 가리키는 이름이다(예:
    "셀라인"). 물리적으로 동일한 설비가 여러 대 있으면(예: 셀라인이
    3대) 인스턴스를 여러 개 만드는 게 아니라 count에 그 대수를 직접
    적는다 - 동일 설비는 어차피 rate/workers가 모두 같으므로
    Order.rate/workers에도 이 line_type_id 하나로 한 번만 값을 적으면
    된다(물리 라인 개수(count)만큼 값을 중복 입력할 필요가 없다).

    실제로 유일한 물리 라인 식별자(리포트/CSV에 찍히는 진짜 line_id,
    예: "셀라인_1", "셀라인_2")는 count>1일 때 solver가 내부적으로
    ("{line_type_id}_1".."{line_type_id}_{count}") 붙여서 생성한다
    (scheduling/pooling.py의 build_line_pools, LinePool.line_ids 참고)
    - 입력 데이터 단계에서는 신경 쓸 필요 없다. line_type_id 자체는
    "타입" 이름일 뿐 어느 물리 라인 하나를 가리키지 않으므로 그 자체로는
    유일하지 않다는 점에 주의(count>1이면 여러 물리 라인이 이 타입을
    공유함).
    """

    line_type_id: str
    count: int = 1  # 이 타입의 물리 설비 대수. 1이면 그냥 라인 하나.


@dataclass
class Order:
    """생산 주문 하나 = 스케줄링 대상 제품 하나.

    이 프로토타입에서는 '제품 1개 = 주문 1개'로 단순화했다. 만약 실제로
    같은 물리적 제품을 마감일이 다른 여러 주문으로 나눠 받는 경우라면,
    product_id는 같게 두고 order_id만 다르게 줘서 별도 Order로 등록하면
    된다(스케줄링 로직 상으로는 각 주문을 별개 제품처럼 다뤄도 무방하다).

    deadline_day=None(ASAP 주문): 수주 시점에 납기가 정해지지 않고 "만드는
    대로 순차 출고"하는 제품군을 위한 모드. 이 경우 하드 마감 제약(반드시
    그 날짜까지 다 만들어야 함) 대신, 매일 남은 미생산분("backlog")에
    비용을 매겨서 목적함수에 더한다(backlog_cost_per_unit_per_day 참고).
    그러면 "사람을 더 써서 당길지 vs 천천히 만들지"를 솔버가 비용으로
    직접 비교해서 결정한다 - 하드 데드라인이 없다고 그냥 무시되거나,
    반대로 억지로 하드 데드라인을 넣어서 다른 진짜 마감일 있는 주문들과
    부자연스럽게 경쟁하는 것 둘 다를 피하기 위함.
    """

    order_id: str
    product_id: str
    category: str
    quantity: int                    # 필요 총 생산수량
    product_name: str = ""  # 원본 품명. 특정 발주처+품명 조합에 예외 라인을
                             # 배정하는 등 category만으로는 구분 안 되는 케이스에 사용(예: plan_from_orders.py의 셀바이오 예외).
    vendor: str = ""         # 발주처(원본 '발주처' 열). 위 product_name과 같은 용도.
    deadline_day: int | None = None  # 1-indexed. 이 날짜의 마지막 슬롯(18-19시)까지 완료되어야 함.
                                      # None이면 ASAP(마감 없음) - 아래 backlog_cost_per_unit_per_day 참고.
    earliest_start_day: int | None = None
        # 1-indexed. 부자재(포장재 등) 입고일처럼 "이 날짜 전에는 아예
        # 생산 자체가 불가능한" 제약이 있을 때 쓴다. 이 날짜의 첫
        # 슬롯(08-09시)부터 생산/셋업이 가능해진다 - 그 전 슬롯들은
        # solver.py에서 deadline_day 이후를 막는 것과 대칭으로 해당
        # 주문의 생산/셋업을 전부 0으로 강제한다. None이면 제약
        # 없음(1일차부터 바로 생산 가능, 지금까지의 기본 동작과 동일).
    backlog_cost_per_unit_per_day: float | None = None
        # ASAP 주문(deadline_day=None)에서만 쓰인다. 하루 지날 때마다 아직
        # 못 만든 수량 1개당 이 비용이 목적함수에 더해진다. None이면
        # ScheduleConfig.default_backlog_cost_per_unit_per_day를 대신 쓴다
        # (전역 기본값 하나로 퉁치고, 필요한 주문만 여기서 개별 조정).
    rate: dict = field(default_factory=dict)     # {line_type_id: 시간당 생산수량}. 없거나 0이면 그 라인 타입에서 생산 불가.
    workers: dict = field(default_factory=dict)  # {line_type_id: 그 라인 타입에서 이 제품 생산/셋업에 필요한 인원}

    # 아래는 전부 스케줄링 로직(CP-SAT)에서는 안 쓰고 plan_report.py의
    # "주문별 상세" 리포트에 원본 근거를 그대로 보여주기 위해서만 들고
    # 다니는 필드다(product_name/vendor와 같은 용도) - data_pipeline/
    # orders_from_excel.py가 엑셀에서 읽은 값을 그대로 채워 넣는다.
    content_inspection: bool = False   # '내용물검사' 열이 'y'인지
    finished_inspection: bool = False  # '완제품검사' 열이 'y'인지
    submaterial_date: date | None = None    # '부자재입고예정일' 원본 날짜(여유일 더하기 전)
    raw_material_date: date | None = None   # '원료입고예정일' 원본 날짜(여유일 더하기 전)
    raw_deadline_date: date | None = None   # 엑셀 납기 원본 날짜(생산 lead days 빼기 전). None이면 ASAP.

    def compatible_line_types(self) -> list[str]:
        return [type_id for type_id, r in self.rate.items() if r and r > 0]

    def is_asap(self) -> bool:
        return self.deadline_day is None


@dataclass
class LinePool:
    """Line 하나(그 line_type_id의 count대 전체)에 대응하는, 풀이에
    필요한 정보를 다 모아둔 구조. line_ids가 이 풀에 속한 실제 물리
    라인들의 진짜(유일한) 식별자 목록이다(예: ["셀라인_1", "셀라인_2",
    "셀라인_3"]) - Line.line_type_id("셀라인")와 달리 이 목록의 각
    원소는 물리적으로 유일한 라인 하나씩을 정확히 가리킨다.
    scheduling/pooling.py의 build_line_pools()가 Line/Order로부터 만든다.
    물리 라인이 하나뿐이면(count=1) k=1짜리 풀이 된다. 라인별로 변수를
    따로 만드는 대신 "이 슬롯에 이 그룹의 몇 대가 무슨 상태인가"라는
    집계 정수 변수로 모델링하면(scheduling/solver.py 참고), 동일 라인이
    여러 대 있을 때 생기는 symmetry(어느 물리 카피가 뭘 하든 목적함수상
    동등해서, 대수가 많아지면(예: 19대) CP-SAT이 최적성 증명에 엄청난
    시간을 쓰게 되는 문제)가 애초에 생기지 않는다.
    """

    line_ids: list[str]
    compat_order_ids: list[str]
    rate: dict[str, float]      # order_id -> 시간당 생산수량 (풀 내 모든 라인에 동일)
    workers: dict[str, int]     # order_id -> 필요인원 (풀 내 모든 라인에 동일)

    @property
    def k(self) -> int:
        return len(self.line_ids)


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

    # ASAP 주문(Order.deadline_day=None)의 하루당 미생산 1개당 지연비용
    # 기본값(원). 실제 계약 페널티/재고비용 수치가 없어서 일단 상식적인
    # 크기로 잡아둔 값이다 - 대략 "일반적인 라인 1대를 하루 더 돌려서
    # 당길 만한 가치가 있는지"를 솔버가 판단할 수 있을 정도의 스케일로
    # 골랐다(하루 인건비 20만원 대비, 시간당 수백개씩 만드는 제품이면
    # 개당 100원/일은 하루이틀 지연엔 별 영향 없지만 몇 주씩 밀리면
    # 무시 못할 크기로 쌓인다). 실제 값은 나중에 얼마든지 바꿔서 쓰면
    # 된다 - --backlog-cost CLI 옵션이나 Order.backlog_cost_per_unit_per_day로
    # 전역/주문별로 둘 다 조정 가능.
    default_backlog_cost_per_unit_per_day: float = 20.0

    # 제품군(Order.category)별 재고 보관비용(원/개/일). 마감일이 있는
    # 주문에서 실제로 그 슬롯에 생산된 수량은 (마감일 - 그 슬롯이 속한
    # 날짜)일만큼 미리 만들어져 재고로 쌓여있다가 마감일에 쓰인다고 보고,
    # "생산량 x 그 날수"에 이 비용을 곱해 목적함수에 더한다(너무 일찍
    # 만들어서 오래 쌓아두는 걸 억제하는 효과). 이 dict에 없는 category는
    # default_storage_cost_per_unit_per_day를 대신 쓴다. ASAP
    # 주문(deadline_day=None)은 "마감일까지 며칠 이른지" 자체를 정의할 수
    # 없어서 애초에 대상에서 제외한다(늦어지는 쪽 페널티는
    # backlog_cost_per_unit_per_day로 이미 별도 처리됨).
    storage_cost_by_category: dict[str, float] = field(default_factory=dict)
    # storage_cost_by_category에 없는 category에 적용할 기본 보관비용.
    # 기본값 0.0 = 아무것도 설정하지 않으면 이 목적함수 항이 항상 0이라
    # 기존 동작과 100% 동일하게 유지된다.
    default_storage_cost_per_unit_per_day: float = 0.0

    # 생산/셋업이 금지되는 날(day index, 1-based, deadline_day와 동일한
    # 축 - reference_date를 1일차로 함). 주말/공휴일 등. solver는 날짜를
    # 전혀 모르고 이 숫자 집합만 본다 - 실제 (기준일 + 휴무일 목록) ->
    # day index 변환은 plan_from_orders.py의 resolve_closed_days()가 담당.
    closed_days: frozenset[int] = field(default_factory=frozenset)

    def resolved_hourly_wage(self) -> float:
        return self.hourly_wage if self.hourly_wage is not None else self.daily_wage / 8.0


# ----------------------------------------------------------------------
# 결과 구조
# ----------------------------------------------------------------------
@dataclass
class ScheduleResult:
    status_name: str
    is_feasible: bool
    total_cost: float | None         # labor_cost + backlog_cost + storage_cost. 1단계 목적함수 값과 동일(비교/최적화 기준용).
    daily_workforce: dict            # {day(1-idx): 그날 고용 인원수}
    overtime_workers: dict           # {day(1-idx): {"17-18": 인원, "18-19": 인원}}
    line_activity: dict              # {line_id: [(day, slot_label, activity, order_id) ...]} (activity: "idle" / "setup:<product_id>" / "produce:<product_id>"; order_id는 idle이면 "", 아니면 그 activity를 발생시킨 주문. 여러 주문이 같은 product_id를 공유할 수 있어서(예: "코드확인중" 같은 placeholder) product_id만으로는 주문을 구분 못 할 수 있음 - order_id로 구분)
    order_fulfillment: dict          # {order_id: {"required":.., "produced":.., "deadline_day":.., "completion_day":.. or None}}
    continuity_score: int | None = None  # 2단계 목적함수 값(대기<->생산 전환횟수 + 셋업횟수). 작을수록 라인이 안 끊기고 오래 이어짐.
    labor_cost: float | None = None      # 실제로 지급되는 돈: 일일 정액임금 합 + 잔업수당 합 (backlog 비용 제외).
    backlog_cost: float | None = None    # ASAP 주문 backlog 페널티 합 - 실제 지급되는 돈이 아니라 "늦게 끝낼수록 손해"를 모델링하기 위한 가상 비용.
    storage_cost: float | None = None    # 마감일 있는 주문의 재고 보관비용 합(storage_cost_by_category 참고) - 실제 지출이 아니라 "너무 일찍 만들어 오래 쌓아두는 것"을 억제하기 위한 가상 비용.
