import os
import re
import sys
import json
import time
import queue
import ctypes
import zipfile
import threading
import subprocess
from io import BytesIO
from pathlib import Path
import requests
from PIL import Image
import customtkinter as ctk
from tkinter import filedialog

# Настройка темы
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

# Определяем папку, где лежит сама программа (.exe или .py)
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))


def get_tool_path(tool_name: str) -> str:
    """Ищет утилиту сначала рядом с программой, затем внутри распакованного .exe (если вшита)."""
    local_path = os.path.join(APP_DIR, tool_name)
    if os.path.exists(local_path):
        return local_path
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bundled_path = os.path.join(meipass, tool_name)
        if os.path.exists(bundled_path):
            return bundled_path
    return local_path


# --- Сохранение/загрузка настроек ---
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")


def load_saved_settings() -> dict:
    default_downloads = str(Path.home() / "Downloads")
    defaults = {
        "save_folder": default_downloads,
        "quality": "1080p",
        "limit": 2,
    }
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                folder = data.get("save_folder")
                if isinstance(folder, str) and folder.strip():
                    defaults["save_folder"] = folder.strip()

                quality = data.get("quality")
                valid_qualities = ["4K (2160p)", "2K (1440p)", "1080p", "720p", "480p", "360p"]
                if quality in valid_qualities:
                    defaults["quality"] = quality

                limit = data.get("limit")
                if isinstance(limit, int) and 1 <= limit <= 5:
                    defaults["limit"] = limit
    except Exception:
        pass
    return defaults


def save_settings_to_file(save_folder: str, quality: str, limit: int):
    try:
        payload = {
            "save_folder": save_folder,
            "quality": quality,
            "limit": int(limit),
        }
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def parse_speed_to_mbs(speed_str: str) -> str:
    """Переводит скорость вида '718.99KiB/s', '2.50MiB/s', '1.2MB/s' в 'X.XX МБ/с'."""
    s = speed_str.strip()
    m = re.match(r"^([0-9]*\.?[0-9]+)\s*([KMGkmg]?i?B)/s$", s)
    if not m:
        return s
    val = float(m.group(1))
    unit = m.group(2).lower()

    if unit in ("kib", "kb"):
        mbs = val / 1024.0
    elif unit in ("mib", "mb"):
        mbs = val
    elif unit in ("gib", "gb"):
        mbs = val * 1024.0
    else:
        mbs = val / (1024.0 * 1024.0)

    return f"{mbs:.2f} МБ/с"


def parse_size_to_mb(size_str: str) -> str:
    """Переводит размер вида '509.98KiB', '120.4MiB' в 'X.X МБ'."""
    s = size_str.strip().lstrip("~")
    m = re.match(r"^([0-9]*\.?[0-9]+)\s*([KMGkmg]?i?B)$", s)
    if not m:
        return s
    val = float(m.group(1))
    unit = m.group(2).lower()

    if unit in ("kib", "kb"):
        mb = val / 1024.0
    elif unit in ("mib", "mb"):
        mb = val
    elif unit in ("gib", "gb"):
        mb = val * 1024.0
    else:
        mb = val / (1024.0 * 1024.0)

    return f"{mb:.1f} МБ"


def format_height_label(height) -> str:
    """Преобразует высоту кадра в понятную подпись качества (4K / 2K / 1080p / ...)."""
    if not height:
        return ""
    try:
        h = int(height)
    except (ValueError, TypeError):
        return str(height)
    if h >= 2160:
        return f"4K ({h}p)"
    if h >= 1440:
        return f"2K ({h}p)"
    return f"{h}p"


# Функции Windows API для паузы/возобновления процесса yt-dlp
PROCESS_SUSPEND_RESUME = 0x0800
kernel32 = ctypes.windll.kernel32


def suspend_process(pid):
    handle = kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, pid)
    if handle:
        ctypes.windll.ntdll.NtSuspendProcess(handle)
        kernel32.CloseHandle(handle)


