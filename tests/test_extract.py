# -*- coding: utf-8 -*-
"""인용추출 회귀 테스트.

A단계에서 실제 군포 조례로 확보한 'carry-over 오해소' 버그 케이스를 고정.
pytest 없이도 실행 가능: python tests/test_extract.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gunpolaw.extract import (
    extract_citations, extract_citations_by_article, group_by_law)


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


def test_ord_article_tagging():
    """인용이 등장한 조례 조문(제Y조)이 clause_articles 로 역추적되어야 한다.

    A+B 목표: "상위법 제X조가 바뀌었으니 → 이 조례 제Y조를 고쳐라".
    """
    articles = [
        {"no": "제3조", "body": "위원회는 「건축법」 제11조에 따라 심의한다."},
        {"no": "제7조", "body": "「건축법」 제11조 및 제14조를 준용한다."},
    ]
    g = group_by_law(extract_citations_by_article(articles))
    ca = g["건축법"]["clause_articles"]
    assert ca["제11조"] == ["제3조", "제7조"], ca       # 제11조는 두 조문에서 인용
    assert ca["제14조"] == ["제7조"], ca


def test_alias_def_with_broken_quote_and_glued_spacing():
    """여는 따옴표가 ··(OCR)로 깨져도 약칭 등록 + 뒤 조항 부착, 띄어쓰기 붙은 약칭도 해소."""
    text = ('「지적재조사에 관한 특별법」(이하··법"이라 한다) 제30조제1항에 따라. '
            '경우에는법 제13조에 의한다. 위원회는법 제16조 및 제17조에 따라.')
    g = group_by_law(extract_citations(text))
    labels = g["지적재조사에 관한 특별법"]["clause_labels"]
    assert "제30조" in labels, labels       # 깨진 약칭정의 뒤 조항 복구
    assert "제13조" in labels and "제16조" in labels and "제17조" in labels, labels  # 띄어쓰기 약칭
    # '경우에는법' 같은 가짜 법명이 생기지 않아야
    assert "경우에는법" not in g and "위원회는법" not in g, list(g)


def test_naked_flag_only_for_bare_full_name():
    """꺽쇠 권고(naked)는 '전체 법령명 맨몸'에만 — 약칭 '법'·「」 인용은 정상(naked=False)."""
    text = '「주민등록법」(이하 "법"이라 한다) 제2조. 법 제3조에 따라. 도로교통법 제5조.'
    g = group_by_law(extract_citations(text))
    occ = {(o["label"], o["naked"]) for o in g["주민등록법"]["occurrences"]}
    assert ("제2조", False) in occ      # 「」 인용 — 정상
    assert ("제3조", False) in occ      # 약칭 '법' — 정상(꺽쇠 권고 X)
    occ2 = {(o["label"], o["naked"]) for o in g["도로교통법"]["occurrences"]}
    assert ("제5조", True) in occ2      # 맨몸 전체 법령명 → 꺽쇠 권고


def test_glued_alias_spacing_flag_and_trimmed_span():
    """붙여쓴 약칭은 alias_spacing + occurrence.spacing=True, 하이라이트는 약칭부터('법 …')."""
    from gunpolaw.extract import normalize_text
    text = '「지적재조사에 관한 특별법」(이하 "법"이라 한다) 제5조. 위원회는법 제16조에 따라.'
    refs = extract_citations(text)
    sp = [r for r in refs if r["alias_source"] == "alias_spacing"]
    assert sp, [r["alias_source"] for r in refs]
    r = sp[0]
    assert r["raw"].startswith("법 제16조"), r["raw"]          # 앞말 제거
    s, e = r["span"]
    assert normalize_text(text)[s:e].startswith("법 제16조")    # span도 약칭부터
    g = group_by_law(refs)
    occ = g["지적재조사에 관한 특별법"]["occurrences"]
    assert any(o.get("spacing") for o in occ) and not any(o.get("naked") for o in occ)


def test_real_law_ending_in_law_not_split():
    """약칭 '법'이 등록돼도 실제 법명(어간+법)은 쪼개지 않는다(수도법 등)."""
    text = '「수도법」(이하 "법"이라 한다) 제2조. 상수도법 제5조에 따라.'
    g = group_by_law(extract_citations(text))
    # '상수도법'은 조사 끝('도')이 아니므로 약칭 redirect 안 됨 → 별도 법명으로
    assert "상수도법" in g, list(g)


def test_occurrences_split_by_article_and_subunit():
    """같은 조를 다른 호로 다른 조례 조문이 인용 → occurrences가 (조례조문×호)로 분리.

    finding이 조례 조문별로 갈라져 각자 인용한 호/목만 비교되게 하는 토대.
    """
    articles = [
        {"no": "제2조", "body": "「재난 및 안전관리 기본법」 제3조제5호에 따른다."},
        {"no": "제4조", "body": "「재난 및 안전관리 기본법」 제3조제1호를 적용한다."},
    ]
    g = group_by_law(extract_citations_by_article(articles))
    occ = g["재난 및 안전관리 기본법"]["occurrences"]
    keys = {(o["ord_article"], o["label"], o["ho"]) for o in occ}
    assert ("제2조", "제3조", "5") in keys, occ
    assert ("제4조", "제3조", "1") in keys, occ
    assert len(occ) == 2                      # 조례 조문별로 별개


def test_carryover_crosses_article_boundary():
    """'같은 법'이 앞 조문에서 정의된 법령에 연결되어야 한다(조문 경계 넘김)."""
    articles = [
        {"no": "제2조", "body": "「벤처투자 촉진에 관한 법률」 제12조에 따른다."},
        {"no": "제3조", "body": "같은 법 제50조에 따른 조합을 둔다."},
    ]
    g = group_by_law(extract_citations_by_article(articles))
    ca = g["벤처투자 촉진에 관한 법률"]["clause_articles"]
    assert ca["제50조"] == ["제3조"], ca


def test_law_name_only_tracks_article():
    """법명만 인용(조항 없음)도 law_articles 로 위치를 보존한다."""
    articles = [{"no": "제1조", "body": "이 조례는 「민원 처리에 관한 법률」에 근거한다."}]
    g = group_by_law(extract_citations_by_article(articles))
    assert g["민원 처리에 관한 법률"]["law_articles"] == ["제1조"]
    assert g["민원 처리에 관한 법률"]["clause_labels"] == []


def test_naked_law_citation_recognized():
    """「」 없이 맨몸으로 쓴 단일토큰 법명도 인식해야 한다(서식 위반이지만 인식 필요).

    실제 케이스(군포시 주민등록사무의 동 위임 조례 제1조):
    '이 조례는 주민등록법 제2조제2항에 의거 …'
    """
    text = ('이 조례는 주민등록법 제2조제2항에 의거 시장이 관장하는 '
            '주민등록 사무에 관한 권한 중 일부를 동장에게 위임한다.')
    g = group_by_law(extract_citations(text))
    assert "주민등록법" in g, list(g)
    assert g["주민등록법"]["type"] == "법령"
    assert "제2조" in _labels(g, "주민등록법")
    assert g["주민등록법"]["naked_any"] and g["주민등록법"]["naked_only"]


def test_naked_excludes_pointer_and_partial_names():
    """지시어('같은/이/위반한 법')와 멀티어절 꼬리('특별법')는 맨몸 채택 제외."""
    for txt in ("같은 법 제3조", "이 법 제5조", "위반한 법 제8조", "특별법 제4조"):
        assert extract_citations(txt) == [], txt


def test_sihaeng_rule_classified_as_national_law():
    """'○○법 시행규칙'은 국가법령(법령) — 규칙으로 끝난다고 자치법규로 보면 시행규칙을
    분석에서 통째로 누락(파이프라인이 자치법규는 스킵). 지자체 규칙은 자치법규 유지."""
    g = group_by_law(extract_citations("「화물자동차 운수사업법 시행규칙」 제13조에 따른다."))
    assert g["화물자동차 운수사업법 시행규칙"]["type"] == "법령", g
    g2 = group_by_law(extract_citations("「군포시 행정기구 설치 규칙」 제2조에 따른다."))
    assert g2["군포시 행정기구 설치 규칙"]["type"] == "자치법규", g2


def test_bracketed_citation_not_marked_naked():
    """정상 「」 인용은 naked 로 표시되면 안 된다(서식 위반 아님)."""
    g = group_by_law(extract_citations("「민법」 제2조에 따른다."))
    assert g["민법"]["naked_any"] is False and g["민법"]["naked_only"] is False


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
