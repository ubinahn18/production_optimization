"use strict";

const IDLE_KEY = "__idle__";

let state = null;       // 현재 날짜의 전체 상태(서버와 동기화)
let currentDay = null;
let allDays = [];

let dragCellSource = null;   // {lineId, slotIdx} - 그리드 셀을 드래그 중일 때
let saveTimer = null;

let currentTransition = 0;   // 0..8: 슬롯 currentTransition -> currentTransition+1 전환 편집 중
let selectedSourceKey = null; // 전환 편집기에서 선택된 출발 항목의 key(라인id 또는 IDLE_KEY)
let autoConnectSameLine = false; // "연속 작업 연결" 체크박스 - 켜져 있는 동안만 적용되는 화면 모드, 서버에 저장 안 함
let arrowDisplayReduction = {}; // 우클릭으로 수동으로 줄인 화면 표시량: {edge.id: 뺄 숫자} - 실제 edge.count는 안 건드림, 서버에 저장 안 함

// 인원 추적(트리) 관련 - 트리 데이터 자체(state.tracking)는 서버에
// 저장되지만, "지금 새 추적을 시작하려고 대기 중인지"/"지금 어느
// 노드가 활성 상태인지"는 이 페이지를 보는 동안만 의미 있는 화면
// 상태라 서버에 안 보내고 이 변수들로만 들고 있는다.
let pendingNewTrack = false;
let activeTrackInfo = null; // {treeId, nodeId} 또는 null
const TRACK_COLORS = ["#2980b9", "#27ae60", "#8e44ad", "#c0392b", "#d35400", "#16a085", "#2c3e50", "#f39c12"];

function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// ---------------------------------------------------------------------
// 데이터 로딩
// ---------------------------------------------------------------------
async function loadDays() {
  const res = await fetch("/api/days");
  const data = await res.json();
  allDays = data.days;
  const sel = document.getElementById("daySelect");
  sel.innerHTML = "";
  for (const d of allDays) {
    const opt = document.createElement("option");
    opt.value = d;
    opt.textContent = `${d}일차` + (data.saved.includes(d) ? " ●" : "");
    sel.appendChild(opt);
  }
  if (allDays.length) {
    await loadDay(allDays[0]);
  }
}

async function loadDay(day) {
  const res = await fetch(`/api/day/${day}`);
  state = await res.json();
  if (!state.closed_lines) state.closed_lines = [];
  if (!state.tracking) state.tracking = {};
  currentDay = day;
  currentTransition = 0;
  selectedSourceKey = null;
  arrowDisplayReduction = {};
  pendingNewTrack = false;
  activeTrackInfo = null;
  autoConnectSameLine = false;
  document.getElementById("autoConnectSameLine").checked = false;
  document.getElementById("daySelect").value = String(day);
  setSaveStatus("idle");
  renderAll();
}

function renderAll() {
  document.getElementById("headcountInfo").textContent =
    `${state.day}일차 - 그날 총 고용인원 ${state.daily_headcount}명`;
  renderClosedLinesStrip();
  renderGrid();
  renderTransitionEditor();
  renderTrackingPanel();
}

// ---------------------------------------------------------------------
// 저장
// ---------------------------------------------------------------------
function setSaveStatus(kind) {
  const el = document.getElementById("saveStatus");
  el.className = "save-status " + kind;
  el.textContent = { idle: "-", saving: "저장 중...", saved: "저장됨", error: "저장 실패" }[kind] || "-";
}

function scheduleSave() {
  setSaveStatus("saving");
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = setTimeout(doSave, 350);
}

async function doSave() {
  try {
    const res = await fetch(`/api/day/${currentDay}/state`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state),
    });
    if (!res.ok) throw new Error("save failed");
    setSaveStatus("saved");
  } catch (e) {
    setSaveStatus("error");
  }
}

// ---------------------------------------------------------------------
// 그리드 렌더링 (라인 x 슬롯, 읽기 전용 + 셀 교환 드래그앤드롭)
// ---------------------------------------------------------------------
function cellWorkers(lineId, slotIdx) {
  return state.grid[lineId][slotIdx].workers;
}

function hashHue(text) {
  let h = 0;
  for (let i = 0; i < text.length; i++) h = (h * 31 + text.charCodeAt(i)) >>> 0;
  return h % 360;
}

const _productColorCache = {};
function productColor(productId) {
  // product_id 문자열을 해시해서 고정된 파스텔 배경색을 만든다(제품마다
  // 색 구분용). 채도/명도를 고정해서 옅은 파스텔로만 나오게 해 어두운
  // 글자색(기본 body 색)이 항상 잘 읽히게 한다.
  if (!(productId in _productColorCache)) {
    _productColorCache[productId] = `hsl(${hashHue(productId)}, 65%, 83%)`;
  }
  return _productColorCache[productId];
}

function isLineClosed(lineId) {
  return state.closed_lines.includes(lineId);
}

function toggleLineClosed(lineId) {
  const idx = state.closed_lines.indexOf(lineId);
  if (idx >= 0) {
    state.closed_lines.splice(idx, 1);
  } else {
    state.closed_lines.push(lineId);
  }
  renderAll();
  scheduleSave();
}

function renderClosedLinesStrip() {
  const wrap = document.getElementById("closedLinesStrip");
  if (!state.closed_lines.length) {
    wrap.style.display = "none";
    wrap.innerHTML = "";
    return;
  }
  wrap.style.display = "";
  wrap.innerHTML = "";
  const label = document.createElement("span");
  label.textContent = "접힌 라인: ";
  wrap.appendChild(label);
  for (const lineId of state.closed_lines) {
    const chip = document.createElement("span");
    chip.className = "closedLineChip";
    chip.textContent = lineId + " ✕";
    chip.title = "클릭하면 다시 펼침";
    chip.addEventListener("click", () => toggleLineClosed(lineId));
    wrap.appendChild(chip);
  }
}

