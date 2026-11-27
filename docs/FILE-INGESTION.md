# 강의·문서 통합 수집 및 파일 분석 설계

## 1. 목적

강의 자료는 음성 녹음과 사진만으로 구성되지 않습니다. 교수자와 조교는 PDF 강의안, PPT/PPTX 슬라이드, DOCX/HWP 안내문, XLSX 실험 데이터, CSV, ZIP 실습 코드, 동영상, 자막, 웹 링크 등 서로 다른 형식으로 자료를 배포할 수 있습니다.

CampusLife는 이를 별도 기능으로 분리하지 않습니다. 녹음·사진·배포 파일은 각자 Capture로 들어오고, Relation으로 같은 LectureSession·Course Item에 연결됩니다. 별도의 자료 묶음 엔터티는 두지 않습니다. 형식별 추출기가 내용을 분석합니다.

엔터티 정의는 [DOMAIN-MODEL.md](DOMAIN-MODEL.md), 구조 결정(D2 Blob 저장과 엔터티 축소, D7 업로드, D8 cloud policy, D12 Annotation·렌디션, D13 Locator)은 [DECISIONS.md](DECISIONS.md)가 기준입니다.

## 2. 사용자 경험

### Android
1. LMS/학교 웹/메일/메신저/파일 앱에서 여러 파일을 선택해 Android 공유 메뉴로 CampusLife에 전달합니다.
2. CampusLife는 원본을 앱 전용 Outbox에 복사하면서 sha256을 계산합니다(`content://` 권한은 일시적이므로 즉시 복사).
3. 사용자가 과목·강의 세션을 고르거나 시간표에서 제안값을 받고, 배포자(교수/조교/학교)와 설명을 적습니다. 배포 정보는 `Capture.origin`에, 고른 과목·강의는 `links[]`로 확정 Relation이 됩니다.
4. 이미 올린 자료에 파일을 더하는 경우에도 이번 공유는 **새 Capture**이고, 같은 LectureSession에 Relation으로 붙습니다(D7).
5. 노트북이 오프라인이면 Outbox에 보관하고 연결되면 Blob 업로드 → `capture.commit` 순서로 보냅니다.

### iPadOS
1. Files 앱·Safari·다른 앱에서 Share Extension 또는 앱 내 fileImporter로 파일/URL을 가져옵니다.
2. Share Extension은 App Group 공유 컨테이너의 outbox에 사본만 기록하고, 업로드는 본 앱이 Android와 같은 계약으로 합니다.

### Windows
1. Downloads, 브라우저 다운로드, USB 등에서 파일/폴더를 Companion Inbox에 드래그앤드롭합니다.
2. 과목·강의 세션을 고르면 Blob 저장 + `capture.commit`(device=`hub`)을 모바일과 같은 함수로 호출합니다. 로컬 UI의 가져오기 폼은 LectureSession을 고르고, CLI는 `import --link item:<id>`를 씁니다. 둘 다 `/api/import`와 같은 `about` links를 넘겨 Attachment마다 그 LectureSession으로 가는 Relation이 생깁니다.
3. commit 트랜잭션에서는 `capture.ingest` 하나만 큐잉되고, 그 job이 판별(identify)과 추출 job을 계획합니다(D26).
4. 로컬 UI의 Capture 화면에서 파일마다 판별 형식, 필요한 job의 상태와 `outcome_code`, 실패 이유를 확인합니다. 이름·신고 MIME이 바이트와 어긋나거나 읽는 추출기가 없으면 그것도 보여 줍니다. 다시 처리하려면 Capture마다 또는 전체의 "Reprocess"를 누르거나 `campuslife-hub reprocess --capture <id> | --all`을 씁니다(§9).

## 3. 우선 지원 포맷 및 분석 방법

