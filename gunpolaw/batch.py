# -*- coding: utf-8 -*-
"""일괄 처리 — 한 지자체의 연계 조례 전체를 분석해 findings 를 DB에 적재.

"누적 오류 일회성 정리"의 본체. 시군 org 코드만 바꾸면 타 지자체 확장.
findings 테이블은 매 실행마다 갱신(스냅샷). 법령 본문은 실행 내 1회만 호출.
"""
import time
from datetime import datetime

from . import db
from . import moleg
from .pipeline import analyze_ordinance


def _now():
    return datetime.now().isoformat(timespec="seconds")


def run_batch(org, limit=None, db_path=db.DEFAULT_DB, sleep=0.1, verbose=True):
    db.init_db(db_path)
    conn = db.connect(db_path)

    # 1) 공식 연계(lnkOrg) — 법령ID 사전 + 처리 대상 조례 목록
    links = moleg.get_org_links(org)
    link_index, msts, seen = {}, [], set()
    for r in links:
        if r["law_name"] and r["law_id"]:
            link_index[r["law_name"].replace(" ", "")] = r["law_id"]
        conn.execute(
            "INSERT OR REPLACE INTO ord_law_links(mst, law_id, law_name) VALUES (?,?,?)",
            (r["mst"], r["law_id"], r["law_name"]))
        if r["mst"] not in seen:
            seen.add(r["mst"]); msts.append((r["mst"], r["name"]))
    conn.commit()
    if limit:
        msts = msts[:limit]
    if verbose:
        print(f"연계 조례 {len(seen)}건 중 {len(msts)}건 처리 (org={org})\n")

    # 2) findings 스냅샷 재작성
    conn.execute("DELETE FROM findings")
    conn.commit()

    law_cache, agg = {}, {}
    errors = 0
    for i, (mst, name) in enumerate(msts, 1):
        res = analyze_ordinance(mst, link_index, law_cache)
        if "error" in res:
            errors += 1
            if verbose:
                print(f"  [{i}/{len(msts)}] {name[:30]} → 오류: {res['error']}")
            continue
        o = res["ordinance"]
        conn.execute(
            "INSERT OR REPLACE INTO ordinances(mst, name, enforce_date, org) VALUES (?,?,?,?)",
            (mst, o["name"], o["enforce_date"], org))
        for f in res["findings"]:
            conn.execute(
                """INSERT INTO findings(mst, law_id, law_name, clause_label, category,
                       severity, detail, ord_enforce, clause_enforce, evidence, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (mst, f["law_id"], f["law_name"], f["clause_label"], f["category"],
                 f["severity"], f["detail"], f["ord_enforce"], f["clause_enforce"],
                 (f["evidence"] or "")[:500], _now()))
            agg[f["severity"]] = agg.get(f["severity"], 0) + 1
        conn.commit()
        if verbose:
            nonc = sum(1 for f in res["findings"] if f["severity"] != "current")
            print(f"  [{i}/{len(msts)}] {o['name'][:28]:<28} → 변경 {nonc}건")
        time.sleep(sleep)

    conn.close()
    return {"processed": len(msts), "errors": errors, "agg": agg, "db": str(db_path)}


def report(db_path=db.DEFAULT_DB, severities=("mechanical", "review", "check")):
    """저장된 findings 를 등급별로 집계 출력."""
    conn = db.connect(db_path)
    print("=== 변경 탐지 집계 (등급별) ===")
    for sev in ("mechanical", "review", "check", "current"):
        n = conn.execute("SELECT COUNT(*) FROM findings WHERE severity=?", (sev,)).fetchone()[0]
        print(f"  {sev:<10}: {n}")
    print("\n=== 조례별 변경 건수 (상위 15) ===")
    rows = conn.execute(
        """SELECT o.name, COUNT(*) AS n
           FROM findings f JOIN ordinances o ON o.mst=f.mst
           WHERE f.severity IN ({})
           GROUP BY f.mst ORDER BY n DESC LIMIT 15""".format(
            ",".join("?" * len(severities))), severities).fetchall()
    for r in rows:
        print(f"  {r['n']:>3}  {r['name']}")
    conn.close()