function renderGrid() {
  const table = document.getElementById("grid");
  table.innerHTML = "";

  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  const cornerTh = document.createElement("th");
  cornerTh.className = "lineNameHeader";
  cornerTh.textContent = "라인 \\ 슬롯";
  headRow.appendChild(cornerTh);
  state.slot_labels.forEach((label) => {
    const th = document.createElement("th");
    th.textContent = label;
    headRow.appendChild(th);
  });
  thead.appendChild(headRow);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  for (const lineId of state.lines) {
    const closed = isLineClosed(lineId);
    const tr = document.createElement("tr");
    tr.className = closed ? "closedRow" : "";

    const nameTd = document.createElement("td");
    nameTd.className = "lineNameCell";

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.className = "closeLineCheckbox";
    checkbox.checked = closed;
    checkbox.title = "체크하면 이 라인은 하루 종일 뻔한 연속생산으로 접어두고(슬롯 전환 편집 대상에서도 빠짐) 표에서도 숨김";
    checkbox.addEventListener("change", () => toggleLineClosed(lineId));
    nameTd.appendChild(checkbox);
    nameTd.appendChild(document.createTextNode(" " + lineId));
    tr.appendChild(nameTd);

    state.slot_labels.forEach((_label, slotIdx) => {
      tr.appendChild(renderCell(lineId, slotIdx));
    });
    tbody.appendChild(tr);
  }

  // "미배치/휴식" 가상 행 - 그날 고용됐지만 그 슬롯엔 어느 라인에도
  // 필요하지 않은 인원. 흐름(전환 편집) 화살표의 출발/도착점으로만
  // 쓰이고, 일정 자체를 스왑할 대상은 아니므로 드래그는 안 된다.
  const idleTr = document.createElement("tr");
  idleTr.className = "idlePoolRow";
  const idleNameTd = document.createElement("td");
  idleNameTd.className = "lineNameCell";
  idleNameTd.textContent = "미배치 / 휴식";
  idleTr.appendChild(idleNameTd);
  state.slot_labels.forEach((_label, slotIdx) => {
    idleTr.appendChild(renderIdlePoolCell(slotIdx));
  });
  tbody.appendChild(idleTr);

  table.appendChild(tbody);
  drawFlowArrows();
  highlightReturnTargetOnGrid(computeReturnTarget());
}

function idleCountAtSlot(slotIdx) {
  let used = 0;
  for (const lineId of state.lines) {
    const c = state.grid[lineId][slotIdx];
    if (c.activity === "produce") used += c.workers;
  }
  return state.daily_headcount - used;
}

function renderIdlePoolCell(slotIdx) {
  const td = document.createElement("td");
  td.className = "cell idlePool";
  td.dataset.line = IDLE_KEY;
  td.dataset.slot = String(slotIdx);
  const count = idleCountAtSlot(slotIdx);
  if (count < 0) {
    td.classList.add("negative");
    td.title = `${state.slot_labels[slotIdx]}: 필요인원 합이 총 고용인원보다 ${-count}명 많음(잘못된 교환일 수 있음)`;
  } else {
    td.title = `${state.slot_labels[slotIdx]} 미배치/휴식 인원: ${count}명`;
  }

  const inner = document.createElement("div");
  inner.className = "cellInner";
  td.appendChild(inner);

  if (count !== 0) {
    const big = document.createElement("div");
    big.className = "bigNumber" + (count < 0 ? " negative" : "");
    big.textContent = String(count);
    inner.appendChild(big);
  }
  const label = document.createElement("div");
  label.className = "activityLabel";
  label.textContent = "미배치";
  inner.appendChild(label);

  return td;
}

function renderCell(lineId, slotIdx) {
  const cellData = state.grid[lineId][slotIdx];
  const td = document.createElement("td");
  td.className = `cell ${cellData.activity}`;
  td.draggable = true;
  td.dataset.line = lineId;
  td.dataset.slot = String(slotIdx);

  let titleParts = [`${lineId} / ${state.slot_labels[slotIdx]}`, `활동: ${cellData.activity}`];
  if (cellData.product_id) titleParts.push(`제품: ${cellData.product_id}`);
  if (cellData.order_id) titleParts.push(`주문: ${cellData.order_id}`);
  if (cellData.activity === "produce") titleParts.push(`필요인원: ${cellData.workers}명`);
  td.title = titleParts.join("\n");

  if (cellData.activity === "produce" && cellData.product_id) {
    td.style.background = productColor(cellData.product_id);
  }

  if (cellData.activity === "setup") {
    const clearBtn = document.createElement("button");
    clearBtn.className = "setupClearBtn";
    clearBtn.textContent = "×";
    clearBtn.title = "셋업 지우기(대기로)";
    clearBtn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      clearSetupCell(lineId, slotIdx);
    });
    td.appendChild(clearBtn);
  }

  // 표(table) 레이아웃이 깨지지 않도록, flex로 가운데 정렬할 내용물은
  // <td> 자체가 아니라 이 안쪽 wrapper div에만 넣는다(display:flex를
  // <td>에 직접 주면 브라우저가 표 레이아웃을 깨는 버그가 실제로 있었음).
  const inner = document.createElement("div");
  inner.className = "cellInner";
  td.appendChild(inner);

  if (cellData.activity === "produce" && cellData.workers > 0) {
    const big = document.createElement("div");
    big.className = "bigNumber";
    big.textContent = String(cellData.workers);
    inner.appendChild(big);
  }

  const label = document.createElement("div");
  label.className = "activityLabel";
  if (cellData.activity === "produce") {
    label.textContent = cellData.product_id || cellData.order_id || "생산";
  } else if (cellData.activity === "setup") {
    label.textContent = "셋업" + (cellData.product_id ? ` → ${cellData.product_id}` : "");
  } else {
    label.textContent = "";
  }
  inner.appendChild(label);

  // --- 드래그(셀 자체를 드래그해서 다른 셀과 교환) ---
  td.addEventListener("dragstart", (ev) => {
    dragCellSource = { lineId, slotIdx };
    td.classList.add("dragging");
    // 화살표 오버레이(#flowOverlay)가 표 위에 z-index로 겹쳐 있어서,
    // 화살표가 지나가는 자리 위로 셀을 드롭하면 그 화살표의 클릭
    // 영역(.flowArrowHit)이 dragover/drop을 가로채 "드래그는 되는데
    // 놓아도 안 붙는" 문제가 있었다 - 드래그하는 동안만 오버레이를
    // 통째로 통과시켜서(pointer-events 무시) 이 문제를 없앤다.
    document.getElementById("flowOverlay").classList.add("dragActive");
    ev.dataTransfer.effectAllowed = "move";
    ev.dataTransfer.setData("text/plain", `${lineId}|${slotIdx}`);
  });
  td.addEventListener("dragend", () => {
    td.classList.remove("dragging");
    document.getElementById("flowOverlay").classList.remove("dragActive");
    clearDragOverStyles();
  });
  td.addEventListener("dragover", (ev) => {
    if (!dragCellSource) return;
    ev.preventDefault();
    const ok = canSwapCells(dragCellSource, { lineId, slotIdx });
    td.classList.toggle("drag-over-ok", ok);
    td.classList.toggle("drag-over-bad", !ok);
  });
  td.addEventListener("dragleave", () => {
    td.classList.remove("drag-over-ok", "drag-over-bad");
  });
  td.addEventListener("drop", (ev) => {
    if (!dragCellSource) return;
    ev.preventDefault();
    td.classList.remove("drag-over-ok", "drag-over-bad");
    trySwapCells(dragCellSource, { lineId, slotIdx });
    dragCellSource = null;
  });

  return td;
}

