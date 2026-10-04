// Test-only stand-in for the plugin's `node:child_process`: the same spawn, without the interpreter's `-I`; or, with
// SR_FAKE_HOOK set, that script under this node instead of the hook client (fake_hook.mjs).
import { spawn as realSpawn } from 'node:child_process'

export function spawn(command, args = [], options = {}) {
  if (process.env.SR_FAKE_HOOK) return realSpawn(process.execPath, [process.env.SR_FAKE_HOOK], options)
  return realSpawn(command, args.filter((arg) => arg !== '-I'), options)
}
