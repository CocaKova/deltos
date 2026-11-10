// A browser stand-in for @hermes/plugin-sdk, for looking at the Memory page
// outside the app. The same exports the page uses, drawn as plain HTML with the
// classes in app.css. Not the app: close enough to judge layout and colour.
// The export names and component props follow Hermes Desktop's plugin SDK
// (apps/desktop/src/sdk in NousResearch/hermes-agent, MIT, Copyright (c) 2025
// Nous Research); none of its code is copied. See THIRD-PARTY.md.
import { createElement as h, useEffect, useSyncExternalStore } from 'react'

export const ROUTES_AREA = 'routes'
export const SIDEBAR_NAV_AREA = 'sidebar.nav'
export const PALETTE_AREA = 'palette'

const cx = (...names) => names.filter(Boolean).join(' ')

// ── state ──────────────────────────────────────────────────────────────────

export function atom(initial) {
  let value = initial
  const subs = new Set()

  return {
    get: () => value,
    set(next) {
      if (Object.is(next, value)) return
      value = next
      subs.forEach(fn => fn())
    },
    subscribe(fn) {
      subs.add(fn)

      return () => subs.delete(fn)
    }
  }
}

export const useValue = store => useSyncExternalStore(store.subscribe, store.get)

// ── requests: one answer per key, kept while it is asked again ─────────────

const cache = new Map()
const watchers = new Set()
const subscribe = fn => (watchers.add(fn), () => watchers.delete(fn))
const put = (key, entry) => (cache.set(key, entry), watchers.forEach(fn => fn()))

function run(key, config) {
  const before = cache.get(key) || { status: 'pending', data: undefined, error: null }
  if (before.fetching) return
  put(key, { ...before, fetching: true, config })
  const attempt = failures =>
    Promise.resolve()
      .then(() => config.queryFn())
      .then(
        data => put(key, { status: 'success', data, error: null, fetching: false, config }),
        error => {
          if (typeof config.retry === 'function' && config.retry(failures, error)) return attempt(failures + 1)
          put(key, { status: 'error', data: before.data, error, fetching: false, config })
        }
      )
  attempt(0)
}

export function useQuery(config) {
  const key = JSON.stringify(config.queryKey)
  const enabled = config.enabled !== false
  const entry = useSyncExternalStore(subscribe, () => cache.get(key))
  useEffect(() => {
    if (enabled && !cache.has(key)) run(key, config)
  }, [key, enabled])
  const status = entry ? entry.status : 'pending'

  return {
    data: entry && entry.data,
    error: entry ? entry.error : null,
    isError: status === 'error',
    isPending: status === 'pending',
    isLoading: status === 'pending' && enabled,
    isFetching: Boolean(entry && entry.fetching),
    refetch: () => (run(key, config), Promise.resolve())
  }
}

export const queryClient = {
  invalidateQueries({ queryKey }) {
    const prefix = JSON.stringify(queryKey).slice(0, -1)
    for (const [key, entry] of [...cache]) if (key.startsWith(prefix) && entry.config) run(key, entry.config)

    return Promise.resolve()
  }
}

/** True while any request is in flight (the harness waits on this). */
export const __busy = () => [...cache.values()].some(entry => entry.fetching)

// ── the app's doors ────────────────────────────────────────────────────────

const harness = (globalThis.__harness ||= { composer: null })

export const host = {
  notify({ kind, message }) {
    const box = document.getElementById('toasts')
    if (!box) return
    const toast = document.createElement('div')
    toast.className = cx('hx-toast', kind && `is-${kind}`)
    toast.textContent = message
    box.append(toast)
  },
  navigate() {},
  newChat() {},
  get composer() {
    return harness.composer || undefined
  }
}

// ── components ─────────────────────────────────────────────────────────────

export function Button({ variant = 'default', size = 'default', loading, className, children, ...rest }) {
  return h(
    'button',
    { type: 'button', className: cx('hx-btn', `hx-btn-${variant}`, `hx-size-${size}`, className), disabled: rest.disabled || loading, ...rest },
    children
  )
}

export function Badge({ variant = 'default', size = 'default', className, ...rest }) {
  return h('span', { className: cx('hx-badge', `hx-badge-${variant}`, `hx-badge-${size}`, className), ...rest })
}

export const Skeleton = ({ className }) => h('div', { className: cx('hx-skeleton', className) })

export const Codicon = ({ name, size, spinning, className }) =>
  h('i', {
    'aria-hidden': 'true',
    className: cx('codicon', `codicon-${name}`, spinning && 'codicon-modifier-spin', className),
    style: size ? { fontSize: size } : undefined
  })

export function SegmentedControl({ options, value, onChange, sizing = 'equal', disabled = false }) {
  return h(
    'div',
    { className: cx('hx-seg', `is-${sizing}`), style: disabled ? { opacity: 0.5 } : undefined },
    options.map(option =>
      h('button', { key: option.id, type: 'button', disabled, 'aria-pressed': option.id === value, onClick: () => onChange(option.id) }, option.label)
    )
  )
}

export function SearchField({ placeholder, value, onChange, variant = 'underline', 'aria-label': label }) {
  return h(
    'div',
    { className: cx('hx-search', `is-${variant}`) },
    h(Codicon, { name: 'search' }),
    h('input', { type: 'text', placeholder, value, 'aria-label': label || placeholder, onChange: event => onChange(event.target.value) }),
    value ? h('button', { type: 'button', 'aria-label': 'Clear', onClick: () => onChange('') }, h(Codicon, { name: 'close' })) : null
  )
}

export const Input = ({ className, ...rest }) => h('input', { className: cx('hx-input', className), ...rest })

export function CopyButton({ text, label }) {
  return h(
    'button',
    {
      type: 'button',
      className: 'hx-btn hx-btn-ghost hx-size-icon-xs',
      'aria-label': label,
      title: label,
      onClick: () => navigator.clipboard && navigator.clipboard.writeText(text).catch(() => {})
    },
    h(Codicon, { name: 'copy', size: 13 })
  )
}
