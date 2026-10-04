// Test-only: drives the dsh plugin as dsh would, without dsh.  A fake `ctx` records the plugin's listeners; the
// harness runs one turn (turn/start, the pre-step waterfall with the person's message, the turn's messages, turn/end),
// waits for the plugin to store the turn, and prints one JSON line with what happened.
//   node --import ./register.mjs harness.mjs <plugin> <config json> <scenario>
// Scenarios: `turn` (above), `aborted` (a pre-step whose signal is aborted), `sweep` (no turn: what the spool already
// holds is stored by the plugin's sweep, which first runs 5 s after it starts).  `config.queued` is a message of the
// person's that the turn's first step takes before the prompt; `config.ignore` names spool files the wait does not wait
// for; `config.expectBacklog` waits `config.waitMs` (4 s) instead of for an empty spool.
import { existsSync, readdirSync, readFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'

const [pluginPath, configJson, scenario = 'turn'] = process.argv.slice(2)
const config = JSON.parse(configJson)
const listeners = {}
const effects = []
const warnings = []
const ctx = {
  on: (name, listener, options) => { (listeners[name] ??= []).push({ listener, options }) },
  effect: (callback) => { effects.push(callback()) },
  logger: { warn: (message) => warnings.push(String(message)), info: () => {} },
  get: (name) => (name === 'profileContext' ? { name: 'test' } : undefined),
}
const plugin = await import(pathToFileURL(pluginPath).href)
plugin.apply(ctx, config)

const session = { id: 'session-TEST-plugin', header: { id: 'session-TEST-plugin', cwd: 'C:/TEST/work' } }
let seq = 0
const emit = (type, data) => {
  for (const { listener } of listeners['session/event'] ?? []) listener(session, { type, seq: seq++, time: Date.now(), data })
}
let ids = 0
const userMessage = (text) => ({ id: `u-${ids++}`, role: 'user', content: [{ type: 'text', text }], source: { kind: 'user' } })

const result = { warnings, injected: null, decisionKept: null, pid: process.pid }
const controller = new AbortController()
const preStep = async (messages, signal = controller.signal) => {
  const [{ listener }] = listeners['agent/pre-step']
  const decision = { kind: 'enter', messages, startsRequestSeries: true }
  return listener({ agent: { session }, messages, turn: 1, step: 1, signal }, async () => decision)
}

if (scenario === 'aborted') {
  const aborted = new AbortController()
  aborted.abort()
  const decision = await preStep([userMessage('TEST 已经取消的一轮')], aborted.signal)
  result.decisionKept = decision.messages.length === 1 && decision.startsRequestSeries === true
} else if (scenario === 'turn') {
  emit('turn/start', { turn: 1 })
  const prompt = userMessage(config.prompt ?? 'TEST 我的猫叫什么名字？')
  const queued = config.queued ? [userMessage(config.queued)] : []
  const decision = await preStep([...queued, prompt])
  const added = decision.messages.filter((message) => message.source?.kind !== 'user')
  result.injected = added.length ? { kind: added[0].source.kind, form: added[0].source.form, text: added[0].content[0].text } : null
  result.decisionKept = decision.kind === 'enter' && decision.startsRequestSeries === true && decision.messages[queued.length] === prompt
  for (const message of queued) emit('user/message', message)
  emit('user/message', prompt)
  emit('user/message', { id: 'ctx-1', role: 'user', content: [{ type: 'text', text: 'TEST runtime context' }], source: { kind: 'runtime-context' } })
  emit('assistant/message', { turn: 1, step: 1, message: { id: 'a-1', role: 'assistant', content: [{ type: 'text', text: 'TEST 我先查一下记忆。' }], source: { kind: 'model', model: 'TEST' } } })
  emit('assistant/message', { turn: 1, step: 2, message: { id: 'a-2', role: 'assistant', content: [{ type: 'text', text: 'TEST 它叫 Mochi。' }], source: { kind: 'model', model: 'TEST' } } })
  emit('turn/end', { turn: 1, reason: { kind: 'completed' } })
}

// Wait for the plugin's store to finish (or give up after 40 s), then let the plugin dispose as dsh's shutdown does.
const spool = config.spool
const ignore = new Set(config.ignore ?? [])
const pending = () => existsSync(spool) && readdirSync(spool).some((name) => name.endsWith('.jsonl') && !ignore.has(name))
const deadline = Date.now() + 40_000
while (scenario !== 'aborted' && pending() && Date.now() < deadline && !config.expectBacklog) await new Promise((r) => setTimeout(r, 200))
if (config.expectBacklog) await new Promise((r) => setTimeout(r, config.waitMs ?? 4_000))
for (const dispose of effects) await dispose?.()
const files = existsSync(spool) ? readdirSync(spool).filter((name) => name.endsWith('.jsonl')) : []
result.files = files
result.spool = files.flatMap((name) => readFileSync(`${spool}/${name}`, 'utf8').split('\n').filter(Boolean).map((line) => JSON.parse(line).role))
const statusFile = `${config.home}/scope-recall/dsh-plugin-status.json`
result.status = existsSync(statusFile) ? JSON.parse(readFileSync(statusFile, 'utf8')) : null
console.log(JSON.stringify(result))
