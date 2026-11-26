# AGENTS.md

## Project intent

CampusLife is a local-first personal campus-life information hub, not only a lecture recorder. It supports academic, campus, opportunity, document, planner, and personal information through a shared capture/inbox model.

## Before implementation

1. Read README.md and the relevant documents under docs/.
2. Check the related GitHub issue and acceptance criteria.
3. Keep the single-user Windows + Android deployment constraints in mind.
4. Prefer incremental changes that preserve the end-to-end working path.

## Architecture boundaries

- Android is the capture client and offline Outbox.
- Windows 11 laptop is the primary local data hub and AI orchestration host.
- Tailscale is private connectivity, not storage, message queue, or a guarantee that the laptop is awake.
- FolderSync + SFTP is a bootstrap PoC only; product sync must support manifest, hashes, ACK, retries, resumability, and idempotency.
- AI provider integrations must sit behind provider interfaces.
- Original files are immutable by default; derived artifacts are versioned and reproducible.
- SQLite + local files are the initial single-user persistence layer.
- Do not introduce a required always-on cloud backend or NAS.

## Privacy and safety

- Never commit API keys, tokens, personal files, real lecture recordings, student data, or real documents.
- Do not send content to a cloud AI provider unless the item's cloud policy explicitly allows it.
- Recording and photography must be user-initiated and comply with school/instructor/participant policies.
- AI-extracted dates, assignments, announcements, and other state-changing data remain suggestions until the user confirms them.
- Do not add face recognition, voice identity recognition, attendance manipulation, or unauthorized school portal automation.

## Engineering expectations

- Define stable IDs and idempotency behavior for every ingestion path.
- Preserve provenance from derived content to source files, pages, images, and audio time ranges.
- Handle offline mode, retries, cancellation, partial files, and app/process restarts.
- Keep time values timezone-aware; use UTC for persistence and Asia/Seoul as the initial display timezone.
- Avoid coupling domain logic to a specific UI, storage provider, or AI vendor.
- Add tests for failure paths as well as happy paths.
- Document configuration and user-visible data retention behavior.
