from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.audit import AuditEmitter
from api.config import Settings
from api.docker_client import ExecOutput, hardening_flags
from api.models import Limits
from api.registry import Registry
from api.server import create_app
from api.sessions import SessionService


class FakeDockerClient:
    """Mocks api.docker_client.DockerClient at the same surface."""

    def __init__(self, s: Settings) -> None:
        self._settings = s
        self.network_ensured = False
        self.created_volumes: list[tuple[str, str, str]] = []
        self.removed_volumes: list[str] = []
        self.created_containers: list[tuple[str, dict[str, Any]]] = []
        self.started: list[str] = []
        self.stopped: list[tuple[str, int]] = []
        self.removed_containers: list[str] = []
        self._counter = 0
        # Slice 11b background-process simulator. Tests flip processes
        # to "exited" via `simulate_process_exit(ospid, exit_code)`;
        # spawn_supervised assigns sequential ospids starting at 1000.
        self._next_ospid = 1000
        # ospid → state ("alive" or {"exited": <int>})
        self._processes: dict[int, dict[str, Any]] = {}
        # (container_id, abs_path) → bytes  — virtual fs for the
        # supervisor's pid/exit/log files.
        self._fs: dict[tuple[str, str], bytes] = {}
        self.spawn_supervised_calls: list[dict[str, Any]] = []
        self.signal_pid_calls: list[tuple[str, int, int]] = []

        # Slice 2 surface.
        self.exec_calls: list[dict[str, Any]] = []
        self.exec_responses: list[ExecOutput] = []
        self.put_archive_calls: list[dict[str, Any]] = []
        self.get_archive_responses: dict[str, tuple[bytes, int] | Exception] = {}
        self.simple_exec_calls: list[tuple[str, list[str]]] = []
        # Default handler: every short exec succeeds. Tests override per case.
        self.simple_exec_handler: Callable[[list[str]], tuple[bytes, bytes, int]] = lambda _argv: (
            b"",
            b"",
            0,
        )

        # Slice 3 surface: streaming exec.
        self.stream_exec_calls: list[dict[str, Any]] = []
        # Each entry is the list of (event_kind, payload) tuples to yield.
        self.stream_exec_scripts: list[list[tuple[str, Any]]] = []

    def health(self) -> bool:
        return True

    def ensure_runtime(self) -> None:
        return None

    def ensure_network(self) -> None:
        self.network_ensured = True

    def create_volume(self, volume_name: str, session_id: str, tenant_id: str) -> None:
        self.created_volumes.append((volume_name, session_id, tenant_id))

    def remove_volume(self, volume_name: str) -> None:
        self.removed_volumes.append(volume_name)

    def create_container(
        self,
        *,
        session_id: str,
        tenant_id: str,
        volume_name: str,
        limits: Limits,
    ) -> str:
        flags = hardening_flags(
            session_id=session_id,
            tenant_id=tenant_id,
            volume_name=volume_name,
            limits=limits,
            image=self._settings.sandbox_image,
            network=self._settings.network_name,
            dev_mode=self._settings.dev_mode,
        )
        self._counter += 1
        cid = f"container-{self._counter}"
        self.created_containers.append((cid, flags))
        return cid

    def start_container(self, container_id: str) -> None:
        self.started.append(container_id)

    def normalize_workspace_perms(self, container_id: str) -> None:
        # v0.1.7 — privileged in-container chown of /workspace. Tests
        # can inspect `workspace_perm_calls` to confirm ordering vs
        # start_container.
        if not hasattr(self, "workspace_perm_calls"):
            self.workspace_perm_calls = []
        self.workspace_perm_calls.append(container_id)

    def stop_container(self, container_id: str, timeout: int = 5) -> None:
        self.stopped.append((container_id, timeout))

    def remove_container(self, container_id: str) -> None:
        self.removed_containers.append(container_id)

    # Slice 6a — startup reconciliation. Default: every container the
    # fake has heard of is "present"; tests override `missing_containers`
    # to simulate a crashed-then-restarted daemon.
    missing_containers: set[str] = set()  # class-level default; overridden per-instance

    def container_exists(self, container_id: str) -> bool:
        # Per-instance override if set, otherwise a permissive default
        # so existing tests don't have to opt in.
        missing = getattr(self, "_missing_containers", set())
        return container_id not in missing

    # ----- slice 13a — orphan reaper helpers -----

    def list_containers_with_label(self, label_key: str) -> list[dict[str, Any]]:
        """Tests stage `orphan_containers` and `orphan_volumes` directly
        on the fake. Items shape mirrors the real DockerClient helper:
        `{name, id, created_epoch_s, labels}`."""
        items = getattr(self, "orphan_containers", [])
        return [it for it in items if label_key in (it.get("labels") or {})]

    def list_volumes_with_label(self, label_key: str) -> list[dict[str, Any]]:
        items = getattr(self, "orphan_volumes", [])
        return [it for it in items if label_key in (it.get("labels") or {})]

    # Slice 6b — resource sampler. Returns a fixed snapshot dict; tests
    # override `_stats_response` per-instance to inject specific values.
    def container_stats(self, container_id: str) -> dict[str, object]:
        if container_id in getattr(self, "_missing_containers", set()):
            return {}
        return getattr(
            self,
            "_stats_response",
            {
                "cpu_percent": 1.5,
                "memory_bytes": 64 * 1024 * 1024,
                "memory_limit_bytes": 2048 * 1024 * 1024,
                "blkio_read_bytes": 0,
                "blkio_write_bytes": 0,
            },
        )

    # ----- slice 2 -----

    def exec_in_container(
        self,
        *,
        container_id: str,
        argv: list[str],
        env: dict[str, str],
        timeout_s: int,
        stdin_bytes: bytes | None = None,
    ) -> ExecOutput:
        self.exec_calls.append(
            {
                "container_id": container_id,
                "argv": argv,
                "env": env,
                "timeout_s": timeout_s,
                "stdin_bytes": stdin_bytes,
            }
        )
        if self.exec_responses:
            return self.exec_responses.pop(0)
        return ExecOutput(stdout=b"", stderr=b"", exit_code=0, duration_ms=10)

    def exec_stream_in_container(
        self,
        *,
        container_id: str,
        argv: list[str],
        env: dict[str, str],
        timeout_s: int,
    ) -> "Any":
        self.stream_exec_calls.append(
            {
                "container_id": container_id,
                "argv": argv,
                "env": env,
                "timeout_s": timeout_s,
            }
        )
        script = self.stream_exec_scripts.pop(0) if self.stream_exec_scripts else [("exit", 0)]
        yield from script

    def _exec_simple(
        self, container_id: str, argv: list[str], *, user: str = "10001:10001"
    ) -> tuple[bytes, bytes, int]:
        self.simple_exec_calls.append((container_id, list(argv)))
        return self.simple_exec_handler(list(argv))

    def put_archive_file(
        self,
        *,
        container_id: str,
        abs_path: str,
        content: bytes,
        mode: int,
    ) -> None:
        self.put_archive_calls.append(
            {
                "container_id": container_id,
                "abs_path": abs_path,
                "content": content,
                "mode": mode,
            }
        )

    # ----- slice 11b — background-process supervisor -----

    def spawn_supervised(
        self,
        *,
        container_id: str,
        argv: list[str],
        env: dict[str, str] | None,
        cwd: str,
        pid_path: str,
        exit_path: str,
        log_path: str,
    ) -> str:
        ospid = self._next_ospid
        self._next_ospid += 1
        self._processes[ospid] = {
            "alive": True,
            "exit_code": None,
            "container_id": container_id,
            "exit_path": exit_path,
            "log_path": log_path,
        }
        # Mimic the real supervisor writing the pid file before exec.
        self._fs[(container_id, pid_path)] = f"{ospid}\n".encode()
        self.spawn_supervised_calls.append(
            {
                "container_id": container_id,
                "argv": list(argv),
                "env": dict(env) if env else None,
                "cwd": cwd,
                "pid_path": pid_path,
                "exit_path": exit_path,
                "log_path": log_path,
                "ospid": ospid,
            }
        )
        return f"exec-fake-{ospid}"

    def pid_alive(self, container_id: str, ospid: int) -> bool:
        state = self._processes.get(ospid)
        return bool(state and state["alive"])

    def signal_pid(self, container_id: str, ospid: int, sig: int) -> None:
        self.signal_pid_calls.append((container_id, ospid, sig))
        # SIGTERM (15) and SIGKILL (9) flip the process to exited
        # immediately in the simulator. Tests that want the SIGTERM
        # grace path can pre-set `immune_to_sigterm` on a process.
        if sig in (15, 9) and ospid in self._processes:
            state = self._processes[ospid]
            if state["alive"]:
                if sig == 15 and state.get("immune_to_sigterm"):
                    return
                state["alive"] = False
                # bash-style: 128 + signal number for signal-killed
                # processes; tests can override via simulate_process_exit.
                if state["exit_code"] is None:
                    state["exit_code"] = 128 + sig
                self._write_exit_file(state)

    def _write_exit_file(self, state: dict[str, Any]) -> None:
        """Mimic the supervisor's `trap 'echo $? > exit_path' EXIT`.
        Called whenever a process transitions to dead in the
        simulator."""
        exit_path = state.get("exit_path")
        cid = state.get("container_id")
        if exit_path and cid is not None:
            self._fs[(cid, exit_path)] = f"{state['exit_code']}\n".encode()

    def read_text_in_container(self, container_id: str, abs_path: str) -> str | None:
        data = self._fs.get((container_id, abs_path))
        if data is None:
            return None
        return data.decode("utf-8", errors="replace")

    def tail_text_in_container(
        self, container_id: str, abs_path: str, *, lines: int
    ) -> tuple[str, bool]:
        data = self._fs.get((container_id, abs_path))
        if data is None:
            return "", False
        text = data.decode("utf-8", errors="replace")
        last_lines = text.splitlines()[-lines:]
        return "\n".join(last_lines) + ("\n" if last_lines else ""), False

    def stream_log_lines(self, container_id: str, abs_path: str):
        data = self._fs.get((container_id, abs_path))
        if data:
            yield data
        # Test simulator stops after the snapshot — production uses
        # `tail -F` which would keep yielding. Tests can extend the
        # virtual fs by writing more bytes and re-driving the route.

    def write_log_in_container(self, container_id: str, abs_path: str, text: str) -> None:
        """Test helper — append to a virtual log file so MCP / SSE
        log endpoints have something to read."""
        existing = self._fs.get((container_id, abs_path), b"")
        self._fs[(container_id, abs_path)] = existing + text.encode("utf-8")

    def simulate_process_exit(self, ospid: int, exit_code: int = 0) -> None:
        """Test helper — flip a tracked process to EXITED with the
        given exit code, including writing the exit file the way the
        production supervisor's EXIT trap would."""
        if ospid not in self._processes:
            raise KeyError(f"ospid {ospid} not tracked")
        state = self._processes[ospid]
        state["alive"] = False
        state["exit_code"] = exit_code
        self._write_exit_file(state)

    def get_archive_file(self, *, container_id: str, abs_path: str) -> tuple[bytes, int]:
        resp = self.get_archive_responses.get(abs_path)
        if resp is None:
            raise FileNotFoundError(abs_path)
        if isinstance(resp, Exception):
            raise resp
        return resp


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        dev_mode=True,
        api_token="test-token",
        db_path=tmp_path / "test.db",
        audit_log_path=tmp_path / "audit.log",
    )


@pytest.fixture
def fake_docker(settings) -> FakeDockerClient:
    return FakeDockerClient(settings)


@pytest.fixture
def service(settings, fake_docker) -> SessionService:
    return SessionService(
        settings=settings,
        registry=Registry(settings.db_path),
        docker=fake_docker,
        audit=AuditEmitter(settings.audit_log_path),
    )


@pytest.fixture
def client(settings, service):
    # start_reaper=False keeps the test loop deterministic; reaper tests
    # invoke tick() directly via the `app.state.reaper` handle.
    app = create_app(settings, service=service, start_reaper=False)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def authed(client):
    client.headers.update({"Authorization": "Bearer test-token"})
    return client
