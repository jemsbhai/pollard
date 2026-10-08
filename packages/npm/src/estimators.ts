import { IdentityPayload, IdentityValue, nonnegativeInteger } from './identity.js';
import { Estimator } from './meters.js';
import { loadTokenizer } from './tokenizer-driver.cjs';

export interface TokenEncoding { encode(text: string): { length: number }; }
export interface Tokenizer { encodingForModel(model: string): TokenEncoding; getEncoding(name: string): TokenEncoding; }
/** Approximation matching Python's textual-leaf counting and family fallback. */
export class OpenAITokenEstimator implements Estimator {
  readonly model?: string;
  readonly tokensPerMessage: number;
  readonly #tokenizer?: Tokenizer;
  constructor(options: { model?: string; tokensPerMessage?: number; tokenizer?: Tokenizer } = {}) {
    if (options.model !== undefined && typeof options.model !== 'string') throw new TypeError('model must be a string');
    this.model = options.model; this.tokensPerMessage = nonnegativeInteger(options.tokensPerMessage ?? 3, 'tokensPerMessage'); this.#tokenizer = options.tokenizer;
  }
  estimateInputTokens(payload: IdentityPayload): number {
    const tokenizer = this.#tokenizer ?? loadTokenizer() as Tokenizer;
    const model = this.model ?? payload.model;
    let encoding: TokenEncoding | undefined;
    if (typeof model === 'string') { try { encoding = tokenizer.encodingForModel(model); } catch { /* Older tokenizer model tables use a family fallback. */ } }
    encoding ??= tokenizer.getEncoding(fallbackEncodingName(model));
    let total = 0;
    const pending: { value: IdentityValue; key?: string }[] = [{ value: payload }];
    while (pending.length) {
      const { value, key } = pending.pop()!;
      if (typeof value === 'string') { if (key !== 'model') total += encoding.encode(value).length; }
      else if (Array.isArray(value)) pending.push(...value.map(value => ({ value })));
      else if (value && typeof value === 'object') pending.push(...Object.entries(value).map(([key, value]) => ({ key, value })));
    }
    if (Array.isArray(payload.messages)) total += payload.messages.length * this.tokensPerMessage;
    return nonnegativeInteger(total, 'estimated token count');
  }
}
export function fallbackEncodingName(model: unknown): 'cl100k_base' | 'o200k_base' {
  const name = typeof model === 'string' ? model.split(':').at(-1)!.toLowerCase() : '';
  return ['gpt-5', 'gpt-4.5', 'gpt-4.1', 'gpt-4o', 'chatgpt-4o', 'o1', 'o3', 'o4-mini'].some(prefix => name.startsWith(prefix)) ? 'o200k_base' : 'cl100k_base';
}
