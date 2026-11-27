# CampusLife 설계 결정 기록

구현 전에 확정해 두는 구조 결정입니다. 다른 문서와 이 문서가 다르면 이 문서가 우선합니다. 결정을 바꿀 때는 새 항목을 추가하고 기존 항목을 `대체됨`으로 표시합니다.

각 항목은 **문제 → 검토한 안 → 결정 → 결과** 순서로 적습니다.

**불변식.** 아래 결정들은 모두 이 다섯 규칙을 지킵니다.
1. Hub는 모든 공유 상태가 직렬화되는 유일한 지점입니다. 클라이언트가 Hub에 보내는 것은 Blob과 Mutation뿐입니다.
2. 모든 바이트는 sha256으로 식별되는 불변 Blob입니다. 파일명, 경로, "버전"은 Blob을 가리키는 메타데이터입니다.
3. 모든 상태는 소유자가 정확히 하나입니다.
4. AI 산출물은 불변이고, AI 제안은 엔터티가 아닙니다. 엔터티는 사용자의 확정으로만 생깁니다.
5. 외부 AI 호출은 Hub의 단일 검사 지점을 통과해야만 나갑니다.

---

## D1. 상태는 소유자별로 분리하고, 사용자 데이터의 쓰기 경로는 Mutation 하나다

**문제:** Capture 상태 필드 하나에 모바일 전송, Hub 수신, AI 처리, 사용자 검토 상태가 섞여 있었습니다. 문서마다 목록도 달랐습니다(ARCHITECTURE `LOCAL_READY → … → COMPLETED`, DOMAIN-MODEL Capture 상태, FILE-INGESTION 처리 상태 14개). 한 필드를 Android와 Hub가 둘 다 쓰는 구조였습니다. 또한 SyncCursor는 Hub 엔터티로 적혀 있었지만 실제로 cursor를 아는 쪽은 클라이언트입니다. Windows UI가 DB에 직접 쓰는 두 번째 쓰기 경로가 생길 여지도 있었습니다.

**검토한 안:**
- A. 엔터티마다 상태 필드를 두고 클라이언트와 Hub가 각자 갱신한다.
- B. 상태를 소유자별로 나누고, 남이 소유한 상태는 읽기 전용 사본으로만 갖는다.

**결정:** B.

| 소유자 | 상태 | 쓰기 경로 |
|---|---|---|
| Hub (시스템) | Attachment 메타(blob, 감지 MIME), Artifact, Suggestion, ProcessingJob, `change_seq`, `backed_up_at`, 계산된 `processing_summary` | Hub 내부 코드만 씁니다. 클라이언트에는 변경 피드로 읽기 전용 전달합니다 |
| 사용자 (어느 장치에서든) | Item, Relation, Tag, AnnotationLayer, `cloud_policy`, Suggestion 수락·기각, ConflictRecord 해소 | **오직 Mutation**으로만 씁니다. Windows 로컬 UI도 같은 Mutation 적용 함수를 호출합니다 |
| 클라이언트 로컬 | Capture 봉인 상태(`open / sealed`), 전송 상태(`pending, uploading, committed, retry_wait, needs_attention`), sync cursor, 캐시 | 해당 클라이언트만 씁니다. Hub에는 진단용 heartbeat로만 보고합니다 |

- 처리 상태는 `ProcessingJob.state`(`pending, running, retry_wait, succeeded, failed, blocked, cancelled`)에만 저장합니다. 실패·부분 성공의 이유는 `outcome_code`(`unsupported, password_required, corrupt, needs_conversion, partial, policy_blocked, budget_blocked`)로 남깁니다. *(D20으로 대체됨: `unsafe_input`이 더해짐(U3))*
- 화면의 "처리 중 / 일부 실패 / 완료"는 job을 집계한 계산값입니다. Hub가 이 값을 `processing_summary`로 피드에 실어 보냅니다.

**결과:**
- Hub DB에 쓰는 경로는 "Mutation 적용기"와 "파이프라인" 두 개뿐이고, 충돌 판정 지점은 하나입니다.
- Capture·Attachment에는 상태 필드가 없습니다.
- SyncCursor는 Hub 엔터티가 아닙니다. Hub는 관측용 `last_pulled_seq`만 기록합니다.

---

## D2. 원본은 sha256 Blob 저장소에 한 번만 두고, 문서·묶음 개념은 Blob·Capture·Relation으로 표현한다