| 파일 종류 | 예시 확장자 | 1차 추출 | AI 분석 |
|---|---|---|---|
| PDF | .pdf | 텍스트, 페이지, 표, 이미지 | 페이지 요약, 개념, 수식, 시험 범위 |
| 스캔 PDF | .pdf | 페이지 렌더링 + OCR | 표/판서/도식 설명 |
| 프레젠테이션 | .pptx, .ppt | 슬라이드 제목/본문/노트/표 | 슬라이드별 요약과 흐름 |
| 문서 | .docx, .doc | 제목/문단/표/링크 | 안내, 과제 지침, 핵심 설명 |
| 한글 문서 | .hwpx, .hwp | HWPX XML 또는 검증된 HWP 변환기 | 한국어 강의안/행정 문서 분석 |
| 스프레드시트 | .xlsx, .csv, .tsv | 시트/셀/표/수식/헤더 | 실험 결과, 데이터 설명 |
| 이미지 | .jpg, .png, .heic, .webp | EXIF, OCR, 영역 | 판서, 수식, 다이어그램, 표 |
| 오디오 | .m4a, .mp3, .wav | metadata, STT | 전사, 구간별 요약 |
| 동영상 | .mp4, .mov | 오디오 추출, 자막, keyframe | 영상 요약, 장면-음성 정렬 |
| 자막 | .srt, .vtt | 타임코드/자막 구간 | 영상/강의 내용 검색 |
| 텍스트/웹 | .txt, .md, .html, URL | 본문과 구조 | 요약, 태그, 일정/할 일 후보 |
| 압축 | .zip | 내부 파일 목록과 안전한 선택 추출 | 파일 묶음 목차/구성 요약 |
| 개발 자료 | .py, .c, .cpp, .java, .js, .ts, .json, .xml, .yaml, .ipynb | 텍스트, 구문 구조, notebook cells | 코드 설명, 실습 단계, 오류 후보 |

초기 우선순위는 PDF, PPTX, DOCX, HWPX/HWP, XLSX/CSV, 이미지, TXT/MD, ZIP입니다. 동영상과 고급 포맷은 다음 단계에서 추가합니다.

구형 .ppt/.doc 바이너리 포맷은 직접 parser를 무리하게 구현하지 않습니다. 변환 도구 또는 사용자가 변환한 파일을 통해 처리하고 fallback 상태를 분명히 표시합니다.

## 4. 공통 데이터 구조

### 묶음 표현 (D2)
"자료구조 4주차 — 트리"에 PDF 1개, PPTX 1개, 실습 ZIP 1개, 강의 녹음, 판서 사진 4개가 있다면:
- 녹음과 사진은 녹음 Capture 하나, 배포 파일은 공유 Capture 하나(또는 여러 개)로 들어옵니다.
- 각 Attachment는 `Relation(attachment → lecture_session)`으로 같은 LectureSession Item에 연결됩니다.
- 교차 문서 분석의 범위는 "한 LectureSession에 연결된 모든 Attachment"입니다.
- 개정판(v2)은 새 Blob이며, `supersedes` Relation은 AI가 Suggestion으로 제안하고 사용자가 확정합니다.

### Attachment
- UUIDv4, 원래 파일명(NFC 정규화 + 원문 보존). 저장 경로는 Blob의 sha256으로 결정되므로 별도 안전 파일명이 필요 없습니다.
- MIME type(감지값과 신고값), size_bytes, blob_sha256, role, captured_at, cloud_policy
- backed_up_at(Hub 소유)
- 처리 상태 필드는 없습니다. 상태는 ProcessingJob에 있습니다(§9).

원본 Blob은 읽기 전용이며 추출/변환 결과는 Artifact Blob으로 따로 저장합니다.

### ContentBlock
Extractor는 여러 포맷을 공통 구조로 정규화합니다. ContentBlock은 테이블이 아니라 extraction Artifact 본문(`artifact/extraction`)의 block입니다(D26). block은 `id = sha256(canonical_json([blob, extractor.id, extractor.version, extractor.params, ordinal]))`(결정적), block_type, text 또는 structured payload, refs(Locator)를 가지고, 본문 안의 순서가 ordinal입니다. 같은 Blob을 같은 추출기·버전·params로 읽는 job은 하나뿐입니다. 이름이 달라 params가 다르면(같은 바이트를 Markdown과 plain text로 읽음) extraction Artifact가 둘입니다.

locator는 공통 Locator 스키마(D13)를 씁니다: `pdf_page`(page, 선택 box), `slide`(slide, shape), `doc_path`(DOCX/HWP/HWPX), `sheet_range`(XLSX/CSV), `media_time`(오디오/비디오/자막), `image_region`, `web_snapshot`, `document`.

## 5. Extractor 플러그인 인터페이스

FileExtractor 계약을 정의하고 형식별 adapter를 등록합니다. 각 extractor는 읽기 전용 원본 경로를 입력받아 document metadata, ordered ContentBlock 목록, warnings, extraction state, tool/version을 반환합니다.

