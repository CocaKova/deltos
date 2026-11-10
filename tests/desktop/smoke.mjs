// Load the bundled Memory page against the stub SDK, register it, and drive it
// through every state it can be in: loading, the summary, the list with its
// filters and search, a thing (checked, unsure, disputed, retired and mistaken
// values), another day, changes, worth a look, an empty store, a backend that
// cannot open memory, a backend of another version, a name that is not there,
// and requests that fail. Every answer comes from tests/desktop/fixtures/, which
// make_fixtures.py writes from the made-up demo store through the real backend.
// Catches identifier typos, bad register shapes, render-time throws, wrong
// requests and missing states.
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { __stub as stub } from './stub-sdk.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const fixture = name => JSON.parse(readFileSync(join(here, 'fixtures', `${name}.json`), 'utf8'))
const DEMO = fixture('demo')
const EMPTY = fixture('empty')
const DOWN = fixture('down')
const SOURCE = readFileSync(join(here, '..', '..', 'adapters', 'hermes', 'deltos', 'desktop', 'plugin.js'), 'utf8')
const VERSION = DEMO['/health'].version
const TODAY = DEMO['/health'].today

// ---------------------------------------------------------------------------
// checks
// ---------------------------------------------------------------------------

let failures = 0
let passed = 0

function ok(label, condition, detail = '') {
  if (condition) {
    passed += 1

    return
  }
  failures += 1
  console.log(`FAIL  ${label}${detail ? `\n      ${detail}` : ''}`)
}

function has(label, haystack, needle) {
  ok(label, haystack.includes(needle), `missing: ${JSON.stringify(needle)}\n      in: ${haystack.slice(0, 600)}`)
}

function lacks(label, haystack, needle) {
  ok(label, !haystack.includes(needle), `should not hold: ${JSON.stringify(needle)}`)
}

function same(label, got, want) {
  ok(label, JSON.stringify(got) === JSON.stringify(want), `got  ${JSON.stringify(got)}\n      want ${JSON.stringify(want)}`)
}

// An independent reading of the page's dates ("5 Nov 2026").
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
const nice = day => `${Number(day.slice(8, 10))} ${MONTHS[Number(day.slice(5, 7)) - 1]} ${day.slice(0, 4)}`
const back = days => new Date(Date.parse(`${TODAY}T00:00:00Z`) - days * 86400000).toISOString().slice(0, 10)
const clone = value => JSON.parse(JSON.stringify(value))
const tick = (ms = 2) => new Promise(done => setTimeout(done, ms))

// ---------------------------------------------------------------------------
// the world the page runs in
// ---------------------------------------------------------------------------

const registered = []
const disposers = []
const listeners = {}
const calls = []
const clipboard = []
const styles = []
const world = { routes: DEMO, fail: {}, clipboardWorks: true }

function httpError(status, detail) {
  return Object.assign(new Error(`${status}: ${JSON.stringify({ detail })}`), { statusCode: status })
}

/** The same failure as the page gets it in the app when it is not a 404: wrapped by
 *  the app's process boundary, and without the numeric status. */
function wrappedError(status, detail) {
  return new Error(`Error invoking remote method 'hermes:api': Error: ${status}: ${JSON.stringify({ detail })}`)
}

const ctx = {
  source: 'plugin:deltos',
  registerMany(list) {
    registered.push(...list)

    return () => {}
  },
  register(c) {
    registered.push(c)

    return () => {}
  },
  onDispose(fn) {
    disposers.push(fn)
  },
  onEvent(type, fn) {
    ;(listeners[type] ||= []).push(fn)

    return () => {}
  },
  setTimeout(fn) {
    const timer = setTimeout(fn, 0)

    return () => clearTimeout(timer)
  },
  // The same door the app gives the page: a path under /api/plugins/deltos.
  rest: async (path, opts) => {
    const request = decodeURIComponent(path)
    calls.push(request)
    if (opts && opts.method && opts.method !== 'GET') throw new Error(`the page is read-only, but sent ${opts.method} ${request}`)
    const route = request.split('?')[0]
    if (world.fail[request] || world.fail[route]) throw world.fail[request] || world.fail[route]
    if (!(request in world.routes)) throw httpError(404, `no fixture for ${request}`)

    return clone(world.routes[request])
  },
  os: {
    writeClipboard: async text => {
      if (world.clipboardWorks) clipboard.push(text)

      return world.clipboardWorks
    }
  }
}

// A document just big enough for the page to bring its styles.
globalThis.document = {
  createElement: tag => {
    const node = { tag, textContent: '', removed: false, remove: () => (node.removed = true) }
    styles.push(node)

    return node
  },
  head: { append: () => {} }
}

const plugin = (await import('./deltos-bundle.mjs')).default
plugin.register(ctx)

const page = registered.find(c => c.area === 'routes')
const nav = registered.find(c => c.area === 'sidebar.nav')
const palette = registered.filter(c => c.area === 'palette')

/** A fresh visit: every atom back to its start, the query cache empty. */
function scene(routes, { fail = {} } = {}) {
  for (const store of stub.atoms) store.reset()
  stub.queries.clear()
  stub.notes.length = 0
  stub.navigated.length = 0
  stub.invalidated.length = 0
  stub.newChats = 0
  stub.composer = null
  calls.length = 0
  clipboard.length = 0
  world.routes = routes
  world.fail = fail
  world.clipboardWorks = true
}

