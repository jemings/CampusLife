# CampusLife Multi-device Architecture

구조 결정의 근거는 [DECISIONS.md](DECISIONS.md)에 있습니다.

## 1. 목표

| Device | Primary role | Native client |
|---|---|---|
| Galaxy Z Fold8 | 강의 녹음/촬영, 이동 중 수집, quick capture | Android / Kotlin / Jetpack Compose |
| iPad Pro 13 | PDF 학습, Apple Pencil 필기, AI 노트 검토, 자료 비교 | iPadOS / SwiftUI / PencilKit / PDFKit |
| Windows 11 laptop | 유일한 원천 데이터 Hub: Blob 저장, AI 처리, 검색, 로컬 웹 UI | Python / FastAPI / SQLite |

- Windows Hub가 모든 공유 상태의 유일한 직렬화 지점입니다. 필수 상시 클라우드는 두지 않습니다.
- Fold8과 iPad는 각자 durable local cache와 Outbox를 유지합니다.
- 노트북이 꺼져 있으면 모바일 장치끼리 직접 동기화하지 않고, 각자 변경을 보존했다가 Hub와 동기화합니다.

## 2. 네트워크와 서비스 노출 (D5)

    Fold8 / iPad
        │  HTTPS  https://<machine>.<tailnet>.ts.net
        ▼
    tailscale serve (공인 *.ts.net 인증서로 TLS 종단)
        │  HTTP  127.0.0.1:P1   sync API — 장치 토큰 필수
        ▼
    CampusLife Hub
        └─ 127.0.0.1:P2  admin API + 로컬 웹 UI — serve에 연결하지 않음, 로컬 admin 토큰

- Hub는 `127.0.0.1`에만 바인딩합니다. Windows 방화벽에 inbound 규칙을 열지 않고, 공유기 포트 포워딩도 하지 않습니다. Funnel은 쓰지 않습니다.
- serve를 거친 요청은 출발지가 127.0.0.1로 보이므로 출처로 권한을 판단하지 않습니다. `Tailscale-User-Login` 신원 헤더는 감사 기록에만 남깁니다(tag가 붙은 장치에서 온 요청에는 채워지지 않음).
- Tailscale HTTPS 인증서를 켜면 장치 FQDN이 공개 Certificate Transparency 로그에 남습니다. 노트북 장치 이름에 실명이나 학교명을 쓰지 않습니다.
- Tailscale ACL/grants는 "내 기기 → Hub:443"만 허용합니다.
- SFTP·FolderSync·OpenSSH는 쓰지 않습니다.
- 알려진 한계:
  - 모바일 OS는 VPN을 동시에 하나만 쓰는 것으로 알려져 있습니다(확인 필요). 학교 VPN 사용 중에는 Hub에 닿지 못할 수 있으므로 앱이 사유를 표시합니다.
  - 노트북이 sleep 상태면 응답하지 않습니다.
  - serve의 대용량 전송 동작은 DECISIONS O8에서 측정합니다.

## 3. Sync API

| API | 목적 |
|---|---|
| `POST /v1/pair` | 일회용 secret으로 장치 등록, 토큰 1회 발급 |
| `POST /v1/blobs/uploads`, `PUT …/chunks/{i}`, `POST …/finalize` | sha256 키의 재개 가능한 Blob 업로드 |
| `GET /v1/blobs/{sha}` | Range 다운로드, ETag = sha256 |
| `POST /v1/mutations` | `capture.commit`, Item patch, `suggestion.accept`, `layer.adopt` 등 모든 쓰기 (배치, 순서 보존) |
| `GET /v1/changes?since=` | 바뀐 행의 현재 상태, `hub_id`, `epoch` |
| `GET /v1/hub` | `hub_id`, `epoch`, `min_client_version` |
| `POST /v1/devices/me/status` | 진단용 heartbeat(대기 건수, 저장 공간, 마지막 오류) |
| admin 포트 | pairing secret 발급, 장치 목록, revoke, 로컬 웹 UI |

경로와 payload는 `packages/api-contracts`의 JSON Schema/OpenAPI가 기준이며, golden fixture로 세 언어 구현을 검증합니다(D17).

### 3.1 수집 업로드 (D7)

