"""Per-session resource sampler tests (slice 6b). SPEC-501."""

import json

from api import metrics
from api.sampler import SessionSampler


def _build_sampler(settings, service):
    return SessionSampler(
        settings=settings,
        registry=service.registry,
        docker=service.docker,
        audit=service.audit,
    )


async def test_tick_emits_sample_per_running_session(authed, service, settings, fake_docker):
    sid = authed.post("/v1/sessions", json={}).json()["session_id"]
    sampler = _build_sampler(settings, service)

    await sampler.tick()

    audit_lines = settings.audit_log_path.read_text().splitlines()
    samples = [
        json.loads(line) for line in audit_lines if json.loads(line)["kind"] == "session.sample"
    ]
    assert len(samples) == 1
    assert samples[0]["session"] == sid
    payload = samples[0]["payload"]
    assert "cpu_percent" in payload
    assert "memory_bytes" in payload
    assert "blkio_read_bytes" in payload


async def test_tick_skips_missing_containers(authed, service, settings, fake_docker):
    authed.post("/v1/sessions", json={}).json()["session_id"]
    container_id = fake_docker.created_containers[0][0]
    fake_docker._missing_containers = {container_id}
    sampler = _build_sampler(settings, service)

    await sampler.tick()

    audit_lines = settings.audit_log_path.read_text().splitlines()
    samples = [line for line in audit_lines if json.loads(line)["kind"] == "session.sample"]
    assert samples == []
    error_count = metrics.resource_samples_total.labels(result="error")._value.get()
    assert error_count >= 1


async def test_disabled_sampler_does_not_start(settings, service):
    s = settings.model_copy(update={"resource_sample_interval_s": 0})
    sampler = _build_sampler(s, service)
    await sampler.start()
    assert sampler._task is None
    await sampler.stop()  # safe to call regardless


async def test_tick_with_no_sessions_is_noop(service, settings):
    # The other sampler tests piggy-back on the `client` fixture, whose
    # lifespan calls Registry.init(). Here we don't go through the API,
    # so the schema needs to be created explicitly.
    await service.registry.init()
    sampler = _build_sampler(settings, service)
    await sampler.tick()  # must not raise

    audit_lines = (
        settings.audit_log_path.read_text().splitlines() if settings.audit_log_path.exists() else []
    )
    samples = [line for line in audit_lines if json.loads(line)["kind"] == "session.sample"]
    assert samples == []
