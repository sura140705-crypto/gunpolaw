# -*- coding: utf-8 -*-
"""변경판정 회귀 테스트 — 꺽쇠 개정일 기준 로직 고정.

사용자 합의(2026-06-26): 현행 조문의 <개정 ...> 날짜가 판단의 1차 기준.
- 현행기준 = 최신 개정일, 당시기준 = 조례 시행일이 끼는 구간의 개정일.
- 조례 시행일이 최종 개정일보다 뒤면 본문이 달라 보여도 '현행유지'.
pytest 없이도: python tests/test_checks.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gunpolaw.checks import amend_dates, basis_dates, diff_clause

_CONTENT = ("제2조(정의) 이 법에서 사용하는 용어의 뜻은 다음과 같다. "
            "<개정 2012.12.11, 2013.5.28, 2015.11.20, 2018.6.12>")


def test_amend_dates_sorted():
    assert amend_dates(_CONTENT) == ["20121211", "20130528", "20151120", "20180612"]
    assert amend_dates("개정 태그 없음") == []


def test_basis_before_first_amendment():
    """조례가 첫 개정 이전 → 당시기준 '원제정', 이후 개정 있으니 검토 대상."""
    was, now, after = basis_dates(_CONTENT, "20121111")
    assert was == "원제정" and now == "20180612" and after is True


def test_basis_mid_interval():
    """조례 2013-05-01 → 직전 개정 2012-12-11 이 당시기준."""
    was, now, after = basis_dates(_CONTENT, "20130501")
    assert was == "20121211" and now == "20180612" and after is True


def test_basis_after_last_amendment_is_current():
    """조례가 최종 개정(2018-06-12)보다 뒤 → 개정 없음(현행유지)."""
    was, now, after = basis_dates(_CONTENT, "20190709")
    assert now == "20180612" and after is False


def _art(content, label="제2조", enforce="20180612"):
    return {label: {"label": label, "content": content,
                    "enforce_date": enforce, "moved_from": "", "moved_to": ""}}


def test_diff_clause_dates_override_content():
    """본문이 달라도(시행본 선택 오차) 조례가 최종 개정보다 뒤면 현행유지.

    전통시장 제2조 가짜 내용변경 케이스의 핵심.
    """
    old = _art("제2조(정의) 옛 내용 — 전혀 다름")          # 당시 본문(오차로 다름)
    cur = _art(_CONTENT)                                   # 현행(최종개정 2018.6.12)
    f = diff_clause(old, cur, "제2조", "20190709", "건축법", "L")
    assert f["severity"] == "current" and f["change_type"] == "동일", f
    assert f["clause_enforce"] == "20180612"


def test_diff_clause_amended_after_is_review():
    """조례가 개정 구간 사이면 내용변경 + 당시/현행 기준일 표기."""
    old = _art("제2조(정의) 옛 내용")
    cur = _art(_CONTENT)
    f = diff_clause(old, cur, "제2조", "20130501", "건축법", "L")
    assert f["severity"] == "review" and f["change_type"] == "내용변경", f
    assert f["old_enforce"] == "20121211" and f["clause_enforce"] == "20180612"
    assert "[당시]" in f["evidence"] and "[현행]" in f["evidence"]


def test_diff_clause_evidence_not_truncated_at_400():
    """긴 조문(>400자): 당시/현행 본문이 잘리지 않고 전체가 evidence에 들어가야
    잘린 꼬리가 '가짜 diff'로 보이지 않는다 (지적재조사 제30조 케이스).

    실제 변경은 ②의 <개정 ...> 날짜뿐. 400자 절단이면 두 시점이 서로 다른
    지점에서 잘려 꼬리표지가 한쪽에서만 사라진다.
    """
    pad = "가나다라마바사아자차" * 60          # 600자(>400) 본문 채움
    tail = "[[꼬리표지]]"
    old = _art(f"제30조(위원회) ① 머리. <개정 2017.4.18> {pad} {tail}", "제30조")
    cur = _art(f"제30조(위원회) ① 머리. <개정 2017.4.18, 2020.6.9, 2024.3.19> {pad} {tail}",
               "제30조")
    f = diff_clause(old, cur, "제30조", "20200929", "지적재조사에 관한 특별법", "L")
    assert f["change_type"] == "내용변경", f
    body_old, _, body_cur = f["evidence"].partition("[현행]")
    assert tail in body_old and tail in body_cur, f["evidence"][:120]


def test_diff_clause_no_tag_falls_back_to_content():
    """개정 태그가 없으면 당시/현행 본문 직접 비교로 폴백."""
    old = _art("같은 내용", enforce="")
    cur = _art("같은 내용", enforce="")
    f = diff_clause(old, cur, "제2조", "20190709", "건축법", "L")
    assert f["change_type"] == "동일"
    old2 = _art("옛 내용", enforce="")
    cur2 = _art("새 내용", enforce="")
    f2 = diff_clause(old2, cur2, "제2조", "20190709", "건축법", "L")
    assert f2["change_type"] == "내용변경"


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
