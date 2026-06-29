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
from . import config
from .parse import law_version_key, law_version_sig
from .pipeline import analyze_ordinance, LiveSource

_LABEL_RE = re.compile(r"제(\d+)조(?:의(\d+))?")


def _label_nums(label):
    """'제30조'→(30,0), '제15조의2'→(15,2)."""
    m = _LABEL_RE.search(label or "")
    if not m:
        return (0, 0)
    return (int(m.group(1)), int(m.group(2) or 0))

# 지자체 기본값·종류코드는 config(환경변수/JSON)로 — 타 시군은 코드 수정 없이 교체.
# 목록(ordin)은 광역 org + 시군 sborg, lnkOrg는 sborg를 org로 사용.


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
                   old_enforce, clause_enforce, evidence, cite_naked, cite_spacing, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (mst, f["law_id"], f["law_name"], f["clause_label"], f.get("clause_detail", ""),
             f["category"], f["severity"], f.get("change_type", ""), f.get("ord_clause", ""),
             f.get("ord_seq", 0), f["detail"], f["ord_enforce"],
             f.get("old_enforce", ""), f["clause_enforce"],
             (f["evidence"] or "")[:8000], f.get("cite_naked", 0),
             f.get("cite_spacing", 0), _now()))
    # 새로 받은(또는 DB에서 재파싱한) 법령 현행 본문·조문 영속
    for law_id, lw in res.get("fetched_laws", {}).items():
        vkey = law_version_sig(lw.get("body_xml", ""))   # 공포일자|공포번호(델타 감지)
        conn.execute(
            "INSERT OR REPLACE INTO laws(law_id, name, body_xml, version_key, fetched_at) "
            "VALUES (?,?,?,?,?)",
            (law_id, lw["name"], lw.get("body_xml", ""), vkey, _now()))
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


def collect_ordinances(conn, org, sborg, verbose=True, knd_codes=None):
    """목록 API(target=ordin)로 자치법규 전수 수집 → ordinances 테이블 적재."""
    knd_codes = knd_codes or config.DEFAULTS["knd_codes"]
    items, by_knd = [], {}
    for knd in knd_codes:
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
        # 목록 메타만 갱신하고 기존 body_xml/dept/phone/fetched_at 은 보존(ON CONFLICT).
        # INSERT OR REPLACE 는 행을 통째로 갈아 body_xml 을 날려 증분 재수집을 무력화했다.
        conn.execute(
            """INSERT INTO ordinances
               (mst, lid, name, knd, org, sborg, promulg_date, enforce_date)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(mst) DO UPDATE SET
                   lid=excluded.lid, name=excluded.name, knd=excluded.knd,
                   org=excluded.org, sborg=excluded.sborg,
                   promulg_date=excluded.promulg_date, enforce_date=excluded.enforce_date""",
            (it["mst"], it["lid"], it["name"], it["knd"], org, sborg,
             it["promulg_date"], it["enforce_date"]))
    conn.commit()
    if verbose:
        print(f"전수 수집: {len(items)}건  종류별={by_knd}")
    return items


def _changed_labels(conn, law_id, revise_type):
    """이번 개정에서 바뀐 상위법 조문 라벨 판별.

    반환: (mode, labels)
      mode='full'    전부개정/제정 → 인용했으면 전부 해당 (labels=None)
      mode='partial' 일부개정 → 바뀐 조문(조문변경여부=Y) 집합 (labels=set)
      mode='unknown' 변경 플래그 없음 → 판별불가, 보수적으로 전부 '확인' (labels=None)
    """
    if "전부개정" in (revise_type or "") or "제정" in (revise_type or ""):
        return "full", None
    labels = {r["label"] for r in conn.execute(
        "SELECT label FROM law_articles WHERE law_id=? AND changed=1", (law_id,))}
    return ("partial", labels) if labels else ("unknown", None)


