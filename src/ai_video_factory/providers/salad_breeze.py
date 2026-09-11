from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from ai_video_factory.inference.contracts import InferenceJobRequest, ObjectOutput
from ai_video_factory.workers.breeze_tts2 import (
    BREEZE_TTS2_GENERATION_PROFILE,
    BREEZE_TTS2_MODEL_ID,
    BREEZE_TTS2_TASK,
    breeze_application_job_id,
)

from .inference_jobs import InferenceJobExecutor
from .speech import GeneratedSpeech, SpeechFormat


class SaladBreezeSpeechProvider:
    """Speech provider backed by the dedicated Salad Breeze TTS 2 queue."""

    def __init__(
        self,
        *,
        executor: InferenceJobExecutor,
        temp_dir: Path,
        cfg_scale: float = 4.0,
        seed: int = 42,
    ) -> None:
        if not 0.0 < cfg_scale <= 10.0:
            raise ValueError("Breeze cfg_scale must be greater than 0 and at most 10")
        if not 0 <= seed <= 2_147_483_647:
            raise ValueError("Breeze seed is outside the supported range")
        self._executor = executor
        self._temp_dir = temp_dir
        self._cfg_scale = cfg_scale
        self._seed = seed

    async def generate_speech(
        self,
        *,
        text: str,
        model: str,
        voice: str,
        instructions: str,
        speed: float,
        output_format: SpeechFormat,
    ) -> GeneratedSpeech:
        return await asyncio.to_thread(
            self._generate_speech_sync,
            text=text,
            model=model,
            voice=voice,
            instructions=instructions,
            speed=speed,
            output_format=output_format,
        )

    def _generate_speech_sync(
        self,
        *,
        text: str,
        model: str,
        voice: str,
        instructions: str,
        speed: float,
        output_format: SpeechFormat,
    ) -> GeneratedSpeech:
        if model != BREEZE_TTS2_MODEL_ID:
            raise ValueError(f"Breeze provider requires model {BREEZE_TTS2_MODEL_ID!r}")
        if output_format != "wav":
            raise ValueError("Breeze provider currently supports only WAV output")

        job_id = breeze_application_job_id(
            text=text,
            voice=voice,
            instructions=instructions,
            speed=speed,
            cfg_scale=self._cfg_scale,
            seed=self._seed,
            model_id=model,
        )
        request = InferenceJobRequest(
            job_id=job_id,
            task=BREEZE_TTS2_TASK,
            output=ObjectOutput(
                key=f"jobs/{job_id}/narration.wav",
                content_type="audio/wav",
            ),
            parameters={
                "generation_profile": BREEZE_TTS2_GENERATION_PROFILE,
                "model_id": model,
                "text": text,
                "voice": voice,
                "instructions": instructions,
                "speed": speed,
                "cfg_scale": self._cfg_scale,
                "seed": self._seed,
            },
        )
        response = self._executor.execute(
            request,
            metadata={"phase": "5", "provider": "breeze-tts2"},
        )

        self._temp_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self._temp_dir) as directory:
            destination = Path(directory) / "narration.wav"
            self._executor.download_output(response, destination)
            content = destination.read_bytes()

        if not content:
            raise RuntimeError("Breeze TTS 2 worker returned an empty WAV artifact")
        return GeneratedSpeech(
            content=content,
            media_type="audio/wav",
            extension="wav",
        )
