from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    log_level: str = "INFO"

    openai_api_key: str | None = None
    openai_model: str = "gpt-5.4-mini"
    openai_image_model: str = "gpt-image-2"
    openai_tts_model: str = "gpt-4o-mini-tts"
    openai_tts_voice: str = "marin"

    ai33_api_key: str | None = None
    ai33_base_url: str | None = None

    output_dir: Path = Path("data/output")
    temp_dir: Path = Path("data/tmp")


settings = Settings()