**문제:**
- 저장 레이아웃 `originals/YYYY/<capture-id>/`는 같은 PDF를 두 번 공유하면 사본이 두 개 생기는 구조였습니다. "원본 복제 없이 Relation 사용"이라는 규칙을 보장하지 못했습니다.
- 같은 대상을 가리키는 엔터티가 여러 개 겹쳐 있었습니다. Document, DocumentVersion, DocumentRevision이 하나의 대상이었고, CourseMaterialSet은 Capture(첨부 묶음)·LectureSession(강의 단위)과 역할이 겹쳤습니다.
- 데이터 루트 기본값 `D:\`는 D: 드라이브가 없는 노트북에서 동작하지 않습니다.

**검토한 안:**
- A. Capture별 경로 + DB 중복 검사, 문서 엔터티 유지.
- B. 내용 주소(content-addressed) Blob 저장 + "bytes 정체성 = Blob, 수집 경위 = Capture, 무엇에 관한 것인가 = Item + Relation"의 세 축.

**결정:** B.
- 원본은 `blobs/<sha[0:2]>/<sha[2:4]>/<sha256>`에 확장자 없이 읽기 전용으로 저장합니다. 임시 업로드 디렉터리는 같은 볼륨에 둡니다. 그래야 이동(rename)이 원자적입니다.
- Attachment는 `(attachment_id, capture_id, blob_sha256, original_filename, declared_mime, captured_at, cloud_policy?)` 참조 행입니다. 서로 다른 Capture의 Attachment가 같은 Blob을 공유합니다.
- 없앤 엔터티와 대체 방식:
  - **Document·DocumentRevision 삭제.** 개정판은 새 Blob입니다. "B가 A의 개정판"은 Attachment 간 Relation `supersedes`로 표현합니다. AI가 감지하면 Suggestion, 사용자가 확정하면 Relation입니다.
  - **CourseMaterialSet 삭제.** 배포 정보(`distributed_by`, `distributed_at`, 설명)는 그 자료를 들여온 Capture의 `origin`에 들어갑니다. "4주차 강의 자료"는 Relation(attachment → LectureSession Item)입니다.
  - **Source 삭제.** `Capture.origin`으로 흡수합니다.
  - **ExtractorRun 삭제.** ProcessingJob이 대신합니다.
- 추출 결과(ContentBlock), PDF 렌디션, 필기는 모두 Blob 해시에 고정합니다. 그래서 같은 파일은 추출도 한 번만 합니다.
- 데이터 루트는 설치 시 선택하며 기본값은 `%LOCALAPPDATA%\CampusLife`입니다. OneDrive·Dropbox 같은 동기화 폴더 아래면 거부합니다. SQLite가 동기화 클라이언트와 충돌하기 때문입니다.
- 삭제와 정리:
  - Attachment를 tombstone 처리한 뒤 휴지통(기본 30일)을 거칩니다. 그다음 어떤 Attachment·Artifact·AnnotationLayer도 참조하지 않는 Blob만 정리합니다.
  - commit되지 않은 업로드 Blob은 7일 뒤 정리합니다.
  - 교수자·참여자가 삭제를 요청하면 휴지통을 건너뛰는 즉시 purge 경로를 씁니다. Blob과 파생물, 인덱스까지 지우고, 백업 세대에는 남아 있음을 UI에 알립니다.
- 사람이 읽을 수 있는 `과목/날짜/원래파일명` 트리는 export 기능으로 생성합니다.

**결과:**
- 클라이언트가 몰라도 되는 엔터티가 다섯 개 줄었습니다.
- 파일 하나를 여러 강의에 연결하는 일은 Relation 행을 하나 추가하는 것뿐입니다.
- 교차 문서 분석의 범위는 "한 LectureSession Item에 Relation으로 연결된 모든 Attachment"입니다.

---

## D3. 동기화는 상태 기반 변경 피드이며, 복원은 `epoch`로 감지한다

**문제:**
- SyncEvent(operation), stream_id, 서버 쪽 SyncCursor, "retention을 벗어나면 full reconcile"이 따로 정의되어 있었습니다. 이벤트 로그를 보관하고 만료시키는 별도 체계가 필요한 설계였습니다.
- Hub를 백업에서 복원하면 sequence가 되감기는데, 클라이언트가 이를 감지할 방법이 없었습니다. 그러면 클라이언트 cursor가 Hub보다 앞서서 이후 변경을 영원히 놓칩니다.

**검토한 안:**
- A. append-only 연산 로그를 재생한다.
- B. 행마다 `rev`(마지막 쓰기 시점의 전역 `change_seq`)를 두고, cursor 이후 바뀐 행의 **현재 상태**를 내려준다.
- C. CRDT 복제.

**결정:** B.
- Hub에는 SQLite 전역 카운터 `change_seq`가 하나 있습니다. 동기화 대상 테이블의 쓰기는 같은 트랜잭션에서 `change_seq += 1`을 하고 그 값을 행의 `rev`에 넣습니다.
- `GET /v1/changes?since=<seq>&limit=500`은 다음을 돌려줍니다: `{hub_id, epoch, changes:[{type, id, rev, deleted, data}], next_since, has_more}`. 스트림은 하나입니다. *(D20으로 대체됨: 항목 구분 키는 `type`이 아니라 `entity`(E18))*
- cursor는 클라이언트가 저장합니다. 같은 페이지를 다시 받아도 행을 덮어쓰기만 하므로 멱등입니다. full reconcile은 `since=0`과 같은 경로입니다.
- tombstone은 휴지통 purge 후에도 `(id, deleted)` 행으로 남깁니다. 작아서 무기한 보존할 수 있으므로 "retention 초과" 상황이 없습니다.
- **`hub_id`와 `epoch`:** 모든 응답에 싣습니다. `epoch`은 복원하거나 새 노트북으로 이전할 때 새로 발급합니다. 클라이언트는 epoch가 바뀐 것을 보면 다음을 합니다.
  1. 로컬 캐시를 버리고 `since=0`부터 다시 받습니다.
  2. ACK를 받았지만 Hub에 없는 Capture를 다시 올립니다. D14의 보존 규칙 덕분에 원본이 아직 남아 있습니다.
  3. 미전송 Mutation을 다시 보냅니다.
- 클라이언트는 pairing 때 저장한 `hub_id`와 응답의 `hub_id`가 다르면 동기화를 중단합니다.
- 피드에 넣지 않는 것: ContentBlock, ProcessingJob 상세, FTS. 파생 콘텐츠는 Artifact Blob으로 필요할 때 받습니다. Artifact·Suggestion의 메타데이터 행은 피드에 포함합니다.
- 감사가 필요한 사건(외부 AI 전송, 삭제, revoke, 충돌 해소)은 Hub 전용 append-only `audit_log`에 기록하고 동기화하지 않습니다.
- CRDT는 쓰지 않습니다. Hub가 유일한 권위이고 모바일끼리 직접 동기화하지 않는 구조라 이득이 없습니다. 같은 Note를 두 기기에서 자주 충돌시키는 사용 패턴이 관측되면 재검토합니다.

**결과:** 클라이언트 sync 엔진은 "cursor 하나 + 페이지 루프 + upsert"가 전부입니다.

---

## D4. ID는 생성자가 만드는 UUIDv4이며, 그 ID가 곧 멱등 키다

**문제:** "capture_id/attachment_id 및 idempotency key"처럼 ID와 멱등 키가 별개 개념인 것처럼 적혀 있었습니다.

**검토한 안:** UUIDv4 / UUIDv7 / ULID, 그리고 별도 `Idempotency-Key` 헤더.

**결정:**
- 엔터티, Mutation, Capture, Attachment ID는 생성한 쪽(클라이언트 또는 Hub)이 만드는 소문자 UUIDv4입니다.
  - 순서는 ID가 아니라 Hub의 `change_seq`가 정합니다.
  - UUIDv7은 Kotlin·Swift 표준 라이브러리에 생성기가 없어 세 언어에 같은 구현을 따로 넣어야 하므로 택하지 않았습니다.
- Blob ID는 sha256 소문자 hex입니다.
- 같은 ID + 같은 내용이 다시 오면 이전 결과를 그대로 돌려줍니다. 같은 ID + 다른 내용이면 `409 id_reuse`로 거부합니다. *(D20으로 대체됨: Mutation 배치 안에서는 HTTP 200 안의 `rejected{reason: id_reuse}`(E12))*
- ProcessingJob은 `job_key = hash(job_type, 입력 blob/artifact 해시 목록, pipeline_version, provider, model, prompt_version, params)`에 UNIQUE를 겁니다. 같은 키면 기존 Artifact를 재사용합니다. 사용자가 명시적으로 "다시 생성"을 누를 때만 params에 nonce를 넣어 새 키를 만듭니다.

**결과:** 별도 멱등 헤더가 없습니다. 재전송 안전성이 ID 규칙 하나로 설명됩니다.

---

## D5. 네트워크 노출은 `tailscale serve`가 맡고, Agent는 localhost에만 바인딩한다

**문제:** "HTTPS API"라고만 적혀 있고 인증서를 어떻게 얻는지가 없었습니다.
- iPadOS ATS와 Android(targetSdk 28+의 cleartext 차단)는 신뢰된 인증서의 HTTPS를 기본으로 요구합니다.
- 자체 서명 인증서를 쓰면 클라이언트마다 신뢰 설정이나 pinning을 구현해야 합니다. iOS 백그라운드 URLSession에서는 서버 신뢰 확인을 위해 앱을 깨워야 하는 부담도 생깁니다.
- ATS 예외(`NSAllowsLocalNetworking`)가 tailnet 주소나 `*.ts.net`에 적용되는지는 확인하지 못했으므로 여기에 기대지 않습니다.

**검토한 안:**
- A. 자체 서명 인증서 + 클라이언트 pinning.
- B. 평문 HTTP + ATS·cleartext 예외.
- C. `tailscale cert`로 받은 인증서를 Agent가 직접 로드.
- D. Agent는 `127.0.0.1`에만 바인딩하고, `tailscale serve`가 `<machine>.<tailnet>.ts.net`의 공인 인증서로 HTTPS를 종단한다.

**결정:** D. C는 측정 결과 serve에 문제가 있을 때 쓰는 대체안입니다(미결 O3).
- **listener를 둘로 나눕니다.**
  - sync 포트(`127.0.0.1:P1`)만 serve로 노출합니다. 모든 요청에 장치 토큰이 필요하며, pairing 엔드포인트도 여기 있습니다.
  - admin 포트(`127.0.0.1:P2`)는 serve에 연결하지 않습니다. 로컬 웹 UI, pairing 코드 발급, revoke가 여기 있습니다. 인증은 DPAPI로 보호한 로컬 admin 토큰으로 합니다. 장치 토큰으로는 admin 경로에 접근할 수 없습니다.
- serve를 거친 요청은 Agent에서 출발지가 127.0.0.1로 보이므로 **출처로 권한을 판단하지 않습니다.**
- serve는 요청에 `Tailscale-User-Login` 등 신원 헤더를 붙이고, 외부에서 넣은 같은 이름의 헤더는 지웁니다. 하지만 tag가 붙은 장치에서 온 요청에는 이 헤더가 채워지지 않습니다. 그래서 인증 근거로는 쓰지 않고 감사 기록에만 남깁니다.
- Tailscale HTTPS 인증서를 켜면 장치 FQDN이 공개 Certificate Transparency 로그에 기록됩니다(Tailscale 공식 문서로 확인). 노트북 장치 이름에 실명이나 학교명을 쓰지 않습니다.
- Funnel은 쓰지 않으며 꺼져 있음을 테스트로 확인합니다. Tailscale ACL은 "내 기기 → Hub:443"만 허용합니다.
- SFTP·FolderSync·OpenSSH는 제품과 PoC 모두에서 쓰지 않습니다. 해시와 ACK를 우회하는 두 번째 수집 경로이기 때문입니다.
- 운영 한계로 문서화할 것:
  - Android와 iOS는 VPN을 동시에 하나만 쓰는 것으로 알고 있습니다(확인 필요). 학교 VPN을 켜면 Hub에 닿지 못할 수 있으므로, 앱이 그 사유를 표시합니다.
  - 노트북이 sleep 상태면 serve도 응답하지 않습니다.

---

## D6. 장치 인증은 QR 한 번의 pairing과 만료 없는 불투명 토큰이다

**문제:**
- "token rotation/expiry", DeviceSession, `reauthentication_required` 같은 개념이 나열만 되어 있었습니다.
- 짧은 만료는 두 가지와 충돌합니다. 며칠 쌓인 오프라인 Outbox가 만료 토큰 때문에 통째로 401이 되고, 요청 헤더가 task 생성 시점에 고정되는 iOS 백그라운드 URLSession도 실패합니다.
- pairing 절차는 "코드 발급 → 장치 스캔 → Hub UI에서 다시 승인"이었습니다. Hub 화면에 코드를 띄우는 행위 자체가 이미 물리적 승인이므로 승인 단계가 중복이었습니다.

**검토한 안:**
- A. 단기 access token + refresh token.
- B. 장치 키쌍 서명 요청 또는 mTLS.
- C. 장치별 256-bit 불투명 토큰. 만료 없이 revoke와 수동 재발급만 둔다.

**결정:** C.
- **Pairing:**
  1. Hub admin UI에서 "장치 추가"를 누르면 QR이 뜹니다. QR에는 `{hub_url, hub_id, 128-bit pairing secret}`이 담기고, 유효 5분·1회용입니다. 수동 입력용 코드도 함께 보여 줍니다.
  2. 장치가 `POST /v1/pair {secret, platform, device_name, app_version}`를 보냅니다.
  3. Hub가 `{device_id, token}`을 한 번만 반환합니다.
  - 실패 시도에는 rate limit을 겁니다. 이미 쓴 secret이 다시 오면 거부하고, 그 secret으로 발급된 장치도 revoke합니다(재사용 탐지).
- **저장:** Hub는 `SHA-256(token)`만 저장합니다.
  - Android는 Keystore 키로 감싼 저장소에 둡니다.
  - iOS는 Keychain에 `AfterFirstUnlockThisDeviceOnly`로 둡니다. 잠금 상태에서도 백그라운드 전송이 토큰을 읽을 수 있어야 하기 때문입니다. 앱과 Share Extension은 access group으로 같은 토큰을 씁니다.
- **장치 상태:** `active | dormant | revoked` 셋입니다.
  - 설정한 기간(기본 90일) 동안 접속이 없으면 `dormant`가 되어 재승인이 필요합니다. 데이터·cursor·Outbox는 보존합니다.
  - 사용자가 "토큰 재발급"을 누르면 새 토큰을 발급합니다. 이전 토큰은 클라이언트가 새 토큰 저장을 확인한 뒤에 폐기합니다. *(D20으로 대체됨: 재발급은 클라이언트 앱이 `POST /v1/devices/me/token`으로 시작하고, 이전 토큰은 새 토큰의 첫 인증 성공 때 폐기됨)*
- **revoke:** 해당 토큰 요청은 401 `device_revoked`가 되고 열린 업로드 세션도 폐기합니다.
  - 그 장치의 미전송 데이터는 재pairing 후 다시 올릴 수 있습니다. Capture는 ID로 멱등이고, Mutation은 CAS로 재판정되기 때문입니다.
- **Windows UI·CLI는 Device가 아닙니다.** admin 포트를 씁니다. 로컬 import의 출처 표기에만 `hub` pseudo-device를 씁니다. *(D21로 대체됨: 출처는 Hub 자신의 `windows_hub` Device 행입니다(E15, #20 A7). 복원하면 Hub 자신을 뺀 active 장치가 모두 dormant가 됩니다(U6))*

---

## D7. 업로드는 Blob을 먼저 올린 뒤 봉인된 Capture를 commit한다

**문제:**
- 수신 방식이 두 가지였습니다. ARCHITECTURE의 디렉터리 + READY 마커 방식과 MULTI-DEVICE의 첨부별 upload session 방식이 공존했습니다.
- ACK의 의미가 정의되어 있지 않았고, 파일 일부만 도착한 Capture를 Hub가 어떻게 다룰지도 없었습니다.
- iOS 백그라운드 URLSession은 파일 기반 upload task만 지원하고 실행 순서를 시스템이 정합니다. 따라서 순서대로 이어 붙이는 방식(tus 식 append)과 맞지 않습니다.

**검토한 안:**
- A. manifest 먼저 → Capture별 upload session.
- B. Blob을 먼저 올리고(sha256이 세션 키, 순서 무관 chunk), 모든 Blob이 있으면 Capture를 commit한다.
- C. tus·IETF Resumable Uploads 같은 표준 프로토콜. Python 서버 구현이 성숙하지 않았고 두 클라이언트에 대응 구현이 고르지 않아 택하지 않았습니다.

**결정:** B.

```
POST /v1/blobs/uploads            {sha256, size, chunk_size} → 200 exists | 201 {received:[i…]}   ← D20으로 대체됨: 요청은 {sha256, size}, chunk_size는 세션 응답이 정함(E19)
PUT  /v1/blobs/uploads/{sha}/chunks/{i}   (순서 무관, 같은 바이트 재전송은 no-op)
POST /v1/blobs/uploads/{sha}/finalize     → 200 stored | 409 hash_mismatch (chunk 폐기)
POST /v1/mutations  [{type: capture.commit, capture_id, origin, links[], attachments[], timeline?}]
     → applied | retry{missing_blobs[]} | conflict(id_reuse)   ← D20으로 대체됨: 봉투는 {mutation_id, op: capture.commit, capture}(E11), id_reuse는 rejected의 reason(E12)
