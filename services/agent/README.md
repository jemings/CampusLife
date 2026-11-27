# CampusLife Hub (`services/agent`)

Windows 11 노트북에서 도는 CampusLife의 유일한 원천 저장소(Hub)입니다. 이 문서는 설치·업데이트·제거와 운영 방법을 적습니다. 설계는 [ARCHITECTURE §2·§6](../../docs/ARCHITECTURE.md)과 [DECISIONS](../../docs/DECISIONS.md)(D16, #2 Hub 코어 결정)을 봅니다.

명령은 모두 `campuslife-hub`(이 폴더에서 `uv run campuslife-hub …`)이고, `campuslife-hub --help`와 `campuslife-hub <명령> --help`가 옵션을 보여 줍니다. 아래 예시는 PowerShell 기준입니다. 괄호 안 번호는 DECISIONS의 결정이고, E·U 번호는 D21(#2 Hub 코어) 안의 번호입니다.

## 준비물

- Windows 11, Hub를 돌릴 사용자 계정 하나
- [Git](https://git-scm.com/)과 [uv](https://docs.astral.sh/uv/)
- Python 3.12: `uv python install 3.12`로 uv가 관리하는 Python을 설치합니다. python.org 설치본도 됩니다.
  - **Microsoft Store Python은 쓸 수 없습니다.** Store Python은 `%LOCALAPPDATA%` 아래 쓰기를 자기 패키지 폴더로 돌려보내므로, 모든 명령이 이유를 출력하고 거부합니다(E21).
  - Hub는 기동할 때 SQLite 3.35 이상과 FTS5·trigram·`json_extract`·generated column을 검사하고, 맞지 않으면 이유와 함께 거부합니다(E2).

## 설치

```powershell
git clone https://github.com/jemings/CampusLife.git $HOME\src\CampusLife
cd $HOME\src\CampusLife\services\agent
uv python install 3.12
uv sync --frozen
uv run campuslife-hub init
uv run campuslife-hub autostart register
schtasks /Run /TN "CampusLife Hub"
uv run campuslife-hub status
uv run campuslife-hub open
```

1. checkout은 OneDrive·Dropbox 같은 동기화 폴더 밖에 둡니다(`.venv`가 함께 동기화되지 않게).
2. `uv sync --frozen`은 `uv.lock`대로 `.venv`를 만들고, 그 안에 `campuslife-hub`(콘솔)과 `campuslife-hubw`(창 없는 pythonw 런처)를 둡니다.
3. `init`은 data root, DB, Hub 신원(`hub_id`)을 만듭니다. 다시 실행해도 바뀌는 것이 없습니다.
   - 기본 data root는 `%LOCALAPPDATA%\CampusLife`입니다. 다른 곳을 쓰려면 모든 명령에 `campuslife-hub --root D:\CampusLife <명령>`처럼 `--root`를 명령 앞에 주거나, 환경 변수 `CAMPUSLIFE_ROOT`를 둡니다.
   - 동기화 폴더(OneDrive·Dropbox) 아래와 `\\`로 시작하는 드라이브(네트워크 공유 등)는 거부합니다(D2, E23).
4. `autostart register`는 이 사용자가 로그온할 때 Hub를 시작하는 Task Scheduler 작업 `CampusLife Hub`를 등록합니다(E21).
   - 작업은 `.venv\Scripts\campuslife-hubw.exe --root <data root> serve`를 실행합니다. 콘솔 창이 없으므로 Hub의 로그와, 기동을 거부한 이유(Store Python, 더 새 DB, 맞지 않는 SQLite, 이미 도는 Hub, 쓰고 있는 포트, 동기화 폴더 등)는 `<data root>\logs\hub.log`에 남습니다. 이유가 남는 것은 data root에 Hub(`database\campuslife.sqlite3`)가 있을 때뿐입니다. Hub가 없는 root에는 파일을 만들지 않습니다. 이 파일은 5 MiB가 차면 `hub.log.1`…`hub.log.5`로 밀려나 최대 6개(30 MiB)를 둡니다.
   - 작업이 곧바로 끝났는데 `hub.log`에 이유가 없으면(data root에 Hub가 없는 경우, 로그 폴더를 쓸 수 없는 경우 등) 콘솔에서 같은 명령 `uv run campuslife-hub --root <data root> serve`를 실행해 이유를 봅니다.
   - 시간 제한 없음, 배터리 사용 중에도 시작·유지, 이미 돌고 있으면 새 시작은 무시합니다.
   - 등록 시점의 data root와, register에 준 serve 옵션(`--admin-port`, `--sync-port`, `--backup-keep`, `--backup-warn-days`, `--public-url`, `--dormant-after-days`)만 작업 인자에 들어갑니다. 바꾸려면 같은 명령을 다시 실행합니다(같은 이름의 작업을 교체). 이름은 `--name`으로 바꿀 수 있습니다.
   - `uv run campuslife-hub autostart xml`은 등록할 작업 XML을 출력만 합니다(인코딩 선언 없음). `--out task.xml`을 주면 register가 schtasks에 넘기는 것과 같은 UTF-16 파일을 씁니다.
5. `schtasks /Run`은 다음 로그온을 기다리지 않고 지금 시작합니다. 로그아웃 후 다시 로그인해도 됩니다. 기동에 몇 초가 걸리므로 `status`가 서빙하는 Hub가 없다고 하면 잠시 뒤 다시 실행합니다.
6. `open`은 브라우저로 로컬 UI(127.0.0.1의 admin 포트, 기본 8731)를 1회용 로그인 링크로 엽니다. 이 포트는 `tailscale serve`에 노출하지 않습니다(D5).
7. 휴대폰·태블릿이 Hub에 닿도록 [sync 리스너와 tailscale serve](#sync-리스너와-tailscale-serve-d5-d22)대로 `tailscale serve`를 한 번 설정하고, `tailscale-check`가 출력한 `hub_url`을 `autostart register --public-url <hub_url>`로 serve에 줍니다. 이 값이 없으면 장치를 추가할 수 없습니다([장치](#장치-d6-d23)).

**한계(D16):** 재부팅 뒤 사용자가 로그인하기 전에는 Hub가 없습니다. 노트북이 잠자거나 꺼져 있는 동안에도 Hub는 멈추고, 각 클라이언트는 Outbox에 쌓았다가 Hub가 돌아오면 다시 보냅니다. 알림은 각 클라이언트가 로컬에서 예약하므로 Hub와 관계없이 울립니다(D19). 실기기에서의 로그온 자동 시작과 장시간 생존은 #14가 확인합니다.

## 업데이트

```powershell
cd $HOME\src\CampusLife\services\agent
uv run campuslife-hub backup --to E:\CampusLifeBackup
uv run campuslife-hub stop
git pull --ff-only
uv sync --frozen
schtasks /Run /TN "CampusLife Hub"
uv run campuslife-hub status
```

- 업데이트 전에 백업합니다. 새 코드는 Hub가 열 때 DB를 새 스키마로 옮기고(E3), DB가 코드보다 새로우면 옛 코드는 기동을 거부합니다. 그래서 되돌리려면 옛 코드로 checkout한 뒤 업데이트 전 백업에서 복원해야 합니다.
- `stop`을 먼저 합니다. 도는 Hub가 `.venv`의 파일을 쥐고 있어 Windows에서는 `uv sync`가 그 파일을 바꾸지 못합니다.
- 작업은 `.venv\Scripts\campuslife-hubw.exe`를 가리키므로 업데이트 뒤 다시 등록할 필요는 없습니다. data root나 serve 옵션을 바꿀 때만 `autostart register`를 다시 실행합니다.

## 제거

```powershell
cd $HOME\src\CampusLife\services\agent
uv run campuslife-hub stop
uv run campuslife-hub autostart unregister
```

- 그다음 checkout 폴더(`.venv` 포함)를 지웁니다.
- data root(기본 `%LOCALAPPDATA%\CampusLife`)에는 모든 원본 파일과 DB, admin 토큰이 있습니다. 지우면 되돌릴 수 없으니 먼저 백업하고, 데이터를 버릴 때만 지웁니다.
- 백업 드라이브의 세대는 암호화하지 않은 사본입니다(U1). 더 쓰지 않으면 그 폴더를 지우거나 드라이브를 안전한 곳에 둡니다.

## 운영

### 명령

| 명령 | 하는 일 |
|---|---|
| `init` | data root, DB, Hub 신원을 만듦 |
| `serve` | Hub 실행(sync 리스너 `127.0.0.1:8730`, 로컬 UI·admin API `127.0.0.1:8731`, job runner, 1시간마다 정리; `--public-url`, `--dormant-after-days`). 보통은 작업이 실행함 |
| `open` | 로컬 UI를 1회용 로그인 링크로 엶 |
| `import <파일…>` | 파일들을 Capture 하나로 가져옴(`--policy`, `--link item:<id>`) |
| `backup --to <폴더>` | 백업 세대 하나를 씀(`--keep N`) |
| `restore --from <폴더>` | 빈 data root에 백업 세대를 복원(`--generation NAME`) |
| `fsck` | 모든 Blob의 해시를 다시 계산하고 누락·손상·고아 Blob을 보고 |
| `status` | 도는 Hub의 job 수, runner 재시작, 마지막 백업과 경고 |
| `stop` | 도는 Hub를 멈춤 |
| `ls` | Capture와 Attachment 목록 |
| `autostart xml\|register\|unregister` | 로그온 자동 시작 작업 |
| `tailscale-check` | `tailscale serve`가 sync 리스너만 노출하는지 확인하고 기대 `hub_url`을 출력(`--sync-port`, `--admin-port`, `--public-url`) |

Hub가 도는 동안 `init`·`import`·`backup`은 그 Hub의 admin API를 거치고, Hub가 없으면 직접 data root를 잠그고 씁니다(E11). `restore`는 빈 data root를 잠그고 채우므로 그 root에서 도는 Hub가 없어야 합니다. `open`·`status`·`stop`은 도는 Hub가 있어야 합니다. `ls`·`fsck`는 잠그지 않고 읽기만 합니다(SQLite가 `-wal`·`-shm` 파일을 만들 수는 있음). `tailscale-check`는 data root를 쓰지 않습니다.

CLI 요청은 600초를 기다립니다. 그보다 오래 걸리는 큰 첫 백업이나 큰 import는 CLI가 시간 초과로 끝나도 Hub가 계속 처리합니다. 백업은 `status`의 `backup last`로, import는 `ls`로 끝났는지 확인합니다. 시간 초과 뒤 import를 다시 하면 같은 파일의 Capture가 둘 생깁니다.

### sync 리스너와 tailscale serve (D5, D22)

`serve`는 sync 리스너를 `127.0.0.1:8730`(`--sync-port`)에만 엽니다. 휴대폰·태블릿은 `tailscale serve`를 거쳐 닿습니다. serve가 이 노트북의 MagicDNS 이름(`<이름>.<tailnet>.ts.net`)의 공인 인증서로 HTTPS를 종단하고, 443을 sync 리스너로 넘깁니다. admin 포트(8731)는 넘기지 않습니다.

```powershell
tailscale serve --bg http://127.0.0.1:8730
uv run campuslife-hub tailscale-check
```

1. 먼저 tailnet 관리 콘솔에서 MagicDNS와 HTTPS 인증서를 켭니다.
2. **인증서를 켜기 전에 이 노트북의 Tailscale 장치 이름을 실명이나 학교명이 들어가지 않은 것으로 바꿉니다.** HTTPS 인증서를 발급하면 장치 FQDN이 공개 Certificate Transparency 로그에 영구히 남습니다.
3. `tailscale serve --bg …`는 설정을 tailscale에 저장하므로 한 번만 하면 되고, 재부팅 뒤에도 남습니다. `--sync-port`를 바꿨으면 그 포트를 씁니다.
4. `tailscale-check`는 `tailscale serve status --json`과 `tailscale status --json`을 읽기만 하고 아무것도 바꾸지 않습니다. 다음을 확인합니다.
   - 443의 HTTPS가 경로 `/`로 `http://127.0.0.1:<sync 포트>`에만 갑니다.
   - 어느 포트·경로·TCP forward도 admin 포트로 가지 않습니다. 이 노트북이 내놓는 Tailscale Service의 설정도 봅니다.
   - Funnel이 꺼져 있습니다. Funnel은 Hub를 인터넷에 엽니다.
   - tailscale이 로그인되어 있고 MagicDNS 이름이 있습니다.
5. 모두 맞으면 `hub_url https://<이름>.<tailnet>.ts.net`과 `ok`를 출력합니다. 하나라도 틀리면 `problem:` 줄을 출력하고 0이 아닌 코드로 끝납니다. 포트를 바꿨으면 `--sync-port`, `--admin-port`를 같은 값으로 줍니다.
   - pairing QR에 들어가는 `hub_url`은 `serve --public-url`의 값입니다(#19). `tailscale-check --public-url <URL>`을 주면, 그 값이 MagicDNS 이름과 다를 때 `warning:`을 출력합니다. 두 명령 모두 `hub_url`이 아닌 값(`http://`, 대문자 호스트, 경로나 끝의 `/`)과 `:443`을 붙인 값을 거부하고, 비교는 글자 그대로 합니다.
   - 두 JSON의 모양은 tailscale 소스에서 읽은 것이라, 실제 tailnet에서는 #39가 확인합니다.
6. 연결 확인: pairing한 장치의 토큰이 없는 요청은 sync 리스너가 400이나 401로 거절합니다. 그래도 응답에 `CampusLife-Hub-Id` 헤더가 있으면 serve를 거쳐 Hub까지 닿은 것입니다. Hub가 꺼져 있으면 serve가 이 헤더 없는 502로 답합니다.

- sync 리스너는 serve가 연결을 다시 쓰도록 유휴 연결을 120초 유지합니다(admin 리스너는 uvicorn 기본값).
- 노트북이 잠자는 동안에는 serve도 답하지 않습니다. 휴대폰에서 학교 VPN 등 다른 VPN을 켜면 Tailscale과 동시에 쓰지 못해 Hub에 닿지 못할 수 있습니다(D5).

### 장치 (D6, D23)

- 장치는 sync 리스너의 `POST /v1/pair`로 pairing하고, 받은 토큰으로 모든 요청을 인증합니다. Hub는 토큰과 pairing secret의 SHA-256만 저장하고 로그에서 가립니다.
- 틀린 secret이 10분에 10번 오면 그 뒤 pairing 시도는 `Retry-After`가 붙은 401입니다. 이 횟수는 메모리에만 있어 Hub를 다시 시작하면 지워집니다.
- 90일(`serve --dormant-after-days`, 1–36500일) 동안 인증하지 않은 장치는 dormant가 되어 재승인할 때까지 401 `device_dormant`를 받습니다. 인증할 때와 1시간마다 하는 정리 때 판정합니다.
- 장치가 보내는 상태(heartbeat)는 마지막 하나만 남깁니다.

### 백업 (U1, U3, E20)

- 백업은 수동입니다. 로컬 UI나 `campuslife-hub backup --to <폴더>`로 합니다. 마지막 백업에서 7일이 지나면 `status`와 로컬 UI가 경고합니다(`serve --backup-warn-days`).
- 한 세대는 DB 스냅샷과 그 스냅샷이 가리키는 Blob(Artifact 포함)입니다. Blob은 세대끼리 공유하므로 드라이브에 없는 것만 복사합니다. 드라이브에는 최근 5세대를 남기고 나머지는 지웁니다(`serve --backup-keep`, CLI `--keep`).
- **백업은 암호화하지 않습니다(U1).** 백업 드라이브를 잃으면 그 안의 강의 녹음·문서·DB가 그대로 드러납니다. 백업이 끝나면 드라이브를 노트북에서 분리해 따로 보관합니다.
- 백업 폴더에는 첫 백업 때 `campuslife-backup.json`(이 Hub의 `hub_id`)이 생깁니다. 이 파일 없이 `generations\`나 `blobs\`가 있는 폴더, 다른 Hub의 폴더, data root를 담거나 data root 안에 있는 폴더는 거부합니다. 백업은 자기가 쓴 파일만 지웁니다.
  - 이 marker가 생기기 전 버전으로 만든 백업 폴더는 거부되니 쓰지 말고 새 폴더에 백업합니다.
- Hub가 꺼진 상태의 CLI `backup`은 `--keep N`을 주지 않으면 세대를 지우지 않습니다("prune skipped"). 유지 세대 수는 `serve`의 설정이라 data root에 저장되어 있지 않기 때문입니다.
- 세대가 끝나면 그 세대가 처음 담은 Blob의 Attachment마다 `backed_up_at`이 한 번 찍히고 피드로 나갑니다. 클라이언트는 이것을 보고 원본을 정리할 수 있습니다(D14).
- 백업 세대에는 그 뒤에 purge한 파일도 남아 있습니다(U2).

### 복원 (E20)

```powershell
uv run campuslife-hub stop
uv run campuslife-hub --root D:\CampusLife-restored restore --from E:\CampusLifeBackup
```

- 비어 있거나 없는 data root에만 복원합니다. 기본으로 가장 최근에 끝난 세대를 고릅니다. 복원한 root를 계속 쓰려면 `campuslife-hub --root D:\CampusLife-restored autostart register`로 작업을 다시 등록합니다.
- 복원하면 새 `epoch`를 발급합니다. 클라이언트는 `since=0`부터 다시 받고, Hub에 없는 Capture를 다시 올립니다(D3).
- **active였던 클라이언트 장치가 모두 dormant가 되어 다시 승인해야 합니다.** revoked 장치는 revoked로 남습니다. 분실했거나 revoke한 장치는 승인하지 않습니다. 재승인은 #19의 흐름입니다.
- **백업 뒤에 한 purge는 복원으로 되돌려집니다.** 출력이 알려 주는 시각 뒤에 purge한 파일은 다시 purge합니다.
- 세대의 Blob이 하나라도 드라이브에 없거나 손상됐으면 복원 전체를 거부합니다(부분 복원은 조용한 데이터 손실이라 그런 옵션은 없습니다). `--generation`으로 다른 세대를 고르거나 다른 백업 드라이브를 씁니다. 원래 Hub가 남아 있으면 `fsck`로 그 Blob을 확인하고 새 백업을 만듭니다.
- 복원이 DB를 놓은 뒤 끝나기 전에 끊기면(전원 차단 등) 그 세대의 백업 기록과 `backed_up_at`이 빠집니다. 데이터는 잃지 않고, 다음 백업이 `backed_up_at`을 다시 찍습니다.

### fsck

`campuslife-hub fsck`는 `blobs\`의 모든 파일의 해시를 다시 계산해 DB와 맞춰 봅니다. 파일이 없거나(missing) 손상됐거나(corrupt) DB가 모르는(orphan) Blob과 끝나지 않은 purge(unfinished_purge)를 보고하고, 발견이 있으면 0이 아닌 코드로 끝납니다. Hub가 도는 동안에는 그 사이 저장되거나 정리되는 Blob 때문에 거짓 양성이 날 수 있으니, 결과가 이상하면 `stop` 뒤에 다시 돌립니다.

### 보존 기간

| 대상 | 보존 |
|---|---|
| 원본 Blob | 무기한(읽기 전용). Attachment·Artifact·Relation 어느 것도 가리키지 않게 된 Blob은 마지막으로 받은 지 7일 뒤 정리 |
| tombstone(삭제 표시) | 무기한. purge 뒤에도 남음 |
| purge | 즉시: Blob, 그것을 가리키는 Attachment, 파생 Artifact·Suggestion과 그 Relation. fork된 Note는 남기고 근거가 지워졌다고 표시(U2). 백업 세대에는 남음 |
| 끝나지 않은 업로드 세션 | 마지막으로 열거나 chunk를 받은 지 7일. 그사이 다른 장치가 같은 Blob을 올렸으면 다시 열 때 바로 끝남 |
| Mutation 결과(`applied_mutations`) | 90일. 그보다 오래된 재전송은 create·patch는 다시 판정되어 해가 없고, delete·restore·Relation create는 다시 적용됨(수용) |
| 백업 세대 | 드라이브마다 최근 N개(기본 5) |
| 로그 `logs\hub.log` | 지금 파일과 이전 파일 5개(`hub.log.1`…`.5`), 각 5 MiB, 최대 30 MiB. 비밀 값과 강의 본문은 가림(E19) |
| `staging\`의 쓰다 만 파일 | 다음 기동 때 지움 |
