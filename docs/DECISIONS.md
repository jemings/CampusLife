# CampusLife 설계 결정 기록

구현 전에 확정해 두는 구조 결정입니다. 다른 문서와 이 문서가 다르면 이 문서가 우선하며, 결정을 바꿀 때는 새 항목을 추가하고 기존 항목을 `대체됨`으로 표시합니다.

각 항목은 **문제 → 검토한 안 → 결정 → 결과** 순서로 적습니다.

---

## D1. 상태는 소유자별로 분리한다

**문제:** Capture 하나의 상태 목록(`local_only, queued, uploading, … processed, needs_review, failed`)에 모바일 전송 상태, Hub 수신 상태, AI 처리 상태, 사용자 검토 상태가 한 필드로 섞여 있었습니다. 문서마다 목록도 달랐습니다(ARCHITECTURE의 `LOCAL_READY → … → COMPLETED`, DOMAIN-MODEL의 Capture 상태, FILE-INGESTION의 파일 처리 상태). Hub는 업로드가 끝나기 전에는 Capture를 알지도 못하므로 `local_only`·`uploading` 같은 값을 Hub DB에 저장할 수 없습니다.

**검토한 안:**
- A. 단일 Capture 상태 머신을 유지하고 값을 늘린다 → 클라이언트와 Hub가 같은 필드를 서로 다르게 갱신하게 되고, 첨부가 여러 개면 "일부 업로드됨"을 표현하지 못합니다.
- B. 상태를 소유자별 레코드로 분리하고, 화면 요약은 그 레코드들에서 계산한다.

**결정:** B.

| 상태 | 소유자 | 저장 위치 | 값 |
|---|---|---|---|
| `OutboxEntry.transfer_state` | 각 클라이언트 | 클라이언트 로컬 DB | `pending, uploading, retry_wait, acked, failed_needs_attention` |
| `Attachment.ingest_state` | Hub | Hub SQLite | `receiving, verified, rejected` |
| `Attachment.extraction_state` | Hub (extractor) | Hub SQLite | FILE-INGESTION §9 목록 |
| `ProcessingJob.state` | Hub (작업 큐) | Hub SQLite | `pending, running, retry_wait, succeeded, failed, cancelled` |
| `Item.review_state` | 사용자 | Hub SQLite (sync 대상) | `suggested, confirmed, dismissed` |
| 유형별 진행 상태 (예: `Task.progress`) | 사용자 | Hub SQLite (sync 대상) | `inbox, planned, in_progress, blocked, completed, cancelled` |

**결과:** "녹음 업로드 중 / AI 처리 중 / 검토 필요" 같은 화면 표시는 위 레코드를 조합한 **계산값**이며 저장하지 않습니다. Capture에는 상태 필드를 두지 않습니다.

---

## D2. 원본 바이트는 내용 주소(content-addressed) 저장소에 한 번만 둔다

**문제:** 저장 레이아웃이 `originals/YYYY/<capture-id>/`였는데, 같은 PDF를 두 번 공유하거나 여러 과목에 연결하면 바이트가 중복되고, "원본 복제 없이 Relation 사용", DocumentRevision의 sha256 기반 버전 판단과도 맞지 않았습니다.

**검토한 안:**
- A. Capture별 디렉터리에 파일명 그대로 저장 → 사람이 탐색하기 쉽지만 중복 판단과 불변성 보장이 경로 규칙에 흩어집니다.
- B. `blobs/sha256/<앞 2자리>/<전체 해시>`에 한 번만 저장하고, 파일명·Capture·MIME은 Attachment 메타데이터로 관리한다. 사람이 읽을 수 있는 트리는 export로 만든다.

**결정:** B.

**결과:**
- Attachment(논리 첨부)와 Blob(바이트)은 N:1입니다. 같은 내용을 다시 받아도 Blob은 하나이며 Attachment는 수집 맥락별로 따로 생깁니다.
- Blob 파일은 쓰기 완료·해시 검증 후 읽기 전용으로 바꾸고 덮어쓰지 않습니다.
- 삭제는 Attachment 단위 tombstone이며, 어떤 Attachment도 참조하지 않는 Blob만 보존 기간이 지난 뒤 정리합니다.
- 백업·학기 export는 `Course/날짜/원래파일명` 같은 읽기 쉬운 트리를 별도로 생성합니다.

