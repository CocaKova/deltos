// Minimal stand-in for @hermes/plugin-sdk: enough to load the Memory page and
// walk its render tree in plain node. useQuery really runs the page's queryFn
// (so ctx.rest is asked exactly what the app would ask) and keeps one result
// per query key, the way the app's query cache does.
//
// The bundle inlines its OWN copy of this file, so everything a test drives
// lives on globalThis, where both copies see it.
const stub = (globalThis.__deltosStub ||= {
  queries: new Map(),
  configs: {},
  atoms: [],
  notes: [],
  navigated: [],
  invalidated: [],
  newChats: 0,
  composer: null
})

export const __stub = stub

export const ROUTES_AREA = 'routes'
export const SIDEBAR_NAV_AREA = 'sidebar.nav'
export const PALETTE_AREA = 'palette'

export const host = {
  notify: note => {
    stub.notes.push(note)
  },
  navigate: path => {
    stub.navigated.push(path)
  },
  newChat: () => {
    stub.newChats += 1
  },
  // The test swaps the composer in and out: absent on an older app.
  get composer() {
    return stub.composer || undefined
  }
}

export const atom = initial => {
  let value = initial
  const store = {
    get: () => value,
    set: next => {
      value = next
    },
    reset: () => {
      value = initial
    }
  }
  stub.atoms.push(store)

  return store
}
export const useValue = store => store.get()

function start(key, config) {
  const entry = { status: 'pending', data: undefined, error: null }
  stub.queries.set(key, entry)
  Promise.resolve()
    .then(() => config.queryFn())
    .then(
      data => {
        entry.status = 'success'
        entry.data = data
      },
      error => {
        entry.status = 'error'
        entry.error = error
      }
    )

  return entry
}

export const useQuery = config => {
  const key = JSON.stringify(config.queryKey)
  stub.configs[key] = config
  const entry =
    stub.queries.get(key) ||
    (config.enabled === false ? { status: 'pending', data: undefined, error: null } : start(key, config))

  return {
    data: entry.data,
    error: entry.error,
    isError: entry.status === 'error',
    isPending: entry.status === 'pending',
    isLoading: entry.status === 'pending',
    isFetching: entry.status === 'pending',
    refetch: () => {
      start(key, config)

      return Promise.resolve()
    }
  }
}

export const queryClient = {
  invalidateQueries: ({ queryKey }) => {
    stub.invalidated.push(queryKey)
    const prefix = JSON.stringify(queryKey).slice(0, -1)
    for (const key of [...stub.queries.keys()]) if (key.startsWith(prefix)) stub.queries.delete(key)
  }
}

// Components: plain tags, so the tree walker sees their props and text.
export const Button = 'button'
export const Badge = 'badge'
export const Skeleton = 'skeleton'
export const Codicon = 'i'
export const Input = 'input'
export const CopyButton = 'copy-button'
export const SearchField = 'search-field'
// Rendered, so its options are buttons a test can press.
export const SegmentedControl = ({ options, value, onChange }) => ({
  type: 'segmented',
  props: {
    children: options.map(option => ({
      type: 'button',
      props: { 'aria-pressed': option.id === value, onClick: () => onChange(option.id), children: option.label },
      key: option.id
    }))
  }
})