GET  /v1/blobs/{sha}              Range 지원, ETag = sha256
```

- finalize 순서: 각 chunk fsync → 결합 → 전체 sha256·size 검증 → 같은 볼륨 원자 이동 → DB 등록. Windows Defender 등이 파일을 잠깐 잠그면 짧게 재시도합니다.
- **ACK = `capture.commit`의 applied 응답입니다.** "모든 Blob이 최종 경로에 있고, Capture·Attachment 행과 초기 ProcessingJob이 한 트랜잭션(`synchronous=FULL`)으로 커밋되었다"는 뜻입니다. 따라서 Hub에는 반쯤 도착한 Capture가 없습니다.
- **Capture 봉인:** Capture는 클라이언트에서 `open → sealed`로 바뀌며 commit은 sealed에서만 합니다.
  - 녹음 중에도 닫힌 세그먼트·사진 Blob은 미리 올릴 수 있습니다.
  - 봉인 후 생긴 자료는 새 Capture로 만들고, Relation으로 같은 LectureSession에 묶습니다.
- `links[]`는 사용자가 직접 고른 연결(녹음 화면에서 고른 과목·강의 세션)입니다. 사용자의 선택이므로 Suggestion이 아니라 확정 Relation으로 들어갑니다.
- **클라이언트 저장 위치:** manifest 파일과 READY 마커는 없앱니다. 클라이언트 상태는 로컬 DB(Room/SQLite) 한 곳에 둡니다.
- **Share Extension(iPad)·Share Intent(Android):** 수신 즉시 파일을 앱 Outbox(iPad는 App Group 공유 컨테이너)로 복사하면서 sha256을 스트리밍으로 계산합니다. 업로드는 본 앱의 sync 엔진이 합니다. Extension에서 업로드를 끝까지 하지 않습니다.
- **Windows 로컬 import:** 드래그앤드롭도 같은 함수(Blob 저장 + `capture.commit`, device=`hub`)를 호출합니다.
- **기본 chunk 크기:** 8 MiB입니다(미결 O8). 50 MB를 넘는 Blob은 기본적으로 unmetered 네트워크에서만 보내며, 사용자가 바꿀 수 있습니다.

---

## D8. cloud policy는 3단계 전순서 enum이며, 파생 데이터는 가장 엄격한 입력을 따른다

**문제:**
- 정책 표현이 셋이었습니다. Capture의 `cloud_policy`, manifest의 boolean `cloud_processing_allowed`, Attachment의 `cloud_processing_policy`.
- 5개 값이 한 축이 아니었습니다.
  - `manual_review`는 데이터 등급이 아니라 동의 방식입니다.
  - `cloud_redacted`는 redactor가 없어서 구현할 수 없습니다.
  - `blocked`가 로컬 처리까지 막는지 모호했습니다.
- 여러 원본에서 나온 데이터의 정책 규칙이 없었습니다. 예: `local_only` 녹음을 로컬 STT로 받아 적은 transcript가 cloud LLM 요약으로 가는 것을 막는 규칙이 없었습니다.

**검토한 안:**
- A. 5값을 유지하고 문서화한다.
- B. 처리 위치를 3단계 전순서 값으로 두고, 동의 방식은 별도 설정으로 분리한다.

**결정:** B.
- **값:** `blocked < local_only < cloud_allowed`. 필드 이름은 `cloud_policy` 하나입니다.
  - `blocked`: 보존, 해시, 형식 검증, 텍스트 추출, FTS만 합니다. 모델 추론은 로컬이든 클라우드든 하지 않습니다.
  - `local_only`: Hub 안의 로컬 provider만 씁니다.
  - `cloud_allowed`: 설정의 provider allow-list로 전송할 수 있습니다.
- **부착 위치:** Attachment에 둡니다. Capture를 만들 때 클라이언트가 해석한 값을 기록하므로 오프라인에서도 가능합니다.
  - 해석 순서: 명시값 → 연결된 Course Item의 `default_cloud_policy` → 전역 기본 `local_only`.
  - 과목 기본값을 `cloud_allowed`로 바꿀 때는 "이 과목은 녹음 처리 허용을 확인함"을 한 번 받고 그 사실을 기록합니다.
- **파생 규칙:** Artifact, Suggestion, AnnotationLayer, Note 요약의 유효 정책은 `min(입력들)`입니다.
  - 같은 Blob을 가리키는 여러 Attachment의 정책도 min입니다.
  - 계산은 저장하지 않고 provider 호출 시점에 입력의 현재 정책으로 합니다. 그래서 정책을 엄격하게 바꾸면 그 뒤의 호출부터 막힙니다.
- **단일 검사 지점:** 모든 provider 호출은 `ProviderGateway.call(capability, inputs, payload)`를 거칩니다. 판정은 `allow | deny(reason) | needs_confirmation`입니다. adapter를 gateway 밖에서 import할 수 없도록 테스트로 강제합니다.
- **동의 방식:** 정책이 아니라 설정입니다.
  - `confirm_each_send`(전역 또는 과목, 기본 false): true면 전송 전에 provider, 모델, 입력 종류·크기, 예상 비용, 취소 방법을 보여 줍니다.
  - budget guard: 월 예산이나 job당 상한을 넘으면 job이 `budget_blocked`가 되어 사용자 승인을 기다립니다.
- **전송 기록:** 외부 호출마다 `audit_log`에 `{시각, provider, model, 입력 id, 바이트, 정책 스냅샷, 확인 여부, 비용}`을 남깁니다. 정책을 엄격하게 바꾸면 "이미 N회 전송됨(회수 불가)"을 UI에 표시합니다.
- `cloud_redacted`는 v1에 없습니다. 한국어 PII 탐지기의 재현율이 검증되면(미결 O5) `local_only`와 `cloud_allowed` 사이 값으로 추가합니다. *(D20으로 대체됨: `cloud_policy`는 닫힌 enum이라 첫 클라이언트 배포 뒤에는 값을 더할 수 없음. 첫 배포 전에 넣거나 계약 버전을 올려야 함)*

**결과:**
- 세 클라이언트가 알아야 할 것은 "값 3개와 min"뿐입니다.
- 로컬 STT가 없으면 `local_only` 녹음의 STT는 `blocked` 상태로 남습니다(미결 O1).

---

## D9. AI 산출물은 불변 버전이고, 사용자 편집은 Note로 fork한다

**문제:**
- "파생 결과는 재생성 가능"과 "사용자 수정 이력 보존"이 같은 Artifact에 걸려 있었습니다.
- LLM 출력은 같은 입력으로도 결정적이지 않으므로 "재생성 가능"은 사실이 아닙니다. 이 문구 때문에 Artifact를 캐시로 착각해 백업에서 빠뜨릴 위험이 있습니다.

**검토한 안:**
- A. Artifact를 편집 가능하게 하고 재실행 시 병합한다.
- B. Artifact는 불변이고, 사용자 편집은 별도 Note로 만든다.

**결정:** B.
- `Artifact{id, artifact_type, subject_ref, job_key, provider, model, prompt_version, pipeline_version, input_hashes[], content_blob, created_at}`. *(D20으로 대체됨: provider·model·prompt_version은 모델을 쓴 job에만 있는 선택 열, `artifact_type`은 열린 값)*
- 재실행은 새 Artifact를 만듭니다. `(subject_ref, artifact_type)`별 `current` 포인터는 Hub가 소유합니다.
- "AI 노트 편집"을 누르면 `Note` Item(`fields.forked_from_artifact`)이 생기고, 본문과 인용이 복사됩니다. 이후 편집은 Note의 Mutation입니다.
- 새 AI 버전이 나오면 UI는 "새 버전 있음(비교)"만 보여 주고 Note를 바꾸지 않습니다.
- 문서 표현을 "**기록된 입력으로 재실행 가능**(결과는 달라질 수 있음)"으로 고칩니다.
  - Artifact는 백업 대상입니다.
  - 캐시로 취급하는 것은 FTS 인덱스, 썸네일, overlay 렌더링뿐입니다.

---

## D10. Item은 kind와 kind별 fields를 가진 단일 엔터티이고, AI 제안은 별도 Suggestion이다

**문제:**
- 범용 Item과 유형 엔터티 16개(Course, Assignment, CampusEvent…)의 관계가 정의되지 않았습니다. 독립 테이블이라면 세 언어가 각각 16개 모델과 충돌 규칙을 구현해야 합니다.
- AI 결과를 `review_state=suggested`인 Item으로 저장하는 구조도 문제였습니다. Item을 읽는 쿼리 중 하나만 `confirmed` 필터를 빠뜨려도 미확정 AI 날짜가 일정·알림에 섞입니다. 재처리 때마다 같은 후보가 중복으로 생기고, 기각한 후보가 다시 살아나는 문제도 있었습니다.

**검토한 안:**
- A. 타입별 테이블과 API.
- B. Item 상위 타입 + 유형별 1:1 상세 테이블.
- C. 단일 Item(공통 열 + kind별 JSON Schema로 검증하는 `fields`), AI 제안은 Suggestion 엔터티로 분리.

**결정:** C.
- **Item 구조:** `Item{id, kind, schema_version, title, body?, status?, starts_at?, due_at?, all_day?, fields{…}, rev, deleted_at?}`. *(D20으로 대체됨: `status`는 없음, task 상태는 `fields.progress`(U4))*
  - 일정 로직에 필요한 날짜·상태는 공통 열에 둡니다. 질의가 필요한 fields는 Hub에서 generated column으로 승격합니다.
  - MVP kind: `term, course, lecture_session, note, task, assignment, exam`.
  - 후속 kind: `event, announcement, opportunity, organization, place, person`.
  - 시간표 슬롯은 `course.fields.schedule_slots[]`이고, 반복 일정은 D11의 `local_time_rule` 타입입니다.
- **LectureSession 생성:** 사용자가 녹음을 시작할 때 생깁니다. Android가 (course, 날짜)로 기존 세션을 찾고, 없으면 create Mutation을 보냅니다. 두 장치가 같은 강의에 세션을 각각 만들면 Item 병합 액션(Relation 이전 + 하나 삭제)으로 정리합니다.
- **Suggestion:** `Suggestion{id, source_artifact_id, action: create_item|patch_item|add_relation, proposed{…}, evidence[](Locator), fingerprint, state: open|accepted|dismissed|superseded}`. *(D20으로 대체됨: `action`은 열린 값이고 알려진 값이 `create_item|patch_item|add_relation`(E21))*
  - 수락은 `suggestion.accept{id, edits?}` Mutation입니다. *(D20으로 대체됨: `{mutation_id, op: suggestion.accept, suggestion_id, edits?}`(E11))* Hub가 한 트랜잭션으로 Item을 만들고 근거를 Relation으로 복사합니다.
  - `fingerprint = hash(action, kind, 정규화한 핵심 필드)`입니다. 기각된 fingerprint는 재처리해도 다시 열리지 않습니다.
  - 상충하는 날짜·지시는 서로 `conflicts_with`로 참조하는 Suggestion 쌍이며, 사용자가 하나를 고릅니다.
- **클라이언트 호환 규칙:** 모르는 kind는 제목·날짜·본문만으로 일반 렌더링합니다. patch는 바꾼 필드만 보내므로, 모르는 필드를 보존할 필요가 없습니다.

**결과:**
- Item 테이블에는 사용자가 확정한 사실만 있습니다. Planner·알림·검색에서 예외 처리가 필요 없습니다.
- 세 클라이언트의 모델은 다음이 전부입니다.
  - 편집 가능: Item, Relation, Tag, AnnotationLayer, ConflictRecord
  - 읽기 전용: Capture, Attachment, Artifact, Suggestion

---

## D11. 시간은 세 타입으로 구분하고, 모든 문자열은 NFC로 정규화한다

**문제:**
- "UTC로 저장, Asia/Seoul로 표시"를 모든 경우에 적용하는 규칙은 맞지 않는 곳이 있습니다. 종일 마감("10월 3일까지")은 순간이 아니라 날짜이고, 매주 반복되는 시간표는 현지 시각 규칙입니다.
- EXIF `DateTimeOriginal`은 시간대가 없을 수 있습니다.
- Apple 기기에서 온 파일명·텍스트는 한글이 NFD(자모 분해)로 들어올 수 있습니다. 그러면 NFC로 입력한 검색어와 일치하지 않습니다.

**결정:**
- `instant`(UTC, RFC 3339 `Z`): 생성·수정·촬영·녹음처럼 **이미 일어난 일**.
- `local_date`(+ IANA tz): 종일 일정, 날짜 마감.
- `local_time_rule`(`HH:MM` + IANA tz + 요일·기간): 시간표 반복. 표시할 때 전개합니다.
- "23:59까지" 같은 마감은 `instant`로 저장하고, 원문 표현은 `fields.due_text`에 보존합니다. "자정"처럼 모호한 표현은 Suggestion으로 확인을 받습니다.
- 저장·비교하는 모든 문자열(파일명, 제목, 본문, 검색어)은 NFC로 정규화하고 원문 문자열을 함께 보존합니다. Blob 바이트는 건드리지 않습니다. *(D20으로 대체됨: wire 문자열은 NFC여야 하고 아니면 `not_canonical`로 거부, 원문 보존은 파일명만(U2))*

---

## D12. Annotation은 (Blob, page, device) 단위의 작성자 하나짜리 레이어다

**문제:**
- Annotation과 AnnotationLayer가 공존해서 동기화 단위가 레이어인지, 개별 주석인지, stroke인지 정해지지 않았습니다.
- overlay PDF를 iPad의 PDFKit으로 만들면 iPad가 꺼져 있을 때 Windows·Android에서 필기를 볼 수 없습니다.
- PDFKit은 PPTX·DOCX·HWP를 렌더링하지 못하므로 PDF 렌디션이 필요합니다. 그런데 렌디션이 다시 만들어지면 필기 위치가 어긋나는 문제를 어느 문서도 다루지 않았습니다.

**검토한 안:**
- 레이어 크기: 문서당 하나 / 페이지당 하나 / stroke 이벤트.
- 작성자: 여러 장치가 공유하는 레이어 / 장치별 레이어.

**결정:**
- **레이어 단위:** `AnnotationLayer(target_blob_sha256, page_number(1-based), author_device_id)`.
  - 각 레이어는 작성자가 하나이므로 정상 경로에서는 충돌이 없습니다.
  - 화면에서는 같은 (Blob, page)의 모든 레이어를 겹쳐 그립니다. 다른 장치의 레이어는 읽기 전용입니다.
  - 기기를 바꾸거나 앱을 재설치하면 `layer.adopt`로 기존 레이어를 복사해 새 장치 소유로 이어 씁니다. 원본 레이어는 보존합니다.
  - 복원 같은 예외 상황에서 `rev`가 맞지 않으면, 제출본을 `conflict_of`가 붙은 형제 레이어로 저장하고 사용자가 "둘 다 유지 / 하나 선택"을 고릅니다. stroke 단위 자동 병합은 하지 않습니다.
- **payload:** 레이어 revision마다 Blob 두 개를 둡니다.
  - `drawing.pkd`: PKDrawing 원본. 불투명하며 iPad만 편집합니다.
  - `strokes.v1.json`: 표준 표현. ink stroke(points, 잉크 종류·색·굵기), highlight, text note를 담습니다.
  - 좌표는 회전·CropBox를 적용한 표시 방향 페이지 기준 `[0,1]` 정규화 좌표입니다. 원점은 좌상단이고, `page_box{w, h, rotation}`을 함께 기록합니다.
  - Hub는 PKDrawing을 해석하지 않습니다.
- **overlay 렌더링:** overlay PDF·PNG는 **Hub가** `strokes.v1.json`으로 렌더링합니다. Artifact이며 캐시입니다. *(D20으로 대체됨: `artifact_type: overlay`인 Artifact 행으로 동기화되고, 같은 입력이면 다시 만들 수 있음. 백업 여부는 Artifact와 같음)* Android와 Windows는 이것을 엽니다. 렌더러는 Hub 하나입니다.
- **렌디션 고정:** PPTX·DOCX·HWP는 Hub가 PDF 렌디션 Blob을 만들고 `Relation(rendition_of)`로 원본에 연결합니다. 필기는 **렌디션 Blob에 고정**됩니다.
  - 렌디션에 레이어가 하나라도 생기면 그 렌디션 Blob은 pin되어 재생성·정리 대상에서 빠집니다.
  - 변환기 버전이 바뀌면 새 렌디션은 새 Blob이 되므로 기존 필기가 어긋나지 않습니다.
- **개정판 처리:** 개정판(새 Blob)으로 필기를 자동 이동하지 않습니다. `supersedes` Relation이 있고 페이지 수가 같으면 "필기 이월" Suggestion을 냅니다.
- **저장과 업로드 주기:** 펜을 떼고 잠시 뒤, 페이지를 넘길 때, 앱이 백그라운드로 갈 때 로컬 revision을 저장합니다. 업로드는 아직 못 올린 최신 revision 하나만 합니다.
- **AI 처리:** 필기 OCR·AI 입력은 strokes를 이미지로 렌더링한 뒤 gateway를 거칩니다. 정책은 대상 문서에서 상속합니다(D8).

---

## D13. 원본 위치(Locator)는 하나의 스키마로 정의하고, AI 인용은 block id로만 한다

**문제:**
- 원본 위치 표현이 다섯 곳에 서로 다른 필드 이름으로 정의되어 있었습니다.
- 페이지 기준이 없었습니다. PDFKit은 0-based이고 사용자 표시는 1-based입니다.
- 오디오 오프셋을 처리용 STT chunk 기준으로 저장하면, chunk 분할 방식이 바뀌는 순간 근거가 틀어집니다.
- LLM에게 페이지 번호를 직접 쓰게 하면 근거를 꾸며낼 수 있습니다.

**결정:** `packages/api-contracts`의 JSON Schema에 `Locator` 하나를 정의하고, 앵커는 Blob 해시입니다.

```json
{ "v": 1, "blob": "<sha256>", "kind": "pdf_page", "page": 12, "box": [0.1, 0.2, 0.5, 0.3] }
{ "v": 1, "blob": "<sha256>", "kind": "slide", "slide": 7, "shape": 3 }
{ "v": 1, "blob": "<sha256>", "kind": "doc_path", "heading_path": ["2. 제출 일정"], "block_index": 4 }
{ "v": 1, "blob": "<sha256>", "kind": "sheet_range", "sheet": "Result", "a1": "B4:B18" }
{ "v": 1, "capture": "<id>", "kind": "media_time", "start_ms": 1930000, "end_ms": 1985000 }
{ "v": 1, "blob": "<sha256>", "kind": "image_region", "box": [0, 0, 1, 1] }
{ "v": 1, "blob": "<snapshot sha256>", "kind": "web_snapshot", "url": "https://…", "anchor": "section-2" }
{ "v": 1, "layer": "<id>", "rev": 5, "kind": "annotation", "box": [0.2, 0.4, 0.3, 0.1] }   ← D20으로 대체됨: annotation kind는 #17이 추가, box는 [x, y, w, h](E17)
{ "v": 1, "blob": "<sha256>", "kind": "document" }
```

- **좌표·번호 규약:**
  - page와 slide는 1-based이고, PDFKit 0-based는 클라이언트 경계에서 변환합니다.
  - `box`는 D12와 같은 정규화 좌표입니다.
  - 시간은 정수 ms입니다.
- **녹음 시간:** `media_time`은 Capture 녹음 타임라인(D15의 세그먼트를 이어 붙인 절대 시각) 기준입니다. 처리 chunk 오프셋은 Artifact를 만들기 전에 반드시 이 절대 시각으로 변환합니다.
- **URL:** 앵커는 저장한 스냅샷 Blob입니다. 스냅샷이 없는 URL은 `document` 수준 근거만 가집니다.
- **정밀 위치를 모를 때:** `document`를 쓰고 추정하지 않습니다.
- **ContentBlock:** `ContentBlock.id = hash(blob_sha256, extractor_id, extractor_version, ordinal)`이므로 결정적입니다.
- **인용 생성:**
  1. LLM 입력에는 `[B:<block_id>]` 태그를 붙인 블록만 넣고, 출력에서 그 id만 인용하게 합니다.
  2. Hub는 인용된 id를 검증합니다. 모르는 id는 제거하고, 해당 주장은 "근거 없음"으로 표시합니다.
  3. 남은 id를 Locator로 치환합니다.
  - 이렇게 하면 LLM이 페이지 번호나 시각을 직접 만드는 경로가 없습니다.
- **Artifact 본문:** 구조화 JSON(`blocks[{text, refs:[ref_id]}]`)이며 Markdown은 렌더링 결과입니다. Note로 fork할 때 refs도 복사됩니다.

---

## D14. 모바일 원본은 Hub 백업 확인 후에만 자동 정리한다

**문제:** "보존 정책이 정해지기 전까지 삭제하지 않는다"만 있어 폰 저장 공간이 계속 찼습니다. 반대로 ACK 직후 지우면 원본이 노트북 한 대에만 남고, Hub를 복원할 때(D3) 다시 올릴 원천도 없습니다.

**결정:**
- Hub는 백업 세대가 완료되면 해당 Blob을 참조하는 Attachment의 `backed_up_at`을 갱신해 피드로 보냅니다. *(D21로 대체됨: 그 Blob을 처음 담은 세대가 끝날 때 한 번 찍습니다)*
- **자동 삭제 조건:** `committed ∧ backed_up_at 존재 ∧ 보존 기간(기본 7일, 설정) 경과`. ACK 전 삭제는 어떤 설정으로도 허용하지 않습니다.
- **수동 정리:** 사용자가 "공간 확보"를 누르면 committed 항목만 대상입니다. `backed_up_at`이 없으면 "노트북에만 있음" 경고를 거칩니다.
- **백업이 없을 때:** 백업이 한 번도 돌지 않았으면 자동 삭제가 일어나지 않고 Hub와 앱이 경고합니다.
- **iPad 캐시:** 다운로드 캐시는 LRU로 정리합니다. 사용자가 "오프라인 보관"으로 고정한 자료와 미전송 레이어는 제외합니다.

---

## D15. 녹음은 크래시에 견디는 세그먼트와 단조 시계 타임라인으로 기록한다

**문제:**
- 녹음 방식이 MediaRecorder와 AudioRecord 사이에서 미정이었고, 문서는 단일 `audio.m4a`를 전제했습니다.
- MPEG-4 파일은 녹음을 끝낼 때 파일 끝에 인덱스(moov)를 씁니다. 그래서 프로세스 강제 종료, 방전, 저장 공간 부족 시 60분 녹음 전체를 재생하지 못하게 됩니다.
- 사진–녹음 연결 기준도 없었습니다.
  - EXIF는 1초 해상도이고 시간대가 없을 수 있습니다.
  - 벽시계는 NTP 보정으로 갑자기 바뀔 수 있습니다.
  - 일시정지하면 녹음 위치와 실제 경과 시간이 달라집니다.

**결정 (계약, 확정):**
- **세그먼트:** 녹음 산출물은 세그먼트 목록입니다. 각 세그먼트는 5분 이하이고, 닫히면 단독으로 재생할 수 있습니다. 크래시로 잃는 범위는 열려 있던 세그먼트 하나입니다.
  - 각 세그먼트는 Attachment(`role=audio_segment`, `seq`)입니다.
- **`timeline`:** `capture.commit`에 포함되는 녹음 타임라인입니다.
  - 이벤트 목록: `start, pause, resume, stop, interrupted, silenced, segment_open, segment_close`. 각 이벤트에 `mono_ns`, `wall_ms`, `media_pos_ms`를 기록합니다. *(D20으로 대체됨: 이벤트는 `{type, mono_ns, media_pos_ms}`이고 wall clock은 anchor에만(E15))*
  - `mono_ns = SystemClock.elapsedRealtimeNanos()`입니다. 기기가 절전 중에도 계속 가는 단조 시계입니다.
  - wall clock은 anchor 한 쌍 `(wall_ms, mono_ns)`과 `boot_id`만 기록합니다. *(D20으로 대체됨: `anchors:[{wall_ms, mono_ns}]`는 1개 이상이며, 재부팅 없이 시계가 바뀌면 anchor를 더함(E15))*
- **사진 연결:** 앱 안에서 찍은 사진은 셔터 시점의 `mono_ns`를 기록합니다.
  - 녹음 시작 앵커는 `AudioRecord.getTimestamp`로 얻을 수 있으면 그 값을 씁니다. MediaRecorder를 쓰면 시작 호출 시각과 `start_uncertainty_ms`를 함께 기록하고, 정밀한 척하지 않습니다.
  - 외부 사진은 EXIF 시각을 쓰되 정확도를 낮게 표시합니다.
- **Hub 처리:** Hub는 타임라인으로 세그먼트를 이어 붙여 정규 녹음 타임라인을 만들고, 사진의 `mono_ns`를 `media_time` Locator로 변환합니다.
  - provider가 세그먼트 형식(예: ADTS)을 받지 않으면, 원본은 그대로 두고 FFmpeg로 remux한 파생 파일을 보냅니다.
- **시작과 복구:**
  - 녹음 시작은 항상 포그라운드 Activity에서 사용자가 한 번 탭해서 합니다. 수업 전 알림은 Activity를 직접 여는 PendingIntent입니다.
  - 앱이 다시 시작되면 `open` Capture를 찾아 마지막 세그먼트를 복구하고 봉인한 뒤 "복구된 녹음"으로 표시합니다.
  - 통화·오디오 포커스 상실·마이크 무음은 `interrupted`/`silenced` 이벤트로 기록합니다.

**구현 수단 (미결 O3):** 후보는 셋입니다.
- (a) `MediaRecorder` + `setNextOutputFile`(API 26+)로 MPEG-4 세그먼트를 회전
- (b) `MediaRecorder` `AAC_ADTS` 출력
- (c) `AudioRecord → MediaCodec → ADTS` 직접 프레이밍. 잃는 범위가 수 초로 줄지만 코드가 가장 많습니다.

기본 출발점은 (a)이며, 실기기 측정 결과로 확정합니다.

---

## D16. Hub 프로세스와 백업

**결정:**
- **프로세스 구성:** 단일 Hub 프로세스(FastAPI + asyncio job runner)로 시작합니다.
  - 상주 방식: Windows 로그온 시 Task Scheduler로 시작합니다. 재부팅 후 로그인 전에는 Hub가 없다는 한계를 문서화하고, Windows 서비스로 바꿀지는 미결 O8에서 판단합니다.
  - SQLite는 WAL 모드로 쓰고 ACK 경로는 `synchronous=FULL`입니다. 쓰기는 연결 하나로 직렬화합니다.
  - 기동 시 `sqlite_version() ≥ 3.34`와 FTS5 컴파일 여부를 검사하고, 조건이 맞지 않으면 기동을 거부합니다. *(D21로 대체됨: 하한 3.35(RETURNING)와 fts5·trigram·json_extract·generated column 기능 probe(E2))*
- **외부 도구 격리:** extractor, FFmpeg, LibreOffice는 subprocess로 실행합니다. 실행 시간 제한, Windows Job Object 메모리 상한을 두고, 결과는 stdout JSON으로 받습니다. parser가 크래시하거나 멈춰도 Hub는 살아 있습니다.
- **백업 순서:**
  1. SQLite Online Backup API로 DB 스냅샷을 만듭니다.
  2. 스냅샷이 참조하는 Blob 중 백업 대상에 없는 것만 복사합니다. 내용 주소 저장이라 증분 판단이 자명합니다.
  3. 세대 manifest를 기록합니다.
  4. 해당 Attachment의 `backed_up_at`을 갱신합니다. *(D21로 대체됨: 그 세대가 처음 담은 Blob의 Attachment에만 한 번)*
  - 백업에서 복원하면 새 `epoch`를 발급합니다.
- **무결성 점검:** `hub fsck` 명령으로 해시를 재계산하고 고아·누락 Blob을 탐지합니다.

---

## D17. 계약 패키지는 JSON Schema가 원천이고, golden fixture로 세 언어를 검증한다

**문제:** README에 `packages/domain`과 `packages/api-contracts`가 따로 있었고, 무엇이 원천인지 정해지지 않았습니다.

**검토한 안:**
- A. OpenAPI에서 세 언어 코드를 생성한다.
- B. JSON Schema(+ 이를 참조하는 OpenAPI)를 원천으로 하고, 공유 golden JSON fixture로 세 언어의 직렬화·역직렬화를 CI에서 검증한다. 코드 생성은 선택이다.

**결정:** B. Swift 생성기 품질과 생성 코드 유지 부담이 크고, D7·D10 덕분에 계약 면적이 작습니다.
- `packages/api-contracts/`에 다음을 둡니다.
  - `schemas/`: manifest·commit, Item kind, Locator, Mutation, changes, AnnotationLayer payload, error *(D20으로 대체됨: manifest는 없음(D7), AnnotationLayer payload는 #17)*
  - `openapi.yaml` *(D20으로 대체됨: `openapi/sync.yaml`)*
  - 클라이언트 로컬 sync 테이블 DDL: `entity_cache`, `mutation_outbox`, `blob_transfer`, `meta(cursor, hub_id, epoch)` *(D20으로 대체됨: 클라이언트 DDL은 계약 패키지가 아니라 각 앱 이슈(#3, #16)가 만듦(U1))*
  - `fixtures/`
- 버전 규칙: 경로는 `/v1`입니다. 스키마는 추가하는 변경만 하위 호환으로 허용합니다. Hub는 `GET /v1/hub`로 `min_client_version`을 알립니다. *(D20으로 대체됨: `contract_version`·`min_contract_version` 정수와 요청 헤더 `CampusLife-Contract`(E18), 첫 클라이언트 배포 전까지는 깨는 변경 허용(E21))*

---

## D18. 충돌은 필드 단위 compare-and-set으로 판정하고, 클라이언트에는 병합 로직이 없다

**문제:**
- 문서는 "서로 다른 필드는 자동 병합, 같은 note body는 사용자 선택"을 요구했습니다. 하지만 엔터티 revision 하나(`base_revision`)만으로는 어느 필드가 바뀌었는지 판단할 수 없습니다.
- 삭제 대 편집 충돌, Relation·Tag 같은 집합 연산도 정의되어 있지 않았습니다.

**검토한 안:**
- A. 엔터티 단위 `base_rev`가 다르면 무조건 충돌로 본다. 오탐이 지나치게 많습니다.
- B. Hub가 필드별 rev를 저장한다.
- C. patch가 필드마다 이전 값(긴 텍스트는 이전 값의 해시)을 함께 보내는 compare-and-set.

**결정:** C.
- **Mutation 형식:** `{mutation_id, entity, id, op: create|patch|delete|restore|suggestion.accept|…, set:{field:{from, to}}}`. *(D20으로 대체됨: `op`로 구분하는 union 하나, 결과는 E12)*
  - 긴 텍스트 필드(스키마에 `x-text: true`)는 `from` 대신 `from_hash`를 보냅니다.
- **적용 규칙:** Hub의 현재 값이 `from`과 같으면 그 필드를 적용하고, 다르면 그 필드만 충돌입니다.
  - 스칼라 필드: 같은 필드를 동시에 바꾼 경우만 충돌이고, 나머지 필드는 적용합니다.
  - 텍스트 본문(Note body): 충돌하면 Hub 값을 유지하고, 제출된 값은 `ConflictRecord{entity_ref, field, theirs_rev, mine_value_blob, state}`로 보존합니다. 사용자가 비교 UI에서 고르거나 직접 합친 결과를 일반 patch로 보내면 해소됩니다. 자동 텍스트 병합은 하지 않습니다.
  - Relation·Tag: 각각 독립 행이고 추가·삭제가 멱등이라 충돌이 없습니다. Relation ID는 `"{from}|{type}|{to}"`입니다.
  - delete: 소프트 삭제라 항상 적용됩니다. 삭제된 엔터티에 patch가 오면 거부하되 제출 값은 ConflictRecord로 보존합니다.
- **재전송:** `mutation_id`는 Hub `applied_mutations`의 PK이고 90일 보관합니다. 그보다 오래된 재전송이 와도 CAS가 다시 판정하므로 두 번 적용되지 않습니다. *(D21로 대체됨: create·patch만 그렇습니다. CAS가 없는 delete·restore·Relation create는 90일 넘은 재전송이 다시 적용됩니다(수용))*
- **클라이언트 동작:** 로컬에 낙관적으로 먼저 반영합니다. conflict 응답을 받으면 Hub 상태로 덮어쓰고 ConflictRecord(동기화 대상)를 표시합니다.
- last-write-wins는 어디에도 없습니다. 결과가 같은 값으로 모이는 경우는 충돌이 아닙니다.

**결과:** 충돌 판정 코드는 Hub(Python) 한 곳에만 있고, Hub가 필드별 rev를 저장할 필요도 없습니다.

---

## D19. 알림은 각 클라이언트가 로컬에서 예약한다

**문제:** Reminder가 Hub 엔터티처럼 적혀 있었습니다. 하지만 노트북이 꺼지면 Hub는 동작하지 않고, 푸시 서버(클라우드)도 두지 않으므로 Hub가 폰에 알림을 보낼 방법이 없습니다.

**결정:**
- 알림 시각은 Item 필드 `fields.reminders[]`입니다. 각 클라이언트가 동기화된 사본으로 OS 로컬 알림을 예약합니다.
  - Android: 정확한 시각 알람에는 권한이 필요할 수 있으므로, 권한이 없으면 근사 알람으로 대체합니다.
  - iPad: `UNUserNotificationCenter`로 예약합니다. 앱당 대기 알림 개수 제한을 고려해 가까운 것부터 예약합니다.
- Reminder 엔터티는 없앱니다.

**결과:** 노트북 상태와 관계없이 알림이 울립니다. 단, 마지막 동기화 이후 바뀐 일정은 반영되지 않습니다.

---

## D20. 계약 정리 (2026-11-26)

**문제:** `packages/api-contracts`(#20)를 만들면서 문서끼리, 또는 문서와 구현 가능성이 어긋난 곳이 드러났습니다. 봉투가 두 가지(D7 `{type: capture.commit,…}`, D18 `{mutation_id, entity, id, op, set}`)였고, `id_reuse`가 D4에서는 409, D7에서는 conflict였습니다. D15는 이벤트마다 `wall_ms`를 적으면서 wall clock은 anchor에만 둔다고 했습니다. D13의 annotation box 예시는 모서리 좌표로는 성립하지 않았고, D17은 없앤 manifest를 적고 있었습니다. Item `status?`에는 값 목록이 없었습니다.

**결정:** 스키마가 규범입니다(E22). 필드를 더하거나 바꾸는 PR은 같은 PR에서 DOMAIN-MODEL을 고칩니다. 아래가 이 정리에서 바뀐 것이며, 앞 항목의 해당 문장에 `D20으로 대체됨`을 달았습니다.

- **U1. 언어별 검증:** Kotlin·Swift 왕복 검사는 각 앱 이슈(#3, #16)가 앱 모델과 함께 만듭니다. #20은 Python 왕복, 하네스 프로토콜(`HARNESS.md`), 비교기를 냅니다. macOS runner는 쓰지 않습니다.
- **U2. NFC:** wire의 모든 문자열은 NFC여야 합니다. 아니면 Hub가 `rejected{reason: not_canonical}`으로 거부하고 조용히 고치지 않습니다(조용히 고치면 CAS가 가짜로 충돌합니다). 원문 보존은 파일명만 합니다: Attachment `original_filename_raw`는 받은 원문이 NFC와 다를 때만 둡니다. D11을 대체합니다.
- **U3. `outcome_code`:** `unsafe_input`(위험한 구조, 상한 초과)을 더합니다. 세부 사유는 동기화하지 않는 job error에 남깁니다. D1을 대체합니다.
- **U4. Item `status`:** 공통 열에서 뺍니다. task 상태는 `fields.progress` 하나입니다. D10을 대체합니다.
- **E11. Mutation 봉투:** `op`로 구분하는 union 하나입니다. 공통은 `{mutation_id, op}`입니다.
  - `capture.commit {capture}`, `create {entity, id, data}`, `patch {entity, id, set:{key: {from, to} | {from_hash, to}}}`, `delete`·`restore {entity, id}`, `suggestion.accept {suggestion_id, edits?}`, `suggestion.dismiss {suggestion_id}`. MVP op는 이 일곱 개이고, 뒤의 op(`layer.adopt`은 #17, ConflictRecord 해소는 #18)는 갈래로 더합니다.
  - `set`의 키는 공통 열 이름이나 `fields.<key>`(한 단계)입니다. patch는 `x-owner: user` 열만 쓰고, `fields`를 통째로 쓰지 않습니다. 배열은 통째로 비교·교체하고, `from: null`은 없던 열, `to: null`은 선택 열 삭제입니다.
  - `x-text: true` 열은 `from` 대신 `from_hash`(NFC + LF로 정규화한 UTF-8 바이트의 sha256)를 보냅니다. 빈 문자열도 해시하고, `null`은 열이 없었다는 뜻입니다.
  - `edits`는 patch의 set처럼 키를 쓰고, 값은 제안 대신 쓸 값입니다. 대상은 늘 Item이므로 공통 열 키는 patch처럼 규칙 `owner`·`set_type`으로 검사합니다. `fields.<key>`의 타입과 "제안에 있는 키인가"는 Suggestion을 봐야 하므로 Hub 적용기(#2)가 판정합니다.
  - D7·D10·D18의 봉투를 대체합니다.
- **E12. Mutation 결과:** `{mutation_id, result: applied|duplicate|conflict|rejected|retry|not_attempted, reason?, applied_fields?, conflicted_fields?, conflict_record_ids?, missing_blobs?, original?}`.
  - `reason`은 `id_reuse, not_canonical, owner, schema, deleted, link_target_missing, unsupported_entity`입니다(A4). `id_reuse`(같은 id, 다른 내용)는 rejected입니다.
  - `duplicate`는 이미 처리한 `mutation_id`의 재전송이며 `original`에 처음 결과를 담습니다.
  - 배치는 순서대로 처리하고 첫 `retry` 뒤는 `not_attempted`입니다. 배치가 형식에 맞으면 HTTP 200입니다. HTTP 409는 업로드 세션 같은 단일 자원에만 씁니다.
  - D4·D7의 `id_reuse` 응답과 D18의 결과 표현을 대체합니다.
- **E13. 시간 표기:** `instant`는 정확히 `YYYY-MM-DDTHH:MM:SS.sssZ`(UTC, 밀리초 3자리)입니다. `local_date`는 `{date, tz}`, `local_time_rule`은 `{weekdays(ISO, 월=1), start, end, tz, from_date, until_date}`(end > start)입니다. Item `starts_at`·`due_at`은 `all_day`가 true면 local_date, 아니면 instant입니다. 날짜 부분은 실제 달력 날짜여야 합니다(규칙 `calendar`).
- **E15. 녹음 타임라인:** 이벤트는 `{type, mono_ns, media_pos_ms}`이고 wall clock은 timeline의 `boot_id`, `anchors:[{wall_ms, mono_ns}]`(1개 이상), `start_uncertainty_ms?`에만 둡니다. `mono_ns`는 2^53을 넘을 수 있는 int64입니다. D15의 이벤트 문장을 대체합니다.
- **E17. Locator:** `locator/locator` 하나이고 kind는 `pdf_page, slide, doc_path, sheet_range, media_time, image_region, web_snapshot, document`입니다. 이 계약의 `v`는 1이고, 1 이상으로 늘 수 있습니다.
  - page·slide·shape는 1부터, `block_index`는 0부터 셉니다.
  - `box`는 `[x, y, w, h]`이고, 각 원소는 `[0,1]`, x+w ≤ 1, y+h ≤ 1입니다(규칙 `box_extent`). 표시 방향 기준이며, `image_region`은 EXIF 회전을 적용한 뒤 기준입니다.
  - `media_time`은 Capture 녹음 타임라인 기준 `start_ms` ≤ `end_ms`입니다.
  - kind는 Hub가 보내는 쪽에서 열려 있고 `v`는 1 이상입니다(아래 값을 더하는 규칙). `annotation` kind는 #17이 더합니다. D13의 예시 목록과 box 예시를 대체합니다.
  - evidence Relation의 근거 위치는 `locators: [Locator]`(1개 이상, 중복 없음, Hub 소유)입니다. 수락하면 Hub가 evidence의 anchor(blob 또는 capture)마다 `item:<id> —evidence→ blob:<sha> | capture:<id>`를 하나씩 만들고 locators를 합집합으로 모읍니다. add_relation은 만든 Relation의 locators에 둡니다. *(D21로 대체됨: 만든 Relation이 evidence type이면 anchor가 `to`인 Locator만 넣고, 다른 type은 locators 없이 만듭니다. 근거는 accepted Suggestion의 `evidence`에 남습니다. patch_item은 결과가 키를 하나라도 적용할 때만 근거 Relation을 만듭니다)* 각 locator의 anchor는 `to`와 같고, 다른 알려진 type에는 locators가 없습니다(규칙 `locator_anchor`).
- **토큰 재발급:** 클라이언트 앱이 `POST /v1/devices/me/token`(지금 토큰으로 인증, 본문 없음)으로 시작합니다. 이전 토큰은 새 토큰이 처음 인증에 성공할 때 폐기됩니다. 그 첫 사용이 "새 토큰을 저장했다"는 확인을 대신하고, 그전까지는 두 토큰이 모두 유효합니다. 같은 요청을 다시 보내면 아직 쓰이지 않은 같은 새 토큰을 돌려줍니다. D6의 재발급 문장을 대체합니다.
- **sync 포트:** 경로, 상태 코드, 헤더는 `openapi/sync.yaml`(OpenAPI 3.1)이 적고, 본문·파라미터·헤더 모양은 모두 JSON Schema를 `$ref`합니다.
  - 모든 응답에 `CampusLife-Hub-Id`·`CampusLife-Epoch`이 붙고, 요청은 `CampusLife-Contract`를 보냅니다.
  - 모든 operation이 400을 가지지만 `GET /v1/hub`는 `contract_too_old`를 내지 않습니다. 너무 오래된 클라이언트도 거기서 `min_contract_version`을 읽고 스스로 멈춥니다.
  - 업로드 세션의 키는 (Device, sha256)이고 chunk 번호는 0부터이며, chunk 크기는 세션 응답이 정합니다. 이미 저장된 Blob의 finalize는 세션이 없어도 200입니다.
  - D7·D17의 해당 문장을 대체합니다.

**값을 더하는 규칙 (첫 클라이언트 배포 뒤):**
- 첫 클라이언트 배포(#3 릴리스) 전까지는 깨는 변경을 허용하고, 그 뒤에는 additive-only입니다(E21).
- Hub가 보내는 모양은 안쪽까지 열려 있습니다. 모르는 멤버는 무시하고, 모르는 entity의 행은 건너뜁니다. Suggestion `proposed`도 마찬가지입니다.
- 값이 늘 수 있는 열거값은 `^[a-z][a-z_]*$`인 모르는 값도 받습니다. Relation `type`, Attachment `role`, Capture `origin.type`, changes `entity`, Artifact `artifact_type`, Locator `kind`, Suggestion `action`, 오류 `code`는 `x-open: true`와 알려진 값 `examples`로 표시합니다. Item `kind`도 같은 pattern으로 열려 있고, 모르는 kind는 제목·날짜·본문만으로 렌더링합니다.
- **feed union은 열려 있습니다.** Locator `kind`와 Suggestion `action`은 알려진 값이면 그 갈래로 검사하고, 모르는 값이면 멤버째 받습니다.
  - 모르는 Locator kind는 `blob`이 있으면 문서 수준 근거로, 없으면 "근거 위치 모름"으로 표시합니다. Locator `v`는 1 이상이고 늘 수 있습니다.
  - 모르는 kind도 `blob`(sha256), `capture`(UUID), `box`(정규화 `[x, y, w, h]`(D12·E17), `box_extent` 규칙도 적용)라는 이름은 타입을 지킵니다. 알려진 kind가 쓰는 공유 멤버라서 Locator 최상위가 예약하고, 클라이언트는 `blob`을 문자열로 읽어도 됩니다. 그 밖의 멤버는 열려 있습니다.
  - 모르는 action은 표시만 하고 수락할 수 없게 합니다.
  - 규칙 `locator_anchor`는 **모르는** Relation `type`의 `locators`를 허용합니다(feed 열림 원칙). 알려진 type인지는 Relation `type`의 `examples`로 판정합니다. `x-open` 필드의 `examples`는 문서용이 아니라 그 열린 enum의 알려진 값 목록(규범)입니다. 규칙 `locator_anchor`와 `set_type`이 이것을 읽으므로, 목록을 바꾸면 규칙 판정이 함께 바뀌고 fixture와 변이 시험이 그 목록을 고정합니다.
  - Suggestion `patch_item.proposed.set`의 키는 같은 계약의 열 이름이라 열려 있지 않습니다. 새 열을 제안하는 patch는 계약 버전이 같은 Hub만 보냅니다. 열림 시험의 `FREE_FORM_IN_FEED`가 이 자리를 면제합니다.
  - 그래서 `annotation` Locator(#17)나 새 action은 첫 배포 뒤에도 더할 수 있습니다.
- **입력 union은 Hub가 먼저 받아들이면 갈래를 더할 수 있습니다.** Mutation `op`가 그렇습니다(E11의 `layer.adopt`, ConflictRecord 해소). 클라이언트가 보내는 쪽의 Locator는 닫힌 view(`locator/locator#/$defs/input`, 알려진 kind, `v` = 1)를 씁니다. 이 view는 `kind`와 `v`만 닫으므로, 멤버는 Locator를 싣는 입력 스키마가 닫습니다(지금 Locator를 싣는 Mutation 입력은 없고, 처음 싣는 것은 #17입니다).
- **닫힌 값 타입은 첫 배포 뒤 멤버를 더할 수 없습니다.** 옛 클라이언트가 디코드에 실패하기 때문입니다.
  - 대상: 닫힌 enum(Suggestion `state`, task `progress`, `cloud_policy`, `semester`, Attachment `captured_at_precision` 등)과 닫힌 객체 값(`local_date`, `local_time_rule`).
  - 이런 값을 더하려면 첫 배포 전에 넣거나, 새 필드·새 엔터티로 내거나, 계약 버전(`min_contract_version`)을 올립니다.
