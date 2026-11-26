# CampusLife 공통 도메인 모델

구조 결정의 근거는 [DECISIONS.md](DECISIONS.md)에 있습니다. 이 문서의 필드 목록은 개념 모델이며, 실제 스키마는 `packages/api-contracts`의 JSON Schema/OpenAPI가 기준입니다.

## 1. 핵심 개념

CampusLife의 중심은 강의가 아니라 **Capture → Item → Relation** 구조입니다.

- **Capture:** 사용자가 어떤 장치/수단으로 한 번에 수집한 단위. 생성 시점의 첨부 목록을 선언하며 이후 바뀌지 않습니다(D7).
- **Attachment:** Capture에 포함된 논리 첨부(녹음 세그먼트, 사진, PDF 등). 바이트는 Blob이 가집니다.
- **Blob:** sha256으로 식별되는 불변 원본 바이트(D2). 여러 Attachment가 같은 Blob을 가리킬 수 있습니다.
- **Item:** 검토 후 사용자가 관리하는 의미 있는 대상(강의, 공지, 행사, 과제, 장학금, 메모 등). 유형별 상세는 1:1 레코드입니다(D10).
- **Source:** 정보의 출처(직접 입력, 수업 녹음, 학교 공지 URL, 이메일 export 등)
- **Artifact:** transcript, OCR, 요약, 퀴즈, 추출 필드 등 파이프라인이 만든 불변 파생 결과(D9)
- **Relation:** 과목·행사·사람·일정·문서 사이의 연결
- **ProcessingJob:** OCR/STT/LLM 등 비동기 처리 작업

모든 엔터티는 생성 장치가 만든 UUIDv7 ID(D4), `created_at`/`updated_at`(UTC), provenance, 삭제 정책을 갖습니다. 사용자가 편집할 수 있는 엔터티는 `revision`을 가집니다.

## 2. 도메인 엔터티

### Core

| Entity | 설명 | 주요 필드 |
|---|---|---|
| Capture | 수집 단위 | id, device_id, captured_at_utc, timezone, source_type, category_hint, cloud_policy, declared_attachment_ids[], user_notes |
| Attachment | 논리 첨부 | id, capture_id, blob_sha256, original_filename, declared_mime, detected_mime, size_bytes, captured_at_utc, cloud_policy(선택 override), ingest_state, extraction_state, backed_up_at |
| Blob | 불변 바이트 | sha256, size_bytes, stored_at, storage_path |
| Item | 통합 정보 객체 | id, item_type, title, description, review_state, importance, revision |
| Source | 정보의 출처 | source_type, uri, publisher, observed_at, trust_level |
| Artifact | 파생 산출물(불변) | id, artifact_type, job_key, provider, model, prompt_version, pipeline_version, content_path, generated_at |
| SourceReference | 파생 결과 → 원본 위치 | artifact_id 또는 item_id, attachment_id, document_revision_id(선택), locator |
| Tag | 사용자 정의 분류 | name, color, scope |
| Relation | 항목 간 연결 | from_id, relation_type, to_id |
| Reminder | 알림/리마인더 | item_id, trigger_at_utc, channel, state |
| ProcessingJob | 처리 작업 | job_key, job_type, state, attempts, last_error, retry_after, started_at, completed_at, cost_estimate, cost_actual |

`ProcessingJob.job_key`는 `(job_type, 입력 blob 해시 목록, pipeline_version, provider, model, prompt_version)`의 해시입니다. 같은 키의 작업은 기존 Artifact를 재사용합니다(D4).

### Item 상세 — Academic

| Entity | 설명 |
|---|---|
| AcademicTerm | 학년도/학기 |
| Course | 과목, 담당 교수/강사, 강의실, 학점 |
| ScheduleSlot | 요일·시간·장소별 강의 시간표 (timezone 포함) |
| LectureSession | 특정 일자의 실제 강의 |
| Assignment | 과제/프로젝트/제출물, 마감(`due_at_utc` + 원문 표기) |
| Exam | 시험/퀴즈/평가 |
| StudyRecord | 학습/복습 수행 기록 |

### Item 상세 — Campus & Personal

| Entity | 설명 |
|---|---|
| CampusEvent | 행사, 세미나, 축제, 특강 |
| Organization | 동아리, 학생회, 스터디, 연구 그룹 |
| Announcement | 학교/학과 공지 |
| Opportunity | 장학금, 공모전, 인턴십, 채용, 대외활동 (요건, 마감, 제출 서류, 진행 상태) |
| Person | 사용자가 직접 관리하는 사람 정보(최소 수집) |
| Place | 강의실, 건물, 시설 등 장소 |
| Task | 범용 개인 할 일 (`progress`) |
| Note | 개인 메모 또는 AI 노트를 사용자가 편집한 문서 (`derived_from_artifact_id` 선택, revision) |
| Expense | 선택적 개인 지출 기록(후속 기능) |

