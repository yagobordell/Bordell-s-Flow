from types import SimpleNamespace

from ai_video_factory.providers.ideogram_rejections import find_safety_rejection


class EmptyStorage:
    def stat(self, key: str):
        del key
        return None


def _request(job_id: str, request_sha256: str):
    return SimpleNamespace(
        job_id=job_id,
        fingerprint=lambda: request_sha256,
    )


def test_confirmed_e6_rejection_cannot_be_replayed() -> None:
    request_sha256 = "51cd84a1f9c48ae3ed2f0f51f912a8e7cc9ad734cfabf942eec048b10c06c79d"
    rejection = find_safety_rejection(
        EmptyStorage(),  # type: ignore[arg-type]
        _request(
            "ideogram-reference-e6a0d2f5296bab78dce70cfd30460fa6",
            request_sha256,
        ),  # type: ignore[arg-type]
    )

    assert rejection is not None
    assert rejection.transport_job_id == "f322302e-658c-40e5-9f62-4a2d708d9e80"
    assert rejection.detail == "Ideogram 4 safety filter blocked all deterministic caption variants"


def test_historical_e6_guard_is_bound_to_exact_request_sha() -> None:
    rejection = find_safety_rejection(
        EmptyStorage(),  # type: ignore[arg-type]
        _request(
            "ideogram-reference-e6a0d2f5296bab78dce70cfd30460fa6",
            "0" * 64,
        ),  # type: ignore[arg-type]
    )

    assert rejection is None


def test_unevaluated_6df_job_is_not_blacklisted() -> None:
    rejection = find_safety_rejection(
        EmptyStorage(),  # type: ignore[arg-type]
        _request(
            "ideogram-reference-6df976c29040693b83fd47f79f664249",
            "0" * 64,
        ),  # type: ignore[arg-type]
    )

    assert rejection is None
