export function loadTokenizer(): unknown {
  try { return require('js-tiktoken'); }
  catch (cause) { throw new Error('OpenAITokenEstimator requires js-tiktoken; install it or pass tokenizer in its options', { cause }); }
}
