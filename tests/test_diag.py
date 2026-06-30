# -*- coding: utf-8 -*-
"""판정 근거 추적(diag) 테스트 — 재실행 트레이스가 엔진을 충실히 재현하는지 고정.

reparse_all 이 만든 finding 을 trace_finding 으로 재실행하면 등급·변경유형이
저장값과 일치(match=True)해야 한다. 불일치는 파서/로직 드리프트 신호.
pytest 없이도: python tests/test_diag.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gunpolaw import db
from gunpolaw.reparse import reparse_all
from gunpolaw.diag import list_findings, trace_finding, _parse_subspec

_ORD_XML = """<?xml version="1.0" encoding="utf-8"?>
<법령>
 <자치법규기본정보>
  <자치법규일련번호>9001</자치법규일련번호>
  <자치법규명>테스트 조례</자치법규명>
  <시행일자>20100101</시행일자>
  <담당부서명>기획과</담당부서명>
 </자치법규기본정보>
 <조문><조>
   <조문번호>000200</조문번호><조문여부>Y</조문여부><조제목>정의</조제목>
   <조내용>제2조(정의) 이 조례는 「건축법」 제2조에 따른다.</조내용>
 </조></조문>
</법령>"""

# 건축법 제2조: 조례 시행(2010) 이후(2020) 개정 → review(내용변경 신호)
_LAW_XML = """<?xml version="1.0" encoding="utf-8"?>
<법령>
 <조문><조문단위>
   <조문여부>조문</조문여부><조문번호>2</조문번호><조문제목>정의</조문제목>
   <조문시행일자>20200101</조문시행일자>
   <조문내용>제2조(정의) 이 법에서 쓰는 용어의 뜻. &lt;개정 2020.1.1&gt;</조문내용>
   <호><호내용>1. "대지"란 각 필지로 나눈 토지를 말한다.</호내용></호>
 </조문단위></조문>
</법령>"""


def _make_db(deep=0):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init_db(path)
    conn = db.connect(path)
    conn.execute("INSERT INTO batch_meta(id,region_name,deep,status) VALUES(1,'테스트시',?,'ok')", (deep,))
    conn.execute("INSERT INTO ordinances(mst,name,enforce_date,dept,body_xml) VALUES(?,?,?,?,?)",
                 ("9001", "테스트 조례", "20100101", "기획과", _ORD_XML))
    conn.execute("INSERT INTO laws(law_id,name,body_xml) VALUES('L1','건축법',?)", (_LAW_XML,))
    conn.commit(); conn.close()
    return path


def test_parse_subspec():
    assert _parse_subspec("제2항제7호") == (2, "7", None)
    assert _parse_subspec("제5호") == (None, "5", None)
    assert _parse_subspec("제3항제1호가목") == (3, "1", "가")
    assert _parse_subspec("") == (None, None, None)


def test_trace_reproduces_engine():
    """reparse 가 만든 finding 을 trace 가 재실행 → 등급/유형 저장값과 일치."""
    path = _make_db(deep=0)
    reparse_all(path, deep=False, verbose=False)
    lst = list_findings(path)
    assert lst["total"] >= 1, lst
    # 건축법 제2조 finding 추적
    target = [f for f in lst["findings"] if f["clause_label"] == "제2조"]
    assert target, lst["findings"]
    t = trace_finding(path, target[0]["id"])
    assert t["recomputed"]["severity"] == "review", t["recomputed"]
    assert t["match"] is True, t
    # 단계에 현행 조문·개정일·basis_dates 포함
    keys = [s["k"] for s in t["steps"]]
    assert any("현행 조문" in k for k in keys), keys
    assert any("basis_dates" in k for k in keys), keys
    os.unlink(path)


def test_trace_all_findings_consistent():
    """모든 finding 의 재실행이 저장값과 일치(엔진 충실 재현 보장)."""
    path = _make_db(deep=0)
    reparse_all(path, deep=False, verbose=False)
    for f in list_findings(path)["findings"]:
        t = trace_finding(path, f["id"])
        if t.get("recomputed") is None:
            continue
        assert t["match"] is True, (f["id"], f["clause_label"], t["recomputed"])
    os.unlink(path)


def test_list_filters():
    path = _make_db(deep=0)
    reparse_all(path, deep=False, verbose=False)
    assert list_findings(path, severity="review")["total"] >= 1
    assert list_findings(path, q="건축법")["total"] >= 1
    assert list_findings(path, q="없는법령명")["total"] == 0
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
