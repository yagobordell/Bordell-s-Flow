# B1.1 → B1.2 → B2 (replacement planning bots)

This standalone, opt-in stage runs the three user-supplied audited instruction documents
**byte-for-byte** from `src/ai_video_factory/bots/prompts/`. The expected SHA-256
checksums are embedded in `workflow.py` and verified before API calls.

- B1.1 runs once against the complete, unmodified script. The runtime resolves unique
  verbatim `first_words` / `last_words` anchors into contiguous block text and refuses any
  gap, duplicate, overlap, normalized text, or ambiguous anchor.
- B1.2 runs independently for each single materialized block. It emits 1A, 1B, ... 1AA,
  2A, ... IDs, preserves exact source characters, and enforces 40 source words per beat.
- B2 runs independently on each completed B1.2 block, preserves all upstream text,
  IDs and metadata, and assigns exactly one of `avatar`, `avatar_media`, `media_image`,
  `media_video` with the documented description/null rules.
- Per-block results are collected in ascending block ID, regardless of call completion
  order. B1.2 workers finish as a parallel wave before any B2 worker begins; B2 workers
  then run as a parallel wave. Both waves have the same bounded concurrency limit.

## Run

Set `OPENAI_API_KEY` in local `.env`, then run:

```powershell
uv run --locked --extra dev python scripts/pipeline/run_b_pipeline.py `
  data/input/script.txt --output data/output/b_pipeline --max-parallel-calls 8
```

All three bots use `OPENAI_B_MODEL=gpt-6-luna` and
`OPENAI_B_REASONING_EFFORT=medium` (both are independently configurable). The CLI
explicitly sets the OpenAI provider to Standard/default service tier. It uses
Responses API Structured Outputs; therefore the instruction documents' requested
Markdown code fence is replaced by the schema transport, with all semantic and
field-level rules unchanged.

Outputs are `b1_1.json`, `b1_2/block_<id>.json`, `b2/block_<id>.json`, and the
application-owned `visual_plan.json` join. No legacy phase2 artifact is changed.

The old bot implementations live only in `src/ai_video_factory/legacy_bots/` for
compatibility with the existing phase3–9 production path. The active
`src/ai_video_factory/bots/` package contains only the replacement B1.1/B1.2/B2
components. Existing legacy imports are redirected to `legacy_bots` until the
downstream pipeline is migrated.

**No production cutover:** the existing phase3–9 pipeline expects integer Beat IDs,
scene/shot groupings, and shot-based keyframes/video. B1.2 emits string Beat IDs and
B2 chooses avatar / still / video per beat. Feeding this visual plan into the old
pipeline via a lossy adapter would silently discard the audited contracts. An
explicit downstream schema, timeline, composition and caching migration is required
and should pass end-to-end tests before enabling it in `run_video_factory.ps1`.

The validations here establish structural fidelity, source reconstruction, 40-word
limits, type/ID preservation, and mandatory intro/close avatars. Whether a media
brief is *semantically* faithful or physically feasible still needs representative
manual or model-based evaluation; schemas alone cannot prove that.
