// react/jsx-runtime for the smoke run: an element is just its type and props.
export const jsx = (type, props, key) => ({ type, props: props || {}, key })
export const jsxs = jsx
export const Fragment = 'fragment'
