# -*- coding: utf-8 -*-
"""
scheduling/pooling.py

Line(타입 하나 + count대)로부터 풀이에 필요한 LinePool을 만드는
build_line_pools()와, solver.py가 그 풀을 "집계 카운트" 방식으로 풀고 난
뒤 그 해를 다시 물리 라인별 스케줄로 복원하는 reconstruct_physical_schedule()을
제공한다.

왜 필요한가: 동일 라인이 k대 있으면, 겹치지 않는 시간대의 작업을 k대 중
어디에 배정하든 비용/연속성 목적함수 값은 완전히 동일하다(symmetry). k가
크면(예: 19대) CP-SAT이 이 symmetry 때문에 "이게 최적이다"를 증명하는 데
막대한 시간을 쓰게 된다. solver.py는 이 문제를 피하려고 라인별 불리언
변수 대신 "이 슬롯에 이 그룹에서 몇 대가 무슨 상태인가"라는 집계 정수
변수로 모델링한다(라인 개별 식별자가 아예 안 나오므로 symmetry가 생기지
않음). 대신 결과를 사람이 보는 CSV/간트로 낼 때는 각 슬롯의 집계 카운트를
실제 k개의 물리 라인 라벨로 다시 분배해줘야 하는데, 그게
reconstruct_physical_schedule()의 역할이다.

이 파일의 두 함수는 서로 정반대 방향의 변환을 담당한다:
  build_line_pools()              : Line(타입+count) -> 풀(LinePool) 목록  (모델링 전, solver.py 시작 시 호출)
  reconstruct_physical_schedule() : 풀 단위 solved 값 -> 물리 라인별 스케줄  (모델링 후, solver.py 결과 추출부에서 호출)
CP-SAT 모델(cp_model) 자체는 이 파일에 전혀 등장하지 않는다 - 둘 다 순수
파이썬 데이터 변환일 뿐이다.
"""

from __future__ import annotations

import collections

from .models import SLOT_LABELS, SLOTS_PER_DAY, Line, LinePool, Order


def _max_flow_avoid_self(group_sizes: dict[str, int], demands: dict[str, int]) -> dict[tuple[str, str], int]:
    """reconstruct_physical_schedule()의 3단계(새 셋업 배정)에서 self-loop을
    최대한 피하기 위해 쓰는 최대유량 계산. group_sizes[X]는 "직전까지
    X로 설정돼 있다가 이번 슬롯엔 그 설정을 안 쓰게 된(=버려진)" 라벨이
    몇 개인지, demands[Y]는 이번 슬롯에 Y로 새로 셋업해야 하는 라벨이
    몇 개인지를 나타낸다. group X를 demand Y에 배정하는 것 자체는
    자유이지만(둘 다 그냥 숫자일 뿐), X==Y인 배정은 self-loop(방금 놓아준
    설정을 그대로 다시 셋업하는 무의미한 셋업)이라 가능하면 피해야 한다.

    source -> group[X](용량 group_sizes[X]) -> demand[Y](X!=Y인 조합만
    간선 존재) -> sink(용량 demands[Y]) 형태의 이분 유량망을 만들고,
    Edmonds-Karp(BFS 증가경로)로 최대유량을 구한다. 모든 간선의 비용이
    동일(0 또는 안 씀)하므로 "최대유량 = self-loop 없이 배정 가능한
    최대 개수"와 정확히 같다 - 이게 이 문제의 이론적 상한이라, 여기서
    못 채운 나머지는(demands[Y] - 여기서 Y에 실제로 배정된 총량) 어떤
    알고리즘을 쓰든 self-loop 없이는 못 채우는, 진짜로 불가피한 최소치다
    (호출하는 쪽이 그 나머지는 self-loop으로 채우면서 경고를 남김).

    반환값: {(X,Y): 그 조합으로 실제 배정된 개수}(X!=Y인 조합만, 0인
    조합은 안 담김) - self-loop 배정은 여기 결과에 아예 안 나타난다."""
    groups = [x for x, sz in group_sizes.items() if sz > 0]
    demand_ids = [y for y, d in demands.items() if d > 0]

    # 인접 리스트 방식 잔여용량 그래프. 노드: "S"/"T"(source/sink),
    # ("G",x)/("D",y)(그룹/수요 노드) - 튜플로 감싸서 group/demand의
    # order_id 문자열과 절대 안 겹치게 한다.
    cap: dict = collections.defaultdict(dict)

    def add_edge(u, v, c):
        cap[u][v] = cap[u].get(v, 0) + c
        cap[v].setdefault(u, 0)  # 역방향 잔여간선(처음엔 0) 미리 만들어둠

    for x in groups:
        add_edge("S", ("G", x), group_sizes[x])
    for y in demand_ids:
        add_edge(("D", y), "T", demands[y])
    for x in groups:
        for y in demand_ids:
            if x != y:
                add_edge(("G", x), ("D", y), group_sizes[x])  # 상한은 어차피 S->G[x]가 이미 제한

    flow: dict[tuple[str, str], int] = {}
    while True:
        # BFS로 S->T 증가경로 탐색(잔여용량 > 0인 간선만 따라감).
        parent = {"S": None}
        queue = collections.deque(["S"])
        while queue:
            u = queue.popleft()
            if u == "T":
                break
            for v, c in cap[u].items():
                if c > 0 and v not in parent:
                    parent[v] = u
                    queue.append(v)
        if "T" not in parent:
            break  # 더 이상 S->T로 흘릴 방법이 없음 = 최대유량 도달

        path = []
        v = "T"
        while v != "S":
            u = parent[v]
            path.append((u, v))
            v = u
        bottleneck = min(cap[u][v] for u, v in path)
        for u, v in path:
            cap[u][v] -= bottleneck
            cap[v][u] += bottleneck
            if isinstance(u, tuple) and u[0] == "G" and isinstance(v, tuple) and v[0] == "D":
                key = (u[1], v[1])
                flow[key] = flow.get(key, 0) + bottleneck
            elif isinstance(u, tuple) and u[0] == "D" and isinstance(v, tuple) and v[0] == "G":
                # 증가경로가 이전에 배정한 흐름을 되돌리는(역방향) 경우 -
                # 실제로는 잘 안 일어나지만(이 그래프 구조상), 일반적인
                # Edmonds-Karp 정확성을 위해 처리해둔다.
                key = (v[1], u[1])
                flow[key] = flow.get(key, 0) - bottleneck

    return {k: v for k, v in flow.items() if v > 0}

