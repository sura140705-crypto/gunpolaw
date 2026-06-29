# -*- coding: utf-8 -*-
"""오프라인 재파싱 — 영속된 body_xml만으로 findings·law_articles·citations 재생성.

파서(moleg.parse_law_articles 등)를 고친 뒤, 법제처 API를 한 번도 호출하지 않고
DB의 원본 XML(ordinances.body_xml / laws.body_xml / law_versions.body_xml)을
다시 파싱·판정해 산출물만 갈아끼운다. 30분 재배치 → 수 초.

전제: 직전 배치가 body_xml(현행+당시본)을 영속했을 것. 새 법령·새 당시본이 필요한
경우(인용이 새로 생긴 미수집 법령)는 재수집(--batch)이 필요하다.
"""
from datetime import datetime, timedelta

from . import db
from . import moleg
from .pipeline import analyze_ordinance, LiveSource
from .batch import persist_result, _now


class _DBSource:
    """analyze_ordinance용 본문 출처를 DB(body_xml)로 갈아끼운 것(라이브 API 0).

    LiveSource와 같은 인터페이스(get_ordinance_body/resolve_law_id/get_law_body/
    list_versions/body_with_xml_by_mst)를 DB 읽기로 구현.
    """

    def __init__(self, conn):
        self.conn = conn
        # 법령명(정규화) → 법령ID. 라이브 resolve_law_id(API 퍼지검색)는 여러 이름 변형을
        # 같은 ID로 풀지만, laws.name엔 '처음 받은 이름' 하나만 남는다. 직전 배치의 '실제
        # 해소 결과'는 findings(law_name→law_id)에 전부 들어 있으므로 그걸 1순위로 쓴다.
        self._index = {}
        # 1순위: citations.law_id — 배치가 새긴 해소 결과(조문 없는 법명-only 인용도 포함)
        for r in conn.execute(
                "SELECT DISTINCT law_name, law_id FROM citations "
                "WHERE law_id IS NOT NULL AND law_id != ''"):
            if r["law_name"]:
                self._index[r["law_name"].replace(" ", "")] = r["law_id"]
        # 2순위: findings(법명→ID) — citations.law_id 미적재 구 DB 호환
        for r in conn.execute(
                "SELECT DISTINCT law_name, law_id FROM findings "
                "WHERE law_id IS NOT NULL AND law_id != ''"):
            if r["law_name"]:
                self._index.setdefault(r["law_name"].replace(" ", ""), r["law_id"])
        for r in conn.execute("SELECT law_id, name FROM laws WHERE name IS NOT NULL"):
            self._index.setdefault(r["name"].replace(" ", ""), r["law_id"])
        for r in conn.execute("SELECT law_id, law_name FROM ord_law_links"):
            if r["law_name"] and r["law_id"]:
                self._index.setdefault(r["law_name"].replace(" ", ""), r["law_id"])

    def get_ordinance_body(self, mst):
        r = self.conn.execute(
            "SELECT name, enforce_date, body_xml FROM ordinances WHERE mst=?",
            (str(mst),)).fetchone()
        if not r or not r["body_xml"]:
            return {"error": "body_xml 미영속(재배치 필요)"}
        parsed = moleg.parse_ordinance_body(r["body_xml"])
        if "error" not in parsed:
            # 시행일은 목록 API 기준의 DB값을 정본으로(본문 XML과 어긋날 때 대비)
            parsed["meta"]["enforce_date"] = r["enforce_date"] or parsed["meta"].get("enforce_date", "")
        return parsed

    def resolve_law_id(self, name):
        return self._index.get(name.replace(" ", ""))

    def get_law_body(self, law_id):
        r = self.conn.execute("SELECT body_xml FROM laws WHERE law_id=?",
                              (str(law_id),)).fetchone()
        return (r["body_xml"] if r else "") or ""

    def list_versions(self, law_name, law_id=None):
        # 적재된 당시본만으로 목록 구성 — as_of(시행일<=조례)가 원배치와 같은 버전을 고른다
        return [{"mst": r["version_mst"], "enforce_date": r["enforce_date"], "law_id": law_id}
                for r in self.conn.execute(
                    "SELECT version_mst, enforce_date FROM law_versions WHERE law_id=?",
                    (str(law_id),))]

    def body_with_xml_by_mst(self, mst):
        r = self.conn.execute("SELECT body_xml FROM law_versions WHERE version_mst=?",
                              (str(mst),)).fetchone()
        xml = (r["body_xml"] if r else "") or ""
        return moleg.parse_law_articles(xml), xml


