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

from .models import SLOT_LABELS, SLOTS_PER_DAY, Line, LinePool, Order


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
    line_id에 대해 rate/workers를 한 번만 적으면 되고(count대 전부에
    동일하게 적용됨), 물리 라인 라벨(member_line_ids)은 여기서 그냥
    기계적으로 만들어낸다.
    """
    order_by_id = {o.order_id: o for o in orders}
    pools: list[LinePool] = []
    for line in lines:
        # 물리 라인 라벨: count==1이면 line_id 그대로(예: "line_tube_1"),
        # count>1이면 "line_id_1".."line_id_count"(예: "line_mask_3_1",
        # "line_mask_3_2") - 예전 expand_line_types()가 쓰던 것과 동일한
        # 명명 규칙이라, 리포트/CSV에 찍히는 물리 라인 라벨 형태는 그대로
        # 유지된다(하위호환).
        member_line_ids = (
            [line.line_id] if line.count <= 1
            else [f"{line.line_id}_{i}" for i in range(1, line.count + 1)]
        )
        # compat_order_ids: 이 라인 타입이 생산 가능한 주문 목록. rate가
        # 0이거나 아예 없는 주문은 "이 라인에서 생산 불가"로 간주해서
        # 제외한다(Order.compatible_lines()와 같은 판단 기준). orders를
        # 그대로 순회하되 결과를 order_id로 정렬해서 가독성/재현성을
        # 맞춘다.
        compat_order_ids = sorted(
            (o.order_id for o in orders if o.rate.get(line.line_id, 0) and o.rate.get(line.line_id, 0) > 0)
        )
        rate = {oid: order_by_id[oid].rate[line.line_id] for oid in compat_order_ids}
        workers = {oid: int(order_by_id[oid].workers.get(line.line_id, 0)) for oid in compat_order_ids}
        pools.append(
            LinePool(
                member_line_ids=member_line_ids,
                compat_order_ids=compat_order_ids,
                rate=rate,
                workers=workers,
            )
        )
    return pools


def reconstruct_physical_schedule(
    pool: LinePool,
    pool_snapshot: dict,
    T: int,
    order_id_to_product: dict[str, str],
) -> dict[str, list[tuple[int, str, str]]]:
    """풀링된 집계 변수의 solved 값(pool_snapshot)을 실제 물리 라인
    member_line_ids 각각의 (day, slot_label, activity) 시퀀스로 복원한다.

    pool_snapshot 형식: {
        "idle_fresh": {t: int}, "prod_fresh": {(t,oid): int},
        "idle_nf_of": {(t,oid): int}, "prod_nf": {(t,oid): int},
        "setup": {(t,oid): int},
    }
    (각 키의 정확한 의미는 scheduling/solver.py의 풀 집계 변수 섹션 주석
    참고 - 여기서는 그 값들을 "이번 슬롯에 이 버킷에 해당하는 물리 라인이
    몇 대 필요한가"라는 수요(demand)로만 취급한다.)

    알고리즘(슬롯을 t=0..T-1 순서로 처리, 라벨(=물리 라인 하나)별로
    (fresh 여부, 현재 설정된 제품) 상태를 유지):
      1. 직전 슬롯에 셋업(oid)을 했던 라벨은 이번 슬롯에 반드시 produce(oid)로
         배정한다(모델의 하드 제약 그대로 재현 - 셋업 후 대기하는 라벨이
         나오면 안 됨).
      2. 나머지 continuation 수요(idle_nf_of/prod_nf)는 현재 그 제품으로
         설정된 non-fresh 라벨 풀에서 채운다.
      3. 새로 셋업하거나(setup) fresh 상태에서 바로 생산 시작(prod_fresh)하는
         수요는 아직 배정 안 된 라벨 중 아무나(해당 fresh/non-fresh 조건에
         맞는 라벨) 채운다 - 이 시점부터는 어느 라벨이든 동등하므로 순서가
         결과에 영향을 주지 않는다.
      4. 남는 fresh 라벨은 idle로 채운다.
    각 단계의 수요는 CP-SAT이 이미 강제한 집계 제약(예: prod_nf[t+1,oid] >=
    setup[t,oid], idle_nf_of+prod_nf <= n_cfg_nf[t-1,oid])에 의해 항상
    공급으로 정확히 충당된다는 게 설계상 보장돼 있다 - 혹시라도 안 맞으면
    (버그면) 조용히 잘못된 스케줄을 내는 대신 즉시 RuntimeError를 낸다.
    """
    labels = list(pool.member_line_ids)

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
    #   forced_next[l]  : l이 바로 이전 슬롯에 oid로 셋업을 했다면, "이번
    #                     슬롯엔 반드시 oid를 생산해야 한다"는 예약을
    #                     여기 적어둔다. 다음 t로 넘어갈 때 소비되고 다시
    #                     빈 딕셔너리로 초기화된다(슬롯 하나짜리 예약이라
    #                     누적되지 않음).
    label_fresh = {l: True for l in labels}
    label_config: dict[str, str | None] = {l: None for l in labels}
    forced_next: dict[str, str] = {}  # label -> oid, 직전 슬롯에 그 제품으로 셋업해서 이번 슬롯엔 반드시 생산해야 함

    entries: dict[str, list[tuple[int, str, str]]] = {l: [] for l in labels}

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
                f"[pooling] 재구성 실패: {from_pool_desc} 풀에서 oid={oid}에 필요한 "
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

        # 이번 슬롯에 아직 배정을 안 정한 라벨들을 fresh/non-fresh 두
        # 풀로 나눈다. 아래 단계들은 이 두 리스트에서 원소를 골라서
        # 제거해가며(take()) "이번 슬롯에 아직 뭘 할지 안 정한 라벨"의
        # 개수를 줄여나가는 방식으로 진행된다.
        fresh_pool = [l for l in labels if label_fresh[l]]
        nonfresh_pool = [l for l in labels if not label_fresh[l]]
        assigned: dict[str, str] = {}  # 이번 슬롯에 각 라벨이 최종적으로 뭘 하는지 (아직 못 정한 라벨은 키가 없음)

        # ------------------------------------------------------------------
        # 1단계: 직전 슬롯 셋업으로 생산이 예약된 라벨부터 강제 처리.
        #   forced_next는 "직전 t-1에서 setup:oid를 한 라벨은 이번 t에 반드시
        #   produce:oid"라는, solver.py의 하드 제약(prod_nf[t+1,oid] >=
        #   setup[t,oid])을 물리 라벨 단위로 그대로 재현한 것. 이 라벨들은
        #   이미 non-fresh이고 label_config도 이미 oid로 맞춰져 있어야
        #   한다(3단계에서 셋업을 배정할 때 같이 설정해둠) - 아니라면 뭔가
        #   앞뒤가 안 맞는 상태이므로 즉시 에러.
        # ------------------------------------------------------------------
        for l, oid in forced_next.items():
            if l not in nonfresh_pool or label_config[l] != oid:
                raise RuntimeError(f"[pooling] 재구성 실패: 셋업 강제 생산 라벨 상태 불일치 (label={l}, oid={oid}, t={t})")
            nonfresh_pool.remove(l)
            assigned[l] = f"produce:{order_id_to_product[oid]}"
        # forced_this: 방금 소비한 예약 내역(2단계에서 prod_nf 수요 계산 시
        # "이미 처리된 몫"을 빼는 데 씀). forced_next 자체는 이번 슬롯에서
        # 새로 발생하는 셋업(3단계)을 위해 즉시 빈 딕셔너리로 재활용한다.
        forced_this = forced_next
        forced_next = {}

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

        # ------------------------------------------------------------------
        # 3단계: 새 셋업 수요. 남은 non-fresh 라벨(=continuation으로도 안
        # 뽑히고 아직 미배정인 라벨) 중 아무나 골라 셋업을 배정한다.
        #   - "아무나"인 이유: 이 시점에 남은 non-fresh 라벨들은 전부 "이번
        #     슬롯엔 무엇이든 새로 셋업해야 하는" 처지라 서로 구분할 이유가
        #     없다(어차피 새로 oid로 설정될 것이므로 이전 이력은 이제
        #     무의미).
        #   - self-loop(자기 자신이 이미 oid로 설정돼 있는데 또 oid로
        #     셋업)이 여기서 나올 걱정은 없다 - 그런 라벨(label_config==oid)은
        #     이미 2단계에서 continuation 후보로 먼저 소진됐거나(대기/생산),
        #     혹은 애초에 solver.py의 `su <= k-nfr-prev_cfg` 제약이 그런
        #     수요 자체를 0으로 막아뒀기 때문에 need_setup이 그 oid로
        #     "이미 oid인 라벨 수"를 초과해서 요구할 일이 없다.
        #   - 셋업을 배정하면 label_config를 즉시 oid로 갱신하고(설정 완료),
        #     forced_next에 "다음 슬롯엔 이 라벨이 반드시 produce:oid"라고
        #     예약해둔다(1단계에서 소비됨).
        # ------------------------------------------------------------------
        for oid in pool.compat_order_ids:
            need_setup = pool_snapshot["setup"].get((t, oid), 0)
            if need_setup > 0:
                chosen = take(nonfresh_pool, lambda l: True, need_setup, "non-fresh new setup", oid)
                for l in chosen:
                    assigned[l] = f"setup:{order_id_to_product[oid]}"
                    label_config[l] = oid
                    forced_next[l] = oid

        # 여기까지 오면 이번 슬롯의 non-fresh 라벨은 전부(1~3단계 어딘가에서)
        # 배정이 끝나 있어야 한다. 남아 있다면 = CP-SAT이 낸 pool_snapshot의
        # 버킷 합계가 non-fresh 전체 인원(k-nfr[t])과 안 맞는다는 뜻이라,
        # solver.py 쪽 집계 제약이 깨졌다는 심각한 버그 신호다.
        if nonfresh_pool:
            raise RuntimeError(f"[pooling] 재구성 실패: t={t}에서 배정 안 된 non-fresh 라벨 {len(nonfresh_pool)}개 남음.")

        # ------------------------------------------------------------------
        # 4단계: fresh 라벨 처리. prod_fresh 수요만큼 "무료로 바로 생산
        # 시작"을 배정하고(이 순간부터 non-fresh로 전환 + label_config
        # 설정), 나머지 fresh 라벨은 계속 대기(idle)하며 fresh 상태를
        # 유지한다(다음 슬롯의 n_fresh_var 계산이 이 idle_fresh 인원수에
        # 의존함 - solver.py의 `nfr == idle_fresh[t-1]` 재귀 참고).
        # ------------------------------------------------------------------
        for oid in pool.compat_order_ids:
            need = pool_snapshot["prod_fresh"].get((t, oid), 0)
            if need > 0:
                chosen = take(fresh_pool, lambda l: True, need, "fresh direct-produce", oid)
                for l in chosen:
                    assigned[l] = f"produce:{order_id_to_product[oid]}"
                    label_config[l] = oid
                    label_fresh[l] = False  # 이 슬롯부터 non-fresh로 전환(오늘 하루 동안 유지)

        # 남은 fresh 라벨(prod_fresh 수요에 뽑히지 않은 나머지)은 이번
        # 슬롯도 그냥 대기 - label_fresh는 True인 채로 유지되므로 다음
        # 슬롯에서도 계속 fresh 후보로 남는다.
        for l in fresh_pool:
            assigned[l] = "idle"

        # 이번 슬롯의 최종 배정 결과를 각 라벨의 시퀀스에 기록.
        for l in labels:
            entries[l].append((day, slot_label, assigned[l]))

    return entries
