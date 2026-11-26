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

필드 이름은 `cloud_policy` 하나입니다. Capture에 기본값을 두고 Attachment는 필요할 때만 override합니다. 유효 정책 = `Attachment.cloud_policy ?? Capture.cloud_policy ?? source_type별 기본값`.

| 정책 | 의미 |
|---|---|
| local_only | 로컬 인덱싱/처리만 허용 |
| cloud_allowed | 외부 AI provider 전송 가능 |
| cloud_redacted | 민감 부분을 제거/마스킹한 뒤 전송 가능 |
| manual_review | 사용자가 개별 요청할 때만 처리 |
| blocked | AI 처리 및 외부 전송 금지 |

기본값은 보수적으로 설정합니다. 강의 녹음은 `manual_review`, 그 외는 `local_only`가 기본이며, 사용자가 과목·자료 유형 단위로 바꿀 수 있습니다. 수업 녹음은 교수자/학교 정책을 확인하고 사용자가 허용한 경우에만 cloud processing을 진행합니다.

- **파생 데이터 상속:** transcript, 요약, 필기(Annotation), 필기 OCR처럼 다른 자료에서 나온 데이터의 유효 정책은 입력 중 가장 엄격한 값입니다. 엄격도: `blocked > local_only > manual_review > cloud_redacted > cloud_allowed`.
- **단일 검사 지점:** 정책 검사는 provider adapter 호출 직전 하나의 함수에서만 수행합니다. 이 함수를 거치지 않는 provider 호출 경로를 만들지 않으며, 테스트로 이를 확인합니다.
- **`cloud_redacted`:** redaction 파이프라인이 구현되기 전까지 `manual_review`와 동일하게 동작합니다.

API 전송 전에 파일/텍스트 종류와 크기, provider/model, 예상 비용, 목적, 취소 방법을 표시합니다.

## 네트워크 및 인증

- Tailscale tailnet에는 본인 소유 장치만 등록합니다.
- Tailscale ACL/grants의 최소 권한 모델을 검토합니다.
- 인터넷 공유기 포트 포워딩을 하지 않습니다.
- 제품 API는 Agent가 `127.0.0.1`에만 바인딩하고 `tailscale serve`가 HTTPS를 종단합니다(D5). Windows 방화벽에 CampusLife inbound 규칙을 열지 않습니다.
- `tailscale serve`를 거친 요청은 localhost 출처로 보이므로 출처로 권한을 판단하지 않습니다. pairing 승인·revoke 등 관리 기능은 serve에 노출하지 않는 별도 localhost 포트에만 둡니다.
- Tailscale HTTPS 인증서를 켜면 장치·tailnet 이름이 공개 Certificate Transparency 로그에 남으므로 노트북 장치 이름에 실명·학교명을 쓰지 않습니다.
- Phase 0 SFTP PoC를 하는 동안에만 OpenSSH를 켜고, 관리자 권한이 없는 전용 계정과 Tailscale 장치로 제한한 방화벽 규칙을 씁니다. PoC가 끝나면 끕니다.
- 제품 API는 장치별 토큰으로 모든 요청을 인증합니다(D6).
- 전송은 체크섬, 멱등 키, 서버 ACK를 사용합니다.
- API key는 Windows Credential Manager/DPAPI 등 OS 보호 저장소에 보관합니다.
- 로그에 access token, API key, 전체 강의 텍스트를 기록하지 않습니다.

## 데이터 무결성

- 원본 파일은 불변 저장을 기본으로 합니다.
- 파일마다 SHA-256, 크기, MIME type, 생성/수집 시각을 기록합니다.
- 임시 파일을 검증한 후에만 최종 위치로 atomic move합니다.
- 중복 전송은 idempotency key로 처리합니다.
- ACK 전 모바일 원본을 자동 삭제하지 않습니다. ACK 후에도 Hub 백업에 포함된 것이 확인되고(`backed_up_at`) 보존 기간이 지난 뒤에만 정리합니다(D14).
- 사용자 삭제는 휴지통/복원 기회를 제공합니다.
- AI 산출물은 원본과 구분하고 model/prompt/version을 기록합니다.

## 백업과 복구

노트북 단일 저장만으로 장기 보존이 충분하지 않습니다. NAS를 상시 운영하지 않더라도 다음을 권장합니다.

- Windows BitLocker 활성화
- 정기적인 외장 SSD 또는 별도 암호화 백업
- SQLite 일관성 백업
- 원본과 DB를 같은 백업 세대에 포함
- 최소 분기별 복원 테스트
- 학기 종료 시 portable export 생성
- 백업 드라이브는 백업 후 분리 보관

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
| 민감 정보 외부 전송 | per-item cloud policy, 미리보기/확인, redact |


## Multi-device Security

- 각 Android/iPadOS 설치는 독립적인 Device ID와 토큰을 갖는다. Windows 로컬 UI는 localhost admin 포트를 쓰며 장치 토큰이 필요 없다.
- 신규 장치는 Windows Hub에서 생성한 일회성 pairing code/QR로 승인한다. 코드는 짧은 유효기간·1회 사용·시도 횟수 제한을 가진다.
- 장치 토큰은 256-bit 무작위 불투명 토큰이며 Hub DB에는 해시만 저장한다. 클라이언트는 Android Keystore 기반 암호화 저장소, iPadOS Keychain에 보관한다(D6).
- 토큰은 기본 만료 없이 장치별로 회전·폐기할 수 있어야 한다. 짧은 만료를 두지 않는 이유는 오프라인 복귀 후 잠김과 iPadOS 백그라운드 전송 401을 피하기 위해서다.
- 분실한 iPad/Fold8을 device registry에서 revoke하면 이후 요청을 거부한다.
- 로컬 cache와 offline Outbox에도 민감한 자료가 남을 수 있으므로 OS 잠금/암호화 및 앱 잠금 정책을 사용한다.
- iPadOS background URLSession task는 인증 만료/작업 복구 상황을 고려하고 로그에 credential이나 문서 내용을 남기지 않는다.
- Tailscale 연결 여부만으로 앱 사용자를 인증된 것으로 취급하지 않는다. Tailscale network policy와 CampusLife application auth를 함께 적용한다.
- 각 자료의 cloud processing policy는 장치가 바뀌어도 동일하게 적용한다.
- 충돌로 생긴 양쪽 revision은 사용자 결정 전 삭제하지 않는다.
