"""Read-only deployment readiness; never starts GPU work or downloads assets."""
import argparse,json,urllib.request
from pathlib import Path
from minicolmap.semantics.selection import load_selection
from minicolmap.semantics.project import write_json
ROOT=Path(__file__).resolve().parents[1]


def check(base_url):
    rows=[];bodies={}
    for route in ('/api/demo','/api/showcase','/api/semantic/stage-diagnostics'):
        try:
            with urllib.request.urlopen(base_url.rstrip('/')+route,timeout=10) as r:value=json.load(r)
            rows.append(dict(check=route,passed=True));bodies[route]=value
        except Exception as exc:rows.append(dict(check=route,passed=False,message=str(exc)))
    demo=bodies.get('/api/demo',{});show=bodies.get('/api/showcase',{})
    rows.append(dict(check='trained_raw3',passed=any(m['id']=='raw3' and m['ready'] for m in demo.get('models',[]))))
    rows.append(dict(check='bound_rgb_run',passed=bool(demo.get('rgb_run_id'))))
    rows.append(dict(check='curated_case_available',passed=bool(show.get('ready'))))
    for card in show.get('selections',[]):
        try:
            load_selection(ROOT,card['selection_id']);passed=True
        except (OSError,ValueError,KeyError):passed=False
        rows.append(dict(check='selection:'+card['selection_id'],passed=passed))
    return dict(status='passed' if all(r['passed'] for r in rows) else 'failed',checks=rows,gpu_busy=demo.get('gpu_busy'),
        note='Read-only checks. GPU live rendering is verified separately by the release browser test.')


def main():
    parser=argparse.ArgumentParser(__doc__);parser.add_argument('--base-url',default='http://127.0.0.1:8765');parser.add_argument('--output',type=Path)
    args=parser.parse_args();result=check(args.base_url)
    if args.output:write_json(args.output,result)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    raise SystemExit(0 if result['status']=='passed' else 1)


if __name__=='__main__':main()
