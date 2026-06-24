# -*- coding: utf-8 -*-
"""CLI: 조례 1건 변경 탐지.

    set LAW_OC_KEY=발급키
    python -m gunpolaw <자치법규일련번호MST>
"""
import sys

from .pipeline import analyze_ordinance
from .checks import SEV_LABEL


def main(argv):
    if not argv:
        print("사용법: python -m gunpolaw <자치법규일련번호MST>")
        print("  (환경변수 LAW_OC_KEY 에 법제처 OC 키 필요)")
        return 1
    res = analyze_ordinance(argv[0])
    if "error" in res:
        print("오류:", res["error"])
        return 1
    o = res["ordinance"]
    print(f"조례: {o['name']} (시행 {o['enforce_date']})")
    print(f"요약: {res['summary']}\n")
    for f in res["findings"]:
        tag = SEV_LABEL.get(f["severity"], f["severity"])
        loc = f["clause_label"] or "(법령단위)"
        print(f"[{tag}] 「{f['law_name']}」 {loc}")
        print(f"      {f['detail']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
