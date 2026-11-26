# CampusLife 공통 도메인 모델

## 1. 핵심 개념

CampusLife의 중심은 강의가 아니라 **Capture → Item → 관계(Relationship)** 구조입니다.

- **Capture:** 사용자가 어떤 정보를 어떤 장치/수단으로 수집했는지 나타내는 원본 수집 단위
- **Attachment:** 녹음, 사진, PDF, 문서 등 Capture에 포함된 파일
- **Item:** 검토 후 사용자가 관리하는 의미 있는 대상(강의, 공지, 행사, 과제, 장학금, 메모 등)
- **Source:** 원본의 출처(직접 입력, 수업 녹음, 학교 공지 URL, 이메일 export 등)
- **Artifact:** transcript, OCR, 요약, 퀴즈, 추출 필드 등 AI가 만든 파생 결과
- **Relation:** 과목·행사·사람·일정·문서 사이의 연결
- **ProcessingJob:** OCR/STT/LLM 등 비동기 처리 작업과 상태

모든 객체는 UUID, 생성/수정 시각, provenance 및 삭제 정책을 갖습니다.

## 2. 도메인 엔터티

### Core

| Entity | 설명 | 주요 필드 |
|---|---|---|
| Capture | 원본 수집 단위 | id, captured_at, source_type, category_hint, status, cloud_policy |
| Attachment | 첨부 원본 파일 | id, capture_id, path, mime_type, size, sha256, captured_at |
| Item | 통합 정보 객체 | id, item_type, title, description, status, importance |
| Source | 정보의 출처 | source_type, uri, publisher, observed_at, trust_level |
| Artifact | 파생 산출물 | artifact_type, provider, model, prompt_version, content_path |
| Tag | 사용자 정의 분류 | name, color, scope |
| Relation | 항목 간 연결 | from_id, relation_type, to_id |
| Reminder | 알림/리마인더 | trigger_at, channel, state |
| ProcessingJob | 처리 상태 | job_type, state, attempts, error, started_at, completed_at |

### Academic

| Entity | 설명 |
|---|---|
| AcademicTerm | 학년도/학기 |
| Course | 과목, 담당 교수/강사, 강의실, 학점 |
| ScheduleSlot | 요일·시간·장소별 강의 시간표 |
| LectureSession | 특정 일자의 실제 강의 |
| Assignment | 과제/프로젝트/제출물 및 마감 |
| Exam | 시험/퀴즈/평가 |
| StudyRecord | 학습/복습 수행 기록 |

### Campus & Personal

| Entity | 설명 |
|---|---|
| CampusEvent | 행사, 세미나, 축제, 특강 |
| Organization | 동아리, 학생회, 스터디, 연구 그룹 |
| Announcement | 학교/학과 공지 |
| Opportunity | 장학금, 공모전, 인턴십, 채용, 대외활동 |
| Person | 사용자가 직접 관리하는 사람 정보(최소 수집) |
| Place | 강의실, 건물, 시설 등 장소 |
| Task | 범용 개인 할 일 |
| Note | 개인 메모 또는 정리 문서 |
| Expense | 선택적 개인 지출 기록(후속 기능) |

성적, 주민등록번호, 금융정보 등 불필요하게 민감한 데이터는 기본 수집 대상이 아닙니다. 성적이나 행정 관련 데이터가 추가되더라도 별도 보호 정책과 사용자 명시 동의가 선행되어야 합니다.

## 3. 통합 Inbox 및 분류

모든 외부 입력은 먼저 Capture로 들어옵니다. 분류 확정 전에는 어떤 도메인 객체에도 강제로 귀속하지 않습니다.

- 강의 녹음 + 판서 사진 → Capture 1개, Attachments N개 → LectureSession 및 Course와 연결
- 학교 행사 공지 PDF → Capture → Announcement → CampusEvent 후보 → 사용자가 확정하면 Planner 일정과 연결
- 장학금 웹 링크 → Capture → Opportunity 후보 → 마감일/요건 추출 후 확인
- 개인 음성 메모 → Capture → Note 및 Task 후보
- 과제 안내 스크린샷 → Capture → Assignment 후보 및 원본 이미지 연결

