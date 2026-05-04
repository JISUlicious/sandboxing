from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SANDBOX_", extra="ignore")

    dev_mode: bool = False
    api_token: str = ""
    bind_host: str = "127.0.0.1"
    bind_port: int = 8000

    # Slice 7 — multi-tenant identity (SPEC-405).
    # token_pepper is HMAC'd with the bearer token to produce the hash
    # stored in the tokens table; rotating it invalidates ALL tokens
    # so set it once and never change. Generate via:
    #     openssl rand -hex 32
    # Required when multi-tenant is in play; left empty in unit-test
    # configs so the default ("") is hashed deterministically.
    token_pepper: str = ""
    # Grace period after rotation during which the old token still
    # authenticates. SPEC-405 has no specific number; 5 minutes is
    # a reasonable client refresh window.
    token_grace_seconds: int = 300

    db_path: Path = Path("./var/sandbox.db")
    audit_log_path: Path = Path("./var/audit.log")
    audit_fallback_log_path: Path | None = None  # default: <audit_log>.fallback.jsonl
    audit_buffer_timeout_s: float = 5.0  # ARCH §7

    sandbox_image: str = "sandbox-runtime:latest"
    network_name: str = "sandbox_egress"
    # Proxy URL injected as HTTP(S)_PROXY into every sandbox. Use an IP
    # if you're on a runtime where Docker's embedded DNS can't be
    # reached from inside the sandbox (e.g., some gVisor versions).
    egress_proxy_url: str = "http://proxy:3128"

    default_vcpu: int = 2
    default_memory_mib: int = 2048
    default_workspace_mib: int = 1024
    default_pids: int = 256
    default_nofile: int = 1024
    default_exec_timeout_s: int = 60

    idle_stop_minutes: int = 15
    hard_destroy_hours: int = 24
    reaper_interval_s: int = 60
    # SPEC-501 — per-session cpu/mem/blkio samples. Set to 0 to disable.
    resource_sample_interval_s: int = 10

    tenant_max_concurrent: int = 50

    # SPEC-302 quota hooks. Both empty by default — no-op in dev mode.
    # In production point these at the deploy/xfs-quota-{setup,teardown}.sh
    # examples (or any script that takes SESSION_ID, VOLUME_NAME,
    # VOLUME_PATH, WORKSPACE_MIB env vars and returns 0 on success).
    quota_setup_cmd: str = ""
    quota_teardown_cmd: str = ""
    # Mountpoint where per-session volume directories live (passed to
    # the quota scripts as VOLUME_BASE).
    quota_volume_base: Path = Path("/var/lib/sandbox-volumes")


settings = Settings()
