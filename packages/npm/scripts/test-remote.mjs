import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const cwd=resolve(dirname(fileURLToPath(import.meta.url)),'..');
const context=process.env.POLLARD_DOCKER_CONTEXT;
const dockerArgs=[...(context?['--context',context]:[]),'compose','-f','compose.yaml','-p','pollard-npm-parity'];
const env={...process.env,
  POLLARD_NPM_POSTGRES_URL:'postgresql://pollard:pollard@localhost:55432/pollard',
  POLLARD_NPM_REDIS_URL:'redis://localhost:56379/0',
  POLLARD_NPM_MONGODB_URL:'mongodb://localhost:57017/?replicaSet=rs0&directConnection=true',
  POLLARD_NPM_NEO4J_URL:'bolt://localhost:57687',
  POLLARD_NPM_NEO4J_USERNAME:'neo4j',
  POLLARD_NPM_NEO4J_PASSWORD:'pollard-test-password',
  POLLARD_NPM_KAFKA_BROKERS:'localhost:59092',
};
function run(command,args,options={}) {
  const result=spawnSync(command,args,{cwd,env,stdio:'inherit',...options});
  if(result.error) throw result.error;
  if(result.status!==0) throw new Error(`${command} exited with status ${result.status}`);
}
let failed=false;
try {
  run('docker',[...dockerArgs,'up','-d','--wait','--wait-timeout','240']);
  run(process.execPath,['node_modules/typescript/bin/tsc','-p','tsconfig.json']);
  run(process.execPath,['node_modules/typescript/bin/tsc','-p','tsconfig.cjs.json']);
  run(process.execPath,['scripts/build.cjs']);
  run(process.execPath,['--test','test/remote.test.mjs']);
} catch(error) { failed=true; console.error(error); }
finally {
  if(failed) spawnSync('docker',[...dockerArgs,'logs','--tail','60'],{cwd,stdio:'inherit'});
  try { run('docker',[...dockerArgs,'down','--volumes','--remove-orphans']); } catch(error) { failed=true;console.error(error); }
}
process.exitCode=failed?1:0;