AI의 분류 결과는 suggested 상태로 저장합니다. 사용자 승인 후 confirmed가 됩니다.

## 4. 공통 상태

- Capture: local_only, queued, uploading, uploaded, verified, ingested, processing, processed, needs_review, failed
- Item review: suggested, confirmed, dismissed
- Task/Assignment: inbox, planned, in_progress, blocked, completed, cancelled
- ProcessingJob: pending, running, retry_wait, succeeded, failed, cancelled

각 상태 전이는 이벤트 로그로 기록하고, 재시도 시 중복 아이템이 생성되지 않도록 stable ID 또는 idempotency key를 사용합니다.

## 5. 시간과 출처

- 저장 시각은 UTC ISO-8601을 기본으로 합니다.
- 사용자 표시 시간대는 기본 Asia/Seoul이며 설정 가능해야 합니다.
- 강의/행사 시간은 timezone을 명시합니다.
- 사진 EXIF, 파일 생성 시각, 사용자가 수정한 시각을 구분합니다.
- 원본 시각과 정규화 시각을 모두 보존합니다.

AI 산출물에는 source_capture_id, attachment_id, page number, audio chunk start/end offset, image captured_at, provider/model/prompt version, generated_at, confidence/review_required를 연결합니다. 사용자가 수정한 내용은 수정 이력을 보존합니다.

## 6. 검색 인덱스

MVP는 SQLite FTS5와 구조화 필터를 사용합니다. semantic/vector search는 데이터량과 사용 패턴을 확인한 후 추가합니다.

인덱싱 대상:
- 원본에서 추출한 텍스트
- transcript
- OCR
- notes / summaries
- title / tags / metadata

인덱스는 파생 데이터이며 재생성 가능해야 합니다.

## 8. 다중 파일 및 문서 도메인 확장

강의 자료는 단일 Attachment가 아니라 여러 파일을 논리적으로 묶는 MaterialSet으로 표현합니다.

| Entity | 설명 | 주요 필드 |
|---|---|---|
| CourseMaterialSet | 교수/조교 배포자료 또는 한 강의의 자료 묶음 | id, course_id, lecture_session_id, title, distributed_by, distributed_at |
| Document | 파일/URL 기반 문서 메타데이터 | id, attachment_id, format, title, language, page_count, extraction_state |
| ContentBlock | 형식에서 추출한 논리 단위 | id, document_id, block_type, text, ordinal, location |
| ExtractorRun | parser/extractor 실행 기록 | extractor_id, version, state, warnings, created_at |
| SourceReference | 파생 문장에서 원본 위치로 이동하는 근거 | artifact_id, attachment_id, locator_type, locator |
| DocumentVersion | 동일 배포 자료의 다른 버전 | document_id, sha256, version_label, observed_at |

MaterialSet은 여러 Attachment를 포함하며, 강의 녹음·현장 사진·교수자 파일이 하나의 LectureSession과 연결될 수 있습니다. 한 파일을 여러 과목이나 강의에 연결할 때는 원본을 복제하지 않고 Relation을 사용합니다.

지원 포맷 계열:
- 문서: PDF, DOCX, HWP, HWPX, TXT, Markdown, RTF, HTML
- 프레젠테이션: PPT, PPTX
- 표/데이터: XLSX, XLS(추후), CSV, TSV
- 이미지: JPEG, PNG, HEIC, WebP 등 지원 형식
- 오디오: M4A, MP3, WAV 등
- 동영상: MP4, MOV 등 및 SRT/VTT 자막
- 압축: ZIP 우선. 내부 파일을 안전 검사 후 선택적으로 추출
- 개발 자료: 소스 코드, JSON, XML, YAML, Jupyter Notebook 등
- 웹 자료: URL, 사용자가 저장한 페이지 및 metadata

