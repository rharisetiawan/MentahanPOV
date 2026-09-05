"""Background `docker compose run` runner for the Run-pipeline tab, plus
small synchronous helpers for `docker compose` bot control.

Production runs everything through Docker (see docker-compose.yml) — the
bot is the `mentahanpov-bot` service, and a manually-triggered pipeline
run goes through `docker compose run --rm mentahanpov-bot python main.py
...` so it gets the exact same image, deps, and mounted
credentials/state as the real bot, rather than needing a second Python
environment on the host with the full (heavy) requirements.txt installed.

Only the Run tab needs the background-with-live-log treatment (a run
takes minutes); bot start/stop/apply are quick one-shot `docker compose`
calls that return as soon as the command itself finishes.
"""

from __future__ import annotations

import subprocess
import threading
from collections import deque
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_LOG_TAIL_LINES = 500


def compose_args(*args: str) -> list[str]:
    return ["docker", "compose", *args]


def run_compose(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        compose_args(*args),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


class Job:
    """One background `docker compose run ...` invocation with a rolling
    in-memory log tail, so the Run tab can poll progress without blocking
    the request that started it."""

    def __init__(self, name: str, args: list[str]) -> None:
        self.name = name
        self.args = args
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
        for raw_line in proc.stdout:
            self.lines.append(raw_line.rstrip("\n"))

    def stop(self) -> None:
        # Terminates the `docker compose run` CLI process, which forwards
        # the stop through to the throwaway container (compose removes it
        # per --rm once the run it was tracking ends).
        if not self.proc or not self.running:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=15)
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


def start(name: str, args: list[str]) -> Job:
    with _REGISTRY_LOCK:
        existing = _REGISTRY.get(name)
        if existing and existing.running:
            raise RuntimeError(f"'{name}' is already running")
        job = Job(name, args)
        _REGISTRY[name] = job
    job.start()
    return job


def stop(name: str) -> None:
    job = _REGISTRY.get(name)
    if job:
        job.stop()


def get(name: str) -> Job | None:
    return _REGISTRY.get(name)
