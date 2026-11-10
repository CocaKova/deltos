/**
 * Deltos — desktop half: the Memory page.
 *
 * A read-only window onto what the agent remembers. Every thing it knows, each
 * fact with the date it became true, the values it held before, how the fact
 * was last checked, and the ones nobody has confirmed in a while. Pick a day
 * and the page shows what memory held as true back then.
 *
 * Everything comes from the plugin's own backend (`/api/plugins/deltos/`,
 * dashboard/plugin_api.py) through `ctx.rest`. Nothing here writes to memory:
 * the one action, "Ask the agent to check", only puts a sentence in the chat
 * box for the person to send.
 *
 * Plain ESM, loaded uncompiled: UI is jsx() calls, not JSX syntax. Only
 * `@hermes/plugin-sdk`, `react` and `react/jsx-runtime` resolve here. The SDK
 * is read through a namespace import so a build that lacks one of the optional
 * components still loads the page (it gets a plain stand-in).
 */

import * as sdk from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'deltos'
/** Kept in step with dashboard/plugin_api.py, dashboard/manifest.json and plugin.yaml (a test checks). */
const PLUGIN_VERSION = '0.2.0'
const ROUTE = '/deltos'
/** The summary strip and the "Changed" filter both look one week back. */
const WEEK = 7
const DUE_ROWS = 50
/** Past values shown before "Show earlier". */
const PAST_SHOWN = 4

const { atom, host, useQuery, useValue } = sdk

/** 'thing' | 'changes' | 'due' */
const $view = atom('thing')
/** The picked thing ('' = the first one in the list). */
const $selected = atom('')
/** 'all' | 'unsure' | 'checked' | 'changed' | 'disputed' */
const $filter = atom('all')
const $search = atom('')
/** The day being looked at, YYYY-MM-DD ('' = today). */
const $day = atom('')
/** How far back the Changes view looks: '7' | '30' | '90'. */
const $days = atom('7')
/** Entries whose older values are unfolded, as "thing|label". */
const $open = atom([])

/** Set in register(); the components reach the backend and the clipboard through it. */
let api = null

// ---------------------------------------------------------------------------
// small helpers
// ---------------------------------------------------------------------------

/** jsx for one child, jsxs for several: the page is written without JSX syntax.
 *  A `key` goes to React as its own argument, never inside the props. */
function el(type, props, ...kids) {
  const { key, ...rest } = props || {}

  return kids.length > 1
    ? jsxs(type, { ...rest, children: kids }, key)
    : jsx(type, kids.length ? { ...rest, children: kids[0] } : rest, key)
}

function cls(...names) {
  return names.filter(Boolean).join(' ')
}

