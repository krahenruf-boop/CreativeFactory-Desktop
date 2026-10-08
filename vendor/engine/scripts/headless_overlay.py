"""Run an explicitly selected experimental overlay plan without starting a UI."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.s3.headless_runtime import Harness, propainter_job

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--allow-experimental-propainter', action='store_true')
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--region-id', required=True)
    parser.add_argument('--mask-edge-correction', action='store_true')
    args = parser.parse_args()
    spec=json.loads(args.plan.read_text())
    if args.allow_experimental_propainter:
        spec.update(backend='propainter',experimental=True)
    with Harness(args.work) as harness:
        result=propainter_job(harness,args.region_id,spec,
            correction_reason='MASK_EDGE' if args.mask_edge_correction else None).result()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result['success'] else 1)
