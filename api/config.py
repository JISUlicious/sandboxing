from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SANDBOX_", extra="ignore")

    dev_mode: bool = False
    api_token: str = ""
    bind_host: str = "127.0.0.1"
    bind_port: int = 8000

    db_path: Path = Path("./var/sandbox.db")
    audit_log_path: Path = Path("./var/audit.log")

    sandbox_image: str = "sandbox-runtime:latest"
    network_name: str = "sandbox_egress"

    default_vcpu: int = 2
    default_memory_mib: int = 2048
    default_workspace_mib: int = 1024
    default_pids: int = 256
    default_nofile: int = 1024
    default_exec_timeout_s: int = 60

    idle_stop_minutes: int = 15
    hard_destroy_hours: int = 24

    tenant_max_concurrent: int = 50


settings = Settings()
