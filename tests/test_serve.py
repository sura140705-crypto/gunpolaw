# -*- coding: utf-8 -*-
"""3단계 SERVE 집계 테스트 (임시 DB — 라이브 API 없음).

overview/list_ordinances 의 등급 집계·담당과 그룹·필터를 고정.
pytest 없이도: python tests/test_serve.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gunpolaw import db
from gunpolaw.serve import overview, list_ordinances, ordinance_detail


def _make_db():
    """담당과 2개·조례 3건·다양한 등급 findings 를 가진 임시 DB."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init_db(path)
    conn = db.connect(path)
    conn.execute("INSERT INTO batch_meta(id,region_name,batch_date,ordinances_n,laws_n,findings_n)"
                 " VALUES(1,'테스트시','2026-06-26T10:00:00',3,2,5)")
    ords = [  # (mst, name, dept, enforce)
        ("1", "가 조례", "기획과", "20200101"),
        ("2", "나 조례", "기획과", "20210101"),
        ("3", "다 조례", "총무과", "20190101"),
    ]
    for mst, name, dept, enf in ords:
        conn.execute("INSERT INTO ordinances(mst,name,dept,enforce_date) VALUES(?,?,?,?)",
                     (mst, name, dept, enf))
    finds = [  # (mst, severity, change_type, cite_naked)
        ("1", "review", "내용변경", 0),    # → review
        ("1", "current", "동일", 0),       # → current
        ("2", "review", "번호이동", 0),    # → mechanical
        ("2", "current", "동일", 1),       # → format(맨몸 승격)
        ("3", "current", "동일", 0),       # → current (정비 없음)
    ]
    for mst, sev, ct, naked in finds:
        conn.execute("INSERT INTO findings(mst,severity,change_type,cite_naked,law_name,clause_label,detail)"
                     " VALUES(?,?,?,?,?,?,?)", (mst, sev, ct, naked, "건축법", "제2조", "d"))
    conn.commit()
    conn.close()
    return path


def test_overview_grade_totals():
    path = _make_db()
    ov = overview(path)
    g = ov["grades"]
    assert g["review"] == 1, g
    assert g["mechanical"] == 1, g       # 번호이동 → 기계적
    assert g["format"] == 1, g           # 현행+맨몸 → 서식 정비
    assert g["current"] == 2, g
    assert ov["totals"]["ordinances"] == 3
    assert ov["totals"]["action"] == 2   # 가·나 (다는 현행만)
    assert ov["totals"]["depts"] == 2
    os.unlink(path)


def test_overview_dept_grouping():
    path = _make_db()
    ov = overview(path)
    by = {d["dept"]: d for d in ov["depts"]}
    assert by["기획과"]["total"] == 2 and by["기획과"]["action"] == 2
    assert by["총무과"]["total"] == 1 and by["총무과"]["action"] == 0
    # 정비 많은 과가 먼저
    assert ov["depts"][0]["dept"] == "기획과", ov["depts"]
    # 배치 메타 동봉
    assert ov["batch"]["region_name"] == "테스트시"
    os.unlink(path)


def test_list_filters():
    path = _make_db()
    assert len(list_ordinances(path)) == 3
    assert len(list_ordinances(path, dept="기획과")) == 2
    assert {o["mst"] for o in list_ordinances(path, action_only=True)} == {"1", "2"}
    assert {o["mst"] for o in list_ordinances(path, dept="기획과", action_only=True)} == {"1", "2"}
    assert list_ordinances(path, dept="총무과", action_only=True) == []
    os.unlink(path)


def test_detail_shape():
    path = _make_db()
    d = ordinance_detail(path, "1")
    assert d["meta"]["name"] == "가 조례"
    assert d["recommend"]["found"] is True       # 정비 항목 있음
    assert d["recommend"]["items_count"] >= 1
    # 현행만인 조례는 권고 없음
    assert ordinance_detail(path, "3")["recommend"]["found"] is False
    os.unlink(path)


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