/** `children` as a flat list, so a wrapper can place them beside its own. */
function listOf(children) {
  if (children == null) return []

  return Array.isArray(children) ? children : [children]
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
const WEEKDAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/
const DAY_MS = 86400000

function stamp(day) {
  if (!ISO_DAY.test(day || '')) return NaN
  const [y, m, d] = day.split('-').map(Number)

  return Date.UTC(y, m - 1, d)
}

/** `day` moved by `n` days, as YYYY-MM-DD. */
function shift(day, n) {
  const at = stamp(day)

  return Number.isNaN(at) ? '' : new Date(at + n * DAY_MS).toISOString().slice(0, 10)
}

/** Whole days from `a` to `b` (NaN when either is not a date). */
function daysBetween(a, b) {
  return Math.round((stamp(b) - stamp(a)) / DAY_MS)
}

/** 2026-11-05 -> "5 Nov 2026": no month/day order to misread. */
function nice(day) {
  const at = stamp(day)
  if (Number.isNaN(at)) return day || ''
  const d = new Date(at)

  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}`
}

function weekday(day) {
  const at = stamp(day)

  return Number.isNaN(at) ? '' : WEEKDAYS[new Date(at).getUTCDay()]
}

/** An age the way memory itself says it: days, then weeks, then months. */
function span(n) {
  if (n === 1) return '1 day'
  if (n < 14) return `${n} days`
  if (n < 60) return `${Math.floor(n / 7)} weeks`

  return `${Math.floor(n / 30)} months`
}

function ago(n) {
  if (!Number.isFinite(n)) return ''
  if (n <= 0) return 'today'
  if (n === 1) return 'yesterday'

  return `${span(n)} ago`
}

function count(n, one, many) {
  return `${n} ${n === 1 ? one : many || `${one}s`}`
}

function capital(text) {
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : ''
}

// Short words that read wrong with only a first capital ("Url", "Ip").
const INITIALS = new Set(['api', 'cpu', 'dns', 'gpu', 'id', 'ip', 'os', 'ram', 'ssh', 'ssl', 'tls', 'ui', 'url', 'vpn'])

/** A fact's label as the page shows it. Only how it reads: the label itself
 *  (what the agent is asked about, what rows are matched on) stays as stored. */
function shown(label) {
  return String(label || '')
    .split(' ')
    .map(word => (INITIALS.has(word.toLowerCase()) ? word.toUpperCase() : word))
    .join(' ')
}

/** True when version `a` is older than `b` (dotted numbers). */
function older(a, b) {
  const left = String(a).split('.').map(n => parseInt(n, 10) || 0)
  const right = String(b).split('.').map(n => parseInt(n, 10) || 0)
  for (let i = 0; i < Math.max(left.length, right.length); i++) {
    if ((left[i] || 0) !== (right[i] || 0)) return (left[i] || 0) < (right[i] || 0)
  }

  return false
}

// ---------------------------------------------------------------------------
// data
// ---------------------------------------------------------------------------

/** What a failed request said, without the wrapping the app's own plumbing adds
 *  on the way to the page ("Error invoking remote method '...': Error: "). */
function errorMessage(err) {
  return String((err && err.message) || err || '')
    .replace(/^Error invoking remote method '[^']*':\s*/, '')
    .replace(/^(?:Error:\s*)+/, '')
}

/** The HTTP status an error carries, or NaN. The desktop's REST door rejects
 *  with `Error("<status>: <body>")`, sometimes with a numeric `statusCode`; read both. */
function errorStatus(err) {
  if (!err) return NaN
  if (typeof err.statusCode === 'number') return err.statusCode
  if (typeof err.status === 'number') return err.status
  const match = /^(\d{3}):/.exec(errorMessage(err))

  return match ? Number(match[1]) : NaN
}

/** The readable part of a failed request: the backend's `detail` when it sent one. */
function errorText(err) {
  const raw = errorMessage(err).replace(/^\d{3}:\s*/, '')
  try {
    const body = JSON.parse(raw)
    if (body && typeof body.detail === 'string') return body.detail
  } catch {
    /* not JSON: the text is the message */
  }

  return raw || 'The request did not finish.'
}

function get(path) {
  return api ? api.rest(path) : Promise.reject(new Error('The page is still starting.'))
}

/** One read of the backend. A request the server refused (4xx) is not retried;
 *  anything else gets one more try before the page says so. */
function useApi(key, path, options) {
  return useQuery({
    queryKey: [ID, ...key],
    queryFn: () => get(path),
    staleTime: 15000,
    retry: (failures, err) => failures < 1 && !(errorStatus(err) >= 400 && errorStatus(err) < 500),
    ...options
  })
}

function refresh() {
  const client = sdk.queryClient
  if (client && typeof client.invalidateQueries === 'function') client.invalidateQueries({ queryKey: [ID] })
}

function q(value) {
  return encodeURIComponent(value)
}

// ---------------------------------------------------------------------------
// moving around
// ---------------------------------------------------------------------------

function openThing(slug) {
  $selected.set(slug || '')
  $view.set('thing')
}

/** Look at `day`; today or later (or anything that is not a date) means "now". */
function travel(day, today) {
  $day.set(ISO_DAY.test(day || '') && day < today ? day : '')
}

function toggleOpen(key) {
  const open = $open.get()
  $open.set(open.includes(key) ? open.filter(k => k !== key) : [...open, key])
}

function wait(ms) {
  return new Promise(resolve => {
    if (api && typeof api.setTimeout === 'function') api.setTimeout(resolve, ms)
    else setTimeout(resolve, ms)
  })
}

function say(kind, message) {
  if (host && typeof host.notify === 'function') host.notify({ kind, message })
}

function checkRequest(thing, label, value, how) {
  return (
    `Check ${thing} · ${label} (memory says ${value}) and remember what you see.` +
    (how ? ` Last time it was checked with: ${how}` : '')
  )
}

/** Put a sentence in the chat box for the person to send: the open chat if one
 *  is listening, else a fresh chat, else the clipboard. Never sends anything. */
async function askAgent(text) {
  const composer = host && host.composer
  try {
    if (composer && typeof composer.insertText === 'function') {
      if (await composer.insertText(null, text, { mode: 'block' })) {
        if (typeof composer.focus === 'function') composer.focus(null)
        say('info', 'Added to your message. Send it when you are ready.')

        return 'inserted'
      }
      if (typeof host.newChat === 'function') {
        host.newChat()
        for (let tries = 0; tries < 12; tries++) {
          await wait(150)
          if (await composer.insertText('new', text, { mode: 'block' })) {
            if (typeof composer.focus === 'function') composer.focus('new')

            return 'new-chat'
          }
        }
      }
    }
  } catch {
    /* fall through to the clipboard */
  }
  let copied = false
  try {
    copied = Boolean(api && api.os && typeof api.os.writeClipboard === 'function' && (await api.os.writeClipboard(text)))
  } catch {
    copied = false
  }
  say(copied ? 'info' : 'warning', copied ? 'Copied. Paste it into a chat with your agent.' : `Ask your agent: ${text}`)

  return copied ? 'copied' : 'shown'
}

// ---------------------------------------------------------------------------
// the app's components, with plain stand-ins for a build that lacks one
// ---------------------------------------------------------------------------

const Button =
  sdk.Button ||
  (({ variant, size, loading, className, ...rest }) =>
    jsx('button', { type: 'button', className: cls('deltos-plain-button', className), ...rest }))

const Badge =
  sdk.Badge || (({ variant, size, className, ...rest }) => jsx('span', { className: cls('deltos-plain-badge', className), ...rest }))

const Skeleton = sdk.Skeleton || (({ className }) => jsx('div', { className: cls('deltos-plain-skeleton', className) }))

const Codicon =
  sdk.Codicon ||
  (({ name, size }) =>
    jsx('span', { className: `codicon codicon-${name}`, 'aria-hidden': 'true', style: size ? { fontSize: size } : undefined }))

const SegmentedControl =
  sdk.SegmentedControl ||
  (({ options, value, onChange }) =>
    jsx('div', {
      className: 'deltos-plain-segments',
      children: options.map(option =>
        jsx(
          'button',
          {
            type: 'button',
            'aria-pressed': option.id === value,
            onClick: () => onChange(option.id),
            children: option.label
          },
          option.id
        )
      )
    }))

const SearchField =
  sdk.SearchField ||
  (({ placeholder, value, onChange }) =>
    jsx('input', {
      className: 'deltos-plain-input',
      type: 'search',
      placeholder,
      'aria-label': placeholder,
      value,
      onChange: event => onChange(event.target.value)
    }))

const DateInput = sdk.Input || 'input'
const CopyButton = sdk.CopyButton || null

// ---------------------------------------------------------------------------
// styles: the app's theme variables only, so every theme (light or dark) fits
// ---------------------------------------------------------------------------

const GOOD = 'var(--ui-green, #2f9e73)'
const WARN = 'var(--ui-yellow, #c08532)'
const BAD = 'var(--ui-red, #d6456a)'
const MONO = 'var(--font-mono, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace)'

const CSS = `
.deltos-page{container-type:inline-size;display:flex;flex-direction:column;height:100%;min-width:0;min-height:0;overflow:hidden;box-sizing:border-box;padding-top:var(--titlebar-height,0px);color:var(--ui-text-primary);font-size:13px;line-height:1.45}
.deltos-page *{box-sizing:border-box}
.deltos-page :where(h1,h2,h3,p){margin:0}
.deltos-head{display:flex;align-items:center;gap:12px 16px;flex-wrap:wrap;padding:14px 20px 10px}
.deltos-title{flex:1 1 220px;min-width:0}
.deltos-title h1{font-size:17px;font-weight:600;letter-spacing:-0.01em;line-height:1.25}
.deltos-title p{font-size:12px;color:var(--ui-text-tertiary)}
.deltos-head-tools{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.deltos-notice{display:flex;gap:8px;align-items:flex-start;margin:0 20px 10px;padding:8px 12px;border-radius:6px;font-size:12px;color:var(--ui-text-primary);background:color-mix(in srgb,${WARN} 12%,transparent);border:1px solid color-mix(in srgb,${WARN} 35%,transparent)}
.deltos-notice .codicon{color:${WARN};margin-top:1px}

.deltos-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(118px,1fr));gap:8px;padding:0 20px 14px}
.deltos-stat{display:flex;flex-direction:column;gap:2px;min-width:0;padding:9px 12px 10px;border-radius:8px;border:1px solid var(--ui-stroke-tertiary);background:var(--ui-bg-quinary);color:inherit;font:inherit;text-align:left}
button.deltos-stat{cursor:pointer}
button.deltos-stat:hover{background:var(--ui-row-hover-background,var(--ui-bg-quaternary));border-color:var(--ui-stroke-secondary)}
button.deltos-stat:focus-visible{outline:2px solid var(--ui-accent);outline-offset:1px}
.deltos-stat-n{font-size:19px;font-weight:600;line-height:1.2;font-variant-numeric:tabular-nums;letter-spacing:-0.01em}
.deltos-stat-label{display:flex;align-items:baseline;gap:6px;font-size:11.5px;line-height:1.3;color:var(--ui-text-secondary)}
.deltos-stat.is-zero .deltos-stat-n{color:var(--ui-text-tertiary)}
.deltos-stat-skeleton{height:58px;border-radius:8px}

.deltos-pip{flex:none;display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--ui-text-quaternary)}
.deltos-pip.is-good{background:${GOOD}}
.deltos-pip.is-warn{background:${WARN}}
.deltos-pip.is-bad{background:${BAD}}

