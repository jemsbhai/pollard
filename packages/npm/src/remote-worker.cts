import { workerData, MessagePort } from 'node:worker_threads';
import { openRemoteBackend, type RemoteBackend, type RemoteBackendOptions } from './remote-backends.cjs';

interface Request { method: string; args: unknown[]; signal: SharedArrayBuffer; }
const data = workerData as { options: RemoteBackendOptions; port: MessagePort; signal: SharedArrayBuffer };
const port = data.port;
let backend: RemoteBackend;
let sequence: Promise<unknown> = Promise.resolve();
const leases = new Map<string, { seconds: number; timer: ReturnType<typeof setInterval>; queued: boolean }>();
function clearLease(id: string): void { const lease = leases.get(id); if (lease) clearInterval(lease.timer); leases.delete(id); }
function errorRecord(error: unknown): Record<string, unknown> {
  if (!(error instanceof Error)) return { name: 'Error', message: String(error) };
  const result: Record<string, unknown> = { name: error.name, message: error.message };
  for (const [key, value] of Object.entries(error)) if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean' || value === null) result[key] = value;
  return result;
}
function respond(signal: SharedArrayBuffer, response: unknown): void {
  port.postMessage(response);
  const flag = new Int32Array(signal);
  Atomics.store(flag, 0, 1); Atomics.notify(flag, 0);
}
async function execute(request: Request): Promise<void> {
  try {
    if (request.method === 'close') {
      for (const id of leases.keys()) clearLease(id);
      await backend.close(); respond(request.signal, { ok: true }); port.close(); return;
    }
    const result = await backend.execute(request.method, request.args);
    if (request.method === 'pollardReserve' && (result as { ok?: boolean })?.ok) {
      const id = String(request.args[0]), seconds = Number(request.args[3]);
      clearLease(id);
      const timer = setInterval(() => {
        const lease = leases.get(id);
        if (!lease || lease.queued) return;
        lease.queued = true;
        sequence = sequence.then(async () => {
          if (!leases.has(id)) return;
          try { if (!await backend.execute('pollardRenew', [id, seconds])) clearLease(id); }
          catch { clearLease(id); }
          finally { lease.queued = false; }
        });
      }, Math.min(2_147_483_647, Math.max(10, seconds * 1000 / 3)));
      timer.unref(); leases.set(id, { seconds, timer, queued: false });
    }
    if (request.method === 'pollardSettle' || request.method === 'pollardRelease') clearLease(String(request.args[0]));
    respond(request.signal, { ok: true, result });
  } catch (error) { respond(request.signal, { ok: false, error: errorRecord(error) }); }
}
async function main(): Promise<void> {
  try {
    const ops = await import('./remote-state.js');
    backend = await openRemoteBackend(data.options, ops);
    await backend.execute('roots', []);
    port.on('message', (request: Request) => { sequence = sequence.then(() => execute(request)); });
    respond(data.signal, { ok: true });
  } catch (error) {
    await backend?.close().catch(() => undefined);
    respond(data.signal, { ok: false, error: errorRecord(error) }); port.close();
  }
}
void main();
