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
from gunpolaw.serve import (overview, list_ordinances, ordinance_detail,
                            _highlight_article)
from gunpolaw.extract import normalize_text
from gunpolaw.report import build_model, model_to_csv


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


def test_search_by_name_and_cited_law():
    """자유검색 q: 조례명 매치 + 인용 법령명 매치(담당과 무관, law_hits 표시)."""
    path = _make_db()
    conn = db.connect(path)
    # '다 조례'(총무과)가 도로교통법을 인용 — 이름엔 없고 인용으로만 걸려야
    conn.execute("INSERT INTO citations(mst,law_id,law_name,clause_label)"
                 " VALUES('3','L9','도로교통법','제2조')")
    conn.commit(); conn.close()

    by_name = list_ordinances(path, q="가")
    assert {o["mst"] for o in by_name} == {"1"}, by_name        # '가 조례'만

    by_law = list_ordinances(path, q="도로교통법")
    assert {o["mst"] for o in by_law} == {"3"}, by_law          # 인용으로 매치
    assert by_law[0]["law_hits"] == ["도로교통법"]               # 매치 법령 표시

    # 이름 매치는 law_hits 비움(이름으로 걸린 건 인용표시 불필요)
    assert by_name[0].get("law_hits") == []
    # 정비대상만 병행 — '다 조례'는 현행만(정비 0)이라 제외
    assert list_ordinances(path, q="도로교통법", action_only=True) == []
    os.unlink(path)


def test_dept_report_filter_and_csv():
    """build_model(dept=) 가 그 과 조례만, model_to_csv 가 그 행만 낸다."""
    path = _make_db()
    m_dept = build_model(path, dept="기획과")
    # 기획과는 가·나 조례(정비 대상)만
    names = {o["name"] for o in m_dept["ordinances"]}
    assert names == {"가 조례", "나 조례"}, names
    # 총무과(다)는 현행만이라 정비 대상 0 → 과별 리포트 비어 있음
    assert build_model(path, dept="총무과")["ordinances"] == []
    csv_text = model_to_csv(m_dept, dept="기획과")
    assert csv_text.count("기획과,") >= 2          # 항목마다 담당과 열
    assert "총무과" not in csv_text
    os.unlink(path)


def test_detail_shape():
    path = _make_db()
    d = ordinance_detail(path, "1")
    assert d["meta"]["name"] == "가 조례"
    assert d["recommend"]["found"] is True       # 정비 항목 있음
    assert d["recommend"]["items_count"] >= 1
    # 현행만인 조례도 통합 뷰에선 '검토 완료'로 표시(include_current) → found True
    d3 = ordinance_detail(path, "3")
    assert d3["recommend"]["found"] is True
    assert d3["recommend"]["items_count"] >= 1
    os.unlink(path)


def test_highlight_wraps_citation_and_escapes():
    body = '제2조(정의) 이 조례는 「건축법」 제2조 및 <b>주의</b>에 따른다.'
    text = normalize_text(body)
    target = "「건축법」 제2조"
    s = text.index(target)
    cites = [{"span_start": s, "span_end": s + len(target), "law_name": "건축법",
              "clause_label": "제2조", "cite_naked": 0}]
    h = _highlight_article("제2조", body, cites)
    assert f'<mark class="cite-law" data-oc="제2조" data-law="건축법"' in h, h
    assert ">「건축법」 제2조</mark>" in h, h
    assert "&lt;b&gt;" in h and "<b>" not in h        # 본문 HTML 이스케이프
    # 맨몸 인용은 cite-naked 클래스
    h2 = _highlight_article("제2조", body, [{**cites[0], "cite_naked": 1}])
    assert 'class="cite-naked"' in h2


def test_highlight_skips_bad_spans():
    body = "제1조 본문"
    text = normalize_text(body)
    # 범위 초과·역전 span 은 무시(예외 없이 본문 그대로)
    bad = [{"span_start": 999, "span_end": 1000, "law_name": "x", "clause_label": "", "cite_naked": 0},
           {"span_start": 5, "span_end": 2, "law_name": "y", "clause_label": "", "cite_naked": 0}]
    h = _highlight_article("제1조", body, bad)
    assert "<mark" not in h and "본문" in h


def test_detail_articles_present():
    """ordinance_detail 이 body_xml→조문별 하이라이트 HTML을 함께 반환."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init_db(path)
    ord_xml = ('<법령><자치법규기본정보><자치법규일련번호>7001</자치법규일련번호>'
               '<자치법규명>샘플</자치법규명><시행일자>20100101</시행일자></자치법규기본정보>'
               '<조문><조><조문번호>000200</조문번호><조문여부>Y</조문여부><조제목>정의</조제목>'
               '<조내용>제2조 이 조례는 「건축법」 제2조에 따른다.</조내용></조></조문></법령>')
    text = normalize_text("제2조 이 조례는 「건축법」 제2조에 따른다.")
    t = "「건축법」 제2조"; s = text.index(t)
    conn = db.connect(path)
    conn.execute("INSERT INTO ordinances(mst,name,enforce_date,body_xml) VALUES('7001','샘플','20100101',?)", (ord_xml,))
    conn.execute("INSERT INTO citations(mst,article_no,law_name,clause_label,span_start,span_end,cite_naked,cite_type)"
                 " VALUES('7001','제2조','건축법','제2조',?,?,0,'법령')", (s, s + len(t)))
    conn.commit(); conn.close()
    d = ordinance_detail(path, "7001")
    assert d["articles"], d
    a = d["articles"][0]
    assert a["no"] == "제2조" and a["cites"] == 1
    assert '<mark class="cite-law"' in a["html"]
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
