const fs = require('node:fs');
fs.writeFileSync('dist/cjs/package.json', JSON.stringify({ type: 'commonjs' }) + '\n');
