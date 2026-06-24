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

# 군포 기본값. 목록(ordin)은 광역 org + 시군 sborg, lnkOrg는 sborg를 org로 사용.
GUNPO_ORG = "6410000"     # 경기도
GUNPO_SBORG = "4020000"   # 군포시
# 전수 대상 자치법규 종류 (훈령/예규/고시는 군포에 0건이지만 포함)
KND_CODES = ["30001", "30002", "30003", "30004", "30010", "30011"]


def _now():
    return datetime.now().isoformat(timespec="seconds")


def collect_ordinances(conn, org, sborg, verbose=True):
    """목록 API(target=ordin)로 자치법규 전수 수집 → ordinances 테이블 적재."""
    items, by_knd = [], {}
    for knd in KND_CODES:
        first = moleg.search_ordinances(org, sborg, knd, page=1, display=100)
        total = first["totalCount"]
        if total == 0:
            continue
        got = list(first["items"])
        for p in range(2, (total + 99) // 100 + 1):
            time.sleep(0.15)
            got += moleg.search_ordinances(org, sborg, knd, page=p, display=100)["items"]
        by_knd[knd] = len(got)
        items += got
    for it in items:
        conn.execute(
            """INSERT OR REPLACE INTO ordinances
               (mst, lid, name, knd, org, sborg, promulg_date, enforce_date)
               VALUES (?,?,?,?,?,?,?,?)""",
            (it["mst"], it["lid"], it["name"], it["knd"], org, sborg,
             it["promulg_date"], it["enforce_date"]))
    conn.commit()
    if verbose:
        print(f"전수 수집: {len(items)}건  종류별={by_knd}")
    return items


def run_batch(org=GUNPO_ORG, sborg=GUNPO_SBORG, limit=None,
              db_path=db.DEFAULT_DB, sleep=0.1, verbose=True):
    db.init_db(db_path)
    conn = db.connect(db_path)

    # 1) 전수 수집(목록 API)
    items = collect_ordinances(conn, org, sborg, verbose)
    msts = [(it["mst"], it["name"]) for it in items]

    # 2) lnkOrg(시군 코드) — 법령ID 보강 사전 + 공식 연계 저장
    link_index = {}
    for r in moleg.get_org_links(sborg):
        if r["law_name"] and r["law_id"]:
            link_index[r["law_name"].replace(" ", "")] = r["law_id"]
        conn.execute(
            "INSERT OR REPLACE INTO ord_law_links(mst, law_id, law_name) VALUES (?,?,?)",
            (r["mst"], r["law_id"], r["law_name"]))
    conn.commit()

    if limit:
        msts = msts[:limit]
    if verbose:
        print(f"분석 대상 {len(msts)}건 (법령ID 보강사전 {len(link_index)}개)\n")

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