변환기(descriptor `kind: render`, job type `render.<id>`)는 **PDF 렌디션**을 만듭니다. 변환기는 작업 디렉터리에 PDF를 쓰고, Hub는 그 파일을 그대로 rendition Artifact(`subject_ref: blob:<원본>`)의 `content_blob`으로 저장합니다. Relation은 만들지 않습니다(D26). iPad·Android 뷰어는 PDF 렌더링만 구현하고, PPT/PPTX/DOCX/HWP 등은 Hub가 만든 렌디션 Blob을 보여줍니다. 변환기 버전이 바뀌어 다시 만들어도 옛 렌디션은 남고, 지우는 것은 원본의 purge뿐입니다. 렌디션은 원본이 아니며, AI 근거 Locator는 원본 형식의 위치(slide, doc_path 등)를 가리킵니다. 필기는 렌디션 Blob에 고정되고, 레이어가 생긴 렌디션은 pin되어 재생성·정리 대상에서 빠집니다(D12).

extractor는 형식 판별 결과(magic bytes + MIME)로 선택합니다. 확장자만으로 선택하지 않습니다. identify job이 child에서 Blob 바이트만으로 형식 계열을 판별하고, Attachment마다 확장자·신고 MIME이 그 계열 안에서만 좁힙니다(`refine`). extractor는 subprocess로 실행하고 timeout·Windows Job Object 메모리 상한을 두며, 결과는 stdout JSON으로 받습니다. parser 크래시나 무한 루프가 Hub 본체를 멈추지 않습니다(D16).

추출기·변환기 하나는 `campuslife_hub.extract`의 함수와 descriptor(`Extractor(id, version, entry, accepts, kind)`)이고, `extractors.ALL`에 넣으면 등록됩니다. child 실행, 상한, 결과 매핑, Locator 앵커와 block id, 기록은 프레임워크가 맡습니다. ZIP 계열은 `extract/safe_zip.py`로만 엽니다.

추천 초기 구현 후보(실제 도입 전에 라이선스/유지보수/Windows 호환성 확인):
- PDF: PyMuPDF 또는 pypdf, 스캔 페이지 OCR은 별도 adapter
- PPTX: python-pptx
- DOCX: python-docx
- XLSX: openpyxl
- CSV/TSV: Python csv 또는 pandas
- HWPX: ZIP 컨테이너 내부 XML parser
- HWP: pyhwp 등 추출기 또는 검증된 Hancom/LibreOffice 변환 adapter. 학교 문서 샘플로 한글·표·수식 보존을 검증
- 이미지: Pillow + OCR/Vision provider
- Audio/Video: FFmpeg 기반 metadata/audio extraction, STT/Vision adapter
- HTML/EPUB: 표준 parser 및 안전한 콘텐츠 추출
- 열람용 PDF 변환: LibreOffice headless 등 (Windows 설치·라이선스 확인)

## 6. 파일별 처리 정책

### PDF
- 텍스트 PDF와 스캔 PDF를 구분하고 페이지별 상태와 page locator를 보존합니다.
- 스캔 페이지는 OCR 또는 Vision fallback을 사용합니다.
- 표/수식 추출은 틀릴 수 있으므로 원본 페이지 이미지를 함께 제시합니다.

### PPT/PPTX
- 슬라이드별 제목, 본문, 표, speaker notes, 이미지 reference를 추출합니다.
- 슬라이드 번호를 근거로 사용합니다.
- 차트와 이미지가 포함된 경우 별도 이미지 분석을 요청할 수 있습니다.

### DOCX/HWP/HWPX
- 제목 계층, 문단, 표, 하이퍼링크를 보존합니다.
- HWPX는 XML 기반 내부 구조를 추출합니다.
- 구형 HWP 바이너리는 도구 호환성이 다를 수 있으므로 성공/부분 성공/변환 필요 상태를 구분합니다.
- 비밀번호 보호, DRM 파일은 우회하지 않습니다.

### XLSX/CSV
- 워크북·시트·행열 구조를 유지합니다.
- 수식과 계산된 표시값을 구분합니다.
- 수치 데이터가 클 경우 전체 셀을 LLM에 보내지 않고 표 구조/통계/선택 범위로 요약합니다.
- 민감하거나 개인 식별 가능한 열은 cloud policy를 확인합니다.

### ZIP 및 개발 자료
- ZIP은 내부 엔트리 목록, 압축 해제 크기, 중첩 수준, 파일 수를 확인합니다.
- path traversal, symlink, 압축 폭탄을 차단합니다.
- 소스코드/노트북은 텍스트와 구조만 분석하며 실행하거나 package install을 하지 않습니다.
- 실행 가능 파일은 실행하지 않고 격리/메타데이터만 기록합니다.

