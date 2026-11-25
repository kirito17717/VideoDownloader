import os
import re
import sys
import json
import time
import uuid
import queue
import ctypes
import shutil
import zipfile
import threading
import subprocess
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image
from PyQt6.QtCore import (
    Qt,
    QTimer,
    QProcess,
    QPropertyAnimation,
    QEasingCurve,
    QRect,
    QPointF,
    pyqtSignal,
    QObject,
)
from PyQt6.QtGui import (
    QPixmap,
    QImage,
    QAction,
    QIcon,
    QPainter,
    QPen,
    QColor,
    QPolygonF,
    QPainterPath,
)
from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QFrame,
    QProgressBar,
    QComboBox,
    QSlider,
    QFileDialog,
    QMenu,
    QSizePolicy,
    QStyleOptionComboBox,
    QStyle,
)

# Определяем папку, где лежит сама программа (.exe или .py)
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

THUMBS_DIR = os.path.join(APP_DIR, "thumbnails")
DB_FILE = os.path.join(APP_DIR, "downloads_db.json")
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")

STARTUP_MODES = [
    "Не загружать автоматически",
    "Начать загрузку при запуске",
]

QUALITIES = ["4K (2160p)", "2K (1440p)", "1080p", "720p", "480p", "360p"]

DB_WRITE_LOCK = threading.Lock()


def create_app_icon() -> QIcon:
    """Создаёт прежнюю синюю иконку приложения с тёмным окном внутри."""
    size = 64
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    # Внешний синий скруглённый блок
    outer_rect = QRect(4, 4, 56, 56)
    outer_path = QPainterPath()
    outer_path.addRoundedRect(outer_rect.x(), outer_rect.y(), outer_rect.width(), outer_rect.height(), 14, 14)
    painter.fillPath(outer_path, QColor("#0095ff"))

    # Правый объёмный блик/грань
    painter.save()
    painter.setClipPath(outer_path)
    painter.fillRect(QRect(38, 0, 26, 64), QColor("#0074d9"))
    painter.restore()

    # Внутренний тёмный квадрат по центру
    inner_rect = QRect(16, 16, 32, 32)
    inner_path = QPainterPath()
    inner_path.addRoundedRect(inner_rect.x(), inner_rect.y(), inner_rect.width(), inner_rect.height(), 7, 7)
    painter.fillPath(inner_path, QColor("#181c24"))

    painter.end()
    return QIcon(pm)


