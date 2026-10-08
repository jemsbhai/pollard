/** Deliberately lazy: importing Pollard does not require a native SQLite driver. */
export function loadSQLiteDriver(): unknown {
  try { return require('better-sqlite3'); }
  catch (cause) { throw new Error('SQLiteStore requires the optional better-sqlite3 package; install it with npm install better-sqlite3', { cause }); }
}
