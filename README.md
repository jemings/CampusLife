# CampusLife

**대학 생활의 모든 정보를 모으고, 이해하고, 실행 가능한 지식으로 바꾸는 개인 캠퍼스 라이프 허브**

CampusLife는 강의 녹음·정리만을 위한 앱이 아닙니다. 수업, 과제, 시험, 시간표뿐 아니라 공지, 행사, 동아리, 장학금, 인턴십, 행정 문서, 개인 일정, 메모, 사진, PDF, 웹 링크 등 대학 생활에서 접하는 정보를 한곳에 모아 관리하는 **local-first 개인 캠퍼스 라이프 허브**입니다.

> 휴대폰으로 기록하거나 자료를 공유하면, 노트북이 온라인일 때 자동으로 수집·분류·분석되고, 과목·날짜·주제·출처를 기준으로 다시 찾고 활용할 수 있습니다.

## 제품 원칙

1. **대학 생활 전체가 대상:** 강의는 첫 번째 대표 사용 사례이며 데이터 모델은 수업에 종속되지 않습니다.
2. **Local-first, store-and-forward:** 원본과 사용자 데이터의 기본 저장 위치는 Windows 노트북입니다. 노트북이 꺼져 있으면 Android가 업로드 대기 자료를 보존하고 연결 복구 후 전송합니다.
3. **사용자 통제:** 녹음·촬영은 사용자가 시작합니다. AI 전송, 일정 등록, 공유는 명시적 설정 또는 확인을 거칩니다.
4. **원본과 파생 결과 분리:** 원본은 보존하고 transcript, 요약, 분류, 퀴즈는 재생성 가능한 파생 데이터로 취급합니다.
5. **AI provider 교체 가능:** STT, Vision, LLM, embedding은 provider interface 뒤에 둡니다.
6. **출처 보존:** AI가 만든 노트와 일정에는 원본 파일, 페이지, 이미지, 녹음 구간 등 근거를 연결합니다.
7. **실패해도 데이터 보호:** ACK, 체크섬, 재시도, 멱등 처리, 백업을 기본 설계에 포함합니다.

## 초기 사용자 환경

- Android: Samsung Galaxy Z Fold8
- PC: Windows 11 노트북 (Intel Core Ultra 또는 AMD Ryzen AI 계열)
- 네트워크: Tailscale 사설 연결
- 초기 전송 PoC: FolderSync + Windows OpenSSH SFTP
- 제품용 동기화: CampusLife Android uploader + Windows local agent
- 저장: Windows 로컬 파일시스템 + SQLite
- AI: OpenAI API를 첫 provider로 지원하되 교체 가능하게 설계
- NAS 또는 상시 서버: 필수 아님

**운영 한계:** 노트북이 꺼져 있거나 절전 상태이면 파일 전송과 노트북 AI 처리는 불가능합니다. 이때 Android는 원본과 manifest를 로컬 Outbox에 보존하고 노트북이 다시 연결 가능해지면 업로드를 재시도합니다. Tailscale은 네트워크 연결이지 파일 저장소나 오프라인 큐가 아닙니다.

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
    │   ├── android/          # Kotlin + Jetpack Compose
    │   └── windows/          # Windows companion UI / local API
    ├── packages/
    │   ├── domain/           # 공통 도메인 계약
    │   └── api-contracts/    # 동기화 및 local-agent 계약
    ├── services/
    │   └── agent/            # Python 수집·작업·AI orchestration
    ├── docs/
    ├── examples/              # synthetic data only
    └── scripts/

이 구조는 목표 구조이며, 실제 디렉터리와 기술 스택은 단계별 이슈에서 검증한 뒤 추가합니다.

## 문서

- [제품 비전 및 범위](docs/PRODUCT-SCOPE.md)
- [시스템 아키텍처](docs/ARCHITECTURE.md)
- [도메인 모델](docs/DOMAIN-MODEL.md)
- [구현 로드맵](docs/ROADMAP.md)
- [보안·개인정보·데이터 수명주기](docs/PRIVACY-SECURITY.md)
- [AI 코딩 에이전트 지침](AGENTS.md)

## 첫 MVP 완료 기준

1. Fold8에서 강의 녹음과 사진을 수집하고 원본·manifest를 Outbox에 안전하게 저장한다.
2. 노트북이 꺼져 있거나 다른 네트워크에 있어도 자료가 손실되지 않는다.
3. 노트북이 온라인이 되면 업로드가 재개되고 체크섬 검증 및 ACK 후 AI 처리를 시작한다.
4. transcript와 사진을 시간 정보로 연결해 출처가 포함된 강의 노트를 만든다.
5. PDF, 이미지, 웹 링크, 텍스트 메모 등 비강의 정보도 같은 수집 모델로 저장·검색한다.
6. AI 또는 네트워크 실패와 앱 재시작 후에도 안전하게 재시도한다.

## 개인정보 주의

강의 녹음·촬영은 학교 및 교수자의 정책과 참여자 동의를 먼저 확인해야 합니다. 강의 자료, 학생 정보, 성적, 행정 문서는 민감할 수 있으므로 기본값은 로컬 보관이며 외부 AI API 전송은 데이터 유형별로 허용 여부를 제어합니다. 실제 사용자 데이터나 API 키를 Git에 커밋하지 않습니다.

- [강의·문서 통합 수집 및 파일 분석 설계](docs/FILE-INGESTION.md)

### 강의 제공 자료 지원

강의 Capture는 녹음·사진만이 아니라 교수자/조교가 제공한 PDF, PPT/PPTX, DOCX, XLSX/CSV, HWP/HWPX, 이미지, 오디오·동영상, 자막, 코드·압축 파일, 웹 링크를 여러 개의 첨부 자료로 함께 수집합니다. 원본은 변경 없이 보존하며 파일별 추출기와 출처/페이지/슬라이드/시트/시간 정보가 있는 파생 결과를 생성합니다.
