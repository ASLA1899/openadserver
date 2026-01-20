"""
Configuration management using Pydantic Settings.

Supports loading from environment variables and YAML files.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseModel):
    """Database configuration."""

    host: str = "localhost"
    port: int = 5432
    name: str = "liteads"
    user: str = "liteads"
    password: str = ""
    pool_size: int = 10
    max_overflow: int = 20

    @property
    def async_url(self) -> str:
        """Get async database URL."""
        return f"postgresql+asyncpg://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"

    @property
    def sync_url(self) -> str:
        """Get sync database URL."""
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"


class RedisSettings(BaseModel):
    """Redis configuration."""

    host: str = "localhost"
    port: int = 6379
    db: int = 0
    password: str = ""
    pool_size: int = 10

    @property
    def url(self) -> str:
        """Get Redis URL."""
        if self.password:
            return f"redis://:{self.password}@{self.host}:{self.port}/{self.db}"
        return f"redis://{self.host}:{self.port}/{self.db}"


class ServerSettings(BaseModel):
    """Server configuration."""

    host: str = "0.0.0.0"
    port: int = 8000
    workers: int = 4
    reload: bool = False


class AdServingSettings(BaseModel):
    """Ad serving configuration."""

    default_num_ads: int = 1
    max_num_ads: int = 10
    timeout_ms: int = 50
    enable_ml_prediction: bool = False
    model_path: str = ""  # Path to CTR model for prediction
    # Use HTTP Referer header as fallback for page_url when not provided.
    # This enables domain targeting for iframe embeds. Set to False to disable.
    use_referer_for_targeting: bool = True


class FrequencySettings(BaseModel):
    """Frequency control configuration."""

    default_daily_cap: int = 3
    default_hourly_cap: int = 1
    ttl_hours: int = 24


class MLSettings(BaseModel):
    """ML model configuration."""

    model_dir: str = "./models"
    ctr_model: str = "deepfm_v1"
    cvr_model: str = "deepfm_cvr_v1"
    embedding_dim: int = 8
    batch_size: int = 128


class LoggingSettings(BaseModel):
    """Logging configuration."""

    level: str = "INFO"
    format: Literal["json", "console"] = "json"


class MonitoringSettings(BaseModel):
    """Monitoring configuration."""

    enabled: bool = True
    prometheus_port: int = 9090


class Settings(BaseSettings):
    """Main application settings."""

    model_config = SettingsConfigDict(
        env_prefix="LITEADS_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # Application
    app_name: str = "LiteAds"
    app_version: str = "0.1.0"
    debug: bool = False
    env: Literal["dev", "prod", "test"] = "dev"

    # Nested settings
    server: ServerSettings = Field(default_factory=ServerSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    ad_serving: AdServingSettings = Field(default_factory=AdServingSettings)
    frequency: FrequencySettings = Field(default_factory=FrequencySettings)
    ml: MLSettings = Field(default_factory=MLSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    monitoring: MonitoringSettings = Field(default_factory=MonitoringSettings)

    @field_validator("env")
    @classmethod
    def validate_env(cls, v: str) -> str:
        if v not in ("dev", "prod", "test"):
            raise ValueError(f"Invalid environment: {v}")
        return v


def load_yaml_config(config_path: Path) -> dict:
    """Load configuration from YAML file."""
    if not config_path.exists():
        return {}

    with open(config_path) as f:
        return yaml.safe_load(f) or {}


def merge_configs(base: dict, override: dict) -> dict:
    """Deep merge two configuration dictionaries."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = merge_configs(result[key], value)
        else:
            result[key] = value
    return result


@lru_cache
def get_settings() -> Settings:
    """
    Get application settings.

    Loads from environment variables with LITEADS_ prefix.
    Nested settings use __ delimiter (e.g., LITEADS_REDIS__HOST).
    """
    return Settings()


# Convenience alias
settings = get_settings()
