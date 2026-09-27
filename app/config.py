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
    # Auto-garant: an Apirone account of its own (USDT BEP20); deal money never mixes with listing payments.
    # The account number is a secret too: Apirone shows an account's history and balance to anyone who has it.
    escrow_apirone_account: SecretStr | None = None
    escrow_apirone_transfer_key: SecretStr | None = None
    timezone: str = "Europe/Moscow"
    data_dir: Path = Path("data")
    backup_passphrase: SecretStr | None = None
    backup_passphrase_old: SecretStr | None = None  # the one before a change: older archives still open
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
