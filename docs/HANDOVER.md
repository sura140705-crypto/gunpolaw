# gunpolaw 인계·이어가기 가이드

군포시 자치법규(조례·규칙)가 인용한 **상위 법령·행정규칙 조항이 조례 시행 이후 개정·삭제됐는지**를
조문 단위로 자동 판정해, 정비가 필요한 조례·조문을 담당 부서(과)별로 보여주는 읽기전용 대시보드.
다른 환경에서 작업을 이어갈 때 이 문서 하나로 구조·재구성·배포를 파악할 수 있게 정리한 것.

---

## 1. 실행 환경 / 설치

- **Python 3** (3.9+). **표준 라이브러리만 사용 — 외부 의존성 0** (`requirements.txt` 비어 있음이 정상).
- OS 무관(개발은 Windows). DB는 SQLite 파일 `gunpolaw.db`(리포에 동봉돼 있어 재구성 없이 바로 서빙 가능).

```bash
python -m gunpolaw --serve 8765     # 대시보드 → http://127.0.0.1:8765/  (키·인터넷 불요)
```

---

## 2. 코드 구조 (gunpolaw/ 패키지)

수집(네트워크)과 분석·서빙(로컬)을 분리한 단일 엔진. "API는 채울 때만, 판정은 로컬 DB로".

| 모듈 | 역할 |
|---|---|
| `config.py` | 대상 지자체 설정(org/sborg/region_name/knd_codes/local_prefix). env > region.json > 기본값(군포시). |
| `moleg.py` | **법제처 OpenAPI 클라이언트(유일한 네트워크 경계, OC키 사용)**. 아래 §5 참조. |
| `db.py` | SQLite 스키마·연결·경량 마이그레이션. |
| `extract.py` | 조례 본문에서 인용 추출(「법령」·"같은 법" carry-over·맨몸·inline 약칭) + `group_by_law`. |
| `clauses.py` | 조·항·호·목 토큰화(`tokenize_clauses`) — 인용 표준(§6) 준수, 항/목 나열 전개. |
| `parse.py` | 법령/행정규칙/조례 본문 XML 파싱, 신구조문 대비 파싱, 조라벨(`제N조[의M]`). |
| `checks.py` | 조항 단위 시점·내용 판정(`check_clause`/`diff_clause`), 등급 도출, 호/목 subunit 추출, 조사(`_josa`). |
| `pipeline.py` | 조례 1건 분석 오케스트레이션(인용→법령ID 해소→비교→findings). 법령 실패 시 행정규칙 폴백·신구법비교. |
| `history.py` | 법령 시행일별 버전 목록/선택(`as_of`) — deep(당시 시행본) 비교용. |
| `batch.py` | 전수 수집·분석, findings/citations/laws 영속, 개정 델타 감지(역추적), `persist_result`(공용). |
| `reparse.py` | **DB-only 재분석**(`_DBSource`, API 0) + 신선도 증분 수집(`StaleAwareSource`). |
| `report.py` | 등급 메타, 권고 모델(`build_model`), HTML 권고서(`render_html`)·조각(`recommend_fragment`)·CSV(`model_to_csv`/`grade_to_csv`), 행동지시(`action_text`/`action_html`). |
| `serve.py` | http.server 대시보드(`DASHBOARD_HTML` 단일 문자열)·JSON API·정적 내보내기(`export_static`)·관리자(`ADMIN_HTML`). |
| `diag.py` | `/admin` 판정 근거 재실행 추적(검토 로직을 사람이 눈으로 검증). |
| `__main__.py` | CLI 진입점. |

**HTML/CSS/JS는 별도 파일이 없다.** 대시보드는 `serve.py`의 `DASHBOARD_HTML`(문자열: `<style>`+`<body>`+`<script>`),
권고서는 `report.py`의 `render_html`+`_CSS`. 오프라인 제약상 외부 CDN·웹폰트 금지, 이미지는 data-URI 인라인
(예: 헤더 군포시 마크 = `gunpolaw/gunpo_logo.png`를 import 시 base64로 주입).

---

## 3. DB 재구성 프로세스 (API로 새로 구성)

`gunpolaw.db`는 리포에 있지만, 처음부터 다시 만들려면:

1. **OC 키 발급** — 국가법령정보 공동활용(open.law.go.kr)에서 신청 → 환경변수로 주입(소스·DB에 저장 금지):
   ```bash
   set LAW_OC_KEY=발급받은_OC_키          # (Windows cmd)  /  export LAW_OC_KEY=...  (bash)
   ```
2. **(선택) 대상 지자체** — 군포시가 기본. 바꾸려면 `region.json` 또는 env:
   ```bash
   set LAW_ORG=6410000        # 광역(경기도)
   set LAW_SBORG=4020000      # 시군(군포시)
   set LAW_REGION=군포시
   set LAW_LOCAL_PREFIX=경기도,안양시,의왕시   # 자치법규 인식 접두어(쉼표구분)
   ```
3. **전수 수집·분석** (라이브 API 호출, 시간 소요):
   ```bash
   python -m gunpolaw --batch --deep      # 목록→본문→인용→상위법 본문(+deep=당시 시행본)까지 받아 findings 생성
   # 증분: --incr(있으면 재사용, 신규만) 또는 --max-age 7(7일 이내 재사용)
   ```
   → `gunpolaw.db`에 ordinances/citations/findings/laws/law_versions/ord_law_links/law_change_log 적재.
