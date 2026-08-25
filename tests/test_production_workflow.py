from ai_video_factory.domain import Script
from ai_video_factory.workflows.production import prepare_generated_script, prepare_source_script


def test_prepare_source_script_uses_user_script_directly() -> None:
    source = prepare_source_script("Guion definitivo escrito y revisado manualmente.")

    assert source.text == "Guion definitivo escrito y revisado manualmente."


def test_generated_script_is_only_an_optional_adapter() -> None:
    generated = Script(
        title="Demo",
        hook="Un hook.",
        narration="Un hook. El resto del guion.",
    )

    source = prepare_generated_script(generated)

    assert source.text == generated.narration
