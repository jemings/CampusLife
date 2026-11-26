# CampusLife 구현 로드맵

## 진행 전략

하나의 저장소에서 Android 앱, Windows companion, 공통 도메인 계약, 문서와 테스트를 함께 관리합니다. 각 실행 단위는 독립적으로 빌드·테스트할 수 있게 분리합니다.

**먼저 파일 손실 없는 end-to-end 강의 MVP를 완성하고, 그 위에 일반 대학 생활 Capture와 Planner 기능을 확장합니다. 데이터 모델은 처음부터 범용으로 만듭니다.**

## Phase 0 — 연결 PoC

목표: Fold8과 Windows 11 노트북이 다른 네트워크에서도 연결되고 파일을 안전하게 보낼 수 있음을 검증합니다.

- Tailscale을 Android와 Windows에 설치
- Tailscale IP/MagicDNS 확인
- Windows OpenSSH Server 및 SFTP 전용 계정 구성
- Windows 방화벽 inbound 범위를 Fold8 Tailscale 주소로 제한
- FolderSync SFTP 업로드 pair 구성
- 노트북 전원 OFF/절전/복귀와 휴대폰 모바일 데이터 전환 테스트
- 중복 업로드, 부분 파일, 파일명 충돌, 큰 파일 테스트

FolderSync+SFTP는 연결 PoC입니다. 장기적으로 CampusLife 자체 uploader와 Windows agent가 ACK, 체크섬, 재개, 멱등성을 처리합니다.

## Phase 1 — 공통 저장소와 Windows Agent

- 로컬 데이터 디렉터리와 SQLite schema
- Capture / Attachment / Item / Source / Artifact / ProcessingJob
- manifest 검증, SHA-256, atomic finalize
- SFTP PoC 수신 또는 로컬 import
- 처리 큐 및 재시도
- Windows 로그인 시 agent 자동 실행
- 처리 상태와 로그 확인 UI/CLI
- 로컬 백업 및 복원 스모크 테스트

완료 기준: 동일 manifest 재수신 시 중복 원본/작업이 생기지 않고, 업로드 중단/재시작 및 Windows 재부팅 후에도 상태를 복구합니다.

## Phase 2 — Android Capture MVP

- 과목/학기/주간 시간표 등록
- 수업 시작 전 알림
- 사용자가 누르는 명시적 녹음 시작/정지
- 녹음 중 Foreground Service와 명확한 상태 표시
- 수업 중 사진 촬영 및 촬영 시각 기록
- 공유 메뉴로 URL/텍스트/PDF/이미지 수집
- Capture manifest 및 Outbox
- WorkManager 기반 업로드 재시도
- 서버 ACK 후 동기화 상태 표시
- 강의 외 자료를 통합 Inbox로 보관

완료 기준: 앱 재시작, 네트워크 변경, 노트북 오프라인 상황에서도 로컬 원본이 보존되고, 한 Capture의 원본/사진/metadata가 같은 ID로 연결됩니다.

## Phase 3 — Lecture AI Pipeline

- 오디오 사전 검사 및 API 크기 제한에 따른 분할
- STT provider interface 및 OpenAI adapter
- 과목별 용어/키워드 힌트
- transcript chunk 시간 오프셋 보존
- 이미지 EXIF 및 분석 결과 저장
- 사진 시각과 transcript 구간 상관관계
- Vision provider interface
- LLM note generator
- 핵심 요약, 상세 설명, 용어, 수식/코드, 질문, 복습 퀴즈
- 불확실성 및 source reference 표시
- 실패 재시도 및 API 비용/사용량 기록

완료 기준: 원본이 수정되지 않고 재처리 가능하며, 중요 주장에 강의 구간/사진 근거가 연결되고 불확실한 내용은 검토 대상으로 표시됩니다.

## Phase 4 — Unified Inbox

- PDF/이미지/문서/URL/텍스트/음성 메모 수집
- 파일 및 문서 metadata 추출
- OCR 및 문서 요약
- Announcement / Event / Opportunity / Organization / Note 후보 분류
- 태그와 관계 편집
- AI 분류 후보 검토 화면
- 공지에서 마감일/장소/대상 조건 추출
- 원본과 출처 링크 보존

완료 기준: 강의와 비강의 데이터가 같은 Capture/Attachment/Artifact pipeline을 공유하고 AI 분류는 사용자 검토 전 확정되지 않습니다.

## Phase 5 — Planner & Campus

- 과제/시험/프로젝트 관리
- 주간/월간 캘린더 및 마감일
- 행사/동아리/스터디 일정
- 장학금/공모전/인턴십 관리
- 지원 요건/마감/제출 서류/진행 상태
- 알림과 반복 할 일
- AI 추출 이벤트의 승인/수정 흐름
- 학교 공식 시스템은 허용된 API/export/import만 연동

완료 기준: 모든 일정/할 일은 원본 Capture와 연결되며 날짜와 timezone을 정확하게 처리합니다.

## Phase 6 — Search & Study Assistant

- SQLite FTS5 전문 검색
- 과목/학기/도메인/날짜/태그 필터
- 자료 원문/요약/전사 검색
- 근거를 포함하는 AI 질문
- 플래시카드 및 간격 반복 복습
- 퀴즈, 오답, 취약 개념 관리
- 학기별/주간 요약
- 필요성 검증 후 embeddings/vector search

완료 기준: AI 답변은 열어볼 수 있는 원본 근거를 제시하고, 검색 결과가 없으면 내용을 꾸며내지 않습니다.

## Phase 7 — 안정화와 확장

- 백업 자동화, 복원 훈련, 데이터 export
- 배터리/저장공간/모바일 데이터 사용량 제어
- AI provider별 비용 추적 및 budget guard
- 접근 통제와 로컬 암호화
- Windows와 Android 업데이트 호환성 테스트
- 필요 시 Calendar/Email/학교 포털 export importer
- 다른 기기/새 노트북으로 데이터 이전
- 학기별 archive와 보존 정책

## 의존성

    Architecture + Domain Contracts
              |
              +-- Windows Agent + Local Storage
              |              |
              +-- Android Outbox + Sync
                             |
                      Lecture AI Pipeline
                             |
                       Unified Inbox
                             |
                       Planner / Campus
                             |
                    Search / Study Assistant

Privacy, backup, observability, tests는 모든 단계에서 함께 구현하고 마지막으로 미루지 않습니다.

## 첫 end-to-end MVP 완료 기준

- 60분 강의 녹음과 사진 3~5장을 Fold8에서 수집
- 노트북이 꺼져 있어도 Fold8에 자료 보존
- 노트북이 온라인이 되면 전송 재개
- 해시 검증 및 ACK 후 AI 처리 시작
- transcript, 이미지 설명, 통합 Markdown 노트 생성
- 강의 시각과 사진을 구간 단위로 연결
- 원본·산출물·처리 상태를 로컬에서 조회
- 실패 후 재시도해도 중복 산출물이 만들어지지 않음
