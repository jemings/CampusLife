# CampusLife

**대학 생활의 모든 정보를 모으고, 이해하고, 실행 가능한 지식으로 바꾸는 개인 캠퍼스 라이프 허브**

CampusLife는 강의 녹음·정리만을 위한 앱이 아닙니다. 수업, 과제, 시험, 시간표뿐 아니라 공지, 행사, 동아리, 장학금, 인턴십, 행정 문서, 개인 일정, 메모, 사진, PDF, 웹 링크 등 대학 생활에서 접하는 정보를 한곳에 모아 관리하는 **local-first 개인 캠퍼스 라이프 허브**입니다.

> 휴대폰으로 기록하거나 자료를 공유하면, 노트북이 온라인일 때 자동으로 수집·분류·분석되고, 과목·날짜·주제·출처를 기준으로 다시 찾고 활용할 수 있습니다.

## 제품 원칙

1. **대학 생활 전체가 대상:** 강의는 첫 번째 대표 사용 사례이며 데이터 모델은 수업에 종속되지 않습니다.
2. **Local-first, store-and-forward:** 원본과 사용자 데이터의 기본 저장 위치는 Windows 노트북입니다. 노트북이 꺼져 있으면 Fold8·iPad가 각자 업로드 대기 자료를 보존하고 연결 복구 후 전송합니다.
3. **사용자 통제:** 녹음·촬영은 사용자가 시작합니다. AI 전송, 일정 등록, 공유는 명시적 설정 또는 확인을 거칩니다.
4. **원본과 파생 결과 분리:** 원본은 보존하고 transcript, 요약, 분류, 퀴즈는 재생성 가능한 파생 데이터로 취급합니다.
5. **AI provider 교체 가능:** STT, Vision, LLM, embedding은 provider interface 뒤에 둡니다.
6. **출처 보존:** AI가 만든 노트와 일정에는 원본 파일, 페이지, 이미지, 녹음 구간 등 근거를 연결합니다.
7. **실패해도 데이터 보호:** ACK, 체크섬, 재시도, 멱등 처리, 백업을 기본 설계에 포함합니다.

## 초기 사용자 환경

| 장치 | 역할 | 앱 |
|---|---|---|
| Galaxy Z Fold8 | 강의 녹음·사진·자료 수집, 빠른 캡처 | Android / Kotlin / Compose |
| iPad Pro 13 | PDF 학습, Apple Pencil 필기, 자료 비교, AI 노트 검토 | iPadOS / SwiftUI / PencilKit / PDFKit |
| Windows 11 노트북 | 기본 데이터 허브(Hub), 파일 저장, AI 처리, 검색 API | Python Agent / FastAPI / SQLite |

- 네트워크: Tailscale 사설 연결, `tailscale serve`로 Hub API HTTPS 노출
- 동기화: 세 클라이언트 공통 HTTPS Sync API (Blob 업로드 + Mutation + 변경 피드, SFTP·FolderSync는 쓰지 않음)
- 저장: Windows 로컬 파일시스템(내용 주소 Blob) + SQLite
- AI: OpenAI API를 첫 provider로 지원하되 교체 가능하게 설계
- NAS 또는 상시 서버: 필수 아님

**운영 한계:** 노트북이 꺼져 있거나 절전 상태이면 파일 전송과 노트북 AI 처리는 불가능합니다. 이때 각 모바일 장치는 원본과 미전송 변경을 로컬 Outbox에 보존하고 노트북이 다시 연결 가능해지면 업로드를 재시도합니다. Tailscale은 네트워크 연결이지 파일 저장소나 오프라인 큐가 아닙니다. 알림은 각 기기가 로컬로 예약하므로 노트북이 꺼져 있어도 울립니다.

## 기능 영역

