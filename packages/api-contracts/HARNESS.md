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
| Hub가 보내는 열린 값 | Relation `type`, Attachment `role`, Capture `origin.type`, changes의 `entity`는 Hub가 보내는 쪽에서 닫힌 enum이 아닙니다. 알려진 값은 스키마의 `examples`에 있습니다. 모르는 값도 디코드해 그대로 보존하고, 모르는 `entity`의 행은 건너뜁니다(forward fixture). 클라이언트가 보내는 `capture.commit`·`create`에서는 알려진 값만 씁니다. |
| Mutation 결과 | 결과의 `reason`·`original`·`missing_blobs`·`conflicted_fields`는 해당 `result` 값에만 나타납니다. 없는 멤버를 `null`로 쓰면 다른 값이 됩니다. |
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

- `fixtures/text_hash/*.json`: `{"text": "...", "hash": "<소문자 hex 64자>"}`. CRLF, CR, 섞인 줄바꿈, NFD, 비BMP 문자, 빈 문자열을 담습니다. 같은 텍스트의 변형(`lf`·`crlf`·`cr`·`mixed_line_endings`, `nfc`·`nfd`)은 같은 해시입니다.
- 빈 문자열 `""`도 텍스트이므로 sha256("") = `e3b0c442…b855`로 해시합니다. `null`은 "열이 없음"만 뜻하고 해시하지 않습니다. 열이 없던 상태의 patch는 `from_hash: null`을 그대로 보냅니다.
- Python 참조 함수: `campuslife_contracts.text_hash(text)`. 문자열이 아니면 `TypeError`입니다.
- 하네스는 모든 vector에 대해 그 언어의 함수를 호출하고, 결과가 `hash`와 다르면 vector 경로를 출력하고 0이 아닌 종료 코드로 끝납니다.

## CI에 넣는 순서 (각 앱 이슈)

1. 하네스를 실행해 `<out>`을 만듭니다(forward 디코드 실패면 여기서 실패).
2. 위 비교기 명령을 실행하고 종료 코드로 판정합니다.
3. cloud_policy vector와 from_hash vector를 돌립니다(하네스의 일부여도 됩니다).

fixture를 추가·수정하는 PR은 이 계약 패키지의 pytest가 먼저 통과해야 하므로, 앱 하네스는 언제나 검증된 fixture만 읽습니다.