# 여기서 order의 line type compatibility 와 rate, workers 를 통해 line pool 의 order compatibility 와
# oid별 rate, workers 를 채우는 것임
def build_line_pools(lines: list[Line], orders: list[Order]) -> list[LinePool]:
    """각 Line(타입 하나, count대)을 그대로 하나의 LinePool로 바꾼다.

    예전 버전은 물리 라인을 미리 count개로 펼쳐서(예: "line_mask_3_1",
    "line_mask_3_2") 각각을 별개 Line으로 만들고, 여기서 그 라인들의
    rate/workers가 서로 완전히 같은지 비교해가며 "같은 그룹인지"를
    거꾸로 알아내야 했다(입력 데이터 쪽에서 동일 물리 라인 count대만큼
    rate/workers를 매번 중복 입력해야 했고, 오타 하나로 값이 미세하게
    달라지면 같은 라인인데도 조용히 서로 다른 풀로 쪼개져서 symmetry
    문제가 되살아나는 위험이 있었다). 지금은 Line.count가 "몇 대인지"를
    직접 명시하는 입력값이라 그런 추론/비교가 아예 필요 없다 - 각
    line_type_id에 대해 rate/workers를 한 번만 적으면 되고(count대 전부에
    동일하게 적용됨), 물리 라인 라벨(LinePool.line_ids)은 여기서 그냥
    기계적으로 만들어낸다.
    """
    order_by_id = {o.order_id: o for o in orders}
    pools: list[LinePool] = []
    for line in lines:
        # 물리 라인 라벨: count==1이면 line_type_id 그대로(예: "line_tube_1"),
        # count>1이면 "line_type_id_1".."line_type_id_count"(예:
        # "line_mask_3_1", "line_mask_3_2") - 예전 expand_line_types()가
        # 쓰던 것과 동일한 명명 규칙이라, 리포트/CSV에 찍히는 물리 라인
        # 라벨 형태는 그대로 유지된다(하위호환). 이 라벨들만이 진짜로
        # 유일한 물리 라인 식별자다 - line_type_id 자체는 유일하지 않다.
        line_ids = (
            [line.line_type_id] if line.count <= 1
            else [f"{line.line_type_id}_{i}" for i in range(1, line.count + 1)]
        )
        # compat_order_ids: 이 라인 타입이 생산 가능한 주문 목록. rate가
        # 0이거나 아예 없는 주문은 "이 라인에서 생산 불가"로 간주해서
        # 제외한다(Order.compatible_line_types()와 같은 판단 기준). orders를
        # 그대로 순회하되 결과를 order_id로 정렬해서 가독성/재현성을
        # 맞춘다.
        compat_order_ids = sorted(
            (o.order_id for o in orders if o.rate.get(line.line_type_id, 0) and o.rate.get(line.line_type_id, 0) > 0)
        )
        rate = {oid: order_by_id[oid].rate[line.line_type_id] for oid in compat_order_ids}
        workers = {oid: int(order_by_id[oid].workers.get(line.line_type_id, 0)) for oid in compat_order_ids}
        pools.append(
            LinePool(
                line_ids=line_ids,
                compat_order_ids=compat_order_ids,
                rate=rate,
                workers=workers,
                setup_hours=line.setup_hours,
            )
        )
    return pools


