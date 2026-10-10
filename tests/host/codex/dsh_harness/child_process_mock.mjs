// Test-only stand-in: keep isolated Python, bind this checkout explicitly, and discard host editable bridges.
// SR_FAKE_HOOK still selects the small failure stand-in (fake_hook.mjs).
import { spawn as realSpawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('../../../../', import.meta.url))
const bootstrap = `import importlib.util, runpy, sys, types
from pathlib import Path
root = Path(sys.argv[1]).resolve()
args = sys.argv[2:]
sys.path = [p for p in sys.path if p and any(Path(p).resolve().is_relative_to(Path(base).resolve()) for base in (sys.prefix, sys.base_prefix))]
sys.meta_path = [f for f in sys.meta_path if not getattr(f, '__module__', '').startswith('__editable__')]
pkg = types.ModuleType('scope_recall')
pkg.__path__ = [str(root)]
sys.modules['scope_recall'] = pkg
at = args.index('-m')
module = args[at + 1]
assert Path(importlib.util.find_spec(module).origin).resolve().is_relative_to(root)
sys.argv = [module, *args[at + 2:]]
runpy.run_module(module, run_name='__main__')
`

export function spawn(command, args = [], options = {}) {
  if (process.env.SR_FAKE_HOOK) return realSpawn(process.execPath, [process.env.SR_FAKE_HOOK], options)
  return realSpawn(command, ['-I', '-B', '-c', bootstrap, root, ...args], options)
}