1. 클라이언트는 Capture가 `sealed`가 되기 전에도 닫힌 파일(녹음 세그먼트, 사진)의 Blob부터 올릴 수 있습니다.
2. `POST /v1/blobs/uploads {sha256, size}`를 보냅니다(`blob/upload_request`). Hub에 이미 있으면 `200 {sha256, size}`가 오고 업로드할 필요가 없습니다. 없으면 `201 {sha256, size, chunk_size, received:[i…]}`로 세션을 열거나 이어 갑니다(`blob/upload_session`).
   - 세션 키는 (Device, sha256)이고, chunk 크기는 Hub가 정하며 세션 동안 바뀌지 않습니다. chunk 번호는 0부터이고 `received`는 이미 받은 번호입니다(D20).
   - 열린 세션에 다른 size를 보내면 400 `invalid_request`입니다. Hub의 남은 공간이 size의 두 배보다 작으면 400 `hub_storage_full`이고, 클라이언트는 나중에 다시 엽니다.
   - 이미 저장된 Blob과 다른 size를 보내도 400 `invalid_request`입니다.
3. chunk는 순서와 무관하게 `PUT /v1/blobs/uploads/{sha}/chunks/{i}`하며, 같은 바이트를 다시 보내도 no-op입니다. 그래서 iOS 백그라운드 task가 뒤섞인 순서로 끝나도 안전합니다. 이미 받은 번호에 다른 바이트가 오면 409 `chunk_mismatch`입니다.
4. `POST /v1/blobs/uploads/{sha}/finalize`에서 Hub는 전체 sha256·size를 검증하고 같은 볼륨에서 원자 이동한 뒤 Blob을 등록합니다(200 `{sha256, size}`). 빠진 chunk가 있으면 409 `incomplete`(`missing_chunks`), 해시가 맞지 않으면 chunk를 폐기하고 409 `hash_mismatch`입니다. 이미 저장된 Blob의 finalize는 세션이 없어도 200이라 다시 보내도 됩니다.
5. 모든 Blob이 올라가면 `capture.commit` Mutation을 보냅니다. 참조한 Blob 중 하나라도 없으면 `retry{missing_blobs}`가 오고, 모두 있으면 한 트랜잭션으로 Capture·Attachment·초기 ProcessingJob을 등록하고 `applied`(= ACK)를 돌려줍니다.
6. 같은 capture_id + 같은 내용은 이전 결과를 반환하고, 다른 내용이면 `409 id_reuse`입니다.
7. 로컬 원본은 ACK + Hub `backed_up_at` + 보존 기간이 지나야 정리합니다(D14).

Hub에는 반쯤 도착한 Capture가 없습니다.

### 3.2 변경 피드 (D3)

- 동기화 대상 테이블의 쓰기는 같은 트랜잭션에서 전역 `change_seq`를 올리고 그 값을 행의 `rev`에 넣습니다.
- `GET /v1/changes?since=N`은 `rev > N`인 행의 현재 상태를 돌려줍니다. 클라이언트는 행을 덮어쓰고 cursor를 저장합니다. full reconcile은 `since=0`입니다.
- tombstone은 무기한 보존하므로 cursor가 만료되는 일이 없습니다.
- 클라이언트는 다음 두 경우를 확인합니다.
  - `epoch`가 바뀌면(Hub 복원·이전) 캐시를 버리고 `since=0`부터 다시 받습니다. ACK를 받았지만 Hub에 없는 Capture는 다시 올리고, 미전송 Mutation은 다시 보냅니다.
  - `hub_id`가 pairing 때와 다르면 동기화를 중단합니다.
- 피드에 넣지 않는 것: ContentBlock, ProcessingJob 상세, FTS. 노트 본문은 Artifact Blob으로 필요할 때 받습니다.

### 3.3 Mutation과 충돌 (D18)

- 형식: `{mutation_id, entity, id, op, set:{field:{from, to}}}`. 긴 텍스트는 `from_hash`를 보냅니다.
- Hub의 현재 값이 `from`과 같으면 적용합니다. 다르면 그 필드만 충돌입니다.
  - 스칼라 필드: 충돌한 필드만 제외하고 나머지를 적용합니다.
  - 텍스트 본문: Hub 값을 유지하고, 제출된 값을 ConflictRecord로 보존한 뒤 사용자에게 비교 UI를 띄웁니다.
  - Relation·Tag: 독립 행이라 충돌이 없습니다.
  - 삭제된 엔터티에 온 patch: 거부하되 제출 값은 ConflictRecord로 보존합니다.