---

## D3. 메타데이터 동기화는 엔터티 스냅샷 변경 피드로 한다

**문제:** SyncEvent에 `operation`이 있어 연산 로그 재생(op-log replay)으로 읽힐 여지가 있었고, "cursor retention을 벗어나면 full reconcile"이 별도 절차로 필요했습니다. 연산 재생은 모든 클라이언트가 연산 의미를 똑같이 구현해야 하고 압축(compaction)이 어렵습니다.

**검토한 안:**
- A. 연산 로그를 순서대로 재생 → 클라이언트 3종(Kotlin·Swift·Python)이 연산 적용 로직을 각자 구현해야 합니다.
- B. Hub가 엔터티별 `revision`을 증가시키고, 변경 피드에는 `(sequence, entity_type, entity_id, revision, deleted)`만 기록한다. 클라이언트는 cursor 이후 바뀐 엔터티의 **최신 스냅샷**을 받는다.

**결정:** B.

**결과:**
- 피드 레코드는 엔터티 변경과 **같은 SQLite 트랜잭션**에서 기록합니다(별도 이벤트 로그와 DB가 어긋나지 않음).
- 같은 엔터티의 이전 피드 레코드는 지워도 됩니다(엔터티당 최신 1건만 남기면 됨). 따라서 full reconcile은 "cursor=0부터 pull"과 같은 경로이며 별도 프로토콜이 필요 없습니다.
- 삭제는 `deleted=true`인 tombstone 스냅샷으로 전파합니다.
- 내부 작업 상태 전이(ProcessingJob 재시도 등)는 피드에 넣지 않습니다. 클라이언트가 보여줘야 하는 요약 상태만 엔터티 필드로 노출합니다.
- 스트림은 Hub당 하나입니다. `stream_id`는 두지 않습니다.

---

## D4. ID는 클라이언트가 만들고, 멱등 키를 경로별로 고정한다

**문제:** "capture_id/attachment_id 및 idempotency key"라고만 적혀 있어 무엇이 같은 요청인지 정의가 없었습니다.

**결정:**
- 모든 엔터티 ID는 생성한 장치가 만드는 **UUIDv7**입니다. 오프라인에서 만든 Capture·Note·Annotation도 서버 왕복 없이 ID를 가집니다.
- 멱등 규칙:

| 경로 | 멱등 키 | 같은 키 + 같은 내용 | 같은 키 + 다른 내용 |
|---|---|---|---|
| Attachment 업로드 | `attachment_id` | 기존 ACK를 그대로 반환 | `409 conflict`, 덮어쓰지 않음 |
| Capture 등록 | `capture_id` | 기존 결과 반환 | `409 conflict` |
| Mutation | `mutation_id` | 첫 처리 결과 반환 | `409 conflict` |
| ProcessingJob | `(job_type, 입력 blob 해시 목록, pipeline_version, provider, model, prompt_version)` | 기존 Artifact 재사용 | 해당 없음(키가 다르면 새 작업) |

**결과:** 재시도는 항상 안전하며, AI 프롬프트나 모델을 바꿨을 때만 새 산출물과 새 비용이 생깁니다.

---

## D5. 네트워크 노출은 `tailscale serve`가 맡고, Agent는 localhost에만 바인딩한다

**문제:** "HTTPS API"라고만 적혀 있고 인증서 발급 방법이 없었습니다. iPadOS는 App Transport Security 때문에 유효한 TLS가 사실상 필요하고, 자체 서명 인증서는 백그라운드 URLSession에서 다루기 번거롭습니다. 또한 Agent를 tailnet 인터페이스에 직접 바인딩하면 Windows 방화벽 규칙을 따로 관리해야 합니다.

**검토한 안:**
- A. Agent가 Tailscale 인터페이스에 직접 바인딩하고 자체 서명 인증서 + 클라이언트 pinning.
- B. Agent는 `127.0.0.1`에만 바인딩하고, `tailscale serve`가 MagicDNS 이름(`<machine>.<tailnet>.ts.net`)의 유효한 인증서로 HTTPS를 종단해 localhost로 전달한다.

**결정:** B.