function clearDragOverStyles() {
  document.querySelectorAll(".cell.drag-over-ok, .cell.drag-over-bad").forEach((el) => {
    el.classList.remove("drag-over-ok", "drag-over-bad");
  });
}

function canSwapCells(a, b) {
  // 예전엔 다른 슬롯끼리는 필요인원이 같아야만 바꿀 수 있게 막았는데,
  // 그냥 자유롭게 바꾸게 풀었다 - 미배치/휴식 인원은 매 렌더마다
  // "그날 총 고용인원 - 그 슬롯 실제 필요인원 합"으로 다시 계산되므로
  // (idleCountAtSlot 참고), 인원수가 안 맞는 교환을 하면 그 슬롯의
  // 미배치 인원이 자동으로 음수로 나타난다 - 그걸 보고 "잘못 옮겼다"는
  // 걸 사람이 스스로 판단하면 된다(여기서 미리 막지 않음).
  if (a.lineId === b.lineId && a.slotIdx === b.slotIdx) return false;
  return true;
}

function trySwapCells(a, b) {
  if (!canSwapCells(a, b)) return;
  const ga = state.grid[a.lineId][a.slotIdx];
  const gb = state.grid[b.lineId][b.slotIdx];
  state.grid[a.lineId][a.slotIdx] = gb;
  state.grid[b.lineId][b.slotIdx] = ga;
  renderGrid();
  scheduleSave();
}

function clearSetupCell(lineId, slotIdx) {
  state.grid[lineId][slotIdx] = { activity: "idle", order_id: "", product_id: "", workers: 0 };
  renderGrid();
  scheduleSave();
}

// ---------------------------------------------------------------------
// 슬롯 전환 편집기: 슬롯 t에 각 라인/미배치에 있던 인원이 슬롯 t+1의
// 어디로 가는지를 화살표(흐름)로 하나씩 정한다. 이름 붙은 "블록"을 미리
// 만들 필요 없이, 같은 라인에 있던 인원도 자유롭게 여러 목적지로
// 나뉠 수 있다.
// ---------------------------------------------------------------------
function itemsForSlot(slotIdx) {
  // 그 슬롯에 인원이 필요한 라인들 + (있으면) 미배치/휴식 인원. 접어둔
  // (체크박스로 닫은) 라인은 목록엔 안 보여주지만 - 어차피 뻔한
  // 연속생산이라 편집할 필요 없다는 뜻이라 - 그 라인에 실제로 인원이
  // 있다는 사실 자체는 그대로 카운트해서 미배치 인원 계산이 안 어긋나게
  // 한다(닫았다고 그 사람들이 갑자기 미배치가 되는 게 아니므로).
  const items = [];
  let used = 0;
  for (const lineId of state.lines) {
    const w = state.grid[lineId][slotIdx].workers;
    if (state.grid[lineId][slotIdx].activity === "produce" && w > 0) {
      used += w;
      if (isLineClosed(lineId)) continue;
      items.push({ key: lineId, label: lineId, total: w });
    }
  }
  const idle = state.daily_headcount - used;
  if (idle > 0) {
    items.push({ key: IDLE_KEY, label: "미배치 / 휴식", total: idle });
  }
  return items;
}

function edgesForTransition(t) {
  if (!state.flows[String(t)]) state.flows[String(t)] = [];
  return state.flows[String(t)];
}

function remainingFor(items, edges, side) {
  // side: "src" 또는 "dst". 각 item의 key별로 이미 배정된 합을 빼서
  // 남은 인원을 계산한다.
  const used = {};
  for (const e of edges) {
    const k = side === "src" ? e.srcLine : e.dstLine;
    used[k] = (used[k] || 0) + e.count;
  }
  const result = {};
  for (const it of items) result[it.key] = it.total - (used[it.key] || 0);
  return result;
}

// "연속 작업 연결"이 켜져 있을 때 호출됨 - 이번 전환(currentTransition)의
// 출발/도착 목록에서, 같은 라인(id)이면서 필요인원(total)이 완전히
// 같은 짝을 찾아 자동으로 이어준다(이미 그만큼 이어져 있으면 다시
// 안 건드림). 렌더링/저장은 호출자가 처리하도록, 여기선 state.flows만
// 고치고 뭔가 바뀌었으면 true를 반환한다.
function autoConnectMatchingLines() {
  const srcItems = itemsForSlot(currentTransition);
  const dstItems = itemsForSlot(currentTransition + 1);
  const edges = edgesForTransition(currentTransition);
  const srcRemaining = remainingFor(srcItems, edges, "src");
  const dstRemaining = remainingFor(dstItems, edges, "dst");
  const dstByKey = {};
  for (const it of dstItems) dstByKey[it.key] = it;

  let changed = false;
  for (const s of srcItems) {
    if (s.key === IDLE_KEY) continue; // "같은 라인"이 아니라 미배치/휴식이므로 대상 아님
    const d = dstByKey[s.key];
    if (!d || s.total !== d.total) continue;
    const amount = Math.min(srcRemaining[s.key] || 0, dstRemaining[d.key] || 0);
    if (amount <= 0) continue;

    const existing = edges.find((e) => e.srcLine === s.key && e.dstLine === s.key);
    if (existing) {
      existing.count += amount;
    } else {
      edges.push({
        id: `edge_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`,
        srcLine: s.key, dstLine: s.key, count: amount,
      });
    }
    srcRemaining[s.key] -= amount;
    dstRemaining[d.key] -= amount;
    changed = true;
  }
  return changed;
}

function transitionLabel(t) {
  return `${state.slot_labels[t]} → ${state.slot_labels[t + 1]}`;
}

function renderTransitionEditor() {
  const maxT = state.slot_labels.length - 2; // 0..(N-2)
  if (currentTransition < 0) currentTransition = 0;
  if (currentTransition > maxT) currentTransition = maxT;
  document.getElementById("transitionLabel").textContent = transitionLabel(currentTransition);

  // "연속 작업 연결"이 켜져 있으면, 렌더링 직전에 이번 전환에서 아직
  // 안 이어진 "같은 라인 + 같은 필요인원" 짝을 자동으로 다 이어준다.
  // (DOM/렌더 함수를 호출하지 않고 state.flows만 조용히 고쳐서, 바로
  // 아래 이어지는 정상적인 렌더링 한 번으로 결과가 반영되게 한다 -
  // 여기서 renderTransitionEditor를 다시 부르면 무한 재귀가 됨.)
  if (autoConnectSameLine && autoConnectMatchingLines()) {
    scheduleSave();
  }

  const srcItems = itemsForSlot(currentTransition);
  const dstItems = itemsForSlot(currentTransition + 1);
  const edges = edgesForTransition(currentTransition);
  const srcRemaining = remainingFor(srcItems, edges, "src");
  const dstRemaining = remainingFor(dstItems, edges, "dst");

  renderTransitionList("sourceList", srcItems, srcRemaining, "src");
  renderTransitionList("destList", dstItems, dstRemaining, "dst");
  renderEdgeList(edges, srcItems, dstItems);
  drawFlowArrows();
}

