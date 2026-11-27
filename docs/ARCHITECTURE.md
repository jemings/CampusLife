# CampusLife 시스템 아키텍처

구조 결정의 근거는 [DECISIONS.md](DECISIONS.md), 장치 간 동기화·인증 세부는 [MULTI-DEVICE-ARCHITECTURE.md](MULTI-DEVICE-ARCHITECTURE.md), 파일 형식별 처리는 [FILE-INGESTION.md](FILE-INGESTION.md)가 기준입니다. 이 문서는 전체 구성과 각 실행 단위의 책임을 정리합니다.

## 1. 목표 배치

    Galaxy Z Fold8 (Android)              iPad Pro 13 (iPadOS)
      - 시간표 / 녹음 / 사진 / 공유          - 자료 열람 / Pencil 필기 / 공유
      - Room + Outbox + WorkManager          - SQLite cache + outbox
                 \                              /
                  \   HTTPS (Tailscale, D5)    /
                   v                          v
    Windows 11 laptop — CampusLife Hub
      - tailscale serve → Agent (127.0.0.1)
      - Sync API / admin API / ingest verifier
      - SQLite (메타데이터·작업 큐·변경 피드·FTS)
      - blobs/ (원본과 파생 파일 모두 sha256 불변 Blob)
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
- 녹음 시작/정지 및 상태 표시, 세그먼트 파일 + 단조 시계 타임라인 기록(D15)
- 강의 중 사진 촬영과 촬영 시각·녹음 세션 기준 오프셋 저장
- Android 공유 메뉴·Storage Access Framework에서 URL, 텍스트, 다중 파일 수집
- 원본 파일을 앱 전용 Outbox에 복사하면서 크기·SHA-256 계산
- 연결 가능 시 Blob을 올리고 `capture.commit`의 ACK 기록
- 과목·시간표 Item을 오프라인에서 만들고 Mutation으로 전송
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
- 로컬 웹 UI (admin 포트, localhost 전용) — MVP의 노트 열람·Suggestion 검토·검색 화면

Agent 내부 모듈과 소유 관계:

| 모듈 | 소유하는 것 | 다른 모듈이 쓰는 방법 |
|---|---|---|
| `blobs` | Blob 업로드 세션, 검증, 원자 이동, 정리(GC), purge | `open_upload()`, `finalize()` |
| `mutations` | 사용자 데이터의 유일한 쓰기 경로: `capture.commit`, Item CAS patch, `suggestion.accept`, ConflictRecord, `change_seq` 증가 | `apply(mutations)` |
| `jobs` | ProcessingJob 큐, 재시도, job_key 멱등, subprocess 격리 | `enqueue(job_key, …)` |
| `gateway` | cloud_policy 유효값(min) 계산, budget guard, audit_log, provider 호출 | 모든 provider 호출은 `ProviderGateway.call()`만 |
| `providers` | STT/Vision/Text/Embedding adapter | `gateway`만 import 가능 |
| `extractors` | 형식별 FileExtractor, PDF 렌디션 | `jobs` 워커가 subprocess로 실행 |
| `sync_api` | `/v1` 경로, 장치 토큰 인증, changes 피드 | `blobs`·`mutations` 호출 |
| `admin_api` | pairing secret 발급, revoke, 로컬 웹 UI | localhost 전용 포트, 쓰기는 `mutations.apply()` |

로컬 Windows import(드래그앤드롭)와 웹 UI의 편집도 `mutations.apply()`를 호출합니다. 수집·편집 경로가 늘어도 검증·멱등·충돌 판정·피드 기록은 한 곳에만 있습니다.

노트북이 켜져 있어야 처리됩니다. AC 전원 연결 시 절전 방지 설정을 안내할 수 있지만 배터리 사용 중 절전 정책은 존중합니다.

### AI Provider Adapters

기능별 provider interface:
- TranscriptionProvider
- VisionProvider
- TextGenerationProvider
- EmbeddingProvider (후속)
- DocumentExtractionProvider (후속)

첫 구현은 OpenAI API adapter를 제공할 수 있지만 모델 ID는 코드에 흩어 놓지 않고 설정으로 관리합니다. 모델 ID·provider는 job_key의 일부이므로 설정을 바꾸면 새 Artifact가 생깁니다(D4). 모든 호출은 `ProviderGateway`를 거칩니다(D8). 신규 파일 전사 권장 모델, 파일 크기 제한과 지원 형식은 공식 문서에서 확인합니다:
https://developers.openai.com/api/docs/guides/transcription

