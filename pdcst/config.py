"""
Base settings for pdcst.
"""
from math import isfinite
from pathlib import Path

from pydantic import BaseSettings, Field, validator


class Settings(BaseSettings):
    root: Path = Field(Path.home() / "pdcst", env="PDCST_HOME")

    audio_max_bytes: int = Field(2 * 1024**3, gt=0, env="PDCST_AUDIO_MAX_BYTES")
    audio_max_seconds: float = Field(6 * 60 * 60, gt=0, env="PDCST_AUDIO_MAX_SECONDS")

    @validator("audio_max_seconds")
    def finite_duration(cls, value):
        if not isfinite(value):
            raise ValueError("Audio duration limit must be finite")
        return value


settings = Settings()
