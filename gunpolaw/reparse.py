# -*- coding: utf-8 -*-
"""오프라인 재파싱 — 영속된 body_xml만으로 findings·law_articles·citations 재생성.

파서(moleg.parse_law_articles 등)를 고친 뒤, 법제처 API를 한 번도 호출하지 않고
DB의 원본 XML(ordinances.body_xml / laws.body_xml / law_versions.body_xml)을
다시 파싱·판정해 산출물만 갈아끼운다. 30분 재배치 → 수 초.

전제: 직전 배치가 body_xml(현행+당시본)을 영속했을 것. 새 법령·새 당시본이 필요한
경우(인용이 새로 생긴 미수집 법령)는 재수집(--batch)이 필요하다.
"""
from . import db
from . import moleg
from .pipeline import analyze_ordinance
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
