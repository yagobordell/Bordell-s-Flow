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

## Correct the underlying visual-decision error

For each reported visual problem, identify the viewer's missing experience
and correct the shot that carries it. Apply the same editorial distinction
used in planning: show unfolding narrated action with `media_video`; give
stable evidence or details a purposeful `media_image` view; restore the
speaking `avatar` where human guidance, direct address, interpretation or
personal connection has been lost. `avatar_media` remains available when
both visible components are helpful together.

When a static image or a grid has replaced a meaningful action, direct a
source-faithful moving shot with the correct participant, visible physical
progression and feasible continuous motion. Solve fragile motion through
better staging, not by changing the action into a photograph. When a moving
shot obscures important static information, provide a clear still or a
separate static detail where the narration supports it. When the narrator
has disappeared from an audience-facing passage, restore their natural
on-screen presence; when presenter footage conceals narrated events,
return the relevant scene to the screen. Preserve the voice-over unchanged.

Use `visual_patches` where the source interval and beat identity remain
correct; use `replaced_groups` only where source cuts/IDs or source-level
annotations truly need revision, and carry the revised cut and visual
choice together. Preserve actor identity, object state, sequence continuity,
precise reveals, authorised overlay notes and protected CTA assets.

Correct reference-frame continuity **across both** moving-shot starting
frames and static detail images, including when the narrator temporarily
returns between parts of the same scene. Plan them as one coherent
chronological visual sequence and place all generated assets from that
continuing scene into the same numbered grid when shared conditioning is
useful. Preserve `media_video` for movement and do not substitute stills
for actions merely to simplify the grid.

When an editor note is at fault, replace the affected beat's
`editor_notes` with a **plain imperative string ordering a concrete
postproduction addition**. Empty the string when no edit is needed.
Remove instructions that merely ask for inspection, verification, checking
source assets, evaluating brand visibility or general visual advice.
Keep beat IDs and original narration unchanged.\n
Review the edited sequence after correction: each moving event must unfold,
static information must be inspectable, and the human storyteller must
remain present at narratively useful moments. Editorial balance is judged
by meaning and viewing experience, not by carrier percentages or filler.

## Targeted corrections to audiovisual rhythm

Treat each B1.1 group `type` and its emotional entry/exit as frozen
context. Apply the six block-sensitive YouTube pacing profiles in
`phases/02_planning.md` when addressing rhythm-related review issues;
the type may inform editing choices but is not a frequency-of-cuts quota.
Preserve the already-approved group boundaries, IDs, original wording and
narrative order.

For every specific pacing finding, diagnose whether the viewer needs
more intelligible continuous action, a more useful static detail, a
meaningful return to the presenter, a sustained view, or a corrected
shot-level source boundary. Add still subshots only where separate stable
views improve comprehension; changing static subshots does not replace
moving action. Choose the smallest correction
that improves hook clarity, forward momentum, comparisons, reveal impact
or the final landing while retaining scene and prop continuity.

If source beat boundaries or beat IDs change, use `replaced_groups` with
every revised beat's exact source coverage **and** visual carrier choice.
If the source span and beat identity are already correct and the issue is
visual pacing, use `visual_patches` for the appropriate same-ID carrier
or image-prompt changes. `media_image` subshots keep one beat ID and
divide its actual narrated interval equally after STT; preserve the
current 1–4 image contract, >=70-character rule and CTA exemption.

When a rhythm map is supplied from prior validation, read its
script-character-relative `between_beat_changes` and
`within_beat_changes` as descriptive feedback. It is not a seconds clock
and does not certify editorial quality. Correct the observed on-screen
problem rather than manipulating the number of beats or changes. Recheck
the intended per-block pacing, the transitions into adjacent groups and
the whole-video visual arc after the targeted revision.



## Grid continuity repairs

When a correction concerns scene continuity, repair the grid assignment together with the beat carrier decisions. If two generated assets belong to the same continuing scene, they may be merged into one numbered grid even when one is a still, another is a video starting frame, and another is an `avatar_media` support still. Do not preserve an incorrect split merely because the carriers differ or because an `avatar` beat sits between them.


When repairing grid metadata, preserve or rebuild the global letter order of
logical grids across the full video. Ensure that every corrected grid contains
at least two generated assets and that each asset receives the proper sequence
label (`A1`, `A2`..., `B1`, `B2`...). Remove any mistaken one-asset grid by
making that asset independent instead.
