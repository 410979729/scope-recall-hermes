// Test-only module hooks: source and freshly installed plugins run hooks from this candidate, not an installed wheel.
const PLUGIN_SUFFIXES = ['/distribution/dsh/scope-recall/index.mjs', '/scope-recall/dsh-plugin/index.mjs']

export async function resolve(specifier, context, next) {
  if (specifier === 'node:child_process' && PLUGIN_SUFFIXES.some((suffix) => context.parentURL?.endsWith(suffix))) {
    return { url: new URL('./child_process_mock.mjs', import.meta.url).href, shortCircuit: true }
  }
  return next(specifier, context)
}
