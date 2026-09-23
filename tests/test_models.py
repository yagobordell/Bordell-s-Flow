from ai_video_factory.domain import Beat, NarrativeBlock, Scene, SourceScript

def test_source_script_is_minimal_production_input() -> None:
    source = SourceScript(text="Un guion ya revisado y listo para producción.")

    assert source.model_dump() == {"text": "Un guion ya revisado y listo para producción."}

def test_narrative_planning_contracts_stay_minimal() -> None:
    block = NarrativeBlock(id=1, text="Los clanes guerreros empiezan a ganar poder.")
    beat = Beat(id=1, block_id=block.id, action="Los clanes acumulan poder político.")
    scene = Scene(id=1, beat_ids=[beat.id])

    assert block.model_dump() == {
        "id": 1,
        "text": "Los clanes guerreros empiezan a ganar poder.",
    }
    assert beat.model_dump() == {
        "id": 1,
        "block_id": 1,
        "action": "Los clanes acumulan poder político.",
    }
    assert scene.model_dump() == {"id": 1, "beat_ids": [1]}

