# CampusLife UX / UI 디자인 기준

## 디자인 방향

CampusLife는 예린이가 대학 생활에서 자료를 모으고, 일정을 정리하고, 학습하는 개인 캠퍼스 허브다. 부드러운 라벤더 계열 강조색과 밝은 중립 배경을 사용하되 장식보다 정보 밀도, 가독성, 빠른 캡처를 우선한다.

- 제품명: CampusLife
- 개인화 표시: Yerin's Campus Hub / 예린
- 톤: 친근하지만 유아적이지 않은 대학생용 생산성 UI
- 주요 액션: 자료 업로드, 빠른 기록, 강의 녹음 시작
- 디자인 데이터는 샘플이며 실제 시간표나 예린이의 실제 개인정보가 아니다.

## Fold8의 두 가지 핵심 사용 모드

### Cover display — 접은 상태

한 손으로 짧게 확인하고 기록하는 화면이다.

- 폭이 좁은 단일 컬럼 레이아웃
- 상단: 현재 위치, 검색, 알림, 프로필
- 첫 화면: 다음 강의, 녹음 시작, 빠른 기록
- 과목/강의/자료 통계는 2열 요약 카드
- 나머지 콘텐츠는 세로 스크롤
- 하단 고정 내비게이션: 홈, 시간표, Inbox, 일정, AI 학습
- 녹음 시작처럼 중요한 터치 액션은 엄지로 누르기 쉬운 위치에 둔다.
- 녹음 상태와 업로드 상태는 화면 전환 후에도 유지한다.

### Inner display — 펼친 상태, 4:3 디자인 기준

작은 태블릿처럼 여러 정보를 함께 읽고 작업하는 화면이다.

- 목표 디자인 캔버스는 4:3이며 실제 앱은 window bounds를 따른다.
- 좌측: 축약형 Navigation Rail
- 상단: breadcrumb, 검색, 화면 모드, 프로필
- 중앙: 오늘의 시간표와 강의/자료 콘텐츠
- 우측: 과제·마감일, 빠른 실행, 진행 중 작업
- 하단/보조 영역: 최근 수집 자료, 과목 현황
- 강의 상세에서는 파일 목록과 AI 노트를 나란히 배치하는 2-pane/3-pane 구조
- 넓어진 공간을 단순 여백으로 남기지 않고 비교·참조·동시 작업에 활용
- 높이가 낮거나 분할 화면에서는 패널을 단일 컬럼으로 재배치

## 반응형 원칙

HTML 프로토타입은 PC → Fold8 접은 화면 → Fold8 펼친 4:3 화면을 상단 버튼으로 미리보기한다. 이는 디자인 검토용 시뮬레이터이며 실제 기기의 접힘 상태 감지 기능은 아니다.

Android 실 구현에서는 기기 모델명이나 고정 화면 비율을 하드코딩하지 않는다. 앱이 실제로 사용할 수 있는 window bounds를 기준으로 레이아웃을 동적으로 선택한다.

권장 Compose 구현:
- androidx.compose.material3.adaptive의 currentWindowAdaptiveInfo() 및 WindowSizeClass 사용
- Compact width: 단일 컬럼, bottom navigation, 빠른 캡처 중심
- Medium width: Navigation Rail + 2-pane 후보. 높이도 함께 고려
- Expanded 이상: Navigation Rail/Drawer + 2~3-pane 작업공간
- WindowInfoTracker / FoldingFeature로 fold posture와 hinge bounds를 고려
- 창 크기/방향/멀티윈도우/접힘 변경 중 선택 과목, 스크롤, 녹음 세션, 입력 내용을 보존
- 힌지 영역이나 접힘으로 가려지는 곳에 주요 버튼과 텍스트를 배치하지 않음
- 4:3은 펼침 UI의 디자인 목표 비율이며 실제 앱은 시스템 window bounds를 따른다.

Android 공식 가이드:
- https://developer.android.com/develop/adaptive-apps/guides/use-window-size-classes
- https://developer.android.com/develop/adaptive-apps/guides/foldables/make-your-app-fold-aware

Window size class는 기기의 종류가 아니라 앱이 사용할 수 있는 window 크기로 결정되고, fold/unfold, 회전, 멀티윈도우 중에도 변경될 수 있다. 따라서 화면 전환을 단발성 기기 분기로 만들지 말고 상태를 유지하는 동적 적응으로 구현한다.

## 화면 구성

