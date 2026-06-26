# -*- coding: utf-8 -*-
"""3단계 권고서 회귀 테스트 (순수 함수 — DB 불필요).

등급 도출과 행동 문안 템플릿을 고정. pytest 없이도: python tests/test_report.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gunpolaw.report import grade_of, action_text, build_model, render_html


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
