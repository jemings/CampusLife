# CampusLife 시스템 아키텍처

구조 결정의 근거는 [DECISIONS.md](DECISIONS.md), 장치 간 동기화·인증 세부는 [MULTI-DEVICE-ARCHITECTURE.md](MULTI-DEVICE-ARCHITECTURE.md), 파일 형식별 처리는 [FILE-INGESTION.md](FILE-INGESTION.md)가 기준입니다. 이 문서는 전체 구성과 각 실행 단위의 책임을 정리합니다.

## 1. 목표 배치

    Galaxy Z Fold8 (Android)              iPad Pro 13 (iPadOS)
      - 시간표 / 녹음 / 사진 / 공유          - 자료 열람 / Pencil 필기 / 공유
      - Room + Outbox + WorkManager          - 로컬 cache + pending mutation
                 \                              /
                  \   HTTPS (Tailscale, D5)    /
                   v                          v
    Windows 11 laptop — CampusLife Hub
      - tailscale serve → Agent (127.0.0.1)
      - Sync API / admin API / ingest verifier
      - SQLite (메타데이터·작업 큐·변경 피드·FTS)
      - blobs/ (불변 원본) + derived/ (파생 결과)
                 |
                 v
      AI provider adapters (STT / Vision / Text / Embedding)
                 |
                 v   cloud_policy가 허용할 때만 (D8)
      External AI provider

Tailscale은 장치 간 사설 연결과 TLS 인증서를 제공합니다. 파일 저장, 오프라인 전달, 처리 큐, 앱 인증은 CampusLife가 구현합니다. 노트북이 종료/절전 상태이면 전송은 보류되고 각 모바일 장치의 Outbox에 원본이 남습니다.

## 2. 실행 단위

### Android Capture App

권장 기반 기술: Kotlin, Jetpack Compose, Room, WorkManager, CameraX, MediaRecorder.

책임:
- 시간표와 과목 메타데이터의 모바일 캐시
- 녹음 시작/정지 및 상태 표시, 세그먼트 파일 기록(D13)
- 강의 중 사진 촬영과 촬영 시각·녹음 세션 기준 오프셋 저장
- Android 공유 메뉴·Storage Access Framework에서 URL, 텍스트, 다중 파일 수집
- 원본 파일을 앱 전용 Outbox에 복사, manifest·크기·SHA-256 생성
- 연결 가능 시 업로드하고 첨부별 ACK 기록
- ACK 전 원본 자동 삭제 금지, ACK 후 정리는 D14 정책
- 동기화/AI 처리 상태 표시

Android 제약:
- 시간표 알람은 녹음을 자동 시작하는 권한이 아닙니다.
- 녹음은 사용자가 앱 또는 알림에서 명시적으로 시작합니다.
- Android 14 이상에서 microphone Foreground Service type과 해당 권한을 선언합니다.
- 앱이 보이지 않는 상태에서 microphone FGS를 임의 시작하지 않습니다.
- 사진 촬영은 사용자 조작으로 실행합니다.
- WorkManager는 네트워크 조건 기반 재시도에 사용합니다. 즉각 실행이나 정확한 주기 실행을 보장한다고 가정하지 않습니다.
- 배터리 최적화, Doze, OEM 백그라운드 제한에 따른 지연을 제품 UX와 테스트에 반영합니다.

참고:
- https://developer.android.com/develop/background-work/services/fgs/service-types
- https://developer.android.com/develop/background-work/services/fgs/restrictions-bg-start

### iPadOS Study App

[MULTI-DEVICE-ARCHITECTURE.md §7](MULTI-DEVICE-ARCHITECTURE.md)과 [apps/ipados/README.md](../apps/ipados/README.md)를 따릅니다.

### Windows Hub (Agent)

권장 기반 기술:
- Windows 11
- Python 3.x, FastAPI
- SQLite (메타데이터, 작업 큐, 변경 피드, FTS)
- 로컬 파일시스템 (Blob과 파생 결과)
- `tailscale serve` (HTTPS 종단, D5)
- Windows Task Scheduler (사용자 로그인 시 Agent 시작)
- 로컬 웹 UI (admin 포트, localhost 전용)

Agent 내부 모듈과 소유 관계:

| 모듈 | 소유하는 것 | 다른 모듈이 쓰는 방법 |
|---|---|---|
| `store` | Blob 저장, Attachment/Capture 등록, 변경 피드 기록 | `ingest_capture()`, `finalize_attachment()` 함수 호출만 |
| `jobs` | ProcessingJob 큐, 재시도, job_key 멱등 | `enqueue(job_key, …)` |
| `policy` | cloud_policy 유효값 계산과 provider 호출 허용 판단 | provider adapter 호출 직전 `require_cloud_allowed()` |
| `providers` | STT/Vision/Text/Embedding adapter | `jobs` 워커만 호출 |
| `extractors` | 형식별 FileExtractor | `jobs` 워커만 호출 |
| `sync_api` | HTTP 경로, 장치 인증, upload session | `store`·`jobs` 함수 호출 |
| `admin_api` | pairing 승인, revoke, 상태 UI | localhost 전용 포트 |