### Video
- 오디오 트랙과 자막을 우선 추출합니다.
- 전체 프레임을 무차별 전송하지 않고 scene/keyframe sampling을 사용합니다.
- 시간 정보를 보존해 자막/전사/프레임을 연결합니다.

## 7. 강의 전체 자료 통합 분석

한 LectureSession에 연결된 모든 Attachment의 추출이 끝나면:
1. 강의 녹음 transcript를 시간순으로 정리합니다.
2. PPT/PDF 페이지 순서 및 슬라이드 구조를 추출합니다.
3. HWP/DOCX 공지에서 과제 지침, 준비물, 마감일 후보를 추출합니다.
4. XLSX/CSV 실습 자료의 열, 수식, 데이터셋을 설명합니다.
5. ZIP 코드 과제의 파일 목록과 README/코드 구조를 설명합니다.
6. 강의 설명과 배포 자료 사이의 관계 및 차이를 분석합니다.
7. 핵심 주제별 상세 노트를 생성합니다.
8. 과제/시험/다음 주 준비사항을 Suggestion으로 만듭니다.
9. 출처는 LLM이 입력 ContentBlock id(`[B:<id>]`)로만 인용하고, Hub가 검증해 Locator로 바꿉니다(D13).
10. 일정과 할 일은 사용자가 Suggestion을 수락해야 Item이 되어 Planner에 들어갑니다(D10).

자료 간 상충하는 날짜/지시/버전은 AI가 임의로 고르지 않고, 서로 `conflicts_with`로 묶인 Suggestion 쌍으로 보여줍니다.

## 8. 출처 표시 예시

- 이진 탐색 트리 설명 — week04-slides.pdf, p. 12
- 과제 제출 기한 — assignment-guide.hwpx, 2. 제출 일정
- 실험 결과 평균 — lab-data.xlsx, sheet Result, range B4:B18
- 교수자의 시간 복잡도 설명 — lecture-audio.m4a, 00:32:10–00:33:05

locator를 추출할 수 없으면 파일명/문서 단위 근거로 표시하며 정밀 페이지를 추정하지 않습니다.

## 9. 파일 처리 상태

상태는 소유자별로 나뉩니다(D1).

- 수신·검증: 클라이언트 전송 상태와 Blob finalize 결과입니다. 해시 불일치는 finalize의 `409 hash_mismatch`로 끝나고, Hub에는 검증된 Blob만 등록됩니다. Attachment에는 수신 상태 필드가 없습니다.
- 추출·AI 분석: 해당 ProcessingJob의 `state`(`pending, running, retry_wait, succeeded, failed, blocked, cancelled`)와 `outcome_code`(`unsupported, password_required, corrupt, needs_conversion, partial, policy_blocked, budget_blocked, unsafe_input`)입니다. `unsafe_input`은 위험한 구조나 상한 초과로 처리하지 않은 입력이며, 세부 사유는 동기화하지 않는 job error에 남깁니다(D20 U3).

한 강의 세션의 파일 일부가 실패해도 성공한 파일의 분석은 진행합니다. 화면의 요약 상태는 Hub가 job을 집계한 `processing_summary`이며 따로 저장하지 않습니다.

Windows 로컬 UI는 파일마다 그 파일에 필요한 job(Blob의 identify, 판별 결과로 고른 extract·render)의 최근 상태와 `outcome_code`, 실패 이유를 보여 줍니다. 이름과 바이트의 불일치, "읽는 추출기 없음"은 저장하지 않고 화면을 그릴 때 계산합니다.

**재처리:** `reprocess`는 `capture.ingest`를 nonce와 함께 다시 넣고, 그 nonce가 identify·extract·render job params에 들어가 다시 돕니다. 원본 Blob은 바꾸지 않고, 같은 추출기 버전이면 block id가 같습니다. 추출기를 올려도 자동으로 다시 처리하지 않으므로, 새 버전으로 읽히고 싶은 Capture는 재처리합니다(D26).

## 10. 보안 및 리소스 보호

- 확장자만 믿지 않고 magic bytes/MIME을 확인합니다.
- 원본은 해시 확인 후 읽기 전용으로 처리합니다.
- 매크로, 스크립트, 외부 링크, embedded object를 자동 실행하지 않습니다.
- ZIP 추출은 파일 수/압축 해제 크기/중첩 깊이/경로를 제한합니다.
- parser는 timeout, 메모리 및 페이지 수 제한을 가집니다.
- 상한은 표 하나(`services/agent/src/campuslife_hub/extract/limits.py`의 `Limits`)에 있습니다. 표는 child 요청에 실리고, 해시가 identify·extract·render pipeline 버전(`<id>-<version>+L<해시 8자>`)에 들어가므로 값을 바꾸면 새 job이 됩니다. 넘으면 job은 `outcome_code: unsafe_input`으로 끝나고 다시 시도하지 않습니다.

