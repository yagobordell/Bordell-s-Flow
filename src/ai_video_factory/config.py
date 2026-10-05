from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "config/app.env", "config/salad.env"),
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    app_env: str = "development"
    log_level: str = "INFO"

    openai_api_key: str | None = None
    gemini_api_key: str | None = None
    openai_model: str = "gpt-6-luna"
    openai_reasoning_effort: str = "medium"

    # AI33 is retained for Fish/voice only; B2 images use the official OpenAI API.
    ai33_api_key: str | None = None
    ai33_fish_voice_id: str = "fishaudio_80e34d5e0b2b4577a486f3a77e357261"
    ai33_fish_speed: float = 0.9
    ai33_fish_poll_timeout_seconds: int = 3600
    ai33_fish_poll_interval_seconds: float = 8.0
    # B2 images use one standard official OpenAI request per image.
    openai_image_model: str = "gpt-image-2.5-flare"
    openai_image_size: str = "1280x720"
    openai_image_quality: str = "low"
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
    image_upscaler_url: str | None = None
    image_upscaler_auto_start: bool = True
    image_upscaler_auto_stop: bool = True
    hf_token: str | None = None
    postgres_dsn: str | None = None
    # The capacity leader may require a session DSN distinct from the worker pool.
    salad_capacity_controller_postgres_dsn: str | None = None

    r2_endpoint_url: str | None = None
    r2_bucket: str | None = None
    r2_access_key_id: str | None = None
    r2_secret_access_key: str | None = None

    inference_client_poll_seconds: float = 5.0
    inference_client_timeout_seconds: float = 3600.0

    # YouTube publishing is optional and never loads credentials at application startup.
    youtube_enabled: bool = False
    youtube_keystore_dir: Path | None = None
    youtube_channel: str | None = None
    # Share this directory across every publisher using the same channel/MP4.
    # Default is the external sibling of YOUTUBE_KEYSTORE_DIR.
    youtube_state_dir: Path | None = None
    youtube_default_privacy: Literal["private", "unlisted", "public"] = "private"
    youtube_default_category_id: str = Field(default="22", pattern=r"^[0-9]+$")
    youtube_default_language: str | None = None
    youtube_made_for_kids: bool = False
    youtube_contains_synthetic_media: bool = True
    youtube_notify_subscribers: bool = False
    youtube_embeddable: bool = True
    youtube_public_stats_viewable: bool = True
    youtube_license: Literal["youtube", "creativeCommon"] = "youtube"

    @field_validator("youtube_channel")
    @classmethod
    def validate_youtube_channel(cls, value: str | None) -> str | None:
        if value is None:
            return None
        from ai_video_factory.integrations.youtube.store import validate_channel_name

        try:
            return validate_channel_name(value)
        except ValueError as exc:
            raise ValueError("Invalid YOUTUBE_CHANNEL") from exc
        except RuntimeError as exc:
            raise ValueError("Invalid YOUTUBE_CHANNEL") from exc

    output_dir: Path = Path("data/output")
    temp_dir: Path = Path("data/tmp")


settings = Settings()
