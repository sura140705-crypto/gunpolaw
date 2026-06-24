# -*- coding: utf-8 -*-
"""CLI: 조례 변경 탐지.

    set LAW_OC_KEY=발급키
    python -m gunpolaw <MST>              # 조례 1건 분석
    python -m gunpolaw --batch [org] [N]  # 일괄(기본 org=4020000 군포), N=건수제한
    python -m gunpolaw --report [org]     # 저장된 findings 집계
"""
import sys

from .pipeline import analyze_ordinance
from .checks import SEV_LABEL

GUNPO_ORG = "4020000"


def main(argv):
    if not argv:
        print("사용법:")
        print("  python -m gunpolaw <MST>             조례 1건 분석")
        print("  python -m gunpolaw --batch [N]       전수 일괄 처리(군포, N=건수제한)")
        print("  python -m gunpolaw --report          저장 결과 집계")
        print("  (환경변수 LAW_OC_KEY 에 법제처 OC 키 필요)")
        return 1

    if argv[0] == "--batch":
        from .batch import run_batch
        nums = [a for a in argv[1:] if a.isdigit()]
        limit = int(nums[0]) if nums else None
        res = run_batch(limit=limit)
        print(f"\n처리 {res['processed']}건 (오류 {res['errors']}) → {res['db']}")
        print(f"등급 집계: {res['agg']}")
        return 0

    if argv[0] == "--report":
        from .batch import report
        report()
        return 0

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
