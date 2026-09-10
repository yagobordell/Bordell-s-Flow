from pydantic import BaseModel, Field


class VideoClip(BaseModel):
    """Persisted generated video asset bound to one canonical shot."""

    shot_id: int = Field(ge=1)
    uri: str = Field(min_length=1)


class FinalVideo(BaseModel):
    """Persisted final audiovisual artifact emitted by the compositor."""

    uri: str = Field(min_length=1)
    duration_seconds: float = Field(gt=0)