Item 상세 레코드는 Item과 같은 ID를 공유하는 1:1 레코드로 유형 고유 필드만 가집니다. 제목·설명·검토 상태는 Item에만 있습니다(D10).

성적, 주민등록번호, 금융정보 등 불필요하게 민감한 데이터는 기본 수집 대상이 아닙니다. 성적이나 행정 관련 데이터가 추가되더라도 별도 보호 정책과 사용자 명시 동의가 선행되어야 합니다.

## 3. 통합 Inbox 및 분류

모든 외부 입력은 먼저 Capture로 들어옵니다. 분류 확정 전에는 어떤 도메인 객체에도 강제로 귀속하지 않습니다.

- 강의 녹음 + 판서 사진 → Capture 1개, Attachment N개(녹음 세그먼트 + 사진) → LectureSession 및 Course와 연결
- 학교 행사 공지 PDF → Capture → Announcement 후보 → CampusEvent 후보 → 사용자가 확정하면 Planner 일정과 연결
- 장학금 웹 링크 → Capture → Opportunity 후보 → 마감일/요건 추출 후 확인
- 개인 음성 메모 → Capture → Note 및 Task 후보
- 과제 안내 스크린샷 → Capture → Assignment 후보 및 원본 이미지 연결

AI가 만든 Item은 `review_state=suggested`로 생성되고, 사용자 승인 후 `confirmed`가 됩니다. suggested Item은 알림·외부 캘린더·Planner 확정 일정에 반영되지 않습니다.

## 4. 상태 — 소유자별 분리 (D1)

| 상태 | 소유자 | 값 |
|---|---|---|
| `OutboxEntry.transfer_state` | 클라이언트 로컬 | pending, uploading, retry_wait, acked, failed_needs_attention |
| `Attachment.ingest_state` | Hub | receiving, verified, rejected |
| `Attachment.extraction_state` | Hub | [FILE-INGESTION.md §9](FILE-INGESTION.md) |
| `ProcessingJob.state` | Hub | pending, running, retry_wait, succeeded, failed, cancelled |
| `Item.review_state` | 사용자 | suggested, confirmed, dismissed |
| `Task.progress` / `Assignment.progress` | 사용자 | inbox, planned, in_progress, blocked, completed, cancelled |

Capture에는 상태 필드가 없습니다. "업로드 중 / 처리 중 / 검토 필요" 같은 화면 요약은 위 상태들에서 계산합니다. Capture 완결 여부는 `declared_attachment_ids`가 모두 `verified`인지로 판단합니다(D7).

## 5. 시간과 출처

- 저장 시각은 UTC ISO-8601입니다.
- 사용자 표시 시간대는 기본 Asia/Seoul이며 설정 가능합니다.
- 강의/행사/마감 시각은 timezone을 명시하고, 원문 표기(예: "11/30(일) 자정")를 함께 보존합니다. "자정" 같은 모호한 마감은 해석 결과를 suggested로 두고 사용자 확인을 받습니다.
- 종일 일정은 시각이 아닌 날짜(`date`) + timezone으로 저장합니다.
- 사진 EXIF, 파일 생성 시각, 앱 기록 시각, 사용자 수정 시각을 구분합니다.
- 앱 내 녹음 중 촬영한 사진은 녹음 세션 시작 기준 경과 시간을 함께 기록합니다(D13).

모든 Artifact는 하나 이상의 SourceReference를 가집니다. SourceReference의 `locator`는 [DECISIONS.md D12](DECISIONS.md)의 공통 Locator 스키마를 사용하며, 정확한 위치를 산출할 수 없으면 `whole_document`로 명시하고 추정하지 않습니다. Artifact 자체에 provider/model/prompt_version/pipeline_version/generated_at이 기록되고, 불확실한 주장은 `review_required`로 표시합니다.

## 6. AI 산출물과 사용자 편집 (D9)

- Artifact는 생성 후 바뀌지 않습니다. 재처리는 새 Artifact 버전을 만듭니다.
- 사용자가 AI 노트를 고치면 `Note(derived_from_artifact_id)`가 생기고 이후 편집은 Note revision으로 관리합니다.
- 새 Artifact 버전이 나와도 기존 Note는 변경되지 않으며 UI가 비교를 제안합니다.

## 7. 다중 파일 및 문서

강의 자료는 단일 Attachment가 아니라 여러 파일을 논리적으로 묶는 MaterialSet으로 표현합니다.

