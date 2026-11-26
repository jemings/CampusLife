# CampusLife 공통 도메인 모델

구조 결정의 근거는 [DECISIONS.md](DECISIONS.md)에 있습니다. 이 문서는 개념 모델이며, 실제 스키마는 `packages/api-contracts`의 JSON Schema가 기준입니다.

## 1. 핵심 축

세 축으로 모든 데이터를 설명합니다(D2).

- **Blob — bytes의 정체성:** sha256으로 식별되는 불변 원본. 같은 내용은 한 번만 저장합니다.
- **Capture — 어떻게 들어왔나:** 한 번의 수집 행위가 만든 봉인된 불변 묶음. Attachment(Capture가 Blob을 포함했다는 사실)를 1..N개 가집니다.
- **Item + Relation — 무엇에 관한 것인가:** 사용자가 확정한 의미 있는 대상(과목, 강의, 과제, 공지, 메모 등)과 그 사이의 연결.

여기에 파이프라인 산출물이 더해집니다.

- **Artifact:** transcript, OCR, 요약, 렌디션, overlay 등 불변 파생 결과(D9)
- **Suggestion:** AI가 낸 제안. 엔터티가 아니며 사용자가 수락해야 Item·Relation이 됩니다(D10)
- **ProcessingJob:** 비동기 처리 작업. 처리 상태는 여기에만 있습니다(D1)

ID는 생성한 쪽이 만드는 UUIDv4이며 그 ID가 곧 멱등 키입니다(D4).

## 2. 엔터티

### 수집 (불변)

| Entity | 주요 필드 |
|---|---|
| Blob | sha256, size_bytes, stored_at |
| Capture | id, device_id, created_at(instant), origin{type: record\|camera\|share\|import\|manual, uri?, app?, publisher?, distributed_by?, distributed_at?}, category_hint, user_note, links[], timeline? |
| Attachment | id, capture_id, blob_sha256, role(예: audio_segment), seq?, original_filename(NFC + 원문), declared_mime, detected_mime, captured_at, cloud_policy, backed_up_at(Hub 소유) |

### 사용자 데이터 (Mutation으로만 변경)

| Entity | 주요 필드 |
|---|---|
| Item | id, kind, schema_version, title, body?, status?, starts_at?, due_at?, all_day?, fields{…}, rev, deleted_at? |
| Relation | id = `"{from}\|{type}\|{to}"`, from, type, to |
| Tag | id, name, color |
| AnnotationLayer | id, target_blob_sha256, page_number, author_device_id, rev, drawing_blob, strokes_blob, page_box, conflict_of? |
| ConflictRecord | id, entity_ref, field, theirs_rev, mine_value_blob, state |

### Hub 파생 (읽기 전용으로 동기화)

| Entity | 주요 필드 |
|---|---|
| Artifact | id, artifact_type, subject_ref, job_key, provider, model, prompt_version, pipeline_version, input_hashes[], content_blob, created_at |
| Suggestion | id, source_artifact_id, action, proposed{…}, evidence[](Locator), fingerprint, state |
| ProcessingJob | id, job_key, job_type, subject_ref, state, outcome_code?, attempts, next_attempt_at, error (상세는 동기화하지 않고 `processing_summary`만 노출) |
| ContentBlock | id = hash(blob, extractor_id, extractor_version, ordinal), blob_sha256, block_type, text 또는 payload, locator (동기화하지 않음) |

### 시스템

| Entity | 주요 필드 |
|---|---|
| Device | id, platform, name, app_version, token_hash, state(active\|dormant\|revoked), created_at, last_seen_at |
| audit_log | 외부 AI 전송, 삭제, revoke, 충돌 해소 기록 (Hub 전용, 동기화하지 않음) |

## 3. Item kind

| kind | 주요 fields | 비고 |
|---|---|---|
| term | year, semester, starts_on, ends_on | MVP |
| course | instructor, room, credits, schedule_slots[](local_time_rule), default_cloud_policy | MVP |
| lecture_session | course_id, date(local_date) | 녹음 시작 시 생성 |
| assignment | course_id, due_text, submission | MVP |
| exam | course_id, scope_text | MVP |
| task | progress(inbox/planned/in_progress/blocked/completed/cancelled), reminders[] | MVP |
| note | forked_from_artifact?, refs[] | MVP |
| event, announcement, opportunity, organization, place, person | kind별 스키마 | 후속 |

- 일정 로직에 필요한 날짜·상태(`starts_at`, `due_at`, `all_day`, `status`)는 공통 열입니다.
- 클라이언트는 모르는 kind를 제목·날짜·본문만으로 렌더링합니다.
- 성적, 주민등록번호, 금융정보 등 불필요하게 민감한 데이터는 기본 수집 대상이 아닙니다. 추가할 때는 별도 보호 정책과 사용자의 명시적 동의가 먼저입니다.

## 4. 통합 Inbox 흐름

