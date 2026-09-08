from pydantic import BaseModel, Field


class VideoClip(BaseModel):
    """Persisted generated video asset bound to one canonical shot."""

    shot_id: int = Field(ge=1)
    uri: str = Field(min_length=1)
