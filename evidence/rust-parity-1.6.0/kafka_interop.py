"""Bidirectional Python/Rust Kafka records, against an explicitly supplied broker.

Only unique topics created by this script are removed. Pollard wheel is pinned.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

parser=argparse.ArgumentParser()
parser.add_argument('--wheel',type=Path,required=True)
parser.add_argument('--crate',type=Path,required=True)
parser.add_argument('--binary',type=Path,help='use a prebuilt immutable example; otherwise build it')
parser.add_argument('--bootstrap',required=True)
parser.add_argument('--output',type=Path,required=True)
args=parser.parse_args()
wheel=args.wheel.resolve()
sha=hashlib.sha256(wheel.read_bytes()).hexdigest()
assert sha=='569fb5f130a82c9be327b8dcbd285e3be063200bd9773ca15c5d6bb62edd627f'
sys.path.insert(0,str(wheel))
import pollard
from pollard import KafkaStore,Runtime,verify
from confluent_kafka import __version__ as client_version
from confluent_kafka.admin import AdminClient,NewTopic
assert pollard.__version__=='1.6.0' and str(wheel) in pollard.__file__
crate=args.crate.resolve()
env=os.environ.copy()
if args.binary:
    binary=args.binary.resolve()
else:
    subprocess.run(['cargo','build','--offline','--quiet','--manifest-path',str(crate/'Cargo.toml'),'--features','kafka','--example','kafka_interop'],check=True,env=env)
    binary=crate/'target/debug/examples'/('kafka_interop.exe' if sys.platform=='win32' else 'kafka_interop')
admin=AdminClient({'bootstrap.servers':args.bootstrap})
topics=[]
payload={'model':'mock','messages':[{'role':'user','content':'héllo'}]}
evidence={'status':'passed','pollard_version':'1.6.0','wheel_sha256':sha,'python_kafka_client':client_version,
          'binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),
          'rust_kafka_client':'rdkafka 0.39.0 / librdkafka 2.12.1','broker':'Redpanda v24.1.21',
          'broker_image_digest':'sha256:dc33c62306c742c0d8850c883c39bea9ab5351c32a93e0d9fa560ae04b43a3c0','cases':{}}
def create():
    name=f'pollard-parity-interop-{uuid4().hex}'
    admin.create_topics([NewTopic(name,1,1,config={'cleanup.policy':'delete','retention.ms':'-1','retention.bytes':'-1'})])[name].result(timeout=10)
    topics.append(name)
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        metadata=admin.list_topics(timeout=2).topics.get(name)
        if metadata and metadata.error is None and len(metadata.partitions)==1:return name
        time.sleep(0.05)
    raise AssertionError('topic metadata unavailable')
def probe(topic,mode,label):
    result=subprocess.run([str(binary),args.bootstrap,topic,mode,label],text=True,encoding='utf-8',capture_output=True,check=True)
    return json.loads(result.stdout)
try:
    topic=create()
    with KafkaStore({'bootstrap.servers':args.bootstrap},topic=topic) as store:
        run=Runtime(store).run('python-to-rust')
        node=run.model_call(payload,fn=lambda _:{'text':'python','usage':{'input_tokens':4,'output_tokens':2}})
        rust=probe(topic,'replay','python-to-rust')
        assert rust['node_id']==node.id and rust['root_id']==run.root_id and rust['result']==node.result
        assert rust['report']['avoided']['tokens']==6
        evidence['cases']['python_to_rust']={'node_id':node.id,'native_report':rust['report'],'matching_result':True}
    topic=create()
    rust=probe(topic,'record','rust-to-python')
    with KafkaStore({'bootstrap.servers':args.bootstrap},topic=topic,read_only=True,require_existing=True) as store:
        run=Runtime(store,mode='replay').run('rust-to-python')
        def unexpected(_):raise AssertionError('strict Python replay dispatched provider')
        node=run.model_call(payload,fn=unexpected)
        assert node.id==rust['node_id'] and node.result==rust['result']
        assert verify(store,node.id).ok
        assert node.meta['charges']['tokens']==10
        evidence['cases']['rust_to_python']={'node_id':node.id,'matching_result':True,'python_verification':True}
    with KafkaStore({'bootstrap.servers':args.bootstrap},topic=topic) as writer:
        writer.update_meta(rust['node_id'],{'python_reviewed':True})
    reviewed=probe(topic,'replay','rust-to-python')
    assert reviewed['meta']['python_reviewed'] is True
    evidence['cases']['cross_language_metadata']={'python_reviewed':True}
finally:
    for name in topics:admin.delete_topics([name])[name].result(timeout=10)
args.output.parent.mkdir(parents=True,exist_ok=True)
args.output.write_text(json.dumps(evidence,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
print(json.dumps({'status':'passed','cases':list(evidence['cases']),'output':str(args.output)}))
