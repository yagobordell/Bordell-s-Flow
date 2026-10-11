# Professional carrier / speaking authority

The following sections are carried verbatim from the current Professional B2 source file. They remain authoritative unless contradicted by the current Python contracts.

Absolute carrier and audio contracts

- avatar: reuse ONLY the original speaking avatar, prompt=null, video_prompt=null, reference=false, audio=true. Do not generate alternative speaking start images.
- avatar_media: show the ORIGINAL speaking avatar alongside one generated supporting still; prompt nonempty, video_prompt=null, audio=true. Its image prompt describes the SUPPORT image only.
- media_image: full-screen generated B-roll still, prompt nonempty, video_prompt=null, audio=false.
- media_video: full-screen generated B-roll clip, prompt for one photographable starting frame, nonempty video_prompt for subsequent motion, ALWAYS audio=false.

Only avatar and avatar_media speak. Generated media_image and media_video are unsynchronized coverage with voice-over. Never depict anyone speaking to camera, delivering narration, silently lip-syncing or acting as a replacement direct-to-camera host in generated B-roll. Generated presenter B-roll is allowed when the narrator is truly the depicted participant in the story; then use reference=true to preserve their identity BUT locate the scene in a clearly different room or physical location from the avatar reference. Never generate a new image of the narrator in the original reference room, regardless of angle, gesture or crop. A narrator identity reference is not a room/setup reference. Do not assign another person's actions to the presenter. Otherwise reference=false.

Narrator-run cap and final close

A maximal consecutive run of audio=true beats across block boundaries has a HARD limit of 120 characters (sum of each beat's text.strip() length). Use a meaningfully justified audio=false B-roll beat to interrupt an overlong run, without changing any text, ID or boundary. The last beat of any close block MUST be untouched base avatar: avatar, prompt=null, video_prompt=null, reference=false, audio=true.

The generated visuals must not recreate a speaking presenter in the original narration room. A visual_type describes the carrier, not the identity of characters in a scene.
