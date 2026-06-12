# -*- coding: utf-8 -*-
from __future__ import annotations

import csv
import ctypes
import datetime as dt
import json
import math
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import traceback
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


def bootstrap_tcl_tk() -> None:
    bases: list[Path] = []
    if getattr(sys, "frozen", False):
        bases.append(Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)))
        bases.append(Path(sys.executable).resolve().parent)
    else:
        bases.append(Path(__file__).resolve().parent / "tcl_runtime_test")
        bases.append(Path(sys.executable).resolve().parent / "tcl")
        bases.append(Path(sys.executable).resolve().parent.parent / "tcl")

    for base in bases:
        tcl_dir = base / "tcl8.6"
        tk_dir = base / "tk8.6"
        nested_tcl_dir = base / "tcl" / "tcl8.6"
        nested_tk_dir = base / "tcl" / "tk8.6"
        if (tcl_dir / "init.tcl").exists() and (tk_dir / "tk.tcl").exists():
            os.environ["TCL_LIBRARY"] = str(tcl_dir)
            os.environ["TK_LIBRARY"] = str(tk_dir)
            return
        if (nested_tcl_dir / "init.tcl").exists() and (nested_tk_dir / "tk.tcl").exists():
            os.environ["TCL_LIBRARY"] = str(nested_tcl_dir)
            os.environ["TK_LIBRARY"] = str(nested_tk_dir)
            return


bootstrap_tcl_tk()

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk


APP_NAME = "TimerApp"
DB_NAME = "worklog.db"
SETTINGS_NAME = "settings.json"
ACTIVE_STATE_NAME = "active_state.json"
BACKUP_DIR_NAME = "backup"

DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"
DATE_FORMAT = "%Y-%m-%d"

WORK_MINUTE_CHOICES = [15, 30, 45, 60, 75, 90]
AUTO_END_CHOICES = [
    (30, "30초"),
    (60, "1분"),
    (120, "2분"),
    (180, "3분"),
    (300, "5분"),
]
END_TYPES = ["수동종료", "자동종료", "중도종료", "재실행자동정리"]
ALARM_STATUSES = ["정상", "기본알람사용", "알람전종료"]


def now_dt() -> dt.datetime:
    return dt.datetime.now().replace(microsecond=0)


def dt_to_str(value: Optional[dt.datetime]) -> str:
    if not value:
        return ""
    return value.strftime(DATETIME_FORMAT)


def str_to_dt(value: str | None) -> Optional[dt.datetime]:
    if not value:
        return None
    return dt.datetime.strptime(value, DATETIME_FORMAT)


def format_duration(seconds: int | float, force_hours: bool = False) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if force_hours or hours:
        return f"{hours:02}:{minutes:02}:{secs:02}"
    return f"{minutes:02}:{secs:02}"


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def app_data_dir() -> Path:
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / "AppData" / "Roaming" / APP_NAME


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def normalize_path(value: str | None) -> str:
    if not value:
        return ""
    return str(Path(value).expanduser())


@dataclass
class Employee:
    id: str
    name: str
    enabled: bool = True
    work_minutes_override: Optional[int] = None
    alarm_music_path: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "Employee":
        override = data.get("work_minutes_override")
        if override not in WORK_MINUTE_CHOICES:
            override = None
        return cls(
            id=str(data.get("id") or f"emp_{uuid.uuid4().hex[:8]}"),
            name=str(data.get("name") or "이름 없음"),
            enabled=bool(data.get("enabled", True)),
            work_minutes_override=override,
            alarm_music_path=normalize_path(data.get("alarm_music_path")),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "work_minutes_override": self.work_minutes_override,
            "alarm_music_path": self.alarm_music_path,
        }

    def work_minutes(self, default_minutes: int) -> int:
        return self.work_minutes_override or default_minutes


@dataclass
class ActiveWork:
    employee_id: str
    employee_name_snapshot: str
    start_at: dt.datetime
    due_at: dt.datetime
    work_minutes: int
    memo: str = ""
    alarm_music_path_snapshot: str = ""
    used_default_alarm: bool = False
    alarm_started_at: Optional[dt.datetime] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ActiveWork":
        return cls(
            employee_id=row["employee_id"],
            employee_name_snapshot=row["employee_name_snapshot"],
            start_at=str_to_dt(row["start_at"]) or now_dt(),
            due_at=str_to_dt(row["due_at"]) or now_dt(),
            work_minutes=int(row["work_minutes"]),
            memo=row["memo"] or "",
            alarm_music_path_snapshot=row["alarm_music_path_snapshot"] or "",
            used_default_alarm=bool(row["used_default_alarm"]),
            alarm_started_at=str_to_dt(row["alarm_started_at"]),
        )

    @classmethod
    def from_dict(cls, data: dict) -> "ActiveWork":
        return cls(
            employee_id=str(data["employee_id"]),
            employee_name_snapshot=str(data.get("employee_name_snapshot") or ""),
            start_at=str_to_dt(data.get("start_at")) or now_dt(),
            due_at=str_to_dt(data.get("due_at")) or now_dt(),
            work_minutes=int(data.get("work_minutes") or 60),
            memo=str(data.get("memo") or ""),
            alarm_music_path_snapshot=normalize_path(data.get("alarm_music_path_snapshot")),
            used_default_alarm=bool(data.get("used_default_alarm", False)),
            alarm_started_at=str_to_dt(data.get("alarm_started_at")),
        )

    def to_dict(self) -> dict:
        return {
            "employee_id": self.employee_id,
            "employee_name_snapshot": self.employee_name_snapshot,
            "start_at": dt_to_str(self.start_at),
            "due_at": dt_to_str(self.due_at),
            "work_minutes": self.work_minutes,
            "memo": self.memo,
            "alarm_music_path_snapshot": self.alarm_music_path_snapshot,
            "used_default_alarm": self.used_default_alarm,
            "alarm_started_at": dt_to_str(self.alarm_started_at),
        }


class SettingsStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict:
        defaults = {
            "background_music_path": "",
            "background_autoplay": False,
            "default_work_minutes": 60,
            "auto_end_wait_seconds": 60,
            "confirm_wait_seconds": 30,
            "employees": [],
        }
        if not self.path.exists():
            return defaults
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return defaults
        merged = defaults | raw
        merged["background_music_path"] = normalize_path(merged.get("background_music_path"))
        merged["background_autoplay"] = bool(merged.get("background_autoplay", False))
        if merged.get("default_work_minutes") not in WORK_MINUTE_CHOICES:
            merged["default_work_minutes"] = 60
        valid_waits = [seconds for seconds, _ in AUTO_END_CHOICES]
        if merged.get("auto_end_wait_seconds") not in valid_waits:
            merged["auto_end_wait_seconds"] = 60
        try:
            confirm_wait = int(merged.get("confirm_wait_seconds", 30))
        except (TypeError, ValueError):
            confirm_wait = 30
        merged["confirm_wait_seconds"] = max(5, confirm_wait)
        employees = []
        seen_ids = set()
        for item in merged.get("employees", []):
            emp = Employee.from_dict(item)
            if emp.id in seen_ids:
                emp.id = f"emp_{uuid.uuid4().hex[:8]}"
            seen_ids.add(emp.id)
            employees.append(emp.to_dict())
        merged["employees"] = employees
        return merged

    def save(self, settings: dict) -> None:
        serializable = dict(settings)
        serializable["employees"] = [
            emp.to_dict() if isinstance(emp, Employee) else emp
            for emp in settings.get("employees", [])
        ]
        atomic_write_text(self.path, json.dumps(serializable, ensure_ascii=False, indent=2))


