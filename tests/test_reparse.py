# -*- coding: utf-8 -*-
"""오프라인 재파싱 테스트 — 영속 body_xml만으로 산출물 재생성(라이브 API 0).

임시 DB에 조례/법령 원본 XML을 넣고 reparse_all 을 돌려, findings·law_articles·
citations 가 다시 생기고(호 보존) 조례 메타는 보존되는지 고정.
pytest 없이도: python tests/test_reparse.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gunpolaw import db
from gunpolaw.reparse import reparse_all

_ORD_XML = """<?xml version="1.0" encoding="utf-8"?>
<법령>
 <자치법규기본정보>
  <자치법규일련번호>9001</자치법규일련번호>
  <자치법규명>테스트 조례</자치법규명>
  <시행일자>20100101</시행일자>
  <공포일자>20091201</공포일자>
  <담당부서명>본문상의과</담당부서명>
  <전화번호>031-000-0000</전화번호>
 </자치법규기본정보>
 <조문><조>
   <조문번호>000200</조문번호>
   <조문여부>Y</조문여부>
   <조제목>정의</조제목>
   <조내용>제2조(정의) 이 조례에서 정하는 바는 「건축법」 제2조에 따른다.</조내용>
 </조></조문>
</법령>"""

# 건축법: 제2조가 조례 시행(2010) 이후(2020) 개정 → 내용변경, 호 본문 포함
_LAW_XML = """<?xml version="1.0" encoding="utf-8"?>
<법령>
 <조문><조문단위>
   <조문여부>조문</조문여부>
   <조문번호>2</조문번호>
   <조문제목>정의</조문제목>
   <조문시행일자>20200101</조문시행일자>
   <조문내용>제2조(정의) 이 법에서 쓰는 용어의 뜻. &lt;개정 2020.1.1&gt;</조문내용>
   <호><호내용>1. "대지"란 각 필지로 나눈 토지를 말한다.</호내용></호>
 </조문단위></조문>
</법령>"""


def _make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init_db(path)
    conn = db.connect(path)
    conn.execute("INSERT INTO batch_meta(id,region_name,deep,status) VALUES(1,'테스트시',0,'ok')")
    conn.execute(
        "INSERT INTO ordinances(mst,name,enforce_date,dept,body_xml) VALUES(?,?,?,?,?)",
        ("9001", "테스트 조례", "20100101", "원래담당과", _ORD_XML))
    conn.execute("INSERT INTO laws(law_id,name,body_xml) VALUES(?,?,?)",
                 ("L1", "건축법", _LAW_XML))
    # 재파싱이 비워야 할 오래된 산출물(스테일) — 사라져야 정상
    conn.execute("INSERT INTO law_articles(law_id,label,content) VALUES('L1','제999조','옛 파싱 잔재')")
    conn.execute("INSERT INTO findings(mst,law_id,law_name,clause_label,category,severity,detail,ord_enforce,clause_enforce) "
                 "VALUES('9001','L1','건축법','제2조','timing','current','옛 결과','20100101','')")
    conn.commit()
    conn.close()
    return path


def test_reparse_regenerates_from_body_xml():
    path = _make_db()
    r = reparse_all(path, deep=False, verbose=False)
    assert r["processed"] == 1 and r["errors"] == 0, r
    conn = db.connect(path)
    # 1) 법령 조문 재파싱 — 호 본문 보존, 스테일 제999조 제거
    art = conn.execute("SELECT content FROM law_articles WHERE law_id='L1' AND label='제2조'").fetchone()
    assert art and "대지" in art["content"], art
    assert conn.execute("SELECT COUNT(*) c FROM law_articles WHERE label='제999조'").fetchone()["c"] == 0
    # 2) findings 재생성 — 조례 시행 후 개정이라 review(검토)
    f = conn.execute("SELECT severity FROM findings WHERE mst='9001' AND clause_label='제2조'").fetchone()
    assert f and f["severity"] == "review", dict(f) if f else None
    # 3) 인용 재추출
    c = conn.execute("SELECT COUNT(*) c FROM citations WHERE mst='9001' AND law_name='건축법'").fetchone()
    assert c["c"] >= 1
    # 4) 조례 메타는 보존(update_ordinance=False) — 담당과 원본 유지
    d = conn.execute("SELECT dept FROM ordinances WHERE mst='9001'").fetchone()["dept"]
    assert d == "원래담당과", d
    # 5) batch_meta 재파싱 stamp
    assert conn.execute("SELECT status FROM batch_meta WHERE id=1").fetchone()["status"] == "reparsed"
    conn.close()
    os.unlink(path)


def test_reparse_no_body_xml_skips():
    """body_xml 없는 조례는 건너뛴다(재배치 필요)."""
    path = _make_db()
    conn = db.connect(path)
    conn.execute("UPDATE ordinances SET body_xml='' WHERE mst='9001'")
    conn.commit(); conn.close()
    r = reparse_all(path, deep=False, verbose=False)
    assert r["processed"] == 0, r       # body_xml 없으니 대상 0
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
