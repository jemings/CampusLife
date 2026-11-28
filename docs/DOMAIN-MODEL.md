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
| ConflictRecord | id(= 그 patch의 mutation_id), entity_ref, set?{key: change}(열림), resolved?{at, mutation_id}(해소). 모든 멤버를 Hub가 씀 |

- Item에는 `status` 열이 없습니다. task의 상태는 `fields.progress` 하나입니다(U4).
- Relation `type`은 `about, supersedes, rendition_of, evidence, tagged, conflicts_with`이며 Hub가 보내는 쪽에서 열린 값입니다. Tag 부여는 `item:<id>` → `tag:<id>`인 `tagged` Relation입니다. `rendition_of`는 사용자가 수동으로 잇는 Relation(교수가 배포한 PDF가 PPTX의 렌디션)이고, Hub가 만든 렌디션은 Relation이 아니라 rendition Artifact입니다(D26).
- Relation `locators`는 사용자가 Suggestion을 수락할 때 Hub가 그 evidence에서 복사하는 근거 위치들입니다(D10). evidence Relation에만 있고, 각 Locator의 anchor(blob 또는 capture)는 `to`와 같습니다. 모르는 Relation type의 locators는 막지 않습니다(알려진 type은 Relation `type`의 `examples`, 곧 알려진 값 목록으로 판정합니다). 한 원본의 여러 곳(예: p.2와 p.5)은 Relation 하나의 locators에 함께 담깁니다. 클라이언트는 쓰지 않습니다.
- AnnotationLayer(#17)의 스키마는 그 이슈가 추가합니다(E20).
- ConflictRecord(`conflict_record/conflict_record`)는 patch가 제출했지만 Hub가 쓰지 않은 값을 보존합니다(D24). Mutation 하나에 기록 하나라서 재전송이 기록을 겹쳐 만들지 않습니다.
  - 대상은 `conflict` 결과의 `conflicted_fields` 전부(스칼라·날짜 묶음 포함)와 `rejected{deleted}`된 patch의 키 전부입니다. `suggestion.accept`가 거절된 것은 Suggestion이 의도를 가지므로 기록하지 않습니다.
  - `set`(patch의 set 모양 그대로)과 `resolved` 중 정확히 하나를 가집니다. 클라이언트는 `set`이 있을 때만 열린 기록으로 봅니다. 해소는 어느 기기에서든 `conflict.resolve` op로 하고, Hub가 `set`을 지우고 `resolved`를 씁니다.
  - 해소된 기록은 남아 같은 Mutation의 재전송이 기록을 다시 열지 못하게 합니다. 대상 엔터티를 즉시 purge하면 기록도 tombstone이 됩니다.

### Hub 파생 (읽기 전용으로 동기화)

| Entity | 주요 필드 |
|---|---|
| Artifact | id, artifact_type(transcript\|ocr\|summary\|rendition\|overlay\|identify\|extraction\|web_snapshot, 열린 값), subject_ref, job_key, provider?, model?, prompt_version?, pipeline_version, input_hashes[](입력 Blob이 없는 job은 빈 배열, extraction·rendition·identify는 1개 이상), content_blob, created_at, fetched?{final_url, media_type}(web_snapshot에 필수) |
| Suggestion | id, source_artifact_id, action: create_item\|patch_item\|add_relation(열린 값), proposed{…}, evidence[](Locator), fingerprint, state, conflicts_with[]?(자기 id 제외) |
| ProcessingJob | id, job_key, job_type, subject_ref, state, outcome_code?, attempts, next_attempt_at, error (상세는 동기화하지 않고 `processing_summary`만 노출) |

- Artifact의 provider·model·prompt_version은 모델을 쓴 job에만 있습니다(렌디션에는 없음). `processing_summary`의 모양은 #8이 정합니다(E20). 유효 `cloud_policy`는 저장하지 않고 호출 때 계산합니다(D8).
- ContentBlock은 테이블이 아니라 extraction Artifact 본문의 block입니다(D26). `id = sha256(canonical_json([blob, extractor.id, extractor.version, extractor.params, ordinal]))`, block_type, text 또는 payload, refs(Locator)를 가집니다. 같은 Blob을 같은 추출기·버전·params로 읽으면 id가 같습니다.
- 알려진 block_type은 `heading`(payload `{level}`), `paragraph`, `page`(PDF 쪽, 텍스트 층이 없으면 text 없음), `table`(문서 안 표, payload 없음), `table_row`(시트·CSV의 행, payload `{formulas?}`), `table_header`, `notes`(슬라이드 노트)입니다(#6). 각 블록의 text·ref·payload 규칙은 `packages/api-contracts/HARNESS.md`의 "Artifact 본문"에 있습니다. extraction warning은 `no_text_layer`, `limit_reached`, `replaced_characters`, `objects_skipped`입니다.
- 본문이 있는 Artifact의 `content_blob`은 공통 envelope `artifact/body` `{v, blocks:[{id, block_type, text?, payload?, refs}]}`이고 type마다 스키마가 좁힙니다(`extraction`, `identify`). `rendition`은 본문이 없고 `content_blob`이 렌디션 PDF 자체입니다. 파이프라인은 렌디션을 Relation으로 잇지 않습니다(D26).
- `web_snapshot` Artifact는 Hub가 링크 Capture의 `url`을 가져온 결과입니다(#63). subject는 `capture:<id>`, `content_blob`은 받은 응답 본문 바이트 그대로(envelope 없음), `input_hashes`는 빈 배열, 가져온 시각은 `created_at`입니다. `fetched`에 리다이렉트 뒤 최종 URL과 매개변수를 뺀 media type을 둡니다. 200 응답만 저장하므로 상태 코드는 없고, 사용자가 준 URL은 Capture `url`에 그대로 남습니다.
- Suggestion `proposed`의 모양은 action을 따릅니다. create_item은 Item create의 data, patch_item은 `{entity: item, id, set}`, add_relation은 Relation이며, Hub가 보내는 모양이라 안쪽도 열려 있습니다. 다만 patch_item의 `set` 키는 같은 계약의 열 이름이라 열려 있지 않습니다(새 열을 제안하는 patch는 계약 버전이 같은 Hub만 보냅니다). 모르는 action은 표시만 하고 수락할 수 없습니다.
- `suggestion.accept`의 `edits`는 patch의 set처럼 키를 쓰고, 값은 제안 대신 쓸 값입니다. 공통 열 키는 계약 검증기가 owner·set_type으로 검사하고, `fields.<key>`와 제안에 있는 키인지는 Hub 적용기(#2)가 판정합니다.
- create_item·patch_item을 수락하면 Hub가 evidence의 anchor마다 evidence Relation을 하나씩 만들고 locators를 합집합으로 모읍니다. patch_item은 결과가 키를 하나라도 적용할 때만 이 근거 Relation을 만듭니다.
  - add_relation은 만든 Relation이 evidence type이면 anchor가 `to`인 Locator만 그 locators에 넣고, 다른 type은 locators 없이 만듭니다(규칙 `locator_anchor`). 근거는 accepted Suggestion의 `evidence`에 그대로 남습니다(D21).

### 시스템

| Entity | 주요 필드 |
|---|---|
| Device | id, platform(android\|ipados\|windows_hub), device_name, app_version, state(active\|dormant\|revoked), created_at, last_seen_at, last_rejected_at, last_status?(마지막 status heartbeat), last_status_at? |
| audit_log | seq, at, event, subject?, details. pairing(성공·실패), revoke, 재승인, dormant 전환, 토큰 재발급·retire를 기록하고(#19) 외부 AI 전송, 삭제, 충돌 해소가 더해집니다. append-only, Hub 전용, 동기화하지 않음 |

- Hub는 토큰과 pairing secret의 SHA-256만 저장합니다(`device_tokens`, `pairing_secrets`). 이 값(예전 표의 `token_hash`)은 Hub DB에만 있고 wire에 나가지 않습니다(D6). 장치마다 current 토큰 하나와, 재발급한 뒤 아직 쓰이지 않은 pending 토큰 하나까지 있습니다. 계약의 Device 모양은 pairing offer(QR), pair 요청·응답, status heartbeat뿐입니다(E20, D23).
- `windows_hub`는 Hub 자신의 Device 행이고 로컬 import가 씁니다. pair하는 장치는 `android`·`ipados`입니다.

### 용어

- **링크 Capture:** `url`이 있는 Capture.
- **`web_snapshot`:** 세 곳에 쓰며 뜻이 다릅니다.
  - Attachment `role`: 클라이언트가 Capture에 담아 올린 페이지 사본입니다.
  - `artifact_type`: Hub가 링크 Capture의 `url`을 가져온 결과입니다. Attachment가 아니며 Capture에 덧붙지 않습니다.
  - Locator `kind`: 스냅샷 Blob 안의 근거 위치입니다(D13). 만드는 쪽은 #8입니다.

## 3. Item kind

| kind | 주요 fields | 비고 |
|---|---|---|
| term | year, semester(first\|summer\|second\|winter), starts_on, ends_on | MVP |
| course | term_id?, instructor?, room?, credits?, schedule_slots[]?(local_time_rule), default_cloud_policy?, confirm_each_send? | MVP |
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
- 장학금 웹 링크 → `url`이 있는 Capture → `web_snapshot` Artifact → 추출 Artifact → `opportunity` Suggestion
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

- 본문이 있는 Artifact는 `artifact/body` envelope `{v, blocks:[{id, block_type, text?, payload?, refs}]}`이고, ref는 공통 Locator를 가리킵니다. #5가 envelope과 extraction·identify 본문을 정했고 #8이 additive로 넓힙니다(D26). `rendition`처럼 PDF 등 파일 자체가 내용인 Artifact는 envelope을 쓰지 않습니다.
- Locator(`locator/locator`)의 kind는 `pdf_page, slide, doc_path, sheet_range, media_time, image_region, web_snapshot, document`입니다. kind는 Hub가 보내는 쪽에서 열린 값이고, 모르는 kind는 `blob`이 있으면 문서 수준 근거로, 없으면 "근거 위치 모름"으로 표시합니다. 이 계약의 `v`는 1이고, page·slide·shape는 1부터, `block_index`는 0부터 셉니다. `box`는 표시 방향 기준 `[x, y, w, h]`입니다(E17). `annotation` kind는 #17이 추가합니다. 모르는 kind도 `blob`(sha256), `capture`(UUID), `box`(정규화 `[x, y, w, h]`(D12·E17), `box_extent` 규칙도 적용)는 타입을 지킵니다. 시트 이름의 31자 한도는 코드 포인트 기준이고, 스프레드시트의 한도는 UTF-16 31단위입니다. `sheet_range`의 `sheet`는 선택이고, 없으면 이름 없는 시트 하나뿐인 구분자 파일(CSV·TSV)의 범위입니다(#6).
- 앵커는 Blob 해시입니다(녹음은 Capture 타임라인, 필기는 레이어 revision).
- LLM은 입력 ContentBlock id(extraction 본문의 block id)로만 인용하고, Hub가 그 id를 검증해 Locator로 치환합니다.
- 정확한 위치가 없으면 `document`를 쓰고 추정하지 않습니다.

## 8. AI 산출물과 사용자 편집 (D9)

- Artifact는 생성 후 바뀌지 않습니다. 재실행은 새 Artifact를 만들고, Hub가 `current` 포인터를 옮깁니다.
- 사용자가 AI 노트를 고치면 `note` Item으로 fork되고, 이후 편집은 Note의 Mutation입니다.
- Artifact는 기록된 입력으로 재실행할 수 있지만 결과가 같다고 보장하지 않으므로 백업 대상입니다.

## 9. Annotation (D12)

- `AnnotationLayer(target_blob, page_number, author_device)`는 작성자가 하나입니다. 화면에서는 같은 (Blob, page)의 모든 레이어를 겹쳐 그립니다.
- payload: `drawing.pkd`(PencilKit 원본, iPad 편집용) + `strokes.v1.json`(표준, 정규화 좌표)
- overlay PDF는 Hub가 strokes로 렌더링하는 Artifact입니다.
- Office·HWP 문서의 필기는 PDF 렌디션 Blob(rendition Artifact의 `content_blob`)에 고정되고, 그 렌디션은 pin됩니다.

## 10. 동기화와 충돌

- 변경 피드: `GET /v1/changes?since=` → 바뀐 행의 현재 상태와 `hub_id`·`epoch`(D3)
  - 행은 `{entity, id, rev, deleted, data}`입니다. entity는 capture, attachment, item, relation, tag, artifact, suggestion, conflict_record이고, 클라이언트는 모르는 entity의 행을 건너뜁니다(E18).
- 쓰기: `POST /v1/mutations` → 필드 단위 compare-and-set(D18)
- 충돌: 같은 키를 함께 바꾼 경우만 충돌로 판정하고, 진 값은 스칼라든 텍스트 본문이든 ConflictRecord 하나에 모두 보존합니다(D24). last-write-wins는 없습니다.
- 복원·이전 뒤: `GET /v1/changes`와 `POST /v1/mutations`는 기준 epoch(`CampusLife-Epoch`)을 싣습니다. Hub의 epoch과 다르면 Hub가 본문을 읽기 전에 409 `epoch_changed`로 답하고, 클라이언트는 캐시를 비우고 `since=0`부터 받고 보관한 Mutation을 다시 보냅니다(D24).

## 11. 검색

- MVP는 SQLite FTS5와 구조화 필터를 씁니다. 한국어 분석기는 한글·CJK 연속 구간을 겹치는 2-gram으로 나눈 뒤 `unicode61`로 색인하는 `hangul-bigram@1`이고, 1글자 질의는 원문을 LIKE로 찾습니다(DECISIONS D27).
- 인덱싱 대상: ContentBlock(extraction 본문의 block), Artifact, Item·Note, 파일명.
- 인덱스는 순수 캐시(`<root>/cache/search.sqlite3`)이므로 백업하지 않고 재구축합니다. Hub의 변경 피드를 cursor로 따라가고, purge 훅이 지운 자료의 문서를 지웁니다(DECISIONS D27 "색인 파일" 절).