4. **코드·파서 수정 후 재적용** (API 0, DB 본문으로 재분석 — 판정 로직 바꿀 때마다):
   ```bash
   python -m gunpolaw --reparse
   ```
5. **서빙 / 내보내기**:
   ```bash
   python -m gunpolaw --serve 8765          # 로컬 대시보드
   python -m gunpolaw --export-static site  # 정적 사이트(서버 없이 호스팅)
   ```

핵심 원칙: **원본 본문(ordinances.body_xml, laws, law_versions)은 보존**하고, 파생물(findings/citations/law_articles)만
매 분석마다 재생성. 그래서 파서·판정 로직을 바꿔도 `--reparse`로 재수집 없이 반영된다(라이브 배치 == reparse 불변식).

---

## 4. 주요 CLI 명령

```
python -m gunpolaw <MST> [--deep]        조례 1건 분석
python -m gunpolaw --batch [N] [--deep] [--incr|--max-age D]   전수 수집·분석
python -m gunpolaw --reparse             DB body_xml로 재파싱(API 0)
python -m gunpolaw --serve [포트]        대시보드 서빙(기본 8765)
python -m gunpolaw --export-static [폴더]  정적 사이트 생성
python -m gunpolaw --export-share [zip]  공유용 슬림 zip(코드+보기DB)
python -m gunpolaw --report              저장 결과 집계
python -m gunpolaw --changes [--all]     법령 개정 → 영향 조례 역추적
python -m gunpolaw --ack <law_id> [--unack]   개정 검토완료 표시/해제
python -m gunpolaw --recommend [--mst <MST>] [경로]   권고서 HTML 생성
```

---

## 5. 법제처 OpenAPI 사용 현황 (moleg.py)

모든 라이브 호출은 `moleg.call(endpoint, params)` 한 곳으로 흐른다. `www.law.go.kr/DRF/{endpoint}` + `OC=키`.

| target | endpoint | 용도 |
|---|---|---|
| `ordin` | lawSearch.do | 자치법규 목록(org+sborg+knd) / lawService.do = 조례 본문 |
| `lnkOrg` | lawSearch.do | 지자체 자치법규-법령 공식 연계(법령ID 보강 + 역추적 '연계' 리콜) |
| `law` | lawSearch.do(해소)·lawService.do(본문) | 상위법령. 각 조문에 `<조문시행일자>` 있어 efYd 없이 시점 판정 |
| `eflaw` | lawService.do·lawSearch.do | 시행일자별 법령본(deep=당시 시행본, history.py) |
| `admrul` | lawSearch.do(정확매칭)·lawService.do(본문) | 행정규칙(훈령·예규·고시) — 법령 해소 실패 시 폴백 |
| `admrulOldAndNew` | lawService.do | 행정규칙 신구조문 대비(개정 전/후) — admrul 개정 탐지 |

지능형 검색(AIS/Lawbot): 자동 해소 실패 법령의 검토 항목에 `www.law.go.kr/LSW/ais/searchList.do?query=<법령명>`
'🔍 지능형 검색' 링크 부착(사람이 유사·예고·폐지 법령까지 직접 확인).

---

## 6. 대한민국 법령 인용 표준 (파서가 준수)

위계: 조(제N조) → 항(①②=제N항) → 호(1.=제N호) → 목(가.=**가목**, '제' 안 붙임).
- 결합 인용은 **붙여쓰기**: `제1조제2항제3호가목`.
- **항 생략(단일 조문)**: ① 없이 바로 호면 `제2조제3호`로 인용, 없는 제1항을 만들지 않음.
- 위계 비가역성: 항→(호 없이)→목 불가. 항·목 나열(`제1항 및 제2항`, `나목 및 다목`)은 각 단위로 전개.

---

## 7. 배포 (두 리포)

- **코드**(비공개): `github.com/sura140705-crypto/gunpolaw` (master).
- **정적 사이트**(공개): `github.com/sura140705-crypto/gunpolaw-view` (main) → GitHub Pages.
  ```bash
  cd /c/gunpo_law && git add -A && git commit -m "..." && git push        # 코드
  python -m gunpolaw --export-static site
  cd site && git add -A && git commit -m "갱신" && git push               # 정적 사이트
  ```
  공개 주소: https://sura140705-crypto.github.io/gunpolaw-view/

---

## 8. 제약 / 주의

- **stdlib only**(외부 라이브러리 금지), **SQLite 단일 DB**, **대시보드 단일 HTML**(외부 CDN·웹폰트 금지, 오프라인).
- PDF는 라이브러리 없이 **브라우저 인쇄→PDF 저장**(권고서의 `document.title`이 `과-조례명(날짜)`로 세팅됨).
- **보안**: OC 키는 env(`LAW_OC_KEY`)로만. 과거 커밋 히스토리에 키 흔적이 있어, 공개 전환 시 **히스토리 스크럽 + 키 재발급** 선행 필요(현재 비공개 전제).
- 판정은 자동 분석이라 완전하지 않음 — 대시보드·권고서·CSV에 "담당자 최종 확인 필수" 고지 내장.

## 9. 관련 문서

- 상세 구조/설계: [`프로젝트_구조.md`](프로젝트_구조.md)
- 공유 배포 안내: [`../테스트_공유_안내.md`](../테스트_공유_안내.md)
- 과거 설계 문서(구식, 참고용): `archive/`
