// Deltos has no page in the web dashboard: its Memory page lives in the Desktop app
// (../../desktop/plugin.js). The dashboard loads one script for every plugin that ships
// a backend, so this file gives it something to load. It registers nothing, and the
// manifest keeps the empty tab out of the navigation ("tab": {"hidden": true}).
