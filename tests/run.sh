#!/bin/bash
# Render all five PoPs and parse them with the real tools (bird -p, nft -c,
# bash -n, wg-quick strip). Needs ansible-core, bird2, nftables, wireguard-tools.
set -euo pipefail
cd "$(dirname "$0")/.."

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/pubkeys" "$tmp/roa"

# Fake node keys and a small ROA sample, only for parsing
for h in r1 r2 r3 r4 r5; do wg genkey | wg pubkey > "$tmp/pubkeys/$h.pub"; done
cat > "$tmp/roa/roa_dn42.conf" <<'EOF'
route 172.22.169.32/27 max 32 as 4242423420;
route 172.22.101.192/29 max 32 as 4242423420;
route 172.20.0.0/24 max 28 as 4242420000;
EOF
cat > "$tmp/roa/roa_dn42_v6.conf" <<'EOF'
route fdcd:f79a:c76b::/48 max 64 as 4242423420;
route fd42:d42:d42::/48 max 64 as 4242420000;
EOF

rm -rf build
ANSIBLE_CONFIG=ansible.cfg ansible-playbook tests/render.yml \
  -e pubkey_dir="$tmp/pubkeys" -e bird_roa_dir="$tmp/roa" \
  -e @tests/sample-peer.yml

fail=0
for h in r1 r2 r3 r4 r5; do
  printf '%-3s ' "$h"
  bird -p -c "build/$h/bird.conf"                  && printf 'bird ok  ' || { printf 'BIRD FAIL '; fail=1; }
  nft -c -f "build/$h/dn42.nft"                     && printf 'nft ok  '  || { printf 'NFT FAIL ';  fail=1; }
  bash -n "build/$h/dn42-roa-update"                && printf 'roa ok  '  || { printf 'ROA FAIL ';  fail=1; }
  for c in build/$h/wireguard/*.conf; do
    i=$(basename "$c" .conf); mkdir -p "$tmp/wg"; install -m 600 "$c" "$tmp/wg/$i.conf"
    WG_QUICK_USERSPACE_IMPLEMENTATION=true wg-quick strip "$tmp/wg/$i.conf" >/dev/null || { printf 'WG FAIL(%s) ' "$i"; fail=1; }
  done
  echo "wg ok ($(ls build/$h/wireguard | wc -l) tunnels)"
done
exit $fail