def _classify_affected(conn, law_id, mode, changed_labels):
    """이 법령을 인용한 조례를 '해당/확인/무관'으로 분류(조문 단위 매칭).

    해당  = 바뀐 조문을 인용 (어느 조인지 clauses 로 표시)
    확인  = 법명만 인용(조 미지정) → 개정 관련 여부 판단 필요
    무관  = 안 바뀐 조문만 인용 → 제외(알람에서 빠짐)
    반환: {"affected":[{mst,name,dept,clauses}], "uncertain":[{mst,name,dept}]}
    """
    by = {}
    for r in conn.execute(
            """SELECT c.mst, c.clause_label, o.name, o.dept
               FROM citations c JOIN ordinances o ON o.mst = c.mst
               WHERE c.law_id = ? ORDER BY o.dept, o.name""", (law_id,)):
        d = by.setdefault(r["mst"], {"mst": r["mst"], "name": r["name"],
                                     "dept": r["dept"], "clauses": set(), "nameonly": False})
        cl = (r["clause_label"] or "").strip()
        if not cl:
            d["nameonly"] = True
        else:
            for lab in cl.split(","):
                lab = lab.strip()
                if lab:
                    d["clauses"].add(lab)
    affected, uncertain = [], []
    for d in by.values():
        base = {"mst": d["mst"], "name": d["name"], "dept": d["dept"]}
        if mode == "full":
            affected.append({**base, "clauses": sorted(d["clauses"])})
        elif mode == "unknown":
            uncertain.append(base)
        else:  # partial — 바뀐 조문과 교집합 있으면 해당
            hit = sorted(d["clauses"] & changed_labels)
            if hit:
                affected.append({**base, "clauses": hit})
            elif d["nameonly"]:
                uncertain.append(base)
            # 안 바뀐 조문만 인용 → 무관(제외)
    return {"affected": affected, "uncertain": uncertain}


