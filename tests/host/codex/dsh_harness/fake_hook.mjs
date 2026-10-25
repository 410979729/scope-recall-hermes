// Test-only stand-in for the hook client (child_process_mock.mjs, SR_FAKE_HOOK): a Stop stores one line of what it is
// sent, as a hook whose time ran out does; with SR_FAKE_HOOK_MODE=broken it fails as an interpreter without the package
// would. With store-broken only the content store fails. Each payload is appended to SR_FAKE_HOOK_LOG.
import { appendFileSync, readFileSync } from 'node:fs'

const payload = JSON.parse(readFileSync(0, 'utf8') || '{}')
const delay = Number(process.env[`SR_FAKE_${payload.hook_event_name?.toUpperCase()}_DELAY`] ?? 0)
if (delay > 0) await new Promise((resolve) => setTimeout(resolve, delay))
if (process.env.SR_FAKE_HOOK_LOG) appendFileSync(process.env.SR_FAKE_HOOK_LOG, `${JSON.stringify(payload)}\n`)
if (process.env.SR_FAKE_HOOK_MODE === 'broken' || (process.env.SR_FAKE_HOOK_MODE === 'store-broken' && payload.hook_event_name === 'Stop')) {
  process.stderr.write("Traceback (most recent call last):\nModuleNotFoundError: No module named 'scope_recall'\n")
  process.exit(1)
}
const answer = payload.hook_event_name === 'PrivateCheck'
  ? { capture_blocked: process.env.SR_FAKE_HOOK_MODE === 'private', recall_blocked: false, mode: 'none',
      decisions: (payload.times ?? []).map(() => process.env.SR_FAKE_HOOK_MODE === 'private') }
  : payload.hook_event_name === 'Stop' ? { through: process.env.SR_FAKE_HOOK_MODE === 'private' ? 0 : Math.min(1, (payload.record ?? []).length) } : {}
if (process.env.SR_FAKE_PRIVATE_RULES) answer.private_rules = process.env.SR_FAKE_PRIVATE_RULES === 'true'
process.stdout.write(JSON.stringify(answer))
