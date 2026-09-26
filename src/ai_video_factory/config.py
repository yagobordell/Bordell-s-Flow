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
    openai_b_model: str = "gpt-6-luna"
    openai_b_reasoning_effort: str = "medium"

    # AI33 is retained for Fish/voice only; B2 images use the official OpenAI API.
    ai33_api_key: str | None = None
    ai33_fish_voice_id: str = "fishaudio_80e34d5e0b2b4577a486f3a77e357261"
    ai33_fish_speed: float = 0.9
    ai33_fish_poll_timeout_seconds: int = 3600
    ai33_fish_poll_interval_seconds: float = 8.0
    # Exactly one official image request per individual batch; local 30-minute fallback.
    openai_image_model: str = "gpt-image-2.5-flare"
    openai_image_size: str = "1280x720"
    openai_image_quality: str = "low"
    openai_image_batch_timeout_seconds: int = 1800
    openai_image_batch_poll_interval_seconds: float = 8.0
    openai_image_max_parallel: int = 4
    openai_service_tier: str = "flex"
    openai_fallback_service_tier: str = "default"
    qwen_image_21_model: str = "Qwen/Qwen-Image-2.1"
    ideogram4_model: str = "ideogram-ai/ideogram-4-nf4"

    breeze_tts_model: str = "BreezeBlue/Breeze-TTS-2"
    breeze_tts_voice: str = (
        "A warm, confident documentary narrator with a clear neutral English accent, "
        "natural low-mid register, crisp articulation, and restrained cinematic presence"
    )
    breeze_tts_cfg_scale: float = 4.0
    breeze_tts_seed: int = 42

    fish_speech_model: str = "fishaudio/s2-pro"
    fish_speech_seed: int = 42
    fish_speech_reference_profile: str | None = None
    fish_speech_reference_audio_key: str | None = None
    fish_speech_reference_audio_sha256: str | None = None
    fish_speech_reference_transcript: str | None = None

    whisper_model: str = "openai/whisper-large-v3-turbo"

    salad_api_key: str | None = None
    hf_token: str | None = None
    postgres_dsn: str | None = None

    r2_endpoint_url: str | None = None
    r2_bucket: str | None = None
    r2_access_key_id: str | None = None
    r2_secret_access_key: str | None = None

    inference_client_poll_seconds: float = 5.0
    inference_client_timeout_seconds: float = 3600.0

    output_dir: Path = Path("data/output")
    temp_dir: Path = Path("data/tmp")


settings = Settings()