// ---------------------------------------------------------------------
// 흐름 화살표 그리기 - 현재 선택된 전환(currentTransition)에 배정된
// 이동을 표(그리드) 위에 실제 화살표 선 + 숫자로 겹쳐 그린다. 사이드
// 패널의 "이동 배정" 목록과 항상 같은 내용을 보여준다(그리는 소스가
// state.flows로 동일).
// ---------------------------------------------------------------------
const SVG_NS = "http://www.w3.org/2000/svg";

function findGridCell(key, slotIdx) {
  // 접어둔(닫은) 라인은 화면에서 숨겨져 있어(display:none) 위치를 잴 수
  // 없으므로, 그런 라인을 가리키는 화살표는 그리지 않는다(데이터는
  // 안 건드림 - 그냥 화면에 안 그릴 뿐).
  if (key !== IDLE_KEY && isLineClosed(key)) return null;
  const cells = document.querySelectorAll(`#grid td.cell[data-slot="${slotIdx}"]`);
  for (const c of cells) {
    if (c.dataset.line === key) return c;
  }
  return null;
}

function drawFlowArrows() {
  const svg = document.getElementById("flowOverlay");
  const container = document.getElementById("gridScroll");
  const table = document.getElementById("grid");
  if (!svg || !container || !table || !state) return;

  svg.querySelectorAll(".flowArrowLine, .flowArrowLabel, .flowArrowHit, .flowArrowTrackHalo, .flowArrowNextCandidate").forEach((el) => el.remove());

  const w = table.scrollWidth;
  const h = table.scrollHeight;
  svg.setAttribute("width", w);
  svg.setAttribute("height", h);

  const contRect = container.getBoundingClientRect();
  const maxT = state.slot_labels.length - 2;

  // 지금 활성 추적 노드가 있으면, 거기서 바로 이어갈 수 있는(=srcLine와
  // slotIdx가 정확히 그 노드의 현재 위치와 일치하는) 화살표들을 미리
  // 계산해둔다 - 그 화살표만 눈에 띄게 강조해서, 여러 화살표가 겹쳐
  // 있어도 "지금 클릭하면 추적이 이어질 화살표"를 바로 구분할 수 있게
  // 한다(tryExtendTracking의 판단 조건과 정확히 같은 조건).
  let activeNodeForHighlight = null;
  if (activeTrackInfo) {
    const tree = state.tracking[activeTrackInfo.treeId];
    activeNodeForHighlight = tree ? tree.nodes[activeTrackInfo.nodeId] : null;
  }

  // 지금 편집 중인 전환만이 아니라 하루 전체(모든 전환)의 화살표를 다
  // 같이 누적해서 보여준다 - 지금 보고 있는 전환만 진하게, 나머지는
  // 옅게 그려서 전체 흐름은 다 보이되 지금 작업 중인 것만 눈에 띄게 한다.
  for (let t = 0; t <= maxT; t++) {
    const edges = edgesForTransition(t);
    for (const e of edges) {
      const srcTd = findGridCell(e.srcLine, t);
      const dstTd = findGridCell(e.dstLine, t + 1);
      if (!srcTd || !dstTd) continue;
      const displayCount = arrowAvailableCount(e);
      // 표시 숫자가 0이 된 화살표(수동으로 다 줄였거나, 추적이 다
      // 가져간 경우)는 아예 안 그린다 - 안 그러면 같은 자리에 숫자만
      // 0인 화살표가 계속 겹쳐 남아서 진짜 화살표를 클릭하기 어려워진다.
      // 실제 배정(edge.count)은 전혀 안 바뀜 - 순수 표시만 안 그리는 것.
      if (displayCount <= 0) continue;
      const isNextCandidate = !!(
        activeNodeForHighlight
        && activeNodeForHighlight.slotIdx === t
        && activeNodeForHighlight.lineId === e.srcLine
      );
      drawOneArrow(svg, contRect, container, srcTd, dstTd, e, t, displayCount, t === currentTransition, isNextCandidate);
    }
  }
}