def resume_process(pid):
    handle = kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, pid)
    if handle:
        ctypes.windll.ntdll.NtResumeProcess(handle)
        kernel32.CloseHandle(handle)


class DownloadItem(ctk.CTkFrame):
    """Карточка одного скачиваемого видео"""
    def __init__(self, master, url, app_ref):
        super().__init__(master, fg_color="#242424", corner_radius=10)
        self.url = url
        self.app = app_ref
        self.process = None
        self.is_paused = False
        self.is_cancelled = False
        self.is_finished = False
        self.last_error = ""
        self.quality_label = ""

        self.grid_columnconfigure(1, weight=1)

        # Место под обложку
        self.thumb_label = ctk.CTkLabel(
            self, text="Загрузка\nпревью...", width=120, height=68,
            fg_color="#1a1a1a", corner_radius=6
        )
        self.thumb_label.grid(row=0, column=0, rowspan=3, padx=10, pady=10)

        # Название видео
        self.title_label = ctk.CTkLabel(self, text=url, font=("Segoe UI", 13, "bold"), anchor="w")
        self.title_label.grid(row=0, column=1, sticky="ew", padx=(0, 10), pady=(8, 0))

        # Бейдж качества скачиваемого видео
        self.quality_badge = ctk.CTkLabel(
            self, text="", font=("Segoe UI", 11, "bold"),
            text_color="#4da6ff", fg_color="#1b2634", corner_radius=5,
            padx=7, pady=1
        )
        self.quality_badge.grid(row=0, column=2, padx=(0, 10), pady=(8, 0), sticky="e")
        self.quality_badge.grid_remove()

        # Статус / Проценты / Скорость в МБ/с / Оставшееся время
        self.status_label = ctk.CTkLabel(
            self, text="В очереди...", font=("Segoe UI", 11),
            text_color="#aaaaaa", anchor="w"
        )
        self.status_label.grid(row=1, column=1, columnspan=2, sticky="ew", padx=(0, 10))

        # Прогресс-бар
        self.progress_bar = ctk.CTkProgressBar(self, height=8)
        self.progress_bar.set(0)
        self.progress_bar.grid(row=2, column=1, columnspan=2, sticky="ew", padx=(0, 10), pady=(2, 10))

        # Кнопки управления (Пауза / Удалить)
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.grid(row=0, column=3, rowspan=3, padx=10)

        self.pause_btn = ctk.CTkButton(
            btn_frame, text="⏸", width=36, height=36,
            fg_color="#3a3a3a", hover_color="#4a4a4a",
            command=self.toggle_pause, state="disabled"
        )
        self.pause_btn.pack(side="left", padx=3)

        self.del_btn = ctk.CTkButton(
            btn_frame, text="✕", width=36, height=36,
            fg_color="#8b2626", hover_color="#a83232",
            command=self.cancel_and_remove
        )
        self.del_btn.pack(side="left", padx=3)

        threading.Thread(target=self.fetch_metadata, daemon=True).start()

    def set_quality_text(self, q_text: str):
        if not q_text:
            return
        self.quality_label = q_text
        self.quality_badge.configure(text=q_text)
        self.quality_badge.grid()

    def fetch_metadata(self):
        try:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            cmd = [get_tool_path("yt-dlp.exe"), "--dump-json", "--no-playlist", "--no-warnings", self.url]
            
            res = subprocess.run(
                cmd, capture_output=True, text=True,
                startupinfo=startupinfo, encoding="utf-8", errors="ignore"
            )
            if res.returncode == 0 and not self.is_cancelled:
                data = json.loads(res.stdout)
                title = data.get("title", self.url)
                thumb_url = data.get("thumbnail")

                max_h = self.app.get_max_height()
                formats = data.get("formats") or []
                heights = [
                    f.get("height") for f in formats
                    if isinstance(f.get("height"), int) and f.get("vcodec") not in (None, "none") and f.get("height") <= max_h
                ]
                if not heights:
                    heights = [
                        f.get("height") for f in formats
                        if isinstance(f.get("height"), int) and f.get("vcodec") not in (None, "none")
                    ]
                est_q = format_height_label(max(heights)) if heights else ""

                self.after(0, lambda t=title: self.title_label.configure(
                    text=t[:55] + ("..." if len(t) > 55 else "")
                ))
                if est_q:
                    self.after(0, lambda eq=est_q: self.set_quality_text(eq))

                if thumb_url:
                    img_data = requests.get(thumb_url, timeout=5).content
                    pil_img = Image.open(BytesIO(img_data)).resize((120, 68), Image.Resampling.LANCZOS)
                    ctk_img = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=(120, 68))
                    self.after(0, lambda: self.thumb_label.configure(image=ctk_img, text=""))
        except Exception:
            pass

    def toggle_pause(self):
        if not self.process or self.is_finished:
            return
        if not self.is_paused:
            suspend_process(self.process.pid)
            self.is_paused = True
            self.pause_btn.configure(text="▶", fg_color="#2e7d32")
            self.status_label.configure(text="На паузе", text_color="#f5a623")
        else:
            resume_process(self.process.pid)
            self.is_paused = False
            self.pause_btn.configure(text="⏸", fg_color="#3a3a3a")
            self.status_label.configure(text="Возобновление...", text_color="#aaaaaa")

    def cancel_and_remove(self):
        self.is_cancelled = True
        if self.process and self.process.poll() is None:
            if self.is_paused:
                resume_process(self.process.pid)
            self.process.terminate()
        self.destroy()