로컬 Windows import(드래그앤드롭)도 `sync_api`가 아니라 `store.ingest_capture()`를 직접 호출하는 같은 경로를 사용합니다. 수집 경로가 늘어도 검증·멱등·피드 기록은 `store` 한 곳에만 있습니다.

노트북이 켜져 있어야 처리됩니다. AC 전원 연결 시 절전 방지 설정을 안내할 수 있지만 배터리 사용 중 절전 정책은 존중합니다.

### AI Provider Adapters

기능별 provider interface:
- TranscriptionProvider
- VisionProvider
- TextGenerationProvider
- EmbeddingProvider (후속)
- DocumentExtractionProvider (후속)

첫 구현은 OpenAI API adapter를 제공할 수 있지만 모델 ID는 코드에 흩어 놓지 않고 설정으로 관리합니다. 모델 ID·provider는 job_key의 일부이므로 설정을 바꾸면 새 Artifact가 생깁니다(D4). 신규 파일 전사 권장 모델, 파일 크기 제한과 지원 형식은 공식 문서에서 확인합니다:
https://developers.openai.com/api/docs/guides/transcription

이미지 분석:
https://developers.openai.com/api/docs/guides/images-vision

## 3. Capture 및 Outbox

각 Capture는 클라이언트가 만든 UUIDv7 ID를 가집니다(D4). 강의 자료든 공지 스크린샷이든 같은 Outbox 흐름을 사용합니다.

클라이언트 로컬 Outbox 예시:

    outbox/<capture-id>/
    ├── manifest.json
    └── payload/
        ├── rec-0001.m4a      # 녹음 세그먼트 (D13)
        ├── rec-0002.m4a
        ├── img-0001.jpg
        └── lecture-04.pdf

manifest 필드는 [MULTI-DEVICE-ARCHITECTURE.md §3.2](MULTI-DEVICE-ARCHITECTURE.md)의 Register Capture 계약과 같습니다. 정책 필드는 `cloud_policy` 하나입니다(D8).

manifest는 로컬에서 확정된 뒤 바뀌지 않습니다(D7). 녹음 중에는 manifest를 확정하지 않고, 녹음 종료 시 세그먼트 목록과 함께 확정합니다. 앱이 녹음 중 종료되면 다음 실행에서 남은 세그먼트로 manifest를 확정하고 "중단된 녹음"으로 표시합니다.

## 4. Store-and-forward Sync

제품 동기화는 Windows Hub의 공통 HTTPS Sync API 하나입니다. 업로드 순서(manifest 등록 → 첨부별 chunk 업로드 → 해시 검증 → atomic finalize → 트랜잭션 등록 → ACK)와 멱등 규칙은 [MULTI-DEVICE-ARCHITECTURE.md §3](MULTI-DEVICE-ARCHITECTURE.md)이 기준입니다.

상태는 소유자별로 분리합니다(D1). 클라이언트는 첨부별 `transfer_state`(pending → uploading → acked, 오류 시 retry_wait / failed_needs_attention)를, Hub는 `ingest_state`·`extraction_state`·`ProcessingJob.state`를 소유합니다.

Phase 0의 FolderSync+SFTP는 Tailscale 경유 연결과 노트북 sleep/복귀 동작을 확인하는 PoC이며, Agent에는 SFTP 수신 경로를 구현하지 않습니다.

## 5. AI 처리 파이프라인

- Ingest: manifest 및 파일 검증 (`store`)
- Extract: MIME/magic bytes 판별, EXIF, 문서 구조, OCR (`extractors`)
- Transcribe: 녹음 세그먼트를 API 제한에 맞게 나누고 세션 기준 오프셋 유지
- Analyze: 슬라이드/사진/PDF 페이지를 Vision 또는 OCR 처리
- Correlate: 사진의 녹음 세션 기준 오프셋과 transcript 구간 연결(D13)
- Synthesize: 과목/행사/주제별 요약과 상세 노트 생성
- Extract actions: 과제, 마감일, 일정, 후속 행동 후보를 `review_state=suggested` Item으로 생성
- Review: 일정/과제 등 상태 변경은 사용자 확인 후 반영
- Index: FTS 인덱스 및 추후 embedding 인덱스 갱신

각 단계는 job_key로 멱등한 ProcessingJob입니다. 외부 provider를 부르는 단계는 호출 직전 `policy.require_cloud_allowed()`를 통과해야 하며, 입력이 여러 개면 가장 엄격한 정책을 따릅니다(D8).

모든 AI 산출물에는 공통 Locator(D12)로 provenance를 기록합니다. word-level timestamp가 제공되지 않는 전사 결과는 구간 단위로만 연결하고 정밀 타임스탬프를 꾸며내지 않습니다.

