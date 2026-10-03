import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import cv2
import mss
import numpy as np
from PIL import Image, ImageTk

from . import __version__
from .config import Config, ROOT
from .runtime import Runner
from .replay import replay_video, SAMPLE_CASTS
from .windows import Hotkeys, list_windows, client_box

BG, PANEL, TEXT, MUTED, ACCENT = "#111820", "#1b2632", "#e6edf3", "#9bafc0", "#40d1b3"

TIMING_FIELDS = [("reel_wait", "普通提竿 → 抛竿 / 秒"), ("reload_wait", "提竿＋换饵 → 抛竿 / 秒"),
                 ("cast_blind", "抛竿后屏蔽 / 秒"), ("zoom_delay", "抛竿后右键 / 秒"),
                 ("calm_time", "平静确认 / 秒")]
VISION_FIELDS = [("splash_ratio", "水花触发面积 / %"), ("calm_ratio", "平静面积上限 / %"),
                 ("value_min", "亮度下限 / 0–255"), ("saturation_max", "饱和度上限 / 0–255"),
                 ("fps", "检测频率 / 每秒"), ("bite_timeout", "等待咬钩超时 / 秒"),
                 ("initial_bait", "手动初始鱼饵 / 1–5")]
ZOOM_LABELS = {"toggle": "单击切换", "hold": "按住右键", "manual": "手动放大"}


