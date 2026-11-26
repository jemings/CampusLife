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
