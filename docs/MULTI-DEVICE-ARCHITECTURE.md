# CampusLife Multi-device Architecture

구조 결정의 근거는 [DECISIONS.md](DECISIONS.md)에 있습니다.

## 1. 목표

CampusLife는 Windows 단일 PC 앱이 아니라 다음 세 장치를 하나의 개인 학습 데이터 공간으로 연결합니다.

| Device | Primary role | Native client |
|---|---|---|
| Galaxy Z Fold8 | 강의 녹음/촬영, 이동 중 수집, quick capture | Android / Kotlin / Jetpack Compose |
| iPad Pro 13 | PDF 학습, Apple Pencil 필기, AI 노트 검토, 자료 비교 | iPadOS / SwiftUI / PencilKit / PDFKit |
| Windows 11 laptop | primary local hub, file storage, AI processing, search | Python agent / FastAPI / SQLite |

Windows는 유일한 원천 데이터 저장소(Hub)이며 필수 상시 클라우드는 두지 않습니다. Fold8과 iPad는 각자 durable local cache/Outbox를 유지합니다. 노트북이 꺼져 있거나 절전 상태일 때 두 모바일 장치는 서로 직접 동기화하지 않고 각자의 변경을 보존하며, 노트북이 다시 접속 가능해지면 Hub와 동기화합니다.

## 2. 네트워크와 서비스 노출 (D5)

    Fold8 / iPad
        │  HTTPS  https://<machine>.<tailnet>.ts.net
        ▼
    tailscale serve (Windows, 유효한 TLS 인증서 종단)
        │  HTTP  127.0.0.1:<sync-port>
        ▼
    CampusLife Agent ── sync API (장치 토큰 필수)
                    └─ admin API 127.0.0.1:<admin-port>  ← serve에 연결하지 않음, 로컬 UI 전용

- Tailscale은 Fold8, iPad, Windows 사이의 사설 네트워크와 TLS 인증서를 제공합니다.
- Agent는 `127.0.0.1`에만 바인딩합니다. Windows 방화벽에 CampusLife용 inbound 규칙을 열지 않으며, 공유기 포트 포워딩도 하지 않습니다.
- `tailscale serve`를 거친 요청은 Agent에서 localhost 출처로 보이므로 **출처 IP로 권한을 판단하지 않습니다.** sync API 포트는 항상 장치 토큰을 검증하고, pairing 승인·revoke 같은 관리 기능은 serve에 노출하지 않는 별도 admin 포트에만 둡니다.
- Tailscale HTTPS 인증서를 켜면 장치 이름과 tailnet 이름이 공개 Certificate Transparency 로그에 남습니다. 노트북의 Tailscale 장치 이름에 실명·학교명을 쓰지 않습니다.
- Tailscale ACL/grants로 tailnet 안에서도 Fold8·iPad만 노트북의 serve 포트에 접근하도록 제한합니다.
- 초기 SFTP/FolderSync는 Phase 0 연결 확인에만 쓰고 Agent에 SFTP 수신 경로를 만들지 않습니다.

Tailscale은 연결성을 제공하지만 서버 프로세스, 파일 큐, 보관소, 앱 인증을 대신하지 않습니다. Windows Agent가 실행 중이어야 API를 사용할 수 있습니다.

## 3. Common Sync API

### 3.1 핵심 API 계약

| API concept | 목적 |
|---|---|
| Pairing | Windows Hub가 만든 일회성 코드로 새 기기 승인 |
| Device Registry | 등록 장치 조회, 이름 변경, 마지막 접속, revoke (admin 포트) |
| Register Capture | Capture manifest 등록 (멱등: capture_id) |
| Upload Session / Chunk / Finalize | 첨부 단위 재개 가능 업로드, 검증, ACK (멱등: attachment_id) |
| Push Mutations | Item/Note/Annotation/Relation 등 사용자 편집 전달 (멱등: mutation_id) |
| Pull Changes | cursor 이후 바뀐 엔터티의 최신 스냅샷 |
| Download Blob / Artifact | 원본·파생 파일 다운로드 (Range 재개) |
| Resolve Conflict | 충돌 revision 비교, 사용자 선택 반영 |

