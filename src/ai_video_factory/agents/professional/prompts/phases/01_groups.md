# Phase 1 — Whole-script grouping

Read the entire raw script before deciding any boundary. Identify narrative_core.central_question and narrative_core.final_answer based exclusively on the script. Identify hook, meaningful rehooks, turning points and narrative phases as annotations anchored to source substrings, without inserting them into B1.1 block fields.

Return the same B1.1 block metadata structure: block_id (1..N), type (intro/development/pivot/correction/climax/close), emotional_entry, emotional_exit and span.first_words / span.last_words copied exactly from the source. Do NOT generate or paraphrase block text: Python materializes each block via those anchors and checks an exact full-script tiling. Groups represent independently completed narrative contributions; do not split by sentence, camera cut or arbitrary target length. Do not combine independently completed list items, cases or lessons merely because they share a topic.

Use submit_groups to check the boundaries. Correct anchors until the tool confirms a unique full-script tiling. Only then advance.
