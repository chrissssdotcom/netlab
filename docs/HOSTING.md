# Hosting things behind the routers

Web pages, DNS, a looking glass, an auto-peering portal: all of it follows the same
rule. **Give the service an address inside your own dn42 space, make sure that
address lives on (or behind) exactly one PoP, and open its port only to dn42.** The
network already does the rest: BIRD carries the address to every other PoP over
iBGP and to the world over your `/27` and `/48` aggregates.

## What each PoP gives you

| Resource | Where it comes from | Notes |
|---|---|---|
| IPv6 | the PoP's `/52` — `fdcd:f79a:c76b:<N>000::/52`, 4096 × `/64` | already in iBGP, so any address in it is reachable network-wide with no routing change |
| IPv6 already used | `<N>f00::/64` road warriors | start services at `<N>001` |
| IPv4 | `172.22.169.32/27`: `.33-.37` routers, `.40/.48/.56` road-warrior `/29`s | **scarce**: `.38-.39` are spare. Prefer IPv6 for services; ask the registry for more IPv4 before building something v4-hungry |
| Anycast IPv4 | `172.22.101.192/29` | bind the same address on several PoPs (see below) |
| Resources | 1 GB VPS (home PoP: whatever your VM has) | fine for BIRD + WireGuard + a web server; not for a database fleet |

## Pattern A — run the service on the router itself (start here)

Put the service address on the router's `dn42` dummy interface and bind the daemon
to it. In `inventory/host_vars/r2.yml`:

```yaml
loopback_extra6: ["fdcd:f79a:c76b:2001::80/128"]   # inside r2's /52
loopback_extra4: ["172.22.169.38/32"]               # optional; IPv4 is scarce
dn42_input_tcp_ports: [80, 443]                     # allowed from dn42 and road warriors only
```

`ansible-playbook site.yml --limit r2` then creates the addresses, opens the ports
on `wg-r*`, `dn42-*` and `rw0` (never on the public interface) and BIRD starts
carrying the `/128` and `/32`, because anything on the dummy that falls inside your
aggregates is exported to iBGP. Install the daemon yourself or add a role.

```
dnf install -y nginx        # example web server, in Rocky's AppStream
```
```nginx
server {
	listen [fdcd:f79a:c76b:2001::80]:80;
	listen 172.22.169.38:80;
	root /srv/www;
}
```

Bind to the specific address, not `::`, so nothing is ever exposed on the public
IP by accident. (The firewall would still drop it, but defence in depth is cheap.)
`dn42_input_*_ports` is a single list per host, so every service on a PoP shares it.

## Pattern B — a subnet behind the router

When you want several machines, containers or VMs rather than one daemon, give them
a `/64` from the PoP's `/52` on a bridge and let the router route to it:

1. Create the bridge `br-svc` (e.g. NetworkManager or a systemd-networkd unit) with
   `fdcd:f79a:c76b:2001::1/64`; run `radvd` or use static addresses for the hosts.
   Add `br-svc` to the NetworkManager unmanaged list in `roles/common`.
2. In `roles/firewall/templates/dn42.nft.j2`, add forward rules like the road-warrior
   block: `br-svc` ⇄ `wg-r*` / `dn42*` / `rw0` accept. Nothing else forwards, so
   without this the subnet is unreachable. This is the same shape as the `rw0` rules.
3. No BIRD change for IPv6: the `/52` covers it. For an IPv4 subnet, add its prefix
   to `pop_prefixes4` so iBGP carries it (that is what the road-warrior pool does).

The home PoP (r1) is the natural place for this: the "bridge" is your LAN, so
real servers on the LAN become reachable from all of dn42.

### Provider VPC behind a router

`vpc_enabled: true` (per PoP, in `host_vars`) automates the plumbing:

| Where | What happens |
|---|---|
| `provision.yml` | creates the PoP's VPC (`dn42-<host>`, private `10.200+id.0.0/24`) and attaches it as a second NIC when the server is created |
| `site.yml` (`vpc` role) | finds that NIC (or use `vpc_interface`), brings it up with DHCP and **no default route**, and gives it the router's dn42 addresses: `<N>001::1/64` and optionally `vpc_dn42_net4` |
| firewall | forwards between the VPC NIC and dn42/road warriors, but only for packets whose **source is your own dn42 space**; private VPC addresses and anything spoofed are never forwarded. VPC hosts cannot reach the public Internet through the router |
| BIRD | announces `vpc_dn42_net4` via iBGP; the IPv6 `/64` is inside the `/52` that is already announced |

