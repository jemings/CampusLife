# 보안·개인정보·데이터 수명주기

## 기본 원칙

CampusLife는 강의 녹음, 학생 정보, 성적, 행정 문서, 개인 일정 등 민감할 수 있는 데이터를 다룹니다.

- 기본 저장 위치는 Windows 노트북(Hub)이며, Fold8·iPad에는 미전송 Outbox와 열람용 cache만 둡니다.
- 외부 AI API 전송은 유형별 허용 설정과 사용자 제어를 따릅니다.
- 학교/교수자의 녹음·촬영 정책과 참여자 동의를 확인합니다.
- 다른 학생의 음성/얼굴 등 불필요한 개인정보를 수집하거나 분석하지 않습니다.
- 자료 공유 기능은 기본 비활성화합니다.
- API key와 인증 정보는 절대 Git에 커밋하지 않습니다.

## 데이터 분류와 AI 처리 정책 (D8)

필드 이름은 `cloud_policy` 하나이고 Attachment에 둡니다. Capture를 만들 때 클라이언트가 `명시값 → 과목(Course)의 default_cloud_policy → 전역 기본 local_only` 순서로 해석해 기록합니다.

| 정책 | 의미 |
|---|---|
| blocked | 보존·해시·형식 검증·텍스트 추출·FTS만. 로컬이든 클라우드든 모델 추론은 하지 않음 |
| local_only | Hub 안의 로컬 provider만 사용 |
| cloud_allowed | 설정의 provider allow-list로 전송 가능 |

순서는 `blocked < local_only < cloud_allowed`입니다. 과목 기본값을 `cloud_allowed`로 바꿀 때는 "이 과목은 녹음 처리 허용을 확인함"을 한 번 받고 기록합니다. 수업 녹음은 교수자/학교 정책을 확인하고 사용자가 허용한 경우에만 cloud 처리를 합니다.

- **파생 데이터 상속:** transcript, 요약, Suggestion, 필기(AnnotationLayer), 필기 OCR처럼 다른 자료에서 나온 데이터의 유효 정책은 `min(입력들)`입니다. 같은 Blob을 가리키는 Attachment가 여럿이면 그중 가장 엄격한 값입니다. 계산은 호출 시점에 하므로 정책을 엄격하게 바꾸면 그 뒤 호출부터 막힙니다.
- **단일 검사 지점:** 모든 provider 호출은 `ProviderGateway.call()`을 거칩니다. adapter를 gateway 밖에서 import할 수 없도록 테스트로 강제합니다.
- **동의 방식은 정책이 아니라 설정입니다:** `confirm_each_send`(전역 또는 과목)를 켜면 전송 전에 provider, 모델, 입력 종류·크기, 예상 비용, 취소 방법을 보여 줍니다. 월 예산·job당 상한을 넘으면 job이 `budget_blocked`로 사용자 승인을 기다립니다.
- **전송 기록:** 외부 호출마다 audit_log에 `{시각, provider, model, 입력 id, 바이트, 정책 스냅샷, 확인 여부, 비용}`을 남깁니다. 정책을 엄격하게 바꾸면 "이미 N회 전송됨(회수 불가)"을 표시합니다.
- **`cloud_redacted`는 v1에 없습니다.** 한국어 PII 탐지 재현율이 검증되면(DECISIONS O5) 추가합니다.

## 네트워크 및 인증

