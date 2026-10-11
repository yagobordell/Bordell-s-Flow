# Professional carrier / speaking authority

## Editorial carrier judgment — voice-over, story and human connection

The presenter speaks the entire original script as the video's voice-over.
The editor creates the accompanying visual story, using the presenter image
to renew personal connection with the audience at meaningful intervals and
using depicted material whenever the viewer benefits from seeing the story.
First decide whether the shot's main contribution is unfolding time,
static information, direct human communication, or complementary
simultaneous communication. Then select the carrier:

- **`media_video` — action in time.** Show source-grounded bodily action,
  interaction, movement, changing expression, response, manipulation,
  physical process or evolving consequence as a feasible moving shot. The
  viewer should see what happens as it happens. Motion is a primary mode of
  narrative B-roll, not a last resort after still images.
- **`media_image` — meaningful stable view.** Give readable space and focus
  to a still detail, object, evidence, fixed comparison, established state,
  spatial relationship or composed concept. An image may isolate a crucial
  observation within a surrounding moving sequence. Its multiple still
  subshots provide distinct static views; they do not stand in for the
  movement that defines an action.
- **`avatar` — visible narrator connection.** Reintroduce the original
  speaking presenter naturally as the human guide when the voice is
  addressing the viewer, interpreting, teaching, evaluating, warning,
  reassuring, posing a direct question, drawing conclusions or giving a
  personal invitation. The presenter's screen presence is an active part
  of the video's communication, not merely a technical speaking option.
  When an explanation is better understood by watching its subject, the
  narrator's voice continues over full-screen coverage.
- **`avatar_media` — meaningful shared attention.** Show the original
  speaking presenter with a supporting still when the simultaneous human
  address and visible information genuinely strengthen each other, without
  crowding material that needs full-screen inspection.

Context determines what a shot must communicate: speech alone does not make
an event the speaker's action, and a narrative `beat_type` does not prescribe
its carrier. Direct connected views as a coherent sequence and let the
narrator return naturally between filmed developments. Judge distribution
and duration by editorial effectiveness across the whole video, without
numerical targets or compulsory alternation.

## Reference-frame continuity is independent of the carrier

Treat connected generated assets from the same scene as one numbered grid
whenever shared conditioning will improve continuity. The grid may include:

- `media_image` stills,
- `media_video` starting frames, and
- `avatar_media` supporting stills.

These assets may be separated by other beats and may mix carriers freely. The
grid follows scene continuity and reveal order, not beat adjacency and not a
single-carrier rule. A temporary `avatar` beat adds no generated cell, but it
does not end or invalidate the same grid before and after that beat.

Do not downgrade action to a still to make it "grid eligible". Keep actions as
`media_video`, static information as `media_image`, simultaneous presenter-plus-
visual moments as `avatar_media`, and direct-to-audience delivery as `avatar`.
The grid organizes the continuity of generated assets across those decisions.


Grid membership is expressed as logical asset labels such as `A1`, `A2`, `A3`.
The letter identifies the continuity grid; the index identifies the asset's
position inside that grid. Write the prefix `A1. `, `A2. `... directly on
each generated still description, video starting-frame prompt or
avatar_media support-image prompt. The orchestrator uses the prefix to
connect assets across all groups and carriers, independently of adjacency.
Only continuities with at least two generated assets become grids.
A single generated asset stays outside any grid.

The following contracts describe how to encode an already-chosen carrier.
They preserve existing source, audio, reference, image, grid and CTA limits.

For OpenAI SparseDirection responses, the rules below describe the final
Python-produced B2 carrier, NOT a request to emit nullable legacy fields.
The model emits only carrier-specific beat fields and exact beat.text.
Python derives audio=true for avatar/avatar_media, audio=false otherwise,
and references from group-level presenter_references and CTA annotations.
Do not output prompt=null, video_prompt=null, image_prompts=null,
end_anchor or occurrence on any model beat.

The following sections are carried verbatim from the current Professional B2 source file. They remain authoritative unless contradicted by the current Python contracts.

Absolute carrier and audio contracts

- avatar: reuse ONLY the original speaking avatar, prompt=null, video_prompt=null, reference=false, audio=true. Do not generate alternative speaking start images.
- avatar_media: show the ORIGINAL speaking avatar alongside one generated supporting still; prompt nonempty, video_prompt=null, audio=true. Its image prompt describes the SUPPORT image only.
- media_image: full-screen generated B-roll stills. Set prompt=null, video_prompt=null, audio=false, image_prompts to 1–4 nonempty self-contained still descriptions. For narration of 70 or more stripped characters, assign 2–4 distinct stills (CTA exemption preserves its single asset). Every still belongs to the SAME beat and receives equal STT-frame duration. For the new mixed-carrier grids, prefix each actual generated image prompt with A1., A2., etc., and omit generation_mode; the legacy field is still accepted on old plans. All other visual carriers use image_prompts=null.
- media_video: full-screen generated B-roll clip, prompt for one photographable starting frame, nonempty video_prompt for subsequent motion, ALWAYS audio=false.

Only avatar and avatar_media speak. Generated media_image and media_video are unsynchronized coverage with voice-over. Never depict anyone speaking to camera, delivering narration, silently lip-syncing or acting as a replacement direct-to-camera host in generated B-roll. Generated presenter B-roll is allowed when the narrator is truly the depicted participant in the story; then use reference=true to preserve their identity BUT locate the scene in a clearly different room or physical location from the avatar reference. Never generate a new image of the narrator in the original reference room, regardless of angle, gesture or crop. A narrator identity reference is not a room/setup reference. Do not assign another person's actions to the presenter. Otherwise reference=false.

Narrator-run cap and final close

A maximal consecutive run of audio=true beats across block boundaries has a HARD limit of 120 characters (sum of each beat's text.strip() length). Use a meaningfully justified audio=false B-roll beat to interrupt an overlong run, without changing any text, ID or boundary. The last beat of any close block MUST be untouched base avatar: avatar, prompt=null, video_prompt=null, reference=false, audio=true.

The generated visuals must not recreate a speaking presenter in the original narration room. A visual_type describes the carrier, not the identity of characters in a scene.
