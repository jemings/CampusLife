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
- URLSession background configuration: 파일 transfer
- Keychain: per-device credential
- SwiftData/Core Data 또는 SQLite: offline cache와 pending mutation

## 데이터 원칙
- 원본 Attachment를 수정하지 않습니다.
- PencilKit drawing은 AnnotationLayer로 별도 저장합니다.
- Annotation은 document revision, page index, normalized coordinate 및 source device를 참조합니다.
- PDF overlay는 Windows/Android에서 읽을 수 있는 derived artifact로 생성합니다.
- Hub ACK 전 pending changes와 원본 파일을 자동 삭제하지 않습니다.
- 충돌이 발생하면 last-write-wins로 덮어쓰지 않고 양쪽 revision을 보존합니다.

## 구현 순서
1. Xcode project, SwiftUI shell, device enrollment
2. common API client와 offline data store
3. CourseMaterialSet 및 PDFKit viewer
4. PencilKit page annotation layer
5. annotation sync와 PDF overlay export
6. Share Extension, file import, background URLSession
7. Stage Manager, Split View, orientation, external keyboard 접근성 테스트

상세 계약은 [Multi-device Architecture](../../docs/MULTI-DEVICE-ARCHITECTURE.md), 화면 정책은 [UX Design](../../docs/UX-DESIGN.md)을 참고합니다.

실제 iPadOS 앱 소스와 Xcode project는 구현 이슈에서 생성합니다. 이 디렉터리 README만으로 빌드 가능한 앱을 의미하지 않습니다.
