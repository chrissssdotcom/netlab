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

# Road warrior: client configs parse with the placeholder key replaced, and the
# addressing is what the design promises (dual-stack where a v4 pool exists).
for c in build/*/clients/*.conf; do
  h=$(basename "$(dirname "$(dirname "$c")")"); n=$(basename "$c" .conf)
  mkdir -p "$tmp/rwc"; sed "s|^PrivateKey = .*|PrivateKey = $(wg genkey)|" "$c" > "$tmp/rwc/$h-$n.conf"
  chmod 600 "$tmp/rwc/$h-$n.conf"
  WG_QUICK_USERSPACE_IMPLEMENTATION=true wg-quick strip "$tmp/rwc/$h-$n.conf" >/dev/null || { echo "RW CLIENT FAIL $h/$n"; fail=1; }
done
want() { grep -qE "$2" "$1" || { echo "RW CHECK FAIL: $1 lacks /$2/"; fail=1; }; }
want build/r2/wireguard/rw0.conf '^Address = fdcd:f79a:c76b:2f00::1/64, 172\.22\.169\.49/29$'
want build/r2/wireguard/rw0.conf '^AllowedIPs = 172\.22\.169\.50/32, fdcd:f79a:c76b:2f00::2/128$'   # laptop
want build/r2/wireguard/rw0.conf '^AllowedIPs = 172\.22\.169\.54/32, fdcd:f79a:c76b:2f00::6/128$'   # phone
want build/r4/wireguard/rw0.conf '^Address = fdcd:f79a:c76b:4f00::1/64$'                              # no v4 pool
want build/r4/wireguard/rw0.conf '^AllowedIPs = fdcd:f79a:c76b:4f00::6/128$'
want build/r2/clients/laptop.conf '^Address = 172\.22\.169\.50/32, fdcd:f79a:c76b:2f00::2/128$'
want build/r2/dn42.nft '"rw0" oifname "wg-r\*" accept'
want build/r2/bird.conf '^	route 172\.22\.169\.48/29 unreachable;'
want build/r2/dn42.nft 'iifname "ens7" oifname "wg-r\*" ip saddr \{ 172\.22\.169\.32/27, 172\.22\.101\.192/29 \} accept'
want build/r2/dn42.nft 'iifname "ens7" oifname "dn42\*" ip6 saddr \{ fdcd:f79a:c76b::/48 \} accept'
# our own dn42 addressing on the VPC NIC: static, so no DHCP rule and no DHCP client
! grep -q 'udp sport 67' build/r2/dn42.nft || { echo "static VPC ruleset allows DHCP"; fail=1; }
want build/r2/dn42-vpc.nmconnection '^method=manual$'
want build/r2/dn42-vpc.nmconnection '^address1=fdcd:f79a:c76b:2001::1/64$'
want build/r2/dn42-vpc.nmconnection '^address1=172\.22\.169\.61/30$'
want build/r2/dn42-vpc.nmconnection '^never-default=true$'
want build/r2/bird.conf '^	route 172\.22\.169\.60/30 unreachable;'
! grep -q 'rw0' build/r1/wireguard/wg-r2.conf || { echo "RW leaked into a mesh tunnel"; fail=1; }
[ ! -e build/r1/clients/tablet.conf ] || { echo "client pinned to r3 rendered on r1"; fail=1; }
echo "road warrior checks done"

# VLAN mode (Linode): static addressing, IPv6 always, no DHCP anywhere
ANSIBLE_CONFIG=ansible.cfg ansible-playbook tests/render.yml -e pubkey_dir="$tmp/pubkeys" -e bird_roa_dir="$tmp/roa"   -e @tests/sample-peer.yml -e vpc_type=vlan >/dev/null
want build/r2/dn42-vpc.nmconnection '^method=manual$'
want build/r2/dn42-vpc.nmconnection '^address1=172\.22\.169\.61/30$'
want build/r2/dn42-vpc.nmconnection '^address1=fdcd:f79a:c76b:2001::1/64$'
! grep -q 'method=auto' build/r2/dn42-vpc.nmconnection || { echo "VLAN profile uses DHCP"; fail=1; }
! grep -q 'udp sport 67' build/r2/dn42.nft || { echo "VLAN ruleset allows DHCP"; fail=1; }
ANSIBLE_CONFIG=ansible.cfg ansible-playbook tests/render.yml -e pubkey_dir="$tmp/pubkeys" -e bird_roa_dir="$tmp/roa"   -e @tests/sample-peer.yml -e vpc_type=vlan -e vpc_dn42_net4="" >/dev/null
want build/r2/dn42-vpc.nmconnection '^method=disabled$'
# classic provider VPC with provider-assigned addresses (no dn42 subnet): DHCP, no default route
ANSIBLE_CONFIG=ansible.cfg ansible-playbook tests/render.yml -e pubkey_dir="$tmp/pubkeys" -e bird_roa_dir="$tmp/roa"   -e @tests/sample-peer.yml -e vpc_type=vpc -e vpc_dn42_net4="" >/dev/null
want build/r2/dn42-vpc.nmconnection '^method=auto$'
want build/r2/dn42-vpc.nmconnection '^never-default=true$'
want build/r2/dn42.nft 'iifname "ens7" udp sport 67 udp dport 68 accept'
echo "vlan checks done"
exit $fail