ContentBlock locator 예:
- PDF: page, optional bounding box
- PPTX: slide index, shape index
- DOCX/HWP: heading path, paragraph/table index
- XLSX: sheet, cell/range
- Audio: start_ms/end_ms
- Video: start_ms/end_ms/frame timestamp
- Image: region
- URL: canonical URL, page section/anchor

실제 schema에서는 locator를 schema_version이 있는 구조로 검증합니다. 모든 AI 생성물은 하나 이상의 SourceReference를 제공하고, 정확한 위치를 산출할 수 없으면 문서 단위 근거임을 명시합니다.


## 9. Multi-device 및 Annotation 도메인 확장

| Entity | 설명 | 주요 필드 |
|---|---|---|
| Device | 앱 설치 인스턴스 | id, platform, device_name, app_version, created_at, last_seen_at, state |
| DeviceSession | 장치 인증 세션 | id, device_id, credential_ref, issued_at, expires_at, revoked_at |
| SyncCursor | 장치별 마지막 수신 이벤트 위치 | device_id, stream_id, last_event_sequence, updated_at |
| SyncEvent | 서버가 발행하는 변경 이벤트 | event_id, sequence, entity_type, entity_id, revision, operation, created_at |
| Mutation | 클라이언트가 제출하는 변경 요청 | mutation_id, device_id, base_revision, payload, state |
| Annotation | 문서/페이지에 연결된 필기 또는 주석 | id, document_id, revision_id, page_index, annotation_type, created_by_device |
| AnnotationLayer | 특정 사용자/문서/페이지의 주석 묶음 | id, document_id, page_index, format, content_path, revision |
| DocumentRevision | 원본 또는 파생 문서 버전 | id, document_id, sha256, parent_revision_id, created_by_device |
| ConflictRecord | 자동 병합할 수 없는 동시 수정 | id, entity_id, base_revision, local_revision, remote_revision, state |

### 9.1 Device와 Session

Device는 기기 하드웨어 자체가 아니라 CampusLife 앱 설치 인스턴스 단위로 식별합니다. 앱 재설치 시 새 ID로 등록할 수 있습니다. credential은 운영체제 보호 저장소에 보관하고 DB에는 secret 자체가 아닌 참조/식별자만 둡니다.

### 9.2 Annotation

Annotation은 원본 Attachment를 변경하지 않는 별도 레이어입니다.

- PencilKit 원본 drawing archive
- 페이지별 annotation JSON 및 좌표계
- highlight, text note, freehand drawing 등 공통 annotation type
- PDF overlay export는 파생 artifact
- 원본 문서의 페이지 크기/회전/자르기 정보를 고려해 좌표 정규화
- 기기·앱 버전에 따라 PencilKit native payload가 다를 수 있으므로 공통 렌더링을 위한 표준 표현 또는 PDF overlay를 함께 보존

### 9.3 Multi-device Revision Rules

- Capture/Attachment 원본은 append-only/immutable
- Annotation과 Note 등 사용자 편집 데이터는 revision을 증가시킴
- 서로 다른 데이터 영역의 변경은 독립적으로 병합 가능
- 같은 문단/같은 annotation layer가 동시에 수정되면 blind last-write-wins로 덮어쓰지 않음
- base_revision이 최신이 아니면 server는 conflict response를 반환하고 양쪽 revision을 보존
- 자동 병합이 안전한 annotation event는 event merge 가능성을 검토하되 실제 구현은 format test 후 활성화
- 사용자에게 비교/병합/한쪽 복사 선택지를 제공
- 삭제는 tombstone event로 전파하고 복구/휴지통 정책을 적용

### 9.4 Provenance

모든 필기/주석은 document_id, document_revision_id, page_index, coordinate_space, source_device_id와 연결합니다. AI가 필기 내용을 OCR/해석한 결과도 원본 annotation layer로 역추적할 수 있어야 합니다.
