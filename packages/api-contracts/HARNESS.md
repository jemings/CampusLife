# 언어별 왕복 하네스 프로토콜

Kotlin(#3)·Swift(#16) 앱 모델이 계약과 같은 JSON을 읽고 쓰는지 검사하는 방법입니다(D17, #20 E8·E9). 각 언어는 아래 입출력 규칙을 따르는 하네스를 만들고, 판정은 이 패키지의 비교기 하나로 합니다. Python 참조 구현은 `tests/test_fixtures.py`가 돌리는 왕복(`json.loads` → `json.dumps` → 비교)입니다.

## 용어

- **schema_id**: `schemas/` 아래 경로에서 `.json`을 뺀 것. 예: `schemas/common/instant.json` → `common/instant`.
- **fixture 종류**(`fixtures/<종류>/<schema_id>/<case>.json`)
  - `valid`: 계약에 맞는 값. 하네스가 **정확히 왕복**해야 합니다.
  - `forward`: 지금 모델이 모르는 필드·kind가 들어 있는 값. 디코더가 **오류 없이** 읽어야 합니다(모르는 것은 무시하고 아는 부분은 읽음, E21).
  - `invalid`: 계약 위반 값과 `<case>.expect.json`(기대 위반 목록). Hub 검증기(`campuslife_contracts.validate`)용이며, 앱 디코더가 거부할 의무는 없습니다.
- **`fixtures/policy/`**: 위 세 종류와 별개인 cloud_policy 해석 vector입니다(아래 "cloud_policy vector").

## 하네스 입출력

하네스는 인자 두 개를 받습니다: fixture 루트(`packages/api-contracts/fixtures`)와 출력 디렉터리 `<out>`.

1. `fixtures/valid/<schema_id>/*.json`을 **모두** 찾습니다. `*.expect.json`은 valid 아래에 없습니다.
2. 각 파일을 UTF-8로 읽어, 그 schema_id에 대응하는 그 언어의 모델 타입으로 디코드합니다.
3. 같은 모델을 다시 JSON으로 인코드해 `<out>/<schema_id>/<같은 파일명>`에 UTF-8로 씁니다(디렉터리는 하네스가 만듭니다).
4. 디코드·인코드에 실패한 fixture는 출력 파일을 쓰지 않습니다. 하네스가 모르는 schema_id도 마찬가지입니다. **출력이 없는 fixture는 비교기가 실패로 판정합니다.** 건너뛰기 목록을 두지 않습니다.
5. `fixtures/forward/<schema_id>/*.json`을 모두 디코드합니다. 하나라도 디코드에 실패하면 하네스가 0이 아닌 종료 코드로 끝납니다. forward 출력은 비교하지 않습니다.

### 인코딩 규칙 (비교기가 보는 것)

| 항목 | 규칙 |
|---|---|
| 정수 | 모든 정수는 int64 범위(`common/int64`)이고 값 그대로 돌아와야 합니다. `mono_ns`처럼 2^53을 넘는 값이 있으므로 double을 거치지 않습니다(`capture/timeline/mono_above_2_pow_53`은 2^53+1, `capture/timeline/int64_max`는 2^63-1). |
| 소수 | 십진 값이 같아야 합니다. `1`과 `1.0`은 같고, float32를 거쳐 `0.1`이 `0.10000000149011612`가 되면 다릅니다. |
| 정수 필드에 소수 표기 | 계약은 `2.0`을 정수로 받지 않습니다(invalid fixture). 인코더도 정수 필드를 `2.0`으로 쓰지 않습니다. |
| 멤버 순서 | 무관. |
| 없음과 `null` | 다릅니다. 원본에 없던 멤버를 `null`로 쓰거나, `null`이던 멤버를 빼면 실패입니다. 선택 필드는 "없음"과 "null"을 구분해 모델링합니다(예: Kotlin `explicitNulls`, Swift `encodeIfPresent`와 `encodeNil`). |
| 문자열 | 코드 포인트 그대로. NFC·NFD 변환, 공백 정리, 대소문자 변경을 하지 않습니다. 이스케이프 형태(`\uac15`와 `강`)는 자유입니다. 예: `original_filename_raw`는 NFD 그대로, `original_filename`은 NFC 그대로 돌아와야 합니다. 비BMP 문자(이모지)도 서로게이트 쌍이 깨지지 않아야 합니다. |
| 모르는 Item kind | `item/item`의 `kind`는 닫힌 enum이 아닙니다. 모르는 kind는 공통 열(title, 날짜, body)만 읽고 `fields`는 해석하지 않습니다(forward `item/item/unknown_kind`). |
| Hub가 보내는 열린 값 | Relation `type`, Attachment `role`, Capture `origin.type`, changes의 `entity`, Artifact `artifact_type`, Locator `kind`, Suggestion `action`, 오류 본문(`common/error`)의 `code`는 Hub가 보내는 쪽에서 닫힌 enum이 아닙니다. 스키마에 `x-open: true`로 표시되어 있고 알려진 값은 그 옆 `examples`에 있습니다. 열린 enum의 값은 모두 같은 모양 `^[a-z][a-z_]*$`(소문자로 시작하고 소문자와 `_`만, 숫자 없음)이므로 디코더는 이 규칙 하나로 모르는 값을 받습니다. 모르는 값도 디코드해 그대로 보존하고, 모르는 `entity`의 행은 건너뛰며, 모르는 오류 `code`는 일반 실패로 다룹니다(forward fixture). 클라이언트가 보내는 `capture.commit`·`create`·`patch`에서는 알려진 값만 씁니다. 다른 곳의 `examples`는 설명일 뿐입니다. |
| Mutation 결과 | 멤버마다 나타나는 `result`가 정해져 있습니다: `reason`은 rejected, `original`은 duplicate, `missing_blobs`는 retry, `conflicted_fields`는 conflict, `applied_fields`는 applied·conflict, `conflict_record_ids`는 conflict와 rejected{deleted}. 없는 멤버를 `null`로 쓰면 다른 값이 됩니다. |
| Locator·Suggestion | Locator(`locator/locator`)의 `kind`와 Suggestion의 `action`은 Hub가 보내는 열린 값입니다. 알려진 값은 갈래(`$defs/known`의 판별 union)로 검사하고, `^[a-z][a-z_]*$`인 모르는 값은 멤버째 보존합니다(forward fixture `kind_unknown`, `action_unknown`). Locator `v`는 1 이상이고 늘 수 있습니다. 모르는 kind도 `blob`(sha256 문자열), `capture`(UUID 문자열), `box`(정규화 `[x, y, w, h]`(D12·E17), `box_extent` 규칙도 적용)는 타입을 지키므로 `blob`을 문자열로 읽어도 됩니다. 클라이언트 규칙: 모르는 kind는 `blob`이 있으면 그 원본의 문서 수준 근거로, 없으면 "근거 위치 모름"으로 표시합니다. 모르는 action은 표시만 하고 수락할 수 없게 합니다. Suggestion `proposed`는 action에 따라 Item create의 data, `{entity, id, set}`, Relation 모양이며 안쪽도 열려 있습니다. 예외로 patch_item의 `set` 키는 같은 계약의 열 이름이라 열려 있지 않습니다(새 열을 제안하는 patch는 계약 버전이 같은 Hub만 보내며, `tests/test_open_closed.py`의 `FREE_FORM_IN_FEED`가 이 자리를 열림 시험에서 면제합니다). Relation `locators`는 Hub만 쓰고, 규칙 `locator_anchor`는 모르는 Relation type의 locators를 막지 않습니다(알려진 type은 Relation `type`의 `examples`로 판정합니다. `x-open` 필드의 `examples`는 문서용이 아니라 알려진 값 목록(규범)이고 규칙 `locator_anchor`·`set_type`이 읽으며, 목록을 바꾸면 규칙 판정이 함께 바뀌고 fixture와 변이 시험이 고정합니다). 클라이언트가 보내는 쪽에 Locator가 들어가면 닫힌 view `locator/locator#/$defs/input`(알려진 kind, `v` = 1)을 씁니다. 이 view는 `kind`와 `v`만 닫으므로 멤버는 Locator를 싣는 입력 스키마가 닫습니다. |
| 시각 문자열 | `instant`는 `YYYY-MM-DDTHH:MM:SS.sssZ` 문자열 그대로 돌아와야 합니다. 날짜 타입으로 바꿔 담는다면 밀리초 3자리 형식으로 다시 써야 합니다. |
| JSON 형식 | 엄격한 JSON: UTF-8, BOM 없음, 중복 멤버 없음, `NaN`/`Infinity` 없음, 짝 없는 서로게이트 이스케이프(`\ud800`) 없음. |

## 비교기

```sh
cd packages/api-contracts
uv sync --frozen
uv run campuslife-contracts compare fixtures/valid <out>
```

- `fixtures/valid` 아래 모든 `*.json`에 대해 `<out>`의 같은 상대 경로 파일을 읽어 JSON 값으로 비교합니다. `<out>`에만 있는 파일은 무시합니다.
- 같음의 정의는 `campuslife_contracts.canonical_json(value) -> bytes`입니다: 멤버를 코드 포인트 순으로 정렬하고 공백 없이 UTF-8로 쓰며, 숫자는 십진 값 기준 정규형(`1`과 `1.0`, `0`과 `-0`이 같음), 문자열은 코드 포인트 그대로, 없음과 `null`은 다릅니다. 두 파일의 정규 바이트가 같으면 통과이고, 다르면 비교기가 다른 위치를 찾아 보고합니다. Hub도 같은 함수로 `id_reuse`의 "같은 내용"을 판정합니다.
- 종료 코드: `0` 모두 같음, `1` 불일치 있음(깨졌거나 너무 깊이 중첩된 **출력** 파일 포함), `2` 사용법 오류(디렉터리 없음, `fixtures/valid`에 `*.json`이 하나도 없음, 깨졌거나 너무 깊이 중첩된 **golden** 파일).
- 불일치는 표준 출력에 한 줄씩 나옵니다.
  - `common/instant/utc_millis.json: missing output`
  - `common/box/inner.json#/0: expected 0.1, got 0.10000000149011612`
  - `common/error/id_reuse.json#/details: unexpected member object of 0`
  - `common/error/id_reuse.json: not strict JSON: duplicate member 'code'`
- 위치는 JSON Pointer(RFC 6901)입니다.

## cloud_policy vector (A6)

클라이언트는 `capture.commit`을 만들 때 Attachment의 `cloud_policy`를 정하고, Hub는 provider 호출마다 입력 정책의 최솟값을 씁니다(D8). 두 계산이 언어마다 같도록 vector를 둡니다. 정책 순서는 `blocked < local_only < cloud_allowed`입니다.

- `fixtures/policy/resolve/*.json`: `{"explicit": p|null, "course_default": p|null, "global_default": p, "expected": p}`. 결과는 `explicit`가 있으면 그것, 없으면 과목의 `default_cloud_policy`, 그것도 없으면 `global_default`입니다(기본 설정값은 `local_only`). 명시값은 과목 기본보다 느슨해도 그대로 씁니다.
- `fixtures/policy/min/*.json`: `{"policies": [p, ...], "expected": p}`. 결과는 가장 제한적인 정책입니다. `policies`는 비어 있지 않습니다. 입력이 없는 호출은 없으므로 빈 목록은 오류입니다.
- Python 참조 함수: `campuslife_contracts.resolve_cloud_policy(explicit, course_default, global_default)`, `campuslife_contracts.effective_cloud_policy(policies)`. 모르는 값은 `ValueError`입니다.
- 하네스는 모든 vector에 대해 그 언어의 함수를 호출하고, 결과가 `expected`와 다르면 vector 경로를 출력하고 0이 아닌 종료 코드로 끝납니다. 출력 파일은 쓰지 않습니다.

## from_hash vector (E11)

`x-text` 열(Item `body`)을 patch할 때 `from` 대신 `from_hash`를 보냅니다. 값은 텍스트를 NFC로 정규화하고 줄바꿈 CRLF·CR을 LF로 바꾼 뒤 UTF-8 바이트의 sha256을 소문자 hex로 쓴 것입니다. 언어마다 값이 다르면 Hub가 모든 patch를 충돌로 판정하므로 vector를 둡니다.

- `fixtures/text_hash/*.json`: `{"text": "...", "hash": "<소문자 hex 64자>"}`. CRLF, CR, 섞인 줄바꿈, LF 뒤의 CR, NFD, 결합 문자, 조합 제외 문자(U+0958), 단일자(U+212B), 비BMP 문자, 빈 문자열을 담습니다. 같은 텍스트의 변형(`lf`·`crlf`·`cr`·`mixed_line_endings`, `nfc`·`nfd` 등)은 같은 해시입니다. 정규화는 NFKC가 아니라 NFC이므로 `ligature_fi`(U+FB01)는 `fi`와 다르고, U+2028과 NEL(U+0085)은 줄바꿈으로 바꾸지 않습니다.
- 빈 문자열 `""`도 텍스트이므로 sha256("") = `e3b0c442…b855`로 해시합니다. `null`은 "열이 없음"만 뜻하고 해시하지 않습니다. 열이 없던 상태의 patch는 `from_hash: null`을 그대로 보냅니다.
- Python 참조 함수: `campuslife_contracts.text_hash(text)`. 문자열이 아니면 `TypeError`입니다.
- 하네스는 모든 vector에 대해 그 언어의 함수를 호출하고, 결과가 `hash`와 다르면 vector 경로를 출력하고 0이 아닌 종료 코드로 끝납니다.

## HTTP 표면 (OpenAPI)

`openapi/sync.yaml`(OpenAPI 3.1)이 sync 포트의 경로, 상태 코드, 미디어 타입, 인증(Bearer), 헤더를 적습니다. 본문·쿼리·경로 파라미터·헤더의 모양은 모두 `schemas/`의 JSON Schema를 `$ref`하며 이 문서에 따로 정의하지 않습니다(D17). 검사는 `uv run openapi-spec-validator openapi/sync.yaml`과 pytest(`tests/test_openapi.py`)입니다.

- `tests/test_openapi.py`는 operation마다 상태 코드별 본문 스키마와 헤더(어느 헤더 컴포넌트인지까지), 파라미터(`in`·`required`·스키마), 요청 본문을 표로 정확히 비교합니다. 엔드포인트를 더하거나 고치면 그 표도 함께 고칩니다.
- 모든 operation이 400(`invalid_request`·`not_canonical`·`contract_too_old`)을 가집니다. `GET /v1/hub`만 `contract_too_old`를 내지 않습니다. 너무 오래된 클라이언트도 여기서 `min_contract_version`을 읽고 스스로 멈춥니다.
- 어느 operation이든 Hub 헤더를 단 500(`internal_error`)이 올 수 있습니다. 상태 표에는 적지 않습니다. `CampusLife-Hub-Id`가 없는 응답(Hub가 꺼졌을 때 `tailscale serve`가 내는 502 등)은 Hub에 닿지 못한 것으로 보고 backoff한 뒤 다시 보냅니다.
- 업로드를 열 때 Hub에 Blob을 받을 공간이 없으면 400 `hub_storage_full`입니다. 형식 오류(`invalid_request`)와 달리 영구 실패가 아니므로 나중에 다시 엽니다.
- `POST /v1/mutations`의 배치는 Mutation 1–200개입니다. Outbox가 더 길면 여러 배치로 나눠 차례로 보냅니다.
- pairing(`device/pair_request`)의 `platform`은 `android`·`ipados`만 받습니다. `windows_hub`는 Hub 자신의 Device 행입니다. `device_name`·`last_error` 같은 표시 문자열은 제어문자, 줄·문단 구분자(U+2028, U+2029), 양방향 서식 문자(U+202A–U+202E, U+2066–U+2069)를 받지 않습니다.
- 토큰 재발급 `POST /v1/devices/me/token`은 지금 토큰으로 인증하고 본문 없이 보내며, 응답은 `device/pair_response` 모양입니다. 이전 토큰은 새 토큰이 처음 인증에 성공할 때 폐기되고, 그전까지는 둘 다 유효합니다. 다시 요청하면 아직 쓰이지 않은 같은 새 토큰을 돌려줍니다.
- 업로드 세션의 키는 (이 Device, sha256)입니다(E19). 같은 sha256으로 열린 세션에 다른 `size`를 보내면 400 `invalid_request`이고, revoke하면 그 Device의 열린 세션이 폐기됩니다(D6). 이미 저장된 Blob의 finalize는 세션이 없어도 200입니다(D7).
- 업로드 chunk 번호는 0부터이고, chunk i는 바이트 `[i*chunk_size, min((i+1)*chunk_size, size))`입니다. chunk는 ceil(size/chunk_size)개이고 `received`의 값은 모두 그보다 작습니다(규칙 `chunk_index`). 크기 0인 Blob은 chunk가 없고 바로 finalize합니다(E19). 경로의 `{i}`는 정수 파라미터라서 스키마는 `07` 같은 선행 0 표기를 거르지 않습니다. 클라이언트는 `7`처럼 선행 0 없이 쓰고, Hub가 그 밖의 표기를 어떻게 다룰지는 Hub 구현(#2)이 정합니다.
- 쿼리 값(`sync/changes_query`)과 헤더 값은 도착한 그대로의 문자열로 검사합니다. 숫자 문자열(`since`, `Range`, `Content-Range`)의 숫자는 int64 범위 안이고(규칙 `int64_text`: `9223372036854775807`까지), 바이트 범위는 first ≤ last이며 `Content-Range`의 last는 크기보다 작습니다(규칙 `byte_range`). 416은 `Content-Range: bytes */<size>`(`blob/range_not_satisfiable_headers`)를 싣습니다.

## HTTP 헤더 이름

헤더 이름은 대소문자를 구분하지 않고(RFC 9110), HTTP/2는 소문자로 보냅니다. 헤더 스키마(`*_headers`)는 이름을 한 가지 표기(`CampusLife-Hub-Id` 등)로 적으므로, 헤더 묶음을 검사하거나 fixture와 비교하기 전에 이름을 그 표기로 바꿉니다.

- Python 참조 함수: `campuslife_contracts.contract_headers(headers)`. 계약이 정의한 이름은 어떤 대소문자로 와도 계약 표기로 바꾸고, 두 번 오면 `ValueError`입니다. 그 밖의 이름은 그대로 두며, 반복되면 값을 `, `로 잇습니다.
- 헤더 fixture(`fixtures/*/sync/*_headers`, `blob/download_*_headers`, `blob/range_not_satisfiable_headers`, `device/pair_failure_headers`)는 계약 표기를 씁니다. 하네스가 HTTP 응답에서 헤더를 꺼내 비교할 때도 같은 규칙으로 이름을 바꿉니다.

## CI에 넣는 순서 (각 앱 이슈)

1. 하네스를 실행해 `<out>`을 만듭니다(forward 디코드 실패면 여기서 실패).
2. 위 비교기 명령을 실행하고 종료 코드로 판정합니다.
3. cloud_policy vector와 from_hash vector를 돌립니다(하네스의 일부여도 됩니다).

fixture를 추가·수정하는 PR은 이 계약 패키지의 pytest가 먼저 통과해야 하므로, 앱 하네스는 언제나 검증된 fixture만 읽습니다.