class StaleAwareSource:
    """신선도 기반 수집 출처 — fetched_at 이 max_age_days 이내면 DB 재사용, 지나면 라이브 재수집.

    max_age_days=0    → 전부 재수집(전체 배치; 법령 개정 재검출).
    max_age_days=None → 무한 재사용(있으면 무조건 DB, 신규만 라이브 = 증분).
    그 외 N           → N일 이내 수집분 재사용, 오래된 것만 재수집(주1회 갱신=7).

    당시 시행본(law_versions)은 불변 과거본이라 본문은 항상 DB 우선(미스만 라이브).
    버전 '목록'은 현행본이 신선하지 않을 때만 라이브로 갱신(새 개정분 포함).
    재사용분의 fetched_at 은 보존해야 신선도 판단이 망가지지 않음 → run_batch 가 사후 복원.
    """

    def __init__(self, conn, max_age_days=0, live=None):
        self.conn = conn
        self.max_age = max_age_days
        self._db = _DBSource(conn)
        self._live = live or LiveSource
        self.reused_law_at = {}    # {law_id: 보존할 fetched_at}
        self.reused_ord_at = {}    # {mst: 보존할 fetched_at}
        self.misses = {"ordinance": 0, "law": 0, "version": 0,
                       "ordinance_reused": 0, "law_reused": 0}
        self._cutoff = None
        if isinstance(max_age_days, (int, float)) and max_age_days > 0:
            self._cutoff = datetime.now() - timedelta(days=max_age_days)

    def _fresh(self, fetched_at):
        if self.max_age is None:            # 무한 — 있으면 재사용
            return bool(fetched_at)
        if self._cutoff is None:            # 0/음수 — 항상 재수집
            return False
        if not fetched_at:
            return False
        try:
            return datetime.fromisoformat(fetched_at) >= self._cutoff
        except ValueError:
            return False

    def get_ordinance_body(self, mst):
        row = self.conn.execute(
            "SELECT fetched_at FROM ordinances WHERE mst=?", (str(mst),)).fetchone()
        if row and self._fresh(row["fetched_at"]):
            r = self._db.get_ordinance_body(mst)
            if "error" not in r:
                self.reused_ord_at[str(mst)] = row["fetched_at"]
                self.misses["ordinance_reused"] += 1
                return r
        self.misses["ordinance"] += 1
        return self._live.get_ordinance_body(mst)

    def resolve_law_id(self, name):
        # 법령ID는 불변 — DB 인덱스(직전 해소 결과)를 신뢰, 미수록 신규명만 라이브 검색.
        return self._db.resolve_law_id(name) or self._live.resolve_law_id(name)

    def get_law_body(self, law_id):
        row = self.conn.execute(
            "SELECT body_xml, fetched_at FROM laws WHERE law_id=?", (str(law_id),)).fetchone()
        if row and row["body_xml"] and self._fresh(row["fetched_at"]):
            self.reused_law_at[str(law_id)] = row["fetched_at"]
            self.misses["law_reused"] += 1
            return row["body_xml"]
        self.misses["law"] += 1
        return self._live.get_law_body(law_id)

    def list_versions(self, law_name, law_id=None):
        have = self._db.list_versions(law_name, law_id)
        row = self.conn.execute(
            "SELECT fetched_at FROM laws WHERE law_id=?", (str(law_id),)).fetchone()
        if have and row and self._fresh(row["fetched_at"]):
            return have                     # 현행본 신선 → 저장 버전목록 재사용
        return self._live.list_versions(law_name, law_id) or have

    def body_with_xml_by_mst(self, mst):
        arts, xml = self._db.body_with_xml_by_mst(mst)   # 당시본은 불변 → DB 우선
        if xml:
            return arts, xml
        self.misses["version"] += 1
        return self._live.body_with_xml_by_mst(mst)


def reparse_all(db_path=db.DEFAULT_DB, deep=None, verbose=True):
    """DB의 body_xml로 findings·law_articles·citations 전건 재생성(API 0).

    deep=None이면 직전 배치의 deep 설정(batch_meta)을 따른다.
    """
    db.init_db(db_path)
    conn = db.connect(db_path)
    meta = conn.execute("SELECT deep FROM batch_meta WHERE id=1").fetchone()
    if deep is None:
        deep = bool(meta["deep"]) if meta else False

    src = _DBSource(conn)
    msts = [r["mst"] for r in conn.execute(
        "SELECT mst FROM ordinances WHERE body_xml IS NOT NULL AND body_xml != ''")]
    if verbose:
        print(f"오프라인 재파싱: 조례 {len(msts)}건 (deep={deep}, 라이브 API 0)")

    # 파싱 산출물만 비운다 — 원본 XML(laws/law_versions/ordinances.body_xml)은 보존
    conn.execute("DELETE FROM findings")
    conn.execute("DELETE FROM citations")
    conn.execute("DELETE FROM law_articles")
    conn.commit()

    law_cache, version_cache, old_cache, agg = {}, {}, {}, {}
    name_cache = {}
    errors = 0
    for i, mst in enumerate(msts, 1):
        res = analyze_ordinance(mst, link_index={}, law_cache=law_cache, deep=deep,
                                version_cache=version_cache, old_cache=old_cache, src=src,
                                law_name_cache=name_cache)
        if "error" in res:
            errors += 1
            continue
        persist_result(conn, mst, res, update_ordinance=False)
        for f in res["findings"]:
            agg[f["severity"]] = agg.get(f["severity"], 0) + 1
        conn.commit()
        if verbose and i % 50 == 0:
            print(f"  [{i}/{len(msts)}] …")

    findings_n = conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
    laws_n = conn.execute("SELECT COUNT(*) FROM laws").fetchone()[0]
    # 재파싱 시점 stamp(원배치 메타는 유지, 집계·기준일만 갱신)
    conn.execute("UPDATE batch_meta SET findings_n=?, laws_n=?, batch_date=?, status=? WHERE id=1",
                 (findings_n, laws_n, _now(), "reparsed"))
    conn.commit()
    conn.close()
    if verbose:
        print(f"완료: findings {findings_n} (오류 {errors}) · 등급 {agg}")
    return {"processed": len(msts), "errors": errors, "agg": agg,
            "findings": findings_n, "laws": laws_n, "db": str(db_path)}
