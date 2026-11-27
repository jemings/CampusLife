# AGENTS.md

## Project intent

CampusLife is a local-first personal campus-life information hub, not only a lecture recorder. It supports academic, campus, opportunity, document, planner, and personal information through a shared capture/inbox model.

## Before implementation

1. Read README.md, docs/DECISIONS.md, and the relevant documents under docs/. DECISIONS.md wins when documents disagree.
2. Check the related GitHub issue, its prerequisites (the `⛔ 선행` line at the top), and acceptance criteria.
3. Keep the single-user deployment in mind: one Windows hub, Android and iPadOS clients.
4. Prefer incremental changes that preserve the end-to-end working path.
5. Define wire formats (blob upload, `capture.commit`, Item kinds, Locator, Mutation, changes feed) as JSON Schema in `packages/api-contracts` first, and verify every language against its golden fixtures; do not invent per-app schemas (D17).

## Architecture boundaries

- Android is the capture client and offline Outbox; iPadOS is the study/annotation client with its own Outbox.
- Windows 11 laptop is the only source-of-truth hub and AI orchestration host.
- The agent binds to 127.0.0.1 only; `tailscale serve` terminates HTTPS. Admin operations (pairing approval, revoke) live on a separate localhost-only port that is never exposed through serve. Never authorize by source address.
- Each state has one owner (D1): seal and transfer state live on the client; processing state lives only in hub `ProcessingJob`; user data (Items, Relations, Tags, annotation layers, policies) changes only through Mutations. Do not add a status field to Capture or Attachment.
- Every write to user data, including the Windows local UI and local import, goes through the hub Mutation applier. Conflicts are decided there by per-field compare-and-set (D18); clients carry no merge logic.
- Original bytes are stored once by sha256 (D2). There is no Document or CourseMaterialSet entity: revisions are new blobs linked by `supersedes`, and grouping is Relations to a LectureSession Item.
- AI output never creates Items directly. It produces immutable Artifacts and Suggestions; an Item exists only after the user accepts (D9, D10).
- Tailscale is private connectivity, not storage, message queue, or a guarantee that the laptop is awake.
- Do not add SFTP, FolderSync, or any second ingestion path. Product sync is blob-first upload (sha256 keyed, order-independent chunks) followed by `capture.commit`, with hashes, ACK, retries, resumability, and idempotency (D7).
- Clients must handle a hub `epoch` change (restore or migration) by resyncing from `since=0` and re-uploading captures the hub no longer has (D3).
- Reminders are scheduled locally on each client from synced Item fields; the hub cannot push to phones (D19).
- AI provider integrations must sit behind provider interfaces.
- Original files are immutable by default; derived artifacts are versioned and reproducible.
- SQLite + local files are the initial single-user persistence layer.
- Do not introduce a required always-on cloud backend or NAS.

## Privacy and safety

- Never commit API keys, tokens, personal files, real lecture recordings, student data, or real documents.
- `cloud_policy` is `blocked < local_only < cloud_allowed`. Do not send content to a cloud AI provider unless the effective policy, the minimum over all inputs, allows it. Every provider call goes through `ProviderGateway`, which also writes the audit log (D8).
- Recording and photography must be user-initiated and comply with school/instructor/participant policies.
- AI-extracted dates, assignments, announcements, and other state-changing data remain suggestions until the user confirms them.
- Do not add face recognition, voice identity recognition, attendance manipulation, or unauthorized school portal automation.

## Engineering expectations

- IDs are generator-created UUIDv4 and double as idempotency keys; ordering comes from the hub `change_seq`. Processing jobs use `job_key` (D4).
- Preserve provenance from derived content to source files, pages, images, and audio time ranges using the single Locator schema. LLMs cite input ContentBlock ids only, and the hub validates them before converting to Locators (D13).
- Handle offline mode, retries, cancellation, partial files, and app/process restarts.
- Use the three time types (D11): `instant` in UTC for things that happened, `local_date` for all-day dates, `local_time_rule` with an IANA zone for timetables. Asia/Seoul is the initial display timezone. NFC-normalize stored and compared strings and keep the original.
- Avoid coupling domain logic to a specific UI, storage provider, or AI vendor.
- Add tests for failure paths as well as happy paths.
- Document configuration and user-visible data retention behavior.


- Test fixtures must be synthetic. Put them under `examples/` or a `fixtures/` directory; `.gitignore` blocks media files everywhere else.

## Pull requests and history

- PRs land by **squash merge** (the default); merge commits and rebase merges are also allowed in this repository. The squash commit title is the PR title, so write the PR title as a Conventional Commit. `Closes #N` goes in a commit message or the PR body of the PR that finishes the issue.
- History inside a PR branch need not be tidy: add review fixes as new commits on top, catch up with the base by `git merge origin/main` (or a rebase), and avoid force-pushing a branch that is under review.
- Verify once at the PR HEAD: run the full test suite on the exact SHA you push, plus CI. Do not run the suite on every commit of the branch; while fixing, run only the failing tests and the tests the change touches.

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
