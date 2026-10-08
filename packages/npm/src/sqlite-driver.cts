/** Deliberately lazy: importing Pollard does not require a native SQLite driver. */
export function loadSQLiteDriver(): unknown {
  const nodeMajor = Number(process.versions.node.split('.')[0]);
  const install = `npm install better-sqlite3@${nodeMajor < 22 ? '11.10.0' : '13.0.3'}`;
  let version: string;
  try { version = require('better-sqlite3/package.json').version; }
  catch (cause) { throw new Error(`SQLiteStore requires the optional better-sqlite3 package; install it with ${install}`, { cause }); }
  const match = /^(\d+)\.(\d+)\.(\d+)$/.exec(version);
  const major = Number(match?.[1]), minor = Number(match?.[2]), patch = Number(match?.[3]);
  const supported = match && major >= 11 && major < 14 && (major !== 13 || minor > 0 || patch >= 3);
  // Legacy ObjectWrap addons can abort during GC on Node 24 even when loaded
  // successfully. Header/runtime differences make a patch-level check unsafe.
  // Read metadata before requiring the addon; native assertions are not catchable.
  if (!supported || (nodeMajor >= 24 && major < 13) || (nodeMajor < 22 && major >= 13)) {
    throw new Error(`SQLiteStore cannot use better-sqlite3 ${version} on Node.js ${process.versions.node}; install a compatible driver with ${install}`);
  }
  try { return require('better-sqlite3'); }
  catch (cause) { throw new Error(`SQLiteStore could not load better-sqlite3 ${version}; reinstall the driver for this Node.js runtime with ${install}`, { cause }); }
}