class RegionPicker(tk.Toplevel):
    def __init__(self, parent, image, box, callback):
        super().__init__(parent)
        self.callback, self.box = callback, box
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.geometry(f'{box["width"]}x{box["height"]}{box["left"]:+d}{box["top"]:+d}')
        self.photo = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB)))
        self.canvas = tk.Canvas(self, highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_image(0, 0, image=self.photo, anchor="nw")
        self.canvas.create_rectangle(0, 0, box["width"], 55, fill=BG, outline="")
        self.canvas.create_text(20, 26, text="拖动框选检测区域 · 松开保存 · Esc 取消", fill="white", font=("Microsoft YaHei UI", 15), anchor="w")
        self.origin, self.rect = None, None
        self.canvas.bind("<ButtonPress-1>", self.press)
        self.canvas.bind("<B1-Motion>", self.drag)
        self.canvas.bind("<ButtonRelease-1>", self.release)
        self.bind("<Escape>", lambda _: self.destroy())
        self.focus_force()
        self.grab_set()

    def press(self, event):
        self.origin = (event.x, event.y)
        if self.rect:
            self.canvas.delete(self.rect)
        self.rect = self.canvas.create_rectangle(event.x, event.y, event.x, event.y, outline=ACCENT, width=3)

    def drag(self, event):
        if self.origin:
            self.canvas.coords(self.rect, *self.origin, event.x, event.y)

    def release(self, event):
        if not self.origin:
            return
        x1, x2 = sorted((self.origin[0], max(0, min(self.box["width"], event.x))))
        y1, y2 = sorted((self.origin[1], max(0, min(self.box["height"], event.y))))
        if x2-x1 >= 8 and y2-y1 >= 8:
            self.callback([x1/self.box["width"], y1/self.box["height"], (x2-x1)/self.box["width"], (y2-y1)/self.box["height"]])
            self.destroy()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("钓鱼经验机 · 本地视觉助手")
        self.geometry(f"{min(680, self.winfo_screenwidth()-60)}x{min(560, self.winfo_screenheight()-70)}")
        self.minsize(620, 460)
        self.configure(bg=BG)
        self.events = queue.Queue()
        self.runner = self.replay_thread = None
        self.replay_stop = threading.Event()
        self.countdown_id = None
        self.pending_generation = 0
        self.closing = False
        self.settings_window = None
        self.hotkey_labels = {1: "F8", 2: "F9"}
        try:
            self.cfg = Config.load()
        except Exception as exc:
            self.cfg = Config()
            messagebox.showwarning("配置已重置", str(exc), parent=self)
        self.style_ui()
        self.build_ui()
        self.build_setting_vars()
        self.refresh_windows()
        self.hotkeys = Hotkeys(self.publish)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(60, self.poll)

    def style_ui(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=TEXT, font=("Microsoft YaHei UI", 10))
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("TLabelframe", background=BG, bordercolor="#344657")
        style.configure("TLabelframe.Label", foreground=ACCENT, background=BG)
        style.configure("TButton", background="#283b4b", padding=(12, 8), borderwidth=0)
        style.map("TButton", background=[("active", "#37566b")])
        style.configure("Accent.TButton", background=ACCENT, foreground="#0e2021", font=("Microsoft YaHei UI", 11, "bold"))
        style.map("Accent.TButton", background=[("active", "#76e5cf")])
        style.configure("Stop.TButton", background="#613345", foreground="#ffdee5")
        style.configure("TEntry", fieldbackground=PANEL, foreground=TEXT, insertcolor=TEXT, padding=5)
        style.configure("TCombobox", fieldbackground=PANEL, background=PANEL, foreground=TEXT, padding=5)
        style.map("TCombobox", fieldbackground=[("readonly", PANEL)], foreground=[("readonly", TEXT)])
        style.configure("TRadiobutton", background=BG)
        style.configure("TCheckbutton", background=BG)
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=PANEL, padding=(16, 8))
        style.map("TNotebook.Tab", background=[("selected", "#30485b")])

    def build_setting_vars(self):
        self.vars = {}
        for key, _ in TIMING_FIELDS+VISION_FIELDS:
            value = getattr(self.cfg, key)
            if key.endswith("ratio"):
                value = round(value*100, 4)
            self.vars[key] = tk.StringVar(value=str(value))
        self.zoom_var = tk.StringVar(value=ZOOM_LABELS[self.cfg.zoom_mode])
        self.bait_var = tk.BooleanVar(value=self.cfg.bait_vision)
        self.video_var = tk.StringVar()
        self.casts_var = tk.StringVar(value=", ".join(map(str, SAMPLE_CASTS)))

    def build_ui(self):
        outer = ttk.Frame(self, padding=18)
        outer.pack(fill="both", expand=True)
        head = ttk.Frame(outer)
        head.pack(fill="x")
        ttk.Label(head, text="钓鱼经验机", font=("Microsoft YaHei UI", 21, "bold")).pack(side="left")
        ttk.Label(head, text=f"LOCAL VISION  /  {__version__}", style="Muted.TLabel").pack(side="right")
        ttk.Label(outer, text="水花识别 · 分段等待 · 自动换饵节奏", style="Muted.TLabel").pack(anchor="w", pady=(3, 12))
        target = ttk.Frame(outer)
        target.pack(fill="x")
        ttk.Label(target, text="游戏窗口").pack(side="left", padx=(0, 10))
        self.window_box = ttk.Combobox(target, state="readonly")
        self.window_box.pack(side="left", fill="x", expand=True)
        ttk.Button(target, text="刷新窗口", command=self.refresh_windows).pack(side="left", padx=(8, 0))
        self.already_cast = tk.BooleanVar(value=False)
        ttk.Checkbutton(outer, text="接管已入水、已放大的鱼竿", variable=self.already_cast).pack(anchor="w", pady=(8, 2))
        controls = ttk.Frame(outer)
        controls.pack(fill="x", pady=(4, 12))
        ttk.Button(controls, text="① 框选水花区域", command=lambda: self.calibrate("splash_roi")).pack(side="left")
        ttk.Button(controls, text="② 框选鱼饵数字", command=lambda: self.calibrate("bait_roi")).pack(side="left", padx=(6, 0))
        ttk.Button(controls, text="③ 单次左键测试", command=lambda: self.start_countdown(test_click=True)).pack(side="left", padx=(6, 0))
        self.start_button = ttk.Button(controls, text="④ 开始 · F8", style="Accent.TButton", command=self.start_countdown)
        self.start_button.pack(side="left", padx=(6, 0))
        self.stop_button = ttk.Button(controls, text="暂停 · F9", style="Stop.TButton", command=self.stop)
        self.stop_button.pack(side="left", padx=(6, 0))
        ttk.Button(controls, text="高级设置…", command=self.open_settings).pack(side="right")
        self.status_var = tk.StringVar(value="默认区域已内置：可直接 ③ 测试、④ 开始；识别不准时再 ①② 重新框选。开始后有 5 秒切回游戏")
        ttk.Label(outer, textvariable=self.status_var, foreground=ACCENT, font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", pady=(0, 10))
        log_frame = ttk.LabelFrame(outer, text="运行记录", padding=10)
        log_frame.pack(fill="both", expand=True)
        self.stats_var = tk.StringVar(value="亮色 —   鱼饵 —   抛竿 0   提竿信号 0")
        ttk.Label(log_frame, textvariable=self.stats_var, style="Muted.TLabel").pack(anchor="w")
        self.log = tk.Text(log_frame, bg=PANEL, fg=TEXT, relief="flat", font=("Microsoft YaHei UI", 9), state="disabled")
        self.log.pack(fill="both", expand=True, pady=(6, 0))
        self.footer_var = tk.StringVar(value="F8 开始 / 暂停   ·   F9 立即停止   ·   切出游戏自动暂停")
        ttk.Label(outer, textvariable=self.footer_var, style="Muted.TLabel").pack(anchor="w", pady=(10, 0))

    def open_settings(self):
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.deiconify()
            self.settings_window.lift()
            self.settings_window.focus_force()
            return
        win = tk.Toplevel(self)
        win.title("高级设置")
        win.configure(bg=BG)
        win.transient(self)
        self.settings_window = win
        outer = ttk.Frame(win, padding=14)
        outer.pack(fill="both", expand=True)
        settings_tabs = ttk.Notebook(outer)
        settings_tabs.pack(fill="both", expand=True)
        timing_frame, vision_frame, replay_frame = (ttk.Frame(settings_tabs, padding=10),
                                                    ttk.Frame(settings_tabs, padding=10),
                                                    ttk.Frame(settings_tabs, padding=10))
        settings_tabs.add(timing_frame, text="节奏")
        settings_tabs.add(vision_frame, text="识别")
        settings_tabs.add(replay_frame, text="录像验证")
        for row, (key, label) in enumerate(TIMING_FIELDS):
            ttk.Label(timing_frame, text=label).grid(row=row, column=0, sticky="w", pady=3)
            ttk.Entry(timing_frame, textvariable=self.vars[key], width=9).grid(row=row, column=1, padx=(10, 0), pady=3)
        ttk.Label(timing_frame, text="右键放大方式").grid(row=5, column=0, sticky="w", pady=5)
        ttk.Combobox(timing_frame, textvariable=self.zoom_var, values=list(ZOOM_LABELS.values()), width=9, state="readonly").grid(row=5, column=1)
        ttk.Label(timing_frame, text="等待均从每次提竿开始计时", style="Muted.TLabel").grid(row=7, column=0, columnspan=2, sticky="w", pady=8)
        for row, (key, label) in enumerate(VISION_FIELDS):
            ttk.Label(vision_frame, text=label).grid(row=row, column=0, sticky="w", pady=3)
            ttk.Entry(vision_frame, textvariable=self.vars[key], width=9).grid(row=row, column=1, padx=(10, 0), pady=3)
        ttk.Checkbutton(vision_frame, text="识别鱼饵数字并确认换饵完成", variable=self.bait_var).grid(row=7, column=0, columnspan=2, sticky="w", pady=6)
        ttk.Entry(replay_frame, textvariable=self.video_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(replay_frame, text="选择录像", command=self.choose_video).grid(row=0, column=1, padx=(5, 0))
        ttk.Label(replay_frame, text="甩竿起点（秒，以逗号分隔；默认是已分析录像）", style="Muted.TLabel").grid(row=1, column=0, columnspan=2, sticky="w", pady=5)
        ttk.Entry(replay_frame, textvariable=self.casts_var).grid(row=2, column=0, sticky="ew")
        ttk.Button(replay_frame, text="离线回放", command=self.start_replay).grid(row=2, column=1, padx=(5, 0))
        replay_frame.columnconfigure(0, weight=1)
        ttk.Button(outer, text="保存参数", command=self.save_config).pack(side="bottom", fill="x", pady=(10, 0), before=settings_tabs)

    def publish(self, kind, payload):
        self.events.put((kind, payload))

    def append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text+"\n")
        if int(self.log.index("end-1c").split(".")[0]) > 300:
            self.log.delete("1.0", "80.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def refresh_windows(self):
        previous = None
        if getattr(self, "windows", None) and self.window_box.current() >= 0:
            previous = self.windows[self.window_box.current()][0]
        self.windows = list_windows()
        self.window_box["values"] = [f"{title}  [PID {pid}]" for _, title, pid in self.windows]
        match = next((i for i, (hwnd, _, _) in enumerate(self.windows) if hwnd == previous), None)
        if match is None:
            match = next((i for i, (_, title, _) in enumerate(self.windows)
                          if "三角洲" in title or "delta force" in title.lower()), None)
        if match is not None:
            self.window_box.current(match)

    def selected_window(self):
        index = self.window_box.current()
        if not 0 <= index < len(self.windows):
            raise ValueError("请先选择游戏窗口")
        return self.windows[index]

    def read_config(self):
        data = self.cfg.__dict__.copy()
        for key, var in self.vars.items():
            value = float(var.get())
            if key.endswith("ratio"):
                value /= 100
            if key in ("fps", "initial_bait", "value_min", "saturation_max"):
                if not value.is_integer():
                    raise ValueError(f"{key} 必须是整数")
                value = int(value)
            data[key] = value
        data["zoom_mode"] = {label: mode for mode, label in ZOOM_LABELS.items()}[self.zoom_var.get()]
        data["bait_vision"] = self.bait_var.get()
        return Config(**data).validate()

    def save_config(self):
        try:
            self.cfg = self.read_config()
            self.cfg.save()
            self.append_log("参数已保存，下次开始时生效。")
            return True
        except Exception as exc:
            messagebox.showerror("参数无效", str(exc), parent=self)
            return False

    def busy(self):
        return (self.runner and self.runner.is_alive()) or (self.replay_thread and self.replay_thread.is_alive())

    def start_countdown(self, test_click=False):
        if self.busy() or self.countdown_id is not None:
            self.stop()
            return
        if not test_click and not self.save_config():
            return
        try:
            window = self.selected_window()
        except Exception as exc:
            messagebox.showerror("选择窗口", str(exc), parent=self)
            return
        self.pending_generation += 1
        generation = self.pending_generation
        def countdown(seconds):
            self.countdown_id = None
            if generation != self.pending_generation:
                return
            if seconds:
                self.status_var.set(f"{seconds} 秒后{'仅点击一次左键' if test_click else '开始'}，请切回所选游戏窗口")
                self.countdown_id = self.after(1000, lambda: countdown(seconds-1))
            else:
                self.runner = Runner(self.cfg, window, self.already_cast.get(), self.publish, test_click=test_click)
                self.runner.start()
        countdown(5)

    def stop(self):
        self.pending_generation += 1
        if self.countdown_id is not None:
            self.after_cancel(self.countdown_id)
            self.countdown_id = None
        if self.runner:
            self.runner.stop()
        self.replay_stop.set()
        self.status_var.set("正在暂停…" if self.busy() else "已暂停")

    def calibrate(self, key):
        self.stop()
        try:
            window = self.selected_window()
        except Exception as exc:
            messagebox.showerror("选择窗口", str(exc), parent=self)
            return
        generation = self.pending_generation
        self.status_var.set("3 秒后截取所选游戏窗口，请先切回游戏")
        def open_picker():
            self.countdown_id = None
            if generation != self.pending_generation:
                return
            try:
                box = client_box(window[0])
                with mss.mss() as capture:
                    image = np.asarray(capture.grab(box))[:, :, :3].copy()
                def selected(roi):
                    setattr(self.cfg, key, roi)
                    self.cfg.save()
                    self.status_var.set("检测区域已保存")
                    self.append_log(f"已保存 {'水花' if key == 'splash_roi' else '鱼饵数字'} 区域。")
                RegionPicker(self, image, box, selected)
            except Exception as exc:
                messagebox.showerror("区域校准失败", str(exc), parent=self)
        self.countdown_id = self.after(3000, open_picker)

    def choose_video(self):
        path = filedialog.askopenfilename(filetypes=[("录像", "*.mp4 *.mkv *.avi"), ("所有文件", "*.*")])
        if path:
            self.video_var.set(path)

    def start_replay(self):
        if self.busy() or self.countdown_id is not None:
            messagebox.showinfo("请先暂停", "暂停当前任务后再回放录像。", parent=self)
            return
        if not self.save_config():
            return
        try:
            casts = [float(x.strip()) for x in self.casts_var.get().replace("，", ",").split(",")]
            path = self.video_var.get().strip()
            if not path:
                raise ValueError("请选择录像")
        except Exception as exc:
            messagebox.showerror("录像参数", str(exc), parent=self)
            return
        self.replay_stop = threading.Event()
        config = self.cfg
        def work():
            try:
                result, output = replay_video(path, config, casts, stop_event=self.replay_stop)
                self.publish("replay_done", (result, str(output)))
            except Exception as exc:
                self.publish("error", str(exc))
        self.replay_thread = threading.Thread(target=work, daemon=True)
        self.replay_thread.start()

    def poll(self):
        if self.closing:
            return
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "toggle":
                self.start_countdown()
            elif kind == "stop":
                self.stop()
            elif kind == "log":
                names = {"cast": "已发送抛竿两次点击", "hook": "提竿信号", "zoom": "已发送右键操作", "start": "开始", "stop": "停止", "test_click": "已发送一次左键测试", "bait_snapshot": "鱼饵裁剪已保存"}
                if value["kind"] == "sample":
                    # One-line telemetry for the stats row, kept out of the scrolling log.
                    bait = value.get("bait")
                    stats = f'亮色 {value["splash_ratio"]*100:.2f}%   鱼饵 {bait if bait is not None else "—"}'
                    if bait is not None:
                        stats += f' ({value.get("bait_confidence", 0)*100:.0f}%)'
                    stats += f'   抛竿 {value.get("casts", 0)}   提竿信号 {value.get("hooks", 0)}'
                    self.stats_var.set(stats)
                    if self.busy():
                        self.status_var.set(value["status"])
                    continue
                detail = value.get("reason", "")
                if value["kind"] == "start":
                    detail = "自动循环；输入是否生效需观察游戏"
                    if value.get("test_click"):
                        detail = "单次左键测试，不等待鱼饵或水花，不会自动循环"
                elif value["kind"] == "focus":
                    detail = f'当前前台：{value["foreground_title"]} [PID {value["foreground_pid"]}]'
                elif value["kind"] == "permissions":
                    detail = f'运行权限：助手 {value["assistant"]["label"]} / 游戏 {value["game"]["label"]}'
                self.append_log(f'{value["time"][11:]}  {names.get(value["kind"], value["kind"])}  {detail}')
            elif kind in ("error", "hotkey_error"):
                self.append_log(str(value))
                self.status_var.set(str(value))
            elif kind == "hotkey_bound":
                self.hotkey_labels[value["id"]] = value["label"]
                self.start_button.configure(text="④ 开始 · "+self.hotkey_labels[1])
                self.stop_button.configure(text="暂停 · "+self.hotkey_labels[2])
                self.footer_var.set(f'{self.hotkey_labels[1]} 开始/暂停 · {self.hotkey_labels[2]} 停止 · 切出游戏自动暂停')
                self.append_log(f'{value["label"]} 热键已就绪')
            elif kind == "finished":
                self.status_var.set(value)
            elif kind == "replay_done":
                result, output = value
                times = ", ".join(f'{x["time_s"]:.2f}s' for x in result["events"])
                self.status_var.set("回放已取消" if result["cancelled"] else f'回放完成 · {len(result["events"])} 次水花信号')
                self.append_log(f"录像信号：{times}\n结果：{output}")
        self.after(60, self.poll)

    def close(self):
        self.closing = True
        self.stop()
        self.hotkeys.close()
        if self.settings_window is not None:
            self.settings_window.destroy()
        def finish():
            if self.busy():
                self.after(50, finish)
            else:
                self.destroy()
        finish()
