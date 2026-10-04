// Test-only module hooks: the dsh plugin's `node:child_process` becomes child_process_mock.mjs, which drops the
// interpreter's `-I` so that the hook process reads PYTHONPATH and imports the tree under test (the gate puts the
// tree there; an isolated interpreter would import whatever is installed).
const PLUGIN_SUFFIX = '/distribution/dsh/scope-recall/index.mjs'

export async function resolve(specifier, context, next) {
  if (specifier === 'node:child_process' && context.parentURL?.endsWith(PLUGIN_SUFFIX)) {
    return { url: new URL('./child_process_mock.mjs', import.meta.url).href, shortCircuit: true }
  }
  return next(specifier, context)
}
