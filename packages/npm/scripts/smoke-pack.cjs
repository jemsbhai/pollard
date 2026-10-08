const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const npmCli = process.env.npm_execpath;
assert(npmCli && fs.existsSync(npmCli), 'Run this check with npm run smoke:pack');
const packageDir = path.resolve(__dirname, '..');
const tempRoot = path.resolve(os.tmpdir());
const consumer = fs.mkdtempSync(path.join(tempRoot, 'pollardai-smoke-'));
function npm(args, cwd) {
  return execFileSync(process.execPath, [npmCli, ...args], { cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
}
try {
  const output = npm(['pack', '--json'], packageDir);
  const packed = JSON.parse(output.slice(output.indexOf('[\n')))[0];
  const tarball = path.join(packageDir, packed.filename);
  assert(packed.files.some(file => file.path === 'LICENSE'));
  assert(packed.files.some(file => file.path === 'dist/cjs/package.json'));
  for (const entry of ['dist/esm/cli.js', 'dist/esm/remote-worker.cjs', 'dist/cjs/remote-worker.cjs', 'dist/esm/sqlite-driver.cjs']) assert(packed.files.some(file => file.path === entry), entry);
  assert(!packed.files.some(file => file.path.startsWith('node_modules/') || file.path.startsWith('test/')));
  fs.writeFileSync(path.join(consumer, 'package.json'), JSON.stringify({ name: 'pollardai-package-smoke', version: '1.0.0', private: true }));
  npm(['install', tarball, '--ignore-scripts', '--package-lock=false', '--offline', '--no-audit', '--no-fund'], consumer);
  const code = `const root = new Runtime().run('tarball-smoke'); const n = root.modelCall({ model: 'demo' }, () => ({ text: 'ok' })); if (n.result.text !== 'ok' || n.id.length !== 64) throw new Error('bad package');`;
  execFileSync(process.execPath, ['--input-type=module', '-e', `import { Runtime } from 'pollardai'; ${code}`], { cwd: consumer });
  execFileSync(process.execPath, ['-e', `const { Runtime } = require('pollardai'); ${code}`], { cwd: consumer });
  const expanded = `
    const expected = ['SQLiteStore','HashRopeStore','PostgresStore','RedisStore','MongoStore','Neo4jStore','KafkaStore','ReplayContract','TokenMeter','CostMeter','WindowMeter','EnergyMeter','TokenmasterMeter','TokenmasterCostMeter','OpenAITokenEstimator','registryFromMCP','exportSpans','seal','merge','exportSubtree','makeResponsesFn','makeMessagesFn','makeConverseFn'];
    for (const name of expected) if (typeof api[name] !== 'function') throw new Error('missing export: '+name);
    const store = new api.MemoryStore();
    const run = new api.Runtime({store}).run('packed-stream');
    const node = run.modelCall({model:'offline'}, function*(){yield {delta:{text:'packed'}};yield {usage:{input_tokens:2,output_tokens:3}};}, {keepChunks:true});
    if(node.result.text!=='packed' || run.report().spent.tokens!==5) throw new Error('streaming package failure');
    const replay = new api.Runtime({store,mode:'replay'}).run('packed-stream');
    if(replay.modelCall({model:'offline'},()=>{throw new Error('live replay');}).id!==node.id) throw new Error('replay package failure');
    if(api.seal(store,run.rootId).entries.length!==2) throw new Error('seal package failure');
    const registry = new api.Registry([new api.ActionSpec({name:'typed',version:'1',description:'',sideEffects:false,schema:{type:'object',properties:{value:{$ref:'#/$defs/value'}},$defs:{value:{type:'integer'}}},handler:args=>args})]);
    new api.Runtime({registry}).run('schema').toolCall('typed',{value:1});
  `;
  execFileSync(process.execPath, ['--input-type=module', '-e', `import * as api from 'pollardai'; ${expanded}`], { cwd: consumer });
  execFileSync(process.execPath, ['-e', `const api = require('pollardai'); ${expanded}`], { cwd: consumer });
  const help = execFileSync(process.execPath, [path.join(consumer,'node_modules/pollardai/dist/esm/cli.js'), '--help'], {cwd:consumer,encoding:'utf8'});
  assert(help.includes('pollardai merge') && help.includes('kafka-env:'));
  const typed = `import { Runtime, Node, Budget, makeResponsesFn, ReplayContract, PostgresStore, Meter } from 'pollardai';\nconst budget: Budget = { steps: 1, usd: '0.2', extra: { bytes: 4 } };\nconst node: Node = new Runtime().run('types', { budget }).modelCall({}, () => ({ text: 'ok' }));\nconst id: string = node.id;\nconst provider = makeResponsesFn({responses:{create:async()=>({output_text:'ok'})}});\nnew Runtime().run('async').modelCallAsync({},provider,{keepChunks:true});\nnew ReplayContract({provider:'mock'}).bind({model:'mock'});\nconst optionalStore: PostgresStore | undefined = undefined;\n`;
  fs.writeFileSync(path.join(consumer, 'esm.mts'), typed);
  fs.writeFileSync(path.join(consumer, 'cjs.cts'), typed);
  execFileSync(process.execPath, [require.resolve('typescript/bin/tsc'), '--noEmit', '--strict', '--target', 'ES2022', '--module', 'NodeNext', '--moduleResolution', 'NodeNext', 'esm.mts', 'cjs.cts'], { cwd: consumer });
  console.log(`Clean tarball install passed: ESM, CommonJS, TypeScript declarations (${packed.filename}, ${packed.size} bytes).`);
} finally {
  // Only remove this mkdtemp-created child of the verified system temp directory.
  const resolved = path.resolve(consumer);
  assert(path.dirname(resolved) === tempRoot && path.basename(resolved).startsWith('pollardai-smoke-'));
  fs.rmSync(resolved, { recursive: true, force: true });
}