| 상한 | 기본값 | 출처 |
|---|---|---|
| `timeout_s` (child 한 번의 실행 시간) | 120 s | 측정하지 않음. 형식별 추출기(#6·#7·#13)와 변환기(O4)가 수백 MiB 강의 PDF·슬라이드에 필요한 여유로 둠 |
| `mem_bytes` (child 메모리: POSIX 주소 공간, Windows commit) | 1 GiB | 위와 같음 |
| `input_max_bytes` (extract·render가 통째로 읽는 Blob) | 256 MiB | 위와 같음. identify는 이 상한을 받지 않음(아래) |
| `rendition_max_bytes` (변환기가 쓴 PDF) | 512 MiB | 위와 같음 |
| `stdout_max_bytes` (child 출력) | 64 MiB | text 추출기 측정: `max_blocks`에서 멈춘 출력이 약 10 MiB, 긴 블록 몇 개짜리 한국어 텍스트가 `max_text_chars`에서 멈추면 약 24 MiB |
| `max_blocks` (본문 하나의 블록) | 20,000 | Hub가 본문을 만들고 검증하는 데 블록당 약 0.78 ms(20,000이면 약 16 s, handler timeout 안). 300쪽 교재 하나(약 3,000블록)를 자르지 않음. 50,000(약 38 s)은 timeout 여유가 적음 |
| `max_block_text_chars` / `max_text_chars` | 200,000 / 8,000,000자 | 위 출력·본문 상한 안에 들도록 정함 |
| `body_max_bytes` (Hub가 저장하는 본문, canonical JSON) | 64 MiB | `max_blocks`에서 멈춘 본문이 약 12.6 MiB |
| `zip_max_members` | 10,000 | 멤버 10,000개 ZIP의 identify가 0.05 s·26 MiB |
| `zip_max_total_bytes` / `zip_max_member_bytes` | 1 GiB / 256 MiB | 측정하지 않음. 멤버를 읽는 #13이 측정함 |
| `zip_max_ratio` (멤버 하나의 압축 해제 비율) | 200 | 0으로 채운 64 MiB의 deflate 비율 1,029(zip bomb)와, 한 문단을 140번 반복한 1 MiB Markdown의 비율 3.8 사이 |
| `zip_max_depth` (ZIP 안의 ZIP) | 2 | 바깥 ZIP과 그 안 한 단계 |

identify는 Blob을 통째로 메모리에 올리지 않으므로 파일 크기와 상관없이 판별합니다. 앞부분을 읽고, ZIP은 멤버 이름만 보고(이름 안전성, 멤버 개수, 중첩 깊이만 검사), OLE 파일은 1 MiB 조각으로 훑어 암호화 표식을 찾습니다. 바이트 상한(`input_max_bytes`, ZIP 멤버·전체 바이트, 압축 비율)은 Blob이나 멤버를 읽는 extract·render job에 걸립니다(D26).

측정은 모두 로컬(Linux, CPython 3.12, 2026-11-28)에서 시험이 만든 합성 입력(한국어·영어 Markdown, ZIP)으로 했고, 사용자 파일이나 운영 데이터가 아닙니다. 수치와 측정 방법은 `limits.py` docstring에 있습니다.
- 손상/암호화/DRM 자료는 우회하지 않습니다.
- 외부 AI 전송은 `ProviderGateway` 하나를 거치며, 입력의 가장 엄격한 cloud policy(D8)를 그 자리에서 확인합니다.
- `confirm_each_send`를 켜면 보낼 내용과 예상 비용을 확인한 뒤 전송하고, 모든 전송은 audit_log에 남습니다.
- AI 결과는 원본을 대체하지 않으며 source reference와 model/prompt 버전을 보존합니다.

## 11. 구현 우선순위

1. 파일 다중 첨부와 LectureSession 연결(Relation)
2. PDF 텍스트 및 OCR, PDF 렌디션
3. PPTX/DOCX/XLSX
4. HWPX 및 HWP fallback
5. 이미지 및 코드/텍스트/ZIP
6. 다중 문서 통합 요약과 근거 추적
7. 오디오/비디오/자막 처리
8. 파일 내 semantic search 및 고급 cross-document QA
