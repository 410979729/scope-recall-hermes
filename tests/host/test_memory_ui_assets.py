"""Execute shipped browser code against a small text-only DOM using the host's Node helper."""

import json
import os
import subprocess
from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parents[2] / "maintenance" / "ui_static"
GUIDE = ASSETS.parents[1] / "docs" / "ui.md"
DOM_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const root = process.argv[1];
const elements = new Map();
class Element {
  constructor(tag = 'div') {
    this.content = 'synthetic-session'; this.tagName = tag; this.children = [];
    this.text = ''; this.value = ''; this.dataset = {}; this.elements = {};
  }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(child => child.textContent).join(''); }
  set innerHTML(value) { throw new Error('Markup sink used: ' + value); }
  set outerHTML(value) { throw new Error('Markup sink used: ' + value); }
  insertAdjacentHTML() { throw new Error('Markup sink used'); }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren() { this.children = []; this.text = ''; }
  addEventListener(name, callback) { this[name] = callback; }
  scrollIntoView() {}
  setAttribute() {}
  removeAttribute() {}
}
global.window = {};
global.document = {
  documentElement: new Element('html'),
  createElement: tag => new Element(tag),
  getElementById: id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); },
  querySelectorAll: () => []
};
Object.defineProperty(global, 'navigator', {value: {language: 'zh-CN'}, configurable: true});
global.location = {pathname: '/', search: '?token=synthetic-token'};
global.history = window.history = {replaceState: () => { location.search = ''; }};
global.fetch = async () => ({ok: true, json: async () => ({data: {rules: []}})});
vm.runInThisContext(fs.readFileSync(root + '/strings.js', 'utf8'));
vm.runInThisContext(fs.readFileSync(root + '/app.js', 'utf8'));
"""
HARNESS = (
    DOM_HARNESS
    + r"""
const payload = '<img src=x onerror="global.executed=true"><script>global.executed=true</script>';
display('detail-data', {content: payload});
if (JSON.parse(document.getElementById('detail-data').textContent).content !== payload)
  throw new Error('Stored content was not preserved as text');
if (global.executed) throw new Error('Stored content executed');
const en = Object.keys(window.UI_STRINGS.en).sort();
const zh = Object.keys(window.UI_STRINGS.zh).sort();
if (JSON.stringify(en) !== JSON.stringify(zh)) throw new Error('Language keys differ');
const html = fs.readFileSync(root + '/index.html', 'utf8');
for (const match of html.matchAll(/data-i18n="([^"]+)"/g)) {
  if (!window.UI_STRINGS.en[match[1]] || !window.UI_STRINGS.zh[match[1]])
    throw new Error('Missing translation: ' + match[1]);
}
if (!en.every(key => typeof window.UI_STRINGS.en[key] === 'string' && window.UI_STRINGS.zh[key].length > 0))
  throw new Error('Empty translation');
if (document.documentElement.lang !== 'zh') throw new Error('Browser language was not selected');
vm.runInThisContext("language = 'en'; translate();");
if (document.documentElement.lang !== 'en') throw new Error('Language switch did not apply');
global.FormData = class { *[Symbol.iterator]() {} };
global.fetch = async () => ({ok: true, json: async () => ({data: {
  items: [{kind: 'claim', ref: 'claim-synthetic', content: 'NameOne'}],
  examined: 1, scan_complete: true, next_cursor: null
}})});
vm.runInThisContext("language = 'zh'; browse(false);").then(() => {
document.getElementById('language').value = 'en';
document.getElementById('language').change();
if (!document.getElementById('items').textContent.includes(window.UI_STRINGS.en.claim))
  throw new Error('Browse kind did not switch language');
if (!document.getElementById('scan').textContent.includes(window.UI_STRINGS.en.examined))
  throw new Error('Browse scan did not switch language');
if (location.search) throw new Error('Launch token remains in browser history');
console.log(JSON.stringify({literal: true, bilingual: en.length}));
});
"""
)
BROWSE_HARNESS = (
    DOM_HARNESS
    + r"""
