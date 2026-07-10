# -*- coding: utf-8 -*-
"""
scheduling/solver.py

CP-SAT(Google OR-Tools) 모델 구성과 2단계(비용 최소화 -> 연속성 최소화)
풀이를 담당하는 핵심 모듈. build_and_solve() 하나가 공개 API 전부다.

모델링 방식: 시간을 300개(=30일 x 10슬롯)의 이산 슬롯으로 보고, 동일한
물리 라인들의 묶음(LinePool, scheduling/pooling.py 참고)마다 '이 슬롯에
이 그룹에서 몇 대가 무슨 상태인가'(대기/셋업(어떤 제품)/생산(어떤 제품))를
정수 카운트 변수로 표현하는 CP-SAT 모델이다. 물리 라인 하나짜리 그룹도
그냥 k=1인 풀로 취급하므로, 물리 라인 개별 식별자는 모델 안에 전혀
등장하지 않는다 - 그래서 동일 라인이 여러 대 있을 때 생기는 symmetry
(겹치지 않는 시간대 작업을 어느 물리 카피에 배정하든 목적함수 값이 동일한
문제, 최대 k! 개의 동등한 배정)가 애초에 생기지 않는다. 결과를 사람이
보는 물리 라인별 스케줄로 되돌리는 건 CP-SAT과 무관한 순수 후처리이며
scheduling/pooling.py의 reconstruct_physical_schedule()이 담당한다.

핵심 모델링 규칙 (자세한 이유는 아래 코드 주석 참고):
  - 라인이 제품을 바꿀 때는 1시간짜리 셋업이 필요하다. 단, 하루 일과
    "안에서" 바뀔 때만 그렇고, 날짜가 바뀌는 시점(전날 마지막 슬롯 ->
    다음날 첫 슬롯)의 제품 변경은 '전날 밤 셋업 완료'로 간주해 셋업 슬롯이
    필요 없다.
  - 셋업은 별도 인력이 처리하므로 그 시간 동안 라인 작업자 인원은 0이다.
  - 셋업은 항상 그 다음 슬롯에 정확히 그 제품 생산으로 이어져야 한다
    (셋업이 공짜라고 해서 의미 없이 여러 번 쓰이는 것을 막는 하드 제약).
  - 목적함수는 2단계로 나뉜다: 1단계는 인건비(일일 정액임금 + 잔업수당)
    최소화, 2단계는 1단계 비용을 그대로 유지한 채(비용을 더 쓰지 않고)
    대기<->생산 전환 및 셋업 횟수를 최소화해서 "사람이 한 라인에 오래
    붙어있는" 스케줄을 우선한다(lexicographic 최적화). 그 안에서도 다시
    "새로 fresh 유닛을 투입하기보다 이미 돌던 유닛을 계속 쓰는 쪽"을
    선호하는 3차 타이브레이크가 있다 - 아래 해당 섹션 주석 참고.
"""

from __future__ import annotations

from ortools.sat.python import cp_model

from .models import (
    MONEY_SCALE,
    OVERTIME_LOCAL_SLOTS,
    SLOTS_PER_DAY,
    Line,
    LinePool,
    Order,
    ScheduleConfig,
    ScheduleResult,
)
from .pooling import build_line_pools, reconstruct_physical_schedule


