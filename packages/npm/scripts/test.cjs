const { readdirSync } = require('node:fs');
const { spawnSync } = require('node:child_process');
const { resolve } = require('node:path');
const cwd = resolve(__dirname, '..');
const files = readdirSync(resolve(cwd, 'test')).filter(name => name.endsWith('.test.mjs')).sort().map(name => `test/${name}`);
const result = spawnSync(process.execPath, ['--test', ...files], { cwd, stdio: 'inherit' });
if (result.error) throw result.error;
process.exitCode = result.status ?? 1;