- 결과 종류: `applied | duplicate | conflict | rejected | retry`. `rejected`는 큐를 막지 않고 "확인 필요" 목록으로 옮깁니다.
- 클라이언트는 로컬에 낙관적으로 먼저 반영하고, conflict 응답을 받으면 Hub 상태로 덮어씁니다. 클라이언트에는 병합 로직이 없습니다.
- 한 장치의 outbox는 FIFO라서 "Course 생성 → capture.commit" 같은 의존 순서가 자연스럽게 지켜집니다.

### 3.4 동기화 시점

WorkManager나 iOS는 "Hub가 켜졌다"는 것을 알 수 없습니다. 그래서 다음 시점에 시도하고, 실패하면 지수 backoff를 겁니다.
- 앱이 포그라운드로 올 때
- Capture를 봉인했을 때
- 네트워크가 바뀌거나 충전을 시작했을 때
- 사용자가 "지금 동기화"를 눌렀을 때
- outbox가 비어 있지 않을 때 주기 작업(최소 15분 간격)

푸시로 Hub 기동을 알릴지는 DECISIONS O6에서 판단합니다.

## 4. 장치 등록과 수명 (D6)

1. Windows 로컬 웹 UI에서 "장치 추가"를 누르면 QR이 뜹니다. QR에는 `{hub_url, hub_id, 128-bit secret}`이 담기고, 유효 5분·1회용입니다.
2. Fold8 또는 iPad가 QR을 스캔하고 `POST /v1/pair`를 보냅니다.
3. Hub가 `{device_id, token}`을 한 번만 돌려줍니다. Hub는 토큰의 SHA-256만 저장합니다.

- 실패 시도에는 rate limit을 겁니다. 이미 쓴 secret이 다시 오면 거부하고, 그 secret으로 발급된 장치도 revoke합니다.
- 토큰 보관: Android는 Keystore 키로 감싼 저장소, iOS는 Keychain(`AfterFirstUnlockThisDeviceOnly`, 앱과 Share Extension이 access group으로 공유)입니다.
- 장치 상태는 `active | dormant | revoked`입니다. 기본 90일 동안 접속이 없으면 `dormant`가 되어 재승인이 필요하며, 데이터·cursor·Outbox는 보존합니다.
- 토큰에 만료가 없으므로 iOS 백그라운드 task에 헤더를 고정해 둬도 안전합니다.
- 기기를 분실하면 그 장치만 revoke합니다. 다른 장치와 데이터는 영향받지 않고, 열린 업로드 세션은 폐기됩니다.

## 5. 클라이언트 동작

### Android
- 로컬 저장은 Room 하나입니다. 계약 패키지의 sync 테이블(`entity_cache`, `mutation_outbox`, `blob_transfer`, `meta`) 형태를 따릅니다.
- 녹음은 세그먼트 + 단조 시계 타임라인으로 기록합니다(D15).
- Share Intent의 `content://` 권한은 일시적이므로 받는 즉시 app-private Outbox로 복사하면서 sha256을 계산합니다.
- 업로드는 WorkManager로 하며, 즉시 실행을 보장한다고 가정하지 않습니다.

### iPadOS
- 로컬 저장은 SQLite(GRDB 등)이고, Android와 같은 sync 테이블 형태를 따릅니다.
- Share Extension은 App Group 공유 컨테이너의 outbox에 파일 사본과 메타데이터만 기록합니다. 업로드는 본 앱이 합니다.
- 백그라운드 URLSession은 파일 기반 업로드만 지원하므로, chunk를 임시 파일로 잘라 chunk마다 task를 만듭니다. 만드는 chunk 파일 수는 디스크 사용을 고려해 창(window)으로 제한합니다.
- 사용자가 앱 전환기에서 강제 종료하면 시스템이 백그라운드 전송을 취소합니다. 앱을 다시 실행하면 `getAllTasks()`와 로컬 transfer 레코드를 대조해, 사라진 task는 업로드 상태 조회부터 다시 넣습니다.
- 동기화는 포그라운드 우선이고, 백그라운드에는 전송 task만 맡깁니다.