실제 경로와 payload는 `packages/api-contracts`의 OpenAPI 및 JSON Schema로 버전 관리하고, 각 클라이언트는 이 계약에서 생성한 타입을 씁니다. API URL, 인증, payload schema를 앱 코드에 손으로 중복 정의하지 않습니다.

### 3.2 파일 업로드 프로토콜 (D4, D7)

1. Client가 Capture manifest를 등록합니다: capture_id, device_id, captured_at_utc, timezone, source_type, cloud_policy, 그리고 첨부별 attachment_id, size_bytes, sha256, MIME, 원래 파일명, (녹음 세그먼트면) 순서·세션 내 시작 오프셋.
2. Hub는 manifest를 저장하고 첨부별 upload session을 만듭니다. 이미 같은 sha256의 Blob이 있으면 그 첨부는 업로드 없이 바로 `verified` + ACK를 돌려줍니다.
3. Client는 첨부를 chunk 단위로 임시 위치에 전송합니다. 각 chunk에는 offset과 chunk hash가 붙습니다.
4. 연결이 끊기면 Client는 session 상태를 조회해 Hub가 받은 offset부터 이어서 보냅니다.
5. Finalize 시 Hub는 전체 길이와 SHA-256을 검증하고 `blobs/sha256/…`로 atomic rename한 뒤 읽기 전용으로 바꿉니다.
6. 같은 DB 트랜잭션에서 Attachment를 `verified`로 바꾸고, 변경 피드 레코드와 ingest ProcessingJob을 등록한 다음 ACK를 반환합니다.
7. Client는 ACK를 기록합니다. 로컬 원본은 [DECISIONS.md D14](DECISIONS.md)의 보존 정책을 만족할 때까지 지우지 않습니다.

멱등 규칙: 같은 attachment_id + 같은 sha256 재요청은 기존 ACK를 반환하고, 같은 attachment_id + 다른 sha256은 `409 conflict`로 거부하며 덮어쓰지 않습니다.

Capture 완결: 선언된 모든 첨부가 `verified`가 되면 완결입니다. 미완결 Capture도 받은 첨부는 열람할 수 있고 "n/m 수신"으로 보입니다. 첨부를 나중에 추가하려면 새 Capture를 만들고 Relation mutation으로 같은 MaterialSet·LectureSession에 연결합니다.

### 3.3 메타데이터와 Mutation 동기화 (D3)

- Hub는 사용자 편집 엔터티마다 `revision`을 증가시키고, 같은 트랜잭션에서 변경 피드에 `(sequence, entity_type, entity_id, revision, deleted)`를 기록합니다.
- 피드는 엔터티당 최신 레코드만 유지해도 됩니다. 따라서 cursor가 오래되어도 "cursor=0부터 pull"로 전체 재조정이 되며 별도 full-reconcile 프로토콜이 없습니다.
- Pull Changes는 cursor 이후 바뀐 엔터티의 **최신 스냅샷**과 새 cursor를 반환합니다. 클라이언트는 연산을 재생하지 않고 스냅샷으로 로컬 레코드를 교체합니다(로컬에 pending mutation이 있는 엔터티 제외 — §6).
- Mutation은 mutation_id, device_id, entity_type, entity_id, base_revision, payload를 포함합니다. Hub는 `accepted(new_revision)`, `rejected(reason)`, `conflict(conflict_id)` 중 하나를 반환하고 같은 mutation_id 재요청에는 첫 결과를 그대로 돌려줍니다.
- 새 엔터티 생성은 클라이언트가 만든 UUIDv7 ID와 `base_revision=0`으로 보냅니다.
- 내부 작업 상태(ProcessingJob 재시도 등)는 피드에 넣지 않습니다. 클라이언트가 보여줘야 하는 요약만 엔터티 필드로 노출합니다.

## 4. Device Enrollment and Lifecycle (D6)

