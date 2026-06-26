# -*- coding: utf-8 -*-
"""로컬 SQLite 스키마.

수집(API)과 분석(findings)을 분리. "API는 채울 때만, 비교는 로컬" 원칙.
시군 코드(org/sborg)만 바꾸면 타 지자체로 확장되도록 ordinances에 보관.
"""
import sqlite3
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "gunpolaw.db"

SCHEMA = """
-- 자치법규(조례 등) : lnkOrg/목록/본문으로 채움
CREATE TABLE IF NOT EXISTS ordinances (
    mst          TEXT PRIMARY KEY,   -- 자치법규일련번호
    lid          TEXT,               -- 자치법규ID
    name         TEXT NOT NULL,
    knd          TEXT,               -- 종류(조례/규칙/…)
    org          TEXT,               -- 광역 지자체코드
    sborg        TEXT,               -- 시군구 지자체코드
    promulg_date TEXT,
    enforce_date TEXT,               -- 비교 기준일
    dept         TEXT,               -- 담당부서명(본문 API) — 과별 리포트 라우팅 축
    phone        TEXT,               -- 담당과 전화번호(본문 API) — 통지 연락처
    body_xml     TEXT,               -- 조례 원본 XML(서빙은 DB만 읽음)
    fetched_at   TEXT
);

-- 배치 스냅샷 메타 : 기준일·재사용 설정 (UI 기준일 배너·타 시군 재사용)
CREATE TABLE IF NOT EXISTS batch_meta (
    id           INTEGER PRIMARY KEY CHECK (id = 1),  -- 단일행
    org          TEXT,
    sborg        TEXT,
    region_name  TEXT,               -- 지자체명(표시용)
    batch_date   TEXT,               -- 배치 기준일(스냅샷 시점)
    ordinances_n INTEGER,
    laws_n       INTEGER,
    findings_n   INTEGER,
    deep         INTEGER,
    status       TEXT
);

-- 상위법령 현행본 : target=law 로 채움
CREATE TABLE IF NOT EXISTS laws (
    law_id     TEXT PRIMARY KEY,
    name       TEXT,
    body_xml   TEXT,
    fetched_at TEXT
);

-- 법령 조문 단위(변경 판정의 핵심) : 법령 본문 파싱
CREATE TABLE IF NOT EXISTS law_articles (
    law_id       TEXT,
    label        TEXT,               -- 제30조 / 제15조의2
    jo           INTEGER,
    ga           INTEGER,
    title        TEXT,
    enforce_date TEXT,               -- 조문시행일자
    moved_from   TEXT,               -- 조문이동이전(코드)
    moved_to     TEXT,               -- 조문이동이후(코드)
    changed      INTEGER,            -- 조문변경여부
    content      TEXT,
    PRIMARY KEY (law_id, label)
);

-- 조례 ↔ 법령 공식 연계 : lnkOrg(법령ID 직접)
CREATE TABLE IF NOT EXISTS ord_law_links (
    mst       TEXT,
    law_id    TEXT,
    law_name  TEXT,
    PRIMARY KEY (mst, law_id)
);

-- 인용(조례 본문 추출) : 조문 단위
CREATE TABLE IF NOT EXISTS citations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    mst          TEXT,
    article_no   TEXT,               -- 인용이 등장한 조례 조문
    law_name     TEXT,
    law_id       TEXT,               -- 연계로 보강(있으면)
    clause_label TEXT,               -- 인용된 상위법 조 라벨
    alias_source TEXT,               -- inline/carry_over/alias/None
    raw_text     TEXT
);

-- 변경 탐지 결과(1단계 산출)
CREATE TABLE IF NOT EXISTS findings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    mst             TEXT,
    law_id          TEXT,
    law_name        TEXT,
    clause_label    TEXT,
    category        TEXT,            -- timing / status / current
    severity        TEXT,            -- review(검토필요) / check(확인) / current
    change_type     TEXT,            -- 내용변경 / 번호이동 / 삭제 / 동일 (2단계)
    ord_clause      TEXT,            -- 인용한 조례 조문(제Y조) — 정비 위치
    ord_seq         INTEGER,         -- 조례 본문 내 등장 순서(정렬용)
    detail          TEXT,
    ord_enforce     TEXT,            -- 조례 시행일(당시 판단 기준일)
    old_enforce     TEXT,            -- 당시 시행본 법령 일자(판단 기준일)
    clause_enforce  TEXT,            -- 현행 조문 시행/개정일(판단 기준일)
    evidence        TEXT,
    cite_naked      INTEGER,         -- 1=「」 없이 맨몸으로 인용된 법령(서식 정비 병행 대상)
    created_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_cit_mst   ON citations(mst);
CREATE INDEX IF NOT EXISTS idx_find_mst  ON findings(mst);
CREATE INDEX IF NOT EXISTS idx_art_law   ON law_articles(law_id);
"""


def connect(db_path=DEFAULT_DB):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path=DEFAULT_DB):
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    # 기존 DB 호환: 신규 컬럼 보강 (없을 때만)
    fcols = [r[1] for r in conn.execute("PRAGMA table_info(findings)")]
    for name, decl in (("change_type", "TEXT"), ("ord_clause", "TEXT"),
                       ("ord_seq", "INTEGER"), ("old_enforce", "TEXT"),
                       ("cite_naked", "INTEGER")):
        if name not in fcols:
            conn.execute(f"ALTER TABLE findings ADD COLUMN {name} {decl}")
    ocols = [r[1] for r in conn.execute("PRAGMA table_info(ordinances)")]
    for name, decl in (("dept", "TEXT"), ("phone", "TEXT")):
        if name not in ocols:
            conn.execute(f"ALTER TABLE ordinances ADD COLUMN {name} {decl}")
    ccols = [r[1] for r in conn.execute("PRAGMA table_info(citations)")]
    for name, decl in (("span_start", "INTEGER"), ("span_end", "INTEGER"),
                       ("cite_naked", "INTEGER"), ("ord_seq", "INTEGER"),
                       ("cite_type", "TEXT")):
        if name not in ccols:
            conn.execute(f"ALTER TABLE citations ADD COLUMN {name} {decl}")
    conn.commit()
    conn.close()
    return db_path


if __name__ == "__main__":
    p = init_db()
    print("스키마 초기화 완료:", p)
