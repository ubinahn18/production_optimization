# -*- coding: utf-8 -*-
"""
gui_app.py

opt_modeling_proto 파이프라인(plan_from_orders.py + output/ 아래 후처리
스크립트들)을 감싸는 아주 단순한 Tkinter 데스크톱 GUI. 계산 로직은 새로
만들지 않고 기존 CLI 스크립트를 서브프로세스로 그대로 호출한 뒤, 그
결과(콘솔 출력 / PNG / CSV)를 화면에 보여주기만 한다.

실행:
    python gui_app.py
"""

from __future__ import annotations

import csv
import os
import subprocess
import sys
import threading
import tkinter as tk
from datetime import date
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PLAN_SCRIPT = os.path.join(BASE_DIR, "plan_from_orders.py")
PLOT_DAY_SCRIPT = os.path.join(BASE_DIR, "output", "plot_day.py")
LABOR_UTIL_SCRIPT = os.path.join(BASE_DIR, "output", "labor_utilization.py")
EFFICIENCY_SCRIPT = os.path.join(BASE_DIR, "output", "real_plan", "production_efficiency.py")
OUTPUT_DIR = os.path.join(BASE_DIR, "output", "real_plan")
DEFAULT_EXCEL_PATH = r"C:\Users\USER\Desktop\유빈_생산계획\수주진행현황(simulation_DATA_1)1.xlsx"