### Pairing
1. 사용자가 Windows 로컬 UI(admin 포트)에서 pairing을 시작하면 Hub가 짧은 유효기간(기본 5분)의 일회성 코드/QR을 발급합니다. QR에는 Hub의 MagicDNS 주소와 코드가 들어갑니다.
2. Fold8 또는 iPad에서 QR을 스캔하거나 코드를 입력하면 장치는 `pending_approval`로 등록됩니다.
3. Windows UI에서 새 장치 이름과 플랫폼을 확인하고 승인합니다.
4. Hub는 256-bit 무작위 장치 토큰을 발급하고 해시만 저장합니다. 토큰은 승인 직후 한 번만 전달됩니다.
5. 앱은 토큰을 OS 보호 저장소(Android Keystore 기반 암호화 저장소, iPadOS Keychain)에 저장합니다.

pairing 코드는 1회 사용 후 폐기되고, 실패 시도는 횟수 제한을 둡니다.

### Credential 수명
- 기본 만료는 없습니다. revoke, 수동 회전, 설정한 장기 미접속 기간 초과 시에만 무효가 됩니다.
- 회전은 "새 토큰 발급 → 클라이언트 저장 확인 → 이전 토큰 폐기" 순서입니다.
- iPadOS 백그라운드 URLSession 작업은 생성 시점의 헤더로 나중에 실행되므로, 짧은 수명 토큰을 쓰지 않는 것이 이 작업들이 401로 실패하지 않는 전제입니다.

### Device states
- pending_approval
- active
- reauthentication_required
- revoked
- retired

기기 분실 시 해당 Device 토큰만 revoke합니다. 다른 장치와 원본 자료는 영향받지 않습니다. revoke된 장치의 미전송 Outbox는 그 장치에 남으며, 재등록하면 새 Device로 다시 업로드합니다(attachment_id가 같으므로 중복되지 않음).

## 5. Offline-first client behavior

### Android
- Room과 app-private Outbox를 사용합니다.
- 녹음은 세그먼트 파일로 기록하고(D13), 사진/파일과 함께 manifest를 로컬에서 확정합니다.
- WorkManager는 네트워크 조건이 충족되면 업로드를 재시도합니다. 즉시 실행을 보장한다고 가정하지 않습니다.
- Android의 마이크 녹음은 사용자 명시 동작과 Foreground Service 정책을 따릅니다.

### iPadOS
- 로컬 metadata cache와 file cache를 유지합니다.
- annotation/note 수정은 durable pending mutation으로 저장합니다.
- 백그라운드 URLSession은 **파일에서 업로드하는 작업만** 지원합니다. 큰 파일은 chunk를 임시 파일로 잘라 chunk마다 upload task를 만듭니다.
- 실행 시점과 완료를 즉시 보장한다고 가정하지 않습니다. 앱 재실행 시 background session의 task 목록과 로컬 transfer 레코드를 대조해 복구합니다.
- 앱 강제 종료, 기기 재부팅, 토큰 revoke, 네트워크 전환은 재시도 또는 재인증 상태로 복구합니다.

### Windows
- Windows가 꺼져 있으면 sync endpoint는 이용할 수 없습니다.
- Agent는 재시작 시 미완료 upload session과 pending ProcessingJob을 복구합니다.
- laptop sleep을 막는 것을 필수 전제로 삼지 않습니다.

## 6. Conflict and Revision Policy

### Immutable content
- Capture, Attachment, Blob, Artifact는 불변입니다.
- 파일 교체는 overwrite가 아니라 새 DocumentRevision입니다.
- annotation export PDF는 Artifact이며 editable source가 아닙니다.

### Concurrent edits
- 모든 사용자 편집 엔터티는 monotonically increasing `revision`을 가집니다.
- base_revision이 현재 revision과 다르면 Hub는 blind overwrite하지 않습니다.
- 필드 단위 자동 병합: 서로 다른 필드만 바뀐 경우(예: 한쪽은 Task 마감일, 다른 쪽은 태그) Hub가 병합하고 새 revision을 만듭니다. 병합 가능 필드 목록은 엔터티 스키마에 명시합니다.
- Note 본문, 같은 AnnotationLayer처럼 병합 불가 필드가 겹치면 ConflictRecord를 만들고 양쪽 내용을 보존한 채 사용자에게 선택/수동 병합을 요청합니다.
- 클라이언트는 pending mutation이 있는 엔터티에 대해 pull 스냅샷으로 로컬 편집을 덮어쓰지 않고, mutation 결과(accepted/conflict)를 받은 뒤 반영합니다.
- 단순한 last-write-wins를 기본 정책으로 쓰지 않습니다.

