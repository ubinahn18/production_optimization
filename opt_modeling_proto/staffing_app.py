# -*- coding: utf-8 -*-
"""
staffing_app.py

30일 최적화 결과(line_schedule.csv)를 바탕으로, 특정 하루 안에서 "실제
인원 배치"를 수작업으로 해볼 수 있는 로컬 웹 도구. gui_app.py(전체 30일
계획을 CP-SAT으로 돌리는 도구)와는 별개로, 이미 나온 계획을 놓고 사람이
직접 라인 배정/인원 편성을 조정하는 용도다 - 여기엔 최적화나 알고리즘이
전혀 없다. 전부 사람이 드래그앤드롭으로 직접 조정하고, 그 결과를 저장/
확인하는 것뿐이다.

핵심 개념 두 가지:
  1) 슬롯 교환: 같은 슬롯(시간) 안에서는 어느 라인이 뭘 하든 그 슬롯의
     총 필요인원이 안 바뀌므로 자유롭게 맞바꿀 수 있다. 다른 슬롯끼리는
     "필요인원이 똑같은 셀끼리만" 맞바꿀 수 있다(그래야 그 두 슬롯의
     총 필요인원이 각각 그대로 유지됨). 셋업 셀은 필요인원이 항상
     0이므로 사실상 아무 0인원 셀과나 자유롭게 바뀌고, 그냥 idle로
     지우는 것도 가능하게 해준다(검증 없이 사람이 알아서 판단).
  2) 인원 "블록": 그날 총 고용인원을 사람이 원하는 크기로 나눠서 여러
     팀(블록)을 만들고, 각 팀을 슬롯별로 어느 라인에 배치할지 드래그로
     정한다. 한 슬롯의 필요인원을 여러 블록을 합쳐서 채울 수 있다(예:
     6명 블록 + 5명 블록 = 11명 슬롯). 배치가 끝나면 블록별로 하루 동안
     어느 라인을 돌았는지 타임라인으로 볼 수 있다.

사용법:
    python staffing_app.py --real-plan --read-specs excel --reference-date 2026-08-03
    (그 뒤 브라우저에서 http://127.0.0.1:5000 접속)

--real-plan 등 소스 관련 인자는 output/_data_source.py의 add_source_args와
동일하다(plot_day.py/labor_utilization.py와 같은 방식) - line_schedule.csv/
daily_workforce.csv는 그대로 읽고, 라인별 필요인원(workers)만 다시 계산
(엑셀 재로딩 필요, 시간이 좀 걸림 - 서버 기동 시 한 번만).
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys

import pandas as pd
from flask import Flask, jsonify, request, send_from_directory

# opt_modeling_proto/ 자체(현재 파일 위치)와 output/(스크립트 폴더, 패키지
# 아님)를 둘 다 import 경로에 추가해야 _data_source.py와 scheduling 패키지를
# 가져올 수 있다.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "output"))

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)

from _data_source import add_source_args, build_workers_lookup, resolve_source  # noqa: E402
from scheduling.models import SLOT_LABELS  # noqa: E402
from scheduling.report import _natural_line_key  # noqa: E402

app = Flask(__name__, static_folder=os.path.join(_HERE, "staffing_static"), static_url_path="")

# 모듈 전역 상태 - 이 도구는 개인 로컬 도구라 단일 사용자/단일 프로세스
# 가정하고 단순하게 전역 변수에 들고 있는다(멀티유저 동시접속 고려 안 함).
STATE: dict = {
    "schedule_df": None,       # line_schedule.csv 전체
    "workforce_df": None,      # daily_workforce.csv 전체
    "workers_lookup": None,    # {(line_id, order_id): workers}
    "state_dir": None,         # 편집 상태 저장용 디렉터리(line_schedule.csv와 같은 폴더 밑 staffing_state/)
    "all_lines": None,         # 자연정렬된 전체 물리 라인 목록(36개 등) - 정렬 기준 및
                               # "그날 활동 있는 라인만" 필터링의 기준 순서로 쓰임(day
                               # 자체의 표시 목록은 아님, _build_fresh_day_state 참고)
}


def _state_path(day: int) -> str:
    return os.path.join(STATE["state_dir"], f"day{day}.json")


def _build_fresh_day_state(day: int) -> dict:
    """line_schedule.csv/daily_workforce.csv/workers_lookup에서 이 날짜의
    초기 상태(사람이 아직 아무것도 안 건드린 상태)를 만든다. 블록/배치는
    빈 채로 시작한다."""
    df = STATE["schedule_df"]
    day_df = df[df["day"] == day]
    if day_df.empty:
        raise ValueError(f"day={day}에 해당하는 행이 없습니다.")

    by_line_slot: dict[tuple[str, str], dict] = {}
    active_lines: set[str] = set()
    for row in day_df.itertuples(index=False):
        by_line_slot[(row.line_id, row.slot)] = {
            "activity": row.activity,
            "order_id": "" if pd.isna(row.order_id) else str(row.order_id),
            "product_id": "" if pd.isna(row.product_id) else str(row.product_id),
        }
        if row.activity != "idle":
            active_lines.add(row.line_id)

    # 그날 하루 종일 대기(idle)만 한 라인은 화면에서 뺀다 - plot_day.py와
    # 같은 필터. 물리 라인이 36대라 다 보여주면 화면 세로 길이가 너무
    # 길어져서 한눈에 안 들어오고, 어차피 그날 아무 일도 안 한 라인으로
    # 옮길 일은 이 도구의 용도(그날 이미 배정된 작업의 재배치)상 거의 없다.
    lines_for_day = [lid for lid in STATE["all_lines"] if lid in active_lines]

    workers_lookup = STATE["workers_lookup"]
    grid: dict[str, list[dict]] = {}
    for line_id in lines_for_day:
        cells = []
        for slot in SLOT_LABELS:
            cell = by_line_slot.get((line_id, slot), {"activity": "idle", "order_id": "", "product_id": ""})
            workers = 0
            if cell["activity"] == "produce" and cell["order_id"]:
                workers = workers_lookup.get((line_id, cell["order_id"]))
                if workers is None:
                    workers = 0
            cells.append({
                "activity": cell["activity"],
                "order_id": cell["order_id"],
                "product_id": cell["product_id"],
                "workers": int(workers),
            })
        grid[line_id] = cells

    wf_row = STATE["workforce_df"]
    wf_row = wf_row[wf_row["day"] == day]
    daily_headcount = int(wf_row.iloc[0]["workforce"]) if not wf_row.empty else 0

    return {
        "day": day,
        "slot_labels": SLOT_LABELS,
        "daily_headcount": daily_headcount,
        "lines": lines_for_day,
        "grid": grid,
        # flows: {"t": [{"id":.., "srcLine":.., "dstLine":.., "count":..}, ...]}
        # 슬롯 t(0-idx)에서 t+1로 넘어갈 때 어느 라인(또는 "__idle__" =
        # 미배치/휴식)에서 어느 라인으로 몇 명이 이동하는지. 슬롯이
        # len(SLOT_LABELS)개면 전환은 그보다 하나 적게 존재한다.
        "flows": {str(t): [] for t in range(len(SLOT_LABELS) - 1)},
        # closed_lines: 사람이 "이건 하루 종일 뻔한 연속생산이라 슬롯
        # 전환 편집에서 안 봐도 된다"고 접어둔 라인 목록 - 그림에서도
        # 행이 숨겨진다(엑셀 행숨김과 동일한 개념, 체크 해제하면 복귀).
        "closed_lines": [],
        # tracking: {tree_id: {id, label, color, rootNodeId, nodes: {node_id: {
        #   count, lineId, slotIdx, edgeId, parentId, childIds}}}} - 특정
        # 인원 부분집합이 하루 동안 화살표를 따라 어떻게 갈라져 이동했는지
        # 사람이 직접 클릭해서 추적해둔 트리(들). 실제 배정과는 무관한
        # 순수 주석/추적용 데이터.
        "tracking": {},
    }


def _migrate_saved_state(data: dict) -> dict:
    """예전 저장 형식을 지금 스키마로 보정한다(둘 다 없으면 그대로,
    있으면 그 부분만 채워넣음 - 매번 전부 다시 만들 필요 없음).

    1) 블록 방식(고정 크기 "블록"을 슬롯별로 라인에 드래그해 배치하던
       예전 UI) -> flows(슬롯 전환별 이동 배정)로 변환. 블록 하나가
       슬롯 t에 라인 X, 슬롯 t+1에 라인 Y에 있었다면, "X에서 Y로 그
       블록 인원만큼 이동"이라는 흐름 한 개로 그대로 옮겨진다(X==Y면
       "그 자리에 계속 있었다"는 self-loop 흐름, None이면 미배치/휴식).
    2) closed_lines(접어둔 라인 체크박스) 필드가 없으면(그 기능이
       생기기 전에 저장된 파일) 빈 목록으로 채워넣음.
    3) tracking(인원 추적 트리) 필드가 없으면 빈 dict로 채워넣음.
    """
    if "flows" not in data:
        blocks = data.get("blocks", [])
        assignments = data.get("assignments", {})
        slot_labels = data.get("slot_labels", SLOT_LABELS)
        n = len(slot_labels)
        flows: dict[str, list[dict]] = {str(t): [] for t in range(n - 1)}
        for b in blocks:
            arr = assignments.get(b["id"], [None] * n)
            for t in range(n - 1):
                src = arr[t] if t < len(arr) else None
                dst = arr[t + 1] if t + 1 < len(arr) else None
                flows[str(t)].append({
                    "id": f"migrated_{b['id']}_{t}",
                    "srcLine": src or "__idle__",
                    "dstLine": dst or "__idle__",
                    "count": b["size"],
                })
        data["flows"] = flows
        data.pop("blocks", None)
        data.pop("assignments", None)

    if "closed_lines" not in data:
        data["closed_lines"] = []

    if "tracking" not in data:
        data["tracking"] = {}

    return data


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/days")
def api_days():
    days = sorted(int(d) for d in STATE["schedule_df"]["day"].unique())
    saved = set()
    if os.path.isdir(STATE["state_dir"]):
        for fn in os.listdir(STATE["state_dir"]):
            if fn.startswith("day") and fn.endswith(".json"):
                try:
                    saved.add(int(fn[3:-5]))
                except ValueError:
                    pass
    return jsonify({"days": days, "saved": sorted(saved)})


@app.route("/api/day/<int:day>")
def api_get_day(day: int):
    path = _state_path(day)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data = _migrate_saved_state(data)
        return jsonify(data)
    return jsonify(_build_fresh_day_state(day))


@app.route("/api/day/<int:day>/state", methods=["POST"])
def api_save_day(day: int):
    data = request.get_json(force=True)
    os.makedirs(STATE["state_dir"], exist_ok=True)
    with open(_state_path(day), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    return jsonify({"ok": True})


@app.route("/api/day/<int:day>/reset", methods=["POST"])
def api_reset_day(day: int):
    path = _state_path(day)
    if os.path.exists(path):
        os.remove(path)
    return jsonify(_build_fresh_day_state(day))


def main():
    parser = argparse.ArgumentParser(description="30일 계획 결과를 놓고 하루치 인원 배치를 수작업으로 조정하는 로컬 웹 도구")
    parser.add_argument("--dir", default=None, help="line_schedule.csv/daily_workforce.csv가 있는 디렉터리")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--no-browser", action="store_true", help="자동으로 브라우저를 열지 않음")
    add_source_args(parser)
    args = parser.parse_args()

    print("[정보] 라인/주문 데이터 로딩 중(필요인원 계산용, 시간이 좀 걸릴 수 있습니다)...")
    # resolve_source()의 --real-plan 기본 디렉터리는 "script_dir/real_plan"으로
    # 계산되는데, plot_day.py 등은 output/ 안에 있어서 output/real_plan이
    # 맞지만 이 파일은 opt_modeling_proto/ 바로 밑에 있으므로 output/을
    # 직접 붙여줘야 같은 output/real_plan 경로가 나온다.
    lines, orders, default_dir = resolve_source(args, os.path.join(_HERE, "output"))
    args.dir = args.dir or default_dir
    workers_lookup = build_workers_lookup(lines, orders)

    schedule_path = os.path.join(args.dir, "line_schedule.csv")
    workforce_path = os.path.join(args.dir, "daily_workforce.csv")
    schedule_df = pd.read_csv(schedule_path)
    workforce_df = pd.read_csv(workforce_path)

    all_lines = sorted(schedule_df["line_id"].unique().tolist(), key=_natural_line_key)

    STATE["schedule_df"] = schedule_df
    STATE["workforce_df"] = workforce_df
    STATE["workers_lookup"] = workers_lookup
    STATE["all_lines"] = all_lines
    STATE["state_dir"] = os.path.join(args.dir, "staffing_state")

    print(f"[정보] {schedule_path} 로드 완료 (라인 {len(all_lines)}개, {schedule_df['day'].nunique()}일치)")
    print(f"[정보] 편집 상태 저장 위치: {STATE['state_dir']}")
    print(f"[정보] http://127.0.0.1:{args.port} 에서 접속하세요")

    if not args.no_browser:
        import threading
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}")).start()

    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