function drawOneArrow(svg, contRect, container, srcTd, dstTd, edge, transitionIdx, count, isCurrent, isNextCandidate) {
  const edgeId = edge.id;
  const s = srcTd.getBoundingClientRect();
  const d = dstTd.getBoundingClientRect();
  // 셀 맞닿은 경계가 아니라 위쪽(천장)에서 출발/도착시켜서, 같은 슬롯
  // 경계에 딱 붙어 안 보이는 문제를 없앤다 - 항상 행 위로 둥글게 솟은
  // 곡선으로 그려서 셀 사이에 파묻히지 않고 뚜렷하게 보인다.
  const x1 = s.left - contRect.left + container.scrollLeft + s.width / 2;
  const y1 = s.top - contRect.top + container.scrollTop;
  const x2 = d.left - contRect.left + container.scrollLeft + d.width / 2;
  const y2 = d.top - contRect.top + container.scrollTop;

  const topY = Math.min(y1, y2);
  const rowGap = Math.abs(y1 - y2);
  const arcHeight = Math.max(26, rowGap * 0.25 + 18);
  const midX = (x1 + x2) / 2;
  const ctrlY = topY - arcHeight;

  const d3 = `M ${x1} ${y1} Q ${midX} ${ctrlY} ${x2} ${y2}`;

  // 이 화살표가 어느 추적 트리에 쓰였으면(부분적으로라도), 그 트리
  // 색으로 굵은 하이라이트 선을 실제 화살표 밑에 깔아서 눈에 띄게 한다.
  const trackColor = firstTrackColorForEdge(edgeId);
  if (trackColor) {
    const halo = document.createElementNS(SVG_NS, "path");
    halo.setAttribute("d", d3);
    halo.setAttribute("class", "flowArrowTrackHalo");
    halo.style.stroke = trackColor;
    svg.appendChild(halo);
  }

  // 지금 활성 추적 노드에서 바로 이어갈 수 있는 화살표는(=다음에 클릭할
  // 후보) 점선 하이라이트를 따로 얹어서, 겹쳐 있는 다른 화살표들 사이에서
  // "이걸 누르면 추적이 이어진다"를 바로 구분할 수 있게 한다.
  if (isNextCandidate) {
    const candidateHalo = document.createElementNS(SVG_NS, "path");
    candidateHalo.setAttribute("d", d3);
    candidateHalo.setAttribute("class", "flowArrowNextCandidate");
    svg.appendChild(candidateHalo);
  }

  // 실제로 보이는 얇은 곡선 밑에, 클릭하기 쉽도록 두꺼운(투명) "히트
  // 영역" 패스를 하나 더 깔아둔다(얇은 2~3px 선은 마우스로 정확히
  // 맞추기 어려움).
  const hit = document.createElementNS(SVG_NS, "path");
  hit.setAttribute("d", d3);
  hit.setAttribute("class", "flowArrowHit");
  hit.addEventListener("click", () => {
    // 클릭으로 화살표를 숨기는 기능은 인원 추적 중에 실수로 화살표가
    // 없어지는 문제가 있어서 없앴다 - 이제 클릭은 추적 확장 시도만
    // 한다(추적 중이 아니면 아무 동작도 안 함). 화면에서 숨기고 싶으면
    // 우클릭으로 표시 숫자를 줄이는 기능만 남아있다.
    tryExtendTracking(edge, transitionIdx);
  });
  hit.addEventListener("contextmenu", (ev) => {
    ev.preventDefault();
    promptReduceArrow(edgeId);
  });
  svg.appendChild(hit);

  // 다음 추적 후보 화살표는 지금 보고 있는 전환(탭)이 아니어도 옅게
  // 죽이지 않는다 - 활성 노드 이후 전환이 사이드바에서 지금 안 보고
  // 있는 탭일 수도 있는데, 그래도 "누르면 이어진다"는 눈에 띄어야 함.
  const emphasize = isCurrent || isNextCandidate;

  const path = document.createElementNS(SVG_NS, "path");
  path.setAttribute("d", d3);
  path.setAttribute("class", "flowArrowLine" + (emphasize ? "" : " dim"));
  svg.appendChild(path);

  // 곡선의 실제 중간 지점(2차 베지어, t=0.5): 0.25*P0 + 0.5*P1 + 0.25*P2
  const labelX = 0.25 * x1 + 0.5 * midX + 0.25 * x2;
  const labelY = 0.25 * y1 + 0.5 * ctrlY + 0.25 * y2;
  const label = document.createElementNS(SVG_NS, "text");
  label.setAttribute("x", labelX);
  label.setAttribute("y", labelY);
  label.setAttribute("text-anchor", "middle");
  label.setAttribute("class", "flowArrowLabel" + (emphasize ? "" : " dim"));
  label.textContent = String(count);
  svg.appendChild(label);
}

function findEdgeById(edgeId) {
  for (const key of Object.keys(state.flows)) {
    const found = state.flows[key].find((e) => e.id === edgeId);
    if (found) return found;
  }
  return null;
}

// ---------------------------------------------------------------------
// 인원 추적(트리) - 특정 인원 부분집합이 화살표를 따라 하루 동안 어떻게
// 갈라져 이동했는지 사람이 직접 클릭해서 기록해두는 기능. 실제 배정
// (state.flows)은 전혀 안 건드리고, 트리 데이터(state.tracking)만
// 저장한다 - "이 화살표 중 몇 명이 이 트리에 이미 배정됐는지"는
// 트리를 훑어서 그때그때 계산한다(따로 저장 안 함, 중복/불일치 방지).
// ---------------------------------------------------------------------
function trackingClaimedOnEdge(edgeId) {
  let sum = 0;
  for (const treeId of Object.keys(state.tracking)) {
    const nodes = state.tracking[treeId].nodes;
    for (const nodeId of Object.keys(nodes)) {
      if (nodes[nodeId].edgeId === edgeId) sum += nodes[nodeId].count;
    }
  }
  return sum;
}

function arrowAvailableCount(edge) {
  const manual = arrowDisplayReduction[edge.id] || 0;
  const claimed = trackingClaimedOnEdge(edge.id);
  return Math.max(0, edge.count - manual - claimed);
}

function firstTrackColorForEdge(edgeId) {
  for (const treeId of Object.keys(state.tracking)) {
    const tree = state.tracking[treeId];
    for (const nodeId of Object.keys(tree.nodes)) {
      if (tree.nodes[nodeId].edgeId === edgeId) return tree.color;
    }
  }
  return null;
}

