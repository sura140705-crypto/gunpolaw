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
        print("  python -m gunpolaw --batch [N] [--deep]  전수 일괄(--deep=내용diff 확정)")
        print("  python -m gunpolaw <MST> [--deep]        조례 1건(--deep=2단계)")
        print("  python -m gunpolaw --report             저장 결과 집계")
        print("  python -m gunpolaw --recommend [경로]   개정 권고서 HTML 생성(3단계)")
        print("  python -m gunpolaw --serve [포트]       총괄 대시보드 서빙(읽기전용, 기본 8765)")
        print("  (배치/권고는 LAW_OC_KEY 필요 · --serve 는 DB만 읽으므로 키 불요)")
        return 1

    deep = "--deep" in argv

    if argv[0] == "--batch":
        from .batch import run_batch
        nums = [a for a in argv[1:] if a.isdigit()]
        limit = int(nums[0]) if nums else None
        res = run_batch(limit=limit, deep=deep)
        print(f"\n처리 {res['processed']}건 (오류 {res['errors']}) → {res['db']}")
        print(f"등급 집계: {res['agg']}  (deep={deep})")
        return 0

    if argv[0] == "--report":
        from .batch import report
        report()
        return 0

    if argv[0] == "--serve":
        from .serve import serve
        nums = [a for a in argv[1:] if a.isdigit()]
        port = int(nums[0]) if nums else 8765
        return serve(port=port)

    if argv[0] == "--recommend":
        from .report import write_report, GRADE_META
        rest = [a for a in argv[1:] if not a.startswith("--")]
        out = rest[0] if rest else "개정권고서.html"
        path, s = write_report(out_path=out)
        print(f"개정 권고서 생성 → {path}")
        print(f"  정비 대상 {s['ordinances_action']}/{s['ordinances_total']}개 조례")
        for k in ("mechanical", "review", "check", "format", "current"):
            m = GRADE_META[k]
            print(f"  {m['emoji']} {m['label']:<7}: {s[k]}")
        return 0

    rest = [a for a in argv if not a.startswith("--")]
    res = analyze_ordinance(rest[0], deep=deep)
    if "error" in res:
        print("오류:", res["error"])
        return 1
    o = res["ordinance"]
    print(f"조례: {o['name']} (시행 {o['enforce_date']})  deep={deep}")
    print(f"요약: {res['summary']}\n")
    for f in res["findings"]:
        tag = SEV_LABEL.get(f["severity"], f["severity"])
        ct = f.get("change_type", "")
        loc = f["clause_label"] or "(법령단위)"
        here = f.get("ord_clause", "")
        head = f"[{tag}{('/'+ct) if ct else ''}] 「{f['law_name']}」 {loc}"
        print(head + (f"  ← 조례 {here}" if here else ""))
        print(f"      {f['detail']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
