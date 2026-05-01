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

    def stop_container(self, container_id: str, timeout: int = 5) -> None:
        self.stopped.append((container_id, timeout))

    def remove_container(self, container_id: str) -> None:
        self.removed_containers.append(container_id)

    # ----- slice 2 -----

    def exec_in_container(
        self,
        *,
        container_id: str,
        argv: list[str],
        env: dict[str, str],
        timeout_s: int,
    ) -> ExecOutput:
        self.exec_calls.append(
            {
                "container_id": container_id,
                "argv": argv,
                "env": env,
                "timeout_s": timeout_s,
            }
        )
        if self.exec_responses:
            return self.exec_responses.pop(0)
        return ExecOutput(stdout=b"", stderr=b"", exit_code=0, duration_ms=10)

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
    app = create_app(settings, service=service)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def authed(client):
    client.headers.update({"Authorization": "Bearer test-token"})
    return client
