# Visual continuity, numbered grids and cross-carrier reference assets

## A grid is a continuity sequence of generated assets

Plan each connected passage as **one coherent visual sequence** derived from
what the script asks the viewer to see. Establish the people, place,
wardrobe, props, object states, camera logic and reveal order. Every later
related asset must preserve those established facts until the narration
establishes a change.

A **grid** is the numbered continuity sequence for that scene. It may include
any generated visual asset that belongs to the same scene continuity:

- each `media_image` still;
- the starting frame of each `media_video` beat; and
- the supporting still of each `avatar_media` beat.

The grid is **not** limited to still-only beats. It is **not** limited to one
carrier type. It is **not** limited to adjacent beats. If a connected scene
begins in one beat and continues later, every generated asset from that scene
may remain in the same grid when shared conditioning is helpful.

A plain `avatar` beat generates no new image cell, but it does not end,
forbid or reset the numbered grid of the surrounding scene.

## Numbered order

Treat the grid as one logical numbered chain of generated assets. Numbering
expresses the continuity order of the generated references used for
conditioning. The ordering follows the scene's chronology and editorial reveal,
not the distinction between still and moving carriers.

A later generated asset in the same scene takes the next number whether it is:

- a still image,
- a video starting frame, or
- an avatar-media support still.

A single beat may contribute one or more numbered assets depending on its
carrier contract:

- `media_image`: 1–4 numbered stills;
- `media_video`: exactly one numbered starting frame;
- `avatar_media`: exactly one numbered supporting still;
- `avatar`: no numbered asset.

## Physical packaging

The logical numbered chain may be packaged into one or more physical grids.
Physical grids contain 2–4 images each. A longer continuity sequence may span
multiple physical grids while remaining one logical numbered chain. The logical
chain may contain 2–18 generated assets in total.

Physical packaging must never change scene meaning or continuity. The split
into physical grids is only an implementation detail of the numbered sequence.

## Beat-level implications

Do not choose a carrier only to satisfy a grid format. Decide first whether a
moment is best represented as `media_video`, `media_image`, `avatar_media` or
`avatar`, then include every generated reference asset from the same continuing
scene in the numbered grid when continuity conditioning is beneficial.

A moving action remains `media_video` even when its starting frame belongs to a
grid. A supporting `avatar_media` still may belong to that same grid if it is
part of the same scene. A static insert may sit between moving shots and remain
inside the same grid. Do not break a coherent grid merely because the carriers
change.

## Protected and exceptional cases

Protected ebook CTA assets keep their existing reference obligations and other
frozen CTA constraints, but they still follow the same editorial concept: a
grid is defined by continuity of generated assets, not by carrier uniformity.
Only exclude an asset from a grid when the scene itself changes or when shared
conditioning would be misleading.

## Grid identifiers and minimum size

Logical grids are named in order of first appearance across the whole video.
The first grid uses the letter `A`, the second `B`, the third `C`, and so on.
If the alphabet is exhausted, continue in spreadsheet style (`AA`, `AB`, ...).
Every generated asset in a grid receives that grid letter plus a running
position index as a **prefix on its actual generated-image prompt**:
`A1. description`, `A2. description`, `A3. description` ...;
`B1. description`, `B2. description`, `B3. description` ... .
The orchestrator reads these prefixes from `media_image.image_prompts`,
`media_video.prompt` and `avatar_media.prompt`; the carrier remains chosen
according to the scene, and no separate grid flag is necessary.

The label belongs to the generated asset, not to the beat. Therefore:

- a `media_video` beat contributes one label for its starting frame;
- an `avatar_media` beat contributes one label for its support still;
- a `media_image` beat contributes one label per still image prompt; and
- an `avatar` beat contributes no label.

A logical grid exists only when **two or more** generated assets belong to the
same continuity sequence. Do not create one-asset grids. A solitary generated
asset remains independent and unlabeled as a grid member.
