"""Background subprocess runner shared by the Bot and Run Pipeline tabs.

The dashboard runs as a single long-lived Flask process on your own
machine (not behind a WSGI stack), so a plain module-level registry is
enough to track "the bot" and "the current pipeline run" across requests
— no external job queue needed. Each named job keeps its own rolling
in-memory log tail (for the dashboard's live view) and also streams to a
file on disk (for a full log after the page is closed), same pattern
`telegram_bot.py` already uses for pipeline runs it triggers itself.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from collections import deque
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_LOG_TAIL_LINES = 500


class Job:
    def __init__(self, name: str, args: list[str], log_path: Path) -> None:
        self.name = name
        self.args = args
        self.log_path = log_path
        self.proc: subprocess.Popen | None = None
        self.lines: deque[str] = deque(maxlen=_LOG_TAIL_LINES)
        self._start_lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        with self._start_lock:
            if self.running:
                raise RuntimeError(f"'{self.name}' is already running")
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self.lines.clear()
            self.proc = subprocess.Popen(
                self.args,
                cwd=REPO_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
            )
            threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        proc = self.proc
        assert proc is not None and proc.stdout is not None
        with self.log_path.open("w", encoding="utf-8") as log_fh:
            for raw_line in proc.stdout:
                line = raw_line.rstrip("\n")
                self.lines.append(line)
                log_fh.write(line + "\n")
                log_fh.flush()

    def stop(self) -> None:
        if not self.proc or not self.running:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def status(self) -> dict:
        return {
            "running": self.running,
            "returncode": self.proc.poll() if self.proc else None,
            "log": "\n".join(self.lines),
        }


_REGISTRY: dict[str, Job] = {}
_REGISTRY_LOCK = threading.Lock()


def start(name: str, args: list[str], log_path: Path) -> Job:
    with _REGISTRY_LOCK:
        existing = _REGISTRY.get(name)
        if existing and existing.running:
            raise RuntimeError(f"'{name}' is already running")
        job = Job(name, args, log_path)
        _REGISTRY[name] = job
    job.start()
    return job


def stop(name: str) -> None:
    job = _REGISTRY.get(name)
    if job:
        job.stop()


def get(name: str) -> Job | None:
    return _REGISTRY.get(name)


def python_args(*script_and_args: str) -> list[str]:
    """`[sys.executable, "-u", ...]` — unbuffered so log lines stream live
    instead of sitting in the child's stdout buffer until it exits."""
    return [sys.executable, "-u", *script_and_args]
