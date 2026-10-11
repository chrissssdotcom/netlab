# AS4242423420 — dn42 network as code

Five PoPs, one ASN, full WireGuard mesh. Rocky Linux 9 + BIRD 2 (EPEL) + nftables,
built and kept in shape by one Ansible playbook.

| PoP | Where | Provider (region id) | Loopbacks | Block |
|---|---|---|---|---|
| r1 | Adelaide, AU | Home | 172.22.169.33, fdcd:f79a:c76b::1 | fdcd:f79a:c76b:1000::/52 |
| r2 | Frankfurt, DE | Vultr (`fra`) | 172.22.169.34, ::2 | :2000::/52 |
| r3 | Los Angeles, US | Linode (`us-lax`) | 172.22.169.35, ::3 | :3000::/52 |
| r4 | Tokyo, JP | Vultr (`nrt`) | 172.22.169.36, ::4 | :4000::/52 |
| r5 | Sydney, AU | Linode (`ap-southeast`) | 172.22.169.37, ::5 | :5000::/52 |

Everything per-PoP is derived from `router_id` (1–15): loopbacks, `/52`, link-local
addresses and WireGuard ports.

## What it builds

- **WireGuard full mesh** — one `wg-rN` interface per other PoP, link-local only
  (`fe80::<id>`), `Table = off`. Host *i* listens for peer *j* on `42000 + j`.
  One keypair per router, generated on the router; only public keys are cached in
  `pubkeys/`.
- **OSPFv3 + BFD** (BIRD) over the mesh — carries only the `/128` loopbacks; BFD
  detects a dead link in about 3 s.
- **Full iBGP mesh** between loopbacks, IPv4 and IPv6 on one session (IPv4 with
  IPv6 next hops, RFC 8950) — no IPv4 spent on links.
- **eBGP to dn42** per PoP over link-local, with strict ROA validation (refreshed
  every 15 min), dn42-only prefix filters, import limits, path-length cap and
  the dn42 latency/bandwidth/crypto + region/country communities.
- **Origination** — the `/48`, `/27`, `/29` aggregates are announced from every
  PoP (anycast entry); each PoP's `/52` and IPv4 service routes stay inside iBGP.
- **Host hardening** — nftables default-deny inbound, forwarding only
  dn42 ↔ dn42 (never to the public Internet), MSS clamping, SSH keys only,
  chrony, automatic security updates.

## 1. Create the VPSes

Rocky Linux 9 x64, smallest plan with IPv4, your SSH key added at creation:

| PoP | Provider | Region | Plan |
|---|---|---|---|
| r2 | Vultr | Frankfurt | Cloud Compute Regular 1 GB ($5/mo) |
| r3 | Linode | Los Angeles, CA | Nanode 1 GB ($5/mo) |
| r4 | Vultr | Tokyo | Cloud Compute Regular 1 GB ($5/mo) |
| r5 | Linode | Sydney, AU | Nanode 1 GB ($5/mo) |

BIRD and WireGuard run comfortably in 1 GB. If you use the providers' cloud
firewalls, allow UDP 42001–42015, UDP 20000–29999 (external peers) and TCP 22.

**r1 (home):** a Rocky 9 VM or mini PC. It dials every tunnel itself, so the home
router only needs to forward UDP if you want dn42 peers to connect *to* you
(forward the `listen_port` of each external peer).

## 2. Fill in the inventory

- `inventory/hosts.yml` — `ansible_host` for each PoP.
- `inventory/host_vars/rN.yml` — `public_ipv4`, `public_interface` (check with
  `ip -br link`), and later `ebgp_peers`.

## 3. Run

```bash
pip install ansible-core            # ansible-core only; no collections needed
ansible-playbook site.yml           # first run: all PoPs together (keys are exchanged)
ansible-playbook site.yml --limit r3   # later: one PoP at a time
```

Configs are syntax-checked on the router before they are installed
(`bird -p`, `nft -c`), so a bad change is refused rather than applied.

## 4. Add an external dn42 peer

Add an entry to that PoP's `ebgp_peers` (see the commented example in
`host_vars/r2.yml`), then `ansible-playbook site.yml --limit r2`. Give the peer:
your ASN, the PoP's public IP and the `listen_port`, your link-local
`fe80::3420`, and the router's public key (`pubkeys/r2.pub`).

## 5. Check it

```bash
birdc show protocols                 # OSPF, BFD, ibgp_r*, ebgp_* all Established/Running
birdc show ospf neighbors            # Full on every mesh link
birdc show bfd sessions              # Up on every mesh link
birdc show route count               # dn42 table size
birdc show route for 172.20.0.53 all # path, communities, next hop
wg show                              # latest handshake < 2 min on every tunnel
journalctl -u dn42-roa -n 20         # ROA refresh results
```

## Test locally

`tests/run.sh` renders all five PoPs and parses them with the real tools
(`bird -p`, `nft -c`, `wg-quick strip`, `bash -n`). It needs ansible-core,
bird2, nftables and wireguard-tools installed.

## Design notes

- **Changing a WireGuard MTU or address** needs `systemctl restart wg-quick@<if>`;
  peer/key/endpoint changes are applied live with `wg syncconf`.
- **OSPF costs** default to 100 on every link. Once the mesh is up, set
  `mesh_costs` in each host's vars to roughly the RTT in ms, so failover paths
  follow real latency.
- **ROA safety** — the updater refuses a download with fewer than
  `roa_min_entries` routes, so a broken fetch can never empty the tables.
