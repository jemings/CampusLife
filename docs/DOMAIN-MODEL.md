# CampusLife 공통 도메인 모델

구조 결정의 근거는 [DECISIONS.md](DECISIONS.md)에 있습니다. 이 문서는 개념 모델이며, 실제 스키마는 `packages/api-contracts`의 JSON Schema가 기준입니다(D20). 필드 이름이 다르면 스키마를 따르고, 스키마를 바꾸는 PR이 이 문서도 함께 고칩니다.

## 1. 핵심 축

세 축으로 모든 데이터를 설명합니다(D2).

- **Blob — bytes의 정체성:** sha256으로 식별되는 불변 원본. 같은 내용은 한 번만 저장합니다.
- **Capture — 어떻게 들어왔나:** 한 번의 수집 행위가 만든 봉인된 불변 묶음. Attachment(Capture가 Blob을 포함했다는 사실)를 1..N개 가집니다.
- **Item + Relation — 무엇에 관한 것인가:** 사용자가 확정한 의미 있는 대상(과목, 강의, 과제, 공지, 메모 등)과 그 사이의 연결.

여기에 파이프라인 산출물이 더해집니다.

- **Artifact:** transcript, OCR, 요약, 렌디션, overlay 등 불변 파생 결과(D9)
- **Suggestion:** AI가 낸 제안. 엔터티가 아니며 사용자가 수락해야 Item·Relation이 됩니다(D10)
- **ProcessingJob:** 비동기 처리 작업. 처리 상태는 여기에만 있습니다(D1)

ID는 생성한 쪽이 만드는 UUIDv4이며 그 ID가 곧 멱등 키입니다(D4).

## 2. 엔터티

### 수집 (불변)

| Entity | 주요 필드 |
|---|---|
| Blob | sha256, size_bytes, stored_at |
| Capture | id, device_id(Hub가 토큰으로 채움), created_at(instant), origin{type: record\|camera\|share\|import\|manual, uri?, app?, publisher?, distributed_by?, distributed_at?}, category_hint?, user_note?, url?(`http(s)://`, 공백·제어문자 없음), timeline?{boot_id(1–64자), anchors[](1개 이상), events[], start_uncertainty_ms?} |
| Attachment | id, capture_id(Hub), blob_sha256(Hub), role: audio_segment\|photo\|file\|web_snapshot\|text, seq?, original_filename?(NFC), original_filename_raw?(받은 원문이 NFC와 다를 때만), declared_mime?, detected_mime?(Hub), captured_at?, captured_at_precision?: exact\|low, captured_mono_ns?, cloud_policy(사용자), backed_up_at?(Hub) |

- `capture.commit`은 `links[]`를 함께 보내지만 Capture 행에는 없습니다. Hub가 commit을 적용할 때 각 link를 확정 Relation으로 바꿉니다. Attachment가 있으면 Attachment마다 `attachment:<id> —type→ <to>`, 없으면 `capture:<id> —type→ <to>`입니다(A5).
- origin `type`과 Attachment `role`은 Hub가 보내는 쪽에서 열린 값입니다. 클라이언트는 모르는 값도 보존하고, 보낼 때는 알려진 값만 씁니다.

### 사용자 데이터 (Mutation으로만 변경)

| Entity | 주요 필드 |
|---|---|
| Item | id, kind, schema_version(≥ 1), title, body?, starts_at?, due_at?, all_day?, fields{…}, rev(≥ 0), deleted_at? |
| Relation | id = `"{from}\|{type}\|{to}"`, from, type, to, locators[]?(Hub) |
| Tag | id, name, color(`#rrggbb`) |
| AnnotationLayer | id, target_blob_sha256, page_number, author_device_id, rev, drawing_blob, strokes_blob, page_box, conflict_of? |
| ConflictRecord | id, entity_ref, field, theirs_rev, mine_value_blob, state |

