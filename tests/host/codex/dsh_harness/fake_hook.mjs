// Test-only stand-in for the hook client (child_process_mock.mjs, SR_FAKE_HOOK): a Stop stores one line of what it is
// sent, as a hook whose time ran out does; with SR_FAKE_HOOK_MODE=broken it fails as an interpreter without the package
// would.  Each payload is appended to SR_FAKE_HOOK_LOG.
import { appendFileSync, readFileSync } from 'node:fs'

const payload = JSON.parse(readFileSync(0, 'utf8') || '{}')
if (process.env.SR_FAKE_HOOK_LOG) appendFileSync(process.env.SR_FAKE_HOOK_LOG, `${JSON.stringify(payload)}\n`)
if (process.env.SR_FAKE_HOOK_MODE === 'broken') {
  process.stderr.write("Traceback (most recent call last):\nModuleNotFoundError: No module named 'scope_recall'\n")
  process.exit(1)
}
process.stdout.write(payload.hook_event_name === 'Stop' ? JSON.stringify({ through: Math.min(1, (payload.record ?? []).length) }) : '{}')
