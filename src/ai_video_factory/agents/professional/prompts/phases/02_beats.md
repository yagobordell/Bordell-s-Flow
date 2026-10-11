# Phase 2a — ONE global YouTube narrative beat segmentation

This is the ORIGINAL professional B1.2 editorial segmentation job, now run
simultaneously on ALL groups from the full script. Read every group before
choosing its cuts. Apply the complete proven B1.2 methodology loaded in
rules/beat_segmentation.md, including its real viewing cadence, hook/rehook
logic, atomicity, rhythmic cohesion, examples, and both hard limits. Do NOT
invent new rhythm targets or make artificial microbeats to satisfy ratios.
A beat is a genuinely autonomous, complete narrative/visual impact.

CRITICAL: Python owns exact original source words. Do not echo or synthesize
beat.text. For each group, return block_id and an ordered set of cuts. Each
cut contains end_anchor (EXACT short source phrase ending that beat, including
its final punctuation where any), occurrence (1-based appearance after the
previous beat's end, normally 1), and the original B1.2 beat_type. Python
resolves these source anchors and includes separator whitespace with the
preceding beat. The last anchor of each group must end at its last original
substantive words. Do not set arbitrary source offsets or paraphrase.

Before emission, check that the proposed cuts cover EVERY source character,
that no beat exceeds 100 decoded stripped characters or 40 words, and that
the five original B1.2 boundary tests pass, without unnecessary rapid
microfragment runs. Return only the strict WholeScriptCuts object. Python
will materialize and validate the complete script before any visual model call.