## 6. 저장 설계 (D2)

Windows 파일시스템이 binary 원본의 source of truth입니다. SQLite에는 파일 bytes가 아니라 메타데이터, 관계, 작업 상태, 변경 피드, 인덱스를 저장합니다.

    D:\CampusLifeData\          (설정 가능한 data root)
    ├── blobs\sha256\ab\<sha256>    # 불변 원본, 읽기 전용
    ├── uploads\<session-id>\       # 검증 전 임시 chunk
    ├── derived\<artifact-id>\      # Artifact 파일 (transcript, notes, overlay PDF, 변환 PDF 등)
    ├── database\campuslife.sqlite3
    ├── cache\
    ├── logs\
    └── backups\

- 원본은 내용 주소로 한 번만 저장하고 덮어쓰지 않습니다.
- 파생 결과는 Artifact ID 단위 디렉터리에 두며 job_key로 재생성할 수 있습니다.
- 임시 업로드(`uploads`)와 원본(`blobs`)은 같은 볼륨에 두어 atomic rename이 가능하게 합니다.
- 삭제는 tombstone → 휴지통/보존 기간 → 참조 없는 Blob 정리 순서입니다.
- 백업은 SQLite Online Backup API로 DB 스냅샷을 만든 뒤 그 스냅샷이 참조하는 Blob을 같은 세대에 복사합니다. 백업 완료 시 해당 Blob의 Attachment에 `backed_up_at`을 기록합니다(D14).
- 사람이 읽을 수 있는 `과목/날짜/원래파일명` 트리는 export 기능으로 생성합니다.

## 7. 검색

MVP 검색:
- SQLite FTS5 전문 검색 (한국어 tokenizer는 측정 후 결정 — DECISIONS 미결 결정)
- 날짜/도메인/과목/태그 필터
- 상세 보기에서 원본 파일과 AI 근거 연결

후속:
- semantic search / embeddings
- 관계 그래프와 학기 지식 맵
- 시험 대비 질의와 복습

유용한 공식 문서:
- Tailscale 가격/Personal: https://tailscale.com/pricing
- Android 설치: https://tailscale.com/docs/install/android
- Windows 설치: https://tailscale.com/docs/install/windows
- Tailscale serve: https://tailscale.com/kb/1312/serve

## 8. 다중 파일 강의 자료 수집 및 분석

강의 자료 하나를 단일 파일로 가정하지 않습니다. 하나의 CourseMaterialSet은 강의 녹음, 수업 중 사진, 교수/조교가 배포한 여러 파일(PDF, 슬라이드, 문서, 스프레드시트, HWP/HWPX, 코드, 자막, 동영상, 링크 등)을 Relation으로 묶습니다.

수집 경로:
- Android 공유 메뉴: LMS/브라우저/메일/메신저에서 파일 또는 링크를 CampusLife로 공유
- Android 파일 선택기: Downloads 및 사용자가 접근을 허용한 문서 선택
- 강의 중 앱 카메라/녹음: 사진·오디오를 같은 LectureSession에 연결
- iPad Share Extension / fileImporter
- Windows Inbox: 브라우저 다운로드, USB, 드래그앤드롭

LMS/메일의 비공식 자동 로그인이나 무단 크롤링을 전제하지 않습니다. 사용자가 공유하거나 다운로드한 파일 및 허용된 공식 API/export를 우선합니다.

파일 처리 파이프라인(Preserve → Validate → Extract → Normalize → Analyze → Link → Review → Index), extractor 계약, 지원 형식, 파일 안전성 규칙은 [FILE-INGESTION.md](FILE-INGESTION.md)가 기준입니다.

## 9. Multi-device Client Architecture

| Client | 주요 책임 | 로컬 저장 |
|---|---|---|
| Android / Fold8 | 시간표, 녹음, 사진, Share Intent, offline Outbox | Room + app-private files |
| iPadOS / iPad Pro | 자료 열람, PDF 주석, Apple Pencil 필기, AI 노트 검토, Share Extension | SwiftData 또는 SQLite 계열 + app sandbox |
| Windows | Hub: API, Blob, SQLite, AI jobs, 검색, 로컬 UI | NTFS + SQLite |

클라이언트별 UI와 플랫폼 API는 네이티브로 구현하되 Domain ID, API contract, sync semantics, Locator·provenance schema는 `packages/api-contracts` 하나를 공유합니다. 플랫폼 간 코드 강제 공유보다 OpenAPI/JSON Schema 계약과 그로부터 생성한 타입을 우선합니다.

동기화 프로토콜, 장치 등록, 충돌 정책, Annotation 데이터 흐름은 [MULTI-DEVICE-ARCHITECTURE.md](MULTI-DEVICE-ARCHITECTURE.md)를 따릅니다.