const assert = require('assert/strict');
global.FormData = class {
  *[Symbol.iterator]() { yield ['query', document.getElementById('filters').value]; }
};
(async () => {
  // Let the initial overview finish before controlling the search responses.
  await new Promise(resolve => setImmediate(resolve));
  const pending = [];
  global.fetch = url => new Promise((resolve, reject) => pending.push({
    url, reject, resolve: data => resolve({ok: true, json: async () => ({data})})
  }));
  function start(query) {
    document.getElementById('filters').value = query;
    return browse(false);
  }
  function page(query, cursor) {
    return {items: [{kind: 'claim', ref: query, content: query}],
      examined: 1, scan_complete: !cursor, next_cursor: cursor};
  }
  const alpha = start('alpha');
  const beta = start('beta');
  const during = browse(true);
  assert.equal(pending.length, 2, 'Pending search started a continuation');
  assert.equal(document.getElementById('next').disabled, true);
  await during;
  pending[1].resolve(page('beta', 'beta-next'));
  await beta;
  pending[0].resolve(page('alpha', 'alpha-next'));
  await alpha;
  assert.deepEqual(browseItems.map(item => item.ref), ['beta'], 'Superseded search mixed its results');
  assert.equal(continuation, 'beta-next', 'Superseded search replaced the cursor');
  assert.equal(searchFilters.query, 'beta');
  assert.equal(document.getElementById('next').disabled, false);

  const more = browse(true);
  const duplicate = browse(true);
  assert.equal(pending.length, 3, 'Overlapping continuation sent a duplicate request');
  await duplicate;
  assert.equal(new URL(pending[2].url, 'http://localhost').searchParams.get('cursor'), 'beta-next');
  translate();
  assert.equal(document.getElementById('next').disabled, true, 'Language switch enabled a pending continuation');

  const gamma = start('gamma');
  pending[2].resolve(page('beta-later', 'beta-last'));
  await more;
  const blocked = browse(true);
  assert.equal(pending.length, 4, 'Stale continuation released the current search');
  await blocked;
  assert.equal(document.getElementById('next').disabled, true);
  pending[3].resolve(page('gamma', 'gamma-next'));
  await gamma;
  assert.deepEqual(browseItems.map(item => item.ref), ['gamma']);
  assert.equal(continuation, 'gamma-next');

  const stale = start('alpha');
  const current = start('beta');
  pending[4].reject(new Error('superseded failure'));
  await stale;
  pending[5].resolve(page('beta', null));
  await current;
  assert.deepEqual(browseItems.map(item => item.ref), ['beta']);
  assert.equal(document.getElementById('next').disabled, true);
  const exhausted = browse(true);
  assert.equal(pending.length, 6, 'Exhausted search started another request');
  await exhausted;
  console.log(JSON.stringify({reversed: true, continuation: true}));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
)


def test_stored_markup_is_literal_and_interface_is_bilingual():
    executable = os.environ.get("SCOPE_RECALL_TEST_NODE")
    if not executable:
        pytest.skip("host tier requires its declared Node helper")
    completed = subprocess.run([executable, "-e", HARNESS, str(ASSETS)], capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["literal"] and result["bilingual"] >= 60


def test_superseded_searches_and_overlapping_continuations_are_discarded():
    executable = os.environ.get("SCOPE_RECALL_TEST_NODE")
    if not executable:
        pytest.skip("host tier requires its declared Node helper")
    completed = subprocess.run(
        [executable, "-e", BROWSE_HARNESS, str(ASSETS)], capture_output=True, text=True, timeout=15
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"reversed": True, "continuation": True}


def test_html_has_external_scripts_and_styles_and_no_event_handlers():
    from html.parser import HTMLParser

    class Page(HTMLParser):
        def handle_starttag(self, tag, attrs):
            values = dict(attrs)
            assert not any(key.startswith("on") for key in values)
            assert "style" not in values
            if tag == "script":
                assert values.get("src", "") in {"/strings.js", "/app.js"}

    Page().feed((ASSETS / "index.html").read_text(encoding="utf-8"))


def test_user_guide_covers_startup_pages_and_local_access():
    guide = GUIDE.read_text(encoding="utf-8")
    for instruction in (
        "scope-recall ui --root",
        "--host",
        "--instance-root",
        "Ctrl+C",
        "127.0.0.1",
        "token",
        "POST",
        "**Overview**",
        "**Browse and search**",
        "**Private mode**",
        "**Export**",
        "**Backup status**",
    ):
        assert instruction in guide


def test_recall_history_renders_reasons_and_only_available_action_links():
    executable = os.environ.get("SCOPE_RECALL_TEST_NODE")
    if not executable:
        pytest.skip("host tier requires its declared Node helper")
    script = (
        DOM_HARNESS
        + r"""
const assert = require('assert/strict');
vm.runInThisContext(`recallRows = [{timestamp: '2026-09-06', request_id: 'TEST-attempt', entry_id: 'TEST-entry',
 session_id: 'TEST-session', scope_bound: true, reasons: ['threshold'], background: {pending: 4},
 capture: {state: 'persisted'}, items: [
  {ref: 'event-visible', revision: 1, rank: 1, reason: 'delivered', readable: true, channels: {lexical: {rank: 1}}},
  {ref: 'event-unavailable', reason: 'deleted', readable: false}
 ]}]; renderRecalls();`);
const text = document.getElementById('recall-items').textContent;
assert(text.includes('TEST-attempt'));
assert(text.includes('event-visible@1'));
assert(!text.includes('event-unavailable'));
assert(text.includes(window.UI_STRINGS.zh.reason_threshold));
assert(/[\u4e00-\u9fff]/.test(window.UI_STRINGS.zh.reason_threshold));
vm.runInThisContext("language = 'en'; translate();");
assert(document.getElementById('recall-items').textContent.includes('Below the relevance threshold'));
console.log(JSON.stringify({recalls: true, visibility: true, bilingual: true}));
"""
    )
    completed = subprocess.run([executable, "-e", script, str(ASSETS)], capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"recalls": True, "visibility": True, "bilingual": True}


def test_health_page_groups_findings_and_pages_undo_actions():
    executable = os.environ.get("SCOPE_RECALL_TEST_NODE")
    if not executable:
        pytest.skip("host tier requires its declared Node helper")
    script = (
        DOM_HARNESS
        + r"""
const assert = require('assert/strict');
(async () => {
  await new Promise(resolve => setImmediate(resolve));
  const calls = [];
  global.fetch = async url => {
    calls.push(url);
    const data = url.startsWith('/api/health-actions') ?
      {items: [{action_id: 'TEST-action', action: 'health_fact_choose', recorded_at: 'TEST-time',
        versions: {'claim-TEST': 2}}],
        next_before: calls.filter(x => x.startsWith('/api/health-actions')).length === 1 ? 17 : null} :
      {entities: [{name: 'TEST-name', facts: [{ref: 'claim-a', revision: 1}, {ref: 'claim-b', revision: 1}]}],
        duplicates: [], contradictions: [], unused: [], observation_start: 'TEST-start', observation_end: 'TEST-end',
        observation_days: 30, records_incomplete: true, complete: false, next_cursor: 'TEST-next'};
    return {ok: true, json: async () => ({data})};
  };
  await loadHealth(false);
  assert.equal(document.getElementById('health-items').children.length, 3);
  assert.ok(document.getElementById('health-items').textContent.includes('TEST-name'));
  assert.ok(document.getElementById('health-period').textContent.includes(window.UI_STRINGS.zh.recordsIncomplete));
  assert.ok(document.getElementById('health-actions').textContent.includes('claim-TEST'));
  assert.ok(document.getElementById('health-actions').textContent.includes(window.UI_STRINGS.zh['health-choose']));
  assert.equal(document.getElementById('health-actions-next').disabled, false);
  await loadHealthActions(true);
  assert.ok(calls.some(x => x.includes('health-actions?before=17')));
  assert.equal(document.getElementById('health-actions-next').disabled, true);
  vm.runInThisContext("language = 'en'; translate();");
  assert.ok(document.getElementById('health-items').textContent.includes(window.UI_STRINGS.en.health_duplicateGroup));
  assert.ok(document.getElementById('health-actions').textContent.includes(window.UI_STRINGS.en['health-choose']));
  console.log(JSON.stringify({groups: 3, actions: true}));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    )
    completed = subprocess.run([executable, "-e", script, str(ASSETS)], capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"groups": 3, "actions": True}
