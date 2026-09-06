"""Serving for real: draining in-flight work, and shutting down on a signal."""

from __future__ import annotations

import asyncio
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from mcp_gateway.app import HEALTH_PATH, create_app, uvicorn_config
from mcp_gateway.config import Settings, load_settings

WINDOWS = sys.platform == "win32"
#: On Windows a console process cannot be sent SIGINT; SIGBREAK is the signal
#: uvicorn handles there, and it reaches a child in its own process group.
STOP_SIGNAL = signal.CTRL_BREAK_EVENT if WINDOWS else signal.SIGINT
CREATION_FLAGS = subprocess.CREATE_NEW_PROCESS_GROUP if WINDOWS else 0
SLOW_SERVER = Path(__file__).with_name("slow_server.py")


def free_port() -> int:
    """Ask the OS for a port nothing is listening on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def write_config(tmp_path: Path, port: int) -> Path:
    config = tmp_path / "config.toml"
    config.write_text(f'[server]\nhost = "127.0.0.1"\nport = {port}\n', encoding="utf-8")
    return config


def settings_for(tmp_path: Path, port: int) -> Settings:
    return load_settings({"config": str(write_config(tmp_path, port))}, environ={})


def recording_service(log: list[str]) -> Callable[[FastAPI], object]:
    @asynccontextmanager
    async def service(app: FastAPI) -> AsyncIterator[None]:
        log.append("start")
        try:
            yield
        finally:
            log.append("stop")

    return service


async def wait_until(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.02)


async def test_the_shutdown_path_drains_in_flight_requests(tmp_path: Path) -> None:
    port = free_port()
    settings = settings_for(tmp_path, port)
    log: list[str] = []
    app = create_app(settings, services=[recording_service(log)])
    handling = asyncio.Event()

    @app.get("/slow")
    async def slow() -> dict[str, str]:
        handling.set()
        await asyncio.sleep(0.3)
        return {"status": "finished"}

    server = uvicorn.Server(uvicorn_config(app, settings))
    serving = asyncio.create_task(server.serve())
    try:
        await wait_until(lambda: server.started)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=10) as client:
            pending = asyncio.create_task(client.get("/slow"))
            await asyncio.wait_for(handling.wait(), timeout=10)

            # Exactly what a signal does, minus the signal.
            server.should_exit = True

            response = await asyncio.wait_for(pending, timeout=10)
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, timeout=10)

    assert response.status_code == 200
    assert response.json() == {"status": "finished"}
    assert log == ["start", "stop"]


def start_server(command: list[str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        creationflags=CREATION_FLAGS,
    )


def wait_for_health(process: subprocess.Popen[str], port: int) -> None:
    deadline = time.monotonic() + 30
    while True:
        if process.poll() is not None:
            pytest.fail(f"server exited early: {process.communicate()[1]}")
        try:
            if httpx.get(f"http://127.0.0.1:{port}{HEALTH_PATH}", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        if time.monotonic() > deadline:
            pytest.fail("server never became healthy")
        time.sleep(0.05)


def test_a_signal_shuts_the_process_down_cleanly(tmp_path: Path) -> None:
    port = free_port()
    config = write_config(tmp_path, port)
    process = start_server(
        [sys.executable, "-m", "mcp_gateway", "--config", str(config), "--log-level", "debug"]
    )
    try:
        wait_for_health(process, port)

        process.send_signal(STOP_SIGNAL)
        _, stderr = process.communicate(timeout=30)
    finally:
        if process.poll() is None:  # pragma: no cover - only on a hung server
            process.kill()
            process.communicate()

    assert process.returncode == 0
    # The lifespan teardown ran, rather than the process being cut short.
    assert "Shutdown complete" in stderr


def test_a_signal_during_a_request_lets_it_finish_and_exits_0(tmp_path: Path) -> None:
    port = free_port()
    config = write_config(tmp_path, port)
    process = start_server([sys.executable, str(SLOW_SERVER), str(config)])
    responses: list[httpx.Response] = []
    try:
        wait_for_health(process, port)

        caller = threading.Thread(
            target=lambda: responses.append(httpx.get(f"http://127.0.0.1:{port}/slow", timeout=30))
        )
        caller.start()
        # The route prints as it starts work, so the signal lands mid-request.
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "handling"

        process.send_signal(STOP_SIGNAL)

        caller.join(timeout=30)
        _, stderr = process.communicate(timeout=30)
    finally:
        if process.poll() is None:  # pragma: no cover - only on a hung server
            process.kill()
            process.communicate()

    assert responses and responses[0].status_code == 200
    assert responses[0].json() == {"status": "finished"}
    assert process.returncode == 0
    assert "Shutdown complete" in stderr