- Item에는 `status` 열이 없습니다. task의 상태는 `fields.progress` 하나입니다(U4).
- Relation `type`은 `about, supersedes, rendition_of, evidence, tagged, conflicts_with`이며 Hub가 보내는 쪽에서 열린 값입니다. Tag 부여는 `item:<id>` → `tag:<id>`인 `tagged` Relation입니다.
- Relation `locators`는 사용자가 Suggestion을 수락할 때 Hub가 그 evidence에서 복사하는 근거 위치들입니다(D10). evidence Relation에만 있고, 각 Locator의 anchor(blob 또는 capture)는 `to`와 같습니다. 모르는 Relation type의 locators는 막지 않습니다(알려진 type은 Relation `type`의 `examples`, 곧 알려진 값 목록으로 판정합니다). 한 원본의 여러 곳(예: p.2와 p.5)은 Relation 하나의 locators에 함께 담깁니다. 클라이언트는 쓰지 않습니다.
- AnnotationLayer(#17)와 ConflictRecord(#18)의 스키마는 각 이슈가 추가합니다(E20).

### Hub 파생 (읽기 전용으로 동기화)

| Entity | 주요 필드 |
|---|---|
| Artifact | id, artifact_type(transcript\|ocr\|summary\|rendition\|overlay, 열린 값), subject_ref, job_key, provider?, model?, prompt_version?, pipeline_version, input_hashes[], content_blob, created_at |
| Suggestion | id, source_artifact_id, action: create_item\|patch_item\|add_relation(열린 값), proposed{…}, evidence[](Locator), fingerprint, state, conflicts_with[]?(자기 id 제외) |
| ProcessingJob | id, job_key, job_type, subject_ref, state, outcome_code?, attempts, next_attempt_at, error (상세는 동기화하지 않고 `processing_summary`만 노출) |
| ContentBlock | id = hash(blob, extractor_id, extractor_version, ordinal), blob_sha256, block_type, text 또는 payload, locator (동기화하지 않음) |

- Artifact의 provider·model·prompt_version은 모델을 쓴 job에만 있습니다(렌디션에는 없음). 본문 구조(`blocks`, `refs`)와 `processing_summary`의 모양은 #8이 정합니다(E20). 유효 `cloud_policy`는 저장하지 않고 호출 때 계산합니다(D8).
- Suggestion `proposed`의 모양은 action을 따릅니다. create_item은 Item create의 data, patch_item은 `{entity: item, id, set}`, add_relation은 Relation이며, Hub가 보내는 모양이라 안쪽도 열려 있습니다. 다만 patch_item의 `set` 키는 같은 계약의 열 이름이라 열려 있지 않습니다(새 열을 제안하는 patch는 계약 버전이 같은 Hub만 보냅니다). 모르는 action은 표시만 하고 수락할 수 없습니다.
- `suggestion.accept`의 `edits`는 patch의 set처럼 키를 쓰고, 값은 제안 대신 쓸 값입니다. 공통 열 키는 계약 검증기가 owner·set_type으로 검사하고, `fields.<key>`와 제안에 있는 키인지는 Hub 적용기(#2)가 판정합니다.
- 수락하면 Hub가 evidence의 anchor마다 evidence Relation을 하나씩 만들고 locators를 합집합으로 모읍니다. add_relation은 만든 Relation의 locators에 evidence를 둡니다.

### 시스템

| Entity | 주요 필드 |
|---|---|
| Device | id, platform(android\|ipados\|windows_hub), device_name, app_version, state(active\|dormant\|revoked), created_at, last_seen_at |
| audit_log | 외부 AI 전송, 삭제, revoke, 충돌 해소 기록 (Hub 전용, 동기화하지 않음) |

- Hub는 토큰의 SHA-256만 저장합니다. 이 값(예전 표의 `token_hash`)은 Hub DB에만 있고 wire에 나가지 않습니다(D6). 계약의 Device 모양은 pair 요청·응답과 status heartbeat뿐입니다(E20).
- `windows_hub`는 Hub 자신의 Device 행이고 로컬 import가 씁니다. pair하는 장치는 `android`·`ipados`입니다.

## 3. Item kind

| kind | 주요 fields | 비고 |
|---|---|---|
| term | year, semester(first\|summer\|second\|winter), starts_on, ends_on | MVP |
| course | instructor?, room?, credits?, schedule_slots[]?(local_time_rule), default_cloud_policy?, confirm_each_send? | MVP |
| lecture_session | course_id, date(local_date) | 녹음 시작 시 생성 |
| assignment | course_id, due_text, submission | MVP |
| exam | course_id, scope_text | MVP |
| task | progress(inbox/planned/in_progress/blocked/completed/cancelled), reminders[] | MVP |
| note | forked_from_artifact? (`refs[]`는 #8이 추가, E20) | MVP |
| event, announcement, opportunity, organization, place, person | kind별 스키마 | 후속 |

- 일정 로직에 필요한 날짜(`starts_at`, `due_at`, `all_day`)는 공통 열입니다. `all_day`가 true면 날짜는 `local_date`, 아니면 `instant`입니다. 상태 공통 열은 없습니다(U4).
- `kind`는 `^[a-z][a-z_]*$`인 열린 값입니다.
- 클라이언트는 모르는 kind를 제목·날짜·본문만으로 렌더링합니다.
- 성적, 주민등록번호, 금융정보 등 불필요하게 민감한 데이터는 기본 수집 대상이 아닙니다. 추가할 때는 별도 보호 정책과 사용자의 명시적 동의가 먼저입니다.

## 4. 통합 Inbox 흐름

모든 외부 입력은 먼저 Capture로 들어옵니다. AI 분류는 Suggestion을 만들 뿐 엔터티를 만들지 않습니다.

- 강의 녹음 + 판서 사진 → Capture 1개(녹음 세그먼트 + 사진 Attachment). 녹음 화면에서 고른 과목·강의는 `links[]`로 즉시 확정 Relation이 됩니다.
- 학교 행사 공지 PDF → Capture → 추출 Artifact → `create_item(kind=event)` Suggestion → 사용자가 수락하면 Item과 근거 Relation이 생깁니다.
- 장학금 웹 링크 → Capture(URL + 스냅샷 Blob) → `opportunity` Suggestion
- 개인 음성 메모 → Capture → `note`·`task` Suggestion
- 교수자 자료 v2 재배포 → 새 Capture·새 Blob → `supersedes` Relation Suggestion

기각된 Suggestion은 fingerprint로 기억하므로 재처리해도 다시 열리지 않습니다.

## 5. 상태 — 소유자별 분리 (D1)

| 상태 | 소유자 | 값 |
|---|---|---|
| Capture 봉인 | 클라이언트 로컬 | open, sealed |
| 전송 | 클라이언트 로컬 | pending, uploading, committed, retry_wait, needs_attention |
| ProcessingJob.state | Hub | pending, running, retry_wait, succeeded, failed, blocked, cancelled |
| ProcessingJob.outcome_code | Hub | unsupported, password_required, corrupt, needs_conversion, partial, policy_blocked, budget_blocked, unsafe_input |
| processing_summary | Hub (계산값) | job 집계 |
| Suggestion.state | 사용자가 결정, Hub가 기록 | open, accepted, dismissed, superseded |
| task.progress | 사용자 | inbox, planned, in_progress, blocked, completed, cancelled |

Capture와 Attachment에는 처리 상태 필드가 없습니다. `unsafe_input`은 위험한 구조나 상한을 넘는 입력이며, 세부 사유는 동기화하지 않는 job error에 남깁니다(U3).

## 6. 시간 (D11)

| 타입 | 용도 |
|---|---|
| `instant` (UTC, RFC 3339 `Z`) | 생성·수정·촬영·녹음 등 이미 일어난 일 |
| `local_date` (+ IANA tz) | 종일 일정, 날짜 마감 |
| `local_time_rule` (`HH:MM` + IANA tz + 요일·기간) | 시간표 반복. 표시할 때 전개 |

- 사용자 표시 시간대는 기본 Asia/Seoul이며 설정할 수 있습니다.
- 마감 원문 표현은 `fields.due_text`에 보존합니다.
- wire의 모든 문자열은 NFC여야 합니다. 아니면 Hub가 `rejected{not_canonical}`로 거부하고 조용히 고치지 않습니다. 원문 보존은 파일명만 합니다(`original_filename_raw`, U2).
- `instant`는 `YYYY-MM-DDTHH:MM:SS.sssZ`(밀리초 3자리), `local_date`는 `{date, tz}`, `local_time_rule`은 `{weekdays(ISO, 월=1), start, end, tz, from_date, until_date}`입니다(E13).
- 녹음 중 사진은 단조 시계(`elapsedRealtimeNanos`) 기준으로 녹음과 연결합니다(D15).

## 7. 출처 (D13)

- 모든 Artifact 본문은 `blocks[{text, refs}]` 구조이고, ref는 공통 Locator를 가리킵니다. 본문 스키마는 #8이 정합니다(E20).
- Locator(`locator/locator`)의 kind는 `pdf_page, slide, doc_path, sheet_range, media_time, image_region, web_snapshot, document`입니다. kind는 Hub가 보내는 쪽에서 열린 값이고, 모르는 kind는 `blob`이 있으면 문서 수준 근거로, 없으면 "근거 위치 모름"으로 표시합니다. 이 계약의 `v`는 1이고, page·slide·shape는 1부터, `block_index`는 0부터 셉니다. `box`는 표시 방향 기준 `[x, y, w, h]`입니다(E17). `annotation` kind는 #17이 추가합니다. 모르는 kind도 `blob`(sha256), `capture`(UUID), `box`(정규화 `[x, y, w, h]`(D12·E17), `box_extent` 규칙도 적용)는 타입을 지킵니다. 시트 이름의 31자 한도는 코드 포인트 기준이고, 스프레드시트의 한도는 UTF-16 31단위입니다.
- 앵커는 Blob 해시입니다(녹음은 Capture 타임라인, 필기는 레이어 revision).
- LLM은 입력 ContentBlock id로만 인용하고, Hub가 그 id를 검증해 Locator로 치환합니다.
- 정확한 위치가 없으면 `document`를 쓰고 추정하지 않습니다.

## 8. AI 산출물과 사용자 편집 (D9)

- Artifact는 생성 후 바뀌지 않습니다. 재실행은 새 Artifact를 만들고, Hub가 `current` 포인터를 옮깁니다.
- 사용자가 AI 노트를 고치면 `note` Item으로 fork되고, 이후 편집은 Note의 Mutation입니다.
- Artifact는 기록된 입력으로 재실행할 수 있지만 결과가 같다고 보장하지 않으므로 백업 대상입니다.

## 9. Annotation (D12)

- `AnnotationLayer(target_blob, page_number, author_device)`는 작성자가 하나입니다. 화면에서는 같은 (Blob, page)의 모든 레이어를 겹쳐 그립니다.
- payload: `drawing.pkd`(PencilKit 원본, iPad 편집용) + `strokes.v1.json`(표준, 정규화 좌표)
- overlay PDF는 Hub가 strokes로 렌더링하는 Artifact입니다.
- Office·HWP 문서의 필기는 PDF 렌디션 Blob에 고정되고, 그 렌디션은 pin됩니다.

## 10. 동기화와 충돌

- 변경 피드: `GET /v1/changes?since=` → 바뀐 행의 현재 상태와 `hub_id`·`epoch`(D3)
  - 행은 `{entity, id, rev, deleted, data}`입니다. entity는 capture, attachment, item, relation, tag, artifact, suggestion이고, 클라이언트는 모르는 entity의 행을 건너뜁니다(E18).
- 쓰기: `POST /v1/mutations` → 필드 단위 compare-and-set(D18)
- 충돌: 스칼라 필드는 같은 필드만 충돌로 판정하고, 텍스트 본문 충돌은 ConflictRecord로 보존합니다. last-write-wins는 없습니다.

## 11. 검색

- MVP는 SQLite FTS5와 구조화 필터를 씁니다. 한국어 분석기는 측정 후 결정합니다(DECISIONS O2).
- 인덱싱 대상: ContentBlock, Artifact, Item·Note, 파일명.
- 인덱스는 순수 캐시이므로 백업하지 않고 재구축합니다.