.deltos-body{flex:1;min-height:0;display:grid;grid-template-columns:272px minmax(0,1fr);border-top:1px solid var(--ui-stroke-tertiary)}
.deltos-rail{display:flex;flex-direction:column;min-height:0;min-width:0;border-right:1px solid var(--ui-stroke-tertiary)}
.deltos-rail-top{display:flex;flex-direction:column;gap:10px;padding:12px 12px 10px}
.deltos-chips{display:flex;flex-wrap:wrap;gap:4px}
.deltos-chip{display:inline-flex;align-items:center;gap:5px;padding:2px 8px;border-radius:999px;border:1px solid var(--ui-stroke-tertiary);background:transparent;color:var(--ui-text-secondary);font:inherit;font-size:11.5px;line-height:18px;cursor:pointer;white-space:nowrap}
.deltos-chip:hover{background:var(--ui-row-hover-background,var(--ui-bg-quaternary));color:var(--ui-text-primary)}
.deltos-chip.is-on{background:var(--ui-text-primary);border-color:var(--ui-text-primary);color:var(--background,var(--ui-bg-chrome,Canvas))}
.deltos-chip:focus-visible{outline:2px solid var(--ui-accent);outline-offset:1px}
.deltos-chip-n{font-variant-numeric:tabular-nums;opacity:.7}
.deltos-list{flex:1;min-height:0;overflow-y:auto;padding:0 6px 12px;scrollbar-gutter:stable}
.deltos-row{display:flex;flex-direction:column;gap:1px;width:100%;min-width:0;padding:7px 10px;border:0;border-radius:6px;background:transparent;color:inherit;font:inherit;text-align:left;cursor:pointer}
.deltos-row:hover{background:var(--ui-row-hover-background,var(--ui-bg-quaternary))}
.deltos-row.is-on{background:var(--ui-row-active-background,var(--ui-bg-tertiary))}
.deltos-row:focus-visible{outline:2px solid var(--ui-accent);outline-offset:-2px}
.deltos-row.is-dim .deltos-row-name,.deltos-row.is-dim .deltos-row-sub{opacity:.55}
.deltos-row-top{display:flex;align-items:center;gap:8px;min-width:0}
.deltos-row-name{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:500}
.deltos-row-marks{display:flex;align-items:center;gap:8px;flex:none;font-size:11px;color:var(--ui-text-secondary);font-variant-numeric:tabular-nums}
.deltos-mark{display:inline-flex;align-items:center;gap:3px}
.deltos-row-sub{font-size:11.5px;color:var(--ui-text-tertiary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.deltos-quiet{padding:18px 12px;font-size:12px;color:var(--ui-text-tertiary);text-align:center}

.deltos-main{min-width:0;min-height:0;overflow-y:auto;scrollbar-gutter:stable}
.deltos-pad{max-width:920px;padding:16px 24px 40px}
.deltos-thing-head{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 10px}
.deltos-thing-head h2{font-size:20px;font-weight:600;letter-spacing:-0.015em;line-height:1.25;overflow-wrap:anywhere}
.deltos-meta{font-size:12px;color:var(--ui-text-tertiary);margin-top:3px}
.deltos-about{margin-top:8px;color:var(--ui-text-secondary)}
.deltos-view-head{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px 16px;margin-bottom:6px}
.deltos-view-head h2{font-size:16px;font-weight:600;letter-spacing:-0.01em}
.deltos-lede{font-size:12px;color:var(--ui-text-tertiary);max-width:64ch}

.deltos-time{margin-top:14px;padding:10px 12px 8px;border-radius:8px;border:1px solid var(--ui-stroke-tertiary);background:var(--ui-bg-quinary)}
.deltos-time.is-past{border-color:color-mix(in srgb,var(--ui-accent) 45%,transparent);background:color-mix(in srgb,var(--ui-accent) 9%,transparent)}
.deltos-time-row{display:flex;flex-wrap:wrap;align-items:center;gap:6px}
.deltos-time-label{display:inline-flex;align-items:center;gap:6px;margin-right:4px;font-size:12px;color:var(--ui-text-secondary);white-space:nowrap}
.deltos-date{margin-left:auto;font:inherit;font-size:12px;color:var(--ui-text-primary);background:transparent;border:1px solid var(--ui-stroke-secondary);border-radius:5px;padding:2px 6px;height:24px;width:auto}
.deltos-track-row{display:flex;align-items:center;gap:8px;margin-top:8px;font-size:11px;color:var(--ui-text-tertiary);white-space:nowrap}
.deltos-track{position:relative;flex:1;min-width:80px;height:22px;margin:0 6px}
.deltos-track::before{content:'';position:absolute;left:-6px;right:-6px;top:50%;height:1px;background:var(--ui-stroke-primary)}
.deltos-dot{position:absolute;top:50%;width:9px;height:9px;margin:-4.5px 0 0 -4.5px;padding:0;border:0;border-radius:50%;background:var(--ui-text-tertiary);cursor:pointer}
.deltos-dot:hover{background:var(--ui-text-primary);transform:scale(1.3)}
.deltos-dot.is-on{background:var(--ui-accent);box-shadow:0 0 0 3px color-mix(in srgb,var(--ui-accent) 28%,transparent)}
.deltos-dot:focus-visible{outline:2px solid var(--ui-accent);outline-offset:2px}
.deltos-here{position:absolute;top:1px;bottom:1px;width:2px;margin-left:-1px;border-radius:1px;background:var(--ui-accent);pointer-events:none}
.deltos-then{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;margin-top:8px;padding-top:8px;border-top:1px solid color-mix(in srgb,var(--ui-accent) 25%,transparent);font-size:12.5px}
.deltos-then-text{flex:1 1 240px;min-width:0}
.deltos-then-text strong{font-weight:600}

.deltos-entries{margin-top:14px}
.deltos-entry{display:grid;grid-template-columns:minmax(92px,148px) minmax(0,1fr);gap:2px 18px;padding:13px 0;border-top:1px solid var(--ui-stroke-tertiary)}
.deltos-entry:first-child{border-top:0}
.deltos-entry-label{display:flex;flex-direction:column;align-items:flex-start;gap:5px;padding-top:2px;font-size:12px;font-weight:500;color:var(--ui-text-secondary);overflow-wrap:anywhere}
.deltos-entry-body{min-width:0;display:flex;flex-direction:column;gap:5px}
.deltos-entry-top{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px}
.deltos-value{flex:1 1 auto;min-width:0;font-size:14.5px;font-weight:600;line-height:1.35;overflow-wrap:anywhere}
.deltos-value.is-none{font-weight:400;font-style:italic;color:var(--ui-text-tertiary)}
.deltos-when{font-size:12px;color:var(--ui-text-tertiary)}
.deltos-state{display:inline-flex;align-items:center;gap:5px}
.deltos-said-other{display:flex;gap:8px;align-items:flex-start;padding:6px 10px;border-radius:6px;font-size:12.5px;background:color-mix(in srgb,${BAD} 9%,transparent);border:1px solid color-mix(in srgb,${BAD} 28%,transparent)}
.deltos-said-other .codicon{color:${BAD};margin-top:2px}
.deltos-said-other b{font-weight:600;overflow-wrap:anywhere}
.deltos-check{display:flex;align-items:center;gap:8px;min-width:0;font-size:12px;color:var(--ui-text-tertiary)}
.deltos-check code{min-width:0;padding:2px 7px;border-radius:4px;background:var(--ui-bg-quaternary,var(--ui-bg-quinary));color:var(--ui-text-primary);font-family:${MONO};font-size:11.5px;overflow-wrap:anywhere}
.deltos-receipt{padding:1px 0 1px 10px;border-left:2px solid var(--ui-stroke-primary);font-size:12.5px;color:var(--ui-text-secondary)}
.deltos-receipt span{color:var(--ui-text-tertiary);font-size:12px}
.deltos-also{font-size:12.5px;color:var(--ui-text-secondary)}
.deltos-past{list-style:none;margin:3px 0 0;padding:0}
.deltos-past li{position:relative;display:flex;flex-wrap:wrap;align-items:baseline;gap:2px 10px;padding:2px 0 2px 18px;font-size:12.5px}
.deltos-past li::before{content:'';position:absolute;left:3px;top:.62em;width:7px;height:7px;border-radius:50%;background:var(--ui-stroke-primary)}
.deltos-past li::after{content:'';position:absolute;left:6px;top:calc(.62em + 9px);bottom:calc(-.62em + 2px);width:1px;background:var(--ui-stroke-secondary)}
.deltos-past li:last-child::after{display:none}
.deltos-past-value{color:var(--ui-text-secondary);font-weight:500;overflow-wrap:anywhere}
.deltos-past-when{color:var(--ui-text-tertiary);font-size:12px}
.deltos-past li.is-wrong .deltos-past-value{text-decoration:line-through;color:var(--ui-text-tertiary);font-weight:400}
.deltos-past li.is-wrong::before{background:transparent;box-shadow:inset 0 0 0 1.5px var(--ui-stroke-primary)}
.deltos-more{align-self:flex-start;margin-left:14px}

.deltos-day{display:flex;align-items:baseline;gap:8px;margin:18px 0 4px;font-size:12px;font-weight:600;color:var(--ui-text-secondary)}
.deltos-day span{font-weight:400;color:var(--ui-text-tertiary)}
.deltos-change{display:grid;grid-template-columns:minmax(120px,230px) minmax(0,1fr);gap:2px 18px;align-items:baseline;width:100%;padding:8px 10px;margin:0 -10px;border:0;border-radius:6px;background:transparent;color:inherit;font:inherit;text-align:left;cursor:pointer}
.deltos-change:hover{background:var(--ui-row-hover-background,var(--ui-bg-quaternary))}
.deltos-change:focus-visible{outline:2px solid var(--ui-accent);outline-offset:-2px}
.deltos-change-what{min-width:0;color:var(--ui-text-secondary);overflow-wrap:anywhere}
.deltos-change-what b{font-weight:600;color:var(--ui-text-primary)}
.deltos-change-to{display:flex;flex-wrap:wrap;align-items:baseline;gap:2px 10px;min-width:0}
.deltos-change-to strong{font-weight:600;overflow-wrap:anywhere}
.deltos-was{color:var(--ui-text-tertiary);font-size:12px;overflow-wrap:anywhere}
.deltos-was s{text-decoration-color:color-mix(in srgb,currentColor 55%,transparent)}

.deltos-due{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:flex-start;padding:13px 0;border-top:1px solid var(--ui-stroke-tertiary)}
.deltos-due-body{flex:1 1 280px;min-width:0;display:flex;flex-direction:column;gap:4px}
.deltos-due-what{font-size:12px;color:var(--ui-text-secondary)}
.deltos-link{padding:0;border:0;background:none;color:inherit;font:inherit;font-weight:600;cursor:pointer;text-align:left}
.deltos-link:hover{text-decoration:underline}
.deltos-link:focus-visible{outline:2px solid var(--ui-accent);outline-offset:2px}
.deltos-due-tools{display:flex;align-items:center;gap:6px;flex:none}

.deltos-center{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;min-height:240px;padding:32px 24px;text-align:center}
.deltos-center h2{font-size:15px;font-weight:600}
.deltos-center p{max-width:52ch;font-size:12.5px;color:var(--ui-text-secondary)}
.deltos-center code{font-family:${MONO};font-size:12px;padding:1px 5px;border-radius:4px;background:var(--ui-bg-quaternary,var(--ui-bg-quinary));color:var(--ui-text-primary)}
.deltos-center>.codicon{color:var(--ui-text-tertiary)}
.deltos-detail{max-width:64ch;padding:8px 12px;border-radius:6px;background:var(--ui-bg-quinary);font-family:${MONO};font-size:11.5px;color:var(--ui-text-secondary);overflow-wrap:anywhere;text-align:left}
.deltos-buttons{display:flex;flex-wrap:wrap;justify-content:center;gap:6px}
.deltos-oops{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;margin:10px 0;padding:8px 12px;border-radius:6px;font-size:12.5px;background:color-mix(in srgb,${BAD} 9%,transparent);border:1px solid color-mix(in srgb,${BAD} 28%,transparent)}
.deltos-oops span{flex:1 1 200px;min-width:0;overflow-wrap:anywhere}
.deltos-sk-line{height:12px;border-radius:4px;margin:8px 0}
.deltos-sk-row{height:44px;border-radius:6px;margin:6px 0}
.deltos-sk-block{height:66px;border-radius:8px;margin:12px 0}

.deltos-plain-button{font:inherit;font-size:12px;padding:3px 10px;border-radius:5px;border:1px solid var(--ui-stroke-secondary);background:transparent;color:inherit;cursor:pointer}
.deltos-plain-badge{display:inline-flex;align-items:center;gap:4px;padding:1px 6px;border-radius:3px;font-size:11px;background:var(--ui-bg-quaternary);color:var(--ui-text-secondary)}
.deltos-plain-skeleton{background:var(--ui-bg-quaternary)}
.deltos-plain-segments{display:inline-flex;gap:2px;padding:2px;border-radius:5px;background:var(--ui-bg-tertiary)}
.deltos-plain-segments button{font:inherit;font-size:11px;padding:2px 10px;border:0;border-radius:3px;background:transparent;color:var(--ui-text-secondary);cursor:pointer}
.deltos-plain-segments button[aria-pressed="true"]{background:var(--ui-bg-quinary);color:var(--ui-text-primary)}
.deltos-plain-input{width:100%;font:inherit;font-size:12px;padding:4px 8px;border-radius:5px;border:1px solid var(--ui-stroke-secondary);background:transparent;color:inherit}

@container (max-width: 760px){
  .deltos-body{grid-template-columns:minmax(0,1fr);grid-template-rows:auto minmax(0,1fr)}
  .deltos-rail{border-right:0;border-bottom:1px solid var(--ui-stroke-tertiary)}
  .deltos-list{flex:none;max-height:168px}
  .deltos-pad{padding:14px 16px 32px}
  .deltos-head{padding:12px 16px 8px}
  .deltos-stats{padding:0 16px 12px}
  .deltos-notice{margin:0 16px 10px}
}
@container (max-width: 520px){
  .deltos-entry{grid-template-columns:minmax(0,1fr)}
  .deltos-entry-label{flex-direction:row;align-items:center}
  .deltos-change{grid-template-columns:minmax(0,1fr)}
  .deltos-date{margin-left:0}
}
`

// ---------------------------------------------------------------------------
// shared pieces
// ---------------------------------------------------------------------------

function Pip({ tone }) {
  return jsx('span', { className: cls('deltos-pip', tone && `is-${tone}`), 'aria-hidden': 'true' })
}

function Lines({ rows = 3, kind = 'row' }) {
  return jsx('div', {
    'aria-busy': 'true',
    'aria-label': 'Loading',
    children: Array.from({ length: rows }, (_, i) => jsx(Skeleton, { className: `deltos-sk-${kind}` }, i))
  })
}

/** A request that failed, with a way to try again. */
function Oops({ what, error, retry }) {
  return el(
    'div',
    { className: 'deltos-oops', role: 'alert' },
    jsx('span', { children: `${what} ${errorText(error)}` }),
    jsx(Button, { variant: 'outline', size: 'xs', onClick: () => retry(), children: 'Try again' })
  )
}

function Center({ icon, title, children }) {
  return el(
    'div',
    { className: 'deltos-center' },
    icon ? jsx(Codicon, { name: icon, size: 24 }) : null,
    jsx('h2', { children: title }),
    ...listOf(children)
  )
}

const VIEWS = [
  { id: 'thing', label: 'Things' },
  { id: 'changes', label: 'Changes' },
  { id: 'due', label: 'Worth a look' }
]

function Shell({ tools, children }) {
  return el(
    'div',
    { className: 'deltos-page' },
    el(
      'header',
      { className: 'deltos-head' },
      el(
        'div',
        { className: 'deltos-title' },
        jsx('h1', { children: 'Memory' }),
        jsx('p', { children: 'What your agent holds as true, since when, and how sure it is.' })
      ),
      tools ? jsx('div', { className: 'deltos-head-tools', children: tools }) : null
    ),
    ...listOf(children)
  )
}

function RefreshButton() {
  return jsx(Button, {
    variant: 'ghost',
    size: 'icon-xs',
    'aria-label': 'Refresh',
    title: 'Refresh',
    onClick: refresh,
    children: jsx(Codicon, { name: 'refresh', size: 14 })
  })
}

// ---------------------------------------------------------------------------
// summary strip
// ---------------------------------------------------------------------------

function Stat({ n, label, tone, onClick, title }) {
  const inner = [
    jsx('span', { className: 'deltos-stat-n', children: n }, 'n'),
    el('span', { className: 'deltos-stat-label', key: 'label' }, tone ? jsx(Pip, { tone }) : null, jsx('span', { children: label }))
  ]
  const className = cls('deltos-stat', (n === 0 || n === '0') && 'is-zero')

  return onClick
    ? jsx('button', { type: 'button', className, onClick, title, children: inner })
    : jsx('div', { className, title, children: inner })
}

function Summary({ query }) {
  if (query.data === undefined) {
    if (query.isError) {
      return jsx('div', {
        className: 'deltos-stats',
        children: jsx(Oops, { what: 'The summary could not be read.', error: query.error, retry: query.refetch })
      })
    }

    return jsx('div', {
      className: 'deltos-stats',
      'aria-busy': 'true',
      children: Array.from({ length: 7 }, (_, i) => jsx(Skeleton, { className: 'deltos-stat-skeleton' }, i))
    })
  }
  const o = query.data
  const outcomes = (o.looks && o.looks.outcomes) || {}
  const right = outcomes.same || 0
  const looked = right + (outcomes.changed || 0)
  const filterBy = id => () => {
    $filter.set(id)
    $view.set('thing')
  }

  return el(
    'div',
    { className: 'deltos-stats' },
    jsx(Stat, { n: o.things, label: o.things === 1 ? 'thing' : 'things', onClick: filterBy('all'), title: 'Show every thing' }),
    jsx(Stat, { n: o.entries, label: o.entries === 1 ? 'fact' : 'facts', title: 'Each fact has a current value and the day it became true' }),
    jsx(Stat, { n: o.past, label: o.past === 1 ? 'past value kept' : 'past values kept', title: 'Older values stay, with the dates they held' }),
    jsx(Stat, {
      n: o.checked,
      label: 'seen with a check',
      tone: 'good',
      onClick: filterBy('checked'),
      title: 'Someone looked, and memory knows how. Show these things'
    }),
    jsx(Stat, {
      n: o.unsure,
      label: o.doubt_days ? `not confirmed for ${span(o.doubt_days)}` : 'not confirmed lately',
      tone: 'warn',
      onClick: () => $view.set('due'),
      title: 'Open the list of facts worth a look'
    }),
    jsx(Stat, {
      n: o.disputed,
      label: o.disputed === 1 ? 'a note disagrees' : 'notes disagree',
      tone: 'bad',
      onClick: filterBy('disputed'),
      title: 'A note said something else since anyone looked. Show these things'
    }),
    jsx(Stat, {
      n: o.changed,
      label: `changed in ${o.days || WEEK} days`,
      onClick: () => {
        $days.set(String(WEEK))
        $view.set('changes')
      },
      title: 'Open the list of changes'
    }),
    looked
      ? jsx(Stat, {
          n: `${right} of ${looked}`,
          label: looked === 1 ? 'check matched memory' : 'checks matched memory',
          title: `Looks in the last ${o.looks.days} days where memory already had a value`
        })
      : null
  )
}

// ---------------------------------------------------------------------------
// left rail: search, filters, the things
// ---------------------------------------------------------------------------

function matches(row, text) {
  const needle = text.trim().toLowerCase()
  if (!needle) return true

  return [row.name, row.kind, ...(row.aliases || [])].some(part => String(part || '').toLowerCase().includes(needle))
}

function changedLately(row, today) {
  const days = daysBetween(row.changed, today)

  return Number.isFinite(days) && days >= 0 && days <= WEEK
}

const FILTERS = [
  { id: 'all', label: 'All', test: () => true },
  { id: 'unsure', label: 'Unsure', tone: 'warn', test: row => row.unsure > 0 },
  { id: 'checked', label: 'Checked', tone: 'good', test: row => row.checked > 0 },
  { id: 'changed', label: 'Changed', test: (row, today) => changedLately(row, today) },
  { id: 'disputed', label: 'Disputed', tone: 'bad', test: row => row.disputed > 0, hideEmpty: true }
]

function ThingRow({ row, active, today }) {
  const sub = [
    row.kind,
    count(row.entries, 'fact'),
    row.dormant ? 'no longer in use' : row.changed ? `changed ${ago(daysBetween(row.changed, today))}` : ''
  ]
  const mark = (n, tone, title) =>
    n > 0 ? el('span', { className: 'deltos-mark', title, key: tone }, jsx(Pip, { tone }), String(n)) : null

  return el(
    'button',
    {
      type: 'button',
      className: cls('deltos-row', active && 'is-on', row.dormant && 'is-dim'),
      'aria-current': active ? 'true' : undefined,
      onClick: () => openThing(row.slug)
    },
    el(
      'span',
      { className: 'deltos-row-top' },
      jsx('span', { className: 'deltos-row-name', children: row.name }),
      jsx('span', {
        className: 'deltos-row-marks',
        children: [
          mark(row.disputed, 'bad', `${count(row.disputed, 'fact')} a note disagrees with`),
          mark(row.unsure, 'warn', `${count(row.unsure, 'fact')} not confirmed lately`),
          mark(row.checked, 'good', `${count(row.checked, 'fact')} seen with a check`)
        ]
      })
    ),
    jsx('span', { className: 'deltos-row-sub', children: sub.filter(Boolean).join(' · ') })
  )
}

function Rail({ query, today, current }) {
  const search = useValue($search)
  const filter = useValue($filter)
  const rows = (query.data && query.data.things) || []
  const active = FILTERS.find(f => f.id === filter) || FILTERS[0]
  const shown = rows.filter(row => matches(row, search) && active.test(row, today))

  let list
  if (query.data === undefined) {
    list = query.isError
      ? jsx(Oops, { what: 'The list could not be read.', error: query.error, retry: query.refetch })
      : jsx(Lines, { rows: 6 })
  } else if (!shown.length) {
    list = jsx('div', {
      className: 'deltos-quiet',
      children: search.trim() ? `Nothing matches “${search.trim()}”.` : 'Nothing here right now.'
    })
  } else {
    list = shown.map(row => jsx(ThingRow, { row, today, active: row.slug === current }, row.slug))
  }

  return el(
    'aside',
    { className: 'deltos-rail', 'aria-label': 'Things in memory' },
    el(
      'div',
      { className: 'deltos-rail-top' },
      jsx(SearchField, {
        placeholder: 'Find a thing',
        'aria-label': 'Find a thing by name, other name or kind',
        value: search,
        onChange: value => $search.set(value),
        variant: 'box'
      }),
      jsx('div', {
        className: 'deltos-chips',
        role: 'group',
        'aria-label': 'Filter',
        children: FILTERS.map(f => {
          const n = rows.filter(row => f.test(row, today)).length
          if (f.hideEmpty && !n && filter !== f.id) return null

          return el(
            'button',
            {
              type: 'button',
              key: f.id,
              className: cls('deltos-chip', filter === f.id && 'is-on'),
              'aria-pressed': filter === f.id,
              onClick: () => $filter.set(f.id)
            },
            f.tone ? jsx(Pip, { tone: f.tone }) : null,
            f.label,
            query.data === undefined ? null : jsx('span', { className: 'deltos-chip-n', children: n })
          )
        })
      })
    ),
    jsx('div', { className: 'deltos-list', children: list })
  )
}

// ---------------------------------------------------------------------------
// thing view: today
// ---------------------------------------------------------------------------

const TYPE_TAGS = { rule: 'Rule', decision: 'Decision' }

function StateBadge({ entry }) {
  if (entry.state === 'checked') {
    return el(
      Badge,
      { variant: 'success', title: 'Someone looked, and it was true on that day' },
      jsx(Codicon, { name: 'check', size: 11 }),
      `Seen ${nice(entry.seen)}`
    )
  }
  if (entry.state === 'unsure') {
    return el(
      Badge,
      { variant: 'warn', title: 'This kind of fact drifts, and nobody has looked in a while' },
      jsx(Codicon, { name: 'question', size: 11 }),
      capital(entry.note) || 'Not confirmed lately'
    )
  }
  if (entry.state === 'disputed') {
    return el(
      Badge,
      { variant: 'destructive', title: 'A note said something else since anyone looked' },
      jsx(Codicon, { name: 'warning', size: 11 }),
      'A note disagrees'
    )
  }
  if (entry.state === 'retired') return jsx(Badge, { variant: 'muted', children: 'Retired' })

  return null
}

function PastList({ id, past }) {
  const open = useValue($open).includes(id)
  if (!past || !past.length) return null
  const shown = open ? past : past.slice(0, PAST_SHOWN)
  const hidden = past.length - PAST_SHOWN

  return el(
    'div',
    null,
    jsx('ul', {
      className: 'deltos-past',
      'aria-label': 'Earlier values, newest first',
      children: shown.map((p, i) =>
        p.wrong
          ? el(
              'li',
              { className: 'is-wrong', key: i },
              jsx('span', { className: 'deltos-past-value', children: p.value }),
              jsx('span', {
                className: 'deltos-past-when',
                children: `recorded by mistake${p.end ? `, corrected ${nice(p.end)}` : ''}`
              })
            )
          : el(
              'li',
              { key: i },
              jsx('span', { className: 'deltos-past-value', children: p.value }),
              jsx('span', {
                className: 'deltos-past-when',
                children: p.start ? `${nice(p.start)} to ${nice(p.end)}` : `until ${nice(p.end)}`
              })
            )
      )
    }),
    hidden > 0
      ? jsx(Button, {
          variant: 'text',
          size: 'xs',
          className: 'deltos-more',
          onClick: () => toggleOpen(id),
          children: open ? 'Show fewer' : `Show ${count(hidden, 'earlier value')}`
        })
      : null
  )
}

function CheckLine({ how }) {
  if (!how) return null

  return el(
    'div',
    { className: 'deltos-check' },
    jsx('span', { children: 'Check' }),
    jsx('code', { children: how }),
    CopyButton ? jsx(CopyButton, { text: how, appearance: 'icon', label: 'Copy the check' }) : null
  )
}

function SaidOtherwise({ value, by, day }) {
  return el(
    'div',
    { className: 'deltos-said-other' },
    jsx(Codicon, { name: 'warning', size: 13 }),
    el(
      'span',
      null,
      `${capital(by) || 'A note'} said `,
      jsx('b', { children: value }),
      `${day ? ` on ${nice(day)}` : ''}. Nobody has looked since.`
    )
  )
}

function Entry({ thing, entry, today }) {
  const when = []
  if (entry.since) when.push(`since ${nice(entry.since)}`)
  if (entry.confirmed && entry.confirmed !== entry.since) when.push(`confirmed ${nice(entry.confirmed)}`)
  if (!entry.since && !entry.confirmed) when.push('no date recorded')
  else if (Number.isFinite(entry.age)) when.push(`last seen ${ago(entry.age)}`)
  const askable = entry.state === 'unsure' || entry.state === 'disputed'
  const tag = TYPE_TAGS[entry.type]

  return el(
    'div',
    { className: 'deltos-entry' },
    el(
      'div',
      { className: 'deltos-entry-label' },
      jsx('span', { children: shown(entry.label) }),
      tag ? jsx(Badge, { variant: 'outline', size: 'xs', children: tag }) : null
    ),
    el(
      'div',
      { className: 'deltos-entry-body' },
      el(
        'div',
        { className: 'deltos-entry-top' },
        jsx('span', { className: 'deltos-value', children: entry.value }),
        jsx(StateBadge, { entry })
      ),
      jsx('div', { className: 'deltos-when', children: when.join(' · ') }),
      entry.other ? jsx(SaidOtherwise, entry.other) : null,
      (entry.also || []).map((x, i) =>
        jsx('div', { className: 'deltos-also', children: `Also: ${x.value}${x.since ? ` (from ${nice(x.since)})` : ''}` }, i)
      ),
      jsx(CheckLine, { how: entry.check }),
      entry.said
        ? el(
            'div',
            { className: 'deltos-receipt' },
            `“${entry.said.words}” `,
            jsx('span', { children: entry.said.day ? `said on ${nice(entry.said.day)}` : 'as it was said' })
          )
        : null,
      jsx(PastList, { id: `${thing.slug}|${entry.label}`, past: entry.past }),
      askable
        ? jsx('div', {
            children: jsx(Button, {
              variant: 'outline',
              size: 'xs',
              onClick: () => askAgent(checkRequest(thing.name, entry.label, entry.value, entry.check)),
              children: 'Ask the agent to check'
            })
          })
        : null
    )
  )
}

// ---------------------------------------------------------------------------
// thing view: another day
// ---------------------------------------------------------------------------

const STEPS = [
  { days: 7, label: 'A week ago' },
  { days: 30, label: 'A month ago' },
  { days: 90, label: '3 months ago' },
  { days: 365, label: 'A year ago' }
]

/** Every day something about this thing changed, oldest first, with what changed. */
function changeDays(thing, today) {
  const days = new Map()
  const add = (day, label) => {
    if (!ISO_DAY.test(day || '') || day > today) return
    if (!days.has(day)) days.set(day, [])
    if (!days.get(day).includes(label)) days.get(day).push(label)
  }
  for (const entry of thing.items || []) {
    add(entry.since, entry.label)
    for (const p of entry.past || []) if (!p.wrong) add(p.start, entry.label)
  }

  return [...days.entries()].sort((a, b) => (a[0] < b[0] ? -1 : 1)).map(([day, labels]) => ({ day, labels }))
}

function TimeBar({ thing, today, day }) {
  const marks = thing ? changeDays(thing, today) : []
  const first = marks.length ? marks[0].day : ''
  const length = first ? daysBetween(first, today) : 0
  const at = day || today
  const place = d => `${Math.min(100, Math.max(0, (daysBetween(first, d) / length) * 100)).toFixed(2)}%`
  const earlier = [...marks].reverse().find(m => m.day < at)
  const later = marks.find(m => m.day > at)

  return el(
    'section',
    { className: cls('deltos-time', day && 'is-past'), 'aria-label': 'Look at another day' },
    el(
      'div',
      { className: 'deltos-time-row' },
      el('span', { className: 'deltos-time-label' }, jsx(Codicon, { name: 'history', size: 13 }), 'Memory as of'),
      jsx(Button, {
        variant: day ? 'ghost' : 'secondary',
        size: 'xs',
        'aria-pressed': !day,
        onClick: () => travel('', today),
        children: 'Today'
      }),
      STEPS.map(step => {
        const target = shift(today, -step.days)

        return jsx(
          Button,
          {
            variant: day === target ? 'secondary' : 'ghost',
            size: 'xs',
            'aria-pressed': day === target,
            onClick: () => travel(target, today),
            children: step.label
          },
          step.days
        )
      }),
      jsx(DateInput, {
        type: 'date',
        className: 'deltos-date',
        'aria-label': 'Pick a day',
        value: at,
        max: today,
        onChange: event => travel(event.target.value, today)
      })
    ),
    length > 0
      ? el(
          'div',
          { className: 'deltos-track-row' },
          jsx(Button, {
            variant: 'ghost',
            size: 'icon-xs',
            disabled: !earlier,
            'aria-label': 'Go to the change before',
            title: 'The change before',
            onClick: () => earlier && travel(earlier.day, today),
            children: jsx(Codicon, { name: 'chevron-left', size: 14 })
          }),
          jsx('span', { children: nice(first) }),
          el(
            'div',
            { className: 'deltos-track' },
            marks.map(m =>
              jsx(
                'button',
                {
                  type: 'button',
                  className: cls('deltos-dot', m.day === at && 'is-on'),
                  style: { left: place(m.day) },
                  title: `${nice(m.day)}: ${m.labels.map(shown).join(', ')}`,
                  'aria-label': `${nice(m.day)}: ${m.labels.map(shown).join(', ')} changed`,
                  onClick: () => travel(m.day, today)
                },
                m.day
              )
            ),
            day && day >= first ? jsx('span', { className: 'deltos-here', style: { left: place(day) } }) : null
          ),
          jsx('span', { children: 'Today' }),
          jsx(Button, {
            variant: 'ghost',
            size: 'icon-xs',
            disabled: !day,
            'aria-label': 'Go to the change after',
            title: 'The change after',
            onClick: () => travel(later ? later.day : '', today),
            children: jsx(Codicon, { name: 'chevron-right', size: 14 })
          })
        )
      : null,
    day
      ? el(
          'div',
          { className: 'deltos-then', role: 'status' },
          el(
            'span',
            { className: 'deltos-then-text' },
            'You are looking at the past: memory as it stood on ',
            jsx('strong', { children: `${weekday(day)} ${nice(day)}` }),
            ` (${ago(daysBetween(day, today))}).`
          ),
          jsx(Button, { variant: 'default', size: 'xs', onClick: () => travel('', today), children: 'Back to today' })
        )
      : null
  )
}

function ThenEntry({ item, now }) {
  const known = item.state === 'current' || item.state === 'past'
  let when
  let badge
  if (item.state === 'current') {
    when = item.start ? `since ${nice(item.start)}` : 'no date recorded'
    badge = jsx(Badge, { variant: 'muted', children: 'Same today' })
  } else if (item.state === 'past') {
    when = item.start ? `${nice(item.start)} to ${nice(item.end)}` : `until ${nice(item.end)}`
    badge = jsx(Badge, { variant: 'default', children: 'Changed since' })
  } else if (item.state === 'before') {
    when = item.start ? `first known on ${nice(item.start)}` : ''
    badge = jsx(Badge, { variant: 'outline', children: 'Not known yet' })
  } else {
    when = 'a gap between known values'
    badge = jsx(Badge, { variant: 'outline', children: 'Not known' })
  }

  return el(
    'div',
    { className: 'deltos-entry' },
    jsx('div', { className: 'deltos-entry-label', children: jsx('span', { children: shown(item.label) }) }),
    el(
      'div',
      { className: 'deltos-entry-body' },
      el(
        'div',
        { className: 'deltos-entry-top' },
        jsx('span', {
          className: cls('deltos-value', !known && 'is-none'),
          children: known ? item.value : 'Nothing known on this day'
        }),
        badge
      ),
      jsx('div', { className: 'deltos-when', children: when }),
      (item.also || []).map((x, i) => jsx('div', { className: 'deltos-also', children: `Also: ${x.value}` }, i)),
      item.state !== 'current' && now
        ? el('div', { className: 'deltos-was' }, 'Today: ', jsx('b', { children: now.value }))
        : null
    )
  )
}

function ThenList({ slug, day, today, thing }) {
  const query = useApi(['on', slug, day], `/on?name=${q(slug)}&day=${q(day)}`)
  if (query.data === undefined) {
    return query.isError
      ? jsx(Oops, { what: 'That day could not be read.', error: query.error, retry: query.refetch })
      : jsx(Lines, { rows: 4 })
  }
  if (!query.data.found) {
    return el(
      Center,
      { icon: 'history', title: 'Memory has nothing to show for that day' },
      jsx('p', { children: `Nothing could be read for ${nice(day)}.` }),
      jsx(Button, { variant: 'outline', size: 'sm', onClick: () => travel('', today), children: 'Back to today' })
    )
  }
  const now = new Map(((thing && thing.items) || []).map(entry => [entry.label, entry]))

  return jsx('div', {
    className: 'deltos-entries',
    children: query.data.thing.items.map(item => jsx(ThenEntry, { item, now: now.get(item.label) }, item.label))
  })
}

// ---------------------------------------------------------------------------
// thing view
// ---------------------------------------------------------------------------

function NotFound({ name, near, rows }) {
  const slugOf = label => (rows.find(row => row.name === label) || {}).slug || label

  return el(
    Center,
    { icon: 'search', title: `Nothing in memory is called “${name}”` },
    jsx('p', {
      children: near.length ? 'It may have been renamed. Did you mean one of these?' : 'It may have been renamed or removed.'
    }),
    el(
      'div',
      { className: 'deltos-buttons' },
      near.map(label => jsx(Button, { variant: 'outline', size: 'sm', onClick: () => openThing(slugOf(label)), children: label }, label)),
      jsx(Button, { variant: 'ghost', size: 'sm', onClick: () => openThing(''), children: 'Back to the list' })
    )
  )
}

function ThingView({ slug, today, rows }) {
  const day = useValue($day)
  const query = useApi(['thing', slug], `/thing?name=${q(slug)}`)
  if (query.data === undefined) {
    return jsx('div', {
      className: 'deltos-pad',
      children: query.isError
        ? jsx(Oops, { what: 'This thing could not be read.', error: query.error, retry: query.refetch })
        : [jsx(Skeleton, { className: 'deltos-sk-line' }, 'a'), jsx(Skeleton, { className: 'deltos-sk-block' }, 'b'), jsx(Lines, { rows: 5 }, 'c')]
    })
  }
  if (!query.data.found) {
    return jsx('div', { className: 'deltos-pad', children: jsx(NotFound, { name: slug, near: query.data.near || [], rows }) })
  }
  const thing = query.data.thing
  const meta = [
    count(thing.entries, 'fact'),
    thing.past ? count(thing.past, 'past value kept', 'past values kept') : '',
    thing.changed ? `last change ${nice(thing.changed)}` : ''
  ]

  return el(
    'div',
    { className: 'deltos-pad' },
    el(
      'div',
      { className: 'deltos-thing-head' },
      jsx('h2', { children: thing.name }),
      thing.kind ? jsx(Badge, { variant: 'outline', children: thing.kind }) : null,
      thing.dormant ? jsx(Badge, { variant: 'muted', children: 'No longer in use' }) : null
    ),
    jsx('div', {
      className: 'deltos-meta',
      children: [(thing.aliases || []).length ? `Also called ${thing.aliases.join(', ')}` : '', ...meta].filter(Boolean).join(' · ')
    }),
    (thing.about || []).length ? jsx('p', { className: 'deltos-about', children: thing.about.join(' ') }) : null,
    jsx(TimeBar, { thing, today, day }),
    day
      ? jsx(ThenList, { slug, day, today, thing })
      : jsx('div', {
          className: 'deltos-entries',
          children: thing.items.length
            ? thing.items.map(entry => jsx(Entry, { thing, entry, today }, entry.label))
            : jsx('div', { className: 'deltos-quiet', children: 'Nothing is recorded about this yet.' })
        })
  )
}

// ---------------------------------------------------------------------------
// changes view
// ---------------------------------------------------------------------------

const WINDOWS = [
  { id: '7', label: '7 days' },
  { id: '30', label: '30 days' },
  { id: '90', label: '90 days' }
]

function ChangeRow({ row }) {
  return el(
    'button',
    { type: 'button', className: 'deltos-change', title: `Open ${row.thing}`, onClick: () => openThing(row.slug) },
    el('span', { className: 'deltos-change-what' }, jsx('b', { children: row.thing }), ` · ${shown(row.label)}`),
    el(
      'span',
      { className: 'deltos-change-to' },
      jsx('strong', { children: row.value }),
      row.was
        ? el(
            'span',
            { className: 'deltos-was' },
            'was ',
            jsx('s', { children: row.was }),
            row.was_since ? ` since ${nice(row.was_since)}` : ''
          )
        : jsx(Badge, { variant: 'default', size: 'xs', children: 'New' }),
      row.corrected ? jsx('span', { className: 'deltos-was', children: 'an earlier value was recorded by mistake' }) : null
    )
  )
}

function ChangesView({ today }) {
  const days = useValue($days)
  const query = useApi(['changes', days], `/changes?days=${days}`)
  let body
  if (query.data === undefined) {
    body = query.isError
      ? jsx(Oops, { what: 'The changes could not be read.', error: query.error, retry: query.refetch })
      : jsx(Lines, { rows: 5 })
  } else if (!query.data.changes.length) {
    body = el(
      Center,
      { icon: 'check', title: `Nothing changed in the last ${days} days` },
      jsx('p', { children: 'When a fact gets a new value, it shows up here with the value it replaced.' })
    )
  } else {
    const groups = []
    for (const row of query.data.changes) {
      const last = groups[groups.length - 1]
      if (last && last.day === row.day) last.rows.push(row)
      else groups.push({ day: row.day, rows: [row] })
    }
    body = groups.map(group =>
      el(
        'section',
        { key: group.day },
        el(
          'h3',
          { className: 'deltos-day' },
          `${weekday(group.day)} ${nice(group.day)}`,
          jsx('span', { children: ago(daysBetween(group.day, today)) })
        ),
        group.rows.map((row, i) => jsx(ChangeRow, { row }, `${row.slug}|${row.label}|${i}`))
      )
    )
  }

  return el(
    'div',
    { className: 'deltos-pad' },
    el(
      'div',
      { className: 'deltos-view-head' },
      jsx('h2', { children: 'What changed' }),
      jsx(SegmentedControl, { options: WINDOWS, value: days, onChange: id => $days.set(id), sizing: 'content' })
    ),
    jsx('p', { className: 'deltos-lede', children: 'Facts that got a new value, newest first, each with the value it replaced.' }),
    body
  )
}

// ---------------------------------------------------------------------------
// worth a look view
// ---------------------------------------------------------------------------

function DueRow({ row }) {
  const when = row.seen ? `Last seen ${nice(row.seen)}, ${ago(row.age)}` : 'Never dated'
  const asked = row.asks ? ` · asked about ${count(row.asks, 'time')} this month` : ''

  return el(
    'div',
    { className: 'deltos-due' },
    el(
      'div',
      { className: 'deltos-due-body' },
      el(
        'div',
        { className: 'deltos-due-what' },
        jsx('button', { type: 'button', className: 'deltos-link', onClick: () => openThing(row.stem), children: row.thing }),
        ` · ${shown(row.attribute)}`
      ),
      jsx('div', { className: 'deltos-value', children: row.value }),
      jsx('div', { className: 'deltos-when', children: when + asked }),
      row.other ? jsx(SaidOtherwise, { value: row.other[0], by: row.other[1], day: row.other[2] }) : null,
      row.moved
        ? jsx('div', {
            className: 'deltos-also',
            children: `${row.moved[0]} changed ${String(row.moved[1] || '').toLowerCase()} on ${nice(row.moved[2])}, and this has not been seen since.`
          })
        : null,
      jsx(CheckLine, { how: row.how })
    ),
    jsx('div', {
      className: 'deltos-due-tools',
      children: jsx(Button, {
        variant: 'outline',
        size: 'xs',
        onClick: () => askAgent(checkRequest(row.thing, row.attribute, row.value, row.how)),
        children: 'Ask the agent to check'
      })
    })
  )
}

function DueView() {
  const query = useApi(['due', DUE_ROWS], `/due?n=${DUE_ROWS}`)
  let body
  if (query.data === undefined) {
    body = query.isError
      ? jsx(Oops, { what: 'The list could not be read.', error: query.error, retry: query.refetch })
      : jsx(Lines, { rows: 5 })
  } else if (!query.data.due.length) {
    body = el(
      Center,
      { icon: 'check', title: 'Nothing needs a look right now' },
      jsx('p', { children: 'Facts that tend to drift show up here once nobody has confirmed them for a while.' })
    )
  } else {
    body = query.data.due.map((row, i) => jsx(DueRow, { row }, `${row.stem}|${row.attribute}|${i}`))
  }

  return el(
    'div',
    { className: 'deltos-pad' },
    jsx('div', { className: 'deltos-view-head', children: jsx('h2', { children: 'Worth a look' }) }),
    jsx('p', {
      className: 'deltos-lede',
      children:
        'Facts nobody has confirmed in a while. The ones a note disagrees with come first, then the oldest. Ask the agent to look and memory corrects itself.'
    }),
    body
  )
}

// ---------------------------------------------------------------------------
// page
// ---------------------------------------------------------------------------

function versionNotice(health) {
  const theirs = String(health.version || '')
  if (theirs === PLUGIN_VERSION) return ''
  if (!theirs || older(theirs, PLUGIN_VERSION)) {
    return `Hermes is still running ${theirs ? `Deltos ${theirs}` : 'an older Deltos'}. Quit and reopen the Hermes app to finish updating to ${PLUGIN_VERSION}.`
  }

  return `This page is Deltos ${PLUGIN_VERSION}, and Hermes is running Deltos ${theirs}. Update the plugin, then reopen the app, so the two match.`
}

function Ready({ health }) {
  const view = useValue($view)
  const picked = useValue($selected)
  const overview = useApi(['overview', WEEK], `/overview?days=${WEEK}`)
  const things = useApi(['things'], '/things')
  const today = health.today
  const rows = (things.data && things.data.things) || []
  const notice = versionNotice(health)
  const due = overview.data ? overview.data.due : 0
  const tabs = VIEWS.map(v => (v.id === 'due' && due ? { ...v, label: `${v.label} · ${due}` } : v))

  if (things.data && !rows.length) {
    return el(
      Shell,
      { tools: jsx(RefreshButton, {}) },
      notice ? el('div', { className: 'deltos-notice', role: 'status' }, jsx(Codicon, { name: 'info', size: 14 }), jsx('span', { children: notice })) : null,
      el(
        Center,
        { icon: 'notebook', title: 'Memory is empty' },
        el(
          'p',
          null,
          'It fills in as your agent learns things while you work together. You can also bring in notes you already have with ',
          jsx('code', { children: 'deltos import' }),
          '.'
        ),
        health.root ? jsx('p', { children: `Memory lives in ${health.root}` }) : null
      )
    )
  }
  const slug = picked || (rows[0] ? rows[0].slug : '')
  let main
  if (view === 'changes') main = jsx(ChangesView, { today })
  else if (view === 'due') main = jsx(DueView, {})
  else if (slug) main = jsx(ThingView, { slug, today, rows }, slug)
  else if (things.isError) main = null
  else main = jsx('div', { className: 'deltos-pad', children: jsx(Lines, { rows: 5 }) })

  return el(
    Shell,
    {
      tools: [
        jsx(SegmentedControl, { options: tabs, value: view, onChange: id => $view.set(id), sizing: 'content' }, 'views'),
        jsx(RefreshButton, {}, 'refresh')
      ]
    },
    notice ? el('div', { className: 'deltos-notice', role: 'status' }, jsx(Codicon, { name: 'info', size: 14 }), jsx('span', { children: notice })) : null,
    jsx(Summary, { query: overview }),
    el(
      'div',
      { className: 'deltos-body' },
      jsx(Rail, { query: things, today, current: view === 'thing' ? slug : '' }),
      jsx('main', { className: 'deltos-main', children: main })
    )
  )
}

function Page() {
  const health = useApi(['health'], '/health')
  if (health.data === undefined) {
    if (!health.isError) {
      return el(
        Shell,
        null,
        jsx('div', {
          className: 'deltos-stats',
          'aria-busy': 'true',
          children: Array.from({ length: 7 }, (_, i) => jsx(Skeleton, { className: 'deltos-stat-skeleton' }, i))
        }),
        jsx('div', { className: 'deltos-pad', children: jsx(Lines, { rows: 6 }) })
      )
    }
    const missing = errorStatus(health.error) === 404

    return jsx(Shell, {
      children: el(
        Center,
        { icon: 'debug-disconnect', title: missing ? 'Hermes is not answering for Deltos yet' : 'Memory could not be reached' },
        jsx('p', {
          children: missing
            ? 'This page is installed, but the Deltos plugin is not running inside Hermes. Run "hermes plugins enable deltos" in a terminal, then quit and reopen Hermes.'
            : 'Hermes did not answer. It may be restarting.'
        }),
        jsx('div', { className: 'deltos-detail', children: errorText(health.error) }),
        jsx(Button, { variant: 'outline', size: 'sm', onClick: () => health.refetch(), children: 'Try again' })
      )
    })
  }
  if (!health.data.ok) {
    const notice = versionNotice(health.data)

    return el(
      Shell,
      null,
      notice ? el('div', { className: 'deltos-notice', role: 'status' }, jsx(Codicon, { name: 'info', size: 14 }), jsx('span', { children: notice })) : null,
      el(
        Center,
        { icon: 'warning', title: 'Memory is not available' },
        jsx('p', { children: 'The plugin is running, but it cannot open memory:' }),
        jsx('div', { className: 'deltos-detail', children: health.data.reason || 'No reason was given.' }),
        jsx(Button, { variant: 'outline', size: 'sm', onClick: () => health.refetch(), children: 'Try again' })
      )
    )
  }

  return jsx(Ready, { health: health.data })
}

// ---------------------------------------------------------------------------
// plugin
// ---------------------------------------------------------------------------

export default {
  id: ID,
  name: 'Deltos',
  register(ctx) {
    api = ctx

    // Styles ride in with the plugin and leave with it.
    if (typeof document !== 'undefined') {
      const style = document.createElement('style')
      style.textContent = CSS
      document.head.append(style)
      if (typeof ctx.onDispose === 'function') ctx.onDispose(() => style.remove())
    }

    // The agent just used a tool: it may have remembered something. Re-read,
    // once things settle, so an open page follows the conversation.
    if (typeof ctx.onEvent === 'function') {
      let pending = false
      ctx.onEvent('tool.complete', () => {
        if (pending) return
        pending = true
        wait(1500).then(() => {
          pending = false
          refresh()
        })
      })
    }

    ctx.registerMany([
      {
        id: 'page',
        area: sdk.ROUTES_AREA,
        title: 'Memory',
        data: { path: ROUTE },
        render: () => jsx(Page, {})
      },
      {
        id: 'nav',
        area: sdk.SIDEBAR_NAV_AREA,
        data: { path: ROUTE, label: 'Memory', codicon: 'notebook' }
      },
      {
        id: 'open',
        area: sdk.PALETTE_AREA,
        data: {
          id: `${ID}.open`,
          label: 'Memory: open',
          keywords: ['memory', 'deltos', 'remember', 'facts', 'history', 'changes'],
          run: () => {
            $view.set('thing')
            if (host && typeof host.navigate === 'function') host.navigate(ROUTE)
          }
        }
      },
      {
        id: 'due',
        area: sdk.PALETTE_AREA,
        data: {
          id: `${ID}.due`,
          label: 'Memory: what is worth a look',
          keywords: ['memory', 'deltos', 'check', 'stale', 'confirm', 'unsure'],
          run: () => {
            $view.set('due')
            if (host && typeof host.navigate === 'function') host.navigate(ROUTE)
          }
        }
      }
    ])
  }
}
