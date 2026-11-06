#!/usr/bin/env bash
# Install a fresh Hermes the way a user does, add Scope Recall the way the plugin catalog does (the repository's
# distribution/hermes directory at one commit), run `hermes memory setup`, and check against a loopback model that
# a fact told in one session reaches the model in the next and that a correction replaces it. No key or remote
# model is involved.
#
#   REPOSITORY=owner/repo COMMIT=<sha> WHEELS=<directory holding the core wheel> bash hermes_catalog_install.sh
#
# WHEELS lets the plugin's pinned core package resolve before that version is on the package index.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
work="${RUNNER_TEMP:-$(mktemp -d)}/hermes-catalog-install"
mkdir -p "$work/home"
export HOME="$work/home"
export PATH="$HOME/.local/bin:$HOME/.hermes/bin:$PATH"
export UV_FIND_LINKS="$WHEELS"

curl -fsSL https://hermes-agent.nousresearch.com/install.sh -o "$work/install.sh"
bash "$work/install.sh" --non-interactive --channel stable
hermes --version

hermes plugins install "$REPOSITORY#distribution/hermes" --ref "$COMMIT" --yes-deps --enable
hermes memory setup scope-recall < /dev/null
hermes memory status

port=18765
python3 "$here/loopback_chat_model.py" "$port" "$work/requests.jsonl" &
model=$!
trap 'kill "$model" 2>/dev/null || true' EXIT
for _ in $(seq 50); do
  curl -fs "http://127.0.0.1:$port/v1/models" > /dev/null && break
  sleep 0.2
done
hermes config set custom_providers "[{\"name\": \"loopback\", \"base_url\": \"http://127.0.0.1:$port/v1\", \"key_env\": \"LOOPBACK_KEY\", \"api_mode\": \"chat_completions\", \"model\": \"loopback-model\", \"models\": {\"loopback-model\": {\"context_length\": 128000}}}]"
hermes config set model.default loopback-model
hermes config set model.provider custom:loopback
hermes config set model.base_url "http://127.0.0.1:$port/v1"
export LOOPBACK_KEY=TEST-loopback-key

# Each `hermes chat -q` is its own session, so anything the model sees of an earlier turn came through memory.
for question in \
  "We are launching the Atlas beta on March 3. It is invite-only." \
  "When does the Atlas beta launch?" \
  "Update: the Atlas beta moves to March 17." \
  "When does the Atlas beta launch?"; do
  hermes chat -q "$question" -Q < /dev/null
done

python3 - "$work/requests.jsonl" <<'EOF'
import json
import re
import sys

# Requests that answer the question and carry recalled memory, in order. Side requests a session makes (a title, for
# instance) carry no memory and are left out.
question = "When does the Atlas beta launch?"
contexts = []
for line in open(sys.argv[1], encoding="utf-8"):
    body = json.loads(line)["body"]
    users = [m for m in body.get("messages") or [] if m.get("role") == "user"]
    found = re.findall(r"<memory-context>[\s\S]*?</memory-context>", json.dumps(body))
    if users and question in json.dumps(users[-1]) and found:
        contexts.append(" ".join(found))
assert contexts, "no request for the question carried recalled memory"
assert "March 3" in contexts[0], f"the first answer's memory did not hold March 3: {contexts[0][:400]!r}"
assert "March 17" in contexts[-1], f"the memory after the correction did not hold March 17: {contexts[-1][:400]!r}"
print("recall in a new session and the correction both reached the model")
EOF