| Entity | 설명 | 주요 필드 |
|---|---|---|
| CourseMaterialSet | 교수/조교 배포자료 또는 한 강의의 자료 묶음 | id, course_id, lecture_session_id, term_id, title, distributed_by, distributed_at, source_uri |
| Document | 파일/URL 기반 문서의 논리 정체성 | id, title, format, language |
| DocumentRevision | 문서의 특정 내용 버전 | id, document_id, attachment_id, blob_sha256, parent_revision_id, version_label, observed_at, page_count |
| ContentBlock | 형식에서 추출한 논리 단위 | id, document_revision_id, block_type, ordinal, text 또는 structured payload, locator |
| ExtractorRun | extractor 실행 기록 | id, attachment_id, extractor_id, extractor_version, state, warnings, created_at |

- MaterialSet은 Relation으로 여러 Attachment와 연결됩니다. 나중에 파일을 추가하면 새 Capture를 만들고 Relation으로 같은 MaterialSet에 붙입니다(D7).
- 한 파일을 여러 과목이나 강의에 연결할 때는 원본을 복제하지 않고 Relation을 사용합니다. 같은 바이트를 다시 받아도 Blob은 하나입니다(D2).
- 같은 Document의 새 배포본은 새 DocumentRevision입니다. 이전 revision의 필기·SourceReference는 이전 revision에 남습니다(D11).

지원 포맷 목록과 추출 정책은 [FILE-INGESTION.md](FILE-INGESTION.md)가 기준입니다.

## 8. 검색 인덱스

MVP는 SQLite FTS5와 구조화 필터를 사용합니다. 한국어 tokenizer는 측정 후 결정합니다([DECISIONS.md 미결 결정](DECISIONS.md#미결-결정)). semantic/vector search는 데이터량과 사용 패턴을 확인한 후 추가합니다.

인덱싱 대상: 원본에서 추출한 텍스트(ContentBlock), transcript, OCR, notes/summaries, title/tags/metadata.

인덱스는 파생 데이터이며 DB의 원천 데이터로부터 언제든 재생성할 수 있어야 합니다.

## 9. Multi-device 및 Annotation

| Entity | 설명 | 주요 필드 |
|---|---|---|
| Device | 앱 설치 인스턴스 | id, platform, device_name, app_version, created_at, last_seen_at, state |
| DeviceCredential | 장치 토큰의 해시 | device_id, token_hash, issued_at, revoked_at |
| ChangeFeed | Hub의 엔터티 변경 피드 | sequence, entity_type, entity_id, revision, deleted |
| SyncCursor | 장치별 마지막 수신 sequence | device_id, last_sequence, updated_at |
| Mutation | 클라이언트 변경 요청 | mutation_id, device_id, entity_type, entity_id, base_revision, payload, result |
| AnnotationLayer | (문서 revision, 페이지)별 필기 레이어 | id, document_revision_id, page_index, revision, native_payload_path, standard_payload_path, updated_by_device |
| ConflictRecord | 자동 병합할 수 없는 동시 수정 | id, entity_type, entity_id, base_revision, server_revision, client_mutation_id, client_payload_path, state |

### 9.1 Device와 credential

Device는 기기 하드웨어가 아니라 CampusLife 앱 설치 인스턴스입니다. 재설치하면 새 Device로 등록합니다. 장치 토큰은 OS 보호 저장소에 두고 Hub DB에는 해시만 둡니다(D6).

### 9.2 Annotation

AnnotationLayer는 원본 Attachment를 변경하지 않는 별도 레이어이며 동기화·revision·충돌의 단위입니다(D11).

- `native_payload`: PencilKit drawing archive (iPad 편집 재개용)
- `standard_payload`: 공통 annotation JSON — highlight, text note, freehand stroke를 공통 Locator 좌표(D12)로 표현
- PDF overlay export는 Artifact(파생)이며 editable source가 아닙니다.
- 좌표는 페이지 회전·crop box를 반영해 정규화합니다.
- PencilKit payload는 OS·앱 버전에 따라 달라질 수 있으므로 standard payload를 항상 함께 저장합니다.

### 9.3 Revision 규칙

- Capture, Attachment, Blob, Artifact는 불변입니다.
- Item, Note, AnnotationLayer 등 사용자 편집 데이터는 수정마다 `revision`이 증가합니다.
- Mutation의 `base_revision`이 현재 revision과 다르면 Hub는 덮어쓰지 않고 ConflictRecord를 만들어 양쪽을 보존합니다. 상세 규칙은 [MULTI-DEVICE-ARCHITECTURE.md §6](MULTI-DEVICE-ARCHITECTURE.md)를 따릅니다.
- 삭제는 tombstone(`deleted=true`)으로 전파하고 휴지통·보존 기간을 적용합니다.

### 9.4 Provenance

모든 필기/주석은 document_revision_id, page_index, 정규화 좌표, updated_by_device와 연결됩니다. 필기를 OCR/AI로 해석한 Artifact는 SourceReference로 해당 AnnotationLayer와 revision을 가리킵니다.