- 들어오는 입력(Mutation, pair 요청, status, 업로드 요청, changes 쿼리)은 닫혀 있어 모르는 멤버를 거부합니다. 입력에 멤버를 더하는 것은 Hub가 먼저 받아들인 뒤 클라이언트가 보내는 순서로만 할 수 있습니다.

**결과:**
- 세 언어가 같은 fixture로 같은 판정을 하고, 규칙(`nfc`, `calendar`, `tz_exists`, `range_order`, `box_extent`, `owner`, `set_type`, `locator_anchor`, `a1_order`, `conflict_self` 등)은 Python 검증기 `validate()` 한 곳에 있습니다.
- D1·D3·D4·D6·D7·D8·D9·D10·D11·D12·D13·D15·D17·D18의 해당 문장과 미결 O5는 이 항목을 따릅니다.

---

## D21. Windows Hub 코어 (#2, 2026-11-26 ~ 2026-11-27)

**문제:** Hub 코어(#2)를 PR 여덟 개로 구현하면서 D1–D4·D14·D16·D18이 정하지 않았거나 구현과 맞지 않는 곳이 드러났습니다. 배치와 도구, 기동 검사, 쓰기 직렬화, 변경 피드 저장층, 멱등 단위, 업로드 세션, 작업 큐와 격리, 프로세스 host와 admin 인증, 백업·복원·purge의 범위, 설치가 그렇습니다. 각 결정의 근거와 재현은 #2 본문의 해당 절(괄호 안 이름)에 있습니다.

**결정:** 사용자가 정한 것과 오케스트레이터가 근거로 정한 것을 나눠 적습니다. 오케스트레이터 결정에는 구현자가 제안하고 리뷰가 받아들인 것도 들어 있습니다. 앞 항목에서 이 결정과 어긋나는 문장에는 `D21로 대체됨`을 달았습니다. U·E 번호는 #2 본문의 번호입니다(D20의 같은 번호와 별개).

**사용자 결정**
- **U1. 백업은 암호화하지 않습니다** (2026-11-26, "결정"). 설치·운영 문서(`services/agent/README.md`)와 PRIVACY-SECURITY에 백업 드라이브 분리 보관과 분실 위험을 적습니다.
- **U2. 즉시 purge는 Blob 단위입니다** (2026-11-26, "결정").
  - 그 Blob을 가리키는 모든 Attachment, 거기서 파생된 Artifact·Suggestion(전이 폐쇄), 인덱스·렌디션을 지웁니다.
  - 그 Artifact에서 fork된 Note(사용자 데이터)는 지우지 않고 근거 링크가 사라졌다고 표시합니다. purge 확인 화면에 "이 Note들도 지울까요?"를 함께 보여 줍니다.
  - 백업 세대에는 남아 있음을 알립니다.
- **U3. 백업은 수동입니다** (2026-11-26, "결정"). 로컬 UI와 CLI로 하고, 마지막 백업에서 7일이 지나면 경고하며, 드라이브에는 최근 5세대를 남깁니다. 둘 다 설정값(`serve --backup-warn-days`, `--backup-keep`)입니다.
- **U4. CI는 ubuntu에서 항상, windows-latest에서는 path filter(`services/agent/**`, `packages/api-contracts/**`)로 전체 pytest를 돌립니다** (2026-11-26, "결정").
- **U5. 웹 계층은 D16대로 FastAPI입니다** (2026-11-27, "PR #32 1차 리뷰 반영"). uvicorn은 그대로 쓰고, 계약 검증은 `validate()` 한 곳에 둡니다(FastAPI 모델층으로 두 번째 스키마를 만들지 않음). 자동 OpenAPI 문서(`/docs`, `/redoc`)는 끕니다.
- **U6. 복원하면 Hub 자신을 뺀 active 장치가 모두 dormant가 됩니다** (2026-11-27, "PR #37 1차 리뷰 반영"). D6대로 재승인이 필요하고 데이터·cursor·Outbox는 남습니다. revoked는 revoked로 둡니다. 재승인 흐름은 #19입니다.

**오케스트레이터 결정: 배치와 기동** (2026-11-26, "결정")
- **E1. 배치:** `services/agent/`(패키지 `campuslife_hub`), `.python-version` 3.12, `uv.lock` 커밋. 계약 패키지는 editable path 의존입니다(non-editable이면 패키지 밖 `schemas/`를 찾지 못함). 루트 uv workspace는 쓰지 않습니다(#20의 패키지별 lock·CI와 충돌).
- **E2. SQLite 기동 검사:** 하한 3.35(RETURNING)와 기능 probe(fts5, trigram, json_extract, generated column). 실패하면 이유를 출력하고 기동을 거부합니다. 빌드의 버전 번호만으로는 어떤 모듈을 넣고 빌드했는지 알 수 없어서 실제로 시도해 봅니다. WAL, `synchronous=FULL`은 항상입니다.
- **E3. 마이그레이션:** 번호 붙인 SQL 파일과 `PRAGMA user_version`. DB가 코드보다 새로우면 기동을 거부합니다. alembic·ORM은 쓰지 않습니다.
- **E11. 단일 인스턴스:** `<root>/hub.lock`(`msvcrt.locking`/`fcntl.flock`). 두 번째 실행은 메시지와 함께 0이 아닌 코드로 끝납니다. 쓰기 CLI는 서빙 중인 Hub의 admin API를 거치고, Hub가 없으면 lock을 직접 잡습니다. restore는 채울 빈 root의 lock을 잡습니다.
  - 읽기 명령(`ls`, `fsck`)은 DB에 쓰지 않습니다(2026-11-26, "PR #28 1차"). migrate·신원 발급을 하지 않고, 스키마가 코드와 다르면 거부합니다. 근거: lock을 쥔 옛 Hub가 있는데 새 checkout의 읽기 명령이 DB를 새 버전으로 올리면 옛 Hub가 깨집니다.
- **E23. data root 검사:** 경로와 알려진 동기화 루트 목록을 받는 순수 함수로 판정하고, 동기화 루트 탐지(`OneDrive*` 환경 변수, Dropbox `info.json`)는 얇은 진입부에 둡니다. `\\`로 시작하는 드라이브(UNC 공유 등)도 거부합니다(2026-11-26, "PR #28 2차": SQLite가 file URI의 authority를 거부함). `\\?\C:\…`는 `C:\…`로 판정합니다.
- **E21. 설치:** git checkout + `uv sync --frozen` + venv의 gui-script 런처(pythonw)를 Task Scheduler XML로 등록합니다. 로그온 트리거, `ExecutionTimeLimit PT0S`, 배터리 시작·중지 조건 끔, `MultipleInstancesPolicy IgnoreNew`입니다. MS Store Python(`WindowsApps` 경로)은 `%LOCALAPPDATA%` 쓰기를 가상화하므로 거부합니다.
  - 구현자 결정, PR #38 리뷰에서 받아들임(2026-11-27): 런처는 `campuslife-hubw`, 작업은 이 사용자로 `InteractiveToken`·`LeastPrivilege`, 이름은 `CampusLife Hub`이고 `campuslife-hub autostart xml|register|unregister`가 다룹니다. 작업 인자에는 등록 시점의 data root와 준 serve 옵션만 들어갑니다. Store Python 거부는 기동만이 아니라 모든 CLI 명령에서 합니다(어느 명령이든 data root를 가상화된 곳에 읽고 쓰게 되므로).
  - pythonw로 뜬 Hub에는 stderr가 없으므로 기동 거부 이유를 `<root>/logs/hub.log`에 남깁니다(2026-11-27, PR #38 리뷰, 오케스트레이터 결정). `run_hub`은 로그 파일을 붙인 뒤 Hub를 끝낸 오류를 기록하고, CLI는 stderr가 없을 때 그보다 앞선 실패(Store Python 거부 등)를 같은 파일에 덧붙입니다.
  - 재부팅 뒤 로그인 전에는 Hub가 없다는 D16의 한계는 그대로이고, 알림은 Hub와 상관없습니다(D19). 실기기의 로그온 자동 시작·장시간 생존은 #14가 확인합니다.

**오케스트레이터 결정: 저장과 쓰기 경로** (2026-11-26, "결정")
- **E4. 쓰기 직렬화:** 쓰기 연결 하나를 전용 스레드가 소유하고(`await db.write(fn)` → `BEGIN IMMEDIATE`), 읽기는 별도 연결입니다.
- **E5. 변경 피드 저장층 `store`:** `store.write(tx, ref, data | None) -> rev`가 `change_seq`를 올리고 `sync_index(entity, id, rev UNIQUE, deleted)`를 갱신하는 유일한 함수입니다. Mutation 적용기, 백업(`backed_up_at`), purge, jobs(Artifact·Suggestion, `processing_summary`를 다시 보내는 `store.touch`)가 모두 이것을 부릅니다. 행마다 고유 rev입니다. 근거: 한 트랜잭션의 여러 행이 같은 rev면 changes 페이지 경계에서 행을 건너뜁니다(grilling 재현). tombstone은 purge 뒤에도 남습니다.
- **E6. Mutation 적용기 `apply(batch, device)`:** 출처(sync·로컬 UI·import)와 상관없이 모든 Mutation에 `validate()`를 부릅니다. 규칙 매핑은 `nfc`→`not_canonical`, `owner`→`owner`, 그 밖은 표에 없으면 `schema`입니다. Mutation마다 트랜잭션이고 첫 `retry` 뒤는 `not_attempted`입니다.
  - 위반 우선순위는 schema > owner > not_canonical입니다(2026-11-26, "PR #28 1차", 구현자 결정 받아들임).
  - 적용기가 아직 처리하지 않는 계약 op가 배치에 있으면 아무것도 쓰기 전에 배치 전체를 거부합니다(배치 일부만 commit되고 결과가 사라지지 않게). op가 문자열이 아니거나 계약에 없으면 지금처럼 `rejected{schema}`입니다(2026-11-26, "PR #28 1차·2차").
- **E7. 멱등 단위:** capture.commit·create는 엔터티 id 기준이고, 처음 payload의 `canonical_json` sha256을 행에 저장해 비교합니다(현재 행과 비교하면 뒤의 patch 때문에 거짓 `id_reuse`가 남). mutation_id가 달라도 내용이 같으면 `applied`(no-op)입니다. `from_hash`는 `text_hash()` 하나로 계산합니다.
- **E8. #18 전의 x-text 충돌값:** 제출값을 동기화하지 않는 내부 `conflict_stash`에 보존합니다. #18이 ConflictRecord로 옮깁니다.
- **CAS가 먼저, schema는 마지막** (2026-11-27, "PR #30 1차"): patch는 키마다 CAS를 먼저 판정합니다. `rejected{schema}`는 모든 키의 `from`이 맞는데 결과 행이 계약을 깰 때만입니다. 행 적합성이 함께 걸린 키 묶음(`all_day`·`starts_at`·`due_at`)은 한 단위로 판정하고, 묶음 밖 키는 각자의 CAS대로 적용합니다. 진 키가 있는 patch에서 묶음 밖의 이긴 키가 혼자 행을 깨면 그 키만 충돌로 돌립니다(구현자 결정 받아들임).
- **E15. 로컬 import:** NFC 파일명(다르면 `original_filename_raw`), cloud_policy 해석, Hub 자신의 `windows_hub` Device 행으로 payload를 만들고 같은 `apply()`를 부릅니다(#20 A7). import 한 번이 Capture 하나입니다. 파일명이 UTF-8로 인코딩되지 않으면 Blob을 저장하기 전에 그 파일을 거부합니다(2026-11-26, "PR #28 1차").
- **E22.** `suggestion.accept`·`dismiss`는 이 적용기가 구현합니다(판정 지점 하나). `edits`의 `fields.<key>`가 kind 스키마에 맞는지, 제안에 있는 키인지는 적용기가 Suggestion을 읽고 판정해 `rejected{schema}`로 답합니다.

**적용기 판정 (받아들인 구현자 결정)** — 클라이언트가 결과로 보는 것
- Relation의 끝이나 commit `links[]`의 대상이 없거나 tombstone이면 `rejected{link_target_missing}`입니다. soft-delete된 Item은 있는 것으로 봅니다(2026-11-26 "PR #28 1차", Relation create까지 넓힌 것은 2026-11-27 "PR #30 1차").
- 계약이 fields를 정하지 않은 kind의 Item create는 `rejected{schema}`입니다(2026-11-27, "PR3(#30) 범위 메모"에서 제안, "PR #30 1차"에서 받아들임). 이 아래 넷의 출처도 같습니다.
- 지울 것이 없는 delete(이미 지워졌거나 없는 대상)는 `applied`입니다. 대상은 결국 없는 상태가 됩니다.
- 없거나 soft-delete된 Item에 온 patch는 `rejected{deleted}`이고 제출값은 `conflict_stash`에 남깁니다. 없는 Item의 restore도 `rejected{deleted}`이며, 남길 값이 없으므로 stash하지 않습니다.
- Tag를 지우면 그 Tag를 끝으로 가진 Relation도 tombstone이 됩니다.
- 이미 `to` 값을 가진 키는 수렴으로 보고 `applied`로 답하며 아무것도 쓰지 않습니다(D18).
- 한 commit 안에서 Attachment id가 겹치면 `rejected{schema}`입니다(2026-11-26, "PR #28 1차").
- 로컬 import가 링크한 과목의 기본 `cloud_policy`가 여럿이면 가장 엄격한 값(min)을 씁니다(2026-11-26, "PR #28 1차", D8).
- 이미 저장된 Blob을 다른 size로 열면 업로드를 거부합니다. HTTP에서는 400 `invalid_request`이고 매핑은 #4가 합니다(2026-11-26, "PR #29 1차"·"#20 종료 뒤 반영" E10).

**오케스트레이터 결정: Blob과 업로드** (2026-11-26, "결정"·"PR #29 1차"·"PR3 첫 할 일")
- **E9. Blob 저장:** `staging/`(같은 볼륨)에 쓰기 → fsync → sha256 → 최종 경로가 이미 있으면 staging만 버리고, 없으면 `os.replace` → 읽기 전용 → `blobs` 행. Windows 잠금(WinError 5/32)은 상한 있는 재시도입니다. 기동 때 lock을 쥔 쪽이 `staging/`의 잔여물을 지우고, 지우지 못한 것은 기록하고 건너뜁니다.
- **E10. 업로드 세션:** 키는 `(device_id, sha256)`, 디렉터리는 `uploads/<session uuid>/<index>`입니다. 같은 sha의 중복 제거는 finalize에서 합니다. revoke하면 그 장치의 세션을 지웁니다. 7일 동안 건드리지 않은 세션과, 어떤 참조도 없고 마지막으로 받은 지 7일이 지난 Blob은 정리합니다. 참조 테이블 목록은 `BLOB_REFERENCES` 한 곳입니다(Attachment, Artifact, Relation 끝; 뒤에 AnnotationLayer).
  - 세션을 열 때 `2 × size`가 data root 볼륨의 남은 공간보다 크면 `invalid_request`입니다. 근거: 아무 size나 받으면 `size=2**63-1` 한 번으로 finalize가 `MemoryError`를 냅니다. finalize는 chunk를 둔 채 결합본을 써서 약 두 배가 필요합니다.
  - 같은 index의 chunk는 배타적으로 만듭니다. 같은 바이트면 no-op, 다르면 `chunk_mismatch`입니다. "이미 저장됨"(200)을 답할 때도 7일 시계를 다시 시작합니다.

**오케스트레이터 결정: 작업 큐와 격리** (2026-11-26 "결정", 2026-11-27 "PR #31 1차"·"PR5 첫 할 일")
- **E12. jobs:** 상태는 D1의 일곱 개, `outcome_code`는 `unsafe_input`을 더한 여덟 개입니다. `enqueue(tx, JobSpec)`이 handler 설정과 합쳐 job_key를 내부에서 계산합니다. 허용 전이는 표 하나이고 `UPDATE … WHERE state IN (이전 상태)`로 적용해, 취소된 job을 끝난 handler가 덮지 않습니다. 시도는 시작할 때 세고 backoff는 30초·2분·10분·1시간, 최대 5회입니다. 기동 때 running은 retry_wait(상한을 넘으면 failed)로 돌립니다.
  - runner는 죽지 않습니다: 계약 밖 결과, 기록 실패, handler 예외, handler 시간 상한(기본 30분) 초과는 모두 defer로 처리합니다. 취소를 무시하는 handler는 배경에 두고 다음 job으로 가며, 그 job은 다시 claim하지 않습니다.
- **E13. `isolate.run`:** Windows는 ctypes Job Object(`KILL_ON_JOB_CLOSE`, 메모리 상한), POSIX는 `setrlimit`입니다. stdout도 상한(기본 16 MiB)을 넘으면 트리를 죽입니다. 근거: 메모리 상한 256 MiB인 자식이 끝없이 쓰면 Hub RSS가 1.8 GiB까지 올랐습니다(재현). 세션·Job 밖으로 나간 프로세스는 죽이지 못합니다.
- **E14. 초기 job:** `capture.commit` 트랜잭션은 `capture.ingest` job 하나만 등록하고, 그 handler가 트랜잭션 밖에서 #5·#8의 planner로 fan-out합니다(남의 코드 실패가 ACK를 깨지 않게).
  - planner마다 spec 검사(저장 경로와 같은 직렬화)를 그 planner 안에서 합니다. 예외를 냈거나, handler가 없는 job, 저장할 수 없는 job, Hub에 한 번도 없던 subject의 job을 낸 planner는 자기 job만 빠지고 ingest는 `PlanningFailed`로 다시 시도합니다(2026-11-27, "PR #31 1차"·"PR8 첫 할 일").
    - "한 번도 없던 subject"는 행도 tombstone도 없는 엔터티이고, Blob이면 파일도 없고 그 Blob을 지운 purge 기록도 없는 것입니다. 그런 spec을 하나라도 낸 planner는 job 전부가 빠집니다(구현자 결정, PR #38 리뷰에서 받아들임, 2026-11-27). 참조 없는 Blob 정리는 기록을 남기지 않으므로 그렇게 지운 Blob도 여기에 듭니다.
  - 계획하는 동안 purge가 subject(tombstone이 남음)나 입력 Blob을 지운 spec은 버리고 warning으로 남깁니다(2026-11-27, "PR #37 2차"·"PR8 첫 할 일").

**오케스트레이터 결정: host와 admin** (2026-11-26 "결정", 2026-11-27 "PR #32 1차·2차"·"PR6 첫 할 일")
- **E17. 프로세스 host `serve`:** admin·로컬 UI 리스너와 job runner를 한 프로세스에서 돌리고, 1시간마다 세션 만료·Blob 정리·Mutation 결과 정리·끝나지 않은 purge 마무리를 합니다. sync 리스너는 #4가 `create_sync_app`으로 끼웁니다. 포트는 설정값입니다(admin 8731, sync 8730).
- **E18. admin 인증:** DPAPI로 보호한 admin 토큰(POSIX 시험 환경은 0600 파일), CLI가 여는 1회용 로그인 URL, HttpOnly·SameSite=Strict 쿠키, Host·Origin 검사(DNS rebinding)입니다. 출발지 주소로 인가하지 않습니다. 원본은 `Content-Disposition: attachment`, `nosniff`, `CSP: sandbox`로 보냅니다.
  - CLI는 토큰을 보내기 전에 Hub가 자기를 증명하게 합니다(nonce에 대한 토큰 키 HMAC). 증명과 요청 서명에는 대상 Host(`127.0.0.1:<port>`)가 들어가고 Hub는 자기 실제 listen 주소로 검증합니다. 근거: 남은 `admin.port`를 믿으면 그 포트의 아무 프로세스에나 토큰을 보내고(재현), Host를 묶지 않으면 중계 가짜가 진짜 Hub의 로그인 코드를 씁니다(재현).
  - lock을 잡은 Hub는 즉시 옛 `admin.port`를 지우고, CLI는 lock을 쥔 채 stale `admin.port`를 지웁니다. 인증 전 경로의 본문은 1 KiB로 제한합니다.
- **E19. 로그 redaction:** 등록된 비밀 값(admin 토큰, #19가 장치 토큰·pairing secret 추가)과 강의 본문 필드를 로그에서 가립니다.

**오케스트레이터 결정: Artifact와 Suggestion** (2026-11-27, "PR6(#36) 계약 어긋남 판정"·"PR #36 1차")
- add_relation을 수락할 때 만든 Relation이 evidence type이면 anchor가 `to`인 Locator만 그 locators에 넣고, 다른 type은 locators 없이 만듭니다. 근거는 accepted Suggestion의 `evidence`에 그대로 남습니다. 근거: 검증기(`relation.json`, 규칙 `locator_anchor`)가 강제하는 쪽이고 되돌리기 쉽습니다.
- patch_item은 결과가 키를 하나라도 적용할 때만 근거 Relation을 만듭니다(`applied_fields`가 빈 `conflict`면 만들지 않음).
- 옛 Artifact의 재실행은 더 나중에 만든 Artifact의 open Suggestion을 덮지 않습니다. `propose`는 x-text 열을 LF로 바꿔 저장하고, Hub에 없는 anchor의 제안은 거부합니다. 근거 Locator는 합칠 때 중복을 거릅니다.

**오케스트레이터 결정: 백업·복원·purge·fsck** (2026-11-26 "결정", 2026-11-27 "PR #37 1차·2차")
- **E20. 백업:** SQLite Online Backup API 스냅샷, 참조 Blob(Artifact 포함) 증분 복사, 세대 manifest(마지막에 씀), `backed_up_at`을 `store.write`로 찍기 순서입니다. 백업은 스냅샷부터 끝까지 Blob 제거를 막습니다.
  - 백업 폴더에는 첫 백업 때 marker `campuslife-backup.json`(hub_id)을 둡니다. marker 없이 `generations/`·`blobs/`가 있거나 다른 hub_id면 거부하고, data root와 백업 폴더가 어느 방향으로든 서로를 담으면 거부합니다. prune은 자기가 쓴 이름의 파일만 지웁니다. 근거: 남의 파일과 data root 안의 live DB까지 지웠습니다(재현).
  - Hub가 꺼진 CLI `backup`은 `--keep`을 주지 않으면 prune하지 않습니다. 유지 세대 수를 data root에 저장하지 않아서 사용자가 정한 세대를 기본값 5로 지울 수 있기 때문입니다.
  - 큰 첫 백업이 CLI 요청 시간(600초)을 넘을 수 있습니다. 배경 작업으로 바꾸지 않고 문서에 적습니다.
- **복원:** 빈 data root에, 그 lock을 잡은 뒤 비었는지 다시 확인하고 합니다. 스냅샷이 DB 자리에 놓이기 전에 `.part`에서 새 epoch(UUIDv4)를 쓰고 장치를 dormant로 바꿉니다(U6). 그래서 놓인 DB는 언제나 새 epoch입니다(D3). 근거: 놓은 뒤 크래시하면 옛 epoch로 serve했습니다(재현).
  - 세대의 Blob 하나라도 없거나 손상됐으면 전체를 거부합니다. `--allow-missing`은 만들지 않습니다(부분 복원은 조용한 데이터 손실). 다른 세대·다른 드라이브를 씁니다.
  - DB를 놓은 뒤 기록 전에 크래시하면 그 세대의 `backups` 행과 `backed_up_at`이 빠집니다. 다음 백업이 다시 찍고 데이터는 잃지 않으므로 받아들입니다.
  - 복원 안내는 "모든 장치를 다시 승인, 분실·revoke한 장치는 승인하지 말 것"과 "백업 뒤에 한 purge는 되돌려졌으니 다시 purge"를 출력합니다.
- **E16. purge:** durable `purge_journal`과 `purge.register(name, fn)` 훅입니다. 행 삭제와 journal 행을 한 트랜잭션에 commit하고, 그 뒤 훅과 파일 삭제를 멱등으로 실행하며, 기동 때 끝나지 않은 것을 재개합니다(FTS는 별도 DB라 한 트랜잭션에 묶을 수 없음).
  - 지워지는 Blob·Attachment·Artifact·Suggestion과 고른 fork Note를 끝으로 가진 Relation도 함께 지웁니다(2026-11-27, "PR #36 1차" 경계 메모·"PR7 첫 할 일").
  - 폐쇄는 실제로 지워질 content Blob만 따라가는 고정점입니다. 남는 계보와 바이트를 공유하는 Artifact에서 만든 것은 남습니다.
  - 지워지는 것을 subject로 가진 job과, 지워지는 Blob을 입력으로 가진 job은 취소합니다. `capture.ingest`는 취소하지 않습니다. 실행할 때 live 상태를 다시 읽고, Attachment 없는 Capture도 ingest 대상이기 때문입니다.
  - fork Note의 표시는 hub 소유 계약 열 `evidence_purged_at`(purge 시각)입니다. 확인 화면에서 고른 fork Note는 휴지통 없이 tombstone이 됩니다. 수락으로 생긴 Item은 표시 없이 근거 Relation만 잃습니다(셋 다 구현자 판단, 2026-11-27 "PR #37 1차"에서 받아들임).
  - purge 뒤에 오프라인 fork Note가 create로 오면 Hub가 `evidence_purged_at`을 찍습니다(2026-11-27, "PR #37 1차" 6, 오케스트레이터 결정).
- **fsck:** 해시를 다시 계산하고 missing·corrupt·orphan Blob과 끝나지 않은 purge를 보고합니다. 잠그지 않으므로 Hub가 도는 동안에는 그 사이 저장·정리되는 Blob이 거짓 양성으로 보일 수 있습니다.

**보존**
- `applied_mutations`는 90일 보관합니다(E6). create·patch는 그보다 오래된 재전송도 CAS·내용 비교로 다시 판정되어 해가 없습니다. **delete·restore·Relation create는 CAS가 없어서 90일 넘은 재전송이 다시 적용됩니다.** 단일 사용자 Hub에서 90일 넘게 보내지 못한 Outbox는 드물어 받아들입니다(2026-11-27, "PR #30 1차").
- **`backed_up_at`은 그 Blob을 처음 담은 세대가 끝날 때 한 번 찍습니다** (2026-11-27, "PR #37 1차", 구현자 결정 받아들임). 뒤 세대도 무엇이 가리키는 동안은 그 Blob을 담기 때문입니다.
- 업로드 세션 7일, 참조 없는 Blob 7일, tombstone 무기한, 백업 세대는 드라이브마다 최근 N개(기본 5), `logs/hub.log`는 지금 파일과 이전 파일 5개(각 5 MiB, 최대 30 MiB)입니다.

**클라이언트 규칙(#3·#16이 구현)**
- `starts_at`·`due_at` patch에는 `all_day`를 함께 싣습니다(2026-11-27, "PR4 첫 할 일"). 싣지 않으면 그사이 바뀐 `all_day`를 CAS가 보지 못해, `from`이 다 맞는데 행이 깨지는 patch가 `conflict`가 아니라 `rejected{schema}`가 됩니다.
- 복원 뒤에는 epoch가 바뀌고(D3) 장치가 dormant가 됩니다(U6). 앱은 재승인을 안내합니다(#19).

**결과:**
- Hub 코어는 `services/agent`에 있고, 설치·업데이트·제거와 운영은 `services/agent/README.md`가 적습니다.
- `change_seq`를 쓰는 곳(Mutation, 백업, purge, jobs)은 모두 `store.write` 하나를 거칩니다.
- D2·D6·D14·D16·D18·D20의 해당 문장은 이 항목을 따릅니다. 범위 밖은 #4(sync HTTP), #19(pairing·토큰·재승인), #18(ConflictRecord·휴지통), #5·#8(extractor·provider), #11(FTS), #9(Suggestion 화면), #14(실기기 확인)입니다.

---

## 미결 결정

모든 수치 기준은 제안값이며, 측정은 합성·공개 데이터로 로컬에서 합니다.

| # | 질문 | 판단 기준 |
|---|---|---|
| O1 | 로컬 STT(faster-whisper, whisper.cpp 등)를 MVP에 넣을지 | 대상 노트북에서 60분 한국어 강의 음원 처리 시간 ≤ 녹음 길이, cloud STT 대비 CER 차이 ≤ 5%p면 `local_only` 기본 provider로 탑재 |
| O2 | 한국어 전문 검색 분석기 | 분석기는 `TextAnalyzer` 인터페이스 뒤에 두고 인덱스에 `analyzer_id@version`을 기록, 바꾸면 전체 재색인. 후보: `unicode61` / `trigram` + 3글자 미만 LIKE / 한글 겹침 2-gram 선토큰화 + `unicode61` / 형태소 분석. 합성 코퍼스로 2음절 질의·조사 결합·영한 혼합의 recall@10, p95 응답 시간, 인덱스 크기 측정. `trigram`은 3글자 미만 MATCH가 걸리지 않으므로 단독 사용 불가 |
| O3 | Android 녹음 구현 수단(D15 a/b/c)과 세그먼트 길이 | 강제 종료·전원 차단 후 복구율, 세그먼트 경계 공백 ≤ 100 ms, 60분 배터리·CPU, 접힘·화면 꺼짐·Samsung 절전에서 FGS 생존 |
| O4 | HWP 5 변환 도구, Office → PDF 렌디션 도구 | 합성·공개 fixture에서 표·수식 보존률, 라이선스, Windows 설치 난이도 |
| O5 | `cloud_redacted` 도입 | 한국어 PII(학번, 전화, 이름) 탐지 재현율 ≥ 0.98일 때만 값 추가. 닫힌 enum이므로 첫 배포 뒤에는 계약 버전을 올려야 함(D20) |
| O6 | Hub가 켜진 뒤 업로드가 시작되기까지의 지연, 푸시 깨우기 필요성 | Doze 포함 지연 중앙값이 15분을 넘으면 FCM/APNs 깨우기 검토(외부 의존·비용 평가 포함) |
| O7 | iPad 배포·서명 | 무료 Apple 계정의 프로비저닝 유효기간, App Group·Background Modes·Share Extension 사용 가능 여부. #16 착수 전에 배포 방식 확정 |
| O8 | `tailscale serve` 대용량 전송, chunk 크기, Hub 상주 방식 | LTE·DERP relay 경유 4/8/16 MiB chunk 업로드의 실패율 < 1%와 재개 정상 여부, 절전 복귀 후 serve·인증서 유지. 실패 시 D5 대체안(C) 사용 |
