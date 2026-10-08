"""Generate source API inventory and actual 1.6.0 wheel behavior fixtures."""
import ast
import hashlib
import json
import pathlib
import sys
import warnings
import argparse

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--wheel', type=pathlib.Path, required=True)
parser.add_argument('--source', type=pathlib.Path, required=True, help='Extracted verified 1.6.0 sdist directory')
parser.add_argument('--output-dir', type=pathlib.Path, default=pathlib.Path(__file__).parent)
args = parser.parse_args()
ROOT = args.output_dir.resolve()
ROOT.mkdir(parents=True, exist_ok=True)
WHEEL = args.wheel.resolve()
assert hashlib.sha256(WHEEL.read_bytes()).hexdigest() == '569fb5f130a82c9be327b8dcbd285e3be063200bd9773ca15c5d6bb62edd627f'
sys.path.insert(0, str(WHEEL))
import pollard
from pollard import Budget, Runtime, MemoryStore, BudgetExceeded, seal, export_subtree
from pollard.meters import StepMeter, TokenMeter, DepthMeter

assert pollard.__version__ == '1.6.0'
assert str(WHEEL) in pollard.__file__
SOURCE = args.source.resolve()
inventory = {}
tests = {}
for path in sorted((SOURCE / 'src/pollard').rglob('*.py')):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    records = []
    for item in tree.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and not item.name.startswith('_'):
            records.append({'kind': 'function', 'name': item.name, 'line': item.lineno, 'signature': ast.unparse(item.args)})
        if isinstance(item, ast.ClassDef) and not item.name.startswith('_'):
            methods = []
            for method in item.body:
                if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) and (not method.name.startswith('_') or method.name == '__init__'):
                    methods.append({'name': method.name, 'line': method.lineno, 'signature': ast.unparse(method.args), 'async': isinstance(method, ast.AsyncFunctionDef)})
            records.append({'kind': 'class', 'name': item.name, 'line': item.lineno, 'methods': methods})
    inventory[str(path.relative_to(SOURCE)).replace('\\', '/')] = records
for path in sorted((SOURCE / 'tests').rglob('test_*.py')):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    tests[str(path.relative_to(SOURCE)).replace('\\', '/')] = [node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test_')]
(ROOT / 'api-inventory.json').write_text(json.dumps({'version': pollard.__version__, 'wheel_sha256': hashlib.sha256(WHEEL.read_bytes()).hexdigest(), 'loaded_from': pollard.__file__, 'public_exports': pollard.__all__, 'source_api': inventory, 'test_functions': tests}, indent=2), encoding='utf-8')

def node_dict(n):
    return {'id': n.id, 'parent': n.parent, 'kind': n.kind, 'attempt': n.attempt, 'payload': n.payload, 'result': n.result, 'result_text': n.result_text, 'result_digest': n.result_digest, 'meta': {k:v for k,v in n.meta.items() if k not in {'created_at', 'duration_s'}}}

meters = lambda: [StepMeter(), TokenMeter(), DepthMeter()]
fixtures = {'version': pollard.__version__, 'wheel_sha256': hashlib.sha256(WHEEL.read_bytes()).hexdigest()}
r = Runtime(meters=meters()).run('first-run', budget=Budget(tokens=10, steps=1))
n = r.model_call({'model': 'local-demo', 'prompt': 'hello'}, fn=lambda p: {'text': f"offline reply for {p['prompt']}", 'usage': {'input_tokens':2, 'output_tokens':4}})
fixtures['first_run'] = {'root_id':r.root_id, 'node':node_dict(n), 'report':r.report(), 'seal':seal(r.store, r.root_id).to_dict()}
export_subtree(r.store, r.root_id, ROOT / 'python-first-run-export.json')

store = MemoryStore()
rt = Runtime(store, meters=meters())
r = rt.run('duplicate')
p = {'model':'mock','prompt':'hello'}
n1 = r.model_call(p, fn=lambda _: {'text':'first', 'usage':{'input_tokens':2,'output_tokens':3}})
r.rollback(r.root_id)
n2 = r.model_call(p, fn=lambda _: {'text':'second', 'usage':{'input_tokens':4,'output_tokens':5}})
fixtures['default_duplicate'] = {'returned':node_dict(n2),'stored':node_dict(store.get(n1.id)),'report':r.report()}

r = Runtime(meters=meters()).run('budget-refusal', budget=Budget(steps=0))
try:
    r.model_call(p, fn=lambda _: (_ for _ in ()).throw(AssertionError('must not dispatch')))
except BudgetExceeded as e:
    fixtures['budget_refusal'] = {'error':str(e),'node':node_dict(r.cursor),'report':r.report()}

class Estimator:
    def estimate_input_tokens(self, payload): return 7
r = Runtime(meters=[StepMeter(), TokenMeter(Estimator(), reserved_output_tokens=3)]).run('missing-usage', budget=Budget(tokens=20))
with warnings.catch_warnings():
    warnings.simplefilter('ignore')
    n = r.model_call(p, fn=lambda _: {'text':'unaccounted'})
fixtures['missing_usage_estimate'] = {'node':node_dict(n),'report':r.report()}
r = Runtime(meters=meters()).run('missing-usage-no-estimate', budget=Budget(tokens=20))
with warnings.catch_warnings():
    warnings.simplefilter('ignore')
    n = r.model_call(p, fn=lambda _: {'text':'unaccounted'})
fixtures['missing_usage_no_estimate'] = {'node':node_dict(n),'report':r.report()}

r = Runtime(meters=meters()).run('stream')
chunks = [{'text':'hel'},{'text':'lo','usage':{'input_tokens':2,'output_tokens':3}}]
received=[]
n=r.model_call(p,fn=lambda _: iter(chunks),on_delta=received.append,keep_chunks=True)
fixtures['stream']={'node':node_dict(n),'report':r.report(),'received':received}

(ROOT / 'behavior-fixtures.json').write_text(json.dumps(fixtures,indent=2,ensure_ascii=False),encoding='utf-8')
print(json.dumps({'api_modules':len(inventory),'test_files':len(tests),'test_functions':sum(map(len,tests.values())),'fixture_cases':len(fixtures)-2,'output_dir':str(ROOT)},indent=2))