**결과:**
- Windows 방화벽에 CampusLife용 inbound 규칙을 열 필요가 없습니다.
- `tailscale serve`를 거친 요청은 Agent 입장에서 모두 localhost에서 온 것으로 보입니다. 따라서 **pairing 승인·장치 revoke 같은 관리 API는 별도 localhost 전용 포트로 분리**하고 그 포트는 serve에 연결하지 않습니다. 동기화 API 포트의 요청은 출처와 무관하게 항상 장치 credential을 검증합니다.
- Tailscale HTTPS 인증서를 켜면 장치 이름과 tailnet 이름이 공개 Certificate Transparency 로그에 기록됩니다. 노트북의 Tailscale 장치 이름은 실명·학교명이 드러나지 않게 정합니다.
- SFTP/FolderSync는 Phase 0 연결 확인 용도로만 남고 Agent에는 SFTP 수신 경로를 구현하지 않습니다.

---

## D6. 장치 credential은 만료 없는 불투명 토큰 + revoke로 시작한다

**문제:** "token rotation/expiry"가 요구사항에 있었지만, Hub가 꺼져 있는 동안 iPad가 2주 오프라인이었다가 돌아오면 만료된 토큰 때문에 Windows에서 재승인을 해야 합니다. 또한 iPadOS 백그라운드 URLSession 작업은 생성 시점의 헤더로 나중에 실행되므로, 짧은 수명의 access token은 OS가 작업을 늦게 실행할 때 401로 실패합니다.

**검토한 안:**
- A. 짧은 access token + refresh token 회전.
- B. 장치마다 256-bit 무작위 불투명 토큰 하나를 발급하고, Hub는 그 해시만 저장한다. 기본 만료는 없고 revoke·수동 회전·장기 미접속(설정값) 시에만 무효가 된다.

**결정:** B. 단일 사용자·사설망 환경에서 A가 막아 주는 위협(토큰 탈취 후 장기 사용)은 revoke로 대응하고, A가 만드는 실패(오프라인 복귀 후 잠김, 백그라운드 전송 401)는 피합니다.

**결과:**
- 토큰은 Android Keystore로 감싼 저장소, iPadOS Keychain에 둡니다. Hub DB에는 해시만 있습니다.
- 회전은 "새 토큰 발급 → 클라이언트 저장 확인 → 이전 토큰 폐기" 순서로, 클라이언트가 확인하기 전에는 이전 토큰을 끊지 않습니다.
- 위협 모델이 바뀌면(예: 다중 사용자) 이 결정을 다시 검토합니다.

---

## D7. Capture manifest는 한 번 보내면 바뀌지 않는다

**문제:** "기존 자료 묶음에 추가"가 가능한데, Capture manifest에 첨부를 나중에 더하면 manifest가 바뀌고 멱등 판단이 깨집니다. 또한 첨부 N개 중 일부만 올라간 Capture를 Hub가 어떻게 보여줄지 정의가 없었습니다.

**결정:**
- Capture manifest는 생성 시점의 첨부 목록을 선언하고 이후 바뀌지 않습니다.
- 나중에 파일을 더하면 **새 Capture**를 만들고, CourseMaterialSet·LectureSession 연결은 Relation mutation으로 합니다.
- Hub는 manifest를 먼저 등록하고, 선언된 첨부가 모두 `verified`가 되면 그 Capture를 완결로 봅니다. 미완결 Capture도 받은 첨부까지는 열람할 수 있고 "n/m 수신" 상태로 보입니다.
- ACK는 첨부 단위입니다. 클라이언트는 첨부별로 재시도합니다.

---

## D8. cloud policy는 하나의 enum이며, 파생 데이터는 가장 엄격한 입력을 따른다

**문제:** 이름이 `cloud_policy`(Capture), `cloud_processing_allowed`(manifest, boolean), `cloud_processing_policy`(Attachment) 세 가지였고, boolean으로는 5단계 정책을 표현할 수 없었습니다. 또한 필기·transcript·요약처럼 여러 원본에서 나온 데이터의 정책 규칙이 없었습니다.

