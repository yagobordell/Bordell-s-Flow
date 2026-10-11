# Phase 4 — focused fixes without destroying a good plan

Evaluate the Python blocking errors and your own global editorial review
against the COMPLETE immutable original script, given as source groups
and the combined original audiovisual direction.
This is the SAME professional director correcting the whole video, NOT
an independent worker invoked per group.

Return ONE strict SparseCorrection object covering ALL actual issues:
- replaced_groups: complete integrated SparseGroup objects ONLY for groups
  whose text boundaries, beat IDs, or beat types need revision, or whose
  source annotations must change. Each replacement includes exact source
  text AND complete visual carrier decisions for that group.
- visual_patches: sparse carrier-specific visual changes ONLY for existing
  beat IDs with unchanged narration. Do not reproduce their source text.
  Python preserves those beats' original verified source narration.
Both arrays are mandatory even when empty.

When you receive actual errors, correct ALL of them coherently in this
single full-video correction response. No extra narrative-only or
visual-only planning passes. Return empty arrays only when no change is
actually warranted.

Narrative boundaries are chosen by YOU based on the original B1.2 rules;
Python cannot rewrite them for you. Once proposed, Python materializes the
new text slices and validates the entire script and the audiovisual plan.
No hardcoded beat pace, length-based automatic splitting, transition quota
or visual-percentage denominator manipulation is acceptable. All former
B2 professional constraints, CTA ebook reference protections, Haiku anchors,
and screen/document + motion rules remain mandatory.
