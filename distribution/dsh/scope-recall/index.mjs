// Scope Recall for DeepSeek Harness (dsh): recall before a turn's first step, keep each turn's messages and store them
// at its end.  A native dsh plugin (an ESM module in the dsh host process), written by `scope-recall apply-install
// --host dsh` and named by a row of dsh's home patch (`$DSH_HOME/cordis.patch.yml`).
//
// It owns no memory policy: it runs the entry's hook client (`scope_recall.adapters.codex.hook_entry --host dsh`), or
// remote_client with `remoteConfig` naming client.json, with a payload on stdin.  A prompt hook stores and answers with
// what is remembered, which this appends to the step as a message of its own source; a Stop hook stores the turn's
// messages, which this keeps on disk (a spool) from the moment dsh commits them until the hook says how many it stored.
// dsh's session log is compressed and its hooks carry no turn or reply, which is why this is a plugin.
//
// Rules from dsh (0.2.0-rc.2): named exports only (a default export drops `inject`); a throw out of `agent/pre-step`
// fails the turn and dsh puts no time limit on it; an unhandled rejection or a throw in a timer ends the dsh process; a
// message's source must be a non-empty kind other than `plugin` (session format V4).  So every listener catches, every
// promise ends in a catch, the recall has a deadline, and the source is `plugin:scope-recall`.
//
// The spool: one file per session and process, `<session>.<pid>.jsonl`, which only that process writes, so that a rewrite
// never loses a line another process added.  A file whose process is gone (or that has been idle for hours) is taken
// whole by a rename, which one process alone wins, and added to the taker's own file.  A message sent twice is stored
// once (the store knows each by its id).  What waits is bounded: older than 14 days, or past 5,000 of a session, a
// message is dropped and said.
import { spawn } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { appendFileSync, mkdirSync, readFileSync, readdirSync, renameSync, rmSync, statSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

export const name = 'scope-recall'
export const inject = []

const SOURCE = Object.freeze({ kind: 'plugin:scope-recall', form: 'recall' })
// The hook reads at most 64 KiB of stdin.  A Stop's record takes up to RECORD_BYTES, which leaves room for the turn's
// reply beside it; one message's text is clipped to MAX_TEXT characters and TEXT_BYTES of JSON, so that it fits alone.
const PAYLOAD_BYTES = 60_000
const RECORD_BYTES = 40_000
const TEXT_BYTES = 36_000
const MAX_TEXT = 20_000
const MAX_OUTPUT = 1 << 20
// A completed turn's reply goes with its Stop only while the store still recognises the same words among the turn's
// messages (it compares moments 120 s apart at most, and the reply's moment is the hook's); a reply said earlier than
// this, or a turn that did not complete, is stored from the turn's messages alone.
const REPLY_FRESH_MS = 60_000
const SWEEP_MS = 60_000
const MAX_BACKOFF_MS = 30 * 60_000
const IDLE_SPOOL_MS = 120_000
const ORPHAN_MS = 6 * 3_600_000
const MAX_AGE_MS = 14 * 24 * 3_600_000
const MAX_KEPT = 5_000
const STATUS_EVERY_MS = 5_000
const SPOOL_NAME = /^(.+)\.(\d+)\.jsonl$/

export function apply(ctx, raw) {
  const logger = ctx.logger ?? console
  const warn = (message) => { try { logger.warn(`scope-recall: ${message}`) } catch {} }
  const cfg = settings(raw)
  if (!cfg) {
    warn('the row names no python and home; automatic recall and capture are off')
    return
  }
  const profile = safe(() => ctx.get?.('profileContext')?.name) ?? null
  const recalled = new Set()
  const turns = new Map()
  const draining = new Map()
  const status = { version: cfg.version, pid: process.pid, profile, loadedAt: new Date().toISOString(), lastRecall: null,
                   lastStore: null, backlog: 0, dropped: 0, retryAfter: null, privacyAlarm: null }
  let statusWritten = 0
  let counter = 0
  let sweeping = false
  let failures = 0
  let sweepAfter = 0
  try { mkdirSync(cfg.spool, { recursive: true }) } catch (error) { warn(`spool folder: ${error}`) }

  const writeStatus = (force = false) => {
    const now = Date.now()
    if (!force && now - statusWritten < STATUS_EVERY_MS) return
    statusWritten = now
    try { writeFileSync(cfg.statusFile, JSON.stringify({ ...status, writtenAt: new Date(now).toISOString() }, null, 2)) } catch {}
  }

  const ownFile = (sessionId) => join(cfg.spool, `${String(sessionId).replace(/[^A-Za-z0-9_.-]/g, '_')}.${process.pid}.jsonl`)

  // A line is written with a line break before it as well as after, so that one a crash cut short ends there.
  const keep = (sessionId, entry) => {
    try {
      appendFileSync(ownFile(sessionId), `\n${JSON.stringify({ k: `${process.pid}-${Date.now()}-${counter++}`, sessionId, ...entry })}\n`)
    } catch (error) { warn(`a message of session ${sessionId} was not kept on disk: ${error}`) }
  }

  // `early`: resolve as soon as the answer is whole.  A prompt hook writes its answer first and then starts the resident
  // recall server; the step need not wait for that.  The hook is still ended at the deadline if it has not exited.
  const runHook = (payload, timeoutMs, signal, early = false) => new Promise((resolve) => {
    let child
    const args = cfg.remoteConfig
      ? ['-I', '-B', '-m', 'scope_recall.adapters.codex.remote_client', '--config', cfg.remoteConfig]
      : ['-I', '-B', '-m', 'scope_recall.adapters.codex.hook_entry', '--home', cfg.home, '--host', 'dsh']
    if (!cfg.remoteConfig && cfg.envFile) args.push('--env-file', cfg.envFile)
    try {
      child = spawn(cfg.python, args, { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true })
    } catch (error) { resolve({ ok: false, error: `start: ${error}` }); return }
    let out = ''
    let err = ''
    let settled = false
    const finish = (result) => {
      if (settled) return
      settled = true
      signal?.removeEventListener?.('abort', onAbort)
      resolve(result)
    }
    const stop = (why) => { try { child.kill() } catch {} finish({ ok: false, error: why }) }
    const onAbort = () => stop('aborted')
    const timer = setTimeout(() => stop('timeout'), timeoutMs)
    timer.unref?.()
    signal?.addEventListener?.('abort', onAbort, { once: true })
    const parsed = () => { try { const answer = JSON.parse(out); return answer && typeof answer === 'object' ? answer : null } catch { return null } }
    child.stdout.setEncoding('utf8')
    child.stderr.setEncoding('utf8')
    child.stdout.on('data', (chunk) => {
      if (out.length < MAX_OUTPUT) out += chunk
      const answer = early && !settled ? parsed() : null
      if (answer) finish({ ok: true, code: null, answer, stderr: err })
    })
    child.stderr.on('data', (chunk) => { if (err.length < 65536) err += chunk })
    child.on('error', (error) => { clearTimeout(timer); finish({ ok: false, error: `run: ${error}` }) })
    child.on('close', (code) => {
      clearTimeout(timer)
      // A hook that ended otherwise than with 0 and no whole answer failed (its interpreter, its package): it says why on
      // stderr, which goes with the failure instead of reading as nothing recalled.
      const answer = out.trim() ? parsed() : code === 0 ? {} : null
      finish(answer ? { ok: true, code, answer, stderr: err } : { ok: false, error: `exit ${code}${tail(err)}`, stderr: err })
    })
    child.stdin.on('error', () => {})
    child.stdin.end(JSON.stringify(payload))
  })

  // -- recall -------------------------------------------------------------------------------------------------------
  ctx.on('agent/pre-step', async (payload, next) => {
    const decision = await next()
    try {
      if (decision?.kind !== 'enter' || payload?.signal?.aborted) return decision
      const session = payload.agent?.session
      const header = session?.header ?? {}
      if (header.origin === 'subagent') return decision
      const sessionId = header.id ?? session?.id
      const prompt = clip(humanText(decision.messages))
      if (!sessionId || !prompt) return decision
      const key = `${sessionId}:${payload.turn}`
      if (recalled.has(key)) return decision
      recalled.add(key)
      if (recalled.size > 1000) recalled.delete(recalled.values().next().value)
      const started = Date.now()
      const result = await runHook({ hook_event_name: 'UserPromptSubmit', session_id: sessionId,
                                     turn_id: String(payload.turn), prompt, cwd: header.cwd ?? process.cwd() },
                                   cfg.recallTimeoutMs, payload.signal, true)
      const context = result.ok ? result.answer?.hookSpecificOutput?.additionalContext : null
      status.lastRecall = { at: new Date().toISOString(), ms: Date.now() - started,
                            outcome: result.ok ? (context ? 'recalled' : 'nothing') : result.error }
      writeStatus()
      if (typeof context !== 'string' || !context.trim() || payload.signal?.aborted) return decision
      return { ...decision, messages: [...decision.messages,
        { id: randomUUID(), role: 'user', content: [{ type: 'text', text: context }], source: SOURCE }] }
    } catch (error) {
      warn(`recall skipped: ${error}`)
      return decision
    }
  }, { prepend: true })

  // -- capture ------------------------------------------------------------------------------------------------------
  ctx.on('session/event', (session, event) => {
    try {
      const header = session?.header ?? {}
      if (header.origin === 'subagent' || !event) return
      const sessionId = session?.id ?? header.id
      if (!sessionId) return
      const data = event.data ?? {}
      const cwd = header.cwd ?? null
      switch (event.type) {
        case 'turn/start':
          turns.set(sessionId, data.turn)
          return
        case 'user/message': {
          if (data.source?.kind !== 'user') return  // dsh's own context and ours are not the person's
          const text = clip(textOf(data.content))
          if (text) keep(sessionId, { role: 'user', id: data.id, text, time: event.time, turn: turns.get(sessionId) ?? null, cwd })
          return
        }
        case 'assistant/message': {
          const message = data.message ?? {}
          const text = clip(textOf(message.content))
          if (text) keep(sessionId, { role: 'assistant', id: message.id, text, time: event.time, turn: data.turn ?? null, cwd })
          return
        }
        case 'turn/end':
          keep(sessionId, { role: 'turn_end', turn: data.turn ?? null, reason: data.reason?.kind ?? null, time: event.time, cwd })
          recalled.delete(`${sessionId}:${data.turn}`)
          drain(sessionId).catch((error) => warn(`store of session ${sessionId}: ${error}`))
          return
        case 'session-log-deepseek/delivery-accepted':
          // dsh uploaded its session log, recalled memories with it: the upload is meant to be off.
          status.privacyAlarm = new Date().toISOString()
          warn('PRIVACY: dsh uploaded a session log to its model API; switch session-log-deepseek off')
          writeStatus(true)
          return
      }
    } catch (error) { warn(`capture skipped: ${error}`) }
  })

  // -- store --------------------------------------------------------------------------------------------------------
  // A missing file holds nothing; one that cannot be read throws, so that nothing is rewritten from a failed read.
  const readSpool = (file) => {
    let text
    try { text = readFileSync(file, 'utf8') } catch (error) { if (error?.code === 'ENOENT') return []; throw error }
    return text.split('\n').filter(Boolean).map((line) => { try { return JSON.parse(line) } catch { return null } })
      .filter((entry) => entry && typeof entry === 'object')
  }

  const replace = (file, entries) => {
    const temporary = `${file}.${process.pid}.tmp`
    writeFileSync(temporary, entries.map((entry) => JSON.stringify(entry)).join('\n') + '\n')
    renameSync(temporary, file)
  }

  // One store at a time per session; a turn that ends meanwhile runs it once more after.  Resolves true when nothing
  // failed.  The promise is kept so that dsh's shutdown can wait for it.
  function drain(sessionId) {
    const running = draining.get(sessionId)
    if (running) { running.again = true; return running.promise }
    const state = { again: false, promise: null }
    state.promise = (async () => {
      let ok = true
      try {
        do {
          state.again = false
          ok = await drainOnce(sessionId)
        } while (state.again && ok)
      } finally { draining.delete(sessionId) }
      return ok
    })()
    draining.set(sessionId, state)
    return state.promise
  }

  async function drainOnce(sessionId) {
    const file = ownFile(sessionId)
    const entries = readSpool(file)
    const now = Date.now()
    const all = entries.filter(isMessage)
    const dropped = new Set(all.filter((entry) => !(now - entry.time < MAX_AGE_MS)).map((entry) => entry.k))
    const young = all.filter((entry) => !dropped.has(entry.k))
    for (const entry of young.slice(0, Math.max(0, young.length - MAX_KEPT))) dropped.add(entry.k)
    const messages = all.filter((entry) => !dropped.has(entry.k))
    const lastEnd = entries.filter((entry) => entry.role === 'turn_end').at(-1)
    const last = lastEnd?.reason === 'completed'
      ? messages.filter((entry) => entry.role === 'assistant' && entry.turn === lastEnd.turn).at(-1) : null
    const reply = last && now - last.time <= REPLY_FRESH_MS ? last : null
    const stored = new Set()
    let failure = null
    // A Stop stores what it can in its time (the hook reads at most 3 s of lines): the rest goes with the next one, and
    // only a Stop that stores nothing ends the store.
    let queue = messages
    while (queue.length) {
      const [chunk] = chunks(queue)
      const payload = { hook_event_name: 'Stop', session_id: sessionId, cwd: lastEnd?.cwd ?? chunk[0]?.cwd ?? process.cwd(),
                        record: chunk.map(({ id, role, text, time }) => ({ id, role, text, time })) }
      if (reply && chunk.includes(reply) && bytes(payload) + bytes(reply.text) <= PAYLOAD_BYTES) {
        Object.assign(payload, { turn_id: String(lastEnd.turn), last_assistant_message: reply.text })
      }
      const result = await runHook(payload, cfg.storeTimeoutMs)
      const through = result.ok ? Number(result.answer?.through) : NaN
      if (!Number.isInteger(through) || through < 0) { failure = result.error ?? 'no count in the answer'; break }
      if (through === 0) { failure = 'nothing stored'; break }
      for (const entry of chunk.slice(0, through)) stored.add(entry.k)
      queue = queue.slice(Math.min(through, chunk.length))
    }
    // Synchronous from here: no event of this process can append in between, and no other process writes this file.
    try {
      const left = readSpool(file).filter((entry) => !stored.has(entry.k) && !dropped.has(entry.k))
      if (!left.some(isMessage)) rmSync(file, { force: true })
      else replace(file, left)
    } catch (error) {
      failure ??= `spool: ${error}`
      warn(`spool of session ${sessionId}: ${error}`)
    }
    if (dropped.size) {
      status.dropped += dropped.size
      warn(`session ${sessionId}: ${dropped.size} message(s) dropped unstored, older than 14 days or past ${MAX_KEPT} waiting`)
    }
    status.backlog = countBacklog()
    status.lastStore = { at: new Date().toISOString(), stored: stored.size, left: status.backlog, error: failure }
    writeStatus(true)
    if (failure) warn(`session ${sessionId}: message(s) kept to store later (${failure})`)
    return !failure
  }

  const spoolNames = () => {
    try { return readdirSync(cfg.spool) } catch { return [] }
  }

  const countBacklog = () => {
    let total = 0
    for (const name of spoolNames()) {
      if (!SPOOL_NAME.test(name)) continue
      try { total += readSpool(join(cfg.spool, name)).filter(isMessage).length } catch {}
    }
    return total
  }

  // Another process's file, or a stray one of this process: taken whole by a rename (one process alone wins it, and a
  // line its owner writes after goes to a new file of that owner's) and added to this process's file of the session.
  const adopt = (file) => {
    const taken = join(cfg.spool, `taken-${counter++}.${process.pid}.jsonl`)
    try { renameSync(file, taken) } catch { return null }
    const entries = readSpool(taken)
    const sessionId = entries.find((entry) => typeof entry.sessionId === 'string')?.sessionId
    if (!sessionId) {
      try { renameSync(taken, `${taken}.unreadable`) } catch {}
      warn(`spool: ${file} holds nothing readable; kept as ${taken}.unreadable`)
      return null
    }
    appendFileSync(ownFile(sessionId), `\n${entries.map((entry) => JSON.stringify(entry)).join('\n')}\n`)
    rmSync(taken, { force: true })
    return sessionId
  }

  // What a turn that never ended here, a store that failed, or a process that is gone left on disk: stored once the
  // file is idle, one session at a time; the first failure ends the pass, and the next waits longer, up to 30 minutes.
  async function sweep() {
    if (sweeping || Date.now() < sweepAfter) return
    sweeping = true
    try {
      for (const name of spoolNames()) {
        const match = SPOOL_NAME.exec(name)
        const file = join(cfg.spool, name)
        let idle
        try { idle = Date.now() - statSync(file).mtimeMs } catch { continue }
        if (!match) {
          if (name.endsWith('.tmp') && idle > ORPHAN_MS) rmSync(file, { force: true })  // a rewrite a crash cut short
          continue
        }
        const pid = Number(match[2])
        const mine = pid === process.pid
        if (idle < IDLE_SPOOL_MS || (!mine && alive(pid) && idle < ORPHAN_MS)) continue
        let sessionId = mine ? readSpool(file).find((entry) => typeof entry.sessionId === 'string')?.sessionId : null
        if (!sessionId || file !== ownFile(sessionId)) sessionId = adopt(file)
        if (!sessionId || draining.has(sessionId)) continue
        if (!(await drain(sessionId))) {
          failures += 1
          sweepAfter = Date.now() + Math.min(MAX_BACKOFF_MS, SWEEP_MS * 2 ** failures)
          status.retryAfter = new Date(sweepAfter).toISOString()
          writeStatus(true)
          return
        }
      }
      failures = 0
      status.retryAfter = null
    } finally { sweeping = false }
  }

  ctx.effect?.(() => {
    const timer = setInterval(() => { sweep().catch((error) => warn(`sweep: ${error}`)) }, SWEEP_MS)
    timer.unref?.()
    const first = setTimeout(() => { sweep().catch((error) => warn(`sweep: ${error}`)) }, 5_000)
    first.unref?.()
    writeStatus(true)
    return async () => {
      clearInterval(timer)
      clearTimeout(first)
      await Promise.race([Promise.allSettled([...draining.values()].map((state) => state.promise)), sleep(3_000)])
      writeStatus(true)
    }
  })
}

function settings(raw) {
  if (!raw || typeof raw.python !== 'string' || !raw.python || typeof raw.home !== 'string' || !raw.home) return null
  const number = (value, fallback, low, high) => Number.isFinite(Number(value)) ? Math.min(high, Math.max(low, Number(value))) : fallback
  return {
    python: raw.python,
    home: raw.home,
    // The remote forwarder owns HTTP/auth; this plugin still owns its record and acknowledgement cursor.
    remoteConfig: typeof raw.remoteConfig === 'string' && raw.remoteConfig ? raw.remoteConfig : null,
    envFile: typeof raw.envFile === 'string' && raw.envFile ? raw.envFile : null,
    version: typeof raw.version === 'string' ? raw.version : null,
    // The hook's own budget is the entry's hook_processing_seconds (at most 6 s), plus its interpreter's start.
    recallTimeoutMs: number(raw.recallTimeoutMs, 9_000, 1_000, 30_000),
    storeTimeoutMs: number(raw.storeTimeoutMs, 20_000, 2_000, 60_000),
    spool: typeof raw.spool === 'string' && raw.spool ? raw.spool : join(raw.home, 'scope-recall', 'dsh-spool'),
    statusFile: join(raw.home, 'scope-recall', 'dsh-plugin-status.json'),
  }
}

function isMessage(entry) {
  return entry.role === 'user' || entry.role === 'assistant'
}

function bytes(value) {
  return Buffer.byteLength(JSON.stringify(value), 'utf8')
}

// The end of what a hook wrote on stderr, for a failure: its last two lines, at most 300 characters.
function tail(text) {
  const end = String(text ?? '').trim().split(/\r?\n/).slice(-2).join(' | ').slice(-300)
  return end ? `: ${end}` : ''
}

function chunks(messages) {
  const result = []
  let current = []
  let size = 1024
  for (const entry of messages) {
    const more = bytes(entry) + 64
    if (current.length && size + more > RECORD_BYTES) { result.push(current); current = []; size = 1024 }
    current.push(entry)
    size += more
  }
  if (current.length) result.push(current)
  return result
}

// Whether a process with this id runs (one this account may not signal does).
function alive(pid) {
  if (!Number.isSafeInteger(pid) || pid <= 0) return false
  try { process.kill(pid, 0); return true } catch (error) { return error?.code === 'EPERM' }
}

function textOf(content) {
  if (typeof content === 'string') return content.trim()
  if (!Array.isArray(content)) return ''
  return content.filter((block) => block?.type === 'text' && typeof block.text === 'string').map((block) => block.text).join('\n').trim()
}

// The person's words of this step: the last message of theirs.  dsh claims one queued message for a turn's first step;
// another of theirs claimed with it is stored from the turn's record.
function humanText(messages) {
  const own = (Array.isArray(messages) ? messages : []).filter((message) => message?.source?.kind === 'user')
  return own.length ? textOf(own.at(-1).content) : ''
}

function clip(text) {
  if (!text || (text.length <= MAX_TEXT && bytes(text) <= TEXT_BYTES)) return text
  let kept = text.slice(0, MAX_TEXT)
  while (kept && bytes(kept) > TEXT_BYTES - 200) kept = kept.slice(0, Math.floor(kept.length * 0.9))
  return `${kept}\n[... ${text.length - kept.length} more characters not kept by Scope Recall]`
}

function safe(read) { try { return read() } catch { return null } }

function sleep(ms) { return new Promise((resolve) => { const timer = setTimeout(resolve, ms); timer.unref?.() }) }
