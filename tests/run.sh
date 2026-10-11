#!/bin/bash
# Render all five PoPs and parse them with the real tools (bird -p, nft -c,
# bash -n, wg-quick strip). Needs ansible-core, bird2, nftables, wireguard-tools.
set -euo pipefail
cd "$(dirname "$0")/.."

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/pubkeys" "$tmp/roa"

# Fake node keys and a small ROA sample, only for parsing
for h in chr-adl-pe00 chr-fra-pe00 chr-lax-pe00 chr-tyo-pe00 chr-syd-pe00; do wg genkey | wg pubkey > "$tmp/pubkeys/$h.pub"; done
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
for h in chr-adl-pe00 chr-fra-pe00 chr-lax-pe00 chr-tyo-pe00 chr-syd-pe00; do
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
# --- IPv4 plan: one /29 per PoP; +1 router, +2 loopback, +3 road-warrior /32 ---------------
want build/chr-adl-pe00/bird.conf '^define OWNIP += 172\.22\.169\.34;'
want build/chr-fra-pe00/bird.conf '^define OWNIP += 172\.22\.169\.42;'
want build/chr-lax-pe00/bird.conf '^define OWNIP += 172\.22\.169\.50;'
want build/chr-tyo-pe00/bird.conf '^define OWNIP += 172\.22\.169\.58;'
want build/chr-syd-pe00/bird.conf '^define OWNIP += 172\.22\.101\.194;'
want build/chr-fra-pe00/dn42-loopback.service 'ip address replace 172\.22\.169\.42/32 dev dn42'
[ "$(grep -c 'route 172\.22\.169\.40/29 unreachable' build/chr-fra-pe00/bird.conf)" = 1 ] || { echo "chr-fra-pe00 block not originated exactly once"; fail=1; }
want build/chr-syd-pe00/bird.conf '^	route 172\.22\.101\.192/29 unreachable;'
# --- road warriors: IPv6 for everyone, the one IPv4 /32 for the device that asks -----------
want build/chr-fra-pe00/wireguard/rw0.conf '^Address = fdcd:f79a:c76b:2f00::1/64$'
want build/chr-fra-pe00/wireguard/rw0.conf '^PostUp = ip -4 route replace 172\.22\.169\.43/32 dev %i$'
want build/chr-fra-pe00/wireguard/rw0.conf '^AllowedIPs = 172\.22\.169\.43/32, fdcd:f79a:c76b:2f00::2/128$'   # laptop
want build/chr-fra-pe00/wireguard/rw0.conf '^AllowedIPs = fdcd:f79a:c76b:2f00::6/128$'                          # phone: IPv6 only
want build/chr-tyo-pe00/wireguard/rw0.conf '^PostUp = ip -4 route replace 172\.22\.169\.59/32 dev %i$'
want build/chr-syd-pe00/wireguard/rw0.conf '^AllowedIPs = 172\.22\.101\.195/32, fdcd:f79a:c76b:5f00::2/128$'
want build/chr-fra-pe00/clients/laptop.conf '^Address = 172\.22\.169\.43/32, fdcd:f79a:c76b:2f00::2/128$'
want build/chr-fra-pe00/clients/phone.conf '^Address = fdcd:f79a:c76b:2f00::6/128$'
[ "$(grep -c 'ip -4 route' build/chr-lax-pe00/wireguard/rw0.conf)" = 1 ] || { echo "chr-lax-pe00: expected one IPv4 route (laptop only)"; fail=1; }
want build/chr-fra-pe00/dn42.nft '"rw0" oifname "wg-r\*" accept'
# --- VPC/VLAN: our /29 as the subnet, router at +1, forwarding only from our own space -----
want build/chr-fra-pe00/dn42.nft 'iifname "ens7" oifname "wg-r\*" ip saddr \{ 172\.22\.169\.32/27, 172\.22\.101\.192/29 \} accept'
want build/chr-fra-pe00/dn42.nft 'iifname "ens7" oifname "dn42\*" ip6 saddr \{ fdcd:f79a:c76b::/48 \} accept'
! grep -q 'udp sport 67' build/chr-fra-pe00/dn42.nft || { echo "static VPC ruleset allows DHCP"; fail=1; }
want build/chr-fra-pe00/dn42-vpc.nmconnection '^method=manual$'
want build/chr-fra-pe00/dn42-vpc.nmconnection '^address1=fdcd:f79a:c76b:2001::1/64$'
want build/chr-fra-pe00/dn42-vpc.nmconnection '^address1=172\.22\.169\.41/29$'
want build/chr-syd-pe00/dn42-vpc.nmconnection '^address1=172\.22\.101\.193/29$'
want build/chr-fra-pe00/dn42-vpc.nmconnection '^never-default=true$'
! grep -q 'rw0' build/chr-adl-pe00/wireguard/wg-r2.conf || { echo "RW leaked into a mesh tunnel"; fail=1; }
[ ! -e build/chr-adl-pe00/clients/tablet.conf ] || { echo "client pinned to chr-lax-pe00 rendered on chr-adl-pe00"; fail=1; }
# --- home PoP: an 802.1Q-tagged VLAN on the LAN NIC; Linode PoPs: untagged L2 on the VLAN NIC ---
want build/chr-adl-pe00/dn42-vpc.nmconnection '^type=vlan$'
want build/chr-adl-pe00/dn42-vpc.nmconnection '^interface-name=ens18\.42$'
want build/chr-adl-pe00/dn42-vpc.nmconnection '^id=42$'
want build/chr-adl-pe00/dn42-vpc.nmconnection '^parent=ens18$'
want build/chr-adl-pe00/dn42-vpc.nmconnection '^address1=172\.22\.169\.33/29$'
want build/chr-adl-pe00/dn42.nft 'iifname "ens18\.42" oifname "wg-r\*" ip saddr'
want build/chr-lax-pe00/dn42-vpc.nmconnection '^type=ethernet$'
want build/chr-lax-pe00/dn42-vpc.nmconnection '^interface-name=ens7$'
! grep -q '^\[vlan\]' build/chr-lax-pe00/dn42-vpc.nmconnection || { echo "Linode NIC rendered as a tagged VLAN"; fail=1; }
echo "road warrior checks done"

