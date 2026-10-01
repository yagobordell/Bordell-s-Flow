# YouTube integration: core (phase 1)

The publisher is a separate distribution stage. This first phase provides settings,
metadata contracts, a flat external credential store, typed errors and a lazy API
client factory. It **does not** authorize a channel or upload videos.

## Installation and settings

YouTube dependencies are optional. When needed, install with
`uv sync --locked --extra dev --extra youtube`. Ordinary B pipeline and Salad
workers do not need to load Google libraries.

The application reads `.env`, `config/app.env` and `config/salad.env`
through the existing Pydantic Settings class. For YouTube, use only
`config/app.env` with variables described in `config/app.env.example`:

- `YOUTUBE_ENABLED=false` keeps automatic publication disabled.
- `YOUTUBE_KEYSTORE_DIR` is the only credential location setting.
  Set it to an **external directory**, such as
  `C:/Users/Example/keystores/youtube` on Windows or
  `/home/example/keystores/youtube` on Linux.
- `YOUTUBE_CHANNEL` is a human-readable local selector. It does not verify
  which YouTube channel was authorized. Authorization is implemented in phase 2.
- Remaining `YOUTUBE_*` options define publication defaults; per-video metadata
  will override them when phase 3 implements `youtube.json`.

Do **not** commit or copy OAuth files, secrets or authorized-user tokens into
the repository, Docker images, R2, Postgres or CI artifacts.

## Credential directory layout

```text
<YOUTUBE_KEYSTORE_DIR>/
    client_secret.json
    Canal Principal.json
    Canal Secundario.json
```

One shared Google OAuth client is used for multiple channels. The store only
operates on direct children, rejects path traversal, unsafe Windows names,
symbolic links and case-insensitive filename collisions, and persists tokens
using temporary files, `fsync` and `os.replace`. Token files are given
owner-only permissions where supported.

`client_secret.json` must be obtained from Google and placed **manually**
in this external directory in phase 2. This repository must never contain
real token or client-secret fixtures. Stored tokens are not printable logs.
The repository's `.gitignore` has defense-in-depth patterns, but these
are **not** a substitute for keeping the whole keystore outside the checkout.

## Models

`YouTubePublishRequest` models title, description, keywords/tags, category,
privacy and scheduling, made-for-kids and synthetic-media declarations,
language/localizations, license, recording date and optional thumbnail.
Structural validation is available in phase 1; MP4/thumbnail probing and
API-specific upload checks belong to phase 3.

The requested channel is a local filename alias. Without expanded scopes,
the publisher will not make `channels.list` calls to verify its identity.

## Milestones

- Phase 1: settings, typed contracts, external keystore, lazy client, unit tests.
- Phase 2: interactive OAuth per channel and automatic access-token refresh.
- Phase 3: safe dry-run, metadata resolution and resumable upload.
- Phase 4: thumbnails and independent retry.
- Phase 5: durable publishing state and idempotency across regenerated runs.
- Phase 6: opt-in hook after an actual verified final MP4.

No generation job, runner, API call or OAuth flow is activated by phase 1.
