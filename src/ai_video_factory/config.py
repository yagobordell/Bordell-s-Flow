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
    openai_model: str = "gpt-5.6-luna"
    openai_reasoning_effort: str = "high"
    openai_service_tier: str = "flex"
    openai_fallback_service_tier: str = "default"
    openai_image_model: str = "gpt-image-2"
    openai_tts_model: str = "gpt-4o-mini-tts"
    openai_tts_voice: str = "marin"

    whisper_model: str = "openai/whisper-large-v3-turbo"

    salad_api_key: str | None = None
    salad_organization: str | None = None
    salad_project: str | None = None
    salad_whisper_queue_name: str = "ai-video-factory-whisper-jobs"

    r2_endpoint_url: str | None = None
    r2_bucket: str | None = None
    r2_access_key_id: str | None = None
    r2_secret_access_key: str | None = None

    inference_client_poll_seconds: float = 5.0
    inference_client_timeout_seconds: float = 3600.0

    ai33_api_key: str | None = None
    ai33_base_url: str | None = None

    output_dir: Path = Path("data/output")
    temp_dir: Path = Path("data/tmp")


settings = Settings()