class WorkDatabase:
    def __init__(self, db_path: Path):
        self.path = db_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=15)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS active_work (
                employee_id TEXT PRIMARY KEY,
                employee_name_snapshot TEXT NOT NULL,
                start_at TEXT NOT NULL,
                due_at TEXT NOT NULL,
                work_minutes INTEGER NOT NULL,
                memo TEXT,
                alarm_music_path_snapshot TEXT,
                used_default_alarm INTEGER NOT NULL DEFAULT 0,
                alarm_started_at TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS work_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id TEXT NOT NULL,
                employee_name_snapshot TEXT NOT NULL,
                start_at TEXT NOT NULL,
                due_at TEXT,
                ended_at TEXT NOT NULL,
                pay_seconds INTEGER NOT NULL,
                end_type TEXT NOT NULL,
                memo TEXT,
                alarm_status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_work_records_start_at ON work_records(start_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_work_records_employee ON work_records(employee_id)")
        self.conn.commit()

    def upsert_active(self, work: ActiveWork) -> None:
        self.conn.execute(
            """
            INSERT INTO active_work (
                employee_id, employee_name_snapshot, start_at, due_at, work_minutes,
                memo, alarm_music_path_snapshot, used_default_alarm, alarm_started_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(employee_id) DO UPDATE SET
                employee_name_snapshot=excluded.employee_name_snapshot,
                start_at=excluded.start_at,
                due_at=excluded.due_at,
                work_minutes=excluded.work_minutes,
                memo=excluded.memo,
                alarm_music_path_snapshot=excluded.alarm_music_path_snapshot,
                used_default_alarm=excluded.used_default_alarm,
                alarm_started_at=excluded.alarm_started_at
            """,
            (
                work.employee_id,
                work.employee_name_snapshot,
                dt_to_str(work.start_at),
                dt_to_str(work.due_at),
                work.work_minutes,
                work.memo,
                work.alarm_music_path_snapshot,
                int(work.used_default_alarm),
                dt_to_str(work.alarm_started_at),
            ),
        )
        self.conn.commit()

    def delete_active(self, employee_id: str) -> None:
        self.conn.execute("DELETE FROM active_work WHERE employee_id = ?", (employee_id,))
        self.conn.commit()

    def load_active(self) -> list[ActiveWork]:
        rows = self.conn.execute("SELECT * FROM active_work ORDER BY due_at, start_at").fetchall()
        return [ActiveWork.from_row(row) for row in rows]

    def clear_active(self) -> None:
        self.conn.execute("DELETE FROM active_work")
        self.conn.commit()

    def insert_record(
        self,
        work: ActiveWork,
        ended_at: dt.datetime,
        pay_seconds: int,
        end_type: str,
        alarm_status: str,
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO work_records (
                employee_id, employee_name_snapshot, start_at, due_at, ended_at,
                pay_seconds, end_type, memo, alarm_status, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                work.employee_id,
                work.employee_name_snapshot,
                dt_to_str(work.start_at),
                dt_to_str(work.due_at),
                dt_to_str(ended_at),
                int(max(0, pay_seconds)),
                end_type,
                work.memo,
                alarm_status,
                dt_to_str(now_dt()),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def query_records(
        self,
        employee_ids: Optional[list[str]] = None,
        start_date: Optional[dt.date] = None,
        end_date: Optional[dt.date] = None,
        end_type: Optional[str] = None,
        include_mid: bool = True,
        include_auto: bool = True,
        include_alarm_error: bool = True,
        limit: Optional[int] = None,
    ) -> list[sqlite3.Row]:
        where = []
        params: list[object] = []
        if employee_ids:
            placeholders = ",".join("?" for _ in employee_ids)
            where.append(f"employee_id IN ({placeholders})")
            params.extend(employee_ids)
        if start_date:
            where.append("start_at >= ?")
            params.append(dt.datetime.combine(start_date, dt.time.min).strftime(DATETIME_FORMAT))
        if end_date:
            where.append("start_at <= ?")
            params.append(dt.datetime.combine(end_date, dt.time.max).replace(microsecond=0).strftime(DATETIME_FORMAT))
        if end_type:
            where.append("end_type = ?")
            params.append(end_type)
        if not include_mid:
            where.append("end_type <> ?")
            params.append("중도종료")
        if not include_auto:
            where.append("end_type <> ?")
            params.append("자동종료")
        if not include_alarm_error:
            where.append("alarm_status <> ?")
            params.append("기본알람사용")
        sql = "SELECT * FROM work_records"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY start_at DESC, id DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return self.conn.execute(sql, params).fetchall()

    def backup_to(self, target_path: Path) -> None:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        backup_conn = sqlite3.connect(str(target_path))
        try:
            self.conn.backup(backup_conn)
        finally:
            backup_conn.close()

    def close(self) -> None:
        self.conn.close()


class BackupManager:
    def __init__(self, root_dir: Path, database: WorkDatabase):
        self.root_dir = root_dir
        self.database = database
        self.backup_dir = root_dir / BACKUP_DIR_NAME
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def cleanup_old_backups(self) -> int:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        cutoff = dt.datetime.now() - dt.timedelta(days=7)
        removed = 0
        for path in self.backup_dir.glob("worklog_backup_*.db"):
            try:
                modified = dt.datetime.fromtimestamp(path.stat().st_mtime)
                if modified < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed

    def create_auto_backup_if_needed(self, today: Optional[dt.date] = None) -> tuple[bool, str]:
        self.cleanup_old_backups()
        today = today or dt.date.today()
        target = self.backup_dir / f"worklog_backup_{today.strftime(DATE_FORMAT)}.db"
        if target.exists():
            return False, f"오늘 자동 백업이 이미 있습니다: {target.name}"
        self.database.backup_to(target)
        return True, f"자동 백업 완료: {target.name}"

    def create_manual_backup(self) -> Path:
        self.cleanup_old_backups()
        stamp = now_dt().strftime("%Y-%m-%d_%H%M")
        target = self.backup_dir / f"worklog_backup_{stamp}.db"
        if target.exists():
            target = self.backup_dir / f"worklog_backup_{now_dt().strftime('%Y-%m-%d_%H%M%S')}.db"
        self.database.backup_to(target)
        return target

    def latest_backup(self) -> Optional[Path]:
        backups = list(self.backup_dir.glob("worklog_backup_*.db"))
        if not backups:
            return None
        return max(backups, key=lambda path: path.stat().st_mtime)


class MciPlayer:
    def __init__(self):
        self.alias = "timerapp_audio"
        self.current_path = ""
        self.process: Optional[subprocess.Popen] = None
        self.backend = ""
        self._mci = ctypes.windll.winmm.mciSendStringW

    def _send(self, command: str) -> None:
        buffer = ctypes.create_unicode_buffer(512)
        error = self._mci(command, buffer, 511, None)
        if error:
            error_buffer = ctypes.create_unicode_buffer(512)
            ctypes.windll.winmm.mciGetErrorStringW(error, error_buffer, 511)
            raise RuntimeError(error_buffer.value or f"MCI error {error}")

    def play_loop(self, path: Path) -> tuple[bool, str]:
        self.stop()
        if not path.exists():
            return False, "파일을 찾을 수 없습니다."
        ok, error = self._play_wpf_loop(path)
        if ok:
            return True, ""
        ok, mci_error = self._play_mci_loop(path)
        if ok:
            return True, ""
        return False, error or mci_error

    def _play_wpf_loop(self, path: Path) -> tuple[bool, str]:
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if not powershell.exists():
            return False, "PowerShell을 찾을 수 없습니다."
        script = (
            "$ErrorActionPreference = 'Stop'; "
            "Add-Type -AssemblyName PresentationCore; "
            "$player = New-Object System.Windows.Media.MediaPlayer; "
            "$player.Open([Uri]::new($env:TIMERAPP_AUDIO_PATH)); "
            "Start-Sleep -Milliseconds 300; "
            "$player.Play(); "
            "$parentPid = 0; "
            "[int]::TryParse($env:TIMERAPP_PARENT_PID, [ref]$parentPid) | Out-Null; "
            "try { "
            "  while ($true) { "
            "    if ($parentPid -gt 0 -and -not (Get-Process -Id $parentPid -ErrorAction SilentlyContinue)) { break } "
            "    Start-Sleep -Milliseconds 200; "
            "    if ($player.NaturalDuration.HasTimeSpan) { "
            "      $duration = $player.NaturalDuration.TimeSpan; "
            "      if ($duration.TotalMilliseconds -gt 0 -and $player.Position.TotalMilliseconds -ge ($duration.TotalMilliseconds - 250)) { "
            "        $player.Position = [TimeSpan]::Zero; "
            "        $player.Play(); "
            "      } "
            "    } "
            "  } "
            "} finally { "
            "  $player.Stop(); "
            "  $player.Close(); "
            "}"
        )
        env = os.environ.copy()
        env["TIMERAPP_AUDIO_PATH"] = str(path)
        env["TIMERAPP_PARENT_PID"] = str(os.getpid())
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        try:
            process = subprocess.Popen(
                [str(powershell), "-STA", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                creationflags=creationflags,
            )
        except Exception as exc:
            return False, str(exc)
        try:
            process.wait(timeout=0.8)
        except subprocess.TimeoutExpired:
            self.process = process
            self.current_path = str(path)
            self.backend = "wpf"
            return True, ""
        stderr = ""
        try:
            stderr = process.stderr.read() if process.stderr else ""
        except Exception:
            stderr = ""
        return False, stderr.strip() or "WPF MediaPlayer를 초기화하지 못했습니다."

    def _play_mci_loop(self, path: Path) -> tuple[bool, str]:
        self._close_mci()
        try:
            safe_path = str(path)
            self._send(f'open "{safe_path}" alias {self.alias}')
            self._send(f"play {self.alias} repeat")
            self.current_path = safe_path
            self.backend = "mci"
            return True, ""
        except Exception as exc:
            self._close_mci()
            self.current_path = ""
            self.backend = ""
            return False, str(exc)

    def stop(self) -> None:
        if self.process:
            process = self.process
            self.process = None
            try:
                process.terminate()
                process.wait(timeout=2)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
            try:
                if process.stderr:
                    process.stderr.close()
            except Exception:
                pass
        self._close_mci()
        self.current_path = ""
        self.backend = ""

    def _close_mci(self) -> None:
        try:
            self._send(f"stop {self.alias}")
        except Exception:
            pass
        try:
            self._send(f"close {self.alias}")
        except Exception:
            pass


def ensure_default_alarm_file() -> Path:
    path = Path(tempfile.gettempdir()) / "timerapp_default_alarm.wav"
    if path.exists() and path.stat().st_size > 0:
        return path
    sample_rate = 44100
    duration = 1.6
    amplitude = 16000
    frames = []
    for index in range(int(sample_rate * duration)):
        t = index / sample_rate
        cycle = t % 0.8
        if cycle < 0.28:
            freq = 880
            sample = int(amplitude * math.sin(2 * math.pi * freq * t))
        elif cycle < 0.40:
            sample = 0
        elif cycle < 0.66:
            freq = 660
            sample = int(amplitude * math.sin(2 * math.pi * freq * t))
        else:
            sample = 0
        frames.append(struct.pack("<h", sample))
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"".join(frames))
    return path


class SpeechEngine:
    def __init__(
        self,
        root: tk.Tk,
        on_started: Callable[[], None],
        on_idle: Callable[[], None],
        on_error: Callable[[str], None],
    ):
        self.root = root
        self.on_started = on_started
        self.on_idle = on_idle
        self.on_error = on_error
        self.queue: list[tuple[str, Optional[Callable[[], None]]]] = []
        self.active = False

    def speak(self, text: str, after: Optional[Callable[[], None]] = None) -> None:
        text = text.strip()
        if not text:
            if after:
                self.root.after(0, after)
            return
        self.queue.append((text, after))
        if not self.active:
            self._start_next()

    def _start_next(self) -> None:
        if not self.queue:
            self.active = False
            self.on_idle()
            return
        text, after = self.queue.pop(0)
        self.active = True
        self.on_started()
        thread = threading.Thread(target=self._run_tts, args=(text, after), daemon=True)
        thread.start()

    def _run_tts(self, text: str, after: Optional[Callable[[], None]]) -> None:
        error = ""
        try:
            powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            command = (
                "Add-Type -AssemblyName System.Speech; "
                "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                "$s.SetOutputToDefaultAudioDevice(); "
                "$s.Speak($env:TIMERAPP_TTS_TEXT)"
            )
            env = os.environ.copy()
            env["TIMERAPP_TTS_TEXT"] = text
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            result = subprocess.run(
                [str(powershell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=creationflags,
                timeout=120,
            )
            if result.returncode != 0:
                error = result.stderr.strip() or "TTS 실행에 실패했습니다."
        except Exception as exc:
            error = str(exc)
        self.root.after(0, lambda: self._complete(after, error))

    def _complete(self, after: Optional[Callable[[], None]], error: str) -> None:
        self.active = False
        if error:
            self.on_error(error)
        if after:
            after()
        elif not self.queue:
            self.on_idle()
        if self.queue:
            self._start_next()


class ScrollableFrame(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, borderwidth=0, highlightthickness=0)
        self.vscroll = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self.inner.bind("<Configure>", self._on_inner_configure)
        self.window_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.vscroll.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vscroll.grid(row=0, column=1, sticky="ns")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

    def _on_inner_configure(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event) -> None:
        self.canvas.itemconfigure(self.window_id, width=event.width)


class EmployeeDialog(tk.Toplevel):
    def __init__(self, master: tk.Tk, employee: Optional[Employee], default_minutes: int):
        super().__init__(master)
        self.title("직원 수정" if employee else "직원 추가")
        self.resizable(False, False)
        self.result: Optional[Employee] = None
        self.employee = employee

        self.name_var = tk.StringVar(value=employee.name if employee else "")
        self.enabled_var = tk.BooleanVar(value=employee.enabled if employee else True)
        override_label = "기본값 사용"
        if employee and employee.work_minutes_override:
            override_label = f"{employee.work_minutes_override}분"
        self.override_var = tk.StringVar(value=override_label)
        self.alarm_path_var = tk.StringVar(value=employee.alarm_music_path if employee else "")

        body = ttk.Frame(self, padding=14)
        body.grid(row=0, column=0, sticky="nsew")

        ttk.Label(body, text="직원명").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(body, textvariable=self.name_var, width=36).grid(row=0, column=1, sticky="ew", pady=4)

        ttk.Label(body, text="상태").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Checkbutton(body, text="활성화", variable=self.enabled_var).grid(row=1, column=1, sticky="w", pady=4)

        ttk.Label(body, text="업무 시간").grid(row=2, column=0, sticky="w", pady=4)
        values = ["기본값 사용"] + [f"{minutes}분" for minutes in WORK_MINUTE_CHOICES]
        ttk.Combobox(body, textvariable=self.override_var, values=values, state="readonly", width=16).grid(
            row=2, column=1, sticky="w", pady=4
        )

        ttk.Label(body, text="알람 음악").grid(row=3, column=0, sticky="w", pady=4)
        path_frame = ttk.Frame(body)
        path_frame.grid(row=3, column=1, sticky="ew", pady=4)
        path_frame.columnconfigure(0, weight=1)
        ttk.Entry(path_frame, textvariable=self.alarm_path_var, width=46).grid(row=0, column=0, sticky="ew")
        ttk.Button(path_frame, text="선택", command=self.choose_alarm).grid(row=0, column=1, padx=(6, 0))
        ttk.Button(path_frame, text="비우기", command=lambda: self.alarm_path_var.set("")).grid(row=0, column=2, padx=(4, 0))

        button_frame = ttk.Frame(body)
        button_frame.grid(row=4, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(button_frame, text="확인", command=self.on_ok).grid(row=0, column=0, padx=4)
        ttk.Button(button_frame, text="취소", command=self.destroy).grid(row=0, column=1, padx=4)

        self.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        self.transient(master)
        self.grab_set()
        self.wait_visibility()
        self.focus()

    def choose_alarm(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            title="알람 음악 선택",
            filetypes=[
                ("음악 파일", "*.mp3 *.wav *.wma *.m4a *.aac"),
                ("모든 파일", "*.*"),
            ],
        )
        if path:
            self.alarm_path_var.set(path)

    def on_ok(self) -> None:
        name = self.name_var.get().strip()
        if not name:
            messagebox.showwarning("확인", "직원명을 입력해 주세요.", parent=self)
            return
        override_text = self.override_var.get()
        override = None
        if override_text != "기본값 사용":
            override = int(override_text.replace("분", ""))
        employee_id = self.employee.id if self.employee else f"emp_{uuid.uuid4().hex[:10]}"
        self.result = Employee(
            id=employee_id,
            name=name,
            enabled=self.enabled_var.get(),
            work_minutes_override=override,
            alarm_music_path=normalize_path(self.alarm_path_var.get()),
        )
        self.destroy()


class ExportDialog(tk.Toplevel):
    def __init__(
        self,
        master: tk.Tk,
        database: WorkDatabase,
        employees: list[Employee],
        output_dir: Path,
        on_message: Callable[[str], None],
    ):
        super().__init__(master)
        self.title("기록 내보내기")
        self.geometry("520x620")
        self.database = database
        self.employees = employees
        self.output_dir = output_dir
        self.on_message = on_message

        today = dt.date.today()
        first_day = today.replace(day=1)
        self.all_var = tk.BooleanVar(value=True)
        self.employee_vars = {emp.id: tk.BooleanVar(value=False) for emp in employees}
        self.start_var = tk.StringVar(value=first_day.strftime(DATE_FORMAT))
        self.end_var = tk.StringVar(value=today.strftime(DATE_FORMAT))
        self.end_type_var = tk.StringVar(value="전체")
        self.include_memo_var = tk.BooleanVar(value=True)
        self.include_mid_var = tk.BooleanVar(value=True)
        self.include_auto_var = tk.BooleanVar(value=True)
        self.include_alarm_error_var = tk.BooleanVar(value=True)

        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)

        employee_frame = ttk.LabelFrame(body, text="직원")
        employee_frame.pack(fill="x", pady=(0, 10))
        ttk.Checkbutton(employee_frame, text="전체", variable=self.all_var, command=self._toggle_all).pack(anchor="w", padx=10, pady=4)
        for emp in employees:
            ttk.Checkbutton(employee_frame, text=emp.name, variable=self.employee_vars[emp.id], command=self._toggle_employee).pack(
                anchor="w", padx=24, pady=2
            )

        period_frame = ttk.LabelFrame(body, text="기간")
        period_frame.pack(fill="x", pady=(0, 10))
        row = ttk.Frame(period_frame)
        row.pack(fill="x", padx=10, pady=8)
        ttk.Label(row, text="시작일").grid(row=0, column=0, sticky="w")
        ttk.Entry(row, textvariable=self.start_var, width=14).grid(row=0, column=1, padx=(6, 18))
        ttk.Label(row, text="종료일").grid(row=0, column=2, sticky="w")
        ttk.Entry(row, textvariable=self.end_var, width=14).grid(row=0, column=3, padx=(6, 0))

        filter_frame = ttk.LabelFrame(body, text="필터")
        filter_frame.pack(fill="x", pady=(0, 10))
        filter_row = ttk.Frame(filter_frame)
        filter_row.pack(fill="x", padx=10, pady=8)
        ttk.Label(filter_row, text="종료 방식").grid(row=0, column=0, sticky="w")
        ttk.Combobox(
            filter_row,
            textvariable=self.end_type_var,
            values=["전체"] + END_TYPES,
            state="readonly",
            width=18,
        ).grid(row=0, column=1, padx=(8, 0), sticky="w")

        option_frame = ttk.LabelFrame(body, text="옵션")
        option_frame.pack(fill="x", pady=(0, 10))
        ttk.Checkbutton(option_frame, text="메모 포함", variable=self.include_memo_var).pack(anchor="w", padx=10, pady=3)
        ttk.Checkbutton(option_frame, text="중도종료 기록 포함", variable=self.include_mid_var).pack(anchor="w", padx=10, pady=3)
        ttk.Checkbutton(option_frame, text="자동종료 기록 포함", variable=self.include_auto_var).pack(anchor="w", padx=10, pady=3)
        ttk.Checkbutton(option_frame, text="알람음 오류 기록 포함", variable=self.include_alarm_error_var).pack(anchor="w", padx=10, pady=3)

        button_frame = ttk.Frame(body)
        button_frame.pack(fill="x", pady=(10, 0))
        ttk.Button(button_frame, text="CSV 내보내기", command=self.export_csv).pack(side="right", padx=4)
        ttk.Button(button_frame, text="닫기", command=self.destroy).pack(side="right", padx=4)

        self.transient(master)
        self.grab_set()

    def _toggle_all(self) -> None:
        if self.all_var.get():
            for var in self.employee_vars.values():
                var.set(False)

    def _toggle_employee(self) -> None:
        if any(var.get() for var in self.employee_vars.values()):
            self.all_var.set(False)
        else:
            self.all_var.set(True)

    def export_csv(self) -> None:
        try:
            start_date = dt.datetime.strptime(self.start_var.get().strip(), DATE_FORMAT).date()
            end_date = dt.datetime.strptime(self.end_var.get().strip(), DATE_FORMAT).date()
        except ValueError:
            messagebox.showwarning("확인", "날짜는 YYYY-MM-DD 형식으로 입력해 주세요.", parent=self)
            return
        if start_date > end_date:
            messagebox.showwarning("확인", "시작일은 종료일보다 늦을 수 없습니다.", parent=self)
            return

        employee_ids = None
        if not self.all_var.get():
            employee_ids = [emp_id for emp_id, var in self.employee_vars.items() if var.get()]
            if not employee_ids:
                messagebox.showwarning("확인", "직원을 하나 이상 선택해 주세요.", parent=self)
                return

        end_type = None if self.end_type_var.get() == "전체" else self.end_type_var.get()
        rows = self.database.query_records(
            employee_ids=employee_ids,
            start_date=start_date,
            end_date=end_date,
            end_type=end_type,
            include_mid=self.include_mid_var.get(),
            include_auto=self.include_auto_var.get(),
            include_alarm_error=self.include_alarm_error_var.get(),
        )

        filename = f"worklog_export_{now_dt().strftime('%Y-%m-%d_%H%M%S')}.csv"
        target = filedialog.asksaveasfilename(
            parent=self,
            title="CSV 저장",
            initialdir=str(self.output_dir),
            initialfile=filename,
            defaultextension=".csv",
            filetypes=[("CSV 파일", "*.csv"), ("모든 파일", "*.*")],
        )
        if not target:
            return

        columns = ["직원명", "시작시각", "타이머만료시각", "종료처리시각", "급여계산시간", "종료방식"]
        if self.include_memo_var.get():
            columns.append("메모")
        columns.append("알람음상태")
        with open(target, "w", newline="", encoding="utf-8-sig") as fp:
            writer = csv.writer(fp)
            writer.writerow(columns)
            for row in rows:
                item = [
                    row["employee_name_snapshot"],
                    row["start_at"],
                    row["due_at"] or "",
                    row["ended_at"],
                    format_duration(row["pay_seconds"], force_hours=True),
                    row["end_type"],
                ]
                if self.include_memo_var.get():
                    item.append(row["memo"] or "")
                item.append(row["alarm_status"])
                writer.writerow(item)
        self.on_message(f"CSV 내보내기 완료: {Path(target).name} ({len(rows)}건)")
        messagebox.showinfo("완료", f"CSV 내보내기를 완료했습니다.\n{target}", parent=self)


class TimerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("직원 업무 타이머")
        self.root.geometry("1220x780")
        self.root.minsize(980, 620)

        self.root_dir = app_root()
        self.data_dir = app_data_dir()
        self.settings_store = SettingsStore(self.data_dir / SETTINGS_NAME)
        self.settings = self.settings_store.load()
        self.employees: list[Employee] = [Employee.from_dict(item) for item in self.settings.get("employees", [])]
        self.employee_by_id: dict[str, Employee] = {emp.id: emp for emp in self.employees}

        self.database = WorkDatabase(self.root_dir / DB_NAME)
        self.backup_manager = BackupManager(self.root_dir, self.database)
        self.default_alarm_path = ensure_default_alarm_file()

        self.active_works: dict[str, ActiveWork] = {}
        self.alarm_queue: list[str] = []
        self.current_alarm_emp_id: Optional[str] = None
        self.audio_role: Optional[str] = None
        self.background_desired = bool(self.settings.get("background_autoplay", False))
        self.last_backup_date = dt.date.today()

        self.reset_confirm_until: dict[str, dt.datetime] = {}
        self.end_confirm_until: dict[str, dt.datetime] = {}

        self.player = MciPlayer()
        self.speech = SpeechEngine(
            root=self.root,
            on_started=self.on_tts_started,
            on_idle=self.resume_audio_policy,
            on_error=self.on_tts_error,
        )

        self.audio_status_var = tk.StringVar(value="현재 재생: 무음 대기")
        self.auto_end_var = tk.StringVar(value="자동 종료까지: -")
        self.queue_var = tk.StringVar(value="대기열: 없음")
        self.message_var = tk.StringVar(value="")
        self.backup_status_var = tk.StringVar(value="")
        self.recent_backup_var = tk.StringVar(value="최근 백업: -")

        self.bg_path_var = tk.StringVar(value=self.settings.get("background_music_path", ""))
        self.bg_autoplay_var = tk.BooleanVar(value=self.settings.get("background_autoplay", False))
        self.default_minutes_var = tk.StringVar(value=f"{self.settings.get('default_work_minutes', 60)}분")
        self.auto_end_wait_var = tk.StringVar(value=self.auto_end_label(self.settings.get("auto_end_wait_seconds", 60)))

        self.employee_tree: Optional[ttk.Treeview] = None
        self.record_tree: Optional[ttk.Treeview] = None

        self.configure_style()
        self.build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.startup_backup()
        self.recover_active_state()
        self.refresh_all()
        self.resume_audio_policy()
        self.root.after(500, self.tick)

    def configure_style(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Top.TLabel", font=("맑은 고딕", 16, "bold"))
        style.configure("Status.TLabel", font=("맑은 고딕", 11))
        style.configure("Header.TLabel", font=("맑은 고딕", 10, "bold"))
        style.configure("Row.TLabel", font=("맑은 고딕", 10))
        style.configure("Small.TButton", padding=(5, 3))

    def build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        top = ttk.Frame(outer)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=1)
        ttk.Label(top, textvariable=self.audio_status_var, style="Top.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(top, textvariable=self.auto_end_var, style="Status.TLabel").grid(row=0, column=1, sticky="e", padx=(12, 0))
        ttk.Label(top, textvariable=self.queue_var, style="Status.TLabel").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Label(top, textvariable=self.message_var, style="Status.TLabel").grid(row=1, column=1, sticky="e", pady=(4, 0))

        toolbar = ttk.Frame(outer)
        toolbar.grid(row=1, column=0, sticky="ew", pady=(10, 8))
        ttk.Button(toolbar, text="직원 추가", command=self.add_employee).pack(side="left", padx=(0, 6))
        ttk.Button(toolbar, text="배경음악 시작", command=self.start_background).pack(side="left", padx=3)
        ttk.Button(toolbar, text="배경음악 정지", command=self.stop_background).pack(side="left", padx=3)
        ttk.Button(toolbar, text="CSV 내보내기", command=self.open_export_dialog).pack(side="left", padx=(14, 3))
        ttk.Button(toolbar, text="지금 백업", command=self.manual_backup).pack(side="left", padx=3)

        self.notebook = ttk.Notebook(outer)
        self.notebook.grid(row=2, column=0, sticky="nsew")
        self.board_tab = ttk.Frame(self.notebook, padding=8)
        self.settings_tab = ttk.Frame(self.notebook, padding=8)
        self.records_tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(self.board_tab, text="상태판")
        self.notebook.add(self.settings_tab, text="설정")
        self.notebook.add(self.records_tab, text="기록")

        self.build_board_tab()
        self.build_settings_tab()
        self.build_records_tab()

    def build_board_tab(self) -> None:
        self.board_tab.columnconfigure(0, weight=1)
        self.board_tab.rowconfigure(1, weight=1)
        header = ttk.Frame(self.board_tab)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        widths = [18, 18, 22, 22, 48]
        labels = ["직원명", "상태", "남은 시간 / 순서", "메모", "조작"]
        for col, (label, width) in enumerate(zip(labels, widths)):
            header.columnconfigure(col, minsize=width * 8, weight=0 if col < 4 else 1)
            ttk.Label(header, text=label, style="Header.TLabel").grid(row=0, column=col, sticky="w", padx=6)
        self.board_scroll = ScrollableFrame(self.board_tab)
        self.board_scroll.grid(row=1, column=0, sticky="nsew")

    def build_settings_tab(self) -> None:
        self.settings_tab.columnconfigure(0, weight=1)
        self.settings_tab.rowconfigure(2, weight=1)

        bg_frame = ttk.LabelFrame(self.settings_tab, text="배경음악")
        bg_frame.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        bg_frame.columnconfigure(1, weight=1)
        ttk.Label(bg_frame, text="파일").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        ttk.Entry(bg_frame, textvariable=self.bg_path_var).grid(row=0, column=1, sticky="ew", padx=4, pady=6)
        ttk.Button(bg_frame, text="선택", command=self.choose_background).grid(row=0, column=2, padx=4, pady=6)
        ttk.Button(bg_frame, text="비우기", command=self.clear_background).grid(row=0, column=3, padx=4, pady=6)
        ttk.Checkbutton(bg_frame, text="앱 실행 시 자동 재생", variable=self.bg_autoplay_var, command=self.save_global_settings).grid(
            row=1, column=1, sticky="w", padx=4, pady=(0, 8)
        )

        global_frame = ttk.LabelFrame(self.settings_tab, text="공통 설정")
        global_frame.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(global_frame, text="기본 업무 시간").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        ttk.Combobox(
            global_frame,
            textvariable=self.default_minutes_var,
            values=[f"{minutes}분" for minutes in WORK_MINUTE_CHOICES],
            state="readonly",
            width=10,
        ).grid(row=0, column=1, sticky="w", padx=4, pady=6)
        ttk.Label(global_frame, text="자동 종료 대기시간").grid(row=0, column=2, sticky="w", padx=(20, 4), pady=6)
        ttk.Combobox(
            global_frame,
            textvariable=self.auto_end_wait_var,
            values=[label for _, label in AUTO_END_CHOICES],
            state="readonly",
            width=10,
        ).grid(row=0, column=3, sticky="w", padx=4, pady=6)
        ttk.Button(global_frame, text="설정 저장", command=self.save_global_settings).grid(row=0, column=4, padx=12, pady=6)

        emp_frame = ttk.LabelFrame(self.settings_tab, text="직원 관리")
        emp_frame.grid(row=2, column=0, sticky="nsew", pady=(0, 8))
        emp_frame.columnconfigure(0, weight=1)
        emp_frame.rowconfigure(0, weight=1)
        columns = ("name", "enabled", "work_minutes", "alarm")
        self.employee_tree = ttk.Treeview(emp_frame, columns=columns, show="headings", height=8)
        for col, label, width in [
            ("name", "직원명", 160),
            ("enabled", "상태", 80),
            ("work_minutes", "업무 시간", 110),
            ("alarm", "알람 음악", 520),
        ]:
            self.employee_tree.heading(col, text=label)
            self.employee_tree.column(col, width=width, anchor="w")
        self.employee_tree.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        emp_scroll = ttk.Scrollbar(emp_frame, orient="vertical", command=self.employee_tree.yview)
        self.employee_tree.configure(yscrollcommand=emp_scroll.set)
        emp_scroll.grid(row=0, column=1, sticky="ns", pady=8)
        emp_buttons = ttk.Frame(emp_frame)
        emp_buttons.grid(row=1, column=0, sticky="e", padx=8, pady=(0, 8))
        ttk.Button(emp_buttons, text="추가", command=self.add_employee).pack(side="left", padx=3)
        ttk.Button(emp_buttons, text="수정", command=self.edit_selected_employee).pack(side="left", padx=3)
        ttk.Button(emp_buttons, text="활성/비활성", command=self.toggle_selected_employee).pack(side="left", padx=3)
        ttk.Button(emp_buttons, text="삭제", command=self.delete_selected_employee).pack(side="left", padx=3)

        backup_frame = ttk.LabelFrame(self.settings_tab, text="DB 백업")
        backup_frame.grid(row=3, column=0, sticky="ew")
        ttk.Label(backup_frame, textvariable=self.backup_status_var).grid(row=0, column=0, sticky="w", padx=8, pady=6)
        ttk.Label(backup_frame, textvariable=self.recent_backup_var).grid(row=1, column=0, sticky="w", padx=8, pady=(0, 6))
        ttk.Label(backup_frame, text="백업 보관: 7일").grid(row=0, column=1, sticky="e", padx=8, pady=6)
        ttk.Button(backup_frame, text="지금 백업", command=self.manual_backup).grid(row=1, column=1, sticky="e", padx=8, pady=(0, 6))
        backup_frame.columnconfigure(0, weight=1)

    def build_records_tab(self) -> None:
        self.records_tab.columnconfigure(0, weight=1)
        self.records_tab.rowconfigure(1, weight=1)
        top = ttk.Frame(self.records_tab)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(top, text="CSV 내보내기", command=self.open_export_dialog).pack(side="left", padx=3)
        ttk.Button(top, text="최근 기록 새로고침", command=self.refresh_records_tree).pack(side="left", padx=3)
        columns = ("employee", "start", "due", "ended", "pay", "type", "memo", "alarm")
        self.record_tree = ttk.Treeview(self.records_tab, columns=columns, show="headings")
        headings = [
            ("employee", "직원명", 130),
            ("start", "시작시각", 150),
            ("due", "타이머만료시각", 150),
            ("ended", "종료처리시각", 150),
            ("pay", "급여계산시간", 110),
            ("type", "종료방식", 100),
            ("memo", "메모", 160),
            ("alarm", "알람음상태", 110),
        ]
        for col, label, width in headings:
            self.record_tree.heading(col, text=label)
            self.record_tree.column(col, width=width, anchor="w")
        self.record_tree.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(self.records_tab, orient="vertical", command=self.record_tree.yview)
        self.record_tree.configure(yscrollcommand=scroll.set)
        scroll.grid(row=1, column=1, sticky="ns")

    def auto_end_label(self, seconds: int) -> str:
        for value, label in AUTO_END_CHOICES:
            if value == seconds:
                return label
        return "1분"

    def auto_end_seconds_from_label(self, label: str) -> int:
        for value, item_label in AUTO_END_CHOICES:
            if item_label == label:
                return value
        return 60

    def set_message(self, text: str) -> None:
        self.message_var.set(text)

    def get_employee(self, employee_id: str) -> Optional[Employee]:
        return self.employee_by_id.get(employee_id)

    def default_work_minutes(self) -> int:
        return int(self.settings.get("default_work_minutes", 60))

    def confirm_wait_seconds(self) -> int:
        return int(self.settings.get("confirm_wait_seconds", 30))

    def auto_end_wait_seconds(self) -> int:
        return int(self.settings.get("auto_end_wait_seconds", 60))

    def save_settings(self) -> None:
        self.settings["employees"] = [emp.to_dict() for emp in self.employees]
        self.settings_store.save(self.settings)

    def save_global_settings(self) -> None:
        try:
            minutes = int(self.default_minutes_var.get().replace("분", ""))
        except ValueError:
            minutes = 60
        if minutes not in WORK_MINUTE_CHOICES:
            minutes = 60
        self.settings["default_work_minutes"] = minutes
        self.settings["auto_end_wait_seconds"] = self.auto_end_seconds_from_label(self.auto_end_wait_var.get())
        self.settings["background_music_path"] = normalize_path(self.bg_path_var.get())
        self.settings["background_autoplay"] = self.bg_autoplay_var.get()
        self.background_desired = self.background_desired or self.bg_autoplay_var.get()
        self.save_settings()
        self.set_message("설정을 저장했습니다.")
        self.resume_audio_policy()

    def choose_background(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.root,
            title="배경음악 선택",
            filetypes=[
                ("음악 파일", "*.mp3 *.wav *.wma *.m4a *.aac"),
                ("모든 파일", "*.*"),
            ],
        )
        if path:
            self.bg_path_var.set(path)
            self.save_global_settings()

    def clear_background(self) -> None:
        self.bg_path_var.set("")
        self.settings["background_music_path"] = ""
        self.save_settings()
        if self.audio_role == "background":
            self.player.stop()
            self.audio_role = None
        self.set_message("배경음악 설정을 비웠습니다.")
        self.update_audio_labels()

    def start_background(self) -> None:
        self.background_desired = True
        self.resume_audio_policy()

    def stop_background(self) -> None:
        self.background_desired = False
        if self.audio_role == "background":
            self.player.stop()
            self.audio_role = None
        self.update_audio_labels()

    def startup_backup(self) -> None:
        try:
            created, message = self.backup_manager.create_auto_backup_if_needed(self.last_backup_date)
            self.backup_status_var.set(message if created else message)
            self.update_recent_backup_label()
        except Exception:
            self.backup_status_var.set("백업 실패: 저장 공간 또는 권한을 확인해 주세요")

    def update_recent_backup_label(self) -> None:
        latest = self.backup_manager.latest_backup()
        if not latest:
            self.recent_backup_var.set("최근 백업: -")
            return
        modified = dt.datetime.fromtimestamp(latest.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        self.recent_backup_var.set(f"최근 백업: {modified}")

    def manual_backup(self) -> None:
        try:
            target = self.backup_manager.create_manual_backup()
            stamp = dt.datetime.fromtimestamp(target.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            self.backup_status_var.set(f"백업 완료: {stamp}")
            self.update_recent_backup_label()
            self.set_message(f"백업 완료: {target.name}")
        except Exception:
            self.backup_status_var.set("백업 실패: 저장 공간 또는 권한을 확인해 주세요")
            self.set_message("백업 실패: 저장 공간 또는 권한을 확인해 주세요")

    def recover_active_state(self) -> None:
        loaded = self.database.load_active()
        if not loaded:
            state_path = self.data_dir / ACTIVE_STATE_NAME
            if state_path.exists():
                try:
                    raw = json.loads(state_path.read_text(encoding="utf-8"))
                    loaded = [ActiveWork.from_dict(item) for item in raw.get("active_work", [])]
                    for work in loaded:
                        self.database.upsert_active(work)
                except Exception:
                    loaded = []

        recovered_count = 0
        cleaned_count = 0
        current = now_dt()
        for work in loaded:
            if work.due_at <= current:
                pay_seconds = int((work.due_at - work.start_at).total_seconds())
                self.database.insert_record(
                    work=work,
                    ended_at=current,
                    pay_seconds=pay_seconds,
                    end_type="재실행자동정리",
                    alarm_status="정상",
                )
                self.database.delete_active(work.employee_id)
                cleaned_count += 1
            else:
                self.active_works[work.employee_id] = work
                recovered_count += 1
        self.save_active_state_file()
        if cleaned_count:
            self.set_message(f"앱이 꺼져 있는 동안 만료된 업무 {cleaned_count}건을 자동 정리했습니다.")
        elif recovered_count:
            self.set_message(f"진행 중 업무 {recovered_count}건을 복구했습니다.")

    def save_active_state_file(self) -> None:
        data = {
            "saved_at": dt_to_str(now_dt()),
            "active_work": [work.to_dict() for work in self.active_works.values()],
        }
        atomic_write_text(self.data_dir / ACTIVE_STATE_NAME, json.dumps(data, ensure_ascii=False, indent=2))

    def add_employee(self) -> None:
        dialog = EmployeeDialog(self.root, None, self.default_work_minutes())
        self.root.wait_window(dialog)
        if dialog.result:
            self.employees.append(dialog.result)
            self.employee_by_id = {emp.id: emp for emp in self.employees}
            self.save_settings()
            self.refresh_all()
            self.set_message(f"{dialog.result.name} 직원을 추가했습니다.")

    def selected_employee_id(self) -> Optional[str]:
        if not self.employee_tree:
            return None
        selected = self.employee_tree.selection()
        if not selected:
            messagebox.showinfo("확인", "직원을 선택해 주세요.", parent=self.root)
            return None
        return selected[0]

    def edit_selected_employee(self) -> None:
        employee_id = self.selected_employee_id()
        if employee_id:
            self.edit_employee(employee_id)

    def toggle_selected_employee(self) -> None:
        employee_id = self.selected_employee_id()
        if employee_id:
            emp = self.get_employee(employee_id)
            if emp and emp.enabled:
                self.disable_employee(employee_id)
            else:
                self.enable_employee(employee_id)

    def delete_selected_employee(self) -> None:
        employee_id = self.selected_employee_id()
        if employee_id:
            self.delete_employee(employee_id)

    def edit_employee(self, employee_id: str) -> None:
        emp = self.get_employee(employee_id)
        if not emp:
            return
        old_name = emp.name
        dialog = EmployeeDialog(self.root, emp, self.default_work_minutes())
        self.root.wait_window(dialog)
        if not dialog.result:
            return
        index = self.employees.index(emp)
        self.employees[index] = dialog.result
        self.employee_by_id = {item.id: item for item in self.employees}
        self.save_settings()
        if old_name != dialog.result.name:
            self.set_message(f"직원명을 {dialog.result.name}(으)로 변경했습니다.")
        else:
            self.set_message(f"{dialog.result.name} 정보를 저장했습니다.")
        self.refresh_all()

    def enable_employee(self, employee_id: str) -> None:
        emp = self.get_employee(employee_id)
        if not emp:
            return
        emp.enabled = True
        self.save_settings()
        self.refresh_all()
        self.set_message(f"{emp.name} 직원을 활성화했습니다.")

    def disable_employee(self, employee_id: str) -> None:
        emp = self.get_employee(employee_id)
        if not emp:
            return
        emp.enabled = False
        if employee_id == self.current_alarm_emp_id:
            self.stop_current_music()
            self.current_alarm_emp_id = None
            self.complete_work(employee_id, "수동종료", alarm_status_override=None)
            self.speech.speak(f"{emp.name} 업무가 종료되었습니다.", after=self.process_next_alarm_or_background)
        elif employee_id in self.alarm_queue:
            self.alarm_queue = [item for item in self.alarm_queue if item != employee_id]
            self.complete_work(employee_id, "수동종료", alarm_status_override=None)
        elif employee_id in self.active_works:
            self.cancel_active_work(employee_id)
        self.reset_confirm_until.pop(employee_id, None)
        self.end_confirm_until.pop(employee_id, None)
        self.save_settings()
        self.refresh_all()
        self.set_message(f"{emp.name} 직원을 비활성화했습니다.")

    def delete_employee(self, employee_id: str) -> None:
        emp = self.get_employee(employee_id)
        if not emp:
            return
        if not messagebox.askyesno("직원 삭제", "정말 이 직원을 삭제할까요?", parent=self.root):
            return
        if employee_id == self.current_alarm_emp_id:
            self.stop_current_music()
            self.current_alarm_emp_id = None
            self.complete_work(employee_id, "수동종료", alarm_status_override=None)
            self.process_next_alarm_or_background()
        elif employee_id in self.alarm_queue:
            self.alarm_queue = [item for item in self.alarm_queue if item != employee_id]
            self.complete_work(employee_id, "수동종료", alarm_status_override=None)
        elif employee_id in self.active_works:
            self.cancel_active_work(employee_id)
        self.employees = [item for item in self.employees if item.id != employee_id]
        self.employee_by_id = {item.id: item for item in self.employees}
        self.save_settings()
        self.refresh_all()
        self.set_message(f"{emp.name} 직원을 삭제했습니다.")

    def cancel_active_work(self, employee_id: str) -> None:
        self.active_works.pop(employee_id, None)
        self.database.delete_active(employee_id)
        self.save_active_state_file()

    def on_start(self, employee_id: str, memo: Optional[str] = None) -> None:
        emp = self.get_employee(employee_id)
        if not emp or not emp.enabled:
            return
        current = now_dt()
        if employee_id in self.active_works or employee_id in self.alarm_queue or employee_id == self.current_alarm_emp_id:
            until = self.reset_confirm_until.get(employee_id)
            if until and until >= current:
                self.reset_employee_work(employee_id, memo or "")
                self.reset_confirm_until.pop(employee_id, None)
                return
            self.reset_confirm_until[employee_id] = current + dt.timedelta(seconds=self.confirm_wait_seconds())
            self.speech.speak(
                "이미 업무가 진행 중입니다. 30초 안에 한 번 더 누르면 타이머가 초기화됩니다.",
                after=self.resume_audio_policy,
            )
            self.refresh_board()
            return
        self.start_work(employee_id, memo or "")

    def on_start_with_memo(self, employee_id: str) -> None:
        emp = self.get_employee(employee_id)
        if not emp or not emp.enabled:
            return
        memo = simpledialog.askstring("메모와 함께 시작", "메모", parent=self.root)
        if memo is None:
            return
        self.on_start(employee_id, memo.strip())

    def start_work(self, employee_id: str, memo: str = "") -> None:
        emp = self.get_employee(employee_id)
        if not emp or not emp.enabled:
            return
        start = now_dt()
        minutes = emp.work_minutes(self.default_work_minutes())
        work = ActiveWork(
            employee_id=emp.id,
            employee_name_snapshot=emp.name,
            start_at=start,
            due_at=start + dt.timedelta(minutes=minutes),
            work_minutes=minutes,
            memo=memo.strip(),
            alarm_music_path_snapshot=emp.alarm_music_path,
        )
        self.active_works[emp.id] = work
        self.database.upsert_active(work)
        self.save_active_state_file()
        self.end_confirm_until.pop(emp.id, None)
        self.reset_confirm_until.pop(emp.id, None)
        self.set_message(f"{emp.name} 업무를 시작했습니다.")
        self.refresh_all()

    def reset_employee_work(self, employee_id: str, memo: str = "") -> None:
        emp = self.get_employee(employee_id)
        if not emp:
            return
        if employee_id == self.current_alarm_emp_id:
            self.stop_current_music()
            self.current_alarm_emp_id = None
        self.alarm_queue = [item for item in self.alarm_queue if item != employee_id]
        self.cancel_active_work(employee_id)
        self.start_work(employee_id, memo)
        self.set_message(f"{emp.name} 타이머를 처음부터 다시 시작했습니다.")
        self.process_next_alarm_or_background()

    def on_end(self, employee_id: str) -> None:
        emp = self.get_employee(employee_id)
        if not emp:
            return
        current = now_dt()
        if employee_id == self.current_alarm_emp_id:
            self.finish_current_alarm("수동종료")
            return
        if employee_id in self.alarm_queue:
            self.alarm_queue = [item for item in self.alarm_queue if item != employee_id]
            self.complete_work(employee_id, "수동종료", alarm_status_override=None)
            self.set_message(f"{emp.name} 대기 중 알람을 종료 처리했습니다.")
            self.refresh_all()
            return
        work = self.active_works.get(employee_id)
        if not work:
            self.set_message(f"{emp.name} 직원은 진행 중인 업무가 없습니다.")
            return
        if current < work.due_at:
            until = self.end_confirm_until.get(employee_id)
            if until and until >= current:
                self.end_confirm_until.pop(employee_id, None)
                self.complete_work(employee_id, "중도종료", ended_at=current, alarm_status_override="알람전종료")
                self.speech.speak(f"{emp.name} 업무가 종료되었습니다.", after=self.resume_audio_policy)
                self.refresh_all()
                return
            self.end_confirm_until[employee_id] = current + dt.timedelta(seconds=self.confirm_wait_seconds())
            self.speech.speak(
                "아직 업무가 진행 중입니다. 30초 안에 한 번 더 누르면 업무를 종료합니다.",
                after=self.resume_audio_policy,
            )
            self.refresh_board()
            return
        self.complete_work(employee_id, "수동종료", ended_at=current, alarm_status_override=None)
        self.speech.speak(f"{emp.name} 업무가 종료되었습니다.", after=self.resume_audio_policy)
        self.refresh_all()

    def complete_work(
        self,
        employee_id: str,
        end_type: str,
        ended_at: Optional[dt.datetime] = None,
        alarm_status_override: Optional[str] = None,
    ) -> None:
        work = self.active_works.get(employee_id)
        if not work:
            return
        ended = ended_at or now_dt()
        if end_type == "중도종료":
            pay_seconds = int((ended - work.start_at).total_seconds())
        else:
            pay_seconds = int((work.due_at - work.start_at).total_seconds())
        if alarm_status_override:
            alarm_status = alarm_status_override
        elif end_type == "중도종료":
            alarm_status = "알람전종료"
        elif work.used_default_alarm:
            alarm_status = "기본알람사용"
        else:
            alarm_status = "정상"
        self.database.insert_record(work, ended, pay_seconds, end_type, alarm_status)
        self.database.delete_active(employee_id)
        self.active_works.pop(employee_id, None)
        self.reset_confirm_until.pop(employee_id, None)
        self.end_confirm_until.pop(employee_id, None)
        self.save_active_state_file()
        self.refresh_records_tree()

    def finish_current_alarm(self, end_type: str) -> None:
        employee_id = self.current_alarm_emp_id
        if not employee_id:
            return
        work = self.active_works.get(employee_id)
        name = work.employee_name_snapshot if work else (self.get_employee(employee_id).name if self.get_employee(employee_id) else "")
        used_default = bool(work and work.used_default_alarm)
        self.stop_current_music()
        self.current_alarm_emp_id = None
        self.complete_work(employee_id, end_type, alarm_status_override=None)
        if end_type == "자동종료":
            text = f"{name} 업무가 자동 종료되었습니다."
        else:
            text = f"{name} 업무가 종료되었습니다."
        if used_default:
            text += " 설정된 알람 음악 파일을 찾을 수 없어 기본 알람음으로 재생했습니다."
        self.speech.speak(text, after=self.process_next_alarm_or_background)
        self.refresh_all()

    def expire_due_work(self) -> None:
        current = now_dt()
        due_items = [
            work
            for work in self.active_works.values()
            if work.employee_id != self.current_alarm_emp_id
            and work.employee_id not in self.alarm_queue
            and work.due_at <= current
        ]
        for work in sorted(due_items, key=lambda item: item.due_at):
            emp = self.get_employee(work.employee_id)
            if emp and not emp.enabled:
                continue
            self.alarm_queue.append(work.employee_id)
        self.alarm_queue.sort(key=lambda emp_id: self.active_works[emp_id].due_at if emp_id in self.active_works else current)
        if self.alarm_queue and not self.current_alarm_emp_id and not self.speech.active:
            self.start_next_alarm()

    def start_next_alarm(self) -> None:
        while self.alarm_queue:
            employee_id = self.alarm_queue.pop(0)
            work = self.active_works.get(employee_id)
            emp = self.get_employee(employee_id)
            if not work or (emp and not emp.enabled):
                continue
            self.current_alarm_emp_id = employee_id
            work.alarm_started_at = now_dt()
            self.play_current_alarm(reset_missing_state=True)
            self.database.upsert_active(work)
            self.save_active_state_file()
            self.refresh_all()
            return
        self.current_alarm_emp_id = None
        self.resume_audio_policy()

    def play_current_alarm(self, reset_missing_state: bool = False) -> None:
        if not self.current_alarm_emp_id:
            return
        work = self.active_works.get(self.current_alarm_emp_id)
        if not work:
            self.current_alarm_emp_id = None
            return
        self.stop_current_music()
        alarm_path = Path(work.alarm_music_path_snapshot) if work.alarm_music_path_snapshot else None
        use_default = False
        play_path = alarm_path
        if not play_path or not play_path.exists():
            use_default = True
            play_path = self.default_alarm_path
        ok, error = self.player.play_loop(play_path)
        if not ok and play_path != self.default_alarm_path:
            use_default = True
            play_path = self.default_alarm_path
            ok, error = self.player.play_loop(play_path)
        if reset_missing_state or use_default:
            work.used_default_alarm = use_default
            self.database.upsert_active(work)
            self.save_active_state_file()
        if ok:
            self.audio_role = "alarm"
            if use_default:
                self.set_message(f"{work.employee_name_snapshot} 알람 파일이 없어 기본 알람음을 재생합니다.")
            else:
                self.set_message(f"{work.employee_name_snapshot} 알람 음악을 재생합니다.")
        else:
            self.audio_role = None
            self.set_message(f"알람 재생 실패: {error}")
        self.update_audio_labels()

    def process_next_alarm_or_background(self) -> None:
        if self.speech.active:
            return
        if self.alarm_queue:
            self.start_next_alarm()
        else:
            self.resume_audio_policy()

    def on_tts_started(self) -> None:
        self.stop_current_music()
        self.update_audio_labels(tts=True)

    def on_tts_error(self, error: str) -> None:
        self.set_message(f"TTS 실패: {error}")

    def stop_current_music(self) -> None:
        if self.audio_role:
            self.player.stop()
            self.audio_role = None

    def resume_audio_policy(self) -> None:
        if self.speech.active:
            return
        if self.current_alarm_emp_id:
            if self.audio_role != "alarm":
                self.play_current_alarm(reset_missing_state=False)
            return
        if self.background_desired:
            path_text = self.settings.get("background_music_path") or self.bg_path_var.get()
            path = Path(path_text) if path_text else None
            if path and path.exists():
                if self.audio_role != "background" or self.player.current_path != str(path):
                    ok, error = self.player.play_loop(path)
                    if ok:
                        self.audio_role = "background"
                        self.set_message("")
                    else:
                        self.audio_role = None
                        self.set_message(f"배경음악 재생 실패: {error}")
            else:
                if self.audio_role == "background":
                    self.player.stop()
                    self.audio_role = None
                if self.background_desired:
                    self.set_message("저장된 배경음악 파일을 찾을 수 없습니다.")
        else:
            if self.audio_role == "background":
                self.player.stop()
                self.audio_role = None
        self.update_audio_labels()

    def update_audio_labels(self, tts: bool = False) -> None:
        if tts or self.speech.active:
            self.audio_status_var.set("현재 재생: TTS 재생 중")
        elif self.current_alarm_emp_id:
            work = self.active_works.get(self.current_alarm_emp_id)
            name = work.employee_name_snapshot if work else "직원"
            self.audio_status_var.set(f"현재 재생: {name} 알람 음악")
        elif self.audio_role == "background":
            self.audio_status_var.set("현재 재생: 잔잔한 배경음악")
        else:
            self.audio_status_var.set("현재 재생: 무음 대기")

        if self.current_alarm_emp_id:
            work = self.active_works.get(self.current_alarm_emp_id)
            if work and work.alarm_started_at:
                elapsed = int((now_dt() - work.alarm_started_at).total_seconds())
                remaining = self.auto_end_wait_seconds() - elapsed
                self.auto_end_var.set(f"자동 종료까지: {format_duration(remaining)}")
            else:
                self.auto_end_var.set("자동 종료까지: -")
        else:
            self.auto_end_var.set("자동 종료까지: -")

        queue_names = []
        for employee_id in self.alarm_queue:
            work = self.active_works.get(employee_id)
            if work:
                queue_names.append(work.employee_name_snapshot)
        self.queue_var.set("대기열: " + (", ".join(queue_names) if queue_names else "없음"))

    def tick(self) -> None:
        try:
            current = now_dt()
            self.drop_expired_confirms(current)
            self.expire_due_work()
            if self.current_alarm_emp_id and not self.speech.active:
                work = self.active_works.get(self.current_alarm_emp_id)
                if work and work.alarm_started_at:
                    elapsed = (current - work.alarm_started_at).total_seconds()
                    if elapsed >= self.auto_end_wait_seconds():
                        self.finish_current_alarm("자동종료")
            if current.date() != self.last_backup_date:
                self.last_backup_date = current.date()
                self.startup_backup()
            self.refresh_board()
            self.update_audio_labels()
        except Exception as exc:
            self.set_message(f"오류: {exc}")
        finally:
            self.root.after(500, self.tick)

    def drop_expired_confirms(self, current: dt.datetime) -> None:
        for mapping in (self.reset_confirm_until, self.end_confirm_until):
            expired = [employee_id for employee_id, until in mapping.items() if until < current]
            for employee_id in expired:
                mapping.pop(employee_id, None)

    def employee_status(self, emp: Employee, current: dt.datetime) -> tuple[str, str, int, int]:
        if not emp.enabled:
            return "비활성화됨", "-", 7, 0
        work = self.active_works.get(emp.id)
        if emp.id == self.current_alarm_emp_id:
            remaining = "-"
            if work and work.alarm_started_at:
                elapsed = int((current - work.alarm_started_at).total_seconds())
                remaining = f"자동 종료 {format_duration(self.auto_end_wait_seconds() - elapsed)}"
            return "알림 재생 중", remaining, 1, 0
        if emp.id in self.alarm_queue:
            index = self.alarm_queue.index(emp.id)
            return "알림 대기 중", f"대기열 {index + 1}번째", 2, index
        until = self.end_confirm_until.get(emp.id)
        if until and until >= current:
            remaining = int((until - current).total_seconds())
            return "종료 확인 대기 중", f"확인 {remaining}초", 3, remaining
        until = self.reset_confirm_until.get(emp.id)
        if until and until >= current:
            remaining = int((until - current).total_seconds())
            return "리셋 확인 대기 중", f"확인 {remaining}초", 4, remaining
        if work:
            remaining = int((work.due_at - current).total_seconds())
            return "업무 중", format_duration(remaining), 5, max(0, remaining)
        return "대기 중", "-", 6, 0

    def sorted_employees(self) -> list[Employee]:
        current = now_dt()

        def key(emp: Employee):
            status, _detail, priority, secondary = self.employee_status(emp, current)
            if status in ("대기 중", "비활성화됨"):
                secondary_key: object = emp.name
            else:
                secondary_key = secondary
            return (priority, secondary_key, emp.name)

        return sorted(self.employees, key=key)

    def refresh_board(self) -> None:
        for child in self.board_scroll.inner.winfo_children():
            child.destroy()
        employees = self.sorted_employees()
        if not employees:
            ttk.Label(self.board_scroll.inner, text="직원을 추가해 주세요.", style="Row.TLabel").grid(
                row=0, column=0, sticky="w", padx=8, pady=8
            )
            return
        current = now_dt()
        for row_index, emp in enumerate(employees):
            status, detail, _priority, _secondary = self.employee_status(emp, current)
            work = self.active_works.get(emp.id)
            memo = work.memo if work and work.memo else "-"
            row = ttk.Frame(self.board_scroll.inner, padding=(0, 3))
            row.grid(row=row_index, column=0, sticky="ew")
            self.board_scroll.inner.columnconfigure(0, weight=1)
            row.columnconfigure(4, weight=1)
            values = [emp.name, status, detail, memo]
            widths = [18, 18, 22, 22]
            for col, (value, width) in enumerate(zip(values, widths)):
                label = ttk.Label(row, text=value, style="Row.TLabel", width=width)
                label.grid(row=0, column=col, sticky="w", padx=6)
            ops = ttk.Frame(row)
            ops.grid(row=0, column=4, sticky="w", padx=6)
            if emp.enabled:
                ttk.Button(ops, text="업무 시작", style="Small.TButton", command=lambda eid=emp.id: self.on_start(eid)).pack(
                    side="left", padx=2
                )
                ttk.Button(ops, text="메모 시작", style="Small.TButton", command=lambda eid=emp.id: self.on_start_with_memo(eid)).pack(
                    side="left", padx=2
                )
                ttk.Button(ops, text="업무 종료", style="Small.TButton", command=lambda eid=emp.id: self.on_end(eid)).pack(
                    side="left", padx=2
                )
                ttk.Button(ops, text="수정", style="Small.TButton", command=lambda eid=emp.id: self.edit_employee(eid)).pack(
                    side="left", padx=2
                )
                ttk.Button(ops, text="비활성화", style="Small.TButton", command=lambda eid=emp.id: self.disable_employee(eid)).pack(
                    side="left", padx=2
                )
            else:
                ttk.Button(ops, text="활성화", style="Small.TButton", command=lambda eid=emp.id: self.enable_employee(eid)).pack(
                    side="left", padx=2
                )
                ttk.Button(ops, text="수정", style="Small.TButton", command=lambda eid=emp.id: self.edit_employee(eid)).pack(
                    side="left", padx=2
                )
            ttk.Button(ops, text="삭제", style="Small.TButton", command=lambda eid=emp.id: self.delete_employee(eid)).pack(
                side="left", padx=2
            )

    def refresh_employee_tree(self) -> None:
        if not self.employee_tree:
            return
        for item in self.employee_tree.get_children():
            self.employee_tree.delete(item)
        for emp in self.employees:
            override = f"{emp.work_minutes_override}분" if emp.work_minutes_override else f"기본값({self.default_work_minutes()}분)"
            self.employee_tree.insert(
                "",
                "end",
                iid=emp.id,
                values=(
                    emp.name,
                    "활성" if emp.enabled else "비활성",
                    override,
                    emp.alarm_music_path or "-",
                ),
            )

    def refresh_records_tree(self) -> None:
        if not self.record_tree:
            return
        for item in self.record_tree.get_children():
            self.record_tree.delete(item)
        for row in self.database.query_records(limit=200):
            self.record_tree.insert(
                "",
                "end",
                values=(
                    row["employee_name_snapshot"],
                    row["start_at"],
                    row["due_at"] or "",
                    row["ended_at"],
                    format_duration(row["pay_seconds"], force_hours=True),
                    row["end_type"],
                    row["memo"] or "",
                    row["alarm_status"],
                ),
            )

    def refresh_all(self) -> None:
        self.refresh_board()
        self.refresh_employee_tree()
        self.refresh_records_tree()
        self.update_audio_labels()
        self.update_recent_backup_label()

    def open_export_dialog(self) -> None:
        ExportDialog(self.root, self.database, self.employees, self.root_dir, self.set_message)

    def on_close(self) -> None:
        if self.active_works:
            proceed = messagebox.askokcancel(
                "종료 확인",
                "진행 중인 업무가 있습니다.\n앱을 종료해도 업무 기록은 보존되고, 다음 실행 시 복구됩니다.\n\n종료하시겠습니까?",
                parent=self.root,
            )
            if not proceed:
                return
        self.save_active_state_file()
        self.player.stop()
        self.database.close()
        self.root.destroy()


def self_test() -> int:
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        db = WorkDatabase(temp_path / DB_NAME)
        employee = Employee(id="emp_test", name="테스트")
        start = now_dt() - dt.timedelta(minutes=10)
        work = ActiveWork(
            employee_id=employee.id,
            employee_name_snapshot=employee.name,
            start_at=start,
            due_at=start + dt.timedelta(minutes=60),
            work_minutes=60,
            memo="검수",
        )
        db.upsert_active(work)
        loaded = db.load_active()
        assert len(loaded) == 1
        db.insert_record(work, now_dt(), 600, "중도종료", "알람전종료")
        records = db.query_records(include_mid=True)
        assert len(records) == 1
        backup = BackupManager(temp_path, db)
        created, _message = backup.create_auto_backup_if_needed()
        assert created
        assert backup.latest_backup() is not None
        db.close()
        assert ensure_default_alarm_file().exists()
    return 0


def main() -> None:
    if "--self-test" in sys.argv:
        raise SystemExit(self_test())
    root = tk.Tk()
    app = TimerApp(root)
    try:
        root.mainloop()
    except Exception:
        app.player.stop()
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
