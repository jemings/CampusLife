# CampusLife 시스템 아키텍처

## 1. 목표 배치

    Galaxy Z Fold8 (Android app)
      - 시간표 / Capture / Recorder / Camera / Share-to-CampusLife
      - Room DB + immutable Outbox + upload retry
                         |
                         | Tailscale private network
                         v
    Windows 11 laptop (CampusLife companion)
      - Local API / SFTP bootstrap / Inbox / file verifier
      - Python Agent + SQLite job queue + local UI
      - Local file store + metadata + FTS search
             |             |                 |
             v             v                 v
            STT          Vision          Text generation
             \             |                 /
              \------------+----------------/
                           |
                           v
                    External AI provider
                  (only when item policy allows)

Tailscale은 장치 간 IP 연결과 암호화된 사설 네트워크를 제공합니다. 파일 저장, 오프라인 전달, 처리 큐는 CampusLife가 구현해야 합니다. 노트북이 종료/절전 상태이면 전송은 보류되고 Android Outbox에 원본이 남습니다.

## 2. 실행 단위

### Android Capture App

권장 기반 기술: Kotlin, Jetpack Compose, Room, WorkManager, CameraX, MediaRecorder/AudioRecord.

책임:
- 시간표와 과목 메타데이터의 모바일 캐시
- 녹음 시작/정지 및 상태 표시
- 강의 중 사진 촬영과 촬영 시각 저장
- Android 공유 메뉴에서 URL, 텍스트, PDF, 이미지 수집
- 원본 파일을 앱 전용 Outbox에 보관
- manifest, 파일 크기, SHA-256 생성
- 연결 가능 시 업로드하고 서버 ACK 확인
- ACK 전 원본 자동 삭제 금지
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

### Windows Companion

권장 기반 기술:
- Windows 11
- Python 3.x, FastAPI 또는 동등한 로컬 API
- SQLite (job 상태, 메타데이터)
- 로컬 파일시스템 (원본과 산출물)
- Windows Task Scheduler (사용자 로그인 시 에이전트 시작)
- 선택적 로컬 웹 UI

책임:
- 모바일 장치 파일 수신
- 임시 업로드와 최종 저장 분리
- 크기/해시/manifest 검증
- ACK 발행
- SQLite 기반 멱등 작업 큐
- AI 처리와 재시도, 비용/사용량 기록
- 인덱싱과 검색
- 백업과 데이터 내보내기

노트북이 켜져 있어야 처리됩니다. AC 전원 연결 시 절전 방지 설정을 안내할 수 있지만 배터리 사용 중 절전 정책은 존중합니다.

### AI Provider Adapters

기능별 provider interface:
- TranscriptionProvider
- VisionProvider
- TextGenerationProvider
- EmbeddingProvider (후속)
- DocumentExtractionProvider (후속)

첫 구현은 OpenAI API adapter를 제공할 수 있지만 모델 ID는 코드에 흩어 놓지 않고 설정으로 관리합니다. 신규 파일 전사 권장 모델, 파일 크기 제한과 지원 형식은 공식 문서에서 확인합니다:
https://developers.openai.com/api/docs/guides/transcription

이미지 분석:
https://developers.openai.com/api/docs/guides/images-vision

## 3. Capture 및 Outbox

각 Capture는 UUID 기반입니다. 강의 자료든 공지 스크린샷이든 같은 Outbox 흐름을 사용합니다.

    capture_<capture-id>/
    ├── manifest.json
    ├── payload/
    │   ├── audio.m4a
    │   ├── images/
    │   ├── documents/
    │   └── attachments/
    └── READY

manifest 필수 필드:
- schema_version
- capture_id
- created_at_utc
- device_id
- category_hint
- source_type
- course_id / event_id (알고 있는 경우)
- timezone
- attachments[]: attachment_id, relative_path, mime_type, size_bytes, sha256, captured_at
- cloud_processing_allowed
- user_notes

READY는 Android 로컬 기록이 완료되었다는 의미입니다. 노트북은 전송 파일의 무결성을 별도로 확인해야 합니다.

