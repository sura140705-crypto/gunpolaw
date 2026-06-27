# -*- coding: utf-8 -*-
"""일괄 처리 — 한 지자체의 연계 조례 전체를 분석해 findings 를 DB에 적재.

"누적 오류 일회성 정리"의 본체. 시군 org 코드만 바꾸면 타 지자체 확장.
findings 테이블은 매 실행마다 갱신(스냅샷). 법령 본문은 실행 내 1회만 호출.
"""
import re
import time
from datetime import datetime

from . import db
from . import moleg
from .pipeline import analyze_ordinance

_LABEL_RE = re.compile(r"제(\d+)조(?:의(\d+))?")


def _label_nums(label):
    """'제30조'→(30,0), '제15조의2'→(15,2)."""
    m = _LABEL_RE.search(label or "")
    if not m:
        return (0, 0)
    return (int(m.group(1)), int(m.group(2) or 0))

# 군포 기본값. 목록(ordin)은 광역 org + 시군 sborg, lnkOrg는 sborg를 org로 사용.
GUNPO_ORG = "6410000"     # 경기도
GUNPO_SBORG = "4020000"   # 군포시
# 전수 대상 자치법규 종류 (훈령/예규/고시는 군포에 0건이지만 포함)
KND_CODES = ["30001", "30002", "30003", "30004", "30010", "30011"]


def _now():
    return datetime.now().isoformat(timespec="seconds")