def build_and_solve(lines: list[Line], orders: list[Order], config: ScheduleConfig) -> ScheduleResult:
    horizon_days = config.horizon_days
    T = horizon_days * SLOTS_PER_DAY

    for o in orders:
        if o.deadline_day is not None and not (1 <= o.deadline_day <= horizon_days):
            raise ValueError(f"주문 {o.order_id}의 마감일({o.deadline_day})이 계획기간(1~{horizon_days})을 벗어났습니다.")

    model = cp_model.CpModel()

    # ------------------------------------------------------------------
    # 각 라인 타입(Line.count대)을 풀(LinePool)로 변환. 물리 라인이
    # 하나뿐인 타입(count=1)도 k=1짜리 풀로 취급해서 전부 같은 방식
    # (집계 카운트)으로 모델링한다.
    # ------------------------------------------------------------------
    pools: list[LinePool] = build_line_pools(lines, orders)
    # build_line_pools()는 lines와 1:1 순서로 풀을 만들어내므로(라인
    # 타입 하나 = 풀 하나), zip으로 그대로 짝지을 수 있다. 이 딕셔너리의
    # 키는 물리 라인 라벨(LinePool.line_ids, 예: "line_mask_3_1")이 아니라
    # 라인 "타입" id(Line.line_type_id, 예: "line_mask_3")다 - Order.rate/
    # workers가 이제 타입 id로만 키가 잡혀 있으므로(compatible_line_types()도
    # 타입 id 목록을 돌려줌), 여기서도 타입 id 기준으로 찾아야 한다.
    line_to_pool: dict[str, LinePool] = {line.line_type_id: pool for line, pool in zip(lines, pools)}

    def compat_pools_for(compat_line_ids: list[str]) -> list[LinePool]:
        """주문의 호환 라인 타입 목록(compatible_line_types())이 속한 풀들을
        중복 없이 뽑는다."""
        seen_keys: set[int] = set()
        result: list[LinePool] = []
        for lid in compat_line_ids:
            p = line_to_pool.get(lid)
            if p is None or id(p) in seen_keys:
                continue
            seen_keys.add(id(p))
            result.append(p)
        return result

    # ------------------------------------------------------------------
    # 풀 집계 변수 + 제약. 각 풀(pool)마다, 각 슬롯(t)마다 "이 슬롯에 이
    # 그룹의 몇 대가 무슨 상태인가"를 정수 카운트(0..k)로 추적한다.
    #
    #   idle_fresh[pool,t]     : 오늘 아직 아무 것도 안 한(fresh) 라인 중
    #                            이번 슬롯도 대기하는 대수
    #   prod_fresh[pool,t,oid] : fresh 라인 중 이번 슬롯에 셋업 없이 바로
    #                            oid 생산을 시작한 대수(무료 전환)
    #   idle_nf_of[pool,t,oid] : non-fresh이면서 oid로 셋업된 라인 중
    #                            이번 슬롯도 대기하는 대수
    #   prod_nf[pool,t,oid]    : non-fresh이면서 oid로 셋업된 라인 중
    #                            이번 슬롯에 oid를 생산하는 대수
    #   pool_setup[pool,t,oid] : non-fresh 라인 중 이번 슬롯에 oid로
    #                            전환하는 1시간 셋업을 하는 대수
    #   n_fresh_var[pool,t]    : 이번 슬롯 "시작 시점"에 fresh인 대수
    #   n_cfg_nf[pool,t,oid]   : 이번 슬롯이 "끝난 시점"에 non-fresh이면서
    #                            oid로 셋업되어 있는 대수(다음 슬롯의
    #                            idle_nf_of/prod_nf "재고" 역할)
    #   n_idle_pool[pool,t]    : 이번 슬롯에 이 그룹에서 대기 중인 총 대수
    #                            (2단계 연속성 목적함수에서 씀)
    #
    # 풀링된 유닛들은 완전히 상호교환 가능해서, 어느 순간의 유휴/가동
    # 총량 차이가 곧 그 순간의 실제(달성 가능한 최소) 전환 횟수와 정확히
    # 같다 - day-boundary(fresh 강제 리셋)에도 예외 없이 이 등식이
    # 성립함은 설계 검토 과정에서 증명됨.
    # ------------------------------------------------------------------
    idle_fresh: dict[tuple, cp_model.IntVar] = {}
    prod_fresh: dict[tuple, cp_model.IntVar] = {}
    idle_nf_of: dict[tuple, cp_model.IntVar] = {}
    prod_nf: dict[tuple, cp_model.IntVar] = {}
    pool_setup: dict[tuple, cp_model.IntVar] = {}
    n_fresh_var: dict[tuple, cp_model.IntVar] = {}
    n_cfg_nf: dict[tuple, cp_model.IntVar] = {}
    n_idle_pool: dict[tuple, cp_model.IntVar] = {}

    # 생성되는 모든 결정변수를 한 군데 모아둔다. 2단계(연속성) 풀이를
    # 시작할 때 "1단계 해를 그대로 힌트로 준다"용으로 쓴다 (아래 build_and_solve
    # 하단의 model.AddHint 호출 참고).
    all_bool_vars: list[cp_model.IntVar] = []

    deadline_slot_by_order = {
        o.order_id: (
            T - 1 if o.deadline_day is None
            else (o.deadline_day - 1) * SLOTS_PER_DAY + (SLOTS_PER_DAY - 1)
        )
        for o in orders
    }

    for p in pools:
        pkey = tuple(p.line_ids)
        k = p.k
        for t in range(T):
            local = t % SLOTS_PER_DAY

            fv = model.NewIntVar(0, k, f"idleFresh[{pkey[0]},{t}]")
            idle_fresh[pkey, t] = fv
            all_bool_vars.append(fv)

            nfr = model.NewIntVar(0, k, f"nFresh[{pkey[0]},{t}]")
            n_fresh_var[pkey, t] = nfr
            all_bool_vars.append(nfr)
            if local == 0:
                model.Add(nfr == k)
            else:
                model.Add(nfr == idle_fresh[pkey, t - 1])

            idl = model.NewIntVar(0, k, f"nIdlePool[{pkey[0]},{t}]")
            n_idle_pool[pkey, t] = idl
            all_bool_vars.append(idl)

            prod_fresh_terms = []
            idle_nf_terms = []
            prod_nf_terms = []
            setup_terms_t = []

            for oid in p.compat_order_ids:
                pf = model.NewIntVar(0, k, f"prodFresh[{pkey[0]},{t},{oid}]")
                inf = model.NewIntVar(0, k, f"idleNfOf[{pkey[0]},{t},{oid}]")
                pnf = model.NewIntVar(0, k, f"prodNf[{pkey[0]},{t},{oid}]")
                su = model.NewIntVar(0, k, f"poolSetup[{pkey[0]},{t},{oid}]")
                cfg = model.NewIntVar(0, k, f"nCfgNf[{pkey[0]},{t},{oid}]")
                prod_fresh[pkey, t, oid] = pf
                idle_nf_of[pkey, t, oid] = inf
                prod_nf[pkey, t, oid] = pnf
                pool_setup[pkey, t, oid] = su
                n_cfg_nf[pkey, t, oid] = cfg
                all_bool_vars += [pf, inf, pnf, su, cfg]

                prod_fresh_terms.append(pf)
                idle_nf_terms.append(inf)
                prod_nf_terms.append(pnf)
                setup_terms_t.append(su)

                prev_cfg = n_cfg_nf[pkey, t - 1, oid] if t > 0 else 0
                # non-fresh 라인 중 oid로 남아있는(대기 또는 계속 생산) 대수는
                # 직전 슬롯 끝에 oid로 설정돼 있던 대수를 넘을 수 없음.
                model.Add(inf + pnf <= prev_cfg)
                # oid로 설정된 상태(끝 시점)의 정의: 이번 슬롯에 oid로 남아있거나
                # (대기/생산), 이번 슬롯에 oid로 셋업했거나, fresh 상태에서
                # 무료로 곧장 oid 생산을 시작한 경우.
                model.Add(cfg == inf + pnf + su + pf)
                # 셋업 공급은 "oid로 이미 설정되지 않은 non-fresh 대수"로
                # 제한(같은 제품으로 또 셋업하는 무의미한 self-loop 방지).
                # 주의: 이 부등식은 prev_cfg(전날 끝 시점의 설정 상태)가
                # nfr(오늘 시작 시점의 fresh 대수)와 "같은 모집단"을
                # 가리킬 때만 유효하다 - 즉 t==0이거나 하루 중간(local!=0)
                # 일 때. 날짜가 바뀌는 시점(t>0, local==0)에는 nfr가
                # k로 강제 리셋되어(day-boundary) k-nfr=0이 되는데, 이건
                # prev_cfg(전날 끝의 설정 대수, 보통 >0)와 무관한 값이라
                # k-nfr-prev_cfg가 음수가 되어 su의 하한(0)과 모순되는
                # 가짜 INFEASIBLE을 만든다. day-boundary에서는 어차피
                # 비-fresh 전체 인원 집계 등식(k-nfr=0)만으로 su=0이
                # 이미 강제되므로 이 부등식은 그때는 걸 필요가 없다
                # (실제로 걸면 안 된다 - 실제 버그로 확인됨).
                if t == 0 or local != 0:
                    model.Add(su <= k - nfr - prev_cfg)

                if local == SLOTS_PER_DAY - 1:
                    model.Add(su == 0)
                if t > deadline_slot_by_order[oid]:
                    model.Add(su == 0)
                    model.Add(pf == 0)
                    model.Add(pnf == 0)

            # fresh 라인 전체 = 이번 슬롯 대기 + 이번 슬롯에 뭔가로 생산 시작
            model.Add(fv + sum(prod_fresh_terms) == nfr)
            # non-fresh 라인 전체 = 대기/생산/셋업 중 하나
            model.Add(sum(idle_nf_terms) + sum(prod_nf_terms) + sum(setup_terms_t) == k - nfr)
            model.Add(idl == fv + sum(idle_nf_terms))

        # 셋업 -> 다음 슬롯 반드시 그 제품 생산(하드 제약). t, t+1 양쪽
        # 변수가 다 만들어진 뒤에 걸어야 하므로 별도 루프로 뺀다.
        for t in range(T - 1):
            for oid in p.compat_order_ids:
                model.Add(prod_nf[pkey, t + 1, oid] >= pool_setup[pkey, t, oid])

    # ------------------------------------------------------------------
    # 생산량 집계 + 마감일 내 수량 충족 제약.
    #   각 슬롯 생산은 "완전 선형"이라 그 슬롯 동안 rate[line]만큼 정확히
    #   생산된다(시작손실/비선형 없음 가정을 그대로 반영).
    #
    #   마감일이 있는 주문: '마감일까지 필요수량 이상 생산'을 하드
    #   제약으로 건다.
    #   ASAP 주문(deadline_day=None): 하드 수량 제약을 아예 걸지 않는다.
    #   대신 아래 backlog 비용 섹션에서 "늦게 끝날수록 손해"를 목적함수에
    #   반영해서, 완료 시점을 하드 제약이 아니라 비용 트레이드오프로
    #   다룬다(docstring 및 Order.deadline_day 참고).
    # ------------------------------------------------------------------
    produced_qty: dict[str, cp_model.IntVar] = {}
    for o in orders:
        oid = o.order_id
        compat = o.compatible_line_types()
        if not compat:
            raise ValueError(f"주문 {oid}({o.product_id})를 생산할 수 있는 라인이 없습니다(rate가 전부 0/미지정).")

        eligible_slots = T if o.is_asap() else min(
            (o.deadline_day - 1) * SLOTS_PER_DAY + SLOTS_PER_DAY, T
        )
        max_rate_sum = sum(int(round(o.rate[lid])) for lid in compat)
        upper_bound = max_rate_sum * eligible_slots + 1  # 이론상 최대 생산량(모든 호환라인이 마감일까지 이 제품만 생산) + 여유 1

        terms = []
        for p in compat_pools_for(compat):
            pkey = tuple(p.line_ids)
            rate_val = int(round(p.rate[oid]))
            if rate_val <= 0:
                continue
            for t in range(eligible_slots):
                terms.append((rate_val, prod_fresh[pkey, t, oid] + prod_nf[pkey, t, oid]))

        pv = model.NewIntVar(0, max(upper_bound, o.quantity), f"produced[{oid}]")
        model.Add(pv == sum(coef * var for coef, var in terms))
        if not o.is_asap():
            model.Add(pv >= o.quantity)  # 마감일 전까지 필요수량 이상 생산(시간 슬롯 단위라 약간의 초과생산은 허용)
        produced_qty[oid] = pv

    # ------------------------------------------------------------------
    # ASAP 주문의 backlog(진도 지연) 비용 집계.
    #
    #   처음엔 "그날까지 못 만든 전체 잔량"(quantity - 누적생산량)을
    #   그대로 backlog로 썼는데, 이러면 계획 첫날부터 "아직 하나도 안
    #   만들었으니 backlog = quantity 전체"가 되어 버려서, 솔버가 그
    #   페널티를 줄이려고 계획 첫 며칠에 인원을 몰아넣고 여러 라인을
    #   동시에 돌려 무리하게 전량을 앞당겨 만들어버리는 부작용이 있었다
    #   (실제로 확인된 문제).
    #
    #   대신 "계획기간 내내 매일 똑같은 양씩 만들어서 마지막 날에 정확히
    #   다 끝낸다"는 가상의 선형 목표 진도(expected_cum)를 날짜별로 잡고,
    #   그 목표보다 실제 누적생산량이 뒤처진 만큼만 backlog로 잡는다.
    #     expected_cum[d] = quantity * (d+1) / horizon_days  (반올림)
    #     backlog[d] = max(0, expected_cum[d] - 실제 누적생산량[d])
    #   이러면 계획 첫날의 목표치 자체가 quantity/horizon_days 정도로
    #   작으니, 하루이틀 늦어도 큰 페널티가 안 붙고, 목표 진도보다
    #   앞서가는 건(초과생산) 아무 페널티가 없다(음수가 안 되게 0에서
    #   막아둠). 계획 마지막 날(d=horizon_days-1)엔 expected_cum이
    #   정확히 quantity와 같아지므로, 최종적으로 다 못 만들었으면 그만큼
    #   그대로 backlog로 잡혀서 "결국 못 끝낸 양"도 여전히 놓치지 않는다.
    # ------------------------------------------------------------------
    backlog_cost_terms: list[tuple[cp_model.IntVar, float]] = []
    backlog_by_order_day: dict[tuple[str, int], cp_model.IntVar] = {}  # (oid, d) -> backlog var, 리포트용
    backlog_rate_by_order: dict[str, float] = {}  # oid -> 하루당 지연비용, 리포트용 총비용 재계산에 사용
    for o in orders:
        if not o.is_asap():
            continue
        oid = o.order_id
        compat = o.compatible_line_types()
        rate_per_day = o.backlog_cost_per_unit_per_day
        if rate_per_day is None:
            rate_per_day = config.default_backlog_cost_per_unit_per_day
        backlog_rate_by_order[oid] = rate_per_day

        # 필요수량을 다 채운 뒤에도 그 라인이 이 제품을 계속 만들지 말라는
        # 보장은 없으므로, 누적생산량 변수의 상한을 quantity가 아니라 '이론상
        # 30일 내내 이 제품만 만들었을 때의 최대치'로 넉넉하게 잡는다
        # (quantity로 좁게 잡으면 실제로 그보다 더 생산하는 해에서
        # INFEASIBLE이 나버린다).
        max_rate_sum = sum(int(round(o.rate[lid])) for lid in compat)
        cum_upper_bound = max(max_rate_sum * T + 1, o.quantity)

        # 날짜별 선형 목표 진도. d=horizon_days-1(마지막 날)에는 정확히
        # o.quantity가 되도록 반올림한다.
        expected_cum = [
            int(round(o.quantity * (d + 1) / horizon_days)) for d in range(horizon_days)
        ]

        compat_pools = compat_pools_for(compat)

        prev_cum_var = None
        for d in range(horizon_days):
            day_start_t = d * SLOTS_PER_DAY
            day_terms = []
            for p in compat_pools:
                pkey = tuple(p.line_ids)
                rate_val = int(round(p.rate[oid]))
                if rate_val <= 0:
                    continue
                for t in range(day_start_t, day_start_t + SLOTS_PER_DAY):
                    day_terms.append((rate_val, prod_fresh[pkey, t, oid] + prod_nf[pkey, t, oid]))
            day_sum = sum(coef * var for coef, var in day_terms) if day_terms else 0

            cum_var = model.NewIntVar(0, cum_upper_bound, f"cumProd[{oid},{d}]")
            if prev_cum_var is None:
                model.Add(cum_var == day_sum)
            else:
                model.Add(cum_var == prev_cum_var + day_sum)
            prev_cum_var = cum_var

            bl = model.NewIntVar(0, o.quantity, f"backlog[{oid},{d}]")
            model.Add(bl >= expected_cum[d] - cum_var)
            backlog_cost_terms.append((bl, rate_per_day))
            backlog_by_order_day[oid, d] = bl

    # ------------------------------------------------------------------
    # 인원 수요 집계: 슬롯별로 '그 순간 라인들에 배치되어 있어야 하는
    # 인원 합계'.
    #
    #   생산 중인 라인만 라인 작업자(line worker) 인원을 필요로 한다.
    #   셋업(하루 일과 중 제품이 바뀌어서 발생하는, 위 전이 제약 참고)은
    #   별도 인력이 처리하는 작업이라 그 시간 동안 이 라인에 투입되는
    #   생산 인원수는 0으로 본다 - 그래서 아래 집계에서 셋업은 아예
    #   빼고 생산(prod_fresh+prod_nf)만 인원 수요에 반영한다. (참고:
    #   야간 셋업은 애초에 셋업 슬롯 자체가 생기지 않으므로 - 위 전이
    #   제약 참고 - 별도 처리가 필요 없다.)
    # ------------------------------------------------------------------
    max_possible_workers = sum(
        p.workers.get(oid, 0) * p.k for p in pools for oid in p.compat_order_ids
    )
    max_possible_workers = max(max_possible_workers, 1)

    total_workers_var: dict[int, cp_model.IntVar] = {}
    for t in range(T):
        terms = []
        for p in pools:
            pkey = tuple(p.line_ids)
            for oid in p.compat_order_ids:
                w = int(p.workers.get(oid, 0))
                if w <= 0:
                    continue
                terms.append((w, prod_fresh[pkey, t, oid]))
                terms.append((w, prod_nf[pkey, t, oid]))
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
    # 목적함수: 일일 정액임금 합 + 잔업수당 합 + ASAP 주문 backlog 비용 합
    #   (최소화). 금액은 MONEY_SCALE 배율로 정수화해서 다루고, 최종
    #   리포트에서 다시 나눠서 원래 단위로 보여준다. backlog 비용을 여기
    #   1단계 목적함수에 바로 넣는 이유: "사람을 더 써서 당길지 vs 천천히
    #   만들지"는 인건비와 직접 트레이드오프되는 진짜 비용 결정이라,
    #   2단계(연속성, 비용을 전혀 안 늘리는 순수 타이브레이크)가 아니라
    #   1단계에서 같이 최소화되어야 한다.
    # ------------------------------------------------------------------
    daily_wage_scaled = int(round(config.daily_wage * MONEY_SCALE))
    ot_hour_wage_scaled = int(round(config.resolved_hourly_wage() * config.overtime_multiplier * MONEY_SCALE))

    objective_terms = []
    for d in range(horizon_days):
        objective_terms.append(daily_workforce_var[d] * daily_wage_scaled)
        for s in OVERTIME_LOCAL_SLOTS:
            objective_terms.append(total_workers_var[d * SLOTS_PER_DAY + s] * ot_hour_wage_scaled)
    for bl, rate_per_day in backlog_cost_terms:
        objective_terms.append(bl * int(round(rate_per_day * MONEY_SCALE)))
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
            "produced_qty": {k: solver.Value(v) for k, v in produced_qty.items()},
            "total_workers": {t: solver.Value(v) for t, v in total_workers_var.items()},
            "daily_workforce": {d: solver.Value(v) for d, v in daily_workforce_var.items()},
            "backlog_by_order_day": {k: solver.Value(v) for k, v in backlog_by_order_day.items()},
            "idle_fresh": {k: solver.Value(v) for k, v in idle_fresh.items()},
            "prod_fresh": {k: solver.Value(v) for k, v in prod_fresh.items()},
            "idle_nf_of": {k: solver.Value(v) for k, v in idle_nf_of.items()},
            "prod_nf": {k: solver.Value(v) for k, v in prod_nf.items()},
            "pool_setup": {k: solver.Value(v) for k, v in pool_setup.items()},
            "n_idle_pool": {k: solver.Value(v) for k, v in n_idle_pool.items()},
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
    #   더 쓰지 않음), 그 안에서 각 풀이 대기<->생산을 왔다갔다하는
    #   전환 횟수 + 셋업 횟수의 합("연속성 점수")을 추가로 최소화한다.
    #   즉 "비용이 같다면 사람이 한 라인에 최대한 오래 붙어있는 스케줄을
    #   우선한다"는 타이브레이크를, 실제 비용을 절대 희생하지 않는 방식으로
    #   정확히 구현한 것(순수 lexicographic 2단계 최적화).
    #   - 대기<->생산 전환: sw >= |n_idle_pool[t]-n_idle_pool[t-1]| 형태의
    #     부등식 두 개만 걸어두면, 목적함수가 sw를 최소화하려 하므로
    #     자동으로 정확한 값으로 수렴한다(등호 제약을 따로 안 걸어도 됨).
    #     풀링된 유닛들은 완전히 상호교환 가능해서, 이 값이 곧 그 순간
    #     실제로 달성 가능한 최소 전환 횟수와 정확히 같다(day-boundary도
    #     예외 없이 동일한 식으로 처리된다).
    #   - 셋업 횟수: pool_setup 변수를 전부 더한 값 = 전체 기간 동안
    #     발생한 제품 전환(셋업) 총 횟수. 셋업(Y) 다음 슬롯은 항상 그
    #     제품(Y) 생산으로 이어지도록 하드 제약을 걸어뒀기 때문에, 이
    #     값은 정확히 '실제로 발생한 제품 전환 이벤트 수'와 같다. 다만
    #     대기<->생산 전환처럼 셋업도 "생산 중이던 라인이 하던 일을
    #     멈추고, 다른 걸 준비해서, 다시 생산으로 돌아오는" 동일한
    #     성격의 사건이므로(대기가 '들어감+나옴' 2점인 것과 대칭적으로),
    #     셋업 1회도 2점으로 계산한다(가중치 2배). 그래서 생산(A)->
    #     대기->생산(A)(2점)와 생산(A)->셋업(B)->생산(B)(2점)가 이제
    #     동등하게 취급되고, 대기 없이 곧바로 셋업으로 넘어가는 쪽이
    #     여전히 더 싸다(대기까지 거치면 셋업 2점 + 대기 2점 = 4점).
    #   - AddHint로 1단계 해를 2단계 탐색의 출발점으로 알려준다. 1단계 해는
    #     이미 '비용 <= best_cost_scaled' 제약을 등호로 만족하므로 2단계
    #     모델에서도 즉시 실행가능(feasible)하다 - 그래서 힌트를 주면 거의
    #     항상 몇 초 안에 최소 하나의 해(=1단계 해 자체)를 확보하고 시작하게
    #     되어, 시간 안에 해를 하나도 못 찾는 상황을 방지한다.
    # ------------------------------------------------------------------
    if config.optimize_continuity:
        model.Add(sum(objective_terms) <= best_cost_scaled)

        pool_switch_terms = []
        for p in pools:
            pkey = tuple(p.line_ids)
            for t in range(1, T):
                sw = model.NewIntVar(0, p.k, f"poolIdleSwitch[{pkey[0]},{t}]")
                model.Add(sw >= n_idle_pool[pkey, t] - n_idle_pool[pkey, t - 1])
                model.Add(sw >= n_idle_pool[pkey, t - 1] - n_idle_pool[pkey, t])
                pool_switch_terms.append(sw)
        pool_setup_terms = list(pool_setup.values())

        # ------------------------------------------------------------------
        # 3차 타이브레이크: "새로 fresh 유닛을 쓰는 것"(prod_fresh)을
        # 최소화한다.
        #
        #   왜 필요한가: 같은 슬롯에 idle_nf_of[oid]=1(이미 오늘 뭔가 해서
        #   non-fresh인 유닛이 계속 대기)과 prod_fresh[oid]=1(오늘 아직
        #   아무것도 안 한 fresh 유닛이 무료로 그 제품 생산을 시작)이
        #   동시에 나오면, 이 둘의 "생산량/인건비/switch 집계"는 완전히
        #   동일하다 - idle 쪽과 produce 쪽을 서로 바꿔도(이미 돌던 유닛이
        #   계속 생산하고, fresh 유닛이 대신 대기) 목적함수는 1도 안
        #   바뀐다. 그런데 물리 라인으로 복원할 땐(reconstruct_physical_
        #   schedule) fresh와 non-fresh는 서로 다른 라벨일 수밖에 없어서,
        #   "이미 그 제품 하던 라인이 버젓이 놀고 있는데 굳이 다른 라인을
        #   새로 투입"하는 것처럼 보이는 스케줄이 나올 수 있다(실제로
        #   확인된 문제 - 겹치지 않는 시간대에 identical 라인끼리 쓸데없이
        #   나뉘는 것과 본질적으로 같은 문제가 fresh/non-fresh 메커니즘을
        #   통해 하루 안에서도 발생함).
        #
        #   그래서 1·2단계 값을 전혀 건드리지 않는 선에서(정수 배율로
        #   확실히 하위 우선순위가 되게 만들어), "가능하면 새 fresh 유닛을
        #   쓰기보다 이미 돌고 있던 유닛을 계속 쓰는 쪽"을 추가로
        #   선호한다. 이건 순수 타이브레이크라 실행가능 해 집합이 전혀
        #   줄어들지 않고, 위에서 증명했듯 1·2단계 목적함수 값과 절대
        #   상충하지 않는다.
        # ------------------------------------------------------------------
        all_prod_fresh_terms = list(prod_fresh.values())
        tiebreak_scale = sum(p.k for p in pools) * T + 1 if pools else 1

        model.Minimize(
            (sum(pool_switch_terms) + 2 * sum(pool_setup_terms)) * tiebreak_scale
            + sum(all_prod_fresh_terms)
        )

        for var, val in zip(all_bool_vars, all_bool_vals):
            model.AddHint(var, val)

        solver.parameters.max_time_in_seconds = config.secondary_time_limit_seconds
        status2 = solver.Solve(model)

        if status2 in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            continuity_score = int(round(solver.ObjectiveValue())) // tiebreak_scale
            status_name = f"{status_name} + 2단계:{solver.StatusName(status2)}"
            best_snapshot = snapshot(solver)  # 2단계 해로 교체 (이때부터는 이 해로 최종 결과를 만든다).
        else:
            print("[경고] 2단계(연속성) 최적화가 시간 내에 해를 못 찾아 1단계 결과를 그대로 사용합니다.")

    # ------------------------------------------------------------------
    # 결과 추출 (전부 best_snapshot에서만 읽는다 - solver.Value()를 여기서
    # 다시 호출하지 않는다. 위 snapshot()의 이유 설명 참고).
    # ------------------------------------------------------------------
    # labor_cost: 실제로 지급되는 돈(일일 정액임금 + 잔업수당). backlog_cost는
    # ASAP 주문이 늦어질수록 커지는 가상의 페널티일 뿐 실제 지출이 아니므로
    # 반드시 분리해서 리턴한다 - 안 그러면 "총비용"만 보고 인건비가 얼마나
    # 되는지 헷갈리게 된다.
    labor_cost_scaled = sum(
        best_snapshot["daily_workforce"][d] * daily_wage_scaled for d in range(horizon_days)
    ) + sum(
        best_snapshot["total_workers"][d * SLOTS_PER_DAY + s] * ot_hour_wage_scaled
        for d in range(horizon_days)
        for s in OVERTIME_LOCAL_SLOTS
    )
    backlog_cost_scaled = sum(
        bl_val * int(round(backlog_rate_by_order[oid] * MONEY_SCALE))
        for (oid, d), bl_val in best_snapshot["backlog_by_order_day"].items()
    )
    labor_cost = labor_cost_scaled / MONEY_SCALE
    backlog_cost = backlog_cost_scaled / MONEY_SCALE
    total_cost = labor_cost + backlog_cost

    daily_workforce = {d + 1: best_snapshot["daily_workforce"][d] for d in range(horizon_days)}
    overtime_workers = {
        d + 1: {
            "17-18": best_snapshot["total_workers"][d * SLOTS_PER_DAY + OVERTIME_LOCAL_SLOTS[0]],
            "18-19": best_snapshot["total_workers"][d * SLOTS_PER_DAY + OVERTIME_LOCAL_SLOTS[1]],
        }
        for d in range(horizon_days)
    }

    order_id_to_product = {o.order_id: o.product_id for o in orders}
    line_activity: dict[str, list[tuple[int, str, str, str]]] = {}
    for p in pools:
        pkey = tuple(p.line_ids)
        pool_snapshot = {
            "idle_fresh": {t: best_snapshot["idle_fresh"][pkey, t] for t in range(T)},
            "prod_fresh": {
                (t, oid): best_snapshot["prod_fresh"][pkey, t, oid]
                for t in range(T) for oid in p.compat_order_ids
            },
            "idle_nf_of": {
                (t, oid): best_snapshot["idle_nf_of"][pkey, t, oid]
                for t in range(T) for oid in p.compat_order_ids
            },
            "prod_nf": {
                (t, oid): best_snapshot["prod_nf"][pkey, t, oid]
                for t in range(T) for oid in p.compat_order_ids
            },
            "setup": {
                (t, oid): best_snapshot["pool_setup"][pkey, t, oid]
                for t in range(T) for oid in p.compat_order_ids
            },
        }
        line_activity.update(
            reconstruct_physical_schedule(p, pool_snapshot, T, order_id_to_product)
        )

    order_fulfillment = {}
    for o in orders:
        oid = o.order_id
        compat = o.compatible_line_types()
        compat_pools = compat_pools_for(compat)
        # 마감일 완료 여부와 별개로, '언제 누적 생산량이 필요수량에 처음
        # 도달했는지'(완료일)를 슬롯 단위로 다시 훑어서 계산한다.
        cumulative = 0
        completion_day = None
        for t in range(T):
            for p in compat_pools:
                pkey = tuple(p.line_ids)
                n = best_snapshot["prod_fresh"][pkey, t, oid] + best_snapshot["prod_nf"][pkey, t, oid]
                if n:
                    cumulative += int(round(p.rate[oid])) * n
            if completion_day is None and cumulative >= o.quantity:
                completion_day = t // SLOTS_PER_DAY + 1
        final_backlog = None
        if o.is_asap():
            final_backlog = best_snapshot["backlog_by_order_day"][oid, horizon_days - 1]
        order_fulfillment[oid] = {
            "product_id": o.product_id,
            "required": o.quantity,
            "produced": best_snapshot["produced_qty"][oid],
            "deadline_day": o.deadline_day,  # None이면 ASAP 주문
            "completion_day": completion_day,
            "final_backlog": final_backlog,  # ASAP 주문에서만 값이 채워짐 (계획기간 마지막날 기준 잔여 미생산량)
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
        labor_cost=labor_cost,
        backlog_cost=backlog_cost,
    )