def reconstruct_physical_schedule(
    pool: LinePool,
    pool_snapshot: dict,
    T: int,
    order_id_to_product: dict[str, str],
) -> dict[str, list[tuple[int, str, str, str]]]:
    """풀링된 집계 변수의 solved 값(pool_snapshot)을 실제 물리 라인
    line_ids 각각의 (day, slot_label, activity, order_id) 시퀀스로
    복원한다. order_id는 idle 슬롯에서는 빈 문자열이고, produce/setup
    슬롯에서는 그 활동이 어느 주문(order_id) 때문인지를 담는다 - 여러
    주문이 같은 product_id를 공유할 수 있어서(예: 아직 품번이 안 나온
    "코드확인중" 같은 placeholder를 여러 실제 제품이 같이 쓰는 경우),
    activity 문자열의 product_id만으로는 서로 다른 주문을 구분할 수
    없다. CP-SAT 모델 자체는 처음부터 order_id 단위로 각 주문의 수량을
    독립적으로 추적하므로(각 주문마다 별도의 produced_qty 제약) 실제
    계획/배정은 항상 주문별로 정확하다 - order_id를 여기서 같이
    돌려주는 건 순전히 "그 결과를 사람이 보는 CSV로 낼 때도 그 구분을
    유지하기 위함"이다.

    pool_snapshot 형식: {
        "idle_fresh": {t: int}, "prod_fresh": {(t,oid): int},
        "idle_nf_of": {(t,oid): int}, "prod_nf": {(t,oid): int},
        "setup": {(t,oid): int}, "setup2": {(t,oid): int},
    }
    (각 키의 정확한 의미는 scheduling/solver.py의 풀 집계 변수 섹션 주석
    참고 - 여기서는 그 값들을 "이번 슬롯에 이 버킷에 해당하는 물리 라인이
    몇 대 필요한가"라는 수요(demand)로만 취급한다. "setup2"는
    pool.setup_hours==1인 풀에서는 항상 빈 딕셔너리다 - 그 풀엔 2단계
    셋업 자체가 없음.)

    알고리즘(슬롯을 t=0..T-1 순서로 처리, 라벨(=물리 라인 하나)별로
    (fresh 여부, 현재 설정된 제품) 상태를 유지):
      1. 직전 슬롯에 셋업(oid)을 했던 라벨은 이번 슬롯에 반드시 다음
         단계로 넘어간다: pool.setup_hours==1이면 바로 produce(oid),
         setup_hours==2면 1단계 다음엔 반드시 2단계(oid), 그 2단계
         다음에야 반드시 produce(oid)(모델의 하드 제약 그대로 재현 -
         셋업 도중이나 셋업 후 대기하는 라벨이 나오면 안 됨).
      2. 나머지 continuation 수요(idle_nf_of/prod_nf, 그리고
         setup_hours==2 풀에서는 setup2도 포함 - 1단계에서 self-loop
         회피로 인해 강제 배정 몫을 넘어서는 나머지가 생길 수 있어서,
         idle_nf_of/prod_nf와 완전히 같은 방식으로 label_config==oid인
         라벨에서 채운다)는 현재 그 제품으로 설정된 non-fresh 라벨
         풀에서 채운다.
      3. 새로 셋업하는(setup, 즉 1단계) 수요는 아직 배정 안 된 non-fresh
         라벨 중에서 채우되, self-loop(자기 자신이 이미 그 oid로 설정돼
         있는데 또 그 oid로 "셋업"하는 무의미한 헛 셋업)을 최대한 피한다.
         이번엔 여러 oid의 수요가 같은 남은 라벨 풀을 두고 서로
         경쟁하므로, oid 하나씩 순서대로 처리하는 탐욕적 방식으로는
         순서에 따라 피할 수 있었던 self-loop을 못 피할 수 있다 - 그래서
         모든 oid의 수요를 한꺼번에 놓고 최대유량(_max_flow_avoid_self)으로
         self-loop 없이 배정 가능한 진짜 최댓값을 구하고, 그래도 못
         채운 나머지만 self-loop으로 채우며 경고를 남긴다. fresh
         상태에서 바로 생산 시작(prod_fresh)하는 수요는 아직 배정 안 된
         fresh 라벨 중 아무나 채운다 - fresh 라벨은 전부 "무엇이든
         무료로 새로 시작 가능"이라 서로 구분할 이유가 없으므로 순서가
         결과에 영향을 주지 않는다.
      4. 남는 fresh 라벨은 idle로 채운다.
    각 단계의 수요는 CP-SAT이 이미 강제한 집계 제약(예: prod_nf[t+1,oid] >=
    setup[t,oid] 또는 setup2[t,oid], idle_nf_of+prod_nf <= n_cfg_nf[t-1,oid])에
    의해 항상 공급으로 정확히 충당된다는 게 설계상 보장돼 있다 - 혹시라도
    안 맞으면(버그면) 조용히 잘못된 스케줄을 내는 대신 즉시 RuntimeError를
    낸다.
    """
    labels = list(pool.line_ids)

    # 라벨(물리 라인)별로 재구성 과정에서 계속 갱신해나갈 "현재 상태" 3종.
    #   label_fresh[l]  : l이 "오늘(당일) 아직 아무 것도 안 한" 상태인지.
    #                     매일 day-start(local==0)에 전원 True로 리셋된다
    #                     (solver.py의 nfr==k 강제 리셋과 대응).
    #   label_config[l] : l이 non-fresh일 때 마지막으로 셋업/생산한 제품
    #                     (order_id). fresh인 동안은 의미 없음(fresh 라벨은
    #                     과거 이력과 무관하게 아무 제품이나 무료로 시작
    #                     가능하므로) - 하지만 한 번 non-fresh가 되면(prod_fresh
    #                     로 생산을 시작하는 순간) 그 즉시 유의미해지고,
    #                     날짜가 바뀌어도(fresh는 리셋되지만) 값 자체는
    #                     그대로 유지된다 - idle로 대기만 하는 라인이 여러
    #                     날에 걸쳐 같은 제품 설정을 그대로 이어가는 실제
    #                     동작을 그대로 반영.
    #   forced_setup2_next[l] : l이 바로 이전 슬롯에 oid로 셋업 1단계를
    #                     했다면(pool.setup_hours==2인 풀만 해당), "이번
    #                     슬롯엔 반드시 oid로 셋업 2단계여야 한다"는 예약.
    #   forced_produce_next[l] : l이 바로 이전 슬롯에 oid로 셋업을
    #                     끝냈다면(setup_hours==1 풀의 유일한 셋업 단계,
    #                     또는 setup_hours==2 풀의 2단계), "이번 슬롯엔
    #                     반드시 oid를 생산해야 한다"는 예약.
    #   둘 다 다음 t로 넘어갈 때(맨 위에서) 소비되고 다시 빈 딕셔너리로
    #   초기화된다(슬롯 하나짜리 예약이라 누적되지 않음).
    label_fresh = {l: True for l in labels}
    label_config: dict[str, str | None] = {l: None for l in labels}
    forced_setup2_next: dict[str, str] = {}
    forced_produce_next: dict[str, str] = {}
    # has_shown_produce[l]: 오늘(당일) l의 화면 표시(assigned)에 "produce:"나
    # "setup:"이 한 번이라도 찍힌 적 있는지. 4.5단계 스왑 때문에 내부적으론
    # non-fresh인데 화면엔 계속 idle로만 보이는 라벨("phantom")이 생길 수
    # 있는데, 이 플래그로 그런 라벨을 가려내서 3단계(새 셋업 대상 선택)에서
    # 후순위로 미룬다 - 그래야 나중에 다른 제품으로 셋업 나갈 때, "화면상
    # 한 번도 안 건드린 것처럼 보이던 라벨이 갑자기 셋업"하는 것처럼 보이는
    # 대신, 실제로 생산 이력이 화면에 보이는 라벨이 먼저 그 역할을 맡는다.
    # label_fresh와 마찬가지로 매일 day-start에 리셋된다(날짜가 바뀌면
    # 셋업이 공짜라 그날의 "이야기"도 새로 시작되는 셈이므로).
    has_shown_produce: dict[str, bool] = {l: False for l in labels}

    entries: dict[str, list[tuple[int, str, str, str]]] = {l: [] for l in labels}

    def take(pool_list: list[str], oid_pred, n: int, from_pool_desc: str, oid: str) -> list[str]:
        """pool_list(그 슬롯에서 아직 배정 안 된 라벨들의 리스트) 중
        oid_pred를 만족하는 라벨을 앞에서부터 n개 골라 pool_list에서
        제거하고 반환한다. 순서는 임의(파이썬 리스트 순회 순서)라서 특별한
        우선순위 없이 "조건 맞는 아무 라벨"을 뽑는 것과 같다 - 이 시점의
        라벨들은 이미 앞 단계에서 진짜로 구분돼야 하는 라벨(continuation
        대상)은 다 빠지고 남은 것들이라, 서로 완전히 동등하므로 순서가
        결과 품질에 영향을 주지 않는다(설계 검토 과정에서 확인됨).

        n개를 못 채우면(pool_list에 조건 맞는 라벨이 부족하면) 이건 절대
        일어나서는 안 되는 상황(CP-SAT이 이미 그 수요가 공급 가능하다고
        보장한 값들이므로) - 조용히 잘못된 스케줄을 만드는 대신 즉시
        RuntimeError로 실패시켜서 버그를 바로 드러낸다.
        """
        chosen = [l for l in pool_list if oid_pred(l)][:n]
        if len(chosen) < n:
            raise RuntimeError(
                f"[pooling] 재구성 실패: t={t}, {from_pool_desc} 풀에서 oid={oid}에 필요한 "
                f"{n}대를 채우지 못함(가능: {len(chosen)}대). 풀링 모델 제약 위반 가능성."
            )
        for l in chosen:
            pool_list.remove(l)
        return chosen

    for t in range(T):
        day = t // SLOTS_PER_DAY + 1
        local = t % SLOTS_PER_DAY
        slot_label = SLOT_LABELS[local]

        # 날짜가 바뀌는 시점(하루의 첫 슬롯, t=0 자체는 이미 위에서 전원
        # fresh로 초기화돼 있으므로 t>0인 경우만): 모든 라벨이 새로
        # fresh해진다. label_config는 건드리지 않는다 - "어제 마지막으로
        # 뭘 하고 있었는지"는 그대로 남아있고, 다만 오늘 첫 행동을 할 땐
        # 그 이력과 무관하게 무료로 아무 제품이나 다시 고를 수 있다는 뜻
        # (solver.py의 fresh 리셋 로직과 동일한 의미).
        if local == 0 and t > 0:
            for l in labels:
                label_fresh[l] = True
                has_shown_produce[l] = False

        # 이번 슬롯에 아직 배정을 안 정한 라벨들을 fresh/non-fresh 두
        # 풀로 나눈다. 아래 단계들은 이 두 리스트에서 원소를 골라서
        # 제거해가며(take()) "이번 슬롯에 아직 뭘 할지 안 정한 라벨"의
        # 개수를 줄여나가는 방식으로 진행된다.
        fresh_pool = [l for l in labels if label_fresh[l]]
        nonfresh_pool = [l for l in labels if not label_fresh[l]]
        assigned: dict[str, str] = {}  # 이번 슬롯에 각 라벨이 최종적으로 뭘 하는지 (아직 못 정한 라벨은 키가 없음)
        assigned_order: dict[str, str] = {}  # 이번 슬롯에 각 라벨의 activity가 어느 주문 때문인지(idle이면 안 채워짐)

        # ------------------------------------------------------------------
        # 1단계: 직전 슬롯 셋업으로 예약된 라벨부터 강제 처리. 맨 위에서
        # 바로 그릇을 비워서(forced_*_this로 넘기고 forced_*_next는 즉시
        # 빈 딕셔너리로), 이번 슬롯에서 새로 만드는 예약이 이번 슬롯
        # 처리에 섞여 들어가지 않게 한다.
        #
        # 1-A) forced_setup2_this: 직전 t-1에서 setup(1단계):oid를 한
        #      라벨(pool.setup_hours==2인 풀만 해당)은 이번 t에 반드시
        #      setup(2단계):oid. 소비하면서 "그럼 다음 슬롯엔 반드시
        #      produce:oid"라고 forced_produce_next에 새로 예약해둔다.
        # 1-B) forced_produce_this: 직전 t-1에서 셋업을 끝낸(setup_hours==1
        #      풀의 유일한 셋업 단계, 또는 setup_hours==2 풀의 2단계) 라벨은
        #      이번 t에 반드시 produce:oid. solver.py의 하드 제약
        #      (prod_nf[t+1,oid] >= setup[t,oid] 또는 setup2[t,oid])을
        #      물리 라벨 단위로 그대로 재현한 것.
        # 두 경우 다 해당 라벨은 이미 non-fresh이고 label_config도 이미
        # oid로 맞춰져 있어야 한다(1-A는 3단계, 1-B는 1-A 또는 2단계에서
        # 같이 설정해둠) - 아니라면 뭔가 앞뒤가 안 맞는 상태이므로 즉시 에러.
        # ------------------------------------------------------------------
        forced_setup2_this = forced_setup2_next
        forced_setup2_next = {}
        forced_produce_this = forced_produce_next
        forced_produce_next = {}

        for l, oid in forced_setup2_this.items():
            if l not in nonfresh_pool or label_config[l] != oid:
                raise RuntimeError(f"[pooling] 재구성 실패: 셋업 2단계 강제 라벨 상태 불일치 (label={l}, oid={oid}, t={t})")
            nonfresh_pool.remove(l)
            assigned[l] = f"setup:{order_id_to_product[oid]}"
            assigned_order[l] = oid
            forced_produce_next[l] = oid  # 다음 슬롯 예약 - 이미 비워둔 그릇에 새로 쓰는 것이라 이번 슬롯엔 영향 없음

        for l, oid in forced_produce_this.items():
            if l not in nonfresh_pool or label_config[l] != oid:
                raise RuntimeError(f"[pooling] 재구성 실패: 셋업 강제 생산 라벨 상태 불일치 (label={l}, oid={oid}, t={t})")
            nonfresh_pool.remove(l)
            assigned[l] = f"produce:{order_id_to_product[oid]}"
            assigned_order[l] = oid
        # forced_this: 방금 소비한 생산 예약 내역(2단계에서 prod_nf 수요
        # 계산 시 "이미 처리된 몫"을 빼는 데 씀).
        forced_this = forced_produce_this

        # ------------------------------------------------------------------
        # 2단계: continuation 수요 - "어제(또는 그 이전부터) 이미 이 제품으로
        # 설정돼 있던 라인이 오늘도 그 설정을 유지한 채 대기하거나 계속
        # 생산하는" 경우. 이 라벨들은 label_config가 이미 oid인 non-fresh
        # 라벨 중에서 골라야만 의미가 맞는다(아무 라벨이나 쓰면 "존재하지
        # 않던 셋업 이력"을 만들어내는 셈이 되어 셋업 카운트가 안 맞게 됨).
        # ------------------------------------------------------------------
        for oid in pool.compat_order_ids:
            # prod_nf 수요 중 1단계에서 이미 채워진 몫(forced_this에 해당
            # oid로 예약됐던 라벨 수)은 빼고 나머지만 여기서 채운다 - 안
            # 그러면 같은 수요를 두 번 채우려다 필요 이상으로 라벨을
            # 소비하게 된다.
            need_prod = pool_snapshot["prod_nf"].get((t, oid), 0) - sum(1 for o in forced_this.values() if o == oid)
            if need_prod > 0:
                chosen = take(nonfresh_pool, lambda l, oid=oid: label_config[l] == oid, need_prod, "non-fresh continuation(prod)", oid)
                for l in chosen:
                    assigned[l] = f"produce:{order_id_to_product[oid]}"
                    assigned_order[l] = oid
                # label_config는 이미 oid이므로 그대로 유지(변경 불필요) -
                # 계속 생산 중이라는 것 자체가 "여전히 oid로 설정돼 있다"는
                # 뜻이라 별도 갱신이 필요 없다.
            need_idle = pool_snapshot["idle_nf_of"].get((t, oid), 0)
            if need_idle > 0:
                chosen = take(nonfresh_pool, lambda l, oid=oid: label_config[l] == oid, need_idle, "non-fresh continuation(idle)", oid)
                for l in chosen:
                    assigned[l] = "idle"
                # 대기여도 non-fresh 상태에서의 idle이므로 설정(label_config)은
                # 그대로 유지된다 - "쉬는 중이지만 셋업은 그대로 남아있는" 것.

            # setup2 continuation (pool.setup_hours==2인 풀만 해당):
            #   solver.py에서 pool_setup2[t,oid]는 pool_setup[t-1,oid]와
            #   하드로 정확히 같은 값이다. 그런데 1단계(forced_setup2_this)로
            #   방금 강제 배정한 라벨 수는 "t-1에 self-loop 회피 없이 진짜로
            #   1단계를 시작한" 라벨 수(=3단계의 non-self-loop 매칭분)뿐이라,
            #   t-1에 self-loop 회피로 "idle"로 재구성됐던 나머지(remaining)는
            #   여기 안 잡혀 있다. 그 나머지도 집계상 setup2[t,oid]에 포함돼
            #   있으므로(aggregate가 이미 self-loop까지 다 센 값), 여기서
            #   need_idle/need_prod와 완전히 같은 방식으로 label_config==oid인
            #   라벨을 마저 채워서 그 몫을 흡수한다 - 안 그러면 그 라벨들이
            #   이번 슬롯에 어디에도 안 잡혀서 결국 아래 "배정 안 된 라벨
            #   남음" RuntimeError로 이어진다.
            if pool.setup_hours == 2:
                need_setup2 = pool_snapshot["setup2"].get((t, oid), 0) - sum(1 for o in forced_setup2_this.values() if o == oid)
                if need_setup2 > 0:
                    chosen = take(nonfresh_pool, lambda l, oid=oid: label_config[l] == oid, need_setup2, "non-fresh setup2 continuation", oid)
                    for l in chosen:
                        assigned[l] = f"setup:{order_id_to_product[oid]}"
                        assigned_order[l] = oid
                        forced_produce_next[l] = oid  # 이 라벨도 다음 슬롯엔 반드시 생산
                    # label_config는 이미 oid이므로 그대로 유지.

        # ------------------------------------------------------------------
        # 3단계: 새 셋업 수요. 남은 non-fresh 라벨(=continuation으로도 안
        # 뽑히고 아직 미배정인 라벨, label_config는 "직전까지 뭐였는지"를
        # 그대로 들고 있음) 중에서 이번 슬롯의 셋업 수요를 채운다.
        #
        # self-loop(자기 자신이 이미 oid로 설정돼 있는데 또 oid로
        # 셋업하는, 실제로는 아무 의미 없는 헛 셋업)을 최대한 피해야
        # 한다 - 2026-07-19에 실제 데이터에서 발견됨: solver.py의
        # `inf+pnf <= prev_cfg`가 등식이 아니라 부등식이라("이미 oid로
        # 설정된 라벨이 이번 슬롯도 계속 oid를 유지해야 한다"는 강제가
        # 없어서), continuity 최적화(2단계)가 시간 안에 완전히 수렴하지
        # 못하면 CP-SAT이 "잘 설정돼 있던 라벨을 굳이 놓아주고 다른
        # 라벨에 새로 그 oid를 셋업하는" 것과 수치상 동일하게 취급된
        # 해를 낼 수 있다 - 집계 총량은 맞지만 특정 라벨로 복원하면
        # self-loop처럼 보인다.
        #
        # 라벨 하나하나를 oid 하나씩 순서대로 처리하는 탐욕적 방식으로는
        # 이걸 완전히 못 피한다 - 뒤에 처리되는 oid에게 필요한 유일한
        # "self-loop 아닌" 라벨을, 굳이 그게 아니어도 되는 앞선 oid가
        # 먼저 가져가버리는 경우가 실제로 있다(순서 의존성). 그래서 모든
        # oid의 수요를 한꺼번에 놓고 최대유량(_max_flow_avoid_self)으로
        # "self-loop 없이 배정 가능한 최대치"를 구한다 - 이건 순서와
        # 무관하게 항상 이론적 최댓값을 찾아내므로, 여기서 못 채운
        # 나머지만이 정말로 self-loop 없이는 불가능한 진짜 최소치다.
        #
        # 셋업(1단계)을 배정하면 label_config를 즉시 그 목적지 oid로
        # 갱신하고, forced_setup2_next 또는 forced_produce_next(풀의
        # setup_hours에 따라 다름, 아래 참고)에 "다음 슬롯엔 이 라벨이
        # 반드시 뭘 해야 하는지" 예약해둔다(1단계에서 소비됨).
        # ------------------------------------------------------------------
        setup_demand = {oid: pool_snapshot["setup"].get((t, oid), 0) for oid in pool.compat_order_ids}
        setup_demand = {oid: n for oid, n in setup_demand.items() if n > 0}
        if setup_demand:
            # 그룹 내에서 "이번 슬롯에 이 그룹을 떠나 다른 oid로 셋업 나갈"
            # 라벨을 고를 때, 화면에 이미 생산 이력이 보이는(honest) 라벨을
            # 먼저 뽑히게 정렬해둔다 - phantom 라벨은 최대한 계속 idle로
            # 남겨서 "안 건드린 라벨"처럼 보이는 걸 유지한다(has_shown_produce
            # 주석 참고). 이건 순수 표시 우선순위일 뿐이라 group_sizes(개수)나
            # 최대유량 계산 결과엔 전혀 영향이 없다.
            nonfresh_pool.sort(key=lambda l: not has_shown_produce[l])
            group_sizes: dict[str, int] = {}
            for l in nonfresh_pool:
                group_sizes[label_config[l]] = group_sizes.get(label_config[l], 0) + 1
            flow = _max_flow_avoid_self(group_sizes, setup_demand)

            for (src_oid, dst_oid), n in flow.items():
                chosen = take(
                    nonfresh_pool, lambda l, src_oid=src_oid: label_config[l] == src_oid, n,
                    "non-fresh new setup(non-self-loop, matched)", dst_oid,
                )
                for l in chosen:
                    assigned[l] = f"setup:{order_id_to_product[dst_oid]}"
                    assigned_order[l] = dst_oid
                    label_config[l] = dst_oid
                    # setup_hours==2 풀은 방금 한 게 1단계일 뿐이라 다음
                    # 슬롯엔 2단계를 예약해야 하고, setup_hours==1 풀은
                    # 방금 한 게 유일한 셋업 단계라 바로 생산을 예약한다.
                    if pool.setup_hours == 2:
                        forced_setup2_next[l] = dst_oid
                    else:
                        forced_produce_next[l] = dst_oid

            for oid, need_setup in setup_demand.items():
                filled = sum(n for (_src, dst_oid), n in flow.items() if dst_oid == oid)
                remaining = need_setup - filled
                if remaining > 0:
                    # 실제로 self-loop을 뒤집어쓰는 라벨(chosen)을 먼저
                    # 정한 다음에 경고를 남긴다 - 그래야 경고 메시지에
                    # "이 풀 어딘가"가 아니라 실제로 몇 번 라인에서
                    # 벌어졌는지를 정확히 적을 수 있다.
                    #
                    # 최대유량이 이미 "self-loop 없이는 절대 못 채운다"는
                    # 걸 증명한 나머지이므로(_max_flow_avoid_self의
                    # 최적성 - group X에 여유가 있고 demand Y(X!=Y)가 안
                    # 채워진 상태로 남는 경우가 없음), 여기 남은
                    # nonfresh_pool 라벨은 전부 label_config==oid임이
                    # 보장된다 - 즉 "이미 oid로 설정된 라벨"이다. 그렇다면
                    # 굳이 아무 의미 없는 셋업 슬롯을 만들 필요 없이, 그냥
                    # "이미 설정된 채 대기"(idle_nf_of)로 재구성하면 된다
                    # - CP-SAT의 집계 해(setup 카운트)와는 어긋나 보이지만,
                    # 그 집계 해 자체가 애초에 "이미 설정된 라벨을 self-loop
                    # 셋업으로 세는 것"과 "그냥 계속 대기시키는 것"을
                    # 구분 못 하는 타이(둘 다 목적함수 값이 동일)라서
                    # (setup.py 3단계 주석 참고), 물리적으로 더 자연스러운
                    # 쪽(대기)을 골라도 실제로 달성한 비용/연속성 점수는
                    # 전혀 안 바뀐다 - 그냥 재구성 표현만 더 정확해질 뿐.
                    # label_config는 이미 oid이므로 안 건드리고,
                    # forced_setup2_next/forced_produce_next도 걸지 않는다
                    # (셋업을 안 했으니 다음 슬롯에 강제로 뭘 시킬 이유가
                    # 없다 - 필요하면 다음 슬롯의 2단계 continuation이
                    # (setup2 continuation 포함) 알아서 이 라벨을 다시
                    # 골라간다).
                    chosen = take(nonfresh_pool, lambda l: True, remaining, "non-fresh new setup(self-loop, unavoidable)", oid)
                    # 안전장치: 위 주석의 수학적 증명이 실제로도 성립하는지
                    # 매번 확인한다 - _max_flow_avoid_self에 버그가 있어서
                    # label_config가 oid가 아닌 라벨이 여기 섞여 들어오면,
                    # 그걸 조용히 "이미 설정된 라벨"인 것처럼 idle로
                    # 재구성해버리면 실제로는 그 제품으로 생산이 시작도
                    # 안 됐는데 이미 준비된 것처럼 잘못된 스케줄을 낼 수
                    # 있다 - 그런 경우는 즉시 RuntimeError로 드러낸다.
                    mismatched = [l for l in chosen if label_config[l] != oid]
                    if mismatched:
                        raise RuntimeError(
                            f"[pooling] 재구성 실패: t={t} oid={oid}에서 self-loop 폴백으로 고른 라벨 "
                            f"{mismatched}의 label_config가 oid와 다름({[label_config[l] for l in mismatched]}) - "
                            f"_max_flow_avoid_self 최적성 가정이 깨졌을 가능성."
                        )
                    print(
                        f"[경고] [pooling] t={t}(day={day}, slot={slot_label}) line={chosen} oid={oid}: "
                        f"self-loop 셋업(이미 그 제품으로 설정된 라인을 다시 그 제품으로 셋업) {remaining}건이 "
                        f"CP-SAT 집계 해 상으로는 필요했지만, 이미 그 제품으로 설정돼 있으므로 셋업 없이 "
                        f"대기(idle)로 재구성합니다 - 목적함수 값(비용/연속성 점수)에는 영향 없음. "
                        f"continuity(2단계) 최적화가 완전히 수렴하지 못했을 가능성이 있습니다."
                    )
                    for l in chosen:
                        assigned[l] = "idle"

        # 여기까지 오면 이번 슬롯의 non-fresh 라벨은 전부(1~3단계 어딘가에서)
        # 배정이 끝나 있어야 한다. 남아 있다면 = CP-SAT이 낸 pool_snapshot의
        # 버킷 합계가 non-fresh 전체 인원(k-nfr[t])과 안 맞는다는 뜻이라,
        # solver.py 쪽 집계 제약이 깨졌다는 심각한 버그 신호다.
        if nonfresh_pool:
            raise RuntimeError(f"[pooling] 재구성 실패: t={t}에서 배정 안 된 non-fresh 라벨 {len(nonfresh_pool)}개 남음.")

        # 4단계(fresh 라벨 처리) 전에, 이번 슬롯에 "이미 그 oid로 설정된
        # 채 대기"(idle_nf_of)로 배정된 라벨을 oid별로 미리 모아둔다 -
        # 아래 4.5단계(fresh 신규 투입과의 맞바꿈)에서 쓴다. 이 시점엔
        # non-fresh 라벨은 전부 1~3단계에서 이미 assigned가 끝나 있고
        # fresh 라벨은 아직 전혀 안 건드려졌으므로, label_fresh[l]==False
        # 인지만 보면 "이번 슬롯 들어오기 전부터 non-fresh였던 라벨"을
        # 정확히 가려낼 수 있다.
        idle_nf_of_by_oid: dict[str, list[str]] = {}
        for l in labels:
            if not label_fresh[l] and assigned.get(l) == "idle":
                idle_nf_of_by_oid.setdefault(label_config[l], []).append(l)

        # ------------------------------------------------------------------
        # 4단계: fresh 라벨 처리. prod_fresh 수요만큼 "무료로 바로 생산
        # 시작"을 배정하고(이 순간부터 non-fresh로 전환 + label_config
        # 설정), 나머지 fresh 라벨은 계속 대기(idle)하며 fresh 상태를
        # 유지한다(다음 슬롯의 n_fresh_var 계산이 이 idle_fresh 인원수에
        # 의존함 - solver.py의 `nfr == idle_fresh[t-1]` 재귀 참고).
        # ------------------------------------------------------------------
        prod_fresh_chosen_by_oid: dict[str, list[str]] = {}
        for oid in pool.compat_order_ids:
            need = pool_snapshot["prod_fresh"].get((t, oid), 0)
            if need > 0:
                chosen = take(fresh_pool, lambda l: True, need, "fresh direct-produce", oid)
                prod_fresh_chosen_by_oid[oid] = chosen
                for l in chosen:
                    assigned[l] = f"produce:{order_id_to_product[oid]}"
                    assigned_order[l] = oid
                    label_config[l] = oid
                    label_fresh[l] = False  # 이 슬롯부터 non-fresh로 전환(오늘 하루 동안 유지)

        # 남은 fresh 라벨(prod_fresh 수요에 뽑히지 않은 나머지)은 이번
        # 슬롯도 그냥 대기 - label_fresh는 True인 채로 유지되므로 다음
        # 슬롯에서도 계속 fresh 후보로 남는다.
        for l in fresh_pool:
            assigned[l] = "idle"

        # ------------------------------------------------------------------
        # 4.5단계: 같은 oid에 대해 "이미 설정된 채 대기"(idle_nf_of)와
        # "fresh 유닛 신규 투입"(prod_fresh)이 같은 슬롯에 동시에
        # 있으면, 어느 물리 라인 "이름"이 생산 기록을 갖는지만 맞바꾼다
        # (전시/출력용 라벨 교환일 뿐 - 상태머신 자체는 안 건드림).
        #
        # 왜 안전한가(scheduling/solver.py 3단계 타이브레이크 주석 참고):
        # n_idle_pool[t](이번 슬롯 대기 총 대수)은 fresh-idle과
        # non-fresh-idle을 구분 안 하고 합쳐서 하나로 센다 - 그래서 "이미
        # 설정된 라벨이 대기하고 fresh 라벨이 대신 생산" vs "이미 설정된
        # 라벨이 생산하고 fresh 라벨은 대신 대기"는 n_idle_pool 값이
        # 완전히 똑같고(총 대기 대수가 안 바뀜), 그래서 1단계(인건비)·
        # 2단계(전환+셋업 횟수) 목적함수 값에 조금도 영향을 안 준다.
        #
        # 주의: label_fresh[l_fresh]를 다시 True로 되돌리면 안 된다.
        # CP-SAT의 실제 해는 이 슬롯에 진짜로 non-fresh 유닛이 2대(원래
        # non-fresh 1대 + 이번에 새로 전환된 1대) 존재한다고 보고 있고,
        # 다음 슬롯들의 버킷 값(nfr, prod_nf, idle_nf_of 등)은 전부 그
        # "2대"라는 전제로 계산돼 있다 - label_fresh를 되돌리면 앞으로도
        # 계속 "1대만 non-fresh"인 것처럼 상태가 틀어져서, 뒤 슬롯에서
        # non-fresh 수요를 못 채우는 재구성 실패가 난다. 최종 CSV 출력은
        # idle_fresh와 idle_nf_of를 구분하지 않고 둘 다 그냥 "idle"로
        # 찍히므로, 상태머신은 그대로 두고 이번 슬롯의 assigned(표시값)만
        # 맞바꿔도 사용자가 보는 결과(로타리_1이 쭉 생산, 로타리_2는 계속
        # 대기)는 동일하게 얻어진다.
        # ------------------------------------------------------------------
        for oid, fresh_labels in prod_fresh_chosen_by_oid.items():
            idle_labels = idle_nf_of_by_oid.get(oid, [])
            n_swap = min(len(idle_labels), len(fresh_labels))
            for i in range(n_swap):
                l_idle, l_fresh = idle_labels[i], fresh_labels[i]
                assigned[l_idle] = f"produce:{order_id_to_product[oid]}"
                assigned_order[l_idle] = oid
                # label_config[l_idle]는 이미 oid이므로 안 건드림.
                assigned[l_fresh] = "idle"
                if l_fresh in assigned_order:
                    del assigned_order[l_fresh]
                # label_fresh[l_fresh]/label_config[l_fresh]는 그대로 둔다
                # (여전히 non-fresh, config=oid) - 위 주의사항 참고.

        # 이번 슬롯의 최종 배정 결과를 각 라벨의 시퀀스에 기록하고,
        # has_shown_produce도 이 최종(4.5단계 스왑까지 반영된) 결과 기준으로
        # 갱신한다 - 스왑으로 뒤바뀐 이후의 진짜 화면 표시값이 기준이어야
        # 한다.
        for l in labels:
            entries[l].append((day, slot_label, assigned[l], assigned_order.get(l, "")))
            if assigned[l].startswith("produce:") or assigned[l].startswith("setup:"):
                has_shown_produce[l] = True

    return entries
