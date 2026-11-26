# AGENTS.md

## Project intent

CampusLife is a local-first personal campus-life information hub, not only a lecture recorder. It supports academic, campus, opportunity, document, planner, and personal information through a shared capture/inbox model.

## Before implementation

1. Read README.md, docs/DECISIONS.md, and the relevant documents under docs/. DECISIONS.md wins when documents disagree.
2. Check the related GitHub issue, its prerequisites (the `⛔ 선행` line at the top), and acceptance criteria.
3. Keep the single-user deployment in mind: one Windows hub, Android and iPadOS clients.
4. Prefer incremental changes that preserve the end-to-end working path.
5. Define wire formats (manifest, entities, Locator, sync API) in `packages/api-contracts` first and generate client types from it; do not hand-write duplicate schemas in each app.

## Architecture boundaries

- Android is the capture client and offline Outbox; iPadOS is the study/annotation client with its own Outbox.
- Windows 11 laptop is the only source-of-truth hub and AI orchestration host.
- The agent binds to 127.0.0.1 only; `tailscale serve` terminates HTTPS. Admin operations (pairing approval, revoke) live on a separate localhost-only port that is never exposed through serve. Never authorize by source address.
- Each state has one owner (D1): client transfer state lives on the client; ingest, extraction, and job state live on the hub; review state belongs to the user. Do not add a combined status field to Capture.
- Original bytes are stored once by sha256 (D2); every ingestion path goes through the hub `store` module.
- Tailscale is private connectivity, not storage, message queue, or a guarantee that the laptop is awake.
- FolderSync + SFTP is a bootstrap PoC only; product sync must support manifest, hashes, ACK, retries, resumability, and idempotency.
- AI provider integrations must sit behind provider interfaces.
- Original files are immutable by default; derived artifacts are versioned and reproducible.
- SQLite + local files are the initial single-user persistence layer.
- Do not introduce a required always-on cloud backend or NAS.

## Privacy and safety

- Never commit API keys, tokens, personal files, real lecture recordings, student data, or real documents.
- Do not send content to a cloud AI provider unless the effective `cloud_policy` allows it. Derived data takes the strictest policy of its inputs, and the check happens in one function right before the provider call (D8).
- Recording and photography must be user-initiated and comply with school/instructor/participant policies.
- AI-extracted dates, assignments, announcements, and other state-changing data remain suggestions until the user confirms them.
- Do not add face recognition, voice identity recognition, attendance manipulation, or unauthorized school portal automation.

## Engineering expectations

- IDs are client-generated UUIDv7. Follow the idempotency keys in DECISIONS.md D4 for attachments, captures, mutations, and processing jobs.
- Preserve provenance from derived content to source files, pages, images, and audio time ranges.
- Handle offline mode, retries, cancellation, partial files, and app/process restarts.
- Keep time values timezone-aware; use UTC for persistence and Asia/Seoul as the initial display timezone.
- Avoid coupling domain logic to a specific UI, storage provider, or AI vendor.
- Add tests for failure paths as well as happy paths.
- Document configuration and user-visible data retention behavior.


- Test fixtures must be synthetic. Put them under `examples/` or a `fixtures/` directory; `.gitignore` blocks media files everywhere else.

## Multi-device requirements

- Treat Windows as the primary local data hub, not as the only UI client.
- Android, iPadOS, and Windows are first-class clients with separate device identities.
- Use a common versioned HTTPS API and sync contract; do not make product sync depend on FolderSync/SFTP.
- Keep SFTP only as an early connectivity PoC.
- Each mobile client owns a durable offline Outbox and sync cursor.
- Do not assume Tailscale provides storage, queueing, app authentication, or an always-awake Windows host.
- Implement pairing, per-device tokens, revocation, idempotency, resumable transfer, and conflict handling.
- iPadOS uses native SwiftUI; PencilKit drawing and PDFKit documents must be stored as separate annotation/revision layers, never by mutating the original attachment.
- Preserve iPad annotations in a cross-platform form (structured annotation data and/or derived PDF overlay) so Windows and Android can view them.
- Do not blindly last-write-wins overwrite concurrent edits to notes or annotation layers.
