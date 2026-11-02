# Shared store in Docker

The image runs the shared store and its remote entry servers on a Linux host. Hooks and MCP clients run on each
host machine and reach the store through a remote entry. The Hermes provider opens store files directly: to share
memory with Hermes, install the complete shared store locally instead of using this image.

Use one local named volume and one container for a store. Stop local processes that open the store before starting
the container, and keep them stopped while it runs. The container lease excludes another container using this image;
local processes do not participate in that lease. Never put the SQLite volume on a network filesystem.
The service listens on a specific private address of the Linux host through host networking. Use a trusted encrypted
network, or put HTTPS in front of it as described in [remote entries](remote-entries.md).

## Build and prepare

From the repository root, set the image version from the package source and give Compose the path of a credentials
file readable by UID 10001. Its contents use the environment names declared by the runtime configuration; the file
is mounted read-only as a Docker secret. Do not put credential values in the repository.

```sh
export SCOPE_RECALL_VERSION="$(python3 -c 'from _version import __version__; print(__version__)')"
read -r -p 'Absolute path to the credentials file: ' SCOPE_RECALL_ENV_FILE
export SCOPE_RECALL_ENV_FILE
docker compose -f distribution/docker/compose.yaml build
```

Create the store and first entry before starting the service:

```sh
docker compose -f distribution/docker/compose.yaml run --rm --entrypoint scope-recall scope-recall init-shared --root /data/store
docker compose -f distribution/docker/compose.yaml run --rm --entrypoint scope-recall scope-recall attach --host codex --instance-root /data/homes/codex --root /data/store --entry codex --display-name Codex
docker compose -f distribution/docker/compose.yaml run --rm --entrypoint scope-recall scope-recall setup --host codex --home /data/homes/codex --store /data/store --project /data --python /usr/local/bin/python --worker on-demand
docker compose -f distribution/docker/compose.yaml run --rm --entrypoint scope-recall scope-recall autostart enable --config /data/store/runtime-config.json --python /usr/local/bin/python --env-file /run/secrets/runtime
```

Setup creates the entry and shared-worker runtime configurations. Model routes and budgets start disabled; follow
[model configuration](install.md#model-routes-and-budgets) to configure them. The generated local host integration
inside the entry home does not connect a remote host; install the remote client on the host machine as described in
[remote entries](remote-entries.md#on-the-client-machine). The container wakes the existing shared worker every five
minutes using its recorded control file.

On the client, create its token as described in that guide. Enter the printed digest and the Linux host's private
address here. The digest is stored in the entry configuration; the token stays on the client.

```sh
read -r -p 'Private address on this Linux host: ' LISTEN_ADDRESS
read -r -p 'Codex token SHA-256 digest: ' CODEX_DIGEST
docker compose -f distribution/docker/compose.yaml run --rm --entrypoint python scope-recall -m scope_recall.adapters.codex.remote_server configure --home /data/homes/codex --host codex --listen "$LISTEN_ADDRESS" --port 18765 --token-sha256 "$CODEX_DIGEST"
docker compose -f distribution/docker/compose.yaml up -d
```

## Add an entry and operate the store

Each entry needs its own home, port, client token and digest. For example, attach a Claude Code entry to the first
entry's scope, then configure its private address and digest. Perform these commands in the running container; restart
it to load the new registration.

```sh
docker compose -f distribution/docker/compose.yaml exec scope-recall scope-recall attach --host claude-code --instance-root /data/homes/claude-code --root /data/store --entry claude-code --display-name 'Claude Code' --grants-like all --capture-like codex --runtime-config-from /data/store/runtime-config.json
read -r -p 'Claude Code token SHA-256 digest: ' CLAUDE_DIGEST
docker compose -f distribution/docker/compose.yaml exec scope-recall python -m scope_recall.adapters.codex.remote_server configure --home /data/homes/claude-code --host claude-code --listen "$LISTEN_ADDRESS" --port 18766 --token-sha256 "$CLAUDE_DIGEST"
docker compose -f distribution/docker/compose.yaml restart
docker compose -f distribution/docker/compose.yaml exec scope-recall scope-recall doctor --host codex --instance-root /data/homes/codex
```

To rotate a token, run `configure` again with its new digest and restart the container. The previous token stops
working. To back up the truth database and copy the snapshot outside the volume:

```sh
docker compose -f distribution/docker/compose.yaml exec scope-recall mkdir -p /data/backups
docker compose -f distribution/docker/compose.yaml exec scope-recall scope-recall backup --database /data/store/memory.sqlite3 --output /data/backups/memory.sqlite3
docker compose -f distribution/docker/compose.yaml cp scope-recall:/data/backups/memory.sqlite3 ./memory.sqlite3
```

Use a new snapshot name for each backup and keep copies outside the Docker host. Follow the
[backup and restore guide](backup-restore.md) for the remaining store files and recovery.

## Upgrade or move a store

For a new image, stop the old container before starting the replacement. Keep the same named volume. The store's
upgrade coordinator takes a snapshot before the first writable open migrates the database; retain that snapshot for
rollback and check the entry with doctor after startup.

```sh
docker compose -f distribution/docker/compose.yaml stop
export SCOPE_RECALL_VERSION="$(python3 -c 'from _version import __version__; print(__version__)')"
docker compose -f distribution/docker/compose.yaml build
docker compose -f distribution/docker/compose.yaml up -d
docker compose -f distribution/docker/compose.yaml exec scope-recall scope-recall doctor --host codex --instance-root /data/homes/codex
```

To move an existing shared store, stop all its entries and worker, back it up, and use a new empty volume. Enter the
absolute path to its copied store directory; it must be readable by UID 10001. Copy the entire directory, adopt it,
then attach each entry again with the entry's existing ID. Configure each remote entry as above before starting the
service.

```sh
read -r -p 'Absolute path to the copied shared store: ' OLD_STORE
docker compose -f distribution/docker/compose.yaml run --rm -v "$OLD_STORE:/old:ro" --entrypoint sh scope-recall -c 'cp -R /old/. /data/store/'
docker compose -f distribution/docker/compose.yaml run --rm --entrypoint scope-recall scope-recall adopt --root /data/store
docker compose -f distribution/docker/compose.yaml run --rm --entrypoint scope-recall scope-recall attach --host codex --instance-root /data/homes/codex --root /data/store --entry codex --display-name Codex --runtime-config-from /data/store/runtime-config.json
```

For other existing entry IDs and hosts, repeat the last command with their own home, ID and display name. See
[moving a shared store](shared-store.md) for the full migration and recovery procedure.
