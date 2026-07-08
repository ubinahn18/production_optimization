# -*- coding: utf-8 -*-
"""
scheduling/pooling.py

동일 물리 라인 그룹(라인별 rate/workers가 완전히 같은 라인들)을 찾아내는
detect_line_pools()와, solver.py가 그 그룹을 "집계 카운트" 방식으로 풀고 난
뒤 그 해를 다시 물리 라인별 스케줄로 복원하는 reconstruct_physical_schedule()을
제공한다.

왜 필요한가: 동일 라인이 k대 있으면, 겹치지 않는 시간대의 작업을 k대 중
어디에 배정하든 비용/연속성 목적함수 값은 완전히 동일하다(symmetry). k가
크면(예: 19대) CP-SAT이 이 symmetry 때문에 "이게 최적이다"를 증명하는 데
막대한 시간을 쓰게 된다. solver.py는 이 문제를 피하려고 큰 그룹은 라인별
불리언 변수 대신 "이 슬롯에 이 그룹에서 몇 대가 무슨 상태인가"라는 집계
정수 변수로 모델링한다(라인 개별 식별자가 아예 안 나오므로 symmetry가
생기지 않음). 대신 결과를 사람이 보는 CSV/간트로 낼 때는 각 슬롯의 집계
카운트를 실제 k개의 물리 라인 라벨로 다시 분배해줘야 하는데, 그게
reconstruct_physical_schedule()의 역할이다.
"""

from __future__ import annotations

from .models import SLOT_LABELS, SLOTS_PER_DAY, Line, LinePool, Order


def detect_line_pools(lines: list[Line], orders: list[Order]) -> list[LinePool]:
    """모든 주문에 대해 rate/workers가 완전히 같은 라인들끼리 묶는다
    (호환되는 주문뿐 아니라 rate=0/미지정인 주문까지 일치해야 같은 풀).
    묶이는 라인이 없으면(=유일한 라인이면) 그 라인 혼자 k=1짜리 풀이 된다.
    """
    sorted_orders = sorted(orders, key=lambda o: o.order_id)
    order_by_id = {o.order_id: o for o in orders}

    def signature(lid: str) -> tuple:
        return tuple(
            (o.order_id, round(float(o.rate.get(lid, 0.0)), 9), int(o.workers.get(lid, 0)))
            for o in sorted_orders
        )

    groups: dict[tuple, list[str]] = {}
    for l in lines:
        groups.setdefault(signature(l.line_id), []).append(l.line_id)

    pools: list[LinePool] = []
    for member_ids in groups.values():
        rep = member_ids[0]
        compat_order_ids = [
            o.order_id for o in sorted_orders if o.rate.get(rep, 0) and o.rate.get(rep, 0) > 0
        ]
        rate = {oid: order_by_id[oid].rate[rep] for oid in compat_order_ids}
        workers = {oid: int(order_by_id[oid].workers.get(rep, 0)) for oid in compat_order_ids}
        pools.append(
            LinePool(
                member_line_ids=member_ids,
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

    알고리즘(슬롯을 t=0..T-1 순서로 처리, 라벨별 (fresh 여부, 현재 설정된
    제품) 상태를 유지):
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
    label_fresh = {l: True for l in labels}
    label_config: dict[str, str | None] = {l: None for l in labels}
    forced_next: dict[str, str] = {}  # label -> oid, 직전 슬롯에 그 제품으로 셋업해서 이번 슬롯엔 반드시 생산해야 함

    entries: dict[str, list[tuple[int, str, str]]] = {l: [] for l in labels}

    def take(pool_list: list[str], oid_pred, n: int, from_pool_desc: str, oid: str) -> list[str]:
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

        if local == 0 and t > 0:
            for l in labels:
                label_fresh[l] = True

        fresh_pool = [l for l in labels if label_fresh[l]]
        nonfresh_pool = [l for l in labels if not label_fresh[l]]
        assigned: dict[str, str] = {}

        # 1. 직전 슬롯 셋업으로 인해 이번 슬롯 생산이 강제된 라벨부터 처리.
        for l, oid in forced_next.items():
            if l not in nonfresh_pool or label_config[l] != oid:
                raise RuntimeError(f"[pooling] 재구성 실패: 셋업 강제 생산 라벨 상태 불일치 (label={l}, oid={oid}, t={t})")
            nonfresh_pool.remove(l)
            assigned[l] = f"produce:{order_id_to_product[oid]}"
        forced_this = forced_next
        forced_next = {}

        # 2. continuation: idle_nf_of / prod_nf 수요를 현재 그 제품으로 설정된 non-fresh 라벨로 채움.
        for oid in pool.compat_order_ids:
            need_prod = pool_snapshot["prod_nf"].get((t, oid), 0) - sum(1 for o in forced_this.values() if o == oid)
            if need_prod > 0:
                chosen = take(nonfresh_pool, lambda l, oid=oid: label_config[l] == oid, need_prod, "non-fresh continuation(prod)", oid)
                for l in chosen:
                    assigned[l] = f"produce:{order_id_to_product[oid]}"
            need_idle = pool_snapshot["idle_nf_of"].get((t, oid), 0)
            if need_idle > 0:
                chosen = take(nonfresh_pool, lambda l, oid=oid: label_config[l] == oid, need_idle, "non-fresh continuation(idle)", oid)
                for l in chosen:
                    assigned[l] = "idle"

        # 3. 새 셋업 수요: 남은 non-fresh 라벨 아무나(자기 자신으로의 self-loop는
        #    풀링 제약(setup <= 비-oid-설정 공급)에 의해 애초에 발생하지 않음).
        for oid in pool.compat_order_ids:
            need_setup = pool_snapshot["setup"].get((t, oid), 0)
            if need_setup > 0:
                chosen = take(nonfresh_pool, lambda l: True, need_setup, "non-fresh new setup", oid)
                for l in chosen:
                    assigned[l] = f"setup:{order_id_to_product[oid]}"
                    label_config[l] = oid
                    forced_next[l] = oid

        if nonfresh_pool:
            raise RuntimeError(f"[pooling] 재구성 실패: t={t}에서 배정 안 된 non-fresh 라벨 {len(nonfresh_pool)}개 남음.")

        # 4. fresh 라벨: prod_fresh 수요부터 채우고, 나머지는 idle(계속 fresh 유지).
        for oid in pool.compat_order_ids:
            need = pool_snapshot["prod_fresh"].get((t, oid), 0)
            if need > 0:
                chosen = take(fresh_pool, lambda l: True, need, "fresh direct-produce", oid)
                for l in chosen:
                    assigned[l] = f"produce:{order_id_to_product[oid]}"
                    label_config[l] = oid
                    label_fresh[l] = False

        for l in fresh_pool:
            assigned[l] = "idle"

        for l in labels:
            entries[l].append((day, slot_label, assigned[l]))

    return entries
