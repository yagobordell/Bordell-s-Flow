# Editor notes — executable postproduction instructions only

`editor_notes` is the sidecar plan for **actual editing operations** applied
after the picture and narration have been planned. Its content is a direct,
positive order to the editor to **add or change an explicit postproduction
element** at a source-anchored point. An editor note is not a visual prompt,
a production checklist, a quality-control request or a reminder about
what should already be visible in a generated image or clip.

## Content of each instruction

Write the `instruction` value as **one clear string** containing an
unambiguous editing command. Specify the actual operation, the graphic,
text or other editorial element to add, and its intended appearance or
placement when necessary. An exact on-screen phrase must be grounded in
the immutable narration or an approved asset. Only create a note when a
real postproduction action is warranted by the editorial result.

Use direct imperative language. Notes must neither ask the editor to check,
verify, ensure, inspect, review or judge whether the generated content
already contains something nor leave a conditional decision to an editor.
Do not issue general instructions about consistency, physical plausibility,
identity, source authenticity, readability or screen composition as notes:
those belong to visual direction, asset preparation or editorial review.
Do not create `brand_identity` or similar check/reminder notes merely
because the narration names a brand or a screen should depict an identity.
When a brand asset itself must be included in the generated scene, that is
an asset-conditioning or composition requirement, not automatically an
overlay request. An actual graphical overlay may be ordered only when
it is deliberately part of the final edit.

## Existing schema and timing

The present `SparseDirection` contract requires a **group-level array of
BeatNote objects**, not a scalar `editor_notes` string. Each entry uses
`beat_id`, `kind`, `instruction` and `trigger`; **`instruction` is the
string containing the actual editorial order**. Keep the envelope because
strict structured output otherwise rejects the response. Do not create
beat-level `editor_notes` fields or invent an alternative JSON schema.
Changing the whole `editor_notes` field into one string would require a
separate approved change to the Python schema and its consumers.

A timed note uses the exact source words of its own beat with the existing
`trigger.text` and 1-based `trigger.occurrence` fields. An untimed editorial
operation may use `trigger=null` where the contract permits it, but it
does not claim a timed word-level event. The actual timestamps come from
STT; Haiku selects approved Remotion components and 3×3 placement
anchors. Any separately generated editorial images belong to the later
asset workflow, rather than the source image/video prompt.
The protected QR and CTA website overlays remain code-owned; do not
recreate or duplicate them in generated prompts or editor notes.

The narration is immutable. Output no editor note when there is no
specific additive editing instruction to perform.