- Tailscale tailnet에는 본인 소유 장치만 등록합니다.
- Tailscale ACL/grants의 최소 권한 모델을 검토합니다.
- 인터넷 공유기 포트 포워딩을 하지 않습니다.
- 제품 API는 Agent가 `127.0.0.1`에만 바인딩하고 `tailscale serve`가 HTTPS를 종단합니다(D5). Windows 방화벽에 CampusLife inbound 규칙을 열지 않습니다.
- `tailscale serve`를 거친 요청은 localhost 출처로 보이므로 출처로 권한을 판단하지 않습니다. pairing secret 발급·revoke 등 관리 기능은 serve에 노출하지 않는 별도 localhost 포트에만 둡니다.
- serve가 붙이는 `Tailscale-User-Login` 신원 헤더는 인증 근거로 쓰지 않고 감사 기록에만 남깁니다(tag가 붙은 장치에서 온 요청에는 채워지지 않음).
- Tailscale HTTPS 인증서를 켜면 장치·tailnet 이름이 공개 Certificate Transparency 로그에 남으므로 노트북 장치 이름에 실명·학교명을 쓰지 않습니다.
- SFTP·FolderSync·OpenSSH는 쓰지 않습니다. 해시와 ACK를 우회하는 두 번째 수집 경로가 되기 때문입니다(D5).
- 제품 API는 장치별 토큰으로 모든 요청을 인증합니다(D6).
- 전송은 sha256 검증, 생성자 ID 멱등(D4), `capture.commit` ACK를 사용합니다.
- API key는 Windows Credential Manager/DPAPI 등 OS 보호 저장소에 보관합니다.
- 로그에 access token, API key, 전체 강의 텍스트를 기록하지 않습니다.
- 로컬 UI는 127.0.0.1 admin 포트에서 12시간짜리 HttpOnly·SameSite=Strict 세션 쿠키로 인증합니다. 쿠키는 포트를 가리지 않으므로, 같은 PC의 다른 포트에서 도는 웹 앱을 브라우저로 열면 그 앱에도 이 쿠키가 전송되고, 그 앱이 같은 이름의 쿠키를 심을 수도 있습니다(잔여 위험). Hub는 다른 앱이 남긴 형식 어긋난 쿠키가 있어도 세션을 읽지만, 쿠키를 가로챈 로컬 앱은 막지 못합니다. 신뢰하지 않는 로컬 웹 앱을 같은 브라우저로 열지 않고, 다 쓰면 로그아웃합니다.

## 데이터 무결성

- 원본 파일은 불변 저장을 기본으로 합니다.
- 파일마다 SHA-256, 크기, MIME type, 생성/수집 시각을 기록합니다.
- 임시 파일을 검증한 후에만 최종 위치로 atomic move합니다.
- 중복 전송은 같은 ID·같은 내용이면 이전 결과를 돌려주고, 다른 내용이면 `409 id_reuse`로 거부합니다.
- ACK 전 모바일 원본을 자동 삭제하지 않습니다. ACK 후에도 Hub 백업에 포함된 것이 확인되고(`backed_up_at`) 보존 기간이 지난 뒤에만 정리합니다(D14).
- 사용자 삭제는 휴지통(기본 30일)을 거칩니다. 교수자·참여자 삭제 요청은 휴지통을 건너뛰는 즉시 purge로 Blob·파생물·인덱스를 지우고, 백업 세대에는 남아 있음을 알립니다. purge는 Blob 단위이고, 그 파일에서 fork한 Note는 지우지 않고 근거가 지워졌다고 표시합니다(확인 화면에서 함께 지울 수 있음, D21 U2).
- AI 산출물은 원본과 구분하고 model/prompt/version을 기록합니다. 재실행 결과가 같다고 보장할 수 없으므로 Artifact는 백업 대상입니다(D9).

## 백업과 복구

노트북 단일 저장만으로 장기 보존이 충분하지 않습니다. NAS를 상시 운영하지 않더라도 다음을 권장합니다.

- Windows BitLocker 활성화
- 정기적인 외장 SSD 백업. 백업은 사용자가 로컬 UI나 CLI로 직접 하고, 마지막 백업에서 7일이 지나면 Hub가 경고하며, 드라이브에는 최근 5세대를 남깁니다(D21 U3)
- **Hub는 백업을 암호화하지 않습니다(D21 U1).** 백업 드라이브를 잃으면 그 안의 강의 녹음·문서·DB가 그대로 드러납니다. 백업 드라이브는 백업 후 노트북에서 분리해 따로 보관합니다
- SQLite 일관성 백업
- 원본과 DB를 같은 백업 세대에 포함
- 최소 분기별 복원 테스트. 복원하면 Hub가 새 `epoch`를 발급하고, 클라이언트가 Hub에 없는 Capture를 다시 올립니다(D3)
- **복원하면 모든 클라이언트 장치가 `dormant`가 되어 다시 승인해야 합니다(D21 U6).** 그래서 백업 뒤에 revoke한 장치도 다시 active가 되지 않습니다. 분실했거나 revoke한 장치는 승인하지 않습니다
- **백업 뒤에 한 purge는 복원으로 되돌려집니다.** 복원 뒤에는 백업 시각 이후 purge한 파일을 다시 purge합니다(Hub의 복원 안내가 그 시각을 알려 줌)
- 백업이 한 번도 돌지 않았으면 모바일 원본 자동 정리가 일어나지 않습니다(D14)
- 학기 종료 시 portable export 생성

절차와 주의 사항은 [`services/agent/README.md`](../services/agent/README.md)의 "백업"·"복원"에 있습니다.

OneDrive 등 동기화 클라우드를 추가하는 경우 암호화, 공유 링크, 보존 정책과 삭제 동작을 별도로 검토합니다. 동기화와 백업은 동일하지 않습니다.