이미지 분석:
https://developers.openai.com/api/docs/guides/images-vision

## 3. Capture 및 Outbox (D7)

각 Capture는 생성한 클라이언트가 만든 UUIDv4 ID를 가집니다. 강의 자료든 공지 스크린샷이든 같은 흐름을 씁니다.

- 클라이언트 상태는 로컬 DB 하나(Room / SQLite)에 둡니다. manifest 파일과 READY 마커는 쓰지 않습니다.
- Capture는 `open → sealed`로 바뀝니다. 녹음 중에는 open이고, 정지하면 봉인됩니다. 앱이 녹음 중 종료되면 다음 실행에서 남은 세그먼트로 봉인하고 "복구된 녹음"으로 표시합니다.
- open 상태에서도 닫힌 세그먼트·사진 Blob은 먼저 올릴 수 있습니다. `capture.commit`은 봉인 후에만 보냅니다.
- 봉인 후 생긴 자료는 새 Capture로 만들고 Relation으로 같은 LectureSession에 붙입니다.
- 정책 필드는 Attachment의 `cloud_policy` 하나입니다(D8).

## 4. Store-and-forward Sync

제품 동기화 경로는 Windows Hub의 HTTPS Sync API 하나입니다. Blob 업로드 → `capture.commit`(ACK), 변경 피드, Mutation CAS의 세부는 [MULTI-DEVICE-ARCHITECTURE.md §3](MULTI-DEVICE-ARCHITECTURE.md)이 기준입니다. SFTP·FolderSync는 쓰지 않습니다.

상태는 소유자별로 분리합니다(D1). 클라이언트는 봉인·전송 상태를, Hub는 `ProcessingJob.state`와 계산된 `processing_summary`를 소유합니다.

## 5. AI 처리 파이프라인

- Ingest: `capture.commit` 트랜잭션에서 초기 job 등록
- Extract: magic bytes 판별, EXIF, 문서 구조, OCR, PDF 렌디션 (`extractors`, subprocess)
- Transcribe: 녹음 세그먼트를 타임라인으로 이어 붙이고 API 제한에 맞게 분할. 오프셋은 Capture 타임라인 절대 시각으로 변환(D13)
- Analyze: 슬라이드/사진/PDF 페이지를 Vision 또는 OCR 처리
- Correlate: 사진의 단조 시계 값과 녹음 타임라인 연결(D15)
- Synthesize: 입력 ContentBlock에 `[B:<id>]` 태그를 붙여 LLM에 넣고, 출력 인용을 Hub가 검증해 Locator로 치환(D13)
- Suggest: 과제, 마감, 일정, 공지 분류를 Suggestion으로 생성. Item은 만들지 않음(D10)
- Index: FTS 인덱스 갱신

각 단계는 job_key로 멱등한 ProcessingJob입니다. 외부 provider를 부르는 단계는 `ProviderGateway`를 통과해야 하고, 입력이 여러 개면 가장 엄격한 정책을 따릅니다(D8). word-level timestamp가 없는 전사 결과는 구간 단위로만 연결하고 정밀 타임스탬프를 꾸며내지 않습니다.

## 6. 저장 설계 (D2, D16, D21)

