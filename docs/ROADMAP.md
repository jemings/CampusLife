# CampusLife 구현 로드맵

## 진행 전략

하나의 저장소에서 Android·iPadOS 앱, Windows Hub, 공통 계약, 문서와 테스트를 함께 관리합니다. 각 실행 단위는 독립적으로 빌드·테스트할 수 있게 분리합니다.

**먼저 파일 손실 없는 end-to-end 강의 MVP를 완성하고, 그 위에 다중 형식 자료, 일반 대학 생활 Capture, Planner, 멀티디바이스 편집을 확장합니다. 데이터 모델과 API 계약은 처음부터 범용·멀티클라이언트로 만듭니다.**

제품 동기화 경로는 처음부터 공통 HTTPS Sync API 하나입니다. Android도 이 API로 업로드하고 과목·시간표를 Mutation으로 보내므로, Blob 업로드·Mutation 적용기·변경 피드·pairing은 Android보다 먼저 준비합니다. SFTP 수신 경로는 만들지 않습니다([DECISIONS.md D5](DECISIONS.md)).

## MVP 정의 (단일 기준)

1. Fold8에서 60분 강의 녹음(세그먼트 + 단조 시계 타임라인)과 사진 3~5장을 수집하고 원본을 Outbox에 보존한다.
2. 노트북이 꺼져 있거나 다른 네트워크에 있어도 자료가 손실되지 않는다. 녹음 중 앱이 강제 종료되어도 마지막 세그먼트 이전은 재생 가능하다.
3. 노트북이 온라인이 되면 Tailscale 경유 HTTPS로 Blob 업로드가 재개되고, 해시 검증과 `capture.commit` ACK 후 AI 처리가 시작된다.
4. transcript와 사진을 녹음 타임라인으로 연결해 출처(Locator)가 포함된 강의 노트를 만들고, Windows 로컬 웹 UI에서 열람한다. 과제·마감 후보는 Suggestion으로만 나온다.
5. PDF, 이미지, 웹 링크, 텍스트 메모 등 비강의 자료도 같은 Capture 경로로 Hub에 저장되고 목록·원본 열람이 된다(형식별 추출·분류는 MVP 이후).
6. 네트워크·앱·AI 실패 후 재시도해도 중복 원본·중복 산출물·중복 비용이 생기지 않는다.

README와 Epic #1의 MVP 기준은 이 목록을 따릅니다.

## Phase 0 — 연결 PoC

목표: Fold8·iPad와 Windows 11 노트북이 다른 네트워크에서도 연결되는지 검증합니다.

- Tailscale을 Android, iPadOS, Windows에 설치하고 MagicDNS 확인
- Windows에서 `tailscale serve`로 localhost의 간단한 health endpoint를 HTTPS로 노출하고 Fold8·iPad 브라우저에서 접근
- 같은 경로로 LTE·DERP relay를 거쳐 4/8/16 MiB chunk를 올려 보고 실패율·재개를 측정 (DECISIONS O8)
- Tailscale 장치 이름을 실명·학교명이 드러나지 않게 설정(인증서 CT 로그 공개 대비)
- Tailscale ACL/grants로 노트북 serve 포트 접근을 Fold8·iPad로 제한
- 노트북 전원 OFF/절전/복귀, 휴대폰 Wi-Fi↔모바일 데이터 전환 시 재접속과 인증서 유지 확인
- 학교 VPN을 켠 상태에서 Hub 접근이 막히는지 확인

## Phase 1 — 공통 계약과 Windows Hub 코어

