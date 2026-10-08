import { Worker, MessageChannel, receiveMessageOnPort, type MessagePort } from 'node:worker_threads';
import { join } from 'node:path';
import type { RemoteBackendOptions } from './remote-backends.cjs';

export class RemoteRPC {
  readonly #worker: Worker;
  readonly #port: MessagePort;
  readonly #timeout: number;
  #closed = false;
  constructor(options: RemoteBackendOptions) {
    this.#timeout = options.timeoutMs ?? 30_000;
    const channel = new MessageChannel();
    this.#port = channel.port1;
    const signal = new SharedArrayBuffer(4);
    this.#worker = new Worker(join(__dirname, 'remote-worker.cjs'), { workerData: { options, port: channel.port2, signal }, transferList: [channel.port2] });
    this.#worker.on('error', () => undefined);
    this.#worker.unref(); this.#port.unref();
    try { this.#receive(signal); }
    catch (error) { this.#closed = true; this.#port.close(); void this.#worker.terminate(); throw error; }
  }
  #receive(signal: SharedArrayBuffer): unknown {
    if (Atomics.wait(new Int32Array(signal), 0, 0, this.#timeout) === 'timed-out') {
      this.#closed = true; this.#port.close(); void this.#worker.terminate();
      throw Object.assign(new Error('remote Pollard operation timed out; its commit outcome may be unknown'), { name: 'RemoteStoreTimeout', outcomeUnknown: true });
    }
    const packet = receiveMessageOnPort(this.#port)?.message as { ok: boolean; result?: unknown; error?: { name: string; message: string; [key: string]: unknown } } | undefined;
    if (!packet) throw new Error('remote Pollard worker returned no response');
    if (!packet.ok) throw Object.assign(new Error(packet.error?.message ?? 'remote Pollard operation failed'), packet.error);
    return packet.result;
  }
  call(method: string, args: unknown[] = []): unknown {
    if (this.#closed) throw new Error('remote Pollard store is closed');
    const signal = new SharedArrayBuffer(4);
    this.#port.postMessage({ method, args, signal });
    return this.#receive(signal);
  }
  close(): void {
    if (this.#closed) return;
    try { this.call('close'); }
    finally { this.#closed = true; this.#port.close(); void this.#worker.terminate(); }
  }
}
