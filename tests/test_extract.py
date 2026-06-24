# -*- coding: utf-8 -*-
"""인용추출 회귀 테스트.

A단계에서 실제 군포 조례로 확보한 'carry-over 오해소' 버그 케이스를 고정.
pytest 없이도 실행 가능: python tests/test_extract.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gunpolaw.extract import extract_citations, group_by_law


def _labels(grouped, name):
    return grouped.get(name, {}).get("clause_labels", [])


def test_carryover_binds_to_preceding_law():
    """'같은 법'은 본문 마지막 법령이 아니라 바로 앞 법령에 붙어야 한다.

    실제 케이스(군포산업진흥원 조례): 「벤처투자 촉진에 관한 법률」 …
    같은 법 제50조 … 같은 법 제70조 … (뒤에 「여신전문금융업법」 등장)
    legacy 버그: 같은 법이 여신전문금융업법에 오연결됨.
    """
    text = (
        '제2조 「벤처투자 촉진에 관한 법률」 제12조에 따른 개인투자조합, '
        '같은 법 제50조에 따른 벤처투자조합, 같은 법 제70조에 따른 조합, '
        '「여신전문금융업법」 제2조에 따른 시설대여업을 말한다.'
    )
    g = group_by_law(extract_citations(text))
    vt = _labels(g, "벤처투자 촉진에 관한 법률")
    yt = _labels(g, "여신전문금융업법")
    assert "제12조" in vt and "제50조" in vt and "제70조" in vt, vt
    assert "제50조" not in yt and "제70조" not in yt, yt


def test_same_law_sihaeng_rule_resolves():
    """'같은 법 시행규칙'은 직전 법의 시행규칙으로 해소되어야 한다.

    실제 케이스(군포시 가족센터 조례).
    """
    text = '「다문화가족지원법」 제12조제5항 및 같은 법 시행규칙 제3조에 따라 둔다.'
    g = group_by_law(extract_citations(text))
    assert "제12조" in _labels(g, "다문화가족지원법")
    assert "다문화가족지원법 시행규칙" in g, list(g)
    assert "제3조" in _labels(g, "다문화가족지원법 시행규칙")


def test_inline_alias_definition_restored():
    """깨졌던 inline 약칭 정의가 동작해야 한다: 「법」(이하 "법"이라 한다)."""
    text = '「개인정보 보호법」(이하 "법"이라 한다) 제15조에 따른다. 법 제30조도 본다.'
    g = group_by_law(extract_citations(text))
    pl = _labels(g, "개인정보 보호법")
    assert "제15조" in pl and "제30조" in pl, pl


def test_range_expansion():
    """범위 인용이 개별 조로 펼쳐져야 한다."""
    text = '「지적재조사에 관한 특별법」 제30조부터 제32조까지의 규정에 따른다.'
    g = group_by_law(extract_citations(text))
    assert _labels(g, "지적재조사에 관한 특별법") == ["제30조", "제31조", "제32조"]


def test_self_reference_not_captured_as_law():
    """'이 조례'는 상위법 인용으로 잡히면 안 된다."""
    text = '이 조례 제5조에 따른 위원회는 제8조의 사무를 처리한다.'
    g = group_by_law(extract_citations(text))
    assert g == {}, g


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
