// Mounts the real Memory page (adapters/hermes/deltos/desktop/plugin.js) with
// real React, the stand-in SDK in sdk.mjs, and the backend answered from the
// demo-store fixtures. What to show comes from the address:
//
//   ?fixture=demo|empty|down     which recorded backend answers
//   &theme=light|dark
//   &case=stale,notfound,...     twists on the answers (see CASES)
//   &composer=1                  pretend a chat box is listening
//   &bare=1                      no app sidebar (for narrow widths)
//   &steps=click:Changes|click:30 days|type:orch|day:2026-09-01
//
// A step clicks the button with that label, text or row name (add #2 for the
// second match), types into the search box, or sets the day field. When all
// steps are done the page marks <html data-ready="1">.
import { createElement as h } from 'react'
import { createRoot } from 'react-dom/client'
import * as sdk from '@hermes/plugin-sdk'
import plugin from '../../../adapters/hermes/deltos/desktop/plugin.js'
import demo from '../fixtures/demo.json'
import empty from '../fixtures/empty.json'
import down from '../fixtures/down.json'

const params = new URLSearchParams(location.search)
const cases = (params.get('case') || '').split(',').filter(Boolean)
const routes = structuredClone({ demo, empty, down }[params.get('fixture') || 'demo'] || demo)

const httpError = (status, detail) => Object.assign(new Error(`${status}: ${JSON.stringify({ detail })}`), { statusCode: status })
const never = new Promise(() => {})
const fail = {}
let hang = () => false

const CASES = {
  // Hermes still runs an older backend than the page.
  stale: () => (routes['/health'].version = '0.1.0'),
  // A row that points at a name since changed: the backend's real "not found" answers.
  notfound: () => {
    routes['/due?n=50'].due[0].stem = 'vps'
    routes['/due?n=50'].due[1].stem = 'nothing-like-this'
  },
  // The backend is not mounted at all.
  offline: () => (fail['/health'] = httpError(404, 'Not Found')),
  // One part of the page fails, the rest stays.
  error: () => (fail['/thing'] = fail['/changes'] = httpError(500, 'could not read the store: notes/orchard.md is not readable')),
  listerror: () => (fail['/things'] = httpError(500, 'could not read the store: the notes folder is not readable')),
  // Answers that never arrive: the skeletons.
  loading: () => (hang = route => route !== '/health'),
  starting: () => (hang = () => true)
}
for (const name of cases) CASES[name] && CASES[name]()

if (params.get('theme') === 'dark') document.documentElement.classList.add('dark')
if (params.get('bare')) document.documentElement.classList.add('hx-bare')
if (params.get('composer')) {
  globalThis.__harness.composer = { insertText: async () => true, focus: () => {} }
}

const contributions = []
const ctx = {
  register: c => (contributions.push(c), () => {}),
  registerMany: list => (contributions.push(...list), () => {}),
  onDispose: () => {},
  onEvent: () => () => {},
  setTimeout: (fn, ms) => {
    const timer = setTimeout(fn, ms)

    return () => clearTimeout(timer)
  },
  rest: async path => {
    const request = decodeURIComponent(path)
    const route = request.split('?')[0]
    if (hang(route)) return never
    if (fail[request] || fail[route]) throw fail[request] || fail[route]
    if (!(request in routes)) throw httpError(404, `no fixture for ${request}`)

    return structuredClone(routes[request])
  },
  os: { writeClipboard: async () => true }
}

plugin.register(ctx)

// ── the app around the page: a sidebar with the plugin's own entry ─────────
const nav = contributions.find(c => c.area === sdk.SIDEBAR_NAV_AREA)
const page = contributions.find(c => c.area === sdk.ROUTES_AREA)
const side = document.getElementById('side')
const row = (icon, label, on) => {
  const el = document.createElement('div')
  el.className = `hx-nav${on ? ' is-on' : ''}`
  el.innerHTML = `<i class="codicon codicon-${icon}"></i><span></span>`
  el.lastChild.textContent = label
  side.append(el)
}
row('comment-discussion', 'Chats')
row('extensions', 'Plugins')
if (nav) row(nav.data.codicon, nav.data.label, true)
const note = document.createElement('div')
note.className = 'hx-side-note'
note.textContent = 'Stand-in for the app. Only the page on the right is the plugin.'
side.append(note)

createRoot(document.getElementById('tile')).render(page.render())

// ── steps ──────────────────────────────────────────────────────────────────
const pause = ms => new Promise(resolve => setTimeout(resolve, ms))
async function settle() {
  for (let i = 0; i < 200; i++) {
    await pause(15)
    if (!sdk.__busy()) break
  }
  await pause(30)
}

const tidy = text => text.replace(/\s+/g, ' ').trim()
function matches(text) {
  const nodes = [...document.querySelectorAll('#tile button, #tile a, #tile [role="button"]')]
  const by = [
    n => n.getAttribute('aria-label') === text,
    n => tidy(n.textContent) === text,
    n => [...n.querySelectorAll('.deltos-row-name')].some(name => tidy(name.textContent) === text),
    n => tidy(n.textContent).startsWith(text)
  ]
  for (const test of by) {
    const found = nodes.filter(test)
    if (found.length) return found
  }

  return []
}

function fill(input, value) {
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, value)
  input.dispatchEvent(new Event('input', { bubbles: true }))
  input.dispatchEvent(new Event('change', { bubbles: true }))
}

async function step(raw) {
  const cut = raw.indexOf(':')
  const verb = raw.slice(0, cut)
  let what = raw.slice(cut + 1)
  if (verb === 'click') {
    let nth = 0
    const pick = /#(\d+)$/.exec(what)
    if (pick) {
      nth = Number(pick[1]) - 1
      what = what.slice(0, pick.index)
    }
    const target = matches(what)[nth]
    if (!target) throw new Error(`nothing to click called "${what}"`)
    target.click()
  } else if (verb === 'type') {
    fill(document.querySelector('#tile .deltos-rail input'), what)
  } else if (verb === 'day') {
    fill(document.querySelector('#tile input[type="date"]'), what)
  } else {
    throw new Error(`unknown step "${raw}"`)
  }
  await settle()
}

;(async () => {
  await settle()
  try {
    for (const raw of (params.get('steps') || '').split('|').filter(Boolean)) await step(raw)
  } catch (error) {
    sdk.host.notify({ kind: 'warning', message: `harness: ${error.message}` })
    document.documentElement.dataset.failed = error.message
  }
  document.documentElement.dataset.ready = '1'
})()
