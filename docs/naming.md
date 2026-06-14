# Naming contract

`scope-recall` intentionally uses two spellings because Hermes plugin identity and Python identifiers have different constraints.

## Public/plugin spelling: `scope-recall`

Use the hyphenated spelling when the name is a user-facing project, plugin, provider, storage, or display identity:

- GitHub repository name: `scope-recall-hermes`
- PyPI/distribution package name: `hermes-scope-recall`
- `plugin.yaml` provider name: `scope-recall`
- Hermes config value: `memory.provider: scope-recall`
- unpacked Hermes plugin directory: `$HERMES_HOME/plugins/scope-recall/`
- provider-owned runtime storage directory: `$HERMES_HOME/scope-recall/`
- README, changelog, release notes, docs, and human-facing log/display text when referring to the plugin as a product

Example:

```yaml
memory:
  provider: scope-recall
```

## Python/identifier spelling: `scope_recall`

Use the underscored spelling when the name must be a Python identifier, CLI/tool identifier, SQL identifier, or config key that cannot safely use a hyphen:

- Python import/package identifier: `import scope_recall`
- Python modules/classes/functions when an identifier form is needed
- Hermes tool names such as `scope_recall_store`, `scope_recall_search`, and `scope_recall_context`
- SQL table/index names such as `idx_scope_recall_digest_*`
- script-internal config keys that already follow Python/SQL identifier conventions

Example:

```python
import scope_recall
```

## Install-shape rule

For normal Hermes runtime discovery, install the distribution package and let its installer copy a complete provider directory to the hyphenated plugin path:

```bash
python -m pip install "hermes-scope-recall[lancedb]"
hermes-scope-recall install --hermes-home "${HERMES_HOME:-$HOME/.hermes}"
hermes config set memory.provider scope-recall
hermes memory setup
```

For development, clone this repository directly into the same hyphenated plugin directory:

```bash
mkdir -p "$HERMES_HOME/plugins"
git clone https://github.com/410979729/scope-recall-hermes.git "$HERMES_HOME/plugins/scope-recall"
python -m pip install -e "$HERMES_HOME/plugins/scope-recall[lancedb]"
hermes config set memory.provider scope-recall
```

Do not install the runtime plugin as `$HERMES_HOME/plugins/scope_recall/` unless Hermes itself later documents underscore-directory aliases for memory-provider discovery.

The Python package can still be imported as `scope_recall` after editable/package installation; that import name is not the Hermes memory-provider name.

## Migration rule

Do not mechanically rename all occurrences to one spelling. New code and docs should follow this contract instead:

- public/Hermes identity: `scope-recall`
- Python/tool/SQL/config identifiers: `scope_recall`

If an old identifier must be changed for correctness, add a targeted migration or compatibility shim and document the compatibility impact in `CHANGELOG.md`.
