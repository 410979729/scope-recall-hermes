// Test-only stand-in for the plugin's `node:child_process`: the same spawn, without the interpreter's `-I`.
import { spawn as realSpawn } from 'node:child_process'

export function spawn(command, args = [], options = {}) {
  return realSpawn(command, args.filter((arg) => arg !== '-I'), options)
}
