# -*- coding: utf-8 -*-
"""3단계 권고서 회귀 테스트 (순수 함수 — DB 불필요).

등급 도출과 행동 문안 템플릿을 고정. pytest 없이도: python tests/test_report.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gunpolaw.report import (
    grade_of, action_text, build_model, render_html, model_to_csv,
    _split_evidence, _diff_marks, _fmtdate, _line_diff_blocks, _evidence_block,
    _ord_block, GRADE_KEYS)


def test_grade_mapping():
    assert grade_of("current", "동일") == "current"
    assert grade_of("review", "내용변경") == "review"
    assert grade_of("review", "삭제") == "review"
    assert grade_of("review", "번호이동") == "mechanical"   # 번호이동은 기계적
    assert grade_of("mechanical", "") == "mechanical"
    assert grade_of("check", "미확인") == "check"
    assert grade_of("check", "법령미해결") == "check"


def _f(**kw):
    base = {"law_name": "건축법", "clause_label": "제2조",
            "severity": "review", "change_type": "내용변경", "detail": "d"}
    base.update(kw)
    return base


def test_action_content_change():
    a = action_text(_f())
    assert "「건축법」" in a and "제2조" in a and "검토" in a


def test_action_renumber_is_mechanical():
    a = action_text(_f(severity="review", change_type="번호이동",
                       detail="제2조 → 제3조 로 이동(번호 변경)"))
    assert "조문번호로 정정" in a and "제3조" in a


def test_action_unresolved_law():
    a = action_text(_f(severity="check", change_type="법령미해결", clause_label=""))
    assert "제명변경" in a and "폐지" in a


def test_action_points_at_ordinance_clause():
    """ord_clause 가 있으면 정비 위치(이 조례 제Y조)를 직접 가리켜야 한다."""
    a = action_text(_f(ord_clause="제7조"))
    assert "이 조례 제7조" in a, a
    # 위치 미상이면 일반 문구로 폴백
    b = action_text(_f())
    assert "해당 조문" in b, b


def test_action_naked_content_change_appends_note():
    """내용변경 + 맨몸 인용 → 내용 정비 문안에 서식 정정 안내가 덧붙는다."""
    a = action_text(_f(cite_naked=1))
    assert "검토" in a and "꺽쇠 서식으로 정정" in a, a


def test_action_format_only_when_current_and_naked():
    """내용 현행 + 맨몸 인용 → 서식 정정만 안내(내용 변경 없음)."""
    a = action_text(_f(severity="current", change_type="동일",
                       cite_naked=1, ord_clause="제1조"))
    assert "서식으로 정정" in a and "내용 변경은 없음" in a, a
    # 맨몸이 아니면(서식 정상) 이 안내는 나오지 않음
    b = action_text(_f(severity="current", change_type="동일", cite_naked=0))
    assert "서식으로 정정" not in b, b


def test_split_evidence():
    old, new = _split_evidence("[당시] 가나다\n[현행] 가라다")
    assert (old, new) == ("가나다", "가라다"), (old, new)
    assert _split_evidence("그냥 텍스트") == (None, None)


def test_diff_marks_highlights_only_change():
    """동일 부분은 그대로, 바뀐 토큰만 <mark> 로 감싼다."""
    o_html, n_html = _diff_marks(
        "이 법에서 사용하는 용어의 뜻은 다음과 같다",
        "이 법에서 쓰는 용어의 뜻은 다음과 같다")
    # 공통 어절은 마크 없이 보존
    assert "이 법에서" in o_html and "용어의" in n_html
    # 바뀐 어절만 강조
    assert '<mark class="d">사용하는</mark>' in o_html, o_html
    assert '<mark class="i">쓰는</mark>' in n_html, n_html
    # 변화 없는 토큰은 마크되지 않음
    assert "<mark" not in o_html.replace('<mark class="d">사용하는</mark>', "")


def test_diff_marks_escapes_html():
    o_html, _ = _diff_marks("a <b> c", "a c")
    assert "&lt;b&gt;" in o_html and "<b>" not in o_html


def test_fmtdate():
    assert _fmtdate("20200101") == "2020-01-01"
    assert _fmtdate("2020-01-01") == "2020-01-01"
    assert _fmtdate("") == "—"


def test_render_html_smoke():
    model = {
        "summary": {"mechanical": 1, "review": 2, "check": 1, "current": 5,
                    "ordinances_total": 3, "ordinances_action": 2},
        "ordinances": [{
            "mst": "1", "name": "테스트 조례", "enforce_date": "20200101",
            "grades": {"mechanical": 0, "review": 1, "check": 0, "current": 0},
            "items": [{"grade": "review", "law_name": "건축법", "clause_label": "제2조",
                       "change_type": "내용변경",
                       "action": action_text(_f()),
                       "evidence": "[당시] a <x>\n[현행] b", "clause_enforce": ""}],
        }],
    }
    h = render_html(model, generated_at="2026-06-25")
    assert h.startswith("<!DOCTYPE html>") and h.rstrip().endswith("</html>")
    assert "테스트 조례" in h
    assert "&lt;x&gt;" in h          # evidence HTML 이스케이프
    assert "<x>" not in h            # 원본 꺾쇠는 새지 않음


def test_line_diff_blocks_skips_identical():
    """동일한 줄(항·호·목)은 생략하고 바뀐 줄만 블록으로."""
    blocks, skipped = _line_diff_blocks("머리글\n가호 동일\n나호 옛", "머리글\n가호 동일\n나호 새")
    assert skipped == 2, skipped          # 머리글·가호 동일
    assert len(blocks) == 1               # 나호만 바뀜
    assert "<mark" in blocks[0][1]        # 바뀐 줄 토큰 하이라이트


def test_evidence_compact_and_expand():
    """기본은 바뀐 부분만(+동일 생략 표시), 동일 줄 있으면 전체 펼치기 제공."""
    it = {"evidence": "[당시] 머리글\n가호 동일\n나호 옛내용\n[현행] 머리글\n가호 동일\n나호 새내용"}
    h = _evidence_block(it)
    assert "동일한 항·호·목 2개 생략" in h, h
    assert "전체 조문 비교 펼치기" in h
    assert "새내용" in h
    # 전부 바뀌면(동일 줄 없음) 펼치기 버튼 없이 간략=전체
    h2 = _evidence_block({"evidence": "[당시] 가\n[현행] 나"})
    assert "펼치기" not in h2 and "drow" in h2


def test_ord_block_shows_ordinance_text():
    """권고 섹션에 조례 조문 원문(무엇을 고칠지)을 함께 보여준다."""
    o = {"mst": "1", "name": "테스트 조례", "enforce_date": "20200101",
         "grades": {k: 0 for k in GRADE_KEYS}, "grades_": None,
         "articles_text": {"제2조": "이 조례는 「건축법」 제2조를 따른다."},
         "items": [{"grade": "review", "law_name": "건축법", "clause_label": "제2조",
                    "change_type": "내용변경", "ord_clause": "제2조", "ord_seq": 0,
                    "action": "검토", "evidence": ""}]}
    o["grades"]["review"] = 1
    h = _ord_block(o)
    assert 'class="ordtext"' in h and "이 조례는 「건축법」" in h, h


def test_ord_block_collapsible_items():
    """collapsible=True면 인용 항목이 <details.citem>(접힘)이고 (law,clause) 키를 갖는다.
    한 조례 조문 안 여러 인용도 각각 별도 항목으로."""
    o = {"mst": "1", "name": "테스트 조례", "enforce_date": "20200101",
         "grades": {k: 0 for k in GRADE_KEYS}, "articles_text": {"제2조": "조례 제2조 원문"},
         "items": [
            {"grade": "review", "law_name": "재난 및 안전관리 기본법", "clause_label": "제16조",
             "change_type": "내용변경", "ord_clause": "제2조", "ord_seq": 0, "action": "a1", "evidence": ""},
            {"grade": "review", "law_name": "재난 및 안전관리 기본법 시행령", "clause_label": "제21조의2",
             "change_type": "내용변경", "ord_clause": "제2조", "ord_seq": 1, "action": "a2", "evidence": ""}]}
    o["grades"]["review"] = 2
    h = _ord_block(o, collapsible=True)
    assert h.count('<details class="item citem"') == 2          # 인용 2개 각각 항목
    assert 'data-law="재난 및 안전관리 기본법" data-clause="제16조"' in h
    assert 'data-law="재난 및 안전관리 기본법 시행령" data-clause="제21조의2"' in h
    assert "<summary" in h                                       # 헤드라인은 summary
    assert 'class="ordtext"' not in h                            # collapsible은 좌측이 본문 담당
    # 평면(기본)은 div.item, details 아님
    assert "<details" not in _ord_block(o)


def test_model_to_csv():
    model = {
        "summary": {},
        "ordinances": [{
            "name": "테스트 조례", "enforce_date": "20200101",
            "grades": {}, "items": [{
                "grade": "review", "law_name": "건축법", "clause_label": "제2조",
                "change_type": "내용변경", "ord_clause": "제3조",
                "action": "「건축법」 제2조 개정 반영", "evidence": "",
                "ord_enforce": "20200101", "old_enforce": "20190101",
                "clause_enforce": "20210101", "cite_naked": 0}]}],
    }
    csv_text = model_to_csv(model, dept="기획과")
    assert csv_text.startswith("﻿"), "엑셀 한글 위해 BOM"
    assert "담당과,조례," in csv_text                    # 헤더
    lines = csv_text.splitlines()
    assert lines[1].startswith("기획과,테스트 조례,2020-01-01,제3조,실질 검토,건축법,제2조,내용변경"), lines[1]
    assert "2019-01-01" in lines[1] and "2021-01-01" in lines[1]   # 기준일


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