## 외부 AI provider

OpenAI API 공식 정책 안내에 따르면 API 입력과 출력은 명시적으로 opt-in하지 않는 한 모델 학습에 사용되지 않습니다. 다만 기본 abuse monitoring 로그에 고객 콘텐츠가 포함될 수 있고 일반적으로 최대 30일 보관될 수 있으며 endpoint별 application state 정책도 다릅니다. Zero Data Retention 등은 자격 및 별도 승인 조건이 있습니다.

참고: https://developers.openai.com/api/docs/guides/your-data

"학습에 사용되지 않는다"를 "어떤 데이터도 저장되지 않는다"와 혼동하지 않습니다. 사용 시점의 provider 정책과 endpoint별 저장·보존 정책을 확인합니다.

## 학교 정책 및 동의

- 강의 녹음·사진 촬영 전에 강의계획서, 학교 규정, 교수자 안내를 확인합니다.
- 허용되지 않는 강의의 자동 녹음/촬영은 사용하지 않습니다.
- 다른 학생이 포함된 음성·이미지의 외부 전송과 공유를 최소화합니다.
- 교수자/참여자의 요청이 있으면 해당 Capture를 중단하거나 삭제할 수 있어야 합니다.
- 학교 자료 및 저작물은 개인 학습 목적의 허용 범위와 이용 약관을 준수합니다.

## 위협 모델 최소 요구사항

| 위협 | 요구사항 |
|---|---|
| 노트북 분실/도난 | BitLocker, 계정 잠금, API key 보호 |
| Fold8/iPad 분실 | 화면 잠금, 앱 로컬 파일 최소 노출, 장치 토큰 revoke |
| tailnet 내부 다른 장치의 접근 | Tailscale ACL/grants, 장치 토큰, admin 기능은 localhost 전용 |
| 임의 네트워크 접근 | Tailscale 인증, 방화벽 제한, 서비스 인증 |
| 중간에 끊긴 업로드 | checksum, temp path, ACK, retry |
| 중복 작업/AI 비용 증가 | idempotency, job state, budget guard |
| AI 환각 | source provenance, uncertainty, review |
| 원본 우발 삭제 | ACK 후에도 보존, 휴지통, 별도 백업 |
| 백업 드라이브 분실 | 백업은 암호화하지 않음(U1): 분리 보관, 복원 뒤 모든 장치 재승인 |
| 민감 정보 외부 전송 | 자료별 cloud_policy, ProviderGateway 단일 검사, confirm_each_send, audit_log |


## Multi-device Security

- 각 Android/iPadOS 설치는 독립적인 Device ID와 토큰을 갖는다. Windows 로컬 UI는 localhost admin 포트를 쓰며 장치 토큰이 필요 없다.
- 신규 장치는 Windows Hub 화면에 띄운 일회성 QR(유효 5분, 128-bit secret)로 등록한다. 화면에 띄우는 행위가 물리적 승인이므로 별도 승인 단계는 없다. 실패 시도는 rate limit을 걸고, 이미 쓴 secret이 다시 오면 그 secret으로 발급된 장치도 revoke한다.
- 장치 토큰은 256-bit 무작위 불투명 토큰이며 Hub DB에는 해시만 저장한다. 클라이언트는 Android Keystore 기반 암호화 저장소, iPadOS Keychain에 보관한다(D6).
- 토큰은 만료 없이 장치별로 재발급·폐기할 수 있다. 짧은 만료를 두지 않는 이유는 오프라인 복귀 후 잠김과 iPadOS 백그라운드 전송 401을 피하기 위해서다. 기본 90일 동안 접속이 없으면 `dormant`가 되어 재승인이 필요하다.
- 분실한 iPad/Fold8을 device registry에서 revoke하면 이후 요청을 거부한다.
- 로컬 cache와 offline Outbox에도 민감한 자료가 남을 수 있으므로 OS 잠금/암호화 및 앱 잠금 정책을 사용한다.
- iPadOS background URLSession task는 인증 만료/작업 복구 상황을 고려하고 로그에 credential이나 문서 내용을 남기지 않는다.
- Tailscale 연결 여부만으로 앱 사용자를 인증된 것으로 취급하지 않는다. Tailscale network policy와 CampusLife application auth를 함께 적용한다.
- 각 자료의 `cloud_policy`는 장치가 바뀌어도 동일하게 적용한다.
- 충돌로 제출이 거부된 값은 ConflictRecord로 보존하고, 사용자 결정 전 삭제하지 않는다(D18).