### Windows Hub
- Windows가 꺼져 있으면 sync endpoint는 없습니다.
- Hub를 재시작하면 `uploads/`에서 받은 chunk를 복구하고 pending job을 다시 실행합니다.
- 로컬 웹 UI의 쓰기도 Mutation 적용 함수를 거칩니다.

## 6. 불변 데이터와 Annotation

- Capture, Attachment, Blob, Artifact는 불변입니다. 파일 교체는 새 Blob + `supersedes` Relation입니다.
- 필기는 `AnnotationLayer(target_blob, page_number, author_device)` 단위이고 작성자가 하나라서 정상 경로에서는 충돌이 없습니다. 화면에서는 같은 페이지의 레이어를 겹쳐 그립니다(D12).
- 기기를 바꾸면 `layer.adopt`로 이어 씁니다. 예외적으로 rev가 맞지 않으면 형제 레이어(`conflict_of`)로 보존하고 사용자가 고릅니다.
- payload는 PencilKit 원본과 `strokes.v1.json`(정규화 좌표) 두 가지입니다. overlay PDF는 Hub가 strokes로 렌더링합니다.
- PPT·DOCX·HWP는 Hub가 만든 PDF 렌디션 위에 필기하며, 필기가 붙은 렌디션은 pin됩니다.

## 7. iPadOS Native Study Client

권장 구성:
- SwiftUI: navigation, adaptive split views, Stage Manager/window resizing
- PencilKit: Apple Pencil drawing
- PDFKit: PDF와 PDF 렌디션 표시
- Share Extension(App Group) 및 fileImporter
- URLSession background transfer(파일 기반 chunk)
- Keychain: 장치 토큰
- SQLite(GRDB 등): 로컬 캐시와 outbox

주요 화면:
- Course sidebar와 학기·강의 탐색
- 중앙 PDF viewer
- 우측 AI notes와 근거 inspector(Locator로 페이지·녹음 시각 이동)
- 하단 transcript timeline
- Pencil toolbar

iPadOS 앱은 실제 창 크기, size class, orientation, Split View, Stage Manager에 반응해야 합니다. 4:3 화면을 하드코딩하지 않습니다. 배포·서명 방식은 #16 착수 전에 확정합니다(DECISIONS O7).

공식 참고:
- PencilKit: https://developer.apple.com/documentation/pencilkit
- SwiftUI layout: https://developer.apple.com/design/human-interface-guidelines/layout
- Desktop-class iPad app: https://developer.apple.com/documentation/UIKit/building-a-desktop-class-ipad-app
- Tailscale iOS: https://tailscale.com/docs/install/ios
- Tailscale serve / HTTPS: https://tailscale.com/kb/1312/serve , https://tailscale.com/kb/1153/enabling-https

## 8. Observability

장치별로 다음을 보여 줍니다(heartbeat 기반).
- last_seen_at, last_successful_sync_at
- pending_upload_count, pending_mutation_count, open_conflict_count
- last_error, retry_after
- app_version, platform
- local cache 크기, Outbox 사용량

## 9. Acceptance scenarios

1. Fold8이 강의 녹음·사진을 수집하고, 노트북이 켜진 뒤 업로드·commit합니다.
2. 녹음 중 앱 프로세스를 강제 종료해도 마지막 세그먼트 이전 녹음은 재생할 수 있습니다.
3. iPad가 오프라인에서 PDF에 필기하고, 재접속 후 Hub가 overlay를 만들어 Fold8·Windows에서 볼 수 있습니다.
4. 노트북이 꺼진 동안 Android와 iPad에서 각각 자료를 추가하면, 복구 후 모두 수렴합니다.
5. 두 장치에서 같은 Note 본문을 동시에 고치면 ConflictRecord가 생기고 양쪽 내용이 보존됩니다.
6. 같은 Task의 서로 다른 필드를 동시에 고치면 둘 다 적용됩니다.
7. iPad를 revoke하면 그 요청은 거부되고 Fold8은 영향받지 않습니다.
8. URLSession 전송 중 앱을 강제 종료해도 다음 실행에서 전송이 복구됩니다.
9. 같은 PDF를 두 장치에서 공유하면 Blob은 하나, Attachment는 둘입니다.
10. Hub를 백업에서 복원하면 epoch가 바뀌고, 클라이언트가 Hub에 없는 Capture를 자동으로 다시 올립니다.
