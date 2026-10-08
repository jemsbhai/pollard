"""Exercise actual PyPI-wheel -> SQLite -> Rust -> SQLite -> Python interoperability.

Run with --wheel PATH --crate PATH --output-dir PATH. No provider credentials.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

parser=argparse.ArgumentParser()
parser.add_argument('--wheel',type=Path,required=True)
parser.add_argument('--crate',type=Path,required=True)
parser.add_argument('--output-dir',type=Path,required=True)
args=parser.parse_args()
wheel=args.wheel.resolve()
expected='569fb5f130a82c9be327b8dcbd285e3be063200bd9773ca15c5d6bb62edd627f'
assert hashlib.sha256(wheel.read_bytes()).hexdigest()==expected
sys.path.insert(0,str(wheel))
from pollard import MemoryStore, Node, NodeKind, SQLiteStore, SQLiteSealSink, export_subtree, import_subtree, seal, verify
import pollard
assert pollard.__version__=='1.6.0' and str(wheel) in pollard.__file__
output=args.output_dir.resolve()
output.mkdir(parents=True,exist_ok=True)
source=output/'python.db'
destination=output/'rust.db'
native_export=output/'rust-native.json'
custody_path=output/'custody.db'
assert not source.exists() and not destination.exists(), 'Use a fresh output directory'
root=Node.make(kind=NodeKind.ROOT,parent=None,payload={'run':'python-native'})
payload={'model':'local','prompt':'Unicode: héllo 🌳'*100,'nested':{'é':['long-value'*200,{'__pollard_ref':'a'*64}]},'literal':{'__pollard_ref':'b'*64}}
child=Node.make(kind=NodeKind.MODEL_CALL,parent=root.id,payload=payload,result={'text':'python result','number':1.25,'usage':{'input_tokens':2,'output_tokens':3}},meta={'charges':{'steps':1,'tokens':5}})
with SQLiteStore(source,intern_threshold=20) as store:
    store.put(root)
    store.put(child)
    python_seal=seal(store,root.id)
    export_subtree(store,root.id,output/'python-native.json')
SQLiteSealSink(custody_path).publish(python_seal,store_id='interop-store',signer_identity='python-signer',sealed_at='2026-10-08T11:00:00Z')
command=['cargo','run','--quiet','--manifest-path',str(args.crate.resolve()/'Cargo.toml'),'--example','storage_interop','--',str(source),str(destination),str(native_export),str(custody_path)]
process=subprocess.run(command,text=True,capture_output=True,check=True)
rust=json.loads(process.stdout)
with SQLiteStore(destination,read_only=True) as store:
    assert seal(store,root.id)==python_seal
    assert store.get(child.id)==child
    assert verify(store,child.id).ok
    assert verify(store,rust['native_node']).ok
    assert seal(store,rust['native_root']).digest==rust['native_seal']
    assert store.get(rust['native_node']).result['number']==1.25
    assert len(store.roots())==2
memory=MemoryStore()
imported=import_subtree(native_export,memory)
assert imported.imported==2 and verify(memory,rust['native_node']).ok
custody=SQLiteSealSink(custody_path).records()
assert len(custody)==2 and custody[0].signer_identity=='python-signer' and custody[1].signer_identity=='rust-signer' and custody[1].digest==rust['native_seal']
result={'status':'passed','python':sys.version,'loaded_from':pollard.__file__,'wheel_sha256':expected,'python_root':root.id,'python_node':child.id,'python_seal':python_seal.digest,'rust':rust,'checks':['Python-created schema v3 opened read-only by Rust','interned strings and literal references retained','exact Python result text and seal retained through Rust merge','Rust-created schema v3 opened read-only by Python','Rust result id and float digest verified in Python','Rust manifest imported and verified in Python','Python and Rust appended interoperable custody records in sequence'],'cargo_stdout':process.stdout,'cargo_stderr':process.stderr}
(output/'interop-result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
print(json.dumps(result,indent=2))