# VLAN mode (Linode): static addressing, IPv6 always, no DHCP anywhere
ANSIBLE_CONFIG=ansible.cfg ansible-playbook tests/render.yml -e pubkey_dir="$tmp/pubkeys" -e bird_roa_dir="$tmp/roa"   -e @tests/sample-peer.yml -e vpc_type=vlan >/dev/null
want build/chr-fra-pe00/dn42-vpc.nmconnection '^method=manual$'
want build/chr-fra-pe00/dn42-vpc.nmconnection '^address1=172\.22\.169\.41/29$'
want build/chr-fra-pe00/dn42-vpc.nmconnection '^address1=fdcd:f79a:c76b:2001::1/64$'
! grep -q 'method=auto' build/chr-fra-pe00/dn42-vpc.nmconnection || { echo "VLAN profile uses DHCP"; fail=1; }
! grep -q 'udp sport 67' build/chr-fra-pe00/dn42.nft || { echo "VLAN ruleset allows DHCP"; fail=1; }
ANSIBLE_CONFIG=ansible.cfg ansible-playbook tests/render.yml -e pubkey_dir="$tmp/pubkeys" -e bird_roa_dir="$tmp/roa"   -e @tests/sample-peer.yml -e vpc_type=vlan -e vpc_dn42_net4="" >/dev/null
want build/chr-fra-pe00/dn42-vpc.nmconnection '^method=disabled$'
# classic provider VPC with provider-assigned addresses (no dn42 subnet): DHCP, no default route
ANSIBLE_CONFIG=ansible.cfg ansible-playbook tests/render.yml -e pubkey_dir="$tmp/pubkeys" -e bird_roa_dir="$tmp/roa"   -e @tests/sample-peer.yml -e vpc_type=vpc -e vpc_dn42_net4="" >/dev/null
want build/chr-fra-pe00/dn42-vpc.nmconnection '^method=auto$'
want build/chr-fra-pe00/dn42-vpc.nmconnection '^never-default=true$'
want build/chr-fra-pe00/dn42.nft 'iifname "ens7" udp sport 67 udp dport 68 accept'
echo "vlan checks done"
exit $fail
