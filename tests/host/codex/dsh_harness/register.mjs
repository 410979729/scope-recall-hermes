// Test-only: `node --import ./register.mjs harness.mjs ...` installs hooks.mjs before the plugin loads.
import { register } from 'node:module'

register('./hooks.mjs', import.meta.url)