**결정:**
- 필드 이름은 `cloud_policy` 하나, 값은 `local_only | manual_review | cloud_redacted | cloud_allowed | blocked`.
- Capture에 기본값을 두고, Attachment는 필요할 때만 override합니다. 유효 정책 = `Attachment.cloud_policy ?? Capture.cloud_policy ?? source_type별 기본값`.
- 파생 데이터(Artifact, Annotation, Note 요약 등)의 유효 정책은 **입력들 중 가장 엄격한 값**입니다. 엄격도 순서: `blocked > local_only > manual_review > cloud_redacted > cloud_allowed`.
- 정책 검사는 provider adapter 호출 직전의 단일 함수가 수행합니다. adapter를 직접 부르는 다른 경로는 두지 않습니다.
- `cloud_redacted`는 redaction 파이프라인이 구현되기 전까지 `manual_review`와 동일하게 동작합니다.
- source_type별 기본값: 강의 녹음 `manual_review`, 그 외 `local_only`. 사용자가 과목·유형 단위로 바꿀 수 있습니다.

---

## D9. AI 산출물은 불변 버전이고, 사용자 편집은 별도 엔터티다

**문제:** "파생 결과는 재생성 가능"과 "사용자가 수정한 내용은 수정 이력 보존"이 같은 Artifact에 걸려 있어, 재생성이 사용자 편집을 덮어쓸 수 있었습니다.

**결정:**
- Artifact는 생성 후 바뀌지 않습니다. 재처리는 새 Artifact 버전을 만듭니다.
- 사용자가 AI 노트를 고치면 `Note(derived_from_artifact_id=…)`가 생기고, 이후 편집은 Note의 revision으로 관리됩니다.
- 새 Artifact 버전이 나와도 기존 Note는 바뀌지 않으며, UI가 "새 AI 버전 있음"을 알려 비교하게 합니다.

---

## D10. Item은 상위 타입이고, 유형별 엔터티는 1:1 상세 레코드다

**문제:** 범용 `Item(item_type, title, status, …)`과 `Assignment`·`CampusEvent`·`Task` 등 유형 엔터티의 관계가 정의되지 않아 제목·상태를 어디에 둘지 두 곳이 생길 수 있었습니다.

**결정:**
- `Item`이 ID, 제목, 설명, `review_state`, 중요도, 태그, 출처 연결을 가집니다.
- `Assignment`, `Exam`, `CampusEvent`, `Announcement`, `Opportunity`, `Task`, `Note` 등은 같은 ID를 공유하는 1:1 상세 레코드로, 유형 고유 필드(마감일, 장소, 지원 요건, 진행 상태 등)만 가집니다.
- AI 분류가 바뀌어 유형이 바뀌면 상세 레코드를 교체하고 Item ID는 유지합니다.

---

## D11. Annotation의 동기화 단위는 (DocumentRevision, page) 레이어다

**문제:** `Annotation`과 `AnnotationLayer`가 모두 document_id·page_index·revision을 가져 어느 쪽이 동기화·충돌 단위인지 불분명했고, `DocumentVersion`(§8)과 `DocumentRevision`(§9)이 같은 개념을 중복 정의했습니다.

**결정:**
- `DocumentVersion`을 없애고 `DocumentRevision`으로 통일합니다.
- 동기화·revision·충돌 단위는 `AnnotationLayer(document_revision_id, page_index)` 하나입니다. 개별 stroke·highlight·text note는 레이어 payload 안의 요소이며 독립 엔터티가 아닙니다.
- 서로 다른 페이지의 필기는 다른 레이어이므로 자동으로 충돌하지 않습니다. 같은 페이지 레이어를 두 장치가 동시에 고치면 D3/MULTI-DEVICE 충돌 규칙을 따릅니다.
- 교수자가 자료 v2를 다시 배포하면 새 DocumentRevision이 생기고, v1의 필기는 v1에 남습니다. 자동 이전은 하지 않으며 UI에서 "이전 버전 필기 보기"를 제공합니다.

---

## D12. 원본 위치(locator)는 하나의 스키마로 정의한다

**문제:** 원본 위치 표현이 DOMAIN-MODEL §5, ContentBlock locator, SourceReference, ARCHITECTURE §8.4, Annotation 좌표에 서로 다른 필드 이름으로 다섯 번 정의되어 있었습니다.

**결정:** `packages/api-contracts`의 JSON Schema에 `Locator`를 하나 정의하고 ContentBlock, SourceReference, AnnotationLayer가 모두 이를 씁니다.

