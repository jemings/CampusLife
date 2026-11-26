# CampusLife iPadOS Companion

이 디렉터리는 iPad Pro 13을 위한 네이티브 Study Client의 프로젝트 시작점입니다.

## 역할
- 강의별 PDF/PPT/문서 열람
- AI 요약과 원본 자료를 나란히 비교
- Apple Pencil로 PDF 페이지에 필기/주석
- Files/Share Extension으로 자료를 CampusLife Inbox에 수집
- 오프라인 자료 열람과 pending annotation 저장
- Windows Hub와 공통 HTTPS Sync API로 동기화

## 기술 선택
- SwiftUI: navigation과 adaptive multi-column workspace
- PencilKit: Apple Pencil drawing 및 PKDrawing source 보존
- PDFKit: PDF rendering과 page navigation
- Share Extension / fileImporter: 자료 수집
- URLSession background configuration: 파일 transfer. 백그라운드 세션은 파일 기반 업로드만 지원하므로 큰 파일은 chunk 임시 파일 단위로 task를 만든다
- Keychain: 장치 토큰 (만료 없는 불투명 토큰, revoke로 무효화 — DECISIONS D6)
- SQLite(GRDB 등): `packages/api-contracts`의 sync 테이블 DDL을 따르는 offline cache와 outbox
- App Group: Share Extension과 본 앱이 공유하는 outbox 컨테이너. Extension은 사본만 기록하고 업로드는 본 앱이 한다
- API client: `packages/api-contracts`의 JSON Schema와 golden fixture로 검증 (코드 생성은 선택)

## 데이터 원칙
- 원본 Attachment를 수정하지 않습니다.
- PencilKit drawing은 (Blob, page, 이 장치) 단위 AnnotationLayer로 별도 저장합니다. 다른 장치의 레이어는 겹쳐 그리되 읽기 전용입니다(DECISIONS D12).
- 레이어는 `drawing.pkd`(PencilKit 원본)와 `strokes.v1.json`(표시 방향 페이지 기준 0–1 정규화 좌표)을 함께 가집니다. PDFKit의 0-based 페이지는 경계에서 1-based로 바꿉니다.
- PPT/DOCX/HWP 등은 Hub가 만든 PDF 렌디션 Blob으로 표시하고, 필기는 그 렌디션에 고정됩니다.
- Windows/Android용 PDF overlay는 Hub가 `strokes.v1.json`으로 렌더링합니다. iPad는 overlay를 만들지 않습니다.
- Hub ACK 전 pending changes와 원본 파일을 자동 삭제하지 않습니다.
- 충돌이 발생하면 last-write-wins로 덮어쓰지 않습니다. 레이어 rev 불일치는 형제 레이어로, Note 본문 충돌은 ConflictRecord로 보존합니다.
- 마감·일정 알림은 동기화된 Item의 `fields.reminders[]`로 `UNUserNotificationCenter`에 로컬 예약합니다.

## 구현 순서
0. 배포·서명 방식 확정 (무료 계정의 App Group·Background Modes·Share Extension 가능 여부, DECISIONS O7)
1. Xcode project, SwiftUI shell, device enrollment
2. common API client와 offline data store
3. 강의 세션별 자료 목록 및 PDFKit viewer
4. PencilKit page annotation layer
5. annotation sync와 Hub overlay 확인
6. Share Extension, file import, background URLSession
7. Stage Manager, Split View, orientation, external keyboard 접근성 테스트

상세 계약은 [Multi-device Architecture](../../docs/MULTI-DEVICE-ARCHITECTURE.md), 화면 정책은 [UX Design](../../docs/UX-DESIGN.md)을 참고합니다.

실제 iPadOS 앱 소스와 Xcode project는 구현 이슈에서 생성합니다. 이 디렉터리 README만으로 빌드 가능한 앱을 의미하지 않습니다.
