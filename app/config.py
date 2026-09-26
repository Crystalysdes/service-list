from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Config(BaseSettings):
    """Process configuration from environment / .env.

    Business settings live in the DB (see ``app.services.settings``).
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: SecretStr = SecretStr("")
    database_url: str = "postgresql+asyncpg://servicelist:servicelist@localhost:5432/servicelist"
    owner_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    cryptopay_token: SecretStr | None = None
    cryptopay_testnet: bool = False
    # Auto-garant: a Crypto Pay app of its own, so deal money never mixes with listing payments
    escrow_cryptopay_token: SecretStr | None = None
    escrow_cryptopay_testnet: bool | None = None  # None: as CRYPTOPAY_TESTNET
    timezone: str = "Europe/Moscow"
    data_dir: Path = Path("data")
    backup_passphrase: SecretStr | None = None
    log_level: str = "INFO"

    @field_validator("owner_ids", mode="before")
    @classmethod
    def _split_ids(cls, value: Any) -> Any:
        if value is None or value == "":
            return []
        if isinstance(value, int):
            return [value]
        if isinstance(value, str):
            return [int(part) for part in value.replace(";", ",").split(",") if part.strip()]
        return value

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"


@lru_cache
def get_config() -> Config:
    return Config()
