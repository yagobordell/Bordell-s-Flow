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

## Phase 2: per-channel desktop authorization (Windows)

This phase does not upload videos, query channel identities or publish anything.
Keep Google OAuth credentials on the machine that runs the publisher.
Do not mount the keystore into GPU/Salad workers or copy its files into Docker.

1. In Google Cloud Console, enable YouTube Data API v3, configure the OAuth
   consent screen (test users if still in Testing), and create an OAuth client
   of type Desktop app. Download the JSON file.
2. Outside the checkout, create an owner-only directory such as a dedicated
   keystores/youtube folder under your Windows user profile. Move the downloaded
   JSON there and rename it to client_secret.json.
3. In PowerShell, configure the persistent Windows user environment variable:

   ```powershell
   [Environment]::SetEnvironmentVariable(
       "YOUTUBE_KEYSTORE_DIR",
       (Join-Path $env:USERPROFILE "keystores\\youtube"),
       "User"
   )
   ```

   Open a NEW PowerShell window. The path must resolve to an absolute external
   location. Symbolic links and Windows junctions are refused.
4. From the checkout, authorize a channel:

   ```powershell
   uv sync --locked --extra youtube
   uv run --locked --extra youtube python scripts/youtube/auth.py --channel "Canal Principal"
   ```

   Confirm the local alias and choose the intended YouTube channel carefully
   in your browser. The OAuth flow uses a temporary local HTTP listener and
   requests only youtube.upload with offline refresh. The result is the
   external file Canal Principal.json. All channels share client_secret.json.
5. Diagnostics and recovery:

   ```powershell
   uv run --locked --extra youtube python scripts/youtube/auth.py --list
   uv run --locked --extra youtube python scripts/youtube/auth.py --check --channel "Canal Principal"
   uv run --locked --extra youtube python scripts/youtube/auth.py --reauthorize --channel "Canal Principal"
   ```

The CLI works independently of YOUTUBE_ENABLED. The --check operation only
checks stored OAuth data and refreshes expired tokens; it never queries the
YouTube channel identity or proves upload rights. A PRIVATE test upload in
phase 3 must be inspected by the operator before any unattended publishing.

Existing Google authorized-user files from a shared external keystore can
be reused if they contain a refresh token and the explicit youtube.upload
scope. Files with missing scopes or invalid data are never silently
overwritten. An unsuccessful reauthorization preserves existing credentials.

The publisher can refresh existing tokens without the OAuth client file or
a browser. Permanently expired or revoked refresh tokens need an explicit
reauthorization. Google external apps still in Testing may issue refresh
tokens that expire after seven days.

Protect external folders with appropriate user ACLs on Windows. POSIX
directories require owner-only permissions. Never commit, upload, log, or
copy the OAuth client or per-channel token files to R2, Postgres, GitHub CI,
or GPU Docker image layers. Concurrent refresh/reauthorization is serialized
using OS-level per-channel locks released when the process exits.