| 영역 | 관리 대상 |
|---|---|
| Academic | 학기, 과목, 강의, 시간표, 녹음, 슬라이드, 강의 노트 |
| Planner | 과제, 시험, 마감일, 일정, 할 일, 알림 |
| Campus | 학교 공지, 행사, 동아리, 스터디, 시설, 학사 일정 |
| Opportunities | 장학금, 공모전, 인턴십, 채용, 대외활동 |
| Documents | 강의자료, PDF, 양식, 행정 문서, 영수증 |
| Personal | 메모, 사진, 음성 메모, 북마크, 아이디어 |
| Knowledge | 전문 검색, AI 질의, 요약, 플래시카드, 퀴즈 |

## 목표 저장소 구조

    CampusLife/
    ├── apps/
    │   ├── android/          # Kotlin + Jetpack Compose (Fold8)
    │   ├── ipados/           # SwiftUI + PencilKit + PDFKit (iPad Pro)
    │   ├── windows/          # (선택) Windows 로컬 UI — Agent의 localhost admin API만 사용
    │   └── prototype/        # Responsive web design prototype
    ├── packages/
    │   └── api-contracts/    # 도메인 엔터티·Locator·Sync API의 JSON Schema/OpenAPI + golden fixture (단일 계약)
    ├── services/
    │   └── agent/            # Windows Hub: Sync/admin API, 저장, 작업 큐, AI orchestration
    ├── docs/
    ├── examples/              # synthetic data only
    └── scripts/

이 구조는 목표 구조이며, 실제 디렉터리와 기술 스택은 단계별 이슈에서 검증한 뒤 추가합니다.

## 문서

| 문서 | 내용 |
|---|---|
| [설계 결정 기록](docs/DECISIONS.md) | 상태 소유권, Blob 저장, 동기화 모델, ID·멱등, 네트워크 노출, 토큰, cloud policy 등 확정된 구조 결정 — 다른 문서와 다르면 이 문서가 우선 |
| [제품 비전 및 범위](docs/PRODUCT-SCOPE.md) | 관리 대상, 사용자 흐름, 비목표 |
| [시스템 아키텍처](docs/ARCHITECTURE.md) | 실행 단위와 책임, Hub 내부 모듈, 저장 레이아웃 |
| [멀티디바이스 아키텍처](docs/MULTI-DEVICE-ARCHITECTURE.md) | Sync API, 업로드 프로토콜, pairing, 충돌 정책 |
| [도메인 모델](docs/DOMAIN-MODEL.md) | 엔터티, 상태, 시간·출처 규칙 |
| [강의·문서 파일 수집](docs/FILE-INGESTION.md) | 형식별 extractor, Locator, 파일 안전성 |
| [보안·개인정보·데이터 수명주기](docs/PRIVACY-SECURITY.md) | cloud policy, 인증, 백업, 위협 모델 |
| [구현 로드맵](docs/ROADMAP.md) | 단일 MVP 정의, Phase, 이슈 의존성 |
| [UX/UI 디자인 기준](docs/UX-DESIGN.md) | Fold8 Cover/Inner, iPad Study Workspace |
| [iPadOS Companion](apps/ipados/README.md) | iPad 앱 시작점 |
| [AI 코딩 에이전트 지침](AGENTS.md) | 구현 시 지켜야 할 경계 |

## 첫 MVP 완료 기준

