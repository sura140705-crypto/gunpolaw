# -*- coding: utf-8 -*-
"""법령 본문 파서 테스트 — 호(號)·목까지 content에 담기는지 고정.

호가 빠지면 정의·열거 조문이 머리글만 남아 diff가 <개정> 날짜만 보인다(회귀 방지).
pytest 없이도: python tests/test_moleg.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gunpolaw.parse import parse_law_articles, article_text, law_name_of
import xml.etree.ElementTree as ET


# 정의 조문(호 직속) + 일반 조문(항>호>목 중첩)
_XML = """<?xml version="1.0" encoding="utf-8"?>
<법령>
 <조문>
  <조문단위>
   <조문여부>조문</조문여부>
   <조문번호>3</조문번호>
   <조문제목>정의</조문제목>
   <조문시행일자>20250107</조문시행일자>
   <조문내용>제3조(정의) 이 법에서 사용하는 용어의 뜻은 다음과 같다. &lt;개정 2025.1.7&gt;</조문내용>
   <호><호번호>1</호번호><호내용>1. "재난"이란 국민의 생명을 해치는 것을 말한다.</호내용></호>
   <호><호번호>2</호번호><호내용>2. "해외재난"이란 대한민국 영역 밖에서 발생하는 재난을 말한다.</호내용></호>
  </조문단위>
  <조문단위>
   <조문여부>조문</조문여부>
   <조문번호>5</조문번호>
   <조문내용>제5조(책무)</조문내용>
   <항><항내용>① 국가는 노력하여야 한다.</항내용>
     <호><호내용>1. 예방 대책</호내용>
       <목><목내용>가. 세부 목 내용</목내용></목>
     </호>
   </항>
  </조문단위>
  <조문단위>
   <조문여부>전문</조문여부>
   <조문내용>부칙 — 조문 아님(무시 대상)</조문내용>
  </조문단위>
 </조문>
</법령>"""


def test_definition_article_keeps_hos():
    arts = parse_law_articles(_XML)
    c = arts["제3조"]["content"]
    assert "재난" in c and "해외재난" in c, c          # 호 본문 보존
    assert "용어의 뜻" in c                            # 머리글도 유지
    assert c.count("\n") >= 2                          # 머리글 + 호2개 = 최소 3줄


def test_nested_hang_ho_mok():
    arts = parse_law_articles(_XML)
    c = arts["제5조"]["content"]
    assert "노력하여야" in c          # 항
    assert "예방 대책" in c           # 항 아래 호
    assert "세부 목 내용" in c        # 호 아래 목


def test_non_article_skipped():
    arts = parse_law_articles(_XML)
    assert "부칙" not in "".join(a["content"] for a in arts.values())
    assert set(arts) == {"제3조", "제5조"}


def test_article_text_order():
    """머리글이 맨 앞, 이어서 호가 등장 순서대로."""
    u = ET.fromstring(_XML).find(".//조문단위")
    t = article_text(u)
    assert t.index("용어의 뜻") < t.index("재난") < t.index("해외재난"), t


def test_law_name_of():
    xml = '<법령><기본정보><법령명_한글>공간정보의 구축 및 관리 등에 관한 법률</법령명_한글></기본정보></법령>'
    assert law_name_of(xml) == "공간정보의 구축 및 관리 등에 관한 법률"
    assert law_name_of("") == "" and law_name_of("<a/>") == ""


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
