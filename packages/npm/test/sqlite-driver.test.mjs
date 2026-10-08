import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';

const source = readFileSync(new URL('../dist/esm/sqlite-driver.cjs', import.meta.url), 'utf8');
function loader(nodeVersion, driverVersion) {
  const requests = [], exports = {}, Driver = class {};
  runInNewContext(source, {
    exports, process: { versions: { node: nodeVersion } },
    require(specifier) {
      requests.push(specifier);
      if (specifier === 'better-sqlite3/package.json') {
        if (driverVersion === undefined) throw Error('MODULE_NOT_FOUND');
        return { version: driverVersion };
      }
      if (specifier === 'better-sqlite3') return Driver;
      throw Error(`Unexpected require: ${specifier}`);
    },
  });
  return { load: exports.loadSQLiteDriver, requests, Driver };
}

test('SQLite driver stays lazy and absent-peer guidance matches the Node engine', () => {
  for (const [nodeVersion, command] of [['20.11.1', '@11.10.0'], ['24.21.0', '@13.0.3']]) {
    const { load, requests } = loader(nodeVersion);
    assert.deepEqual(requests, []);
    assert.throws(load, error => error.message.includes(command) && error.cause.message === 'MODULE_NOT_FOUND');
    assert.deepEqual(requests, ['better-sqlite3/package.json']);
  }
});

test('SQLite rejects unsafe native addon versions before loading their code', () => {
  for (const [nodeVersion, driverVersion] of [
    ['24.18.0', '11.10.0'], ['24.20.0', '12.11.1'], ['26.4.0', '12.11.1'],
    ['20.11.1', '13.0.3'], ['24.21.0', '13.0.0'], ['24.21.0', '13.0.2'],
    ['24.21.0', '14.0.0'], ['24.21.0', 'invalid'],
  ]) {
    const { load, requests } = loader(nodeVersion, driverVersion);
    assert.throws(load, /cannot use better-sqlite3 .*install a compatible driver/);
    assert.deepEqual(requests, ['better-sqlite3/package.json']);
  }
});

test('SQLite loads supported legacy and N-API drivers', () => {
  for (const [nodeVersion, driverVersion] of [
    ['20.11.1', '11.10.0'], ['20.11.1', '12.4.1'], ['22.0.0', '11.10.0'],
    ['22.0.0', '13.0.3'], ['24.21.0', '13.0.3'], ['26.4.0', '13.1.0'],
  ]) {
    const { load, requests, Driver } = loader(nodeVersion, driverVersion);
    assert.equal(load(), Driver);
    assert.deepEqual(requests, ['better-sqlite3/package.json', 'better-sqlite3']);
  }
});