/** The tree as plain nodes, function components called the way React would. */
function expand(node) {
  if (node == null || typeof node === 'boolean') return []
  if (typeof node === 'string' || typeof node === 'number') return [String(node)]
  if (Array.isArray(node)) return node.flatMap(expand)
  if (typeof node.type === 'function') return expand(node.type(node.props || {}))
  const { children, ...props } = node.props || {}

  return [{ type: node.type, props, kids: expand(children) }]
}

const raw = node => (typeof node === 'string' ? node : ` ${node.kids.map(raw).join('')} `)
const textOf = nodes => (Array.isArray(nodes) ? nodes : [nodes]).map(raw).join('').replace(/\s+/g, ' ').trim()

function all(nodes, test, out = []) {
  for (const node of nodes) {
    if (typeof node === 'string') continue
    if (test(node)) out.push(node)
    all(node.kids, test, out)
  }

  return out
}

const byClass = (nodes, name) => all(nodes, n => String(n.props.className || '').split(' ').includes(name))
const pending = () => [...stub.queries.values()].some(entry => entry.status === 'pending')

/** Render now, without waiting for anything. */
const peek = () => expand(page.render())

/** Render until every request the page asked for has answered. */
async function show() {
  for (let round = 0; round < 60; round++) {
    const tree = peek()
    if (!pending()) return tree
    await tick()
  }
  throw new Error('the page never stopped loading')
}

/** Press the control whose text (or aria-label) is `label`, then let it settle. */
async function press(tree, label, { within } = {}) {
  const pool = within || tree
  const hits = all(
    pool,
    n => typeof n.props.onClick === 'function' && (textOf(n) === label || n.props['aria-label'] === label)
  )
  if (!hits.length) {
    ok(`a control called "${label}"`, false, `controls: ${all(pool, n => typeof n.props.onClick === 'function').map(n => textOf(n) || n.props['aria-label']).join(' | ')}`)

    return tree
  }
  await hits[0].props.onClick()
  await tick()

  return show()
}

/** Open the thing called `name` from the list. */
async function openRow(tree, name) {
  const hit = byClass(tree, 'deltos-row').find(r => textOf(byClass([r], 'deltos-row-name')) === name)
  if (!hit) {
    ok(`a list row called "${name}"`, false)

    return tree
  }
  await hit.props.onClick()
  await tick()

  return show()
}

const ask = row => all([row], n => typeof n.props.onClick === 'function' && textOf(n) === 'Ask the agent to check')[0].props.onClick()
const rowNames = tree => byClass(tree, 'deltos-row').map(row => textOf(byClass([row], 'deltos-row-name')))
const chip = (tree, label) => byClass(tree, 'deltos-chip').find(c => textOf(c).startsWith(label))
// How the page prints a label: the demo's "Url" and "Ip" read as "URL" and "IP".
const shown = label => label.replace(/\b(url|ip)\b/gi, word => word.toUpperCase())
const entryOf = (tree, label) =>
  byClass(tree, 'deltos-entry').find(e => textOf(byClass([e], 'deltos-entry-label')).startsWith(shown(label)))

// ---------------------------------------------------------------------------
// 1. what the plugin registers
// ---------------------------------------------------------------------------

console.log('id        :', plugin.id)
console.log('name      :', plugin.name)
console.log('areas     :', registered.map(c => `${c.id}@${c.area}`).join(', '))

same('the plugin id is deltos', plugin.id, 'deltos')
ok('a full page is registered', page && page.data.path === '/deltos' && typeof page.render === 'function')
same('the sidebar row', nav && nav.data, { path: '/deltos', label: 'Memory', codicon: 'notebook' })
ok('two palette commands', palette.length === 2 && palette.every(c => typeof c.data.run === 'function' && c.data.label && c.data.id.startsWith('deltos.')))
same('only a style element is made', styles.map(s => s.tag), ['style'])
has('the styles came in', styles[0].textContent, '.deltos-page')
ok('the styles leave with the plugin', disposers.length === 1 && (disposers[0](), styles[0].removed))
has('the pane version matches the fixtures', SOURCE, `const PLUGIN_VERSION = '${VERSION}'`)