Windows 파일시스템이 바이트의 원천이고, SQLite에는 메타데이터, 관계, 작업 상태, 변경 피드, 인덱스를 저장합니다.

    %LOCALAPPDATA%\CampusLife\         (설치 시 선택, 동기화 폴더 아래나 \\ 드라이브면 거부)
    ├── hub.lock                       # 단일 인스턴스 lock (E11)
    ├── admin.token                    # admin 토큰, DPAPI로 보호 (E18)
    ├── admin.port                     # 서빙 중인 Hub의 admin 포트, 서빙하는 동안만 있음
    ├── database\campuslife.sqlite3     # WAL, synchronous=FULL. purge_journal·backups 표 포함
    ├── blobs\ab\cd\<sha256>            # 원본·Artifact 본문·필기 payload, 읽기 전용
    ├── staging\                       # 해시를 확인하기 전 파일 (blobs와 같은 볼륨, 기동 때 잔여물 삭제)
    ├── uploads\<session uuid>\<index>  # 업로드 세션의 chunk (blobs와 같은 볼륨, 7일 지난 세션 정리)
    ├── logs\hub.log                   # 지금 파일 + 이전 5개(각 5 MiB), 비밀 값·강의 본문 가림 (E19)
    ├── cache\                         # 이후(#11 등): FTS 인덱스, 썸네일, overlay 렌더링 — 백업 안 함
    └── exports\                       # 이후: 사람이 읽는 트리 복사본

    <백업 드라이브의 폴더>\
    ├── campuslife-backup.json         # 이 폴더에 백업하는 Hub의 hub_id (marker)
    ├── generations\<name>\             # campuslife.sqlite3(스냅샷), manifest.json(마지막에 씀)
    └── blobs\ab\cd\<sha256>            # 세대끼리 공유하는 Blob

- 같은 바이트는 한 번만 저장하고 덮어쓰지 않습니다.
- 삭제는 tombstone → 휴지통(30일) → 참조 없는 Blob 정리 순서입니다. 교수자·참여자 요청은 즉시 purge합니다.
- purge는 Blob 단위입니다. 그 Blob의 Attachment, 파생 Artifact·Suggestion(전이 폐쇄), 그 끝을 가진 Relation, 인덱스·렌디션을 지우고, fork된 Note는 남겨 근거가 지워졌다고 표시합니다(U2). DB 삭제와 `purge_journal` 행을 한 트랜잭션에 commit한 뒤 훅과 파일 삭제를 실행하고, 끊기면 기동 때 마저 합니다(E16).
- Artifact는 재실행 결과가 다를 수 있으므로 백업 대상입니다. 캐시는 FTS·썸네일·overlay뿐입니다.
- 백업은 수동이고 암호화하지 않습니다(U1, U3). DB 스냅샷 → 참조 Blob 증분 복사 → 세대 manifest → `backed_up_at`(그 Blob을 처음 담은 세대에 한 번) 순서입니다. 백업은 marker가 없거나 다른 Hub의 폴더를 거부하고, 자기가 쓴 파일만 지웁니다.
- 복원은 빈 data root에 하고 새 `epoch`를 발급하며, active인 client 장치를 모두 dormant로 바꿉니다. revoked 장치와 Hub 자신(`windows_hub`)은 그대로입니다(D3, D21 U6).
- `hub fsck`로 해시와 고아·누락 Blob, 끝나지 않은 purge를 점검합니다.
- 설치·업데이트·제거와 백업·복원 운영은 [`services/agent/README.md`](../services/agent/README.md)에 있습니다.

## 7. 검색

MVP 검색:
- SQLite FTS5 전문 검색, 모든 텍스트 NFC 정규화 (한국어 분석기는 겹침 2-gram 선토큰화 — DECISIONS D27)
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

강의 자료 하나를 단일 파일로 가정하지 않습니다. 강의 녹음, 수업 중 사진, 교수/조교가 배포한 여러 파일(PDF, 슬라이드, 문서, 스프레드시트, HWP/HWPX, 코드, 자막, 동영상, 링크 등)은 여러 Capture로 들어와 Relation으로 같은 LectureSession·Course Item에 묶입니다. 별도의 자료 묶음 엔터티는 두지 않습니다(D2).

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
| iPadOS / iPad Pro | 자료 열람, PDF 주석, Apple Pencil 필기, AI 노트 검토, Share Extension | SQLite(GRDB 등) + App Group outbox |
| Windows | Hub: API, Blob, SQLite, AI jobs, 검색, 로컬 UI | NTFS + SQLite |

클라이언트별 UI와 플랫폼 API는 네이티브로 구현하되 Domain ID, API contract, sync semantics, Locator schema, 로컬 sync 테이블 DDL은 `packages/api-contracts` 하나를 공유합니다. 원천은 JSON Schema이고, 세 언어 구현은 golden fixture로 검증합니다. 코드 생성은 선택입니다(D17).

동기화 프로토콜, 장치 등록, 충돌 정책, Annotation 데이터 흐름은 [MULTI-DEVICE-ARCHITECTURE.md](MULTI-DEVICE-ARCHITECTURE.md)를 따릅니다.
