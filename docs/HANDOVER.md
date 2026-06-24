군포시 조례-상위법령 정합성 검토 시스템 v3.4 — 작업 인계

[프로젝트 목표]
군포시 자치법규 640건이 인용하는 상위 법령·조항이 조례 제정 이후
개정되었는지를 조항 단위로 자동 검증해서 "기계적 일괄 개정 가능 후보"를
식별. 연말 시연용.

[작업 환경]
- 경로: C:\probe_v3\
- 파일: file1.py (Phase 1~4+규칙엔진) / phase6.py (조항 검증) / gunpo_ui_v2.html (UI)
  ※ file1.py는 gunpo_ui_v2.html을 serve하므로 v3 HTML도 v2 이름 유지
- 실행: cd C:\probe_v3 && python file1.py → http://localhost:8765
- OC 키: youzen618
- DB: gunpo_ordinances.db (SQLite)
- 환경: Windows cmd (sh 문법 주의: # 주석 안 됨)

[현재 v3.4 상태 — 모두 정상 동작 검증 완료]
Phase 1~4: 자치법규 전수 수집 + 본문 파싱 + 인용 추출 + 최신화 점검
Phase 5  : 규칙 엔진 통합 (R001~R015 양식 A/B/C 내장)
           - file1.py의 extract_all_refs()에 직접 녹여넣음
           - 약칭 inline 정의(R006) + 정의 직후 조항(R006X)
           - carry-over (R001/R015 "같은 법", "같은 법 시행령")
           - 메타 부착: quantifier(R009), exclusion(R011), proviso(R014),
             byeolpyo(R010), range_expanded(R003), alias_source(R007)
           - 분류 트랙 3개: excluded_refs / local_ordinance_refs / byeolpyo_track
Phase 6  : 조항 단위 시점 검증 (phase6.py 무수정)
           - tokenize_clauses: "제30조부터 제32조까지" → [30,31,32] 전개
           - check_clauses_batch + get_old_and_new

[UI 상태 — STEP 2 좌우 분할 뷰 완성]
좌측: 조례 원문 (조문별 카드)에 「법령명」+조항 표현을 색칠 mark로 감쌈
우측: 인스펙터 — 클릭 시 법령/조항/메타 배지/원문 인용 표현/검증 결과 표시
인스펙터 [⚖️ 이 인용 즉시 검증] 버튼 → phase6 호출 → 본문 색이 빨/노/녹으로 변경

색상 범례:
- 노랑(yellow-300) = 법령 미검증
- 보라(purple-300) = 자치법규 (검토 대상 외)
- 빨강(red-300) = 위험(critical)
- 호박(amber-300) = 검토권고(warning)
- 초록(green-300) = 최신(current)

[골든케이스 검증 결과]
「군포시 지적재조사위원회 등 구성 및 운영에 관한 조례」 (시행 2013-12-01)
인용: 「지적재조사에 관한 특별법」 제30조 / 제31조 / 제32조
판정: 모두 🚨 4,415일 뒤 개정 (2017/2020/2024 3회)

[법제처 OpenAPI 탐사 결과 — 재탐사 불필요]
살아있는 API:
- law      / lawService.do   ID= 또는 LM=  (본문 + 각 조문에 <조문시행일자> 박혀있음)
- eflaw    / lawService.do   ID=          (시행일자별 본문)
- eflaw    / lawSearch.do    query=       (시행일자별 법령 목록)
- oldAndNew/ lawService.do   ID=          (신구법 비교 본문)
- ordin    / lawSearch.do    org+sborg+knd (자치법규 검색)

죽은 API (OC키 권한 없음): lsHstInf, lsHst, lsJoHst, joHst, dayJoHst,
                          delegated, lsSysDgm, lsAbbrv, abbrLs
efYd 파라미터: 본문 조회엔 무력 (검색에선 작동)

핵심 발견: lawService.do?target=law&ID= 응답의 각 <조문단위>에
<조문시행일자>, <조문이동이전>, <조문이동이후>, <조문변경여부>가 박혀있어
efYd 없이도 조항 시점 검증 가능 → 이게 Phase 6의 작동 원리

[알려진 사용자 환경 이슈]
복사·붙여넣기 과정에서 정규식 리터럴(/.../) 안의 백슬래시+대괄호 조합이
$ 기호로 깨지는 경우 있었음. 해결책: new RegExp(문자열) 방식 + 한글은 그대로
string으로. buildHitSpans 함수가 이 방식으로 작성돼 있으니 참고.

[다음에 하고 싶은 것 — 사용자가 다음 창에서 지시]
시연 사용자 니즈 = "이 조례를 이렇게 개정해야 한다"는 행동 지시서 출력.
(현재는 데이터 분석가 뷰. 개정 권고서 + 등급 분류는 미구현)

candidate 작업 후보:
A) STEP 4 보고서를 "개정 권고서"로 재설계
   - 등급 분류: 🔧 기계적 개정 / ⚠️ 실질 검토 / 📋 확인 / ✅ 현행 유지
   - 권고 문안 초안 자동 생성
   - python-docx로 결재 가능 .docx 다운로드
B) STEP 1 상단에 "일괄 개정 후보 위젯" — 640건 중 N건 자동 분류
C) 분할 뷰 인스펙터 개선 — 신구법 비교 인라인 표시 등

[작업 원칙]
- file1.py / phase6.py / gunpo_ui_v2.html 단일 파일 유지 (별도 모듈 분리 X)
- DB 스키마 무변경
- 기존 함수 시그니처 유지
- 시연 시간 3-5분 내 설명 가능한 규모

지금부터 ___ 작업 시작해줘.