## 4. Store-and-forward Sync

초기 연결 PoC:
- Tailscale: Android와 Windows 노트북을 같은 Tailnet에 등록
- Windows OpenSSH Server: SFTP 수신
- FolderSync: SFTP 업로드 예약 및 재시도 검증

이 조합은 빠른 PoC용입니다. 제품용 Android 앱에서는 다음 프로토콜을 구현합니다.

1. Android가 capture manifest를 전송하고 서버가 upload session을 만듭니다.
2. 파일을 임시 경로에 전송합니다. 큰 파일은 청크/재개 가능한 전송을 지원합니다.
3. Windows agent가 파일 크기와 SHA-256을 검증합니다.
4. 검증된 파일을 최종 경로로 atomic rename합니다.
5. DB에 capture와 attachment를 등록하고 ACK를 반환합니다.
6. Android는 ACK를 기록합니다. 보존 정책이 정해지기 전까지 원본을 자동 삭제하지 않습니다.
7. 동일 capture_id/attachment_id 재전송은 멱등 처리합니다.

상태 예시:
LOCAL_READY → UPLOADING → UPLOADED → VERIFIED → INGESTED → PROCESSING → COMPLETED

오류 상태는 RETRY_WAIT 또는 FAILED_NEEDS_ATTENTION으로 분리합니다.

SFTP PoC는 목적지에 노트북이 보일 때만 진행됩니다. 노트북이 오프라인이면 다음 동기화 주기에 재시도합니다. Tailscale 자체가 파일을 큐잉하지 않습니다.

## 5. AI 처리 파이프라인

- Ingest: manifest 및 파일 검증
- Extract: MIME/type sniffing, EXIF, PDF metadata, OCR
- Transcribe: 오디오를 API 제한에 맞게 나누고 구간 오프셋 유지
- Analyze: 슬라이드/사진/PDF 페이지를 Vision 또는 OCR 처리
- Correlate: 촬영 시각과 transcript segment 시간 구간 연결
- Synthesize: 과목/행사/주제별 요약과 상세 노트 생성
- Extract actions: 과제, 마감일, 일정, 후속 행동 후보 생성
- Review: 일정/과제 등 상태 변경은 사용자 확인 후 반영
- Index: FTS 인덱스 및 추후 embedding 인덱스 갱신

모든 AI 산출물에는 원본과 page/image/time range 등 provenance를 기록합니다. word-level timestamp가 제공되지 않는 전사 결과는 구간 단위로만 연결하고 정밀 타임스탬프를 꾸며내지 않습니다.

## 6. 저장 설계

Windows 파일시스템이 binary 원본의 source of truth입니다. SQLite에는 파일 bytes가 아니라 메타데이터, 관계, job 상태, 인덱스를 저장합니다.

    D:\CampusLifeData\
    ├── inbox/
    ├── originals/YYYY/<capture-id>/
    ├── derived/
    │   ├── transcripts/
    │   ├── notes/
    │   ├── summaries/
    │   ├── extracted/
    │   └── exports/
    ├── database/campuslife.sqlite3
    ├── cache/
    ├── logs/
    └── backups/

- 원본은 불변 보관을 기본으로 합니다.
- 파생 결과는 pipeline/model/prompt 버전을 기록하고 재생성 가능하게 합니다.
- 임시 업로드와 최종 원본 디렉터리를 분리합니다.
- 삭제는 휴지통/보존 기간/사용자 확인을 거칩니다.
- SQLite Backup API 또는 DB를 일관성 있게 복사하는 방식으로 백업합니다.

## 7. 검색 및 배포

MVP 검색:
- SQLite FTS5 전문 검색
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
- Windows OpenSSH: https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_install_firstuse

인터넷 공유기 포트 포워딩을 열지 않습니다. Windows 방화벽은 필요한 Tailscale 장치로 제한하고 SFTP 전용 계정은 관리자 권한을 갖지 않습니다.

FolderSync+SFTP는 전송 PoC로만 보고, 제품 앱은 명시적인 ACK/체크섬/재개/멱등성을 가진 프로토콜을 사용합니다.
