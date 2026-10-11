# Editor notes — actual postproduction operations only

Each beat carries an `editor_notes` **string**. Write one or more concise,
direct, positive commands to **add an explicit editing element** at that beat.
Use `""` when nothing needs postproduction editing. The beat ID and its
original narration interval provide the source anchor; real timings follow
the STT alignment. This is an instruction to the editor, not an input to
image or video generation.

Order additions of deliberately designed overlay text, graphical
annotations, compositing or other concrete edits when they serve the
spoken narrative. Make the text and appearance specific, grounded in the
original narration or approved assets. Never put image quality checks,
verification steps, branding inspections, general advice, visual
composition reminders or questions about whether something is already
visible into `editor_notes`.

Do not create ad hoc brand-identity notes merely because a brand occurs
in the narration. Branding that belongs inside the underlying scene is
handled through scene and reference assets, not by an instruction to inspect
it. The QR and website overlays for protected ebook CTAs are code-owned:
the editor must not duplicate them.

The output field is one plain text string on the beat. Legacy group-side
BeatNote objects may be read for backward compatibility, but the new
agent should not emit them. Source narration remains immutable.
