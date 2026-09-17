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

    ideogram4_model: str = "ideogram-ai/ideogram-4-nf4"
    flux_schnell_model: str = "black-forest-labs/FLUX.1-schnell"

    breeze_tts_model: str = "BreezeBlue/Breeze-TTS-2"
    breeze_tts_voice: str = (
        "A warm, confident documentary narrator with a clear neutral English accent, "
        "natural low-mid register, crisp articulation, and restrained cinematic presence"
    )
    breeze_tts_cfg_scale: float = 4.0
    breeze_tts_seed: int = 42

    whisper_model: str = "openai/whisper-large-v3-turbo"

    salad_api_key: str | None = None
    salad_organization: str | None = None
    salad_project: str | None = None
    salad_breeze_tts2_queue_name: str = "ai-video-factory-breeze-tts2-jobs"
    salad_ideogram4_queue_name: str = "ai-video-factory-ideogram4-jobs"
    salad_flux_schnell_queue_name: str = "ai-video-factory-flux-schnell-jobs"
    salad_whisper_queue_name: str = "ai-video-factory-whisper-jobs"

    r2_endpoint_url: str | None = None
    r2_bucket: str | None = None
    r2_access_key_id: str | None = None
    r2_secret_access_key: str | None = None

    inference_client_poll_seconds: float = 5.0
    inference_client_timeout_seconds: float = 3600.0
    flux_fallback_pending_timeout_seconds: float = 1800.0

    ai33_api_key: str | None = None
    ai33_base_url: str | None = None

    output_dir: Path = Path("data/output")
    temp_dir: Path = Path("data/tmp")


settings = Settings()
