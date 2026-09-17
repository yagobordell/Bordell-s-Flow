# Phase 4 transient R2 safety-cache resilience

A fresh end-to-end production run on 2026-09-17 exposed a failure mode in the Ideogram safety-only fallback path.

The Ideogram worker correctly rejected a reference for safety, but the client then hit a transient R2 connect timeout while persisting the negative safety-cache record. That secondary storage failure replaced the original `RemoteInferenceRejectedError`, so the safety fallback wrapper could not classify the result as terminal safety and FLUX was never invoked.

The negative rejection cache is an optimization, not the source of truth. The paid provider rejection is authoritative. Phase 4 therefore now treats failures while persisting a confirmed safety rejection as non-fatal: it logs the storage failure, preserves the provider rejection, continues through the remaining deterministic Ideogram variants, and can still activate FLUX when all executable variants are terminally safety-rejected.

The controlled Phase 4 wrapper also performs FLUX restore/queue cleanup whenever fresh Ideogram work is planned, because a previously unknown safety rejection can dynamically enqueue FLUX even when the preflight cache plan reported `flux_schnell=False`.

Regression coverage verifies both behaviors.
