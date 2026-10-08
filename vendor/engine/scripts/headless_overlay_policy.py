"""Policy-only entry: never dispatches a renderer or a paid API."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.s3.overlay_policy import route, lock_plan, render_gate, final_visual_gate, propainter_preview_spec, qa_status


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action', choices=['route','lock','check','final-qa','propainter-preview'])
    parser.add_argument('--plan', required=True, type=Path)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--review', type=Path)
    parser.add_argument('--region-id')
    parser.add_argument('--start',type=float)
    parser.add_argument('--end',type=float)
    args=parser.parse_args();plan=json.loads(args.plan.read_text())
    try:
        if args.action=='route':
            if 'overlays' not in plan: raise ValueError('OVERLAY_CLASSIFICATION_REQUIRED')
            result={r['id']:route(r,plan.get('usage_scope','production')) for r in plan.get('overlays',[])}
        elif args.action=='propainter-preview':
            if not args.out or args.start is None or args.end is None: raise ValueError('PREVIEW_OUTPUT_AND_INTERVAL_REQUIRED')
            region=next((r for r in plan.get('overlays',[]) if r['id']==args.region_id),None)
            if region is None: raise ValueError('OVERLAY_REGION_REQUIRED')
            spec=propainter_preview_spec(plan,region,[args.start,args.end],args.out)
            from app.s3.experimental_propainter import ProPainterBackend
            spec_path=args.out.with_suffix('.request.json')
            if args.out.exists() or spec_path.exists(): raise ValueError('PREVIEW_OUTPUT_MUST_BE_NEW')
            args.out.parent.mkdir(parents=True,exist_ok=True)
            spec_path.write_text(json.dumps(spec,ensure_ascii=False,indent=2))
            result=ProPainterBackend().run(spec)
        elif args.action=='lock':
            if not args.out or args.out.resolve()==args.plan.resolve(): raise ValueError('NEW_LOCKED_PLAN_PATH_REQUIRED')
            result=lock_plan(plan)
            issues=render_gate(result)
            if issues: raise ValueError(', '.join(issues))
            args.out.write_text(json.dumps(result,ensure_ascii=False,indent=2))
        else:
            issues=(final_visual_gate(plan,json.loads(args.review.read_text())) if args.action=='final-qa' and args.review else render_gate(plan))
            if args.action=='final-qa' and not args.review: raise ValueError('FINAL_REVIEW_REQUIRED')
            result=dict(status=qa_status(issues),issues=issues)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return int(result.get('status') in {'REVIEW_REQUIRED','FAIL'} or result.get('success') is False)
    except (ValueError,KeyError,OSError,TypeError) as exc:
        print(json.dumps(dict(status='REVIEW_REQUIRED',reason=str(exc)),ensure_ascii=False))
        return 1


if __name__=='__main__':
    raise SystemExit(main())