```json
{ "schema_version": 1, "kind": "pdf_page", "page": 12, "rect": [0.1, 0.2, 0.5, 0.3] }
{ "schema_version": 1, "kind": "slide", "slide": 7, "shape": 3 }
{ "schema_version": 1, "kind": "sheet_range", "sheet": "Result", "range": "B4:B18" }
{ "schema_version": 1, "kind": "heading_path", "path": ["2. 제출 일정"], "paragraph": 4 }
{ "schema_version": 1, "kind": "time_range", "start_ms": 1930000, "end_ms": 1985000 }
{ "schema_version": 1, "kind": "image_region", "rect": [0, 0, 1, 1] }
{ "schema_version": 1, "kind": "url_anchor", "url": "https://…", "anchor": "section-2" }
{ "schema_version": 1, "kind": "whole_document" }
```

`rect`는 페이지 회전·crop box를 반영한 0–1 정규화 좌표입니다. 정확한 위치를 모르면 `whole_document`를 쓰고 추정하지 않습니다.

---

## D13. 녹음은 세그먼트 파일로 저장하고, 시간은 녹음 시작 기준 오프셋으로 연결한다

**문제:** 60분 녹음을 MPEG-4 파일 하나로 기록하면 프로세스가 강제 종료될 때 파일 끝의 메타데이터(moov)가 쓰이지 않아 전체 녹음을 재생할 수 없게 됩니다. 또한 사진–transcript 연결을 벽시계 시각으로 하면 네트워크 시간 동기화나 시간대 변경 때 어긋납니다.

**결정:**
- Android는 녹음을 일정 길이(기본 5분)의 세그먼트로 이어서 기록합니다(`MediaRecorder.setNextOutputFile`, API 26+). 비정상 종료 시 잃는 범위는 마지막 세그먼트로 제한됩니다.
- 한 녹음 세션의 세그먼트들은 같은 Capture의 첨부이며 manifest에 순서와 세션 내 시작 오프셋을 기록합니다.
- 앱 안에서 찍은 사진은 `captured_at_utc`와 함께 **녹음 세션 시작 기준 경과 시간**(`elapsedRealtime` 차이)을 기록합니다. 사진–transcript 연결은 이 오프셋을 우선 사용하고, 외부 사진은 EXIF 시각(+`OffsetTime`이 있으면 사용)으로 대체하되 정확도를 낮게 표시합니다.

---

## D14. 원본 로컬 사본은 Hub 백업 확인 후에만 자동 정리한다

**문제:** "보존 정책이 정해지기 전까지 원본을 자동 삭제하지 않는다"만 있어 정책이 비어 있었습니다. Hub ACK 직후 지우면 노트북 디스크 고장 시 유일한 사본을 잃습니다.

**결정:**
- Hub는 첨부별로 `backed_up_at`(그 Blob이 포함된 첫 백업 세대 완료 시각)을 엔터티 필드로 노출합니다.
- 클라이언트의 기본 정책은 "ACK + `backed_up_at` 존재 + 7일 경과 후 로컬 사본 정리"이며 사용자가 바꿀 수 있습니다. ACK 전 삭제는 어떤 설정으로도 허용하지 않습니다.
- 저장 공간이 부족하면 자동으로 지우지 않고 사용자에게 정리 대상을 제안합니다.

---

## 미결 결정

| 항목 | 결정 시점 | 판단 기준 |
|---|---|---|
| 한국어 전문 검색 tokenizer (FTS5 `unicode61` / `trigram` / 형태소 분석 후 색인) | #11 착수 전 | 합성 한국어 자료로 2음절 검색어(예: "과제")·조사 결합("자료구조를")·부분 문자열 재현율 측정. `trigram`은 3글자 미만 질의가 MATCH로 걸리지 않으므로 단독 사용 불가 |
| HWP 5 바이너리 변환 도구 | #7 | 라이선스, Windows 설치 난이도, 합성 fixture에서 표·수식 보존률 |
| iPad 로컬 저장소 (SwiftData / GRDB 등 SQLite) | #16 착수 시 | pending mutation의 트랜잭션 보장, 마이그레이션 용이성 |
| 녹음 세그먼트 길이 | #3 | 실기기에서 세그먼트 경계 공백(gap) 측정 |
