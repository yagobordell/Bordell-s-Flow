# YouTube integration: manual publishing (phases 1–3)

YouTube is an independent distribution stage. Phase 1 established the settings,
external keystore and metadata contracts; phase 2 added desktop OAuth and token
refresh; phase 3 adds guarded, manual MP4 uploads and offline dry-run.
Automatic pipeline publication, thumbnails and full publisher state come later.

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

## Phase 3: guarded, independent manual video upload

This phase adds an isolated local video uploader and a dry-run. It does NOT
connect YouTube to Video Factory's B pipeline, auto-publish, upload thumbnails,
or implement the final phase-5 durable per-run publishing state.

From a Windows PowerShell terminal inside the repository:

```powershell
uv sync --locked --extra youtube

uv run --locked --extra youtube python scripts/youtube/upload.py --help
```

Prepare an ordinary external MP4 such as C:/Videos/test.mp4 and a file
C:/Videos/youtube.json:

```json
{
  "schema_version": "youtube-publish/v1",
  "channel": "test01",
  "title": "Video Factory - Private Upload Test",
  "description": "Manual private integration test.",
  "keywords": ["video factory", "private test"],
  "category_id": "22",
  "privacy": "private",
  "notify_subscribers": false,
  "made_for_kids": false,
  "contains_synthetic_media": true,
  "default_language": "es",
  "localizations": {}
}
```

Channel names select LOCAL credentials only; with the upload-only scope
there is no API-based channel identity check. You must manually inspect
the destination channel after the first private upload.

### Mandatory first step: offline preflight

```powershell
uv run --locked --extra youtube python scripts/youtube/upload.py `
  --channel "test01" `
  --video "C:\Videos\test.mp4" `
  --metadata "C:\Videos\youtube.json" `
  --dry-run
```

Dry-run validates the local MP4 extension, size and ftyp header, metadata
schema and constraints, an existing recognized Video Factory publication
manifest (SHA-256) when present, keystore, and an upload-authorized local
token. It displays resolved API-writable request metadata. It does NOT
open a browser, refresh tokens, contact YouTube, upload media or write an
upload attempt record. Only YouTube can perform definitive codec/container
and category acceptance checks; ftyp is an early rejection filter, not a
complete MP4 decoder.

### Actual private test upload

```powershell
uv run --locked --extra youtube python scripts/youtube/upload.py `
  --channel "test01" `
  --video "C:\Videos\test.mp4" `
  --metadata "C:\Videos\youtube.json"
```

Inspect the preview; type SUBIR to authorize the actual upload.
Credentials load/refresh automatically and the YouTube Data API receives
one resumable videos.insert with 8 MiB chunks. Only transient network or
API errors are retried, within the SAME resumable session. Quota errors
are not automatically retried. Successful output reports the video ID
and watch URL. Verify the selected actual channel and private video
manually before using this on real material. The --yes flag bypasses the
interactive confirmation and must be used deliberately; it is not
integration with the production runner.

Supported CLI overrides include --channel, --title, --description, repeated
--keyword, --privacy, --category-id, --default-language, --license,
--publish-at, --recording-date, and optional Boolean flags such as
--made-for-kids or --no-made-for-kids. Localizations remain expressible
through youtube.json. Precedence is explicit CLI > JSON > Settings >
internal defaults. Language codes are checked for a BCP-47 shape locally;
YouTube remains responsible for accepting specific languages. The
thumbnail_path field may be null or absent;
non-null thumbnails are rejected until phase 4. The request model is
strict and rejects unknown metadata keys.

### Safety: provisional upload attempt record

Immediately before the first upload chunk, the manual CLI exclusively
creates a local sidecar next to the MP4:

```text
C:/Videos/test.mp4.youtube-upload-attempt.json
```

This non-secret provisional record contains hashes of the MP4 and
normalized request, local alias, privacy and upload status. Once the API
returns a usable video ID, it is written to the record atomically before
printing the result. Repeating the exact same successful command reuses
the local receipt and NEVER calls videos.insert again.

If the program is interrupted or a network/response failure makes the
outcome uncertain, the record remains in uploading state. The next
execution refuses to create a new upload, even with modified metadata.
DO NOT delete/rename the record and rerun casually; first inspect the
actual YouTube channel and reconcile any prior submission manually.
This basic local path-specific guard is NOT the cross-run, cross-device
publisher idempotency implementation; phase 5 will add the full
youtube_publish.json state machine and manage all recovery cases.

Use a distinct, non-symlink, writable local file path for the video and
metadata. Do not treat the completed local receipt as verification of
YouTube transcoding. The manual CLI is independent of YOUTUBE_ENABLED,
which remains off until phase 6.

The API may restrict projects that have not completed the YouTube compliance
audit to private uploads, independently of your chosen metadata. Consult:
https://developers.google.com/youtube/v3/docs/videos/insert

### CI and phase boundaries

The YouTube CI job installs actual optional Google libraries and tests
the uploader with mocked HTTP responses only. Its real-library compatibility
tests do not contact Google. A live private smoke test is a manual
acceptance step using your external keystore and existing authorized alias.

- Phase 4 will add an independent thumbnail operation; it must NEVER call
  videos.insert again after a successful video upload.
- Phase 5 will implement full durable publisher state and more complete
  idempotency across multiple runs and locations.
- Phase 6 will opt-in to an automatic post-validation pipeline hook.