### Annotation merge (D11)
- 동기화 단위는 `AnnotationLayer(document_revision_id, page_index)`입니다. 다른 페이지 필기는 충돌하지 않습니다.
- PencilKit drawing archive는 native payload로 보존하고, 공통 표현은 standard payload(정규화 좌표)로 함께 저장합니다.
- 같은 페이지 레이어 동시 수정 시 append-only stroke 병합은 포맷 테스트로 안전성이 확인된 뒤에만 켭니다. 그 전에는 ConflictRecord로 보존하고 UI에서 compare/merge합니다.
- PDF overlay는 모든 장치에서 보기 위한 파생 결과이며 editable source를 대체하지 않습니다.

## 7. iPadOS Native Study Client

권장 구성:
- SwiftUI: navigation, adaptive split views, Stage Manager/window resizing
- PencilKit: Apple Pencil drawing capture 및 PKDrawing 저장
- PDFKit: PDF page rendering, text selection, navigation
- Share Extension 및 fileImporter: 외부 자료 수집
- URLSession background transfer: 파일 기반 queued upload/download
- Keychain: 장치 토큰
- SwiftData 또는 SQLite(GRDB 등): local cache와 pending mutation (#16에서 결정)

주요 화면:
- Course sidebar 및 학기/강의 탐색
- 중앙 PDF viewer (PPT/DOCX 등은 Hub가 만든 PDF 변환본 Artifact로 표시)
- 우측 AI summary/notes/annotation inspector
- 하단 transcript timeline와 관련 이미지
- Pencil drawing toolbar, page marker, color/highlighter

iPadOS 앱은 실제 화면 크기, regular/compact size class, orientation, Split View, Stage Manager의 동적 창 크기에 반응해야 합니다. iPad Pro 13의 4:3 화면만 하드코딩하지 않습니다.

공식 참고:
- Apple Pencil drawing: https://developer.apple.com/documentation/pencilkit
- SwiftUI adaptive layout: https://developer.apple.com/design/human-interface-guidelines/layout
- iPad desktop-class app/window resizing: https://developer.apple.com/documentation/UIKit/building-a-desktop-class-ipad-app
- Tailscale iPad support: https://tailscale.com/docs/install/ios
- Tailscale serve / HTTPS: https://tailscale.com/kb/1312/serve , https://tailscale.com/kb/1153/enabling-https

## 8. Observability

client별 다음 상태를 보여줍니다.
- last_seen_at / last_successful_sync_at
- pending_upload_count / pending_download_count
- pending_mutation_count / open_conflict_count
- last_error / retry_after
- app_version / platform
- local cache size 및 Outbox 사용량

전체 동기화 성공률뿐 아니라 기기별 지연과 실패 이유를 사용자가 진단할 수 있어야 합니다.

## 9. Acceptance scenarios

1. Fold8이 강의 녹음/사진을 수집하고 laptop online 후 업로드한다.
2. 녹음 중 앱 프로세스를 강제 종료해도 마지막 세그먼트 이전 녹음은 재생 가능하다.
3. iPad가 PDF를 offline cache에 저장하고 비행기 모드에서 Pencil annotation을 작성한다.
4. laptop이 다시 online이면 iPad annotation이 Hub에 올라가고 Fold8/Windows에서도 export PDF를 볼 수 있다.
5. laptop offline 중 Android와 iPad에서 각각 다른 자료를 추가하고 복구 후 모두 수렴한다.
6. 두 장치에서 같은 note를 동시에 수정하면 양쪽 revision이 보존되고 conflict UI가 표시된다.
7. 두 장치에서 같은 Task의 서로 다른 필드를 수정하면 자동 병합된다.
8. iPad를 분실했다고 가정하고 Device Registry에서 토큰을 revoke한 뒤 요청이 거부되고, Fold8은 영향받지 않는다.
9. URLSession transfer 중 앱을 강제 종료하거나 네트워크를 끊고, 다음 실행에서 파일 전송이 복구된다.
10. 같은 PDF를 Fold8과 iPad에서 각각 공유하면 Blob은 하나, Attachment는 둘이다.
