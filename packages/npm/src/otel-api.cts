/** Load the optional peer from the package, independently of the caller's cwd. */
export function loadTelemetryAPI(): unknown {
  try { return require('@opentelemetry/api'); }
  catch (cause) { throw new Error('exportSpans requires @opentelemetry/api; install it or pass a TelemetryAPI as the fourth argument', { cause }); }
}
