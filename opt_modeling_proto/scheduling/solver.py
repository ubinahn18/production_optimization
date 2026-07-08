# -*- coding: utf-8 -*-
"""
scheduling/solver.py

CP-SAT(Google OR-Tools) 모델 구성과 2단계(비용 최소화 -> 연속성 최소화)
풀이를 담당하는 핵심 모듈. build_and_solve() 하나가 공개 API 전부다.

모델링 방식: 시간을 300개(=30일 x 10슬롯)의 이산 슬롯으로 보고, 각
(라인, 슬롯) 쌍마다 '이 슬롯에 이 라인이 뭘 하고 있는가'(대기/셋업(어떤
제품)/생산(어떤 제품))를 불리언 변수로 표현하는 CP-SAT 모델이다.

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
    라인의 대기<->생산 전환 및 셋업 횟수를 최소화해서 "사람이 한 라인에
    오래 붙어있는" 스케줄을 우선한다(lexicographic 최적화).
"""

from __future__ import annotations

from ortools.sat.python import cp_model

from .models import (
    MONEY_SCALE,
    OVERTIME_LOCAL_SLOTS,
    SLOT_LABELS,
    SLOTS_PER_DAY,
    Line,
    Order,
    ScheduleConfig,
    ScheduleResult,
)


def build_and_solve(lines: list[Line], orders: list[Order], config: ScheduleConfig) -> ScheduleResult:
    horizon_days = config.horizon_days
    T = horizon_days * SLOTS_PER_DAY

    for o in orders:
        if o.deadline_day is not None and not (1 <= o.deadline_day <= horizon_days):
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
        # ASAP 주문(deadline_day=None)은 "계획기간 마지막 슬롯이 마감"인
        # 것처럼 취급해서, 아래 '마감 이후 생산/셋업 금지' 로직이 사실상
        # 절대 발동하지 않게(=전체 30일 어디서나 생산 가능하게) 만든다.
        # 실제 완료를 강제하는 하드 제약은 따로 걸지 않고, 대신 아래
        # backlog 비용으로 "늦게 끝날수록 손해"만 부여한다.
        deadline_slot = {
            o.order_id: (
                T - 1 if o.deadline_day is None
                else (o.deadline_day - 1) * SLOTS_PER_DAY + (SLOTS_PER_DAY - 1)
            )
            for o in compat_orders
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

                # 그 날의 마지막 슬롯(18-19시)에서의 셋업은 항상 손해다 -
                # 그 시점에 제품을 바꿔봤자 그날은 더 생산할 시간이 없고,
                # 다음날 아침은 어차피 '프레시(fresh)' 구간이라 셋업 없이
                # 아무 제품이나 바로 시작할 수 있으므로 굳이 전날 밤에
                # 미리 셋업할 이유가 없다(오히려 다음날로 미루면 공짜다).
                # 그래서 이 슬롯의 셋업은 아예 금지한다 - 셋업이 공짜(인원
                # 0)라서 솔버가 아무 의미 없이 야근시간에 셋업을 '채워넣는'
                # 걸 막기 위한 하드 제약. (그 날의 첫 슬롯을 포함한 '프레시'
                # 구간에서의 셋업 금지는 아래 전이 제약에서 fresh 변수로
                # 일괄 처리한다.)
                local = t % SLOTS_PER_DAY
                if local == SLOTS_PER_DAY - 1:
                    model.Add(su == 0)

            # 슬롯 하나 = 대기/셋업(제품중 하나)/생산(제품중 하나) 중 정확히 1개
            model.Add(sum(activity_terms) == 1)
            # 셋업 상태도 '미설정' 또는 '특정 제품 1개로 설정' 중 정확히 1개
            model.Add(sum(cfg_terms) == 1)

    # ------------------------------------------------------------------
    # "프레시(fresh)" 상태: 그 날 시작(day-start) 이후로 아직 생산도
    # 셋업도 한 번도 안 하고 계속 대기만 하고 있는 중이라는 뜻.
    #   fresh[lid,t] = (그 날의 첫 슬롯이다) OR (fresh[lid,t-1] AND 직전
    #   슬롯이 대기였다)
    #   즉 하루가 시작된 뒤로 쭉 대기만 하고 있으면 fresh가 계속 유지되고,
    #   생산이든 셋업이든 뭐 하나라도 하는 순간 그 날은 더 이상 fresh가
    #   아니게 된다(다음날 아침에 다시 초기화).
    #
    #   왜 필요한가(사용자 확인 반영): "전날 밤에 셋업을 미리 해두고,
    #   오늘 첫 슬롯은 대기하다가 두 번째 슬롯부터 일을 시작"하는 것도
    #   당연히 가능해야 한다. 그런데 예전 버전은 "그 날의 정확히 첫
    #   슬롯"에서 생산을 시작할 때만 무료 전환을 인정해서, 첫 슬롯에
    #   대기하고 그 다음 슬롯부터 생산을 시작하면 불필요한 낮 시간 셋업을
    #   강제하는 문제가 있었다. fresh를 도입하면 "그 날 아직 아무 일도
    #   안 했으면 언제 첫 생산을 시작하든 무료"로 정확히 일반화된다.
    # ------------------------------------------------------------------
    fresh: dict[tuple[str, int], cp_model.IntVar] = {}
    for lid in line_ids:
        for t in range(T):
            local = t % SLOTS_PER_DAY
            fv = model.NewBoolVar(f"fresh[{lid},{t}]")
            if local == 0:
                model.Add(fv == 1)  # 그 날의 첫 슬롯은 항상 프레시 (전날 밤 셋업 가능 구간의 시작점)
            else:
                prev_fresh = fresh[lid, t - 1]
                prev_idle = is_idle[lid, t - 1]
                # fv = prev_fresh AND prev_idle (0/1 변수의 표준 AND 선형화)
                model.Add(fv <= prev_fresh)
                model.Add(fv <= prev_idle)
                model.Add(fv >= prev_fresh + prev_idle - 1)
            fresh[lid, t] = fv
            all_bool_vars.append(fv)

    # ------------------------------------------------------------------
    # 전이(transition) 제약: 활동에 따라 '셋업 상태'가 어떻게 바뀌는지 정의.
    #
    #   핵심 규칙(사용자 확인 반영): 하루 일과 안에서 제품이 바뀔 때만
    #   셋업 슬롯이 필요하다. 그 날 아직 프레시한 상태에서(=하루 시작 후
    #   계속 대기만 하다가) 생산을 시작하는 건 '전날 밤에 이미 셋업해둔
    #   것'으로 간주해 셋업 슬롯 없이 곧바로 허용한다.
    #
    #   [생산]
    #     * 프레시한 상태에서 생산 시작: 직전 상태와 무관하게 곧바로 그
    #       제품으로 설정됨 (무료 전환).
    #     * 프레시하지 않은 상태(그 날 이미 뭔가 했음)에서 생산: 직전
    #       슬롯에 이미 같은 제품으로 셋업되어 있어야 함("같은 제품
    #       연속이면 셋업 불필요").
    #   [셋업]
    #     * 프레시한 동안에는 셋업이 의미가 없다(무료 전환이 이미
    #       가능하므로) - 셋업 변수를 0으로 고정해 탐색공간에서 제외.
    #     * 프레시하지 않을 때만 실제 셋업이 필요/가능하다.
    #   [대기]
    #     * 직전 슬롯의 셋업 상태를 그대로 유지(day-start든 아니든 동일 -
    #       다만 계획 전체의 첫 슬롯(t=0)은 참조할 '이전'이 없으므로
    #       별도 처리).
    #
    #   '지금 이 활동이면 이 상태가 된다'는 정방향 함의만 걸어주면
    #   충분하다 - 각 t에서 '셋업 상태는 정확히 하나'라는 제약이 이미
    #   있어서, 특정 상태로 강제되는 순간 다른 상태들은 자동으로
    #   배제되기 때문이다(그래서 반대 방향 제약은 따로 필요 없음).
    # ------------------------------------------------------------------
    for lid in line_ids:
        compat_orders = compat_orders_by_line[lid]
        for t in range(T):
            fv = fresh[lid, t]

            for o in compat_orders:
                oid = o.order_id
                pr = is_prod[lid, t, oid]
                su = is_setup[lid, t, oid]
                cfg = configured[lid, t, oid]

                # 프레시한 동안엔 셋업 자체를 금지(무료 전환이 이미
                # 가능하므로 낮 시간 셋업은 항상 손해).
                model.Add(su == 0).OnlyEnforceIf(fv)

                # 생산 중이거나 셋업했다면 -> 이번 슬롯 종료 시점엔 이 제품으로 설정된 상태.
                model.Add(cfg == 1).OnlyEnforceIf(pr)
                model.Add(cfg == 1).OnlyEnforceIf(su)

                if t > 0:
                    prev_cfg = configured[lid, t - 1, oid]
                    # 프레시하지 않을 때만 "직전 슬롯에 이미 이 제품으로
                    # 셋업되어 있어야 생산 가능"이 적용된다. 프레시하면
                    # 무료 전환이라 이 제약이 없다.
                    model.Add(prev_cfg == 1).OnlyEnforceIf([pr, fv.Not()])
                    # (성능 최적화용 중복 제약) 이미 이 제품으로 설정돼
                    # 있는데 같은 제품으로 또 셋업하는 건 비용만 들고
                    # 아무 의미가 없으므로 금지(프레시하면 su가 이미 0으로
                    # 고정돼 있어 무해).
                    model.Add(su + prev_cfg <= 1)

                # 핵심 제약: 셋업은 '실제로 그 제품을 생산하기 직전에만'
                # 할 수 있다 - 셋업(oid) 다음 슬롯은 반드시 그 oid를
                # 생산해야 한다. 셋업이 인원 0이라 공짜라는 이유로 솔버가
                # "셋업 2연속"이나 "쓰지도 않을 제품으로 셋업" 같은
                # 무의미한 조합을 만들지 못하게 막는 하드 제약이다.
                # day-end 슬롯은 이미 셋업 자체가 금지돼 있으므로(su=0
                # 고정) t+1을 참조해도 되는 경우에만(=계획기간 전체의
                # 마지막 슬롯이 아닐 때만) 건다.
                if t + 1 < T:
                    model.Add(is_prod[lid, t + 1, oid] == 1).OnlyEnforceIf(su)

            # 대기 중이면 직전 슬롯의 셋업 상태 전체를 그대로 유지.
            idle_v = is_idle[lid, t]
            if t == 0:
                # 계획 첫날 이전엔 참조할 '전날'이 없으므로, 대기 중이면
                # 아직 아무 것도 정해지지 않은 '미설정' 상태로 취급한다.
                model.Add(configured_none[lid, 0] == is_idle[lid, 0])
            else:
                model.Add(configured_none[lid, t] == configured_none[lid, t - 1]).OnlyEnforceIf(idle_v)
                for o in compat_orders:
                    oid = o.order_id
                    model.Add(configured[lid, t, oid] == configured[lid, t - 1, oid]).OnlyEnforceIf(idle_v)

    # ------------------------------------------------------------------
    # 생산량 집계 + 마감일 내 수량 충족 제약.
    #   각 슬롯 생산은 "완전 선형"이라 그 슬롯 동안 rate[line]만큼 정확히
    #   생산된다(시작손실/비선형 없음 가정을 그대로 반영).
    #
    #   마감일이 있는 주문: 기존과 동일하게 '마감일까지 필요수량 이상
    #   생산'을 하드 제약으로 건다.
    #   ASAP 주문(deadline_day=None): 하드 수량 제약을 아예 걸지 않는다.
    #   대신 아래 backlog 비용 섹션에서 "늦게 끝날수록 손해"를 목적함수에
    #   반영해서, 완료 시점을 하드 제약이 아니라 비용 트레이드오프로
    #   다룬다(이 파일 상단 docstring 및 Order.deadline_day 설명 참고).
    # ------------------------------------------------------------------
    produced_qty: dict[str, cp_model.IntVar] = {}
    for o in orders:
        oid = o.order_id
        compat = o.compatible_lines()
        if not compat:
            raise ValueError(f"주문 {oid}({o.product_id})를 생산할 수 있는 라인이 없습니다(rate가 전부 0/미지정).")

        eligible_slots = T if o.is_asap() else min(
            (o.deadline_day - 1) * SLOTS_PER_DAY + SLOTS_PER_DAY, T
        )
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
        compat = o.compatible_lines()
        rate_per_day = o.backlog_cost_per_unit_per_day
        if rate_per_day is None:
            rate_per_day = config.default_backlog_cost_per_unit_per_day
        backlog_rate_by_order[oid] = rate_per_day

        # 필요수량을 다 채운 뒤에도 그 라인이 이 제품을 계속 만들지 말라는
        # 보장은 없으므로(그럴 이유는 없지만 하드 제약으로 막아두진
        # 않았다), 누적생산량 변수의 상한을 quantity가 아니라 '이론상
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

        prev_cum_var = None
        for d in range(horizon_days):
            day_start_t = d * SLOTS_PER_DAY
            day_terms = []
            for lid in compat:
                rate_val = int(round(o.rate[lid]))
                if rate_val <= 0:
                    continue
                for t in range(day_start_t, day_start_t + SLOTS_PER_DAY):
                    day_terms.append((rate_val, is_prod[lid, t, oid]))
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
            "is_idle": {k: solver.Value(v) for k, v in is_idle.items()},
            "is_setup": {k: solver.Value(v) for k, v in is_setup.items()},
            "is_prod": {k: solver.Value(v) for k, v in is_prod.items()},
            "produced_qty": {k: solver.Value(v) for k, v in produced_qty.items()},
            "total_workers": {t: solver.Value(v) for t, v in total_workers_var.items()},
            "daily_workforce": {d: solver.Value(v) for d, v in daily_workforce_var.items()},
            "backlog_by_order_day": {k: solver.Value(v) for k, v in backlog_by_order_day.items()},
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