// The catalog's rules for a desktop plugin: its own backend through ctx.rest, nothing else.
for (const banned of [
  'eval(',
  'new Function',
  'XMLHttpRequest',
  'WebSocket',
  'import(',
  'http://',
  'https://',
  'querySelector',
  'hermesDesktop',
  "createElement('script'",
  '<script',
  'data-slot',
  'localStorage',
  'innerHTML'
]) {
  lacks(`the source stays inside the SDK (${banned})`, SOURCE, banned)
}
ok('the source never calls fetch itself', !/(^|[^.\w])fetch\(/m.test(SOURCE))
same(
  'only allowed modules are imported',
  [...SOURCE.matchAll(/^import .* from '([^']+)'$/gm)].map(m => m[1]).sort(),
  ['@hermes/plugin-sdk', 'react/jsx-runtime']
)

// ---------------------------------------------------------------------------
// 2. loading, then the summary
// ---------------------------------------------------------------------------

scene(DEMO)
let tree = peek()
ok('loading shows placeholders', all(tree, n => n.type === 'skeleton').length >= 7)
has('loading still says where you are', textOf(tree), 'Memory')
await tick()
same('loading asks only for health', calls, ['/health'])

tree = await show()
let text = textOf(tree)
const o = DEMO['/overview?days=7']
const right = o.looks.outcomes.same || 0
const looked = right + (o.looks.outcomes.changed || 0)
for (const want of [
  `${o.things} things`,
  `${o.entries} facts`,
  `${o.past} past values kept`,
  `${o.checked} seen with a check`,
  `${o.unsure} not confirmed for 3 weeks`,
  `${o.disputed} a note disagrees`,
  `${o.changed} changed in 7 days`,
  `${right} of ${looked} checks matched memory`
]) {
  has(`summary: ${want}`, text, want)
}
ok('the demo has looks to report', looked > 0)
has('the tab carries how many are worth a look', text, `Worth a look · ${o.due}`)
lacks('no version notice when the two halves match', text, 'Quit and reopen')
ok('the first visit asks for health, overview, things and the first thing', ['/health', '/overview?days=7', '/things'].every(c => calls.includes(c)))

// ---------------------------------------------------------------------------
// 3. the list: every thing, filters, search
// ---------------------------------------------------------------------------

const THINGS = DEMO['/things'].things
same('every thing is listed, newest change first', rowNames(tree), THINGS.map(t => t.name))
const dim = byClass(tree, 'is-dim').map(row => textOf(byClass([row], 'deltos-row-name')))
same('things no longer in use are dimmed', dim, THINGS.filter(t => t.dormant).map(t => t.name))
ok('the demo has a thing no longer in use', dim.length > 0)
has('a dimmed row says why', textOf(byClass(tree, 'is-dim')), 'no longer in use')
same('the first thing is open by default', calls.filter(c => c.startsWith('/thing?')), [`/thing?name=${THINGS[0].slug}`])

const week = t => {
  const days = (Date.parse(TODAY) - Date.parse(t.changed)) / 86400000

  return days >= 0 && days <= 7
}
const FILTERS = {
  All: () => true,
  Unsure: t => t.unsure > 0,
  Checked: t => t.checked > 0,
  Changed: week,
  Disputed: t => t.disputed > 0
}
for (const [label, test] of Object.entries(FILTERS)) {
  const want = THINGS.filter(test).map(t => t.name)
  ok(`the demo has ${label.toLowerCase()} things`, want.length > 0)
  same(`the ${label} chip counts`, textOf(chip(tree, label)), `${label} ${want.length}`)
  tree = await press(tree, textOf(chip(tree, label)))
  same(`the ${label} filter`, rowNames(tree), want)
}
same('the Changed filter agrees with the summary', THINGS.filter(week).length, o.changed_things)
tree = await press(tree, textOf(chip(tree, 'All')))

// Search is done in the page; it must agree with what the backend's own search answers.
const searchField = () => all(peek(), n => n.type === 'search-field')[0]
for (const query of ['orch', 'machine', 'zzz']) {
  searchField().props.onChange(query)
  tree = await show()
  same(`search "${query}" agrees with the backend`, rowNames(tree), DEMO[`/things?q=${query}`].things.map(t => t.name))
}
has('a search with no match says so', textOf(tree), 'Nothing matches “zzz”.')
searchField().props.onChange('THE SHOP')
tree = await show()
same('search reads other names too, in any case', rowNames(tree), ['Orchard'])
searchField().props.onChange('')
tree = await show()
ok('search never asks the backend', !calls.some(c => c.startsWith('/things?q=')))

// ---------------------------------------------------------------------------
// 4. a thing
// ---------------------------------------------------------------------------

const ORCHARD = DEMO['/thing?name=orchard'].thing
tree = await openRow(tree, 'Orchard')
text = textOf(byClass(tree, 'deltos-main'))
ok('opening a thing asks for it', calls.includes('/thing?name=orchard'))
same('the open row is marked', byClass(tree, 'is-on').filter(n => n.type === 'button' && String(n.props.className).includes('deltos-row')).map(r => textOf(byClass([r], 'deltos-row-name'))), ['Orchard'])
has('thing: name', text, 'Orchard')
has('thing: kind', text, ORCHARD.kind)
has('thing: other names', text, `Also called ${ORCHARD.aliases.join(', ')}`)
has('thing: counts', text, `${ORCHARD.entries} facts`)
same('thing: one row per fact', byClass(tree, 'deltos-entry').length, ORCHARD.items.length)

const version = ORCHARD.items.find(e => e.label === 'Version')
let row = entryOf(tree, 'Version')
text = textOf(row)
has('checked: value', text, version.value)
has('checked: badge with the day it was seen', textOf(all([row], n => n.type === 'badge')), `Seen ${nice(version.seen)}`)
has('checked: since', text, `since ${nice(version.since)}`)
has('checked: the check, as typed', textOf(all([row], n => n.type === 'code')), version.check)
same('checked: the check can be copied', all([row], n => n.type === 'copy-button').map(n => n.props.text), [version.check])
const pastRows = all([row], n => n.type === 'li')
same('past values, newest first', pastRows.map(li => textOf(byClass([li], 'deltos-past-value'))), version.past.map(p => p.value))
const wrong = version.past.findIndex(p => p.wrong)
ok('the demo has a value recorded by mistake', wrong >= 0)
same('only the mistake is struck through', pastRows.map(li => String(li.props.className || '').includes('is-wrong')), version.past.map(p => p.wrong))
has('the mistake says so', textOf(pastRows[wrong]), `recorded by mistake, corrected ${nice(version.past[wrong].end)}`)
const kept = version.past.find(p => !p.wrong && p.start)
has('a past value has its dates', textOf(pastRows[version.past.indexOf(kept)]), `${nice(kept.start)} to ${nice(kept.end)}`)
lacks('a checked fact needs no asking', text, 'Ask the agent to check')

const status = ORCHARD.items.find(e => e.state === 'unsure')
row = entryOf(tree, status.label)
has('unsure: badge', textOf(all([row], n => n.type === 'badge')), status.note.charAt(0).toUpperCase() + status.note.slice(1))
same('unsure: the badge is the amber one', all([row], n => n.type === 'badge')[0].props.variant, 'warn')
has('unsure: offers to ask the agent', textOf(row), 'Ask the agent to check')

for (const [type, tag] of [['rule', 'Rule'], ['decision', 'Decision']]) {
  const entry = ORCHARD.items.find(e => e.type === type)
  ok(`the demo has a ${type}`, Boolean(entry))
  has(`${type}: quiet tag`, textOf(byClass([entryOf(tree, entry.label)], 'deltos-entry-label')), tag)
}
const plain = ORCHARD.items.find(e => e.state === 'plain' && e.type === 'identity')
same('a plain fact carries no badge', all([entryOf(tree, plain.label)], n => n.type === 'badge').length, 0)

// A fact a note disagrees with.
const LOCAL = DEMO['/thing?name=local-model'].thing
const disputed = LOCAL.items.find(e => e.state === 'disputed')
tree = await openRow(tree, LOCAL.name)
row = entryOf(tree, disputed.label)
text = textOf(row)
same('a label made of initials is printed in capitals', [disputed.label, textOf(byClass([row], 'deltos-entry-label'))], ['Url', 'URL'])
has('disputed: badge', textOf(all([row], n => n.type === 'badge')), 'A note disagrees')
same('disputed: the badge is the red one', all([row], n => n.type === 'badge')[0].props.variant, 'destructive')
has('disputed: who said what, and when', text, `A meeting note said ${disputed.other.value} on ${nice(disputed.other.day)}. Nobody has looked since.`)
has('disputed: memory still shows its own value', textOf(byClass([row], 'deltos-value')), disputed.value)

// A retired thing.
const OLD = THINGS.find(t => t.dormant)
tree = await openRow(tree, OLD.name)
text = textOf(byClass(tree, 'deltos-main'))
has('retired: the thing says it is out of use', text, 'No longer in use')
has('retired: badge', textOf(all(byClass(tree, 'deltos-main'), n => n.type === 'badge')), 'Retired')

// The demo store has no fact with the words it was said in, a second value, or
// more than four past values. These three are the real Orchard answer with
// those fields filled in, so the branches that draw them run too.
const rich = clone(DEMO)
const richVersion = rich['/thing?name=orchard'].thing.items.find(e => e.label === 'Version')
richVersion.said = { words: 'we shipped three five this morning', day: version.since }
richVersion.also = [{ value: '3.5.0-hotfix on the staging copy', since: version.since }]
richVersion.past = [...richVersion.past, ...clone(version.past.filter(p => !p.wrong).slice(0, 2))]
scene(rich)
tree = await show()
tree = await openRow(tree, 'Orchard')
row = entryOf(tree, 'Version')
has('receipt: the words', textOf(byClass([row], 'deltos-receipt')), '“we shipped three five this morning”')
has('receipt: the day', textOf(byClass([row], 'deltos-receipt')), `said on ${nice(version.since)}`)
has('a second value is shown', textOf(row), `Also: 3.5.0-hotfix on the staging copy (from ${nice(version.since)})`)
same('long histories start folded', all([row], n => n.type === 'li').length, 4)
tree = await press(tree, `Show ${richVersion.past.length - 4} earlier values`)
same('and unfold', all([entryOf(tree, 'Version')], n => n.type === 'li').length, richVersion.past.length)
tree = await press(tree, 'Show fewer')
same('and fold again', all([entryOf(tree, 'Version')], n => n.type === 'li').length, 4)

// ---------------------------------------------------------------------------
// 5. another day
// ---------------------------------------------------------------------------

scene(DEMO)
tree = await show()
tree = await openRow(tree, 'Orchard')
lacks('today is not marked as the past', textOf(tree), 'You are looking at the past')
const monthAgo = back(30)
tree = await press(tree, 'A month ago')
ok('a step back asks for that day', calls.includes(`/on?name=orchard&day=${monthAgo}`), calls.join(' '))
text = textOf(byClass(tree, 'deltos-main'))
has('the past is clearly marked', text, 'You are looking at the past: memory as it stood on')
has('with the day', text, nice(monthAgo))
has('and a way back', text, 'Back to today')
ok('the time bar changes colour', byClass(tree, 'deltos-time')[0].props.className.includes('is-past'))
const THEN = DEMO[`/on?name=orchard&day=${monthAgo}`].thing
same('the past: one row per fact', byClass(tree, 'deltos-entry').length, THEN.items.length)
for (const item of THEN.items) {
  row = entryOf(tree, item.label)
  const now = ORCHARD.items.find(e => e.label === item.label)
  if (item.state === 'current') {
    has(`then (${item.label}): same value`, textOf(byClass([row], 'deltos-value')), item.value)
    has(`then (${item.label}): same today`, textOf(row), 'Same today')
    lacks(`then (${item.label}): no "today" line`, textOf(row), 'Today:')
  } else if (item.state === 'past') {
    has(`then (${item.label}): the value then`, textOf(byClass([row], 'deltos-value')), item.value)
    has(`then (${item.label}): changed since`, textOf(row), 'Changed since')
    has(`then (${item.label}): the value today`, textOf(row), `Today: ${now.value}`)
    has(`then (${item.label}): the dates it held`, textOf(row), `to ${nice(item.end)}`)
  }
}
ok('the demo month has both kinds of row', THEN.items.some(i => i.state === 'past') && THEN.items.some(i => i.state === 'current'))
lacks('the past offers no asking', text, 'Ask the agent to check')

tree = await press(tree, 'Back to today')
lacks('back to today', textOf(tree), 'You are looking at the past')
same('today again: one row per fact', byClass(tree, 'deltos-entry').length, ORCHARD.items.length)

// The history line: one dot per day something changed.
const changeDays = [...new Set(ORCHARD.items.flatMap(e => [e.since, ...e.past.filter(p => !p.wrong).map(p => p.start)]).filter(Boolean))].sort()
const dots = byClass(tree, 'deltos-dot')
same('one dot per day something changed', dots.length, changeDays.length)
has('the line starts at the first known day', textOf(byClass(tree, 'deltos-track-row')), nice(changeDays[0]))
ok('dots sit along the line in order', dots.every((d, i) => i === 0 || parseFloat(d.props.style.left) >= parseFloat(dots[i - 1].props.style.left)))
const dotDay = changeDays[1]
tree = await press(tree, all(tree, n => String(n.props['aria-label'] || '').startsWith(`${nice(dotDay)}:`))[0].props['aria-label'])
ok('a dot jumps to its day', calls.includes(`/on?name=orchard&day=${dotDay}`))
has('the dot day is shown', textOf(byClass(tree, 'deltos-then')), nice(dotDay))
tree = await press(tree, 'Go to the change before')
ok('the arrow steps to the change before', calls.includes(`/on?name=orchard&day=${changeDays[0]}`))
tree = await press(tree, 'Go to the change after')
has('and forward again', textOf(byClass(tree, 'deltos-then')), nice(dotDay))

const yearAgo = back(365)
tree = await press(tree, 'A year ago')
const EARLY = DEMO[`/on?name=orchard&day=${yearAgo}`].thing
ok('the demo year has nothing known yet', EARLY.items.every(i => i.state === 'before'))
text = textOf(byClass(tree, 'deltos-main'))
has('before anything was known: says so', text, 'Nothing known on this day')
has('before anything was known: badge', text, 'Not known yet')
has('before anything was known: when it first was', text, `first known on ${nice(EARLY.items[0].start)}`)

// Looking at another thing keeps the day (the bar stays marked).
tree = await openRow(tree, LOCAL.name)
ok('the day carries to the next thing', calls.includes(`/on?name=local-model&day=${yearAgo}`))
has('and stays marked', textOf(byClass(tree, 'deltos-main')), 'You are looking at the past')

// The date field: a real day travels, tomorrow or nonsense means today.
const dateField = () => all(peek(), n => n.type === 'input' && n.props.type === 'date')[0]
same('the date field cannot pick the future', dateField().props.max, TODAY)
dateField().props.onChange({ target: { value: back(7) } })
tree = await show()
ok('the date field travels', calls.includes(`/on?name=local-model&day=${back(7)}`))
for (const junk of ['2999-01-01', TODAY, '', 'yesterday']) {
  dateField().props.onChange({ target: { value: back(7) } })
  dateField().props.onChange({ target: { value: junk } })
  tree = await show()
  lacks(`the date field reads ${JSON.stringify(junk)} as today`, textOf(tree), 'You are looking at the past')
}

// A day the backend cannot answer: the page says so and offers another try.
const oddDay = back(11)
ok('no fixture covers the odd day', !(`/on?name=local-model&day=${oddDay}` in DEMO))
world.fail[`/on?name=local-model&day=${oddDay}`] = httpError(500, 'the notes folder could not be read')
dateField().props.onChange({ target: { value: oddDay } })
tree = await show()
text = textOf(byClass(tree, 'deltos-main'))
has('a failed day says what failed', text, 'That day could not be read. the notes folder could not be read')
world.fail = {}
world.routes = { ...DEMO, [`/on?name=local-model&day=${oddDay}`]: DEMO[`/on?name=local-model&day=${back(7)}`] }
tree = await press(tree, 'Try again')
lacks('trying again recovers', textOf(byClass(tree, 'deltos-main')), 'could not be read')
same('after recovering: one row per fact', byClass(tree, 'deltos-entry').length, LOCAL.items.length)

// The backend says it has nothing for that day.
world.routes = { ...DEMO, [`/on?name=local-model&day=${back(12)}`]: { found: false, near: [] } }
dateField().props.onChange({ target: { value: back(12) } })
tree = await show()
has('a day with no reading', textOf(byClass(tree, 'deltos-main')), 'Memory has nothing to show for that day')

// ---------------------------------------------------------------------------
// 6. changes
// ---------------------------------------------------------------------------

scene(DEMO)
tree = await show()
tree = await press(tree, 'Changes')
ok('changes asks for the last week', calls.includes('/changes?days=7'))
let changes = DEMO['/changes?days=7'].changes
same('one row per change', byClass(tree, 'deltos-change').length, changes.length)
same('grouped by day, newest first', all(byClass(tree, 'deltos-main'), n => n.type === 'h3').length, new Set(changes.map(c => c.day)).size)
text = textOf(byClass(tree, 'deltos-main'))
for (const c of changes) {
  has(`change: ${c.thing} · ${c.label}`, text, `${c.thing} · ${shown(c.label)} ${c.value} was ${c.was}`)
  has(`change: its day`, text, nice(c.day))
}
same('the rail stays, with nothing marked open', byClass(tree, 'deltos-row').filter(r => r.props['aria-current']).length, 0)
for (const days of ['30', '90']) {
  tree = await press(tree, `${days} days`)
  ok(`changes asks for ${days} days`, calls.includes(`/changes?days=${days}`))
  same(`${days} days: one row per change`, byClass(tree, 'deltos-change').length, DEMO[`/changes?days=${days}`].changes.length)
}
changes = DEMO['/changes?days=90'].changes
ok('the demo has a brand-new fact in 90 days', changes.some(c => !c.was))
const fresh = changes.find(c => !c.was)
has('a brand-new fact is marked New', textOf(byClass(tree, 'deltos-change').find(r => textOf(r).startsWith(`${fresh.thing} · ${shown(fresh.label)} `))), `${fresh.value} New`)
tree = await press(tree, `Open ${changes[0].thing}`, { within: all(tree, n => n.props.title === `Open ${changes[0].thing}`).map(n => ({ ...n, props: { ...n.props, 'aria-label': n.props.title } })) })
has('a change opens its thing', textOf(byClass(tree, 'deltos-thing-head')), changes[0].thing)

const quiet = clone(DEMO)
quiet['/changes?days=7'] = EMPTY['/changes?days=7']
scene(quiet)
tree = await press(await show(), 'Changes')
has('a quiet week says so', textOf(byClass(tree, 'deltos-main')), 'Nothing changed in the last 7 days')

// ---------------------------------------------------------------------------
// 7. worth a look
// ---------------------------------------------------------------------------

scene(DEMO)
tree = await show()
tree = await press(tree, `Worth a look · ${o.due}`)
ok('worth a look asks for the list', calls.includes('/due?n=50'))
const DUE = DEMO['/due?n=50'].due
let dueRows = byClass(tree, 'deltos-due')
same('one row per fact worth a look', dueRows.length, DUE.length)
DUE.forEach((d, i) => {
  const t = textOf(dueRows[i])
  has(`due ${i}: what`, t, `${d.thing} · ${shown(d.attribute)}`)
  has(`due ${i}: the value memory holds`, textOf(byClass([dueRows[i]], 'deltos-value')), d.value)
  has(`due ${i}: when it was last seen`, t, `Last seen ${nice(d.seen)}`)
  if (d.other) has(`due ${i}: what a note said`, t, `said ${d.other[0]} on ${nice(d.other[2])}`)
})
ok('the demo list starts with the fact a note disagrees with', Boolean(DUE[0].other))
has('ages read as people say them', textOf(dueRows[1]), 'months ago')

// "Ask the agent to check": the open chat first.
const asked = []
const wantText = `Check ${DUE[0].thing} · ${DUE[0].attribute} (memory says ${DUE[0].value}) and remember what you see.`
stub.composer = { insertText: async (to, words, opts) => (asked.push([to, words, opts]), true), focus: to => asked.push(['focus', to]) }
tree = await press(tree, 'Ask the agent to check', { within: [dueRows[0]] })
await tick(20)
same('asking writes into the open chat, and only writes', asked, [[null, wantText, { mode: 'block' }], ['focus', null]])
same('and says so', stub.notes.map(n => n.message), ['Added to your message. Send it when you are ready.'])
same('without opening a new chat', stub.newChats, 0)

// No chat open: a fresh one.
asked.length = 0
stub.notes.length = 0
let refusals = 3
stub.composer = { insertText: async (to, words) => (asked.push(to), to === 'new' && --refusals < 0), focus: () => {} }
await ask(dueRows[0])
await tick(60)
same('with no chat open, a new one is started', stub.newChats, 1)
ok('and the words land there once it is up', asked[0] === null && asked.slice(1).every(to => to === 'new') && asked.length === 5, JSON.stringify(asked))
same('nothing is copied when the chat took it', clipboard.length, 0)

// An app with no composer door: the clipboard.
stub.composer = null
stub.notes.length = 0
await ask(dueRows[0])
await tick(20)
same('without a chat box the words are copied', clipboard, [wantText])
same('and the page says to paste them', stub.notes.map(n => n.message), ['Copied. Paste it into a chat with your agent.'])

// Nothing works: the words are at least shown.
world.clipboardWorks = false
stub.notes.length = 0
await ask(dueRows[0])
await tick(20)
same('if nothing takes them, the words are shown', stub.notes, [{ kind: 'warning', message: `Ask your agent: ${wantText}` }])

// The thing's name opens it.
tree = await press(tree, DUE[0].thing, { within: [dueRows[0]] })
has('a row opens its thing', textOf(byClass(tree, 'deltos-thing-head')), DUE[0].thing)

// No demo row has a known check or a thing that moved under it. These are real
// rows with those two fields filled in, so both lines are drawn.
const moved = clone(DEMO)
moved['/due?n=50'].due[1].how = version.check
moved['/due?n=50'].due[2].moved = ['vps-2', 'Ip', back(3)]
scene(moved)
tree = await press(await show(), `Worth a look · ${o.due}`)
dueRows = byClass(tree, 'deltos-due')
has('due: a known check is shown', textOf(all([dueRows[1]], n => n.type === 'code')), version.check)
has('due: a move is explained', textOf(dueRows[2]), `vps-2 changed ip on ${nice(back(3))}, and this has not been seen since.`)
stub.composer = { insertText: async (to, words) => (asked.push(words), true) }
asked.length = 0
await ask(dueRows[1])
await tick(20)
has('asking passes the known check along', asked[0] || '', `Last time it was checked with: ${version.check}`)

const calm = clone(DEMO)
calm['/due?n=50'] = EMPTY['/due?n=50']
scene(calm)
tree = await press(await show(), `Worth a look · ${o.due}`)
has('nothing worth a look says so', textOf(byClass(tree, 'deltos-main')), 'Nothing needs a look right now')

// The summary tiles are shortcuts.
scene(DEMO)
tree = await show()
tree = await press(tree, `${o.unsure} not confirmed for 3 weeks`)
has('the not-confirmed tile opens worth a look', textOf(byClass(tree, 'deltos-main')), 'Facts nobody has confirmed in a while')
tree = await press(tree, `${o.changed} changed in 7 days`)
has('the changed tile opens changes', textOf(byClass(tree, 'deltos-main')), 'What changed')
tree = await press(tree, `${o.disputed} a note disagrees`)
same('the disagrees tile filters the list', rowNames(tree), THINGS.filter(t => t.disputed > 0).map(t => t.name))
tree = await press(tree, `${o.checked} seen with a check`)
same('the checked tile filters the list', rowNames(tree), THINGS.filter(t => t.checked > 0).map(t => t.name))
tree = await press(tree, `${o.things} things`)
same('the things tile shows everything', rowNames(tree), THINGS.map(t => t.name))

// ---------------------------------------------------------------------------
// 8. an empty store
// ---------------------------------------------------------------------------

scene(EMPTY)
tree = await show()
text = textOf(tree)
has('empty: says so', text, 'Memory is empty')
has('empty: how it fills', text, 'It fills in as your agent learns things while you work together.')
has('empty: how to bring notes in', text, 'deltos import')
has('empty: where memory lives', text, EMPTY['/health'].root)
same('empty: no list, no tabs', byClass(tree, 'deltos-rail').length + all(tree, n => n.type === 'segmented').length, 0)
ok('empty: nothing asked about a thing', !calls.some(c => c.startsWith('/thing?')))

// ---------------------------------------------------------------------------
// 9. the backend cannot open memory
// ---------------------------------------------------------------------------

scene(DOWN)
tree = await show()
text = textOf(tree)
has('down: says so', text, 'Memory is not available')
has('down: the reason the backend gave', text, DOWN['/health'].reason)
same('down: nothing else is asked', calls, ['/health'])
world.routes = DEMO
tree = await press(tree, 'Try again')
same('down: trying again loads the page', rowNames(tree), THINGS.map(t => t.name))

// ---------------------------------------------------------------------------
// 10. the two halves are different versions
// ---------------------------------------------------------------------------

const stale = clone(DEMO)
stale['/health'].version = '0.1.0'
scene(stale)
tree = await show()
text = textOf(tree)
has('older backend: says to reopen the app', text, `Hermes is still running Deltos 0.1.0. Quit and reopen the Hermes app to finish updating to ${VERSION}.`)
same('older backend: the page still works', rowNames(tree), THINGS.map(t => t.name))

const ahead = clone(DEMO)
ahead['/health'].version = '9.0.0'
scene(ahead)
text = textOf(await show())
has('newer backend: says to update the plugin', text, `This page is Deltos ${VERSION}, and Hermes is running Deltos 9.0.0.`)

const unversioned = clone(DEMO)
delete unversioned['/health'].version
scene(unversioned)
has('a backend with no version reads as older', textOf(await show()), 'Hermes is still running an older Deltos.')

const staleDown = clone(DOWN)
staleDown['/health'].version = '0.1.0'
scene(staleDown)
text = textOf(await show())
has('older and down: both are said', text, 'Hermes is still running Deltos 0.1.0.')
has('older and down: the reason too', text, DOWN['/health'].reason)

// ---------------------------------------------------------------------------
// 11. a name that is not there
// ---------------------------------------------------------------------------

// A row that points at a thing since renamed: the real answers for "vps" (two
// things are near) and for a name nothing is near.
const renamed = clone(DEMO)
renamed['/due?n=50'].due[0].stem = 'vps'
renamed['/due?n=50'].due[1].stem = 'nothing-like-this'
scene(renamed)
tree = await press(await show(), `Worth a look · ${o.due}`)
dueRows = byClass(tree, 'deltos-due')
tree = await press(tree, renamed['/due?n=50'].due[0].thing, { within: [dueRows[0]] })
text = textOf(byClass(tree, 'deltos-main'))
has('not found: says so', text, 'Nothing in memory is called “vps”')
const NEAR = DEMO['/thing?name=vps'].near
ok('the demo offers near names', NEAR.length === 2)
for (const name of NEAR) has(`not found: offers ${name}`, text, name)
tree = await press(tree, NEAR[0], { within: byClass(tree, 'deltos-center') })
has('a near name opens that thing', textOf(byClass(tree, 'deltos-thing-head')), NEAR[0])
ok('by its file name', calls.includes(`/thing?name=${THINGS.find(t => t.name === NEAR[0]).slug}`))

tree = await press(tree, `Worth a look · ${o.due}`)
dueRows = byClass(tree, 'deltos-due')
tree = await press(tree, renamed['/due?n=50'].due[1].thing, { within: [dueRows[1]] })
text = textOf(byClass(tree, 'deltos-main'))
has('not found, nothing near: says so', text, 'Nothing in memory is called “nothing-like-this”')
has('not found, nothing near: why', text, 'It may have been renamed or removed.')
tree = await press(tree, 'Back to the list')
has('back to the list opens the first thing', textOf(byClass(tree, 'deltos-thing-head')), THINGS[0].name)

// ---------------------------------------------------------------------------
// 12. requests that fail
// ---------------------------------------------------------------------------

scene(DEMO, { fail: { '/health': httpError(404, 'Not Found') } })
tree = await show()
text = textOf(tree)
has('no backend: says the plugin is not running', text, 'Hermes is not answering for Deltos yet')
has('no backend: what to do', text, 'Run "hermes plugins enable deltos" in a terminal, then quit and reopen Hermes.')
same('no backend: nothing else is asked', calls, ['/health'])
world.fail = {}
tree = await press(tree, 'Try again')
same('no backend: trying again loads the page', rowNames(tree), THINGS.map(t => t.name))

scene(DEMO, { fail: { '/health': new Error('Failed to reach the gateway') } })
text = textOf(await show())
has('unreachable: says so', text, 'Memory could not be reached')
has('unreachable: the error as given', text, 'Failed to reach the gateway')

scene(DEMO, { fail: { '/overview': httpError(500, 'overview broke'), '/things': httpError(500, 'the notes folder could not be read') } })
tree = await show()
text = textOf(tree)
has('a failed summary says what failed', text, 'The summary could not be read. overview broke')
has('a failed list says what failed', text, 'The list could not be read. the notes folder could not be read')
world.fail = {}
tree = await press(tree, 'Try again', { within: byClass(tree, 'deltos-rail') })
same('the list recovers on its own retry', rowNames(tree), THINGS.map(t => t.name))
tree = await press(tree, 'Try again', { within: byClass(tree, 'deltos-stats') })
has('the summary recovers on its own retry', textOf(byClass(tree, 'deltos-stats')), `${o.things} things`)

scene(DEMO, { fail: { '/thing': httpError(500, 'that note is not readable') } })
tree = await show()
has('a failed thing says what failed', textOf(byClass(tree, 'deltos-main')), 'This thing could not be read. that note is not readable')
world.fail = {}
tree = await press(tree, 'Try again', { within: byClass(tree, 'deltos-main') })
has('the thing recovers', textOf(byClass(tree, 'deltos-thing-head')), THINGS[0].name)

scene(DEMO, { fail: { '/thing': wrappedError(500, 'that note is not readable') } })
tree = await show()
has('a wrapped failure reads as its detail', textOf(byClass(tree, 'deltos-main')), 'This thing could not be read. that note is not readable')
lacks('and shows none of the wrapping', textOf(tree), 'remote method')
world.fail = {}

for (const [route, tab, words] of [
  ['/changes', 'Changes', 'The changes could not be read. boom'],
  ['/due', `Worth a look · ${o.due}`, 'The list could not be read. boom']
]) {
  scene(DEMO, { fail: { [route]: httpError(500, 'boom') } })
  tree = await press(await show(), tab)
  has(`a failed ${route} says what failed`, textOf(byClass(tree, 'deltos-main')), words)
  world.fail = {}
  tree = await press(tree, 'Try again', { within: byClass(tree, 'deltos-main') })
  lacks(`${route} recovers`, textOf(byClass(tree, 'deltos-main')), 'could not be read')
}

// What the app's query layer is told: one more try for a hiccup, none for a refusal.
const retry = stub.configs[JSON.stringify(['deltos', 'health'])].retry
same('a refused request is not retried', retry(0, httpError(404, 'x')), false)
same('a failed one gets one more try', [retry(0, httpError(500, 'x')), retry(1, httpError(500, 'x'))], [true, false])
same('so does a network error', retry(0, new Error('socket hang up')), true)
same('a refusal with no status number is not retried', retry(0, new Error('404: {"detail":"Not Found"}')), false)
same('nor a wrapped refusal', retry(0, wrappedError(422, 'x')), false)
same('a wrapped failure gets one more try', [retry(0, wrappedError(500, 'x')), retry(1, wrappedError(500, 'x'))], [true, false])
ok('every request is a read', calls.length > 0)

// ---------------------------------------------------------------------------
// 13. following the conversation, and the palette
// ---------------------------------------------------------------------------

scene(DEMO)
await show()
ok('the page listens for the agent finishing a tool', (listeners['tool.complete'] || []).length === 1)
for (let i = 0; i < 5; i++) listeners['tool.complete'][0]({ payload: {} })
await tick(30)
same('a burst of tool calls re-reads memory once', stub.invalidated, [['deltos']])
calls.length = 0
await show()
ok('and the page asks again', calls.includes('/health') && calls.includes('/things'))

scene(DEMO)
palette.find(c => c.data.id === 'deltos.due').data.run()
same('the palette goes to the page', stub.navigated, ['/deltos'])
has('on the view it named', textOf(byClass(await show(), 'deltos-main')), 'Worth a look')
palette.find(c => c.data.id === 'deltos.open').data.run()
ok('the other command opens the things', byClass(await show(), 'deltos-thing-head').length === 1)

// The refresh button re-reads everything.
stub.invalidated.length = 0
tree = await press(await show(), 'Refresh')
same('refresh re-reads memory', stub.invalidated, [['deltos']])

// ---------------------------------------------------------------------------

console.log(`checks    : ${passed + failures}`)
console.log(`passed    : ${passed}`)
console.log(`failed    : ${failures}`)
if (failures) {
  console.log('\nSMOKE FAILED')
  process.exit(1)
}
console.log('\nSMOKE OK')