- `packages/api-contracts`: Blob 업로드·`capture.commit`, Item kind, Locator, Mutation, changes, 오류 응답의 JSON Schema/OpenAPI, 클라이언트 sync 테이블 DDL, golden fixture (#20, D17)
- 로컬 data root(`%LOCALAPPDATA%\CampusLife`, 동기화 폴더 거부), SQLite schema·migration, 기동 시 SQLite 버전·FTS5 검사
- `blobs`: 내용 주소 저장, 원자 이동, 정리(GC)·purge (D2)
- `mutations`: `capture.commit`, Item create/patch(필드 CAS), Relation·Tag, `change_seq` (D1, D3, D18)
- `jobs`: job_key 멱등 작업 큐, 재시도, 취소, subprocess 격리 (D4, D16)
- Windows 로컬 import(드래그앤드롭)를 같은 Blob 저장 + `capture.commit` 경로로 연결
- Windows 로그온 시 Hub 자동 실행(Task Scheduler)
- 처리 상태와 로그 확인 CLI/최소 로컬 UI
- 백업 세대(DB 스냅샷 + 참조 Blob + Artifact)와 복원 시 새 `epoch` 발급, `backed_up_at` 기록, `hub fsck`

완료 기준: 같은 파일을 두 번 import해도 Blob은 하나이고, 같은 Capture·Mutation 재전송은 중복을 만들지 않으며, Hub 강제 종료·Windows 재부팅 후 pending 작업이 복구됩니다.

## Phase 2 — Sync 전송과 장치 등록

- `tailscale serve` + localhost Agent 구성 (D5)
- QR pairing, 장치 토큰 발급·해시 저장·revoke·dormant (D6, #19)
- sha256 키의 순서 무관 chunk 업로드 / finalize / `capture.commit` ACK (#4, D7)
- `POST /v1/mutations`, `GET /v1/changes`(`hub_id`·`epoch` 포함) (D3)
- Blob·Artifact 다운로드(Range 재개)
- admin 포트 분리

완료 기준: 테스트 클라이언트로 업로드를 중간에 끊고 이어서 완료할 수 있고, 같은 capture_id 재전송은 이전 결과를 돌려주며, revoke된 토큰은 거부됩니다.

## Phase 3 — Android Capture MVP

- 과목/학기/주간 시간표 등록
- 수업 시작 전 알림
- 사용자가 포그라운드에서 누르는 녹음 시작/정지, 세그먼트 + 단조 시계 타임라인 기록, 재시작 시 복구·봉인 (D15)
- 녹음 중 Foreground Service와 명확한 상태 표시
- 수업 중 사진 촬영, 셔터 시점 `elapsedRealtimeNanos` 기록
- 공유 메뉴로 URL/텍스트/다중 파일 수집 (즉시 Outbox 복사)
- Room 기반 Outbox·entity cache, pairing
- WorkManager 기반 Blob 업로드·`capture.commit`·Mutation 재시도, ACK 표시
- 과목 Item의 `fields.reminders[]`로 수업 전 로컬 알림 예약 (D19)
- Cover/Inner 적응형 화면의 캡처 동선 (#15 Android 부분)

완료 기준: 앱 재시작, 네트워크 변경, 노트북 오프라인 상황에서도 로컬 원본이 보존되고, 한 Capture의 녹음 세그먼트·사진·타임라인이 같은 Capture ID로 commit됩니다.

## Phase 4 — Lecture AI Pipeline (MVP 완료)

- 녹음 세그먼트 사전 검사 및 API 크기 제한에 따른 분할
- STT provider interface 및 OpenAI adapter
- 과목별 용어/키워드 힌트
- transcript 구간을 Capture 타임라인 절대 시각(`media_time`)으로 변환
- 이미지 EXIF 및 분석 결과 저장
- 사진 단조 시계 값과 transcript 구간 상관관계
- Vision provider interface
- LLM note generator: 핵심 요약, 상세 설명, 용어, 수식/코드, 질문, 복습 퀴즈. 입력 블록 id로만 인용하고 Hub가 검증 (D13)
- 과제·마감 후보는 Suggestion으로 생성 (D10)
- 불확실성과 근거 Locator 표시, Windows 로컬 웹 UI의 노트 열람·Suggestion 검토 화면
- `ProviderGateway` 단일 검사 지점, budget guard, audit_log (D8)
- 실패 재시도 및 API 비용/사용량 기록

완료 기준: 원본이 수정되지 않고 재처리 가능하며, 중요 주장에 강의 구간/사진 근거가 연결되고 불확실한 내용은 검토 대상으로 표시됩니다. 위 MVP 정의 6개 항목을 모두 만족합니다.

## Phase 5 — 교수자/조교 배포 파일 수집 및 분석

- 다수 파일을 Relation으로 같은 LectureSession·Course에 연결, 개정판은 `supersedes` Suggestion (#5)
- 확장자 + MIME + magic bytes 기반 형식 탐지, extractor subprocess 격리
- PDF 텍스트·스캔 OCR, PPTX, DOCX, XLSX/CSV (#6)
- PDF 렌디션 Blob(`rendition_of`)
- HWP/HWPX adapter와 미지원 fallback (#7)
- ZIP 안전 검사, 코드/Jupyter, 오디오/비디오/자막 (#13)
- 결정적 ContentBlock id와 공통 Locator (D13)
- 여러 파일 간 통합 요약, 상충 지시는 `conflicts_with` Suggestion 쌍 (#8 확장)

완료 기준:
- 하나의 강의에 녹음, 사진, PDF, PPTX, DOCX, XLSX, HWPX 등 복수 파일을 연결할 수 있습니다.
- 가능한 경우 페이지/슬라이드/시트/셀/시간 구간으로 AI 요약의 근거를 추적할 수 있습니다.
- 지원하지 않는 파일도 원본을 보존하고 실패 사유를 사용자에게 설명합니다.
- 매크로나 스크립트가 실행되지 않으며 ZIP 추출 제한이 테스트됩니다.

## Phase 6 — Unified Inbox

- PDF/이미지/문서/URL/텍스트/음성 메모 분류
- event / announcement / opportunity / organization / note `create_item` Suggestion 생성, 기각 fingerprint 기억
- 태그와 관계 편집, Suggestion 검토 화면(수락 시 수정 가능)
- 공지에서 마감일/장소/대상 조건 추출

완료 기준: 강의와 비강의 데이터가 같은 Capture/Attachment/Artifact pipeline을 공유하고, AI 분류는 사용자가 수락하기 전에는 Item이 되지 않습니다.

## Phase 7 — Planner & Campus

- 과제/시험/프로젝트, 행사/동아리/스터디, 장학금/공모전/인턴십 관리
- 주간/월간 캘린더, 마감일, 반복 할 일(`local_time_rule`)
- 각 클라이언트의 OS 로컬 알림 예약 (D19)
- AI 추출 일정 Suggestion의 수락/수정 흐름
- 학교 공식 시스템은 허용된 API/export/import만 연동

완료 기준: AI에서 온 일정/할 일은 근거 Relation으로 원본과 연결되며, `instant`·`local_date`·`local_time_rule`을 구분해 정확하게 처리합니다(D11).

## Phase 8 — Search & Study Assistant

- SQLite FTS5 전문 검색, NFC 정규화 (한국어 분석기는 한글·CJK 겹침 2-gram 선토큰화 `hangul-bigram@1`, DECISIONS D27)
- 과목/학기/도메인/날짜/태그 필터
- 근거를 포함하는 AI 질문
- 플래시카드, 간격 반복 복습, 퀴즈·오답·취약 개념
- 학기별/주간 요약
- 필요성 검증 후 embeddings/vector search

완료 기준: AI 답변은 열어볼 수 있는 원본 근거를 제시하고, 검색 결과가 없으면 내용을 꾸며내지 않습니다.

## Phase 9 — 메타데이터 동기화와 충돌 처리

두 번째 편집 클라이언트(iPad)가 생기기 전에 완성합니다. Mutation 적용기(필드 CAS)와 변경 피드는 Phase 1·2에서 이미 동작하므로, 여기서는 충돌 보존·해소와 복원 처리를 완성합니다.

- 텍스트 본문 충돌의 ConflictRecord 보존, 삭제된 엔터티 patch 처리 (D18, #18)
- 충돌 비교/선택 UI (Windows 로컬 UI 우선)
- tombstone 삭제 전파와 휴지통, `applied_mutations` 보관 정책
- `epoch` 변경 감지 후 `since=0` 재동기화와 누락 Capture 재업로드 (D3)

완료 기준:
- 노트북 오프라인 동안 두 클라이언트의 변경이 재접속 후 수렴
- 동시에 수정한 같은 Note 본문은 덮어쓰지 않고 양쪽 보존
- 같은 Task의 서로 다른 필드를 동시에 수정하면 둘 다 적용
- cursor=0 전체 pull이 증분 pull과 같은 결과
- 백업 복원 후 클라이언트가 Hub에 없는 Capture를 자동으로 다시 올림

## Phase 10 — iPadOS Native Companion

- 배포·서명 방식 확정 (DECISIONS O7), SwiftUI 앱 골격, 계약 fixture로 검증한 API client, pairing (#16)
- SQLite offline metadata cache 및 file download cache(LRU, 오프라인 보관 고정)
- Files/Share Extension(App Group outbox) 기반 자료 수집
- 강의 세션별 자료, PDF(렌디션 포함), 강의 노트 브라우징
- PencilKit 필기, (Blob, page, device) AnnotationLayer, `strokes.v1.json` 표준 payload, Hub 렌더링 overlay (#17)
- AI 노트와 원본 자료의 split-view 학습 workspace (#15 iPad 부분)
- Stage Manager, Split View, 외부 키보드, orientation 대응
- background URLSession(파일 기반 chunk 업로드) 및 relaunch reconciliation

완료 기준:
- iPad에서 PDF 강의자료에 Pencil로 필기하고 앱 종료/재실행 후에도 유지
- 노트북 온라인 시 annotation이 동기화되고 Windows/Android에서 Hub가 렌더링한 overlay 확인
- iPad offline 상태에서도 다운로드한 자료의 annotation 작성 가능
- 백그라운드 전송 중단/강제 종료 이후 재시도
- 창 크기 변경과 멀티태스킹 중 선택 과목/문서 페이지/필기 상태 유지

## 상시 — 안정화와 운영

마지막에 몰아서 하지 않고 각 Phase에 포함합니다.

- 파일 안전성, cloud policy, 백업/복원 (#12)
- E2E 테스트와 설치·운영 가이드 (#14)
- 배터리/저장공간/모바일 데이터 사용량 제어
- AI provider별 비용 추적 및 budget guard
- 학기별 archive와 보존 정책, 새 노트북으로 데이터 이전

## 의존성

    #20 Contracts ──┬── #2 Hub core ──┬── #4 Sync transport ──┬── #3 Android ── #8 Lecture AI ══ MVP
                    │                 │        │              │
                    │                 │        └── #19 Pairing┘
                    │                 │
                    │                 ├── #5 Extractor framework ── #6 / #7 / #13
                    │                 │                               └── #8 다중 자료 확장
                    │                 ├── #9 Inbox(Suggestion) ── #10 Planner
                    │                 └── #11 Search
                    │
                    └── #18 충돌 보존·복원 처리 ── #16 iPad ── #17 Annotation
                         (#4, #19 필요)

    #12 Security, #14 Quality, #15 Adaptive UI: 각 클라이언트·Phase와 병행

Phase 6 이후(Inbox, Planner, Search, iPad)의 순서는 우선순위에 따라 바꿀 수 있습니다. 위 의존 화살표만 지키면 됩니다.

## GitHub Issues

제품 전체 Epic: [#1 CampusLife 통합 대학생활 허브](https://github.com/jemings/CampusLife/issues/1). 전체 이슈 표는 [README.md](../README.md#github-issues)에 있습니다.