Hosts in the VPC then need, on the VPC NIC: an address from `<N>001::/64` (and/or
the IPv4 subnet), gateway `<N>001::1`, and routes for `fd00::/8` and
`172.20.0.0/14` via the router.

**Verify before building on it.** Providers' VPCs are private IPv4 networks that may
drop packets whose addresses they did not assign. Test with one VM: add a dn42
address to it and ping the router's `<N>001::1` and a remote PoP. If that fails, use
Pattern A or a WireGuard overlay instead. The provider VPCs themselves carry no
IPv6 (Linode's is in limited availability), so IPv6 only works if that test passes.

**Limits.** VPCs attach at creation; changing `vpc_enabled` for an existing server
means rebuilding it. Deleting a VPC (or the server) stays a manual step in the
provider console.

**Containers**: the ruleset starts with `flush ruleset`, which also deletes the NAT
tables Docker and podman create, breaking their networking on the next nftables
reload. Run containers with host networking bound to the service address, or give
them a routed bridge as in Pattern B. Do not use published ports.

## Web pages

- Use Pattern A, one `http://` vhost per address, or one address with name-based
  vhosts. dn42 users resolve `*.dn42` names through the dn42 resolvers, so register
  your domain in the registry when you want nice names (`rw_dns` lets road warriors
  use those resolvers too).
- **TLS**: Let's Encrypt cannot validate a dn42 address, but it can issue for a
  real domain whose AAAA points at your dn42 address, using the DNS-01 challenge.
  Plain HTTP is also normal inside dn42.
- **Anycast**: bind the same address from `172.22.101.192/29` on several PoPs
  (`loopback_extra4: ["172.22.101.193/32"]`) with identical content. Each PoP
  prefers its own copy and visitors from outside land on the nearest one. Caveat:
  a PoP whose web server has died still attracts that address; withdrawing it
  automatically needs a health check driving BIRD, which this repo does not have yet.
- **Reachable from the public Internet?** Not today, by design: inbound is
  default-drop on the public interface. If you want some pages on the clearnet,
  add a `public_input_tcp_ports` list to the firewall template (input only,
  still no forwarding). That variable does not exist yet.

## An auto-peering system

What dn42 users expect: visit a page, prove they own an ASN, submit a WireGuard
key and endpoint, get a working session within minutes. The safe way to build it on
top of this repo:

**1. Front end** — a small web app on one PoP (Pattern A, so IPv6 only is fine),
reachable from dn42. Fields: ASN, WireGuard public key, endpoint (optional), their
link-local address, which PoP they want.

**2. Prove identity** — look the ASN up in the dn42 registry
(`git.dn42.dev/dn42/registry`): `aut-num` → `mnt-by` → the `mntner` object, whose
`auth:` lines hold a PGP fingerprint or SSH public key. Issue a nonce and require a
signature from that key. Never trust a bare ASN.

**3. Validate** — same rules as `roles/common` already enforces: name `[a-z0-9]{1,10}`,
a 44-character base64 key, `fe80::/64` link-local, ASN in `4242420000-4242423999`,
at most N peers per ASN, rate limit per source.

**4. Apply through git, not the router.** Write the peer into the repo and let the
existing pipeline deploy it. Do **not** have the app create tunnels or BIRD config
on the router directly: the `wireguard` role deletes every `dn42-*` and `wg-r*`
config that is not in the inventory, so anything created behind Ansible's back
disappears on the next run, and you lose the `bird -p` / `nft -c` safety checks.

   - Move each PoP's peers to `inventory/host_vars/rN/peers.yml` (and delete the
     `ebgp_peers: []` line from `rN.yml`, since two files defining the same variable
     do not merge). The app only ever edits that one file.
   - The app commits and pushes it (a deploy key, or open a pull request if you want
     to approve each peer by hand), then starts the Semaphore template through its
     API: `POST /api/project/<id>/tasks` with `{"template_id": <n>, "limit": "rN"}`
     and an API token. The token can only start that template.
   - Ansible then opens the peer's UDP port in `nft`, brings up `dn42-<name>`, and
     BIRD starts `ebgp_<name>`. The reply page shows your public key (read it from
     `pubkeys/rN.pub`), your endpoint, your link-local `fe80::3420` and the UDP port.

**5. Keep it honest** — the app needs no root and no SSH to the routers: it can only
produce a reviewed YAML change. Add a periodic Semaphore job that reports peers whose
session is not `Established` after a few days so abandoned entries can be removed.

A good first milestone is the manual form of this (`peers.yml` per PoP plus a
Semaphore button) because it is half of the system already, and every step above
slots onto it.
