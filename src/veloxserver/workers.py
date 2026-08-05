from __future__ import annotations

import multiprocessing
import os
import json
import signal
import shlex
import socket
import ssl
import subprocess
import sys
import time
from dataclasses import replace

from .server import ServerConfig, VeloxServer


def run_worker_pool(config: ServerConfig) -> int:
    if config.workers <= 1:
        raise ValueError("worker pool requires workers > 1")
    if os.name == "nt":
        raise RuntimeError("multi-process worker mode requires SO_REUSEPORT and is not supported on Windows")

    worker_config = replace(config, reuse_port=True)
    processes = [
        multiprocessing.Process(target=_run_worker, args=(worker_config,), daemon=False)
        for _ in range(config.workers)
    ]
    for process in processes:
        process.start()

    def stop(_signum: int, _frame: object) -> None:
        for process in processes:
            if process.is_alive():
                process.terminate()

    def upgrade(_signum: int, _frame: object) -> None:
        if request_worker_pool_upgrade(config):
            stop(_signum, _frame)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    sigusr2 = getattr(signal, "SIGUSR2", None)
    if sigusr2 is not None:
        signal.signal(sigusr2, upgrade)

    exit_code = 0
    try:
        for process in processes:
            process.join()
            if process.exitcode not in {0, None}:
                exit_code = process.exitcode or 1
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
    return exit_code


def _run_worker(config: ServerConfig) -> None:
    import asyncio

    try:
        asyncio.run(VeloxServer(config).serve_forever())
    except KeyboardInterrupt:
        raise SystemExit(130)


def exec_current_process() -> str:
    return " ".join([sys.executable, *sys.argv])


def request_worker_pool_upgrade(config: ServerConfig) -> bool:
    if not config.upgrade_command:
        print("veloxserver worker master upgrade requested but upgrade_command is not configured")
        return False
    current_generation = int(os.environ.get("VELOXSERVER_GENERATION", "0") or "0")
    next_generation = current_generation + 1
    env = os.environ.copy()
    env["VELOXSERVER_UPGRADE_FROM_PID"] = str(os.getpid())
    env["VELOXSERVER_GENERATION"] = str(next_generation)
    try:
        process = subprocess.Popen(shlex.split(config.upgrade_command), close_fds=False, env=env)
    except OSError as exc:
        print(f"veloxserver worker master upgrade failed: {exc}")
        return False
    write_upgrade_state(config, process.pid, next_generation, "started")
    ready = wait_for_generation(config, next_generation)
    write_upgrade_state(config, process.pid, next_generation, "ready" if ready else "failed")
    if ready:
        time.sleep(config.upgrade_grace_seconds)
    return ready


def wait_for_generation(config: ServerConfig, generation: int) -> bool:
    deadline = time.time() + config.upgrade_ready_timeout
    while time.time() < deadline:
        if probe_generation(config, generation):
            return True
        time.sleep(0.1)
    return False


def probe_generation(config: ServerConfig, generation: int) -> bool:
    try:
        sock = socket.create_connection((config.host, config.port), timeout=1.0)
        if config.tls_certfile is not None:
            context = ssl._create_unverified_context()
            sock = context.wrap_socket(sock, server_hostname=config.host)
        with sock:
            request = (
                f"GET {config.health_path} HTTP/1.1\r\n"
                f"Host: {config.host}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
            sock.sendall(request)
            response = bytearray()
            while b"\r\n\r\n" not in response and len(response) < 65536:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response.extend(chunk)
    except OSError:
        return False
    return f"x-veloxserver-generation: {generation}".encode("ascii") in bytes(response).lower()


def write_upgrade_state(config: ServerConfig, pid: int, generation: int, state: str) -> None:
    if config.upgrade_state_path is None:
        return
    payload = {
        "old_pid": os.getpid(),
        "new_pid": pid,
        "generation": generation,
        "state": state,
        "time": time.time(),
    }
    try:
        config.upgrade_state_path.parent.mkdir(parents=True, exist_ok=True)
        config.upgrade_state_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    except OSError as exc:
        print(f"veloxserver worker master upgrade state write failed: {exc}")
