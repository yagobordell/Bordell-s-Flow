# Phase 1 — Whole-script grouping

Read the entire raw script before deciding any boundary. Identify narrative_core.central_question and narrative_core.final_answer based exclusively on the script. Identify hook, meaningful rehooks, turning points and narrative phases as annotations anchored to source substrings, without inserting them into B1.1 block fields.

Return the same B1.1 block metadata structure: block_id (1..N), type (intro/development/pivot/correction/climax/close), emotional_entry, emotional_exit and span.first_words / span.last_words copied exactly from the source. Do NOT generate or paraphrase block text: Python materializes each block via those anchors and checks an exact full-script tiling. Groups represent independently completed narrative contributions; do not split by sentence, camera cut or arbitrary target length. Do not combine independently completed list items, cases or lessons merely because they share a topic.

After the boundaries are settled, execute the whole-script `type` decision
pass in `rules/grouping.md`. For EACH block determine the completed
macro-function from its text, compare it with the surrounding narrative,
discriminate plausible competing labels (especially development/pivot,
pivot/correction and climax/close), and audit the ordered set of types.
Use the established emotional entry/exit as grounded narrative context,
never as a substitute for macro-function. Preserve all six allowed types
and positional eligibility; no cut counts, timing or visual instructions
are decided in this phase. The assigned type is authoritative context for
the later block-sensitive YouTube pacing decisions.

Return the strict structured GroupProposal with narrative_core and block metadata ONLY. Do not call any tools during this phase and do not include full group texts, explanations, or duplicated source text. Python materializes and validates the anchors immediately after your SINGLE response; it asks for a corrected response only if the anchors are actually invalid.