class StyledComboBox(QComboBox):
    """QComboBox с чётким бордером и стрелкой '>', повёрнутой вниз."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("""
            QComboBox {
                background-color: #2c2c2c;
                color: #f0f0f0;
                border: 1px solid #484848;
                border-radius: 6px;
                padding: 6px 34px 6px 10px;
                font-size: 12px;
            }
            QComboBox:hover {
                background-color: #323232;
                border: 1px solid #606060;
            }
            QComboBox:on {
                border: 1px solid #3b8ed0;
            }
            QComboBox::drop-down {
                subcontrol-origin: border;
                subcontrol-position: top right;
                width: 28px;
                border: none;
                border-left: 1px solid #404040;
                background-color: transparent;
            }
            QComboBox::down-arrow {
                image: none;
                width: 0px;
                height: 0px;
            }
            QComboBox QAbstractItemView {
                background-color: #252525;
                color: #f0f0f0;
                border: 1px solid #484848;
                selection-background-color: #1f538d;
                selection-color: #ffffff;
                outline: none;
            }
        """)

    def paintEvent(self, event):
        super().paintEvent(event)

        opt = QStyleOptionComboBox()
        self.initStyleOption(opt)
        drop_rect = self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox,
            opt,
            QStyle.SubControl.SC_ComboBoxArrow,
            self,
        )
        if not drop_rect.isValid() or drop_rect.width() <= 0:
            drop_rect = QRect(self.width() - 28, 0, 28, self.height())

        cx = drop_rect.center().x()
        cy = drop_rect.center().y()

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Рисуем символ '>', развёрнутый вниз (v-образный уголок из '>' повернутого на 90° по часовой)
        pen = QPen(QColor("#d4d4d4"), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        w = 4.5
        h = 3.2
        p_left = QPointF(cx - w, cy - h / 2.0)
        p_tip = QPointF(cx, cy + h)
        p_right = QPointF(cx + w, cy - h / 2.0)
        painter.drawPolyline(QPolygonF([p_left, p_tip, p_right]))
        painter.end()


def get_tool_path(tool_name: str) -> str:
    """Ищет утилиту рядом с программой, в текущей папке, внутри .exe или в PATH."""
    local_path = os.path.join(APP_DIR, tool_name)
    if os.path.exists(local_path):
        return local_path

    cwd_path = os.path.join(os.getcwd(), tool_name)
    if os.path.exists(cwd_path):
        return cwd_path

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bundled_path = os.path.join(meipass, tool_name)
        if os.path.exists(bundled_path):
            return bundled_path

    in_path = shutil.which(tool_name)
    if in_path:
        return in_path

    return local_path


def load_saved_settings() -> dict:
    default_downloads = str(Path.home() / "Downloads")
    defaults = {
        "save_folder": default_downloads,
        "quality": "1080p",
        "limit": 2,
        "startup_mode": "Продолжить с того момента",
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
                if quality in QUALITIES:
                    defaults["quality"] = quality

                limit = data.get("limit")
                if isinstance(limit, int) and 1 <= limit <= 5:
                    defaults["limit"] = limit

                startup_mode = data.get("startup_mode")
                if startup_mode in STARTUP_MODES:
                    defaults["startup_mode"] = startup_mode
    except Exception:
        pass
    return defaults


def save_settings_to_file(save_folder: str, quality: str, limit: int, startup_mode: str):
    try:
        payload = {
            "save_folder": save_folder,
            "quality": quality,
            "limit": int(limit),
            "startup_mode": startup_mode,
        }
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def load_downloads_db() -> list:
    try:
        if os.path.exists(DB_FILE):
            with open(DB_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


def save_downloads_db(records: list):
    with DB_WRITE_LOCK:
        try:
            tmp_path = DB_FILE + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(records, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, DB_FILE)
        except Exception:
            pass


def quality_to_max_height(quality_str: str) -> int:
    height_map = {
        "4K (2160p)": 2160,
        "2K (1440p)": 1440,
        "1080p": 1080,
        "720p": 720,
        "480p": 480,
        "360p": 360,
    }
    return height_map.get(quality_str, 1080)


def parse_speed_to_mbs(speed_str: str) -> str:
    s = speed_str.strip().lstrip("~")
    m = re.match(r"^([0-9]*\.?[0-9]+)\s*([KMGkmg]?i?B)/s$", s)
    if not m:
        return s if s else "0.00 МБ/с"
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
    s = size_str.strip().lstrip("~")
    m = re.match(r"^([0-9]*\.?[0-9]+)\s*([KMGkmg]?i?B)$", s)
    if not m:
        return s if s else "? МБ"
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


def extract_video_id_from_url(url: str) -> str:
    patterns = [
        r"(?:v=|/v/|/embed/|/shorts/|youtu\.be/)([A-Za-z0-9_-]{6,})",
        r"status/(\d+)",
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return ""


def kill_process_tree(pid: int):
    """Мгновенно завершает yt-dlp.exe и все его дочерние процессы (включая ffmpeg.exe)."""
    if not pid:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
                timeout=3,
            )
        except Exception:
            pass


# Windows API для заморозки/разморозки процесса
if sys.platform == "win32":
    PROCESS_SUSPEND_RESUME = 0x0800
    kernel32 = ctypes.windll.kernel32

    def suspend_pid(pid: int):
        handle = kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, pid)
        if handle:
            ctypes.windll.ntdll.NtSuspendProcess(handle)
            kernel32.CloseHandle(handle)

    def resume_pid(pid: int):
        handle = kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, pid)
        if handle:
            ctypes.windll.ntdll.NtResumeProcess(handle)
            kernel32.CloseHandle(handle)
else:
    def suspend_pid(pid: int):
        pass

    def resume_pid(pid: int):
        pass


class WorkerSignals(QObject):
    dep_progress = pyqtSignal(float, str)
    dep_status = pyqtSignal(str, str)
    dep_ready = pyqtSignal()
    meta_loaded = pyqtSignal(str, str, str, str)


class DownloadCard(QFrame):
    """Карточка скачивания с QProcess и сохранением состояния"""

    def __init__(self, record: dict, main_window, parent=None):
        super().__init__(parent)
        self.win = main_window

        self.item_id = record.get("id") or str(uuid.uuid4())
        self.url = record["url"]
        self.title = record.get("title") or self.url
        self.thumb_path = record.get("thumb_path") or ""
        self.locked_quality = record.get("quality") or "1080p"
        self.save_folder = record.get("save_folder") or str(Path.home() / "Downloads")
        self.status = record.get("status") or "queued"
        self.percent = float(record.get("percent") or 0.0)
        self.size_mb = record.get("size_mb") or ""
        self.file_path = record.get("file_path") or ""
        self.actual_quality_label = record.get("actual_quality_label") or ""

        self.process: QProcess | None = None
        self.is_paused = (self.status == "paused")
        self.is_cancelled = False
        self.stopping_for_exit = False
        self.last_error = ""
        self.recent_lines: list[str] = []
        self.known_paths: set[str] = set()
        if self.file_path:
            self.known_paths.add(self.file_path)

        self.last_save_ts = 0.0
        self.last_ui_ts = 0.0

        self._setup_ui()
        self._sync_ui_from_state()

        if self.thumb_path and os.path.exists(self.thumb_path):
            self._set_pixmap_from_file(self.thumb_path)
        else:
            threading.Thread(target=self._fetch_metadata_thread, daemon=True).start()

    def _setup_ui(self):
        self.setObjectName("DownloadCard")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setStyleSheet("""
            QFrame#DownloadCard {
                background-color: #242424;
                border-radius: 10px;
                border: 1px solid #303030;
            }
            QLabel {
                background: transparent;
                border: none;
            }
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        self.thumb_label = QLabel("Превью")
        self.thumb_label.setFixedSize(116, 65)
        self.thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb_label.setStyleSheet("""
            QLabel {
                background-color: #181818;
                color: #888888;
                border-radius: 6px;
                font-size: 11px;
            }
        """)
        layout.addWidget(self.thumb_label)

        info_container = QWidget()
        info_container.setMinimumWidth(80)
        info_container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        info_container.setStyleSheet("background: transparent; border: none;")
        info_layout = QVBoxLayout(info_container)
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(5)

        top_row = QHBoxLayout()
        top_row.setSpacing(6)

        self.title_label = QLabel(self._short_title(self.title))
        self.title_label.setMinimumWidth(40)
        self.title_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.title_label.setStyleSheet("background: transparent; color: #f0f0f0; font-size: 13px; font-weight: bold;")
        top_row.addWidget(self.title_label, 1)

        self.quality_badge = QLabel(self.actual_quality_label or self.locked_quality)
        self.quality_badge.setStyleSheet("""
            QLabel {
                background-color: #1b2634;
                color: #4da6ff;
                border-radius: 5px;
                padding: 2px 7px;
                font-size: 11px;
                font-weight: bold;
            }
        """)
        top_row.addWidget(self.quality_badge)

        info_layout.addLayout(top_row)

        self.status_label = QLabel("В очереди...")
        self.status_label.setMinimumWidth(40)
        self.status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.status_label.setStyleSheet("background: transparent; color: #aaaaaa; font-size: 11px;")
        info_layout.addWidget(self.status_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setFixedHeight(8)
        self.progress_bar.setTextVisible(False)
        self._apply_progress_style(completed=False)
        info_layout.addWidget(self.progress_bar)

        layout.addWidget(info_container, 1)

        btn_container = QWidget()
        btn_container.setStyleSheet("background: transparent; border: none;")
        btn_layout = QHBoxLayout(btn_container)
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.setSpacing(5)

        self.action_btn = QPushButton("⏸")
        self.action_btn.setFixedSize(34, 34)
        self.action_btn.clicked.connect(self.on_action_clicked)
        btn_layout.addWidget(self.action_btn)

        self.del_disk_btn = QPushButton("💾")
        self.del_disk_btn.setFixedSize(34, 34)
        self.del_disk_btn.setStyleSheet("""
            QPushButton {
                background-color: #3c3526;
                color: #f5c26b;
                border: none;
                border-radius: 6px;
                font-size: 11px;
                font-weight: bold;
            }
            QPushButton:hover { background-color: #544932; }
        """)
        self.del_disk_btn.clicked.connect(self.delete_from_disk)
        btn_layout.addWidget(self.del_disk_btn)

        self.del_all_btn = QPushButton("✕ всё")
        self.del_all_btn.setFixedSize(52, 34)
        self.del_all_btn.setStyleSheet("""
            QPushButton {
                background-color: #8b2626;
                color: #ffffff;
                border: none;
                border-radius: 6px;
                font-size: 11px;
                font-weight: bold;
            }
            QPushButton:hover { background-color: #a83232; }
        """)
        self.del_all_btn.clicked.connect(self.delete_all)
        btn_layout.addWidget(self.del_all_btn)

        layout.addWidget(btn_container)

    def _apply_progress_style(self, completed: bool):
        chunk_color = "#2e7d32" if completed else "#1f538d"
        self.progress_bar.setStyleSheet(f"""
            QProgressBar {{
                background-color: #161616;
                border: none;
                border-radius: 4px;
            }}
            QProgressBar::chunk {{
                background-color: {chunk_color};
                border-radius: 4px;
            }}
        """)

    @staticmethod
    def _short_title(title: str) -> str:
        return title[:70] + ("..." if len(title) > 70 else "")

    def _set_pixmap_from_file(self, path: str):
        try:
            pil_img = Image.open(path).convert("RGB").resize((116, 65), Image.Resampling.LANCZOS)
            data = pil_img.tobytes("raw", "RGB")
            qimg = QImage(data, 116, 65, 116 * 3, QImage.Format.Format_RGB888)
            self.thumb_label.setPixmap(QPixmap.fromImage(qimg))
            self.thumb_label.setText("")
        except Exception:
            pass

    def _fetch_metadata_thread(self):
        try:
            cmd = [get_tool_path("yt-dlp.exe"), "--dump-json", "--no-playlist", "--no-warnings", self.url]
            creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            res = subprocess.run(
                cmd, capture_output=True, text=True,
                creationflags=creationflags, encoding="utf-8", errors="ignore", timeout=25
            )
            if res.returncode == 0 and not self.is_cancelled:
                data = json.loads(res.stdout)
                title = data.get("title") or self.title
                thumb_url = data.get("thumbnail")

                max_h = quality_to_max_height(self.locked_quality)
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
                actual_q = self.actual_quality_label or (format_height_label(max(heights)) if heights else "")

                saved_thumb = ""
                if thumb_url:
                    os.makedirs(THUMBS_DIR, exist_ok=True)
                    img_data = requests.get(thumb_url, timeout=8).content
                    pil_img = Image.open(BytesIO(img_data)).convert("RGB")
                    saved_thumb = os.path.join(THUMBS_DIR, f"{self.item_id}.jpg")
                    pil_img.save(saved_thumb, format="JPEG", quality=85)

                self.win.signals.meta_loaded.emit(self.item_id, title, saved_thumb, actual_q)
        except Exception:
            pass

    def apply_metadata(self, title: str, saved_thumb: str, actual_q: str):
        self.title = title
        if saved_thumb:
            self.thumb_path = saved_thumb
        if actual_q and not self.actual_quality_label:
            self.actual_quality_label = actual_q

        self.title_label.setText(self._short_title(self.title))
        self.quality_badge.setText(self.actual_quality_label or self.locked_quality)
        if self.thumb_path and os.path.exists(self.thumb_path):
            self._set_pixmap_from_file(self.thumb_path)
        self.win.persist_all()

    def _sync_ui_from_state(self):
        self.progress_bar.setValue(int(round(min(100.0, max(0.0, self.percent)) * 10)))
        self._apply_progress_style(completed=(self.status == "completed"))

        if self.status == "completed":
            q_part = f" ({self.actual_quality_label or self.locked_quality})"
            self.status_label.setText(f"Скачано{q_part} ✓")
            self.status_label.setStyleSheet("background: transparent; color: #4caf50; font-size: 11px;")
            self.action_btn.setText("✓")
            self.action_btn.setEnabled(False)
            self.action_btn.setStyleSheet("background-color: #254728; color: #a5d6a7; border: none; border-radius: 6px; font-weight: bold;")
        elif self.status == "removed_from_disk":
            self.status_label.setText("Файл удалён с диска • Нажмите ⬇ для скачивания")
            self.status_label.setStyleSheet("background: transparent; color: #bbbbbb; font-size: 11px;")
            self.action_btn.setText("⬇")
            self.action_btn.setEnabled(True)
            self.action_btn.setStyleSheet("QPushButton { background-color: #1f538d; color: #fff; border: none; border-radius: 6px; font-size: 14px; } QPushButton:hover { background-color: #2b6cb0; }")
        elif self.status == "paused":
            pct_part = f"{self.percent:.1f}%" if self.percent > 0 else "0.0%"
            self.status_label.setText(f"На паузе ({pct_part}) • Нажмите ▶ для продолжения")
            self.status_label.setStyleSheet("background: transparent; color: #f5a623; font-size: 11px;")
            self.action_btn.setText("▶")
            self.action_btn.setEnabled(True)
            self.action_btn.setStyleSheet("QPushButton { background-color: #2e7d32; color: #fff; border: none; border-radius: 6px; font-size: 13px; } QPushButton:hover { background-color: #388e3c; }")
        elif self.status == "downloading":
            self.status_label.setStyleSheet("background: transparent; color: #aaaaaa; font-size: 11px;")
            self.action_btn.setText("⏸")
            self.action_btn.setEnabled(True)
            self.action_btn.setStyleSheet("QPushButton { background-color: #3a3a3a; color: #fff; border: none; border-radius: 6px; font-size: 13px; } QPushButton:hover { background-color: #4a4a4a; }")
        elif self.status == "error":
            err_txt = self.last_error[:90] if self.last_error else f"Ошибка ({self.percent:.1f}%)"
            self.status_label.setText(f"Ошибка: {err_txt}")
            self.status_label.setStyleSheet("background: transparent; color: #ff5555; font-size: 11px;")
            self.action_btn.setText("⬇")
            self.action_btn.setEnabled(True)
            self.action_btn.setStyleSheet("QPushButton { background-color: #1f538d; color: #fff; border: none; border-radius: 6px; font-size: 14px; } QPushButton:hover { background-color: #2b6cb0; }")
        else:
            self.status_label.setText("В очереди...")
            self.status_label.setStyleSheet("background: transparent; color: #aaaaaa; font-size: 11px;")
            self.action_btn.setText("⏸")
            self.action_btn.setEnabled(False)
            self.action_btn.setStyleSheet("background-color: #323232; color: #777777; border: none; border-radius: 6px;")

    def to_dict(self) -> dict:
        st = self.status
        if self.stopping_for_exit and st in ("downloading", "queued"):
            st = "paused"
        return {
            "id": self.item_id,
            "url": self.url,
            "title": self.title,
            "thumb_path": self.thumb_path,
            "quality": self.locked_quality,
            "save_folder": self.save_folder,
            "status": st,
            "percent": self.percent,
            "size_mb": self.size_mb,
            "file_path": self.file_path,
            "actual_quality_label": self.actual_quality_label,
        }

    def on_action_clicked(self):
        if self.status == "downloading":
            self.pause_download()
        elif self.status in ("paused", "removed_from_disk", "error"):
            self.resume_or_redownload()

    def pause_download(self):
        if self.status != "downloading":
            return
        self.is_paused = True
        self.status = "paused"
        if self.process and self.process.state() != QProcess.ProcessState.NotRunning:
            pid = self.process.processId()
            if pid:
                suspend_pid(pid)
        self._sync_ui_from_state()
        self.win.persist_all()

    def resume_or_redownload(self):
        if self.status == "completed":
            return
        if self.status == "paused" and self.process and self.process.state() != QProcess.ProcessState.NotRunning:
            pid = self.process.processId()
            if pid:
                resume_pid(pid)
            self.is_paused = False
            self.status = "downloading"
            self._sync_ui_from_state()
            self.status_label.setText("Возобновление...")
            self.win.persist_all()
            return

        self.is_paused = False
        self.is_cancelled = False
        self.last_error = ""
        if self.status == "removed_from_disk":
            self.percent = 0.0
        self.status = "queued"
        self._sync_ui_from_state()
        self.win.persist_all()
        self.win.enqueue_item(self)

    def start_qprocess(self):
        os.makedirs(self.save_folder, exist_ok=True)
        out_tmpl = os.path.join(self.save_folder, "%(title)s.%(ext)s")
        max_h = quality_to_max_height(self.locked_quality)
        fmt = f"bestvideo[height<={max_h}]+bestaudio/best[height<={max_h}]/best"

        yt_dlp_path = get_tool_path("yt-dlp.exe")
        ffmpeg_path = get_tool_path("ffmpeg.exe")

        args = [
            "--ffmpeg-location", ffmpeg_path,
            "-f", fmt,
            "--merge-output-format", "mp4",
            "--continue",
            "--progress",
            "--newline",
            "--no-colors",
            "-o", out_tmpl,
            self.url,
        ]

        self.recent_lines.clear()
        self.last_error = ""
        self.is_paused = False
        self.is_cancelled = False
        self.status = "downloading"
        self._sync_ui_from_state()
        self.status_label.setText("Подготовка к скачиванию...")

        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        proc.readyReadStandardOutput.connect(self._on_proc_output)
        proc.finished.connect(self._on_proc_finished)
        self.process = proc
        proc.start(yt_dlp_path, args)

    def _on_proc_output(self):
        if not self.process or self.is_cancelled:
            return
        raw = bytes(self.process.readAllStandardOutput()).decode("utf-8", errors="ignore")
        if not raw:
            return

        pct_regex = re.compile(r"\[download\]\s+([0-9]+(?:\.[0-9]+)?)%")
        size_regex = re.compile(r"\bof\s+~?([0-9]+(?:\.[0-9]+)?\s*[KMGkmg]?i?B)\b")
        speed_regex = re.compile(r"\bat\s+~?([0-9]+(?:\.[0-9]+)?\s*[KMGkmg]?i?B/s)\b")
        eta_regex = re.compile(r"\bETA\s+(\S+)")

        res_regex = re.compile(r"(\d{3,4})x(\d{3,4})")
        dest_regex = re.compile(r"\[download\] Destination:\s+(.+)$")
        merge_dest_regex = re.compile(r'\[Merger\] Merging formats into "(.+)"')
        already_regex = re.compile(r"\[download\]\s+(.+?)\s+has already been downloaded")

        now = time.time()
        for line in re.split(r"[\r\n]+", raw):
            clean = line.strip()
            if not clean:
                continue

            self.recent_lines.append(clean)
            if len(self.recent_lines) > 8:
                self.recent_lines.pop(0)

            m_dest = dest_regex.search(clean) or merge_dest_regex.search(clean) or already_regex.search(clean)
            if m_dest:
                detected_path = m_dest.group(1).strip()
                self.file_path = detected_path
                self.known_paths.add(detected_path)

            if "[info]" in clean or "[download]" in clean:
                rm = res_regex.search(clean)
                if rm:
                    q_str = format_height_label(int(rm.group(2)))
                    if q_str and q_str != self.actual_quality_label:
                        self.actual_quality_label = q_str
                        self.quality_badge.setText(q_str)

            m_pct = pct_regex.search(clean)
            if m_pct:
                pct = float(m_pct.group(1))
                m_sz = size_regex.search(clean)
                m_spd = speed_regex.search(clean)
                m_eta = eta_regex.search(clean)

                sz = parse_size_to_mb(m_sz.group(1)) if m_sz else (self.size_mb or "? МБ")
                spd = parse_speed_to_mbs(m_spd.group(1)) if m_spd else "0.00 МБ/с"
                eta = m_eta.group(1) if m_eta else "--:--"

                self.percent = pct
                self.size_mb = sz

                if not self.is_paused and self.status == "downloading" and (now - self.last_ui_ts >= 0.08):
                    self.last_ui_ts = now
                    self.progress_bar.setValue(int(round(min(100.0, max(0.0, pct)) * 10)))
                    q_part = f" • {self.actual_quality_label or self.locked_quality}"
                    self.status_label.setText(f"{pct:.1f}% из {sz}{q_part} • {spd} • Осталось: {eta}")

                if now - self.last_save_ts >= 1.5:
                    self.last_save_ts = now
                    self.win.persist_all()

            elif "[Merger]" in clean or "Merging formats" in clean:
                if not self.is_paused:
                    self.status_label.setText("Склейка видео и звука (FFmpeg)...")
            elif "ERROR:" in clean:
                self.last_error = clean.split("ERROR:", 1)[1].strip()

    def _on_proc_finished(self, exit_code: int, exit_status):
        proc = self.process
        self.process = None
        if proc:
            proc.deleteLater()

        self.win.release_download_slot()

        if self.is_cancelled:
            return

        if self.stopping_for_exit:
            self.status = "paused"
            self.is_paused = True
            self.win.persist_all()
            return

        if self.status == "paused":
            self.win.persist_all()
            return

        if exit_code == 0:
            self.status = "completed"
            self.percent = 100.0
        else:
            if not self.last_error and self.recent_lines:
                self.last_error = self.recent_lines[-1]
            self.status = "error"

        self._sync_ui_from_state()
        self.win.persist_all()

    def stop_process_fast(self):
        proc = self.process
        self.process = None
        if proc and proc.state() != QProcess.ProcessState.NotRunning:
            pid = proc.processId()
            if pid and self.is_paused:
                resume_pid(pid)
            try:
                proc.readyReadStandardOutput.disconnect()
                proc.finished.disconnect()
            except Exception:
                pass
            if pid:
                kill_process_tree(pid)
            proc.kill()
            proc.waitForFinished(800)
            proc.deleteLater()
            self.win.release_download_slot()

    def _collect_matching_files(self) -> set[str]:
        targets = set()

        all_recorded = set(self.known_paths)
        if self.file_path:
            all_recorded.add(self.file_path)

        base_prefixes = set()
        for p in all_recorded:
            if not p:
                continue
            targets.add(p)
            targets.add(p + ".part")
            targets.add(p + ".ytdl")
            no_ext = os.path.splitext(p)[0]
            clean_prefix = re.sub(r"\.f\d+$", "", no_ext)
            base_prefixes.add(os.path.abspath(clean_prefix))
            for ext in (".mp4", ".webm", ".mkv", ".m4a", ".mp3", ".part", ".ytdl", ".temp"):
                targets.add(clean_prefix + ext)

        vid_id = extract_video_id_from_url(self.url)
        folders_to_check = set()
        for f in (self.save_folder, self.win.save_folder, str(Path.home() / "Downloads"), APP_DIR):
            if f and os.path.isdir(f):
                folders_to_check.add(os.path.abspath(f))

        title_tokens = [
            tok.lower() for tok in re.findall(r"[\w\u0400-\u04ff]{3,}", self.title or "")
            if tok.lower() not in ("http", "https", "youtu", "youtube", "shorts", "watch")
        ]

        for folder in folders_to_check:
            try:
                for fname in os.listdir(folder):
                    full = os.path.abspath(os.path.join(folder, fname))
                    if not os.path.isfile(full):
                        continue
                    low = fname.lower()
                    matched = False

                    for pref in base_prefixes:
                        if full.startswith(pref):
                            matched = True
                            break

                    if not matched and vid_id and vid_id.lower() in low:
                        matched = True

                    if not matched and title_tokens:
                        hits = sum(1 for t in title_tokens if t in low)
                        if hits >= min(2, len(title_tokens)):
                            matched = True

                    if matched:
                        targets.add(full)
            except Exception:
                pass

        return targets

    def _remove_files_from_disk(self):
        targets = self._collect_matching_files()
        for _ in range(4):
            still_locked = []
            for p in targets:
                try:
                    if os.path.exists(p) and os.path.isfile(p):
                        os.remove(p)
                except PermissionError:
                    still_locked.append(p)
                except Exception:
                    pass
            if not still_locked:
                break
            targets = set(still_locked)
            time.sleep(0.15)

    def delete_from_disk(self):
        """Удаляет файл с диска, но оставляет карточку в списке."""
        self.is_cancelled = True
        self.is_paused = False
        self.stop_process_fast()
        self._remove_files_from_disk()

        self.file_path = ""
        self.known_paths.clear()
        self.percent = 0.0
        self.status = "removed_from_disk"
        self._sync_ui_from_state()
        self.win.persist_all()

    def delete_all(self):
        """Удаляет файл с диска (если есть) и полностью убирает карточку."""
        self.is_cancelled = True
        self.is_paused = False
        self.stop_process_fast()
        self._remove_files_from_disk()

        if self.thumb_path and os.path.exists(self.thumb_path):
            try:
                os.remove(self.thumb_path)
            except Exception:
                pass

        self.win.remove_card(self)


class MainWindow(QMainWindow):
    SETTINGS_WIDTH = 270

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Video Downloader")
        self.setWindowIcon(create_app_icon())
        self.resize(900, 580)
        self.setMinimumSize(740, 460)

        saved = load_saved_settings()
        self.save_folder = saved["save_folder"]
        self.quality = saved["quality"]
        self.limit = saved["limit"]
        self.startup_mode = saved["startup_mode"]

        self.tools_ready = False
        self.closing = False
        self.panel_open = False
        self.active_downloads = 0
        self.task_queue: queue.Queue[DownloadCard] = queue.Queue()
        self.cards: list[DownloadCard] = []

        self.signals = WorkerSignals()
        self.signals.dep_progress.connect(self._on_dep_progress)
        self.signals.dep_status.connect(self._on_dep_status)
        self.signals.dep_ready.connect(self._on_dep_ready)
        self.signals.meta_loaded.connect(self._on_meta_loaded)

        self._apply_dark_theme()
        self._setup_ui()
        self._setup_panel_animation()
        self._restore_cards_from_db()

        self.queue_timer = QTimer(self)
        self.queue_timer.setInterval(200)
        self.queue_timer.timeout.connect(self._pump_queue)
        self.queue_timer.start()

        threading.Thread(target=self._ensure_dependencies_thread, daemon=True).start()

    def _apply_dark_theme(self):
        self.setStyleSheet("""
            QMainWindow {
                background-color: #141414;
                color: #f0f0f0;
            }
            QWidget {
                color: #f0f0f0;
                font-family: "Segoe UI";
            }
            QLabel {
                background: transparent;
                border: none;
            }
            QScrollArea {
                border: none;
                background-color: #181818;
                border-radius: 10px;
            }
            QScrollBar:vertical {
                background: #181818;
                width: 10px;
                margin: 2px;
                border-radius: 5px;
            }
            QScrollBar::handle:vertical {
                background: #383838;
                min-height: 24px;
                border-radius: 5px;
            }
            QScrollBar::handle:vertical:hover {
                background: #505050;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
            QSlider {
                background: transparent;
            }
            QSlider::groove:horizontal {
                height: 6px;
                background: #323232;
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                background: #3b8ed0;
                width: 16px;
                height: 16px;
                margin: -5px 0;
                border-radius: 8px;
            }
        """)

    def _setup_ui(self):
        central = QWidget()
        central.setStyleSheet("background-color: #141414;")
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(15, 15, 15, 12)
        root_layout.setSpacing(10)

        # Верхняя панель
        top_bar = QHBoxLayout()
        top_bar.setSpacing(10)

        self.menu_btn = QPushButton("☰")
        self.menu_btn.setFixedSize(40, 40)
        self.menu_btn.setStyleSheet("""
            QPushButton { background-color: #2b2b2b; border: none; border-radius: 8px; font-size: 18px; }
            QPushButton:hover { background-color: #3b3b3b; }
        """)
        self.menu_btn.clicked.connect(self._toggle_settings_panel)
        top_bar.addWidget(self.menu_btn)

        input_frame = QFrame()
        input_frame.setFixedHeight(40)
        input_frame.setStyleSheet("""
            QFrame {
                background-color: #343638;
                border: 1px solid #565b5e;
                border-radius: 8px;
            }
        """)
        input_layout = QHBoxLayout(input_frame)
        input_layout.setContentsMargins(8, 2, 4, 2)
        input_layout.setSpacing(4)

        self.url_input = QLineEdit()
        self.url_input.setStyleSheet("background: transparent; border: none; font-size: 13px;")
        self.url_input.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.url_input.customContextMenuRequested.connect(self._show_input_context_menu)
        self.url_input.returnPressed.connect(self.add_download)
        input_layout.addWidget(self.url_input)

        self.paste_btn = QPushButton("📋")
        self.paste_btn.setFixedSize(32, 32)
        self.paste_btn.setStyleSheet("""
            QPushButton { background: transparent; border: none; border-radius: 6px; font-size: 15px; }
            QPushButton:hover { background-color: #46484b; }
        """)
        self.paste_btn.clicked.connect(self.paste_from_clipboard)
        input_layout.addWidget(self.paste_btn)

        top_bar.addWidget(input_frame, 1)

        self.download_btn = QPushButton("Скачать")
        self.download_btn.setFixedSize(110, 40)
        self.download_btn.setEnabled(False)
        self.download_btn.setStyleSheet("""
            QPushButton {
                background-color: #1f538d; color: #ffffff; border: none;
                border-radius: 8px; font-size: 13px; font-weight: bold;
            }
            QPushButton:hover { background-color: #2b6cb0; }
            QPushButton:disabled { background-color: #262626; color: #666666; }
        """)
        self.download_btn.clicked.connect(self.add_download)
        top_bar.addWidget(self.download_btn)

        root_layout.addLayout(top_bar)

        # Основная область: панель настроек слева + область видео справа
        body_layout = QHBoxLayout()
        body_layout.setSpacing(0)

        self.settings_panel = QFrame()
        self.settings_panel.setObjectName("SettingsPanel")
        self.settings_panel.setMinimumWidth(0)
        self.settings_panel.setMaximumWidth(0)
        self.settings_panel.setStyleSheet("""
            QFrame#SettingsPanel {
                background-color: #202020;
                border-radius: 10px;
                border: 1px solid #2e2e2e;
            }
        """)

        self.settings_inner = QWidget(self.settings_panel)
        self.settings_inner.setFixedWidth(self.SETTINGS_WIDTH)
        self.settings_inner.setStyleSheet("background: transparent; border: none;")
        self._build_settings_ui()
        body_layout.addWidget(self.settings_panel)

        self.panel_gap = QWidget()
        self.panel_gap.setFixedWidth(0)
        self.panel_gap.setStyleSheet("background: transparent;")
        body_layout.addWidget(self.panel_gap)

        right_widget = QWidget()
        right_widget.setMinimumWidth(200)
        right_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        right_widget.setStyleSheet("background: transparent;")
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.list_container = QWidget()
        self.list_container.setStyleSheet("background-color: #181818;")
        self.list_layout = QVBoxLayout(self.list_container)
        self.list_layout.setContentsMargins(8, 8, 8, 8)
        self.list_layout.setSpacing(8)
        self.list_layout.addStretch(1)

        self.scroll_area.setWidget(self.list_container)
        right_layout.addWidget(self.scroll_area, 1)

        self.dep_bar = QFrame()
        self.dep_bar.setStyleSheet("background-color: #202020; border-radius: 8px; border: none;")
        dep_layout = QHBoxLayout(self.dep_bar)
        dep_layout.setContentsMargins(12, 6, 12, 6)

        self.dep_label = QLabel("Проверка компонентов (yt-dlp и ffmpeg)...")
        self.dep_label.setStyleSheet("background: transparent; color: #aaaaaa; font-size: 11px;")
        dep_layout.addWidget(self.dep_label, 1)

        self.dep_progress = QProgressBar()
        self.dep_progress.setRange(0, 1000)
        self.dep_progress.setFixedSize(180, 8)
        self.dep_progress.setTextVisible(False)
        self.dep_progress.setStyleSheet("""
            QProgressBar { background-color: #141414; border: none; border-radius: 4px; }
            QProgressBar::chunk { background-color: #1f538d; border-radius: 4px; }
        """)
        dep_layout.addWidget(self.dep_progress)

        right_layout.addWidget(self.dep_bar)
        body_layout.addWidget(right_widget, 1)

        root_layout.addLayout(body_layout, 1)

    def _setup_panel_animation(self):
        self.min_anim = QPropertyAnimation(self.settings_panel, b"minimumWidth")
        self.min_anim.setDuration(200)
        self.min_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        self.max_anim = QPropertyAnimation(self.settings_panel, b"maximumWidth")
        self.max_anim.setDuration(200)
        self.max_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "settings_inner") and hasattr(self, "settings_panel"):
            self.settings_inner.setFixedHeight(self.settings_panel.height())

    def _toggle_settings_panel(self):
        self.panel_open = not self.panel_open
        self.settings_inner.setFixedHeight(self.settings_panel.height())

        start_w = self.settings_panel.width()
        end_w = self.SETTINGS_WIDTH if self.panel_open else 0

        if self.panel_open:
            self.panel_gap.setFixedWidth(12)

        self.min_anim.stop()
        self.max_anim.stop()

        self.min_anim.setStartValue(start_w)
        self.min_anim.setEndValue(end_w)

        self.max_anim.setStartValue(start_w)
        self.max_anim.setEndValue(end_w)

        if not self.panel_open:
            self.max_anim.finished.connect(self._on_panel_closed)

        self.min_anim.start()
        self.max_anim.start()

    def _on_panel_closed(self):
        try:
            self.max_anim.finished.disconnect(self._on_panel_closed)
        except Exception:
            pass
        if not self.panel_open:
            self.panel_gap.setFixedWidth(0)

    def _build_settings_ui(self):
        layout = QVBoxLayout(self.settings_inner)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        hdr = QHBoxLayout()
        title = QLabel("Настройки")
        title.setStyleSheet("background: transparent; font-size: 16px; font-weight: bold;")
        hdr.addWidget(title)
        hdr.addStretch(1)

        close_btn = QPushButton("✕")
        close_btn.setFixedSize(28, 28)
        close_btn.setStyleSheet("QPushButton { background: transparent; border: none; } QPushButton:hover { background: #333; border-radius: 6px; }")
        close_btn.clicked.connect(self._toggle_settings_panel)
        hdr.addWidget(close_btn)
        layout.addLayout(hdr)

        lbl_folder = QLabel("Папка для сохранения:")
        lbl_folder.setStyleSheet("background: transparent; font-size: 12px; font-weight: bold;")
        layout.addWidget(lbl_folder)

        folder_row = QHBoxLayout()
        self.folder_edit = QLineEdit(self.save_folder)
        self.folder_edit.setReadOnly(True)
        self.folder_edit.setStyleSheet("background-color: #2c2c2c; border: 1px solid #484848; border-radius: 6px; padding: 5px; font-size: 11px;")
        folder_row.addWidget(self.folder_edit, 1)

        browse_btn = QPushButton("...")
        browse_btn.setFixedSize(36, 28)
        browse_btn.setStyleSheet("background-color: #343434; border: 1px solid #484848; border-radius: 6px; font-weight: bold;")
        browse_btn.clicked.connect(self.choose_folder)
        folder_row.addWidget(browse_btn)
        layout.addLayout(folder_row)

        lbl_q = QLabel("Качество новых загрузок:")
        lbl_q.setStyleSheet("background: transparent; font-size: 12px; font-weight: bold;")
        layout.addWidget(lbl_q)

        self.quality_combo = StyledComboBox()
        self.quality_combo.addItems(QUALITIES)
        self.quality_combo.setCurrentText(self.quality)
        self.quality_combo.currentTextChanged.connect(self._on_settings_ui_changed)
        layout.addWidget(self.quality_combo)

        lbl_lim = QLabel("Одновременных загрузок:")
        lbl_lim.setStyleSheet("background: transparent; font-size: 12px; font-weight: bold;")
        layout.addWidget(lbl_lim)

        lim_row = QHBoxLayout()
        self.limit_slider = QSlider(Qt.Orientation.Horizontal)
        self.limit_slider.setRange(1, 5)
        self.limit_slider.setSingleStep(1)
        self.limit_slider.setValue(self.limit)
        self.limit_slider.valueChanged.connect(self._on_limit_slider_changed)
        lim_row.addWidget(self.limit_slider, 1)

        self.limit_val_label = QLabel(str(self.limit))
        self.limit_val_label.setFixedWidth(24)
        self.limit_val_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.limit_val_label.setStyleSheet("background: transparent; font-size: 13px; font-weight: bold;")
        lim_row.addWidget(self.limit_val_label)
        layout.addLayout(lim_row)

        lbl_start = QLabel("При запуске программы:")
        lbl_start.setStyleSheet("background: transparent; font-size: 12px; font-weight: bold;")
        layout.addWidget(lbl_start)

        self.startup_combo = StyledComboBox()
        self.startup_combo.addItems(STARTUP_MODES)
        self.startup_combo.setCurrentText(self.startup_mode)
        self.startup_combo.currentTextChanged.connect(self._on_settings_ui_changed)
        layout.addWidget(self.startup_combo)

        layout.addStretch(1)

    def _on_limit_slider_changed(self, val: int):
        self.limit = int(val)
        self.limit_val_label.setText(str(self.limit))
        self._on_settings_ui_changed()

    def _on_settings_ui_changed(self, _=None):
        self.save_folder = self.folder_edit.text()
        self.quality = self.quality_combo.currentText()
        self.limit = int(self.limit_slider.value())
        self.startup_mode = self.startup_combo.currentText()
        save_settings_to_file(self.save_folder, self.quality, self.limit, self.startup_mode)

    def choose_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Выберите папку сохранения", self.save_folder)
        if path:
            self.folder_edit.setText(path)
            self._on_settings_ui_changed()

    def _show_input_context_menu(self, pos):
        menu = QMenu(self)
        act_paste = QAction("Вставить", self)
        act_paste.triggered.connect(self.paste_from_clipboard)
        act_clear = QAction("Очистить", self)
        act_clear.triggered.connect(self.url_input.clear)
        menu.addAction(act_paste)
        menu.addAction(act_clear)
        menu.exec(self.url_input.mapToGlobal(pos))

    def paste_from_clipboard(self):
        txt = QApplication.clipboard().text().strip()
        if txt:
            self.url_input.setText(txt)
        self.url_input.setFocus()

    def _ensure_dependencies_thread(self):
        try:
            ytdlp_path = get_tool_path("yt-dlp.exe")
            ffmpeg_path = get_tool_path("ffmpeg.exe")
            creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

            ytdlp_url = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
            if not os.path.exists(ytdlp_path):
                self._stream_download(ytdlp_url, ytdlp_path, "Скачивание yt-dlp с GitHub")
            elif os.path.dirname(os.path.abspath(ytdlp_path)) == os.path.abspath(APP_DIR):
                self.signals.dep_status.emit("Обновление yt-dlp до актуальной версии...", "#aaaaaa")
                try:
                    subprocess.run(
                        [ytdlp_path, "-U"],
                        capture_output=True, text=True, timeout=25,
                        creationflags=creationflags, encoding="utf-8", errors="ignore"
                    )
                except Exception:
                    pass

            if not os.path.exists(ffmpeg_path):
                ffmpeg_zip_url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
                zip_temp = os.path.join(APP_DIR, "ffmpeg_temp.zip")
                self._stream_download(ffmpeg_zip_url, zip_temp, "Скачивание FFmpeg с GitHub")

                self.signals.dep_status.emit("Распаковка ffmpeg.exe...", "#aaaaaa")
                with zipfile.ZipFile(zip_temp, "r") as zf:
                    for member in zf.namelist():
                        if member.endswith("bin/ffmpeg.exe"):
                            with zf.open(member) as src, open(ffmpeg_path, "wb") as dst:
                                dst.write(src.read())
                            break
                if os.path.exists(zip_temp):
                    os.remove(zip_temp)

            self.tools_ready = True
            self.signals.dep_ready.emit()
        except Exception as e:
            self.signals.dep_status.emit(f"Ошибка загрузки компонентов: {e}", "#ff5555")

    def _stream_download(self, url: str, dest: str, prefix: str):
        with requests.get(url, stream=True, timeout=30) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            done = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        f.write(chunk)
                        done += len(chunk)
                        if total > 0:
                            pct = done / total
                            mb_d = done / (1024 * 1024)
                            mb_t = total / (1024 * 1024)
                            self.signals.dep_progress.emit(
                                pct, f"{prefix}: {mb_d:.1f} / {mb_t:.1f} МБ ({pct * 100:.0f}%)"
                            )

    def _on_dep_progress(self, pct: float, text: str):
        self.dep_progress.setValue(int(round(pct * 1000)))
        self.dep_label.setText(text)

    def _on_dep_status(self, text: str, color: str):
        self.dep_label.setText(text)
        self.dep_label.setStyleSheet(f"background: transparent; color: {color}; font-size: 11px;")

    def _on_dep_ready(self):
        self.download_btn.setEnabled(True)
        self.dep_bar.setVisible(False)
        if self.startup_mode == "Начать загрузку при запуске":
            for card in self.cards:
                if card.status in ("paused", "queued", "error"):
                    card.is_paused = False
                    card.is_cancelled = False
                    card.status = "queued"
                    card._sync_ui_from_state()
                    self.enqueue_item(card)

    def _on_meta_loaded(self, item_id: str, title: str, saved_thumb: str, actual_q: str):
        for card in self.cards:
            if card.item_id == item_id:
                card.apply_metadata(title, saved_thumb, actual_q)
                break

    def _restore_cards_from_db(self):
        records = load_downloads_db()
        auto_start = (self.startup_mode == "Начать загрузку при запуске")
        for rec in records:
            if not isinstance(rec, dict) or not rec.get("url"):
                continue
            st = rec.get("status", "paused")
            if st in ("downloading", "queued"):
                rec["status"] = "queued" if auto_start else "paused"
            elif st == "paused" and auto_start:
                rec["status"] = "queued"

            card = DownloadCard(rec, self, self.list_container)
            self.list_layout.insertWidget(self.list_layout.count() - 1, card)
            self.cards.append(card)

    def add_download(self):
        if not self.tools_ready or self.closing:
            return
        url = self.url_input.text().strip()
        if not url:
            return
        self.url_input.clear()

        record = {
            "id": str(uuid.uuid4()),
            "url": url,
            "title": url,
            "thumb_path": "",
            "quality": self.quality,
            "save_folder": self.save_folder,
            "status": "queued",
            "percent": 0.0,
            "size_mb": "",
            "file_path": "",
            "actual_quality_label": "",
        }
        card = DownloadCard(record, self, self.list_container)
        self.list_layout.insertWidget(self.list_layout.count() - 1, card)
        self.cards.append(card)
        self.persist_all()
        self.enqueue_item(card)

    def enqueue_item(self, card: DownloadCard):
        if not self.closing:
            self.task_queue.put(card)

    def release_download_slot(self):
        self.active_downloads = max(0, self.active_downloads - 1)

    def _pump_queue(self):
        if self.closing or not self.tools_ready:
            return
        while self.active_downloads < self.limit and not self.task_queue.empty():
            card = self.task_queue.get()
            if card.is_cancelled or card.status != "queued" or card not in self.cards:
                continue
            self.active_downloads += 1
            card.start_qprocess()
            self.persist_all()

    def remove_card(self, card: DownloadCard):
        if card in self.cards:
            self.cards.remove(card)
        self.list_layout.removeWidget(card)
        card.deleteLater()
        self.persist_all()

    def persist_all(self):
        snapshot = [c.to_dict() for c in self.cards]
        save_downloads_db(snapshot)

    def closeEvent(self, event):
        self.closing = True
        self.queue_timer.stop()
        self._on_settings_ui_changed()

        for card in list(self.cards):
            if card.status in ("downloading", "queued") or (
                card.process and card.process.state() != QProcess.ProcessState.NotRunning
            ):
                card.stopping_for_exit = True
                card.status = "paused"
                card.is_paused = True
                card.stop_process_fast()

        self.persist_all()
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setWindowIcon(create_app_icon())
    win = MainWindow()
    win.show()
    sys.exit(app.exec())