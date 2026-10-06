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
  assert(!packed.files.some(file => file.path.startsWith('node_modules/') || file.path.startsWith('test/')));
  fs.writeFileSync(path.join(consumer, 'package.json'), JSON.stringify({ name: 'pollardai-package-smoke', version: '1.0.0', private: true }));
  npm(['install', tarball, '--ignore-scripts', '--package-lock=false', '--offline', '--no-audit', '--no-fund'], consumer);
  const code = `const root = new Runtime().run('tarball-smoke'); const n = root.modelCall({ model: 'demo' }, () => ({ text: 'ok' })); if (n.result.text !== 'ok' || n.id.length !== 64) throw new Error('bad package');`;
  execFileSync(process.execPath, ['--input-type=module', '-e', `import { Runtime } from 'pollardai'; ${code}`], { cwd: consumer });
  execFileSync(process.execPath, ['-e', `const { Runtime } = require('pollardai'); ${code}`], { cwd: consumer });
  const typed = `import { Runtime, Node, Budget } from 'pollardai';\nconst budget: Budget = { steps: 1 };\nconst node: Node = new Runtime().run('types', { budget }).modelCall({}, () => ({ text: 'ok' }));\nconst id: string = node.id;\n`;
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
