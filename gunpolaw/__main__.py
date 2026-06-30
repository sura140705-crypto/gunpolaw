# -*- coding: utf-8 -*-
"""CLI: 조례 변경 탐지.

    set LAW_OC_KEY=발급키
    python -m gunpolaw <MST>              # 조례 1건 분석
    python -m gunpolaw --batch [N]        # 전수 일괄(N=건수제한), 대상은 config(환경변수/JSON)
    python -m gunpolaw --report           # 저장된 findings 집계
    (대상 지자체 교체: LAW_ORG/LAW_SBORG/LAW_REGION 환경변수 또는 region.json — gunpolaw.config)
"""
import sys

from .pipeline import analyze_ordinance
from .checks import SEV_LABEL


def main(argv):
    if not argv:
        print("사용법:")
        print("  python -m gunpolaw <MST>             조례 1건 분석")
        print("  python -m gunpolaw --batch [N] [--deep] [--incr|--max-age D]  전수 일괄")
        print("       (--deep=내용diff 확정 · --incr=무한재사용 · --max-age D=D일 신선도)")
        print("  python -m gunpolaw <MST> [--deep]        조례 1건(--deep=2단계)")
        print("  python -m gunpolaw --report             저장 결과 집계")
        print("  python -m gunpolaw --changes [--all]    법령 개정 → 영향 조례(역추적, 미검토만/전체)")
        print("  python -m gunpolaw --ack <law_id>        개정 검토완료 표시(--unack=해제)")
        print("  python -m gunpolaw --recommend [경로]   개정 권고서 HTML 생성(전체)")
        print("  python -m gunpolaw --recommend --mst <MST> [경로]  조례 1건 권고서(담당자 확인용)")
        print("  python -m gunpolaw --serve [포트]       총괄 대시보드 서빙(읽기전용, 기본 8765)")
        print("  python -m gunpolaw --reparse            영속 body_xml로 재파싱(라이브 API 0)")
        print("  python -m gunpolaw --export-share [zip] 조원 공유용 올인원 zip(코드+슬림DB)")
        print("  python -m gunpolaw --export-static [폴더] 정적 사이트(서버 없이 호스팅·읽기전용)")
        print("  (배치/권고는 LAW_OC_KEY 필요 · --serve/--reparse 는 DB만 쓰므로 키 불요)")
        return 1

    deep = "--deep" in argv

    if argv[0] == "--batch":
        from .batch import run_batch
        args = argv[1:]
        max_age = 0                          # 기본: 전체 재수집
        if "--max-age" in args:
            i = args.index("--max-age")
            val = args[i + 1] if i + 1 < len(args) else ""
            if val.isdigit():
                max_age = int(val)
                args = args[:i] + args[i + 2:]   # 플래그+값 소비(limit 오인 방지)
        if ("--incr" in args) or ("--incremental" in args):
            max_age = None                   # 증분: 무한 재사용
        nums = [a for a in args if a.isdigit()]
        limit = int(nums[0]) if nums else None
        res = run_batch(limit=limit, deep=deep, max_age_days=max_age)
        mode = "증분" if max_age is None else ("전체" if max_age == 0 else f"{max_age}일")
        print(f"\n처리 {res['processed']}건 (오류 {res['errors']}) → {res['db']}  [{mode}]")
        print(f"등급 집계: {res['agg']}  (deep={deep})  개정감지 {res['changed_laws']}건")
        return 0

    if argv[0] == "--report":
        from .batch import report
        report()
        return 0

    if argv[0] == "--export-static":
        from .serve import export_static
        rest = [a for a in argv[1:] if not a.startswith("--")]
        r = export_static(out_dir=rest[0] if rest else "site")
        print(f"  조례 {r['ordinances']}건 · 파일 {r['files']}개 → {r['out']}")
        return 0

    if argv[0] == "--export-share":
        from .batch import export_share
        rest = [a for a in argv[1:] if not a.startswith("--")]
        r = export_share(out_zip=rest[0] if rest else "gunpolaw_테스트.zip", verbose=False)
        print(f"공유 zip 생성 → {r['zip']} ({r['size_mb']}MB)")
        print("  조원에게 이 파일 하나만 전달 → 풀고 `python -m gunpolaw --serve`")
        return 0

    if argv[0] == "--changes":
        from .batch import law_changes_report
        show_all = "--all" in argv          # 검토완료 포함 전체 이력
        rows = law_changes_report(include_acked=show_all)
        if not rows:
            msg = ("개정 이력 없음." if show_all else
                   "미검토 개정 없음(전체는 --changes --all). 직전 스냅샷 대비 변화 기준.")
            print(msg)
            return 0
        scope = "전체 이력" if show_all else "미검토"
        print(f"=== 법령 개정 {scope} {len(rows)}건 (바뀐 조문 ↔ 인용 조문 매칭) ===")
        print("   (검토완료 표시: python -m gunpolaw --ack <law_id>)")
        for r in rows:
            ca = r["changed_articles"]
            chg = "전부개정" if ca == "*" else ("판별불가" if not ca else f"바뀐 조문 {ca}")
            mark = "✓검토완료 " if r["acked"] else ""
            print(f"\n{mark}「{r['name']}」 [{r['law_id']}] {r['old_key']} → {r['new_key']} "
                  f"[{r['revise_type']}, 시행 {r['new_enforce']}] · {chg}")
            print(f"   해당 {len(r['affected'])}건 · 확인필요 {len(r['uncertain'])}건")
            for o in r["affected"]:
                cl = ", ".join(o["clauses"]) or "(법령단위)"
                print(f"    [해당] {o['name']} ({o['dept'] or '미지정'}) ← 인용 {cl}")
            for o in r["uncertain"]:
                print(f"    [확인] {o['name']} ({o['dept'] or '미지정'}) ← 법명만 인용")
        return 0

    if argv[0] in ("--ack", "--unack"):
        from .batch import ack_law_change
        rest = [a for a in argv[1:] if not a.startswith("--")]
        if not rest:
            print("사용법: python -m gunpolaw --ack <law_id> [new_key]  (--unack=해제)")
            return 1
        law_id = rest[0]
        new_key = rest[1] if len(rest) > 1 else None
        n = ack_law_change(law_id, new_key=new_key, acked=(argv[0] == "--ack"))
        verb = "검토완료" if argv[0] == "--ack" else "검토완료 해제"
        print(f"{verb} 표시: {n}건 (law_id={law_id}{', '+new_key if new_key else ''})")
        return 0

    if argv[0] == "--serve":
        from .serve import serve
        nums = [a for a in argv[1:] if a.isdigit()]
        port = int(nums[0]) if nums else 8765
        return serve(port=port)

    if argv[0] == "--reparse":
        from .reparse import reparse_all
        r = reparse_all()
        print(f"\n재파싱 {r['processed']}건 (오류 {r['errors']}) → {r['db']}")
        print(f"등급 집계: {r['agg']}")
        return 0

    if argv[0] == "--recommend":
        from .report import write_report, write_ordinance_report, GRADE_META
        args = argv[1:]
        mst = None
        if "--mst" in args:                       # 조례 1건만 — 담당자 확인·배포용
            i = args.index("--mst")
            mst = args[i + 1] if i + 1 < len(args) else None
            args = args[:i] + args[i + 2:]
        rest = [a for a in args if not a.startswith("--")]
        if mst:
            path, s, name = write_ordinance_report(mst, out_path=(rest[0] if rest else None))
            print(f"조례 권고서 생성 → {path}  (「{name}」, 인용 전건 분석)")
        else:
            path, s = write_report(out_path=(rest[0] if rest else "개정권고서.html"))
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