def run_command_async(args: list[str], on_line, on_done) -> None:
    """[sys.executable, *args]를 백그라운드 스레드에서 실행하면서 stdout을
    한 줄씩 on_line(str)으로 넘기고, 끝나면 on_done(returncode)을 부른다.
    on_line/on_done은 Tk 메인스레드가 아니라 워커 스레드에서 호출되므로,
    호출하는 쪽에서 위젯을 건드릴 때는 반드시 root.after(0, ...)로 감싸야
    한다."""

    def worker():
        try:
            proc = subprocess.Popen(
                [sys.executable, *args],
                cwd=BASE_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            for line in proc.stdout:
                on_line(line.rstrip("\n"))
            proc.wait()
            on_done(proc.returncode)
        except Exception as exc:  # 스크립트 자체가 없거나 실행 불가한 경우 등
            on_line(f"[오류] {exc}")
            on_done(-1)

    threading.Thread(target=worker, daemon=True).start()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("생산 스케줄링 도구")
        self.geometry("1200x800")

        self.excel_path_var = tk.StringVar(value=DEFAULT_EXCEL_PATH)
        self.reference_date_var = tk.StringVar(value=date.today().isoformat())

        self._image_refs: dict[str, ImageTk.PhotoImage] = {}  # GC 방지용 보관

        self._build_common_frame()

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self.run_tab = ttk.Frame(notebook)
        self.overview_tab = ttk.Frame(notebook)
        self.workforce_tab = ttk.Frame(notebook)
        self.day_tab = ttk.Frame(notebook)
        self.efficiency_tab = ttk.Frame(notebook)
        self.labor_tab = ttk.Frame(notebook)

        notebook.add(self.run_tab, text="최적화 실행")
        notebook.add(self.overview_tab, text="생산 스케줄 (전체)")
        notebook.add(self.workforce_tab, text="일별 Workforce")
        notebook.add(self.day_tab, text="일별 스케줄 그림")
        notebook.add(self.efficiency_tab, text="생산 효율 지표")
        notebook.add(self.labor_tab, text="실작업 투입 효율")

        self._build_run_tab()
        self._build_overview_tab()
        self._build_workforce_tab()
        self._build_day_tab()
        self._build_efficiency_tab()
        self._build_labor_tab()

        self.status_var = tk.StringVar(value="준비됨")
        ttk.Label(self, textvariable=self.status_var, anchor="w", relief="sunken").pack(
            fill="x", side="bottom"
        )

    # ------------------------------------------------------------------
    # 공통 설정 (엑셀 경로 / 기준일) - 모든 탭이 공유
    # ------------------------------------------------------------------
    def _build_common_frame(self):
        frame = ttk.LabelFrame(self, text="공통 설정")
        frame.pack(fill="x", padx=8, pady=8)

        ttk.Label(frame, text="엑셀 파일 경로:").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        ttk.Entry(frame, textvariable=self.excel_path_var, width=80).grid(
            row=0, column=1, sticky="we", padx=4, pady=4
        )
        ttk.Button(frame, text="찾아보기...", command=self._browse_excel).grid(
            row=0, column=2, padx=4, pady=4
        )

        ttk.Label(frame, text="기준일 (reference-date, YYYY-MM-DD):").grid(
            row=1, column=0, sticky="w", padx=4, pady=4
        )
        date_frame = ttk.Frame(frame)
        date_frame.grid(row=1, column=1, columnspan=2, sticky="w", padx=4, pady=4)
        self.reference_date_entry = ttk.Entry(date_frame, textvariable=self.reference_date_var, width=20)
        self.reference_date_entry.pack(side="left")
        ttk.Button(date_frame, text="오늘로 설정", command=self._set_reference_date_to_today).pack(
            side="left", padx=(6, 12)
        )
        self.use_today_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            date_frame, text="항상 오늘 날짜 사용(실행 시점 기준)",
            variable=self.use_today_var, command=self._on_toggle_use_today,
        ).pack(side="left")

        frame.columnconfigure(1, weight=1)

    def _set_reference_date_to_today(self):
        self.reference_date_var.set(date.today().isoformat())

    def _on_toggle_use_today(self):
        if self.use_today_var.get():
            self._set_reference_date_to_today()
            self.reference_date_entry.configure(state="disabled")
        else:
            self.reference_date_entry.configure(state="normal")

    def _get_reference_date(self) -> str:
        """실행 시점의 기준일 문자열을 돌려준다. '항상 오늘 날짜 사용'이
        체크돼 있으면 입력칸 내용과 무관하게 그 순간의 오늘 날짜를 쓴다
        (앱을 여러 날에 걸쳐 켜둔 채로 쓰는 경우를 위함)."""
        if self.use_today_var.get():
            today = date.today().isoformat()
            self.reference_date_var.set(today)
            return today
        return self.reference_date_var.get().strip()

    def _browse_excel(self):
        path = filedialog.askopenfilename(
            title="수주진행현황 엑셀 선택",
            filetypes=[("Excel 파일", "*.xlsx *.xls"), ("모든 파일", "*.*")],
        )
        if path:
            self.excel_path_var.set(path)

    def _set_status(self, text: str):
        self.status_var.set(text)

    # ------------------------------------------------------------------
    # 탭 1: 최적화 실행
    # ------------------------------------------------------------------
    def _build_run_tab(self):
        top = ttk.Frame(self.run_tab)
        top.pack(fill="x", padx=8, pady=8)

        self.time_limit_var = tk.StringVar(value="60")
        self.secondary_time_limit_var = tk.StringVar(value="60")
        self.closed_dates_var = tk.StringVar(value="")

        ttk.Label(top, text="time-limit (초, 1단계):").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        ttk.Entry(top, textvariable=self.time_limit_var, width=12).grid(row=0, column=1, sticky="w", padx=4, pady=4)

        ttk.Label(top, text="secondary-time-limit (초, 2단계):").grid(row=0, column=2, sticky="w", padx=4, pady=4)
        ttk.Entry(top, textvariable=self.secondary_time_limit_var, width=12).grid(
            row=0, column=3, sticky="w", padx=4, pady=4
        )

        ttk.Label(top, text="휴무일(closed-dates, 쉼표로 구분, YYYY-MM-DD):").grid(
            row=1, column=0, sticky="w", padx=4, pady=4
        )
        ttk.Entry(top, textvariable=self.closed_dates_var, width=60).grid(
            row=1, column=1, columnspan=3, sticky="we", padx=4, pady=4
        )

        self.run_button = ttk.Button(top, text="최적화 실행", command=self._on_run_optimization)
        self.run_button.grid(row=2, column=0, sticky="w", padx=4, pady=8)

        top.columnconfigure(3, weight=1)

        log_frame = ttk.Frame(self.run_tab)
        log_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.run_log = tk.Text(log_frame, wrap="word", state="disabled")
        scroll = ttk.Scrollbar(log_frame, command=self.run_log.yview)
        self.run_log.configure(yscrollcommand=scroll.set)
        self.run_log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def _append_run_log(self, text: str):
        self.run_log.configure(state="normal")
        self.run_log.insert("end", text + "\n")
        self.run_log.see("end")
        self.run_log.configure(state="disabled")

    def _on_run_optimization(self):
        excel_path = self.excel_path_var.get().strip()
        reference_date = self._get_reference_date()
        try:
            date.fromisoformat(reference_date)
        except ValueError:
            messagebox.showerror("입력 오류", "기준일은 YYYY-MM-DD 형식이어야 합니다.")
            return

        try:
            time_limit = float(self.time_limit_var.get())
            secondary_time_limit = float(self.secondary_time_limit_var.get())
        except ValueError:
            messagebox.showerror("입력 오류", "time-limit / secondary-time-limit은 숫자여야 합니다.")
            return

        closed_dates = [d.strip() for d in self.closed_dates_var.get().split(",") if d.strip()]
        for d in closed_dates:
            try:
                date.fromisoformat(d)
            except ValueError:
                messagebox.showerror("입력 오류", f"휴무일 형식이 잘못됨: {d!r} (YYYY-MM-DD)")
                return

        args = [
            PLAN_SCRIPT,
            "--excel-path", excel_path,
            "--reference-date", reference_date,
            "--time-limit", str(time_limit),
            "--secondary-time-limit", str(secondary_time_limit),
        ]
        for d in closed_dates:
            args += ["--closed-date", d]

        self.run_log.configure(state="normal")
        self.run_log.delete("1.0", "end")
        self.run_log.configure(state="disabled")
        self.run_button.configure(state="disabled")
        self._set_status("최적화 실행 중... (수 분 걸릴 수 있습니다)")

        def on_line(line: str):
            self.after(0, self._append_run_log, line)

        def on_done(code: int):
            def finish():
                self.run_button.configure(state="normal")
                if code == 0:
                    self._set_status("최적화 완료")
                    messagebox.showinfo("완료", "최적화 실행이 끝났습니다.")
                else:
                    self._set_status(f"최적화 실패 (종료 코드 {code})")
                    messagebox.showerror("실패", f"스크립트가 오류로 종료됐습니다(코드 {code}). 로그를 확인하세요.")

            self.after(0, finish)

        run_command_async(args, on_line, on_done)

    # ------------------------------------------------------------------
    # 탭 2: 생산 스케줄 (전체) - gantt_overview.png
    # ------------------------------------------------------------------
    def _build_overview_tab(self):
        top = ttk.Frame(self.overview_tab)
        top.pack(fill="x", padx=8, pady=8)
        ttk.Button(top, text="불러오기 / 새로고침", command=self._load_overview_image).pack(side="left")
        ttk.Button(top, text="원본 파일 열기", command=lambda: self._open_file(
            os.path.join(OUTPUT_DIR, "gantt_overview.png"))).pack(side="left", padx=8)

        self.overview_canvas = self._make_scrollable_image(self.overview_tab)

    def _load_overview_image(self):
        path = os.path.join(OUTPUT_DIR, "gantt_overview.png")
        self._show_image(path, self.overview_canvas, "overview")

    def _make_scrollable_image(self, parent) -> tk.Canvas:
        """간트 차트 PNG는 라인 수/기간에 따라 창보다 훨씬 크거나 길 수
        있어서(Label 하나로는 넘치는 부분이 잘려서 안 보임), 원본 해상도
        그대로 그리고 좌우/상하 스크롤로 전체를 볼 수 있게 Canvas +
        스크롤바 조합을 쓴다."""
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True, padx=8, pady=8)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        canvas = tk.Canvas(frame, background="#ffffff")
        vscroll = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
        hscroll = ttk.Scrollbar(frame, orient="horizontal", command=canvas.xview)
        canvas.configure(yscrollcommand=vscroll.set, xscrollcommand=hscroll.set)

        canvas.grid(row=0, column=0, sticky="nsew")
        vscroll.grid(row=0, column=1, sticky="ns")
        hscroll.grid(row=1, column=0, sticky="ew")
        return canvas

    def _show_image(self, path: str, canvas: tk.Canvas, key: str):
        if not os.path.exists(path):
            messagebox.showwarning("파일 없음", f"{path}\n먼저 최적화를 실행해서 결과를 만들어야 합니다.")
            return
        img = Image.open(path)
        photo = ImageTk.PhotoImage(img)
        self._image_refs[key] = photo  # GC 방지
        canvas.delete("all")
        canvas.create_image(0, 0, anchor="nw", image=photo)
        canvas.configure(scrollregion=(0, 0, img.width, img.height))
        self._set_status(f"이미지 표시: {path} ({img.width}x{img.height})")

    def _open_file(self, path: str):
        if not os.path.exists(path):
            messagebox.showwarning("파일 없음", path)
            return
        os.startfile(path)  # Windows 전용 - 이 프로젝트는 Windows 환경 기준

    # ------------------------------------------------------------------
    # 탭 3: 일별 Workforce - daily_workforce.csv
    # ------------------------------------------------------------------
    def _build_workforce_tab(self):
        top = ttk.Frame(self.workforce_tab)
        top.pack(fill="x", padx=8, pady=8)
        ttk.Button(top, text="불러오기 / 새로고침", command=self._load_workforce_table).pack(side="left")

        self.workforce_tree = self._make_tree(self.workforce_tab)

    def _make_tree(self, parent) -> ttk.Treeview:
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True, padx=8, pady=8)
        tree = ttk.Treeview(frame, show="headings")
        vscroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vscroll.set)
        tree.pack(side="left", fill="both", expand=True)
        vscroll.pack(side="right", fill="y")
        return tree

    def _load_csv_into_tree(self, path: str, tree: ttk.Treeview):
        if not os.path.exists(path):
            messagebox.showwarning("파일 없음", f"{path}\n먼저 해당 계산을 실행하세요.")
            return
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
        if not rows:
            return
        header, body = rows[0], rows[1:]

        tree.delete(*tree.get_children())
        tree["columns"] = header
        for col in header:
            tree.heading(col, text=col)
            tree.column(col, width=max(80, 700 // len(header)), anchor="center")
        for row in body:
            tree.insert("", "end", values=row)
        self._set_status(f"불러옴: {path}")

    def _load_workforce_table(self):
        self._load_csv_into_tree(os.path.join(OUTPUT_DIR, "daily_workforce.csv"), self.workforce_tree)

    # ------------------------------------------------------------------
    # 탭 4: 일별 스케줄 그림 - plot_day.py --real-plan --day N
    # ------------------------------------------------------------------
    def _build_day_tab(self):
        top = ttk.Frame(self.day_tab)
        top.pack(fill="x", padx=8, pady=8)

        ttk.Label(top, text="날짜(1일차부터):").pack(side="left")
        self.day_var = tk.StringVar(value="1")
        ttk.Spinbox(top, from_=1, to=365, textvariable=self.day_var, width=6).pack(side="left", padx=4)

        self.day_button = ttk.Button(top, text="그림 생성 및 보기", command=self._on_generate_day_plot)
        self.day_button.pack(side="left", padx=8)
        ttk.Button(top, text="원본 파일 열기", command=self._open_day_file).pack(side="left")

        self.day_canvas = self._make_scrollable_image(self.day_tab)

    def _current_day_png(self) -> str:
        day = self.day_var.get().strip()
        return os.path.join(OUTPUT_DIR, f"gantt_day{day}.png")

    def _on_generate_day_plot(self):
        try:
            day = int(self.day_var.get())
        except ValueError:
            messagebox.showerror("입력 오류", "날짜는 정수여야 합니다.")
            return

        excel_path = self.excel_path_var.get().strip()
        reference_date = self._get_reference_date()
        args = [
            PLOT_DAY_SCRIPT,
            "--real-plan",
            "--day", str(day),
            "--excel-path", excel_path,
            "--reference-date", reference_date,
        ]

        self.day_button.configure(state="disabled")
        self._set_status(f"{day}일차 스케줄 그림 생성 중...")
        log_lines: list[str] = []

        def on_line(line: str):
            log_lines.append(line)

        def on_done(code: int):
            def finish():
                self.day_button.configure(state="normal")
                if code == 0:
                    self._show_image(self._current_day_png(), self.day_canvas, "day")
                else:
                    self._set_status(f"{day}일차 그림 생성 실패 (코드 {code})")
                    messagebox.showerror("실패", "\n".join(log_lines[-20:]) or f"종료 코드 {code}")

            self.after(0, finish)

        run_command_async(args, on_line, on_done)

    def _open_day_file(self):
        self._open_file(self._current_day_png())

    # ------------------------------------------------------------------
    # 탭 5: 생산 효율 지표 - production_efficiency.py
    # ------------------------------------------------------------------
    def _build_efficiency_tab(self):
        top = ttk.Frame(self.efficiency_tab)
        top.pack(fill="x", padx=8, pady=8)

        ttk.Label(top, text="WEIGHT_CONTAINER_TUBE:").pack(side="left")
        self.weight_var = tk.StringVar(value="5")
        ttk.Radiobutton(top, text="5", variable=self.weight_var, value="5").pack(side="left", padx=4)
        ttk.Radiobutton(top, text="6.8", variable=self.weight_var, value="6.8").pack(side="left", padx=4)

        self.efficiency_button = ttk.Button(top, text="계산", command=self._on_compute_efficiency)
        self.efficiency_button.pack(side="left", padx=12)

        text_frame = ttk.Frame(self.efficiency_tab)
        text_frame.pack(fill="both", expand=True, padx=8, pady=8)
        self.efficiency_text = tk.Text(text_frame, wrap="word", state="disabled")
        scroll = ttk.Scrollbar(text_frame, command=self.efficiency_text.yview)
        self.efficiency_text.configure(yscrollcommand=scroll.set)
        self.efficiency_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def _on_compute_efficiency(self):
        excel_path = self.excel_path_var.get().strip()
        reference_date = self._get_reference_date()
        weight = self.weight_var.get()

        args = [
            EFFICIENCY_SCRIPT,
            "--excel-path", excel_path,
            "--reference-date", reference_date,
            "--weight-container-tube", weight,
        ]

        self.efficiency_text.configure(state="normal")
        self.efficiency_text.delete("1.0", "end")
        self.efficiency_text.configure(state="disabled")
        self.efficiency_button.configure(state="disabled")
        self._set_status("생산 효율 지표 계산 중...")

        def on_line(line: str):
            def append():
                self.efficiency_text.configure(state="normal")
                self.efficiency_text.insert("end", line + "\n")
                self.efficiency_text.configure(state="disabled")

            self.after(0, append)

        def on_done(code: int):
            def finish():
                self.efficiency_button.configure(state="normal")
                self._set_status("생산 효율 지표 계산 완료" if code == 0 else f"실패 (코드 {code})")

            self.after(0, finish)

        run_command_async(args, on_line, on_done)

    # ------------------------------------------------------------------
    # 탭 6: 실작업 투입 효율 - labor_utilization.py --real-plan
    # ------------------------------------------------------------------
    def _build_labor_tab(self):
        top = ttk.Frame(self.labor_tab)
        top.pack(fill="x", padx=8, pady=8)
        self.labor_button = ttk.Button(top, text="계산", command=self._on_compute_labor_utilization)
        self.labor_button.pack(side="left")

        self.labor_tree = self._make_tree(self.labor_tab)

    def _on_compute_labor_utilization(self):
        excel_path = self.excel_path_var.get().strip()
        reference_date = self._get_reference_date()

        args = [
            LABOR_UTIL_SCRIPT,
            "--real-plan",
            "--excel-path", excel_path,
            "--reference-date", reference_date,
        ]

        self.labor_button.configure(state="disabled")
        self._set_status("실작업 투입 효율 계산 중...")
        log_lines: list[str] = []

        def on_line(line: str):
            log_lines.append(line)

        def on_done(code: int):
            def finish():
                self.labor_button.configure(state="normal")
                if code == 0:
                    self._load_csv_into_tree(os.path.join(OUTPUT_DIR, "labor_utilization.csv"), self.labor_tree)
                    self._set_status("실작업 투입 효율 계산 완료")
                else:
                    self._set_status(f"실패 (코드 {code})")
                    messagebox.showerror("실패", "\n".join(log_lines[-20:]) or f"종료 코드 {code}")

            self.after(0, finish)

        run_command_async(args, on_line, on_done)


if __name__ == "__main__":
    App().mainloop()