def detect_law_changes(conn, old_keys):
    """직전 스냅샷의 버전키(old_keys: {law_id: version_key}) 대비 바뀐 법령을
    law_changes 에 적재. 영향 조례는 '바뀐 조문을 인용했는지'까지 따져 정밀 집계한다.
    반환: 감지된 개정 건수.

    첫 베이스라인(old_key 없음)·신규 수집 법령은 '개정'으로 보지 않는다(거짓 대량감지 방지).
    """
    conn.execute("DELETE FROM law_changes")
    changed = 0
    for r in conn.execute("SELECT law_id, name, body_xml, version_key FROM laws"):
        lid, nkey = r["law_id"], (r["version_key"] or "")
        okey = old_keys.get(lid)
        if not okey or not nkey or okey == nkey:
            continue                      # 베이스라인 없음/신규/동일 → 개정 아님
        k = law_version_key(r["body_xml"])
        mode, labels = _changed_labels(conn, lid, k.get("revise_type", ""))
        cls = _classify_affected(conn, lid, mode, labels)
        ca = "*" if mode == "full" else ("" if mode == "unknown"
                                         else ",".join(sorted(labels)))
        conn.execute(
            """INSERT OR REPLACE INTO law_changes
               (law_id, name, old_key, new_key, new_enforce, revise_type,
                changed_articles, affected_n, uncertain_n, detected_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (lid, r["name"], okey, nkey, k.get("enforce_date", ""),
             k.get("revise_type", ""), ca, len(cls["affected"]),
             len(cls["uncertain"]), _now()))
        changed += 1
    conn.commit()
    return changed


def law_changes_report(db_path=db.DEFAULT_DB):
    """감지된 법령 개정 + 영향 조례(조문 매칭). 해당 많은 순.

    각 항목: {…law_changes 행…, "affected":[{mst,name,dept,clauses}],
              "uncertain":[{mst,name,dept}]}
    """
    conn = db.connect(db_path)
    out = []
    for r in conn.execute(
            "SELECT * FROM law_changes ORDER BY affected_n DESC, uncertain_n DESC, name"):
        ca = r["changed_articles"]
        if ca == "*":
            mode, labels = "full", None
        elif not ca:
            mode, labels = "unknown", None
        else:
            mode, labels = "partial", set(ca.split(","))
        cls = _classify_affected(conn, r["law_id"], mode, labels)
        out.append({**dict(r), **cls})
    conn.close()
    return out


def run_batch(org=None, sborg=None, limit=None,
              db_path=db.DEFAULT_DB, sleep=0.1, verbose=True, deep=False,
              region_name=None, max_age_days=0, knd_codes=None):
    """전수 일괄 수집·분석. org/sborg/region_name/knd_codes 미지정 시 config(환경변수/JSON).

    수집 신선도(max_age_days)로 재사용/재수집을 fetched_at 단위로 자동 판단:
      0(기본)  전부 재수집 — 모든 법령 본문을 새로 받아 개정까지 재검출(주1회 풀배치).
      None     무한 재사용 — DB에 있으면 무조건 재사용, 신규만 라이브(빠른 증분).
      N        N일 이내 수집분 재사용, 오래된 것만 재수집(예: 7=주1회 신선도).
    원본 본문(laws/law_versions/ordinances.body_xml)은 보존하고 신선도로 선별 재사용하며,
    파생물(findings/citations/law_articles)은 매 실행 재생성한다. 델타 감지는 모든 모드에서
    동작(재수집된 법령만 개정 비교 — 재사용분은 version_key 동일이라 자동 제외).
    """
    cfg = config.load()
    org = org or cfg["org"]
    sborg = sborg or cfg["sborg"]
    region_name = region_name or cfg["region_name"]
    knd_codes = knd_codes or cfg["knd_codes"]

    db.init_db(db_path)
    conn = db.connect(db_path)
    if verbose:
        print(f"대상: {region_name} (org={org}, sborg={sborg})")

    # 1) 전수 수집(목록 API)
    items = collect_ordinances(conn, org, sborg, verbose, knd_codes=knd_codes)
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

    # 신선도 기반 단일 출처 — fetched_at 으로 재사용/재수집을 자동 판단(지연 import: 순환 회피)
    from .reparse import StaleAwareSource
    src = StaleAwareSource(conn, max_age_days)
    # 델타 감지 기준: 갈아엎기 전 직전 스냅샷의 버전키 포착(개정 비교)
    old_keys = {r["law_id"]: (r["version_key"] or "")
                for r in conn.execute("SELECT law_id, version_key FROM laws")}
    # 파생물만 비운다 — 원본 본문은 보존(신선도로 선별 재사용)
    conn.execute("DELETE FROM findings")
    conn.execute("DELETE FROM citations")
    conn.execute("DELETE FROM law_articles")
    conn.commit()
    if verbose:
        mode = ("전체 재수집" if max_age_days == 0 else
                "증분(무한 재사용)" if max_age_days is None else
                f"신선도 {max_age_days}일")
        print(f"분석 대상 {len(msts)}건 · 모드={mode} "
              f"(법령ID 보강사전 {len(link_index)}개)\n")

    law_cache, version_cache, old_cache, agg = {}, {}, {}, {}
    name_cache = {}
    errors = 0
    for i, (mst, name) in enumerate(msts, 1):
        res = analyze_ordinance(mst, link_index, law_cache,
                                deep=deep, version_cache=version_cache, old_cache=old_cache,
                                src=src, law_name_cache=name_cache)
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

    # 재사용분 fetched_at 복원 — persist 가 now 로 덮어쓴 것을 원시점으로(신선도 판단 보존)
    for lid, at in src.reused_law_at.items():
        conn.execute("UPDATE laws SET fetched_at=? WHERE law_id=?", (at, lid))
    for m, at in src.reused_ord_at.items():
        conn.execute("UPDATE ordinances SET fetched_at=? WHERE mst=?", (at, m))
    conn.commit()

    # 법령 개정 델타 감지 — 재수집된 법령만 version_key 변화로 잡힘(재사용분은 자동 제외)
    changed_laws = detect_law_changes(conn, old_keys)
    if verbose and changed_laws:
        print(f"\n법령 개정 감지: {changed_laws}건 (영향 조례는 --changes 로 확인)")

    # 배치 스냅샷 stamp — UI 기준일 배너·타 시군 재사용 설정
    findings_n = conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
    laws_n = conn.execute("SELECT COUNT(*) FROM laws").fetchone()[0]
    conn.execute(
        """INSERT OR REPLACE INTO batch_meta
           (id, org, sborg, region_name, batch_date, ordinances_n, laws_n, findings_n, deep, status)
           VALUES (1,?,?,?,?,?,?,?,?,?)""",
        (org, sborg, region_name, _now(), len(msts), laws_n, findings_n, 1 if deep else 0,
         "ok" if max_age_days == 0 else
         "incremental" if max_age_days is None else f"max_age_{max_age_days}"))
    conn.commit()
    conn.close()
    misses = getattr(src, "misses", None)
    if verbose and misses:
        print(f"수집: 법령 재수집 {misses['law']}·재사용 {misses['law_reused']} · "
              f"당시본 신규 {misses['version']} · "
              f"조례 재수집 {misses['ordinance']}·재사용 {misses['ordinance_reused']}")
    return {"processed": len(msts), "errors": errors, "agg": agg, "db": str(db_path),
            "laws": laws_n, "findings": findings_n, "batch_date": _now(),
            "max_age_days": max_age_days, "misses": misses, "changed_laws": changed_laws}


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
