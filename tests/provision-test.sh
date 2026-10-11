#!/bin/bash
# Run provision.yml against a local mock of the Linode and Vultr APIs: no account,
# no cost. Needs ansible-core and python3.
#   --check creates nothing; a real run creates 4 servers; a second run creates none;
#   addresses reach the later plays; a missing server fails when provision_create=false.
set -euo pipefail
cd "$(dirname "$0")/.."

port=8099
python3 tests/mock_provider_api.py $port & mock=$!
trap 'kill $mock 2>/dev/null' EXIT
for _ in $(seq 20); do curl -fs "http://127.0.0.1:$port/_stats" >/dev/null && break; sleep 0.2; done

export LINODE_TOKEN=testtoken VULTR_API_KEY=testtoken
export DN42_ADMIN_SSH_PUBKEY="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAItestkeytestkeytestkeytestkeytestkey admin@test"
args=(-e "linode_api_url=http://127.0.0.1:$port/v4" -e "vultr_api_url=http://127.0.0.1:$port/v2"
      -e provision_wait_ssh=false -e provision_poll_delay=1 -e provision_timeout=30)
stat() { curl -fs "http://127.0.0.1:$port/_stats" | python3 -c "import json,sys; print(json.load(sys.stdin)['$1'])"; }
fail=0
ok() { if [ "$2" = "$3" ]; then echo "ok   $1"; else echo "FAIL $1 (got '$2', want '$3')"; fail=1; fi; }

ansible-playbook provision.yml --check "${args[@]}" >/dev/null
ok "--check creates nothing" "$(stat creates)" 0

ansible-playbook provision.yml "${args[@]}" >/dev/null
ok "first run creates the 4 cloud PoPs (not the home one)" "$(stat creates)" 4
ok "Vultr admin key registered once, not per server" "$(stat ssh_keys)" 1
ok "no unauthenticated calls" "$(stat unauthorised)" 0

ansible-playbook provision.yml "${args[@]}" >/dev/null
ok "second run is idempotent" "$(stat creates)" 4

# the addresses must reach later plays and beat the TODO values in host_vars
out=$(ansible-playbook tests/show-provisioned.yml "${args[@]}" 2>&1 | grep -E '^\s+"msg"' || true)
echo "$out" | grep -q 'r2: 192.0.2\.' && echo "$out" | grep -q 'r3: 198.51.100\.' \
  && echo "ok   provider addresses override inventory values" \
  || { echo "FAIL provider addresses not applied:"; echo "$out"; fail=1; }
echo "$out" | grep -q 'r1: 192.168.88.10' && echo "ok   home PoP untouched" || { echo "FAIL home PoP changed"; fail=1; }

if ansible-playbook provision.yml -e provision_create=false -e linode_api_url=http://127.0.0.1:$port/v4 \
     -e vultr_api_url=http://127.0.0.1:$port/v2 -e provision_label="dn42-nonexistent-{{ inventory_hostname }}" \
     -e provision_wait_ssh=false >/dev/null 2>&1; then
  echo "FAIL lookup-only mode created or ignored a missing server"; fail=1
else echo "ok   lookup-only mode fails when a server is missing"; fi
ok "lookup-only mode created nothing" "$(stat creates)" 4

# An unset admin key must stop everything before any API call can match or create anything
before=$(stat creates)
if DN42_ADMIN_SSH_PUBKEY="" ansible-playbook provision.yml "${args[@]}" >/dev/null 2>&1; then
  echo "FAIL empty DN42_ADMIN_SSH_PUBKEY was accepted"; fail=1
else echo "ok   empty DN42_ADMIN_SSH_PUBKEY is rejected"; fi
ok "rejected run created nothing" "$(stat creates)" "$before"

# VPCs: opt-in, created per PoP, attached to newly created servers only.
# The mock is fresh for this block because the servers above already exist.
kill $mock 2>/dev/null; wait $mock 2>/dev/null || true
python3 tests/mock_provider_api.py $port & mock=$!
for _ in $(seq 20); do curl -fs "http://127.0.0.1:$port/_stats" >/dev/null && break; sleep 0.2; done
ansible-playbook provision.yml --check -e vpc_enabled=true "${args[@]}" >/dev/null
ok "VPC --check creates nothing" "$(stat vpcs)" 0
ansible-playbook provision.yml -e vpc_enabled=true "${args[@]}" >/dev/null
ok "VPC run creates one VPC per cloud PoP" "$(stat vpcs)" 4
ok "every new server is attached to its VPC" "$(stat attached)" 4
ansible-playbook provision.yml -e vpc_enabled=true "${args[@]}" >/dev/null
ok "VPC re-run creates nothing more" "$(stat vpcs)" 4
ok "VPC re-run creates no new server" "$(stat creates)" 4

exit $fail
