from pathlib import Path

DOWNLOAD_SCRIPT = Path("docker/workers/whisper/download_models.sh")


def test_whisper_download_uses_staging_before_promotion() -> None:
    text = DOWNLOAD_SCRIPT.read_text(encoding="utf-8")

    assert 'staging_root="${model_root}.staging"' in text
    assert 'rm -rf "${staging_root}"' in text
    assert '--local-dir "${staging_root}"' in text
    assert '[[ ! -s "${staging_root}/config.json" ]]' in text
    assert 'find "${staging_root}" -type f -name \'*.safetensors\'' in text
    assert 'rm -rf "${model_root}"' in text
    assert 'mv "${staging_root}" "${model_root}"' in text
    assert text.index('--local-dir "${staging_root}"') < text.index(
        'mv "${staging_root}" "${model_root}"'
    )