class App(ctk.CTk):
    SETTINGS_TARGET_WIDTH = 270
    STEP_PX = 20
    FRAME_MS = 10

    def __init__(self):
        super().__init__()
        self.title("Video Downloader")
        self.geometry("840x540")
        self.minsize(680, 440)

        # Настройки по умолчанию (загружаем сохранённые)
        saved = load_saved_settings()
        self.save_folder = ctk.StringVar(value=saved["save_folder"])
        self.quality_var = ctk.StringVar(value=saved["quality"])
        self.limit_var = ctk.IntVar(value=saved["limit"])

        # Состояние плавной боковой панели
        self.menu_open = False
        self.anim_running = False
        self.current_menu_width = 0

        self.tools_ready = False

        # Очередь задач и счетчик активных загрузок
        self.task_queue = queue.Queue()
        self.active_downloads = 0
        self.lock = threading.Lock()

        self.setup_ui()

        # Проверяем/обновляем yt-dlp.exe и проверяем ffmpeg.exe
        threading.Thread(target=self.ensure_dependencies, daemon=True).start()
        # Фоновый диспетчер очереди
        threading.Thread(target=self.queue_worker, daemon=True).start()

    def setup_ui(self):
        # Верхняя панель
        top_bar = ctk.CTkFrame(self, fg_color="transparent")
        top_bar.pack(fill="x", padx=15, pady=(15, 10))

        # Кнопка "3 полоски" (Плавное меню настроек слева)
        self.menu_btn = ctk.CTkButton(
            top_bar, text="☰", width=40, height=40, font=("Segoe UI", 18),
            fg_color="#2b2b2b", hover_color="#3b3b3b", command=self.toggle_menu
        )
        self.menu_btn.pack(side="left", padx=(0, 10))

        # Контейнер поля ввода без подсказки
        input_box = ctk.CTkFrame(
            top_bar, height=40, fg_color="#343638",
            border_width=1, border_color="#565b5e", corner_radius=8
        )
        input_box.pack(side="left", fill="x", expand=True, padx=(0, 10))
        input_box.pack_propagate(False)

        self.url_entry = ctk.CTkEntry(
            input_box, height=36, fg_color="transparent",
            border_width=0, font=("Segoe UI", 13)
        )
        self.url_entry.pack(side="left", fill="x", expand=True, padx=(8, 2), pady=2)
        self.url_entry.bind("<Return>", lambda e: self.add_download())
        self.url_entry.bind("<Button-1>", self.focus_entry)
        self.url_entry.bind("<Button-3>", self.paste_from_clipboard)
        self.url_entry.bind("<Control-KeyPress>", self.on_entry_ctrl_key)

        self.paste_btn = ctk.CTkButton(
            input_box, text="📋", width=32, height=32, font=("Segoe UI", 15),
            fg_color="transparent", hover_color="#46484b", text_color="#cccccc",
            command=self.paste_from_clipboard
        )
        self.paste_btn.pack(side="right", padx=(0, 4))

        # Кнопка Скачать
        self.download_btn = ctk.CTkButton(
            top_bar, text="Скачать", width=110, height=40,
            font=("Segoe UI", 13, "bold"), command=self.add_download, state="disabled"
        )
        self.download_btn.pack(side="right")

        # Основной контейнер под шапкой: выезжающая панель слева + область скачиваний справа
        self.main_body = ctk.CTkFrame(self, fg_color="transparent")
        self.main_body.pack(fill="both", expand=True, padx=15, pady=(0, 12))

        # Боковая панель настроек без скругления и рамки при движении
        self.settings_panel = ctk.CTkFrame(
            self.main_body, width=1, fg_color="#202020",
            corner_radius=0, border_width=0
        )
        self.settings_panel.pack_propagate(False)

        self.settings_inner = ctk.CTkFrame(
            self.settings_panel, width=self.SETTINGS_TARGET_WIDTH, fg_color="transparent"
        )
        self.settings_inner.place(x=0, y=0, relheight=1)
        self.build_settings_panel()

        # Правая область со списком загрузок
        self.content_area = ctk.CTkFrame(self.main_body, fg_color="transparent")
        self.content_area.pack(side="right", fill="both", expand=True)

        self.scroll_frame = ctk.CTkScrollableFrame(self.content_area, fg_color="#181818", corner_radius=10)
        self.scroll_frame.pack(fill="both", expand=True, pady=(0, 8))

        self.dep_frame = ctk.CTkFrame(self.content_area, fg_color="#202020", corner_radius=8)
        self.dep_frame.pack(fill="x")

        self.dep_label = ctk.CTkLabel(
            self.dep_frame, text="Проверка компонентов (yt-dlp и ffmpeg)...",
            font=("Segoe UI", 11), text_color="#aaaaaa"
        )
        self.dep_label.pack(side="left", padx=12, pady=6)

        self.dep_progress = ctk.CTkProgressBar(self.dep_frame, width=180, height=8)
        self.dep_progress.set(0)
        self.dep_progress.pack(side="right", padx=12, pady=6)

    def focus_entry(self, event=None):
        try:
            self.url_entry._entry.focus_set()
        except Exception:
            self.url_entry.focus_set()

    def on_entry_ctrl_key(self, event):
        ch = (event.char or "").lower()
        # Ctrl+A (en) и Ctrl+Ф (ru)
        if event.keycode == 65 or ch in ("a", "ф"):
            self.url_entry.select_range(0, "end")
            self.url_entry.icursor("end")
            return "break"
        # Ctrl+V (en) и Ctrl+М (ru)
        if event.keycode == 86 or ch in ("v", "м"):
            self.paste_from_clipboard()
            return "break"
        return None

    def paste_from_clipboard(self, event=None):
        self.focus_entry()
        try:
            text = self.clipboard_get().strip()
            if text:
                self.url_entry.delete(0, "end")
                self.url_entry.insert(0, text)
        except Exception:
            pass
        return "break"

    def build_settings_panel(self):
        header = ctk.CTkFrame(self.settings_inner, fg_color="transparent")
        header.pack(fill="x", padx=15, pady=(15, 10))
        ctk.CTkLabel(header, text="Настройки", font=("Segoe UI", 16, "bold")).pack(side="left")
        ctk.CTkButton(
            header, text="✕", width=28, height=28, fg_color="transparent",
            hover_color="#333333", command=self.toggle_menu
        ).pack(side="right")

        # 1. Папка сохранения
        ctk.CTkLabel(self.settings_inner, text="Папка для сохранения:", font=("Segoe UI", 12, "bold")).pack(anchor="w", padx=15, pady=(8, 5))
        folder_box = ctk.CTkFrame(self.settings_inner, fg_color="transparent")
        folder_box.pack(fill="x", padx=15)
        ctk.CTkEntry(folder_box, textvariable=self.save_folder, state="readonly", font=("Segoe UI", 11)).pack(side="left", fill="x", expand=True, padx=(0, 5))
        ctk.CTkButton(folder_box, text="...", width=35, command=self.choose_folder).pack(side="right")

        # 2. Качество видео
        ctk.CTkLabel(self.settings_inner, text="Максимальное качество:", font=("Segoe UI", 12, "bold")).pack(anchor="w", padx=15, pady=(20, 5))
        qualities = ["4K (2160p)", "2K (1440p)", "1080p", "720p", "480p", "360p"]
        self.quality_menu = ctk.CTkOptionMenu(
            self.settings_inner, values=qualities,
            variable=self.quality_var, command=self.on_quality_change
        )
        self.quality_menu.pack(fill="x", padx=15)

        # 3. Лимит одновременных скачиваний
        ctk.CTkLabel(self.settings_inner, text="Одновременных загрузок:", font=("Segoe UI", 12, "bold")).pack(anchor="w", padx=15, pady=(20, 5))
        limit_box = ctk.CTkFrame(self.settings_inner, fg_color="transparent")
        limit_box.pack(fill="x", padx=15)

        self.limit_label = ctk.CTkLabel(
            limit_box, text=str(self.limit_var.get()),
            font=("Segoe UI", 13, "bold"), width=25
        )
        self.limit_label.pack(side="right")

        slider = ctk.CTkSlider(
            limit_box, from_=1, to=5, number_of_steps=4,
            variable=self.limit_var, command=self.on_limit_change
        )
        slider.pack(side="left", fill="x", expand=True, padx=(0, 10))

    # --- Обработчики изменений настроек с сохранением в файл ---
    def on_quality_change(self, value=None):
        save_settings_to_file(self.save_folder.get(), self.quality_var.get(), self.limit_var.get())

    def on_limit_change(self, value):
        val = int(round(float(value)))
        self.limit_var.set(val)
        self.limit_label.configure(text=str(val))
        save_settings_to_file(self.save_folder.get(), self.quality_var.get(), val)

    # --- Быстрая и плавная анимация боковой панели без дёрганий ---
    def toggle_menu(self):
        self.menu_open = not self.menu_open
        if not self.settings_panel.winfo_manager():
            self.current_menu_width = 1
            self.settings_panel.configure(width=1)
            self.settings_panel.pack(side="left", fill="y", padx=(0, 12), before=self.content_area)

        if not self.anim_running:
            self.anim_running = True
            self._animate_menu_step()

    def _animate_menu_step(self):
        target = self.SETTINGS_TARGET_WIDTH if self.menu_open else 0

        if self.current_menu_width < target:
            self.current_menu_width = min(target, self.current_menu_width + self.STEP_PX)
        elif self.current_menu_width > target:
            self.current_menu_width = max(target, self.current_menu_width - self.STEP_PX)

        self.settings_panel.configure(width=max(1, self.current_menu_width))
        self.update_idletasks()

        if self.current_menu_width != target:
            self.after(self.FRAME_MS, self._animate_menu_step)
        else:
            self.anim_running = False
            if target == 0:
                self.current_menu_width = 0
                self.settings_panel.pack_forget()

    # --- Автозагрузка и обновление yt-dlp и ffmpeg ---
    def download_with_progress(self, url: str, dest_path: str, label_prefix: str):
        with requests.get(url, stream=True, timeout=30) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            downloaded = 0
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total > 0:
                            pct = downloaded / total
                            mb_done = downloaded / (1024 * 1024)
                            mb_tot = total / (1024 * 1024)
                            self.after(0, lambda p=pct, d=mb_done, t=mb_tot: (
                                self.dep_progress.set(p),
                                self.dep_label.configure(text=f"{label_prefix}: {d:.1f} / {t:.1f} МБ ({p*100:.0f}%)")
                            ))

    def ensure_dependencies(self):
        try:
            ytdlp_path = get_tool_path("yt-dlp.exe")
            ffmpeg_path = get_tool_path("ffmpeg.exe")

            ytdlp_url = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
            if not os.path.exists(ytdlp_path):
                self.download_with_progress(ytdlp_url, ytdlp_path, "Скачивание yt-dlp с GitHub")
            elif os.path.dirname(os.path.abspath(ytdlp_path)) == os.path.abspath(APP_DIR):
                self.after(0, lambda: self.dep_label.configure(text="Обновление yt-dlp до актуальной версии..."))
                try:
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    subprocess.run(
                        [ytdlp_path, "-U"],
                        capture_output=True, text=True, timeout=25,
                        startupinfo=startupinfo, encoding="utf-8", errors="ignore"
                    )
                except Exception:
                    pass

            if not os.path.exists(ffmpeg_path):
                ffmpeg_zip_url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
                zip_temp = os.path.join(APP_DIR, "ffmpeg_temp.zip")
                self.download_with_progress(ffmpeg_zip_url, zip_temp, "Скачивание FFmpeg с GitHub")

                self.after(0, lambda: self.dep_label.configure(text="Распаковка ffmpeg.exe..."))
                with zipfile.ZipFile(zip_temp, "r") as zf:
                    for member in zf.namelist():
                        if member.endswith("bin/ffmpeg.exe"):
                            with zf.open(member) as src, open(ffmpeg_path, "wb") as dst:
                                dst.write(src.read())
                            break
                if os.path.exists(zip_temp):
                    os.remove(zip_temp)

            self.tools_ready = True
            self.after(0, self.on_dependencies_ready)

        except Exception as e:
            self.after(0, lambda err=e: self.dep_label.configure(
                text=f"Ошибка загрузки компонентов с GitHub: {err}", text_color="#ff5555"
            ))

    def on_dependencies_ready(self):
        self.download_btn.configure(state="normal")
        self.dep_frame.pack_forget()

    # --- Основная логика UI и загрузок ---
    def choose_folder(self):
        path = filedialog.askdirectory(initialdir=self.save_folder.get())
        if path:
            self.save_folder.set(path)
            save_settings_to_file(self.save_folder.get(), self.quality_var.get(), self.limit_var.get())

    def add_download(self):
        if not self.tools_ready:
            return
        url = self.url_entry.get().strip()
        if not url:
            return
        self.url_entry.delete(0, "end")

        item = DownloadItem(self.scroll_frame, url, self)
        item.pack(fill="x", padx=5, pady=5)
        self.task_queue.put(item)

    def queue_worker(self):
        while True:
            with self.lock:
                can_start = self.tools_ready and (self.active_downloads < self.limit_var.get())

            if can_start and not self.task_queue.empty():
                item = self.task_queue.get()
                if item.is_cancelled:
                    continue
                with self.lock:
                    self.active_downloads += 1
                threading.Thread(target=self.process_download, args=(item,), daemon=True).start()
            time.sleep(0.3)

    def get_max_height(self) -> int:
        q = self.quality_var.get()
        height_map = {
            "4K (2160p)": 2160,
            "2K (1440p)": 1440,
            "1080p": 1080,
            "720p": 720,
            "480p": 480,
            "360p": 360,
        }
        return height_map.get(q, 1080)

    def get_format_string(self):
        h = self.get_max_height()
        return f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"

    def process_download(self, item: DownloadItem):
        try:
            item.after(0, lambda: item.pause_btn.configure(state="normal"))
            item.after(0, lambda: item.status_label.configure(text="Подготовка к скачиванию..."))

            out_tmpl = os.path.join(self.save_folder.get(), "%(title)s.%(ext)s")
            fmt = self.get_format_string()

            yt_dlp_path = get_tool_path("yt-dlp.exe")
            ffmpeg_path = get_tool_path("ffmpeg.exe")

            cmd = [
                yt_dlp_path,
                "--ffmpeg-location", ffmpeg_path,
                "-f", fmt,
                "--merge-output-format", "mp4",
                "--newline",
                "-o", out_tmpl,
                item.url
            ]

            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            item.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="ignore",
                startupinfo=startupinfo
            )

            progress_regex = re.compile(
                r"\[download\]\s+(\d+(?:\.\d+)?)%\s+of\s+~?(\S+)\s+at\s+(\S+)(?:\s+ETA\s+(\S+))?"
            )
            res_regex = re.compile(r"(\d{3,4})x(\d{3,4})")

            for line in item.process.stdout:
                if item.is_cancelled:
                    break
                clean_line = line.strip()

                if "[info]" in clean_line or "[download]" in clean_line:
                    rm = res_regex.search(clean_line)
                    if rm:
                        h_val = int(rm.group(2))
                        q_str = format_height_label(h_val)
                        if q_str and q_str != item.quality_label:
                            item.after(0, lambda qs=q_str: item.set_quality_text(qs))

                match = progress_regex.search(clean_line)
                if match:
                    percent = float(match.group(1))
                    size_mb = parse_size_to_mb(match.group(2))
                    speed_mbs = parse_speed_to_mbs(match.group(3))
                    eta = match.group(4) if match.group(4) else "--:--"
                    item.after(0, lambda p=percent, s=size_mb, sp=speed_mbs, e=eta: self.update_item_progress(item, p, s, sp, e))
                elif "[Merger]" in clean_line or "Merging formats" in clean_line:
                    item.after(0, lambda: item.status_label.configure(text="Склейка видео и звука (FFmpeg)..."))
                elif "ERROR:" in clean_line:
                    item.last_error = clean_line.split("ERROR:", 1)[1].strip()

            item.process.wait()

            if not item.is_cancelled:
                item.is_finished = True
                if item.process.returncode == 0:
                    item.after(0, lambda: self.finish_item(item, True))
                else:
                    item.after(0, lambda: self.finish_item(item, False))

        except Exception as e:
            if not item.is_cancelled:
                item.after(0, lambda: item.status_label.configure(text=f"Ошибка: {e}", text_color="#ff5555"))
        finally:
            with self.lock:
                self.active_downloads -= 1

    def update_item_progress(self, item, percent, size_mb, speed_mbs, eta):
        if not item.is_paused and not item.is_cancelled:
            item.progress_bar.set(percent / 100.0)
            q_part = f" • {item.quality_label}" if item.quality_label else ""
            item.status_label.configure(
                text=f"{percent:.1f}% из {size_mb}{q_part} • {speed_mbs} • Осталось: {eta}",
                text_color="#aaaaaa"
            )

    def finish_item(self, item, success):
        item.pause_btn.configure(state="disabled")
        if success:
            item.progress_bar.set(1.0)
            item.progress_bar.configure(progress_color="#2e7d32")
            q_part = f" ({item.quality_label})" if item.quality_label else ""
            item.status_label.configure(text=f"Скачивание завершено{q_part} ✓", text_color="#4caf50")
        else:
            err_text = item.last_error[:90] if item.last_error else "Ошибка скачивания (проверьте ссылку)"
            item.status_label.configure(text=f"Ошибка: {err_text}", text_color="#ff5555")


if __name__ == "__main__":
    app = App()
    app.mainloop()