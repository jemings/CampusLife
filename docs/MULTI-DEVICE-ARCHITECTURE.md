# CampusLife Multi-device Architecture

## 1. 목표

CampusLife는 Windows 단일 PC 앱이 아니라 다음 세 장치를 하나의 개인 학습 데이터 공간으로 연결합니다.

| Device | Primary role | Native client |
|---|---|---|
| Galaxy Z Fold8 | 강의 녹음/촬영, 이동 중 수집, quick capture | Android / Kotlin / Jetpack Compose |
| iPad Pro 13 | PDF 학습, Apple Pencil 필기, AI 노트 검토, 자료 비교 | iPadOS / SwiftUI / PencilKit / PDFKit |
| Windows 11 laptop | primary local hub, file storage, AI processing, search | Python agent / FastAPI / SQLite |

Windows는 기본 데이터 허브이며 필수 상시 클라우드는 두지 않습니다. Fold8과 iPad는 각자 durable local cache/Outbox를 유지합니다. 노트북이 꺼져 있거나 절전 상태일 때는 두 모바일 장치가 서로 직접 동기화되지 않고 각자의 변경을 보존하며, 노트북이 다시 접속 가능해지면 hub와 동기화합니다.

## 2. 네트워크와 서비스

- Tailscale은 Fold8, iPad, Windows 사이의 사설 네트워크 연결을 담당합니다.
- Windows Local Agent는 공통 HTTPS API를 제공합니다.
- 외부 인터넷에 공개 포트를 열지 않습니다. Tailscale ACL/grants와 Windows 방화벽으로 접근 장치를 제한합니다.
- Tailscale 연결 여부만으로 CampusLife API 인증을 대체하지 않습니다.
- 앱은 Device ID와 개별 credential로 인증합니다.
- 초기 SFTP/FolderSync는 연결 검증 PoC로 유지할 수 있지만 제품 동기화는 공통 HTTPS Sync API로 통합합니다.

참고: Tailscale은 연결성을 제공하지만 서버 프로세스, 파일 큐, 보관소를 대신하지 않습니다. Windows 서비스가 실행 중이어야 API를 사용할 수 있습니다.

## 3. Common Sync API

### 3.1 핵심 API 계약

| API concept | 목적 |
|---|---|
| Pairing | Windows Hub가 만든 일회성 코드로 새 기기 승인 |
| Device Registry | 등록 장치 조회, 이름 변경, 마지막 접속, revoke |
| Create Upload Session | Capture/Attachment 업로드 세션 생성 |
| Upload Chunk | 재개 가능한 chunk upload |
| Finalize Upload | 크기/SHA-256 검증 후 atomic finalize 및 ACK |
| Push Mutations | note/annotation/task 등 client 변경 요청 전달 |
| Pull Events | cursor 이후 서버 변경 이벤트 가져오기 |
| Download Artifact | 파일과 AI artifact 다운로드 |
| Conflict Resolution | 충돌 revision 비교, 사용자 선택 반영 |

실제 경로와 payload는 OpenAPI 및 JSON Schema로 버전 관리합니다. API URL, 인증, payload schema를 앱 코드에 중복 정의하지 않습니다.

### 3.2 File upload protocol

1. Client는 capture_id, attachment_id, size, MIME, SHA-256, file name, cloud policy를 포함한 manifest를 보냅니다.
2. Hub는 인증된 device가 접근할 수 있는 upload session을 생성합니다.
3. Client는 temporary location에 파일을 chunk 단위로 전송합니다.
4. 연결이 끊기면 Hub가 받은 chunk offset/bitmap을 반환하고 client가 이어서 전송합니다.
5. Hub는 전체 길이와 SHA-256을 검증하고 최종 파일 경로로 atomic finalize합니다.
6. DB transaction으로 attachment metadata 및 ingest job을 등록한 뒤 ACK를 반환합니다.
7. Client는 ACK를 저장합니다. 설정된 보존 정책 전까지 로컬 원본을 자동 삭제하지 않습니다.

같은 capture_id/attachment_id 및 idempotency key의 재요청은 중복 원본/처리 작업을 만들지 않아야 합니다.

### 3.3 Metadata and mutation sync

- Windows Hub는 각 domain object revision과 ordered change event sequence를 관리합니다.
- 각 client는 SyncCursor를 통해 마지막으로 처리한 stream sequence를 보관합니다.
- client mutation은 mutation_id, device_id, entity_id, base_revision, operation, payload를 포함합니다.
- server는 mutation을 validate하고 accepted/rejected/conflict 결과를 반환합니다.
- client가 event pull을 다시 요청해도 마지막 cursor 이후 이벤트만 전달합니다.
- 이벤트가 오래되어 cursor retention을 벗어난 경우 full reconcile 절차를 제공해야 합니다.

## 4. Device Enrollment and Lifecycle

### Pairing
1. Windows Hub가 짧은 유효기간의 one-time pairing code/QR을 발급합니다.
2. Fold8 또는 iPad에서 스캔하거나 코드를 입력합니다.
3. Windows UI에서 새 장치 이름과 플랫폼을 확인하고 승인합니다.
4. 서버는 장치 전용 credential을 발급하고 장치의 권한/생성 시각을 기록합니다.
5. 앱은 credential을 OS secure storage에 저장합니다.

### Device states
- pending_approval
- active
- reauthentication_required
- revoked
- retired

기기 분실 시 해당 Device credential만 revoke합니다. 다른 장치와 원본 자료 전체를 강제로 초기화하지 않습니다.

