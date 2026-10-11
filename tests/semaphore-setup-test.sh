#!/bin/bash
# Run tools/semaphore_setup.py against a local mock of the Semaphore API.
# No real Semaphore, token or secret is involved. Needs python3.
set -euo pipefail
cd "$(dirname "$0")/.."

port=8098
python3 tests/mock_semaphore.py $port & mock=$!
tmp=$(mktemp -d)
trap 'kill $mock 2>/dev/null; rm -rf "$tmp"' EXIT
for _ in $(seq 20); do curl -fs "http://127.0.0.1:$port/_stats" >/dev/null && break; sleep 0.2; done

export SEMAPHORE_URL="http://127.0.0.1:$port" SEMAPHORE_TOKEN=semtoken
export DN42_GIT_URL="git@github.com:example/netlab.git"
printf 'FAKE-ROUTER-PRIVATE-KEY-SENTINEL\n' > "$tmp/router.key"
printf 'FAKE-GIT-PRIVATE-KEY-SENTINEL\n' > "$tmp/git.key"
export DN42_ROUTERS_SSH_KEY_FILE="$tmp/router.key" DN42_GIT_KEY_FILE="$tmp/git.key"
export VULTR_API_KEY=VULTR-SECRET-SENTINEL LINODE_TOKEN=LINODE-SECRET-SENTINEL
export DN42_ADMIN_SSH_PUBKEY="ssh-ed25519 AAAAtest admin@test"

stat() { curl -fs "http://127.0.0.1:$port/_stats" | python3 -c "import json,sys; print(json.load(sys.stdin)['$1'])"; }
fail=0
ok() { if [ "$2" = "$3" ]; then echo "ok   $1"; else echo "FAIL $1 (got '$2', want '$3')"; fail=1; fi; }

python3 tools/semaphore_setup.py --dry-run > "$tmp/dry.txt"
ok "dry run writes nothing" "$(stat writes)" 0
ok "dry run creates no project" "$(stat projects)" 0

python3 tools/semaphore_setup.py > "$tmp/run1.txt"
ok "one project" "$(stat projects)" 1
ok "3 keys (routers, git, none)" "$(stat keys)" 3
ok "one repository, environment, inventory" "$(stat repositories)$(stat environment)$(stat inventory)" 111
ok "five task templates" "$(stat templates)" 5
w=$(stat writes)

python3 tools/semaphore_setup.py > "$tmp/run2.txt"
ok "second run creates nothing new" "$(stat keys)$(stat templates)$(stat environment)$(stat projects)" 3511
echo "     (second run rewrote $(( $(stat writes) - w )) objects in place)"

if grep -qE 'SECRET-SENTINEL|PRIVATE-KEY-SENTINEL|semtoken' "$tmp/dry.txt" "$tmp/run1.txt" "$tmp/run2.txt"; then
  echo "FAIL a secret or the token appeared in the output"; fail=1
else echo "ok   no secret or token in any output"; fi

SEMAPHORE_TOKEN=wrong python3 tools/semaphore_setup.py >/dev/null 2>"$tmp/err.txt" && { echo "FAIL bad token accepted"; fail=1; } \
  || { grep -q 401 "$tmp/err.txt" && echo "ok   bad token is reported as 401" || { echo "FAIL bad token message"; fail=1; }; }

unset DN42_GIT_KEY_FILE VULTR_API_KEY
python3 tools/semaphore_setup.py --project other > "$tmp/run3.txt"
grep -q 'WARNING.*deploy key' "$tmp/run3.txt" && echo "ok   warns when the SSH repo has no deploy key" || { echo "FAIL no deploy-key warning"; fail=1; }
grep -q 'VULTR_API_KEY is not set' "$tmp/run3.txt" && echo "ok   notes the missing provider token" || { echo "FAIL no provider-token note"; fail=1; }

# A resolver that fails on "both families" but answers per family (WSL's DNS proxy)
python3 - <<'PY' && echo "ok   resolver fallback works when only AF_UNSPEC lookups fail" || { echo "FAIL resolver fallback"; fail=1; }
import socket, sys
sys.path.insert(0, "tools")
def fake(host, port, family=0, *a, **k):
    if family in (0, socket.AF_UNSPEC):
        raise socket.gaierror(-5, "No address associated with hostname")
    if family == socket.AF_INET:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.7", port))]
    raise socket.gaierror(-5, "nope")
socket.getaddrinfo = fake
import semaphore_setup
semaphore_setup.install_resolver_fallback()
assert socket.getaddrinfo("x", 443)[0][4][0] == "192.0.2.7"
try:
    socket.getaddrinfo("x", 443, socket.AF_INET6); sys.exit(1)
except socket.gaierror:
    pass
PY

exit $fail
