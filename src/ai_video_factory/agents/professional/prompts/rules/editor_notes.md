# Separate editor instructions for Haiku

Do not put overlay typography, labels, generated charts, QR codes or dynamic subtitles into a photograph/video prompt. Editor notes form an independent sidecar plan keyed by original beat_id and, when relevant, image subshot index. A word-triggered event uses {trigger:{text:"exact source substring", occurrence:1}} where occurrence counts appearances within THAT beat; validation rejects missing or out-of-range occurrences. Unanchored conceptual notes may use trigger:null, but are not precise timeline events.

The Haiku postproduction pipeline will resolve word timestamps from actual STT and select preapproved Remotion components and 3×3 placement anchors. Code, not Haiku, resolves timing and rejects unsafe positions; generated images requested by the editor are a separate later process. The original narration never changes.
