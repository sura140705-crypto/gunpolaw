# 군포시 자치법규 정비 점검 (gunpolaw)

군포시 자치법규(조례·규칙 등)가 인용한 **상위 법령 조항이 조례 시행 이후 개정되었는지**를
조문 단위로 자동 점검하고, 정비가 필요한 부분을 권고하는 도구입니다.

**오프라인 우선**: 한 번의 수집으로 필요한 데이터를 모두 로컬 SQLite(`gunpolaw.db`)에 적재한 뒤,
이후 보기·검증·리포트·역추적은 **DB만 읽어** 동작합니다(라이브 API·인증키 불요).

## 두 가지 역할

1. **수집(운영자)** — 법제처 OpenAPI로 데이터 적재. **본인 OC 키 필요**. 주 1회 정도 재생성.
2. **보기(테스터·담당자)** — 적재된 DB로 대시보드·권고서 열람. 키·인터넷 불요.

## 설치

- **Python 3** (표준 라이브러리만 사용 — 외부 의존성 0)

## 사용

**수집 (OC 키 필요)**
```bash
set LAW_OC_KEY=발급받은_OC_키
python -m gunpolaw --batch [--deep] [--incr|--max-age D]  # 전수/증분 수집·분석
python -m gunpolaw --reparse           # 코드 수정 후 재분석(라이브 API 0)
python -m gunpolaw --recommend [경로]  # 전체 개정 권고서 HTML
python -m gunpolaw --export-share      # 공유용 슬림 zip 생성(코드+보기전용 DB+안내문)
```

**보기 (키 불요)**
```bash
python -m gunpolaw --serve [포트]            # 대시보드 → http://127.0.0.1:8765/
python -m gunpolaw --recommend --mst <MST>   # 조례 1건 분석 권고서 HTML
python -m gunpolaw --changes [--all]         # 법령 개정 → 영향 조례(역추적)
```

대시보드: 담당과 필터·**조례명/인용법령 검색**, 조례 통합뷰(본문 **인용 밑줄**·당시↔현행 diff·
**상위법령 정보 카드**), **조례별 분석 권고서**, 법령 개정 역추적 알림(검토완료 표시),
`/admin` **판정 근거 검사**(검토 로직 재실행 추적).

## 대상 지자체 교체

코드 수정 없이 환경변수 `LAW_ORG`/`LAW_SBORG`/`LAW_REGION`/`LAW_LOCAL_PREFIX` 또는
`region.json` 로 교체 (`gunpolaw/config.py`).

## 배포 모델

- **코드**(비공개): `sura140705-crypto/gunpolaw`. `gunpolaw.db`(전체 DB)가 함께 커밋돼 있어
  받은 즉시 `--serve`/`--reparse` 가능. DB를 API로 새로 만들려면 → [`docs/HANDOVER.md`](docs/HANDOVER.md) §3.
- **정적 사이트**(공개): `sura140705-crypto/gunpolaw-view` → `--export-static site` 결과를 푸시,
  GitHub Pages로 서빙(https://sura140705-crypto.github.io/gunpolaw-view/).
- 공유용 슬림 zip: `--export-share`(코드+보기전용 DB+안내문). 슬림 DB는 상위법령 원문을 비운
  보기 전용이라 `--reparse`·`/admin` 정밀 재실행은 전체 DB에서만 동작.

## 보안 주의

- OC 키는 소스·리포에 저장하지 않습니다(환경변수 `LAW_OC_KEY`).
- ⚠️ **과거 베이스라인 커밋 히스토리에 키 흔적이 있어, 공개 전환 시 히스토리 스크럽 +
  키 재발급이 선행되어야 합니다.** 현재는 비공개 리포 전제.

## 문서

- **인계·이어가기 가이드(권위)**: [`docs/HANDOVER.md`](docs/HANDOVER.md) — 구조·DB 재구성·배포·API·인용표준
- 상세 구조: [`docs/프로젝트_구조.md`](docs/프로젝트_구조.md)
- 배포 안내(공유 zip): [`테스트_공유_안내.md`](테스트_공유_안내.md)
- 과거 설계 문서(구식, 참고용): [`docs/archive/`](docs/archive/)

## 개발

```bash
# 테스트(표준 unittest 스타일, pytest 불요)
for t in tests/test_*.py; do python "$t"; done
```