- Dashboard: 다음 강의, 녹음 CTA, 오늘 시간표, 할 일, 최근 자료
- Schedule: 주간 시간표와 수업별 빠른 진입
- Courses: 과목 목록 및 강의별 자료 묶음
- Inbox: 강의/캠퍼스/기회/개인 자료를 한데 모으는 통합 수집함
- Planner: 과제, 시험, 마감, 행사와 개인 할 일
- AI Study: 원본 근거를 함께 보여주는 과목별 질문/복습
- Campus Life: 공지, 행사, 동아리, 장학금과 인턴십

## 프로토타입 실행

- apps/prototype/index.html을 브라우저에서 연다.
- 상단 기기 보기 버튼으로 PC / Fold8 접은 화면 / Fold8 펼친 4:3 화면이 순환한다.
- 메뉴 이동, 파일 선택/드롭, 할 일 체크, 빠른 메모, 예시 AI 질문은 브라우저 내 데모 동작이다.
- 실제 녹음, SFTP 동기화, AI API, 영구 데이터 저장소와 연결된 기능은 아니다.

## 디자인 검수 항목

- [ ] 접은 화면에서 녹음 시작과 빠른 기록을 한 손으로 쉽게 누를 수 있다.
- [ ] 접은 화면에서 긴 제목과 파일명이 깨지지 않고 줄바꿈/말줄임 처리된다.
- [ ] 펼친 4:3 화면에서 Navigation Rail이 콘텐츠 폭을 과도하게 차지하지 않는다.
- [ ] 펼친 화면에서 시간표/할 일/최근 자료를 동시에 확인할 수 있다.
- [ ] 강의 상세의 파일 목록과 AI 노트를 나란히 볼 수 있다.
- [ ] 화면 전환 중 선택 과목·녹음 상태·입력 내용이 유지된다.
- [ ] Split-screen/가로 방향/높이가 낮은 창에서 과도한 다중 패널을 피한다.
- [ ] 터치 영역, 글자 크기, 색상 대비가 모바일 사용에 적합하다.


## iPad Pro 13 — Study Workspace

iPad Pro 13은 단순한 모바일 대시보드가 아니라 CampusLife의 정식 학습 단말입니다.

기본 가로 레이아웃:
- 왼쪽 sidebar: 학기, 과목, 강의, Inbox
- 중앙 document canvas: PDF/PPT/PPT 변환 자료
- 오른쪽 inspector: AI 강의 노트, 예린이의 메모, 자료 출처
- 선택 가능한 하단/보조 패널: 녹음 타임라인, 페이지 썸네일, 과제
- Apple Pencil 사용 시 문서 위에 직접 필기하되 원본 파일을 변경하지 않음

### iPad 주요 동작

- 강의 자료와 AI 요약을 나란히 비교
- PDF 페이지에서 Apple Pencil로 freehand drawing, highlight, text note
- 필기 레이어를 문서/페이지별로 분리 저장
- Files/Share Extension에서 PDF, PPTX, HWPX, 이미지, 링크를 Inbox로 가져오기
- 키보드와 포인터, drag and drop 지원
- 오프라인으로 내려받은 강의 자료와 기존 노트 열람/편집
- 연결 복구 시 변경사항을 공통 Sync API로 전송

### 세부 설계 원칙

- iPad 앱은 SwiftUI 기반 native client로 구현한다.
- Pencil 입력/편집 데이터는 PencilKit을 사용한다.
- PDF 열람과 page navigation은 PDFKit을 사용한다.
- PencilKit 원본 drawing 데이터와 공통 PDF overlay export를 함께 보존한다.
- 원본 PDF에 필기를 직접 덮어쓰지 않는다.
- Split View, Stage Manager, 창 크기 변경, portrait/landscape를 지원한다.
- iPad 13-inch의 특정 해상도나 고정 aspect ratio를 하드코딩하지 않는다.
- Regular/Compact size class와 실제 window bounds에 따라 navigation, document canvas, inspector를 재배치한다.
- Fold8 Inner와 iPad가 모두 넓은 화면을 제공하더라도 각 플랫폼의 입력 방식과 navigation 관습에 맞춰 별도 최적화한다.

### 기기별 디자인 미리보기

프로토타입 상단의 기기 보기 전환은 다음을 제공한다.
1. PC Desktop
2. Fold8 Cover
3. Fold8 Inner (4:3 design canvas)
4. iPad Pro 13 (4:3 study workspace)

iPad 모드는 4:3 디자인 미리보기이며 실제 iPad 화면 크기/Stage Manager 창 크기를 대체하지 않는다.