def persist_result(conn, mst, res, org="", update_ordinance=True):
    """analyze_ordinance 결과 1건을 DB에 적재(배치·reparse 공용 단일 출처).

    update_ordinance=False(reparse)면 조례 메타(dept/시행일 등)는 건드리지 않고
    findings/citations/law_articles 등 '파싱 산출물'만 다시 쓴다. 호출 측에서
    findings·citations 는 미리 비워둔다(배치는 전체 DELETE, reparse도 동일).
    """
    o = res["ordinance"]
    if update_ordinance:
        # collect_ordinances 가 채운 lid/knd/sborg/promulg 는 보존하고 본문·담당과만 갱신
        conn.execute(
            """UPDATE ordinances
               SET name=?, enforce_date=?, org=?, dept=?, phone=?, body_xml=?, fetched_at=?
               WHERE mst=?""",
            (o["name"], o["enforce_date"], org, o.get("dept", ""), o.get("phone", ""),
             res.get("body_xml", ""), _now(), mst))
    for f in res["findings"]:
        conn.execute(
            """INSERT INTO findings(mst, law_id, law_name, clause_label, clause_detail,
                   category, severity, change_type, ord_clause, ord_seq, detail, ord_enforce,
                   old_enforce, clause_enforce, evidence, cite_naked, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (mst, f["law_id"], f["law_name"], f["clause_label"], f.get("clause_detail", ""),
             f["category"], f["severity"], f.get("change_type", ""), f.get("ord_clause", ""),
             f.get("ord_seq", 0), f["detail"], f["ord_enforce"],
             f.get("old_enforce", ""), f["clause_enforce"],
             (f["evidence"] or "")[:8000], f.get("cite_naked", 0), _now()))
    # 새로 받은(또는 DB에서 재파싱한) 법령 현행 본문·조문 영속
    for law_id, lw in res.get("fetched_laws", {}).items():
        conn.execute(
            "INSERT OR REPLACE INTO laws(law_id, name, body_xml, fetched_at) VALUES (?,?,?,?)",
            (law_id, lw["name"], lw.get("body_xml", ""), _now()))
        for label, a in lw["articles"].items():
            jo, ga = _label_nums(label)
            conn.execute(
                """INSERT OR REPLACE INTO law_articles
                   (law_id, label, jo, ga, title, enforce_date,
                    moved_from, moved_to, changed, content)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (law_id, label, jo, ga, a.get("title", ""), a.get("enforce_date", ""),
                 a.get("moved_from", ""), a.get("moved_to", ""),
                 1 if a.get("changed") else 0, a.get("content", "")))
    # 당시 시행본(deep) 원본 XML 영속 — 다음 파서 변경 시 deep 근거도 오프라인 재파싱
    for vmst, v in res.get("fetched_versions", {}).items():
        conn.execute(
            """INSERT OR REPLACE INTO law_versions
               (law_id, version_mst, enforce_date, body_xml, fetched_at)
               VALUES (?,?,?,?,?)""",
            (v["law_id"], vmst, v.get("enforce_date", ""), v.get("body_xml", ""), _now()))
    # 인용 위치(span)·맨몸 플래그 영속 — 하이라이트를 DB에서 서빙(라이브 추출 제거)
    for r in res.get("citations", []):
        if r["type"] == "기타":           # 일반어(법령/다른 법령 등) 제외
            continue
        s = r.get("span") or (0, 0)
        conn.execute(
            """INSERT INTO citations
               (mst, article_no, law_name, law_id, clause_label, alias_source,
                raw_text, span_start, span_end, cite_naked, ord_seq, cite_type)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (mst, r.get("ord_article", ""), r["name"], r.get("law_id", ""),
             ",".join(r["clause_labels"]), r.get("alias_source") or "",
             r.get("raw", ""), s[0], s[1],
             1 if r.get("alias_source") == "naked" else 0,
             r.get("ord_seq", 0), r["type"]))


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
              db_path=db.DEFAULT_DB, sleep=0.1, verbose=True, deep=False,
              region_name="군포시"):
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

    # 2) findings·법령본문 스냅샷 재작성(현행 기준으로 갈아끼움)
    conn.execute("DELETE FROM findings")
    conn.execute("DELETE FROM laws")
    conn.execute("DELETE FROM law_articles")
    conn.execute("DELETE FROM law_versions")
    conn.execute("DELETE FROM citations")
    conn.commit()

    law_cache, version_cache, old_cache, agg = {}, {}, {}, {}
    name_cache = {}
    errors = 0
    for i, (mst, name) in enumerate(msts, 1):
        res = analyze_ordinance(mst, link_index, law_cache,
                                deep=deep, version_cache=version_cache, old_cache=old_cache,
                                law_name_cache=name_cache)
        if "error" in res:
            errors += 1
            if verbose:
                print(f"  [{i}/{len(msts)}] {name[:30]} → 오류: {res['error']}")
            continue
        o = res["ordinance"]
        persist_result(conn, mst, res, org=org)
        for f in res["findings"]:
            agg[f["severity"]] = agg.get(f["severity"], 0) + 1
        conn.commit()
        if verbose:
            nonc = sum(1 for f in res["findings"] if f["severity"] != "current")
            print(f"  [{i}/{len(msts)}] {o['name'][:28]:<28} → 변경 {nonc}건")
        time.sleep(sleep)

    # 배치 스냅샷 stamp — UI 기준일 배너·타 시군 재사용 설정
    findings_n = conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
    laws_n = conn.execute("SELECT COUNT(*) FROM laws").fetchone()[0]
    conn.execute(
        """INSERT OR REPLACE INTO batch_meta
           (id, org, sborg, region_name, batch_date, ordinances_n, laws_n, findings_n, deep, status)
           VALUES (1,?,?,?,?,?,?,?,?,?)""",
        (org, sborg, region_name, _now(), len(msts), laws_n, findings_n,
         1 if deep else 0, "ok"))
    conn.commit()
    conn.close()
    return {"processed": len(msts), "errors": errors, "agg": agg, "db": str(db_path),
            "laws": laws_n, "findings": findings_n, "batch_date": _now()}


def report(db_path=db.DEFAULT_DB, severities=("mechanical", "review", "check")):
    """저장된 findings 를 등급별로 집계 출력."""
    conn = db.connect(db_path)
    print("=== 변경 탐지 집계 (등급별) ===")
    for sev in ("mechanical", "review", "check", "current"):
        n = conn.execute("SELECT COUNT(*) FROM findings WHERE severity=?", (sev,)).fetchone()[0]
        print(f"  {sev:<10}: {n}")
    print("=== 변경유형별 (2단계) ===")
    for ct, n in conn.execute(
            "SELECT COALESCE(NULLIF(change_type,''),'(미분류)'), COUNT(*) "
            "FROM findings GROUP BY change_type ORDER BY COUNT(*) DESC"):
        print(f"  {ct:<10}: {n}")
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