모든 외부 입력은 먼저 Capture로 들어옵니다. AI 분류는 Suggestion을 만들 뿐 엔터티를 만들지 않습니다.

- 강의 녹음 + 판서 사진 → Capture 1개(녹음 세그먼트 + 사진 Attachment). 녹음 화면에서 고른 과목·강의는 `links[]`로 즉시 확정 Relation이 됩니다.
- 학교 행사 공지 PDF → Capture → 추출 Artifact → `create_item(kind=event)` Suggestion → 사용자가 수락하면 Item과 근거 Relation이 생깁니다.
- 장학금 웹 링크 → Capture(URL + 스냅샷 Blob) → `opportunity` Suggestion
- 개인 음성 메모 → Capture → `note`·`task` Suggestion
- 교수자 자료 v2 재배포 → 새 Capture·새 Blob → `supersedes` Relation Suggestion

기각된 Suggestion은 fingerprint로 기억하므로 재처리해도 다시 열리지 않습니다.

## 5. 상태 — 소유자별 분리 (D1)

| 상태 | 소유자 | 값 |
|---|---|---|
| Capture 봉인 | 클라이언트 로컬 | open, sealed |
| 전송 | 클라이언트 로컬 | pending, uploading, committed, retry_wait, needs_attention |
| ProcessingJob.state | Hub | pending, running, retry_wait, succeeded, failed, blocked, cancelled |
| ProcessingJob.outcome_code | Hub | unsupported, password_required, corrupt, needs_conversion, partial, policy_blocked, budget_blocked |
| processing_summary | Hub (계산값) | job 집계 |
| Suggestion.state | 사용자 | open, accepted, dismissed, superseded |
| task.progress | 사용자 | inbox, planned, in_progress, blocked, completed, cancelled |

Capture와 Attachment에는 처리 상태 필드가 없습니다.

## 6. 시간 (D11)

| 타입 | 용도 |
|---|---|
| `instant` (UTC, RFC 3339 `Z`) | 생성·수정·촬영·녹음 등 이미 일어난 일 |
| `local_date` (+ IANA tz) | 종일 일정, 날짜 마감 |
| `local_time_rule` (`HH:MM` + IANA tz + 요일·기간) | 시간표 반복. 표시할 때 전개 |

- 사용자 표시 시간대는 기본 Asia/Seoul이며 설정할 수 있습니다.
- 마감 원문 표현은 `fields.due_text`에 보존합니다.
- 모든 문자열은 NFC로 정규화하고 원문을 함께 보존합니다.
- 녹음 중 사진은 단조 시계(`elapsedRealtimeNanos`) 기준으로 녹음과 연결합니다(D15).

## 7. 출처 (D13)

- 모든 Artifact 본문은 `blocks[{text, refs}]` 구조이고, ref는 공통 Locator를 가리킵니다.
- 앵커는 Blob 해시입니다(녹음은 Capture 타임라인, 필기는 레이어 revision).
- LLM은 입력 ContentBlock id로만 인용하고, Hub가 그 id를 검증해 Locator로 치환합니다.
- 정확한 위치가 없으면 `document`를 쓰고 추정하지 않습니다.

## 8. AI 산출물과 사용자 편집 (D9)

- Artifact는 생성 후 바뀌지 않습니다. 재실행은 새 Artifact를 만들고, Hub가 `current` 포인터를 옮깁니다.
- 사용자가 AI 노트를 고치면 `note` Item으로 fork되고, 이후 편집은 Note의 Mutation입니다.
- Artifact는 기록된 입력으로 재실행할 수 있지만 결과가 같다고 보장하지 않으므로 백업 대상입니다.

## 9. Annotation (D12)

- `AnnotationLayer(target_blob, page_number, author_device)`는 작성자가 하나입니다. 화면에서는 같은 (Blob, page)의 모든 레이어를 겹쳐 그립니다.
- payload: `drawing.pkd`(PencilKit 원본, iPad 편집용) + `strokes.v1.json`(표준, 정규화 좌표)
- overlay PDF는 Hub가 strokes로 렌더링하는 Artifact입니다.
- Office·HWP 문서의 필기는 PDF 렌디션 Blob에 고정되고, 그 렌디션은 pin됩니다.

## 10. 동기화와 충돌

- 변경 피드: `GET /v1/changes?since=` → 바뀐 행의 현재 상태와 `hub_id`·`epoch`(D3)
- 쓰기: `POST /v1/mutations` → 필드 단위 compare-and-set(D18)
- 충돌: 스칼라 필드는 같은 필드만 충돌로 판정하고, 텍스트 본문 충돌은 ConflictRecord로 보존합니다. last-write-wins는 없습니다.

## 11. 검색

- MVP는 SQLite FTS5와 구조화 필터를 씁니다. 한국어 분석기는 측정 후 결정합니다(DECISIONS O2).
- 인덱싱 대상: ContentBlock, Artifact, Item·Note, 파일명.
- 인덱스는 순수 캐시이므로 백업하지 않고 재구축합니다.
