# -*- coding: utf-8 -*-
"""역추적 알림 — 영속 개정이력(law_change_log) + 검토완료(ack) 회귀 테스트.

핵심 운영 보장: law_changes 는 매 배치 비워지지만, law_change_log 는 누적되며
재배치해도 검토완료(acked) 상태가 보존된다(주1회 운영 중 알림 유실 방지).
pytest 없이도: python tests/test_changes.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gunpolaw import db, batch


def _make_db():
    """법령 L1(바뀐 조문 제3조) + 그 조문을 인용한 조례 M1, 법명만 인용 M2."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init_db(path)
    conn = db.connect(path)
    conn.execute("INSERT INTO laws(law_id,name,body_xml,version_key,fetched_at)"
                 " VALUES('L1','지방자치법','x','new','t')")
    conn.execute("INSERT INTO law_articles(law_id,label,jo,ga,changed)"
                 " VALUES('L1','제3조',3,0,1)")
    conn.execute("INSERT INTO ordinances(mst,name,dept,enforce_date)"
                 " VALUES('M1','가 조례','기획과','2024')")
    conn.execute("INSERT INTO ordinances(mst,name,dept,enforce_date)"
                 " VALUES('M2','나 조례','총무과','2024')")
    conn.execute("INSERT INTO citations(mst,law_id,law_name,clause_label)"
                 " VALUES('M1','L1','지방자치법','제3조')")        # 바뀐 조문 인용 → 해당
    conn.execute("INSERT INTO citations(mst,law_id,law_name,clause_label)"
                 " VALUES('M2','L1','지방자치법','')")            # 법명만 → 확인필요
    conn.commit()
    return path, conn


def test_log_accumulates_and_classifies():
    path, conn = _make_db()
    n = batch.detect_law_changes(conn, {"L1": "old"})
    conn.close()
    assert n == 1
    rep = batch.law_changes_report(path)
    assert len(rep) == 1
    r = rep[0]
    assert [o["mst"] for o in r["affected"]] == ["M1"]      # 바뀐 조문 인용
    assert [o["mst"] for o in r["uncertain"]] == ["M2"]     # 법명만
    assert r["acked"] == 0 and "first_detected_at" in r


def test_ack_hides_from_default_report():
    path, conn = _make_db()
    batch.detect_law_changes(conn, {"L1": "old"})
    conn.close()
    assert batch.ack_law_change("L1", db_path=path) == 1
    assert batch.law_changes_report(path) == []                       # 미검토 0
    assert len(batch.law_changes_report(path, include_acked=True)) == 1
    # 해제
    assert batch.ack_law_change("L1", acked=False, db_path=path) == 1
    assert len(batch.law_changes_report(path)) == 1


def test_ack_survives_rebatch():
    """재배치(detect 재호출)해도 검토완료가 보존된다 — 영속 로그의 핵심 보장."""
    path, conn = _make_db()
    batch.detect_law_changes(conn, {"L1": "old"})
    batch.ack_law_change("L1", db_path=path)
    # 다음 주 재배치 시뮬 — 같은 개정 재감지(INSERT OR IGNORE)
    batch.detect_law_changes(conn, {"L1": "old"})
    conn.close()
    all_rows = batch.law_changes_report(path, include_acked=True)
    assert all_rows[0]["acked"] == 1, "재배치 후 검토완료가 사라지면 안 됨"
    assert batch.law_changes_report(path) == []                       # 여전히 미검토 0


def test_volatile_table_wiped_but_log_persists():
    """law_changes(휘발)는 비워질 수 있어도 law_change_log(영속)는 남는다."""
    path, conn = _make_db()
    batch.detect_law_changes(conn, {"L1": "old"})
    # 다음 배치: 변화 없음(old_keys=현재키) → law_changes 비워짐, 로그는 유지
    batch.detect_law_changes(conn, {"L1": "new"})
    lc = conn.execute("SELECT COUNT(*) FROM law_changes").fetchone()[0]
    lg = conn.execute("SELECT COUNT(*) FROM law_change_log").fetchone()[0]
    conn.close()
    assert lc == 0, "변화 없는 배치는 law_changes 비움"
    assert lg == 1, "law_change_log 는 영속"
    assert len(batch.law_changes_report(path)) == 1                   # 알림 유지


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {fn.__name__}: {e}")
        except Exception as e:
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(fns)} passed")
    return passed == len(fns)


if __name__ == "__main__":
    sys.exit(0 if _run() else 1)