## 5. Offline-first client behavior

### Android
- Room과 app-private Outbox를 사용합니다.
- 녹음/사진/파일은 manifest와 함께 로컬에서 확정합니다.
- WorkManager는 네트워크 조건이 충족되면 업로드를 재시도합니다.
- Android의 마이크 녹음은 사용자 명시 동작과 Foreground Service 정책을 따릅니다.

### iPadOS
- 로컬 metadata cache와 file cache를 유지합니다.
- annotation/note 수정은 durable pending mutation으로 저장합니다.
- URLSession background configuration을 파일 transfer에 활용할 수 있으나 실행 시점과 완료를 즉시 보장한다고 가정하지 않습니다.
- 앱 재실행 시 background session의 task 목록과 DB의 transfer records를 대조해 복구합니다.
- 앱 강제 종료, 기기 재부팅, credential 만료, 네트워크 전환은 재시도 상태로 복구합니다.

### Windows
- Windows가 꺼져 있으면 sync endpoint는 이용할 수 없습니다.
- Windows agent는 재시작 시 incomplete upload session과 pending AI job을 복구합니다.
- laptop sleep을 막는 것을 필수 전제로 삼지 않습니다.

## 6. Conflict and Revision Policy

### Immutable content
- Capture original, Attachment original, distributed course material은 기본적으로 immutable입니다.
- 파일 교체는 기존 파일 overwrite가 아니라 새 DocumentRevision으로 등록합니다.
- annotation export PDF는 원본이 아닌 derived artifact입니다.

### Concurrent edits
- 모든 mutable entity는 monotonically increasing revision 또는 ETag를 가집니다.
- mutation은 base_revision을 함께 보냅니다.
- base_revision이 최신 revision과 다르면 server는 blind overwrite하지 않고 conflict record를 만듭니다.
- 서로 다른 필드/서로 다른 page annotation 등 안전하게 분리 가능한 변화만 자동 병합 후보로 삼습니다.
- 같은 note body 또는 같은 annotation region의 충돌은 양쪽 revision을 보존하고 사용자에게 선택/수동 병합을 요청합니다.
- 단순한 last-write-wins를 기본 정책으로 쓰지 않습니다.

### Annotation merge
- PencilKit drawing archive는 원본 payload로 보존합니다.
- 공통 annotation representation은 page index와 normalized coordinate를 사용합니다.
- 단순 append-only stroke event는 겹침과 순서가 안전한 경우에만 병합합니다.
- 서로 다른 iPad 앱 세션에서 같은 layer를 수정한 경우 별도 revision으로 저장한 뒤 UI에서 compare/merge합니다.
- 표준 PDF overlay는 모든 장치에서 확인하기 위한 파생 결과이며 editable source를 대체하지 않습니다.

## 7. iPadOS Native Study Client

권장 구성:
- SwiftUI: navigation, adaptive split views, Stage Manager/window resizing
- PencilKit: Apple Pencil drawing capture 및 PKDrawing 저장
- PDFKit: PDF page rendering, text selection, navigation
- Share Extension 및 fileImporter: 외부 자료 수집
- URLSession background transfer: queued upload/download
- Keychain: device credential
- SwiftData/Core Data 또는 SQLite adapter: local cache와 pending mutation

주요 화면:
- Course sidebar 및 학기/강의 탐색
- 중앙 PDF/PPT export viewer
- 우측 AI summary/notes/annotation inspector
- 하단 transcript timeline와 관련 이미지
- Pencil drawing toolbar, page marker, color/highlighter

iPadOS 앱은 실제 화면 크기, regular/compact size class, orientation, Split View, Stage Manager의 동적 창 크기에 반응해야 합니다. iPad Pro 13의 4:3 화면만 하드코딩하지 않습니다.

공식 참고:
- Apple Pencil drawing: https://developer.apple.com/documentation/pencilkit
- SwiftUI adaptive layout: https://developer.apple.com/design/human-interface-guidelines/layout
- iPad desktop-class app/window resizing: https://developer.apple.com/documentation/UIKit/building-a-desktop-class-ipad-app
- Tailscale iPad support: https://tailscale.com/docs/install/ios

## 8. Observability

client별 다음 상태를 보여줍니다.
- last_seen_at / last_successful_sync_at
- pending_upload_count / pending_download_count
- pending_mutation_count
- last_error / retry_after
- app_version / platform
- local cache size 및 Outbox 사용량

전체 동기화 성공률뿐 아니라 기기별 지연과 실패 이유를 사용자가 진단할 수 있어야 합니다.

## 9. Acceptance scenarios

1. Fold8이 강의 녹음/사진을 수집하고 laptop online 후 업로드한다.
2. iPad가 PDF를 offline cache에 저장하고 비행기 모드에서 Pencil annotation을 작성한다.
3. laptop이 다시 online이면 iPad annotation이 Hub에 올라가고 Fold8/Windows에서도 export PDF를 볼 수 있다.
4. laptop offline 중 Android와 iPad에서 각각 다른 자료를 추가하고 복구 후 모두 수렴한다.
5. 두 장치에서 같은 note를 동시에 수정하면 양쪽 revision이 보존되고 conflict UI가 표시된다.
6. iPad를 분실했다고 가정하고 Device Registry에서 credential을 revoke한 뒤 요청이 거부된다.
7. URLSession transfer 중 앱을 강제 종료하거나 네트워크를 끊고, 다음 실행에서 파일 전송이 복구된다.
