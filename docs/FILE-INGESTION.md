# 강의·문서 통합 수집 및 파일 분석 설계

## 1. 목적

강의 자료는 음성 녹음과 사진만으로 구성되지 않습니다. 교수자와 조교는 PDF 강의안, PPT/PPTX 슬라이드, DOCX/HWP 안내문, XLSX 실험 데이터, CSV, ZIP 실습 코드, 동영상, 자막, 웹 링크 등 서로 다른 형식으로 자료를 배포할 수 있습니다.

CampusLife는 이를 별도 기능으로 분리하지 않고 **하나의 강의 자료 묶음(CourseMaterialSet)** 으로 수집합니다. 녹음·사진·배포 파일을 같은 강의 세션에 연결하고 형식별 추출기로 내용을 분석합니다.

## 2. 사용자 경험

### Android
1. LMS/학교 웹/메일/메신저/파일 앱에서 여러 파일을 선택해 Android 공유 메뉴로 CampusLife에 전달합니다.
2. CampusLife는 원본을 앱 전용 Outbox에 복사하고 메타데이터와 해시를 생성합니다.
3. 사용자가 학기, 과목, 강의일, 자료 제목, 배포자(교수/조교/학교)를 선택하거나 시간표에서 제안값을 받습니다.
4. 기존 자료 묶음에 추가하거나 새 자료 묶음을 만듭니다.
5. 노트북이 오프라인이면 Outbox에 보관하고 연결되면 업로드합니다.

### Windows
1. Downloads, 브라우저 다운로드, USB 등에서 파일/폴더를 Companion Inbox에 드래그앤드롭합니다.
2. 여러 파일을 하나의 CourseMaterialSet으로 묶습니다.
3. 원본 보존 및 검증 후 추출 작업이 큐잉됩니다.
4. 자료별 추출 진행 상태와 실패 이유를 확인합니다.

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

### CourseMaterialSet
- id, title, course_id, lecture_session_id(선택), term_id
- distributed_by (교수, 조교, 학과, 학교, 기타), distributed_at, captured_at
- source_uri(선택), description, attachment_ids[]

예: 자료구조 4주차 - 트리에 PDF 1개, PPTX 1개, 실습 ZIP 1개, 강의 녹음 1개, 판서 사진 4개를 묶습니다.

### Attachment
- UUID, 원래 파일명 및 안전한 저장 파일명
- MIME type(감지값과 신고값), extension, size_bytes, SHA-256
- captured_at / modified_at, source, cloud_processing_policy
- extraction_state, course_material_set_id

원본 파일은 보존하고 추출/변환 결과를 원본 경로에 덮어쓰지 않습니다.

### ExtractedDocument / ContentBlock
Extractor는 여러 포맷을 공통 구조로 정규화합니다. ContentBlock은 id, document_id, block_type, ordinal, text 또는 structured payload, locator, metadata를 가집니다.

Locator 예:
- PDF: page, optional bounding box
- PPTX: slide index, shape index
- DOCX/HWPX: heading path, paragraph/table index
- XLSX: sheet, cell/range
- Audio: start_ms/end_ms
- Video: start_ms/end_ms/frame timestamp
- Image: region
- URL: canonical URL, page section/anchor

## 5. Extractor 플러그인 인터페이스

FileExtractor 계약을 정의하고 형식별 adapter를 등록합니다. 각 extractor는 읽기 전용 원본 경로를 입력받아 document metadata, ordered ContentBlock 목록, warnings, extraction state, tool/version을 반환합니다.

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

CourseMaterialSet 내 모든 파일의 추출이 끝나면:
1. 강의 녹음 transcript를 시간순으로 정리합니다.
2. PPT/PDF 페이지 순서 및 슬라이드 구조를 추출합니다.
3. HWP/DOCX 공지에서 과제 지침, 준비물, 마감일 후보를 추출합니다.
4. XLSX/CSV 실습 자료의 열, 수식, 데이터셋을 설명합니다.
5. ZIP 코드 과제의 파일 목록과 README/코드 구조를 설명합니다.
6. 강의 설명과 배포 자료 사이의 관계 및 차이를 분석합니다.
7. 핵심 주제별 상세 노트를 생성합니다.
8. 과제/시험/다음 주 준비사항 후보를 만듭니다.
9. 출처를 page/slide/sheet/file/time range로 연결합니다.
10. 일정과 할 일은 사용자가 확인한 뒤 Planner에 등록합니다.

자료 간 상충하는 날짜/지시/버전은 AI가 임의로 선택하지 않고 충돌 목록으로 보여줍니다.

## 8. 출처 표시 예시

- 이진 탐색 트리 설명 — week04-slides.pdf, p. 12
- 과제 제출 기한 — assignment-guide.hwpx, 2. 제출 일정
- 실험 결과 평균 — lab-data.xlsx, sheet Result, range B4:B18
- 교수자의 시간 복잡도 설명 — lecture-audio.m4a, 00:32:10–00:33:05

locator를 추출할 수 없으면 파일명/문서 단위 근거로 표시하며 정밀 페이지를 추정하지 않습니다.

## 9. 파일 처리 상태

- queued, validating, extracting, extracted, partial
- needs_ocr, password_required, unsupported, corrupt, blocked_by_policy
- analyzing, completed, failed_retryable, failed_terminal

한 자료 세트 안에 일부 실패한 파일이 있어도 성공한 파일의 분석은 진행할 수 있어야 합니다. 자료 세트의 상태는 각 attachment의 상태를 요약해 보여줍니다.

## 10. 보안 및 리소스 보호

- 확장자만 믿지 않고 magic bytes/MIME을 확인합니다.
- 원본은 해시 확인 후 읽기 전용으로 처리합니다.
- 매크로, 스크립트, 외부 링크, embedded object를 자동 실행하지 않습니다.
- ZIP 추출은 파일 수/압축 해제 크기/중첩 깊이/경로를 제한합니다.
- parser는 timeout, 메모리 및 페이지 수 제한을 가집니다.
- 손상/암호화/DRM 자료는 우회하지 않습니다.
- 외부 AI 전송 전에 사용자/attachment별 cloud policy를 확인합니다.
- AI provider에 보낼 내용과 예상 비용을 확인할 수 있게 합니다.
- AI 결과는 원본을 대체하지 않으며 source reference와 model/prompt 버전을 보존합니다.

## 11. 구현 우선순위

1. 파일 다중 첨부 및 CourseMaterialSet metadata
2. PDF 텍스트 및 OCR
3. PPTX/DOCX/XLSX
4. HWPX 및 HWP fallback
5. 이미지 및 코드/텍스트/ZIP
6. 다중 문서 통합 요약과 근거 추적
7. 오디오/비디오/자막 처리
8. 파일 내 semantic search 및 고급 cross-document QA