[ROADMAP.md의 MVP 정의](docs/ROADMAP.md#mvp-정의-단일-기준)를 따릅니다. 요약하면 Fold8에서 강의 녹음·사진을 손실 없이 수집해 노트북이 온라인일 때 HTTPS로 업로드하고, 출처가 연결된 강의 노트를 만들며, 비강의 자료도 같은 Capture 경로로 저장하는 것까지입니다. 다중 형식 추출, Inbox 분류, Planner, iPad는 MVP 이후입니다.

## 개인정보 주의

강의 녹음·촬영은 학교 및 교수자의 정책과 참여자 동의를 먼저 확인해야 합니다. 강의 자료, 학생 정보, 성적, 행정 문서는 민감할 수 있으므로 기본값은 로컬 보관이며 외부 AI API 전송은 자료별 `cloud_policy`로 제어합니다. 실제 사용자 데이터나 API 키를 Git에 커밋하지 않습니다.

### 강의 제공 자료 지원

강의 Capture는 녹음·사진만이 아니라 교수자/조교가 제공한 PDF, PPT/PPTX, DOCX, XLSX/CSV, HWP/HWPX, 이미지, 오디오·동영상, 자막, 코드·압축 파일, 웹 링크를 여러 개의 첨부 자료로 함께 수집합니다. 원본은 변경 없이 보존하며 파일별 추출기와 출처/페이지/슬라이드/시트/시간 정보가 있는 파생 결과를 생성합니다.

## GitHub Issues

제품 전체 Epic: [#1 CampusLife 통합 대학생활 허브](https://github.com/jemings/CampusLife/issues/1). 의존 관계는 [ROADMAP.md](docs/ROADMAP.md#의존성)에 있습니다.

| 영역 | 이슈 | 선행 |
|---|---|---|
| Contracts | [#20 공통 도메인·Sync API 계약](https://github.com/jemings/CampusLife/issues/20) | — |
| MVP | [#2 Windows Hub 코어](https://github.com/jemings/CampusLife/issues/2) | #20 |
| MVP | [#4 Sync 전송: Tailscale serve + HTTPS 업로드/다운로드](https://github.com/jemings/CampusLife/issues/4) | #2, #20 |
| MVP | [#19 Device pairing·장치 토큰·revoke](https://github.com/jemings/CampusLife/issues/19) | #2 |
| MVP | [#3 Android Capture](https://github.com/jemings/CampusLife/issues/3) | #4, #19 |
| MVP | [#8 강의 녹음·사진·배포 문서 AI 노트](https://github.com/jemings/CampusLife/issues/8) | #2, #3 (다중 문서 부분은 #5) |
| Ingestion | [#5 다중 파일 강의 자료 연결 및 Extractor Framework](https://github.com/jemings/CampusLife/issues/5) | #2 |
| Ingestion | [#6 PDF·PPTX·DOCX·XLSX/CSV](https://github.com/jemings/CampusLife/issues/6) | #5 |
| Ingestion | [#7 HWP·HWPX](https://github.com/jemings/CampusLife/issues/7) | #5 |
| Ingestion | [#13 ZIP·코드·Jupyter·동영상·자막](https://github.com/jemings/CampusLife/issues/13) | #5 |
| Campus | [#9 통합 Inbox·Suggestion 검토](https://github.com/jemings/CampusLife/issues/9) | #2, #5 |
| Planner | [#10 과제·시험·행사·기회 관리](https://github.com/jemings/CampusLife/issues/10) | #9 |
| Search | [#11 검색·AI 질의·복습](https://github.com/jemings/CampusLife/issues/11) | #2, #5 |
| Sync | [#18 충돌 보존·해소와 Hub 복원 처리](https://github.com/jemings/CampusLife/issues/18) | #4, #19 |
| iPadOS | [#16 Native Study Companion](https://github.com/jemings/CampusLife/issues/16) | #18 |
| iPadOS | [#17 Apple Pencil·PDF Annotation](https://github.com/jemings/CampusLife/issues/17) | #16 |
| UI | [#15 Fold8/iPad 적응형 UX](https://github.com/jemings/CampusLife/issues/15) | #3, #16과 병행 |
| Security | [#12 파일 안전·cloud policy·백업](https://github.com/jemings/CampusLife/issues/12) | 각 Phase와 병행 |
| Quality | [#14 E2E 테스트·운영 가이드](https://github.com/jemings/CampusLife/issues/14) | 각 Phase와 병행 |

## 디자인 프로토타입

[인터랙티브 웹 프로토타입](apps/prototype/index.html) — 브라우저에서 열어 PC / Fold8 접은 화면 / Fold8 펼친 4:3 화면 / iPad Pro 13을 전환해 볼 수 있습니다. 디자인 검토용이며 실제 녹음·동기화·AI와 연결되어 있지 않습니다.