function genTrackId(prefix) {
  return `${prefix}_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
}

// 노드가 이미 뻗어낸 자식 가지들의 인원을 빼고, 아직 어느 가지로도
// 안 보낸(=더 뻗어나갈 수 있는) 인원수.
function remainingForNode(tree, node) {
  let used = 0;
  for (const childId of node.childIds) {
    const child = tree.nodes[childId];
    if (child) used += child.count;
  }
  return node.count - used;
}

// 주어진 노드의 "부모부터" 위로 거슬러 올라가며, 아직 다 안 갈라진
// (remainingForNode>0) 가장 가까운 조상을 찾는다 - "이 가지를 끝까지
// 다 따라왔는데, 트리를 마저 완성하려면 어디로 돌아가야 하나"를 알려줄
// 때 쓴다. 시작 노드 자신은 검사 대상에서 뺀다 - 아직 한 번도 안
// 갈라진 리프는 childIds가 비어 있어서 remainingForNode가 항상 자기
// count 전체와 같게 나오는데(자명하게 "다 안 갈라짐"), 이건 "아직
// 못 끝낸 가지"가 아니라 그냥 "지금 여기 있다"는 뜻이라 돌아갈
// 곳으로 치면 안 된다 - 실수로 자기 자신을 매번 답으로 골라버리는
// 버그가 있었음(2026-07-21 실제로 확인됨).
function findNearestIncompleteAncestor(tree, nodeId) {
  const self = tree.nodes[nodeId];
  let cur = self && self.parentId ? tree.nodes[self.parentId] : null;
  while (cur) {
    if (remainingForNode(tree, cur) > 0) return cur;
    cur = cur.parentId ? tree.nodes[cur.parentId] : null;
  }
  return null;
}

// 활성 추적 노드를 기준으로 "돌아가야 할 지점"을 계산한다. 그리드
// 강조(renderGrid)와 사이드바 강조(renderTrackingPanel)가 서로 어느
// 쪽이 먼저 그려지든 항상 같은 기준을 쓰도록 여기 하나로 모아뒀다 -
// 활성 노드 자신이 그 지점이면(=지금 있는 자리가 이미 안 갈라진
// 상태) "돌아갈" 필요가 없으므로 null을 반환한다.
function computeReturnTarget() {
  if (!activeTrackInfo) return null;
  const tree = state.tracking[activeTrackInfo.treeId];
  if (!tree) return null;
  const candidate = findNearestIncompleteAncestor(tree, activeTrackInfo.nodeId);
  if (!candidate || candidate.id === activeTrackInfo.nodeId) return null;
  return { treeId: activeTrackInfo.treeId, nodeId: candidate.id };
}

// 계산된 "돌아갈 지점"을 그리드 셀에 시각적으로 표시한다(이전 강조는
// 먼저 지움 - renderGrid가 매번 표를 통째로 새로 그리므로, 지우는
// 대상은 사실 항상 새로 그려진 빈 상태지만 방어적으로 남겨둠).
function highlightReturnTargetOnGrid(returnTarget) {
  document.querySelectorAll("#grid td.cell.trackReturnTarget").forEach((td) => {
    td.classList.remove("trackReturnTarget");
  });
  if (!returnTarget) return;
  const tree = state.tracking[returnTarget.treeId];
  const node = tree && tree.nodes[returnTarget.nodeId];
  if (!node) return;
  const td = findGridCell(node.lineId, node.slotIdx);
  if (td) td.classList.add("trackReturnTarget");
}

function tryExtendTracking(edge, transitionIdx) {
  if (!pendingNewTrack && !activeTrackInfo) return false;

  const available = arrowAvailableCount(edge);
  if (available <= 0) {
    alert("이 화살표엔 더 배정할 수 있는 인원이 없습니다(이미 다른 추적이나 화면 표시 조정으로 다 소진됨).");
    return true; // 클릭을 추적 시도로 소비 - 평소 숨기기 동작으로 안 넘어가게
  }

  if (pendingNewTrack) {
    const treeId = genTrackId("track");
    const startNodeId = genTrackId("node");
    const firstNodeId = genTrackId("node");
    const color = TRACK_COLORS[Object.keys(state.tracking).length % TRACK_COLORS.length];

    // 화살표를 처음 골라서 추적을 시작하는 순간, 그 화살표의 "출발점"
    // (도착점보다 슬롯 하나 이전)도 트리의 실제 노드로 같이 만들어둔다.
    // 안 그러면 예를 들어 08-09→09-10 화살표로 추적을 시작했을 때
    // 08-09 슬롯 자체는 트리/엑셀 내보내기 어디에도 안 나오게 된다 -
    // 화살표를 고른 시점에 이미 그 출발 라인을 알고 있으니 굳이 뺄
    // 이유가 없다.
    const startNode = {
      id: startNodeId, count: available, lineId: edge.srcLine, slotIdx: transitionIdx,
      edgeId: null, parentId: null, childIds: [firstNodeId],
    };
    const firstNode = {
      id: firstNodeId, count: available, lineId: edge.dstLine, slotIdx: transitionIdx + 1,
      srcLine: edge.srcLine, edgeId: edge.id, parentId: startNodeId, childIds: [],
    };
    state.tracking[treeId] = {
      id: treeId, label: `그룹${Object.keys(state.tracking).length + 1}`, color,
      rootNodeId: startNodeId, nodes: { [startNodeId]: startNode, [firstNodeId]: firstNode },
    };
    pendingNewTrack = false;
    activeTrackInfo = { treeId, nodeId: firstNodeId };
    renderAll();
    scheduleSave();
    return true;
  }

  // 활성 노드에서 뻗어나가는 경우 - 그 노드의 현재 위치(라인,슬롯)에서
  // 시작하는 화살표여야만 확장으로 인정한다(엉뚱한 화살표를 눌러도
  // 추적이 튀지 않게).
  const { treeId, nodeId } = activeTrackInfo;
  const tree = state.tracking[treeId];
  const activeNode = tree && tree.nodes[nodeId];
  if (!tree || !activeNode) {
    activeTrackInfo = null;
    return false;
  }
  if (activeNode.slotIdx !== transitionIdx || activeNode.lineId !== edge.srcLine) {
    return false; // 활성 노드 위치와 안 맞음 - 추적 확장 대상 아님, 평소 클릭으로 처리
  }

  // 활성 노드가 이미 다른 가지로 일부를 내보냈다면, 그만큼은 빼고
  // 남은 만큼만 이 화살표로 더 보낼 수 있다(안 그러면 5명짜리 노드가
  // 이미 3명을 한 가지로 보낸 뒤에도 또 3명을 다른 가지로 보낼 수
  // 있게 되어 실제 인원수보다 더 많이 추적되는 버그가 생김).
  const remaining = remainingForNode(tree, activeNode);
  const takenAmount = Math.min(remaining, available);
  if (takenAmount <= 0) {
    alert(`활성 노드(${activeNode.count}명)는 이미 다른 가지로 다 배정되어 더 뻗어나갈 인원이 없습니다.`);
    return true;
  }
  const newNodeId = genTrackId("node");
  const newNode = {
    id: newNodeId, count: takenAmount, lineId: edge.dstLine, slotIdx: transitionIdx + 1,
    srcLine: edge.srcLine,
    edgeId: edge.id, parentId: nodeId, childIds: [],
  };
  tree.nodes[newNodeId] = newNode;
  activeNode.childIds.push(newNodeId);
  activeTrackInfo = { treeId, nodeId: newNodeId };
  renderAll();
  scheduleSave();
  return true;
}

function startNewTrack() {
  pendingNewTrack = true;
  activeTrackInfo = null;
  updateTrackingButtonUI();
  drawFlowArrows(); // 이전 활성 노드의 "다음 후보" 강조를 지운다
}

function updateTrackingButtonUI() {
  const btn = document.getElementById("newTrackBtn");
  if (!btn) return;
  btn.classList.toggle("pending", pendingNewTrack);
  btn.textContent = pendingNewTrack ? "화살표를 클릭하세요..." : "+ 새 추적 시작";
}

function setActiveTrackNode(treeId, nodeId) {
  activeTrackInfo = { treeId, nodeId };
  pendingNewTrack = false;
  updateTrackingButtonUI();
  renderTrackingPanel();
  drawFlowArrows(); // 활성 노드가 바뀌면 "다음 후보" 화살표 강조도 다시 계산해야 함
}

function deleteTrackTree(treeId) {
  if (!confirm("이 추적(전체 가지 포함)을 삭제할까요? 실제 배정은 안 바뀝니다.")) return;
  delete state.tracking[treeId];
  if (activeTrackInfo && activeTrackInfo.treeId === treeId) activeTrackInfo = null;
  renderAll();
  scheduleSave();
}

function renameTrackTree(treeId, newLabel) {
  const tree = state.tracking[treeId];
  if (!tree) return;
  tree.label = newLabel || tree.label;
  scheduleSave();
}

function renderTrackingPanel() {
  updateTrackingButtonUI();
  const wrap = document.getElementById("trackingList");
  if (!wrap) return;
  wrap.innerHTML = "";

  // 활성 노드 기준 "돌아갈 지점" - renderGrid에서도 독립적으로 같은
  // 계산을 하지만(어느 쪽이 먼저 그려지든 항상 최신값이 반영되게),
  // 여기서도 한 번 더 반영해서 그리드 강조와 항상 같은 상태를 보여준다.
  const returnTarget = computeReturnTarget();
  highlightReturnTargetOnGrid(returnTarget);

  for (const treeId of Object.keys(state.tracking)) {
    const tree = state.tracking[treeId];
    const box = document.createElement("div");
    box.className = "trackTree";

    const header = document.createElement("div");
    header.className = "trackTreeHeader";

    const swatch = document.createElement("span");
    swatch.className = "trackColorSwatch";
    swatch.style.background = tree.color;
    header.appendChild(swatch);

    const labelInput = document.createElement("input");
    labelInput.className = "trackTreeLabel";
    labelInput.value = tree.label;
    labelInput.addEventListener("change", () => renameTrackTree(treeId, labelInput.value.trim()));
    header.appendChild(labelInput);

    const del = document.createElement("span");
    del.className = "trackTreeDelete";
    del.textContent = "삭제";
    del.addEventListener("click", () => deleteTrackTree(treeId));
    header.appendChild(del);

    box.appendChild(header);

    const rootUl = document.createElement("ul");
    rootUl.className = "trackNodeList";
    rootUl.appendChild(renderTrackNodeItem(tree, tree.rootNodeId, returnTarget));
    box.appendChild(rootUl);

    wrap.appendChild(box);
  }
}

function renderTrackNodeItem(tree, nodeId, returnTarget) {
  const node = tree.nodes[nodeId];
  const li = document.createElement("li");
  const isActive = activeTrackInfo && activeTrackInfo.treeId === tree.id && activeTrackInfo.nodeId === nodeId;
  const isReturnTarget = !!(returnTarget && returnTarget.treeId === tree.id && returnTarget.nodeId === nodeId);
  li.className = "trackNode" + (isActive ? " active" : "") + (isReturnTarget ? " returnTarget" : "");
  const slotLabel = state.slot_labels[node.slotIdx] || `슬롯${node.slotIdx}`;
  const lineLabel = node.lineId === IDLE_KEY ? "미배치" : node.lineId;
  li.textContent = `${node.count}명 @ ${lineLabel} (${slotLabel})` + (isReturnTarget ? " ↩ 여기로 돌아가기" : "");
  li.title = "클릭하면 여기서부터 새 가지를 뻗을 수 있음(활성 노드로 지정)";
  li.addEventListener("click", (ev) => {
    ev.stopPropagation();
    setActiveTrackNode(tree.id, nodeId);
  });

  if (node.childIds.length) {
    const childUl = document.createElement("ul");
    childUl.className = "trackNodeList";
    for (const childId of node.childIds) {
      childUl.appendChild(renderTrackNodeItem(tree, childId, returnTarget));
    }
    li.appendChild(childUl);
  }
  return li;
}

function promptReduceArrow(edgeId) {
  const edge = findEdgeById(edgeId);
  if (!edge) return;
  const current = arrowDisplayReduction[edgeId] || 0;
  const input = prompt(
    `이 화살표(실제 배정 ${edge.count}명)에서 화면 표시상 몇 명을 뺄까요?\n` +
      `(실제 배정은 안 바뀝니다 - 화면에만 반영됩니다. 0을 입력하면 원래 숫자로 되돌아갑니다.)`,
    String(current)
  );
  if (input === null) return; // 취소
  const n = parseInt(input, 10);
  if (isNaN(n) || n <= 0) {
    delete arrowDisplayReduction[edgeId];
  } else {
    arrowDisplayReduction[edgeId] = n;
  }
  drawFlowArrows();
}

let _arrowRedrawQueued = false;
function scheduleRedrawArrows() {
  if (_arrowRedrawQueued) return;
  _arrowRedrawQueued = true;
  requestAnimationFrame(() => {
    _arrowRedrawQueued = false;
    drawFlowArrows();
  });
}
window.addEventListener("resize", scheduleRedrawArrows);
document.getElementById("gridScroll").addEventListener("scroll", scheduleRedrawArrows, { passive: true });
document.getElementById("autoConnectSameLine").addEventListener("change", (ev) => {
  autoConnectSameLine = ev.target.checked;
  renderTransitionEditor(); // 켜지는 순간 지금 보고 있는 전환에 바로 적용
});
document.getElementById("newTrackBtn").addEventListener("click", () => {
  if (pendingNewTrack) {
    pendingNewTrack = false;
    updateTrackingButtonUI();
  } else {
    startNewTrack();
  }
});
document.getElementById("exportTrackingBtn").addEventListener("click", async () => {
  const btn = document.getElementById("exportTrackingBtn");
  if (!Object.keys(state.tracking).length) {
    alert("아직 추적 중인 그룹이 없습니다. 먼저 인원 추적을 시작하세요.");
    return;
  }
  const orig = btn.textContent;
  btn.textContent = "내보내는 중...";
  btn.disabled = true;
  try {
    const res = await fetch(`/api/day/${currentDay}/export_tracking`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state),
    });
    if (!res.ok) throw new Error(await res.text());
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `day${currentDay}_인원추적_시간표.xlsx`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (err) {
    alert("엑셀 내보내기에 실패했습니다: " + err.message);
  } finally {
    btn.textContent = orig;
    btn.disabled = false;
  }
});

function itemLabelText(items, key) {
  const it = items.find((x) => x.key === key);
  return it ? it.label : key;
}

function renderTransitionList(elId, items, remainingMap, side) {
  const ul = document.getElementById(elId);
  ul.innerHTML = "";
  for (const it of items) {
    const remaining = remainingMap[it.key];
    const li = document.createElement("li");
    li.classList.toggle("filled", remaining <= 0);
    if (side === "src" && it.key === selectedSourceKey) li.classList.add("selected");

    const name = document.createElement("span");
    name.className = "itemName";
    name.textContent = it.label;
    li.appendChild(name);

    const count = document.createElement("span");
    count.className = "itemCount";
    count.textContent = `${Math.max(remaining, 0)}/${it.total}`;
    li.appendChild(count);

    li.title = `${it.label}: ${it.total}명 중 ${Math.max(remaining, 0)}명 미배정`;

    if (side === "src") {
      li.addEventListener("click", () => {
        if (remaining <= 0) return;
        selectedSourceKey = (selectedSourceKey === it.key) ? null : it.key;
        renderTransitionEditor();
      });
    } else {
      li.addEventListener("click", () => {
        if (remaining <= 0 || !selectedSourceKey) return;
        addOrGrowEdge(selectedSourceKey, it.key);
      });
    }
    ul.appendChild(li);
  }
}

function addOrGrowEdge(srcKey, dstKey) {
  const edges = edgesForTransition(currentTransition);
  const srcItems = itemsForSlot(currentTransition);
  const dstItems = itemsForSlot(currentTransition + 1);
  const srcRemaining = remainingFor(srcItems, edges, "src")[srcKey] || 0;
  const dstRemaining = remainingFor(dstItems, edges, "dst")[dstKey] || 0;
  const moveCount = Math.min(srcRemaining, dstRemaining);
  if (moveCount <= 0) return;

  const existing = edges.find((e) => e.srcLine === srcKey && e.dstLine === dstKey);
  if (existing) {
    existing.count += moveCount;
  } else {
    edges.push({ id: `edge_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`, srcLine: srcKey, dstLine: dstKey, count: moveCount });
  }

  // 출발 쪽이 다 채워졌으면 선택 해제(더 나눌 여지가 없으므로), 아직
  // 남았으면 선택 유지해서 바로 다른 목적지를 이어서 클릭할 수 있게 한다.
  const newSrcRemaining = srcRemaining - moveCount;
  if (newSrcRemaining <= 0) selectedSourceKey = null;

  renderTransitionEditor();
  scheduleSave();
}

function renderEdgeList(edges, srcItems, dstItems) {
  const ul = document.getElementById("edgeList");
  ul.innerHTML = "";
  if (!edges.length) {
    const li = document.createElement("li");
    li.className = "emptyMsg";
    li.textContent = "아직 배정된 이동이 없습니다.";
    ul.appendChild(li);
    return;
  }
  for (const e of edges) {
    const li = document.createElement("li");

    const label = document.createElement("span");
    label.className = "edgeLabel";
    label.textContent = `${itemLabelText(srcItems, e.srcLine)} → ${itemLabelText(dstItems, e.dstLine)}`;
    li.appendChild(label);

    const input = document.createElement("input");
    input.type = "number";
    input.min = "1";
    input.value = String(e.count);
    input.addEventListener("change", () => {
      updateEdgeCount(e.id, parseInt(input.value, 10));
    });
    li.appendChild(input);
    li.appendChild(document.createTextNode("명"));

    const del = document.createElement("span");
    del.className = "edgeDelete";
    del.textContent = "×";
    del.title = "이 이동 삭제";
    del.addEventListener("click", () => deleteEdge(e.id));
    li.appendChild(del);

    // 우클릭으로 화면 표시 숫자를 줄여둔 화살표는(특히 0까지 줄이면
    // 그림에서 아예 안 그려지므로) 그림에서 다시 클릭할 방법이 없다 -
    // 여기 목록에서 초기화할 수 있게 해준다(실제 배정은 항상 그대로라
    // 이 목록의 count는 안 바뀌어 있었음 - 화면 표시만 원래대로 되돌림).
    if (arrowDisplayReduction[e.id]) {
      const resetBtn = document.createElement("span");
      resetBtn.className = "edgeResetDisplay";
      resetBtn.textContent = `표시 -${arrowDisplayReduction[e.id]} 초기화`;
      resetBtn.title = "우클릭으로 줄여둔 화면 표시 숫자를 원래대로(실제 배정 그대로) 되돌립니다.";
      resetBtn.addEventListener("click", () => {
        delete arrowDisplayReduction[e.id];
        renderAll();
      });
      li.appendChild(resetBtn);
    }

    ul.appendChild(li);
  }
}

function updateEdgeCount(edgeId, newCount) {
  const edges = edgesForTransition(currentTransition);
  const edge = edges.find((e) => e.id === edgeId);
  if (!edge) return;
  if (!newCount || newCount <= 0) {
    deleteEdge(edgeId);
    return;
  }
  // 이 edge를 뺀 상태에서 양쪽에 남는 여유(= 이 edge가 가질 수 있는 최대치)로 clamp.
  const srcItems = itemsForSlot(currentTransition);
  const dstItems = itemsForSlot(currentTransition + 1);
  const others = edges.filter((e) => e.id !== edgeId);
  const srcRemaining = remainingFor(srcItems, others, "src")[edge.srcLine] || 0;
  const dstRemaining = remainingFor(dstItems, others, "dst")[edge.dstLine] || 0;
  edge.count = Math.max(1, Math.min(newCount, srcRemaining, dstRemaining));
  renderTransitionEditor();
  scheduleSave();
}

function deleteEdge(edgeId) {
  const edges = edgesForTransition(currentTransition);
  const idx = edges.findIndex((e) => e.id === edgeId);
  if (idx >= 0) edges.splice(idx, 1);
  renderTransitionEditor();
  scheduleSave();
}

// ---------------------------------------------------------------------
// 상단 컨트롤
// ---------------------------------------------------------------------
document.getElementById("daySelect").addEventListener("change", (ev) => {
  loadDay(parseInt(ev.target.value, 10));
});
document.getElementById("prevDayBtn").addEventListener("click", () => {
  const idx = allDays.indexOf(currentDay);
  if (idx > 0) loadDay(allDays[idx - 1]);
});
document.getElementById("nextDayBtn").addEventListener("click", () => {
  const idx = allDays.indexOf(currentDay);
  if (idx >= 0 && idx < allDays.length - 1) loadDay(allDays[idx + 1]);
});
document.getElementById("resetBtn").addEventListener("click", async () => {
  if (!confirm(`${currentDay}일차의 수동 편집을 전부 초기화할까요? (되돌릴 수 없습니다)`)) return;
  const res = await fetch(`/api/day/${currentDay}/reset`, { method: "POST" });
  state = await res.json();
  currentTransition = 0;
  selectedSourceKey = null;
  setSaveStatus("idle");
  renderAll();
});
document.getElementById("prevTransBtn").addEventListener("click", () => {
  if (currentTransition > 0) {
    currentTransition -= 1;
    selectedSourceKey = null;
    renderTransitionEditor();
  }
});
document.getElementById("nextTransBtn").addEventListener("click", () => {
  if (currentTransition < state.slot_labels.length - 2) {
    currentTransition += 1;
    selectedSourceKey = null;
    renderTransitionEditor();
  }
});

loadDays();
