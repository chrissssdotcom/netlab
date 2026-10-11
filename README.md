# AS4242423420 — dn42 network as code

Five PoPs, one ASN, full WireGuard mesh: four Linode servers and one at home, all named
`chr-<loc>-pe00.dc.chrissss.dn42` and each with a VLAN behind it ([diagram](docs/NETWORK.md)).
Rocky Linux 9 + BIRD 2 (EPEL) + nftables, built and kept in shape by Ansible.

| PoP | Where | Platform | IPv4 /29 | Loopbacks | IPv6 block |
|---|---|---|---|---|---|
| chr-adl-pe00 | Adelaide, AU | home server | 172.22.169.32/29 | 172.22.169.34, fdcd:f79a:c76b::1 | fdcd:f79a:c76b:1000::/52 |
| chr-fra-pe00 | Frankfurt, DE | Linode (`de-fra-2`) | 172.22.169.40/29 | 172.22.169.42, ::2 | :2000::/52 |
| chr-lax-pe00 | Los Angeles, US | Linode (`us-lax`) | 172.22.169.48/29 | 172.22.169.50, ::3 | :3000::/52 |
| chr-tyo-pe00 | Tokyo, JP | Linode (`jp-tyo-3`) | 172.22.169.56/29 | 172.22.169.58, ::4 | :4000::/52 |
| chr-syd-pe00 | Sydney, AU | Linode (`ap-southeast`) | 172.22.101.192/29 | 172.22.101.194, ::5 | :5000::/52 |

**IPv4 plan:** each PoP owns one `/29` (`pop_block4` in `host_vars`), laid out the same
way everywhere: `+0` network, **`+1` router** (gateway for hosts behind it), **`+2`
loopback**, **`+3` the one road-warrior `/32`**, `+4..+6` hosts behind the router, `+7`
broadcast. The same `/29` is the VLAN subnet. Loopbacks, the road-warrior
address and the router are derived from it, and a preflight rejects a block that is
misaligned, outside `dn42_aggregates4` or shared between PoPs.

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
- **Road warrior** — a dual-stack WireGuard server per PoP for roaming devices,
  dn42-only (see section 5).
- **Host hardening** — nftables default-deny inbound, forwarding only
  dn42 ↔ dn42 (never to the public Internet), MSS clamping, SSH keys only,
  chrony, automatic security updates.

## 1. Create the VPSes

Rocky Linux 9 x64, smallest plan with IPv4, your SSH key added at creation:

| PoP | Provider | Region | Plan |
|---|---|---|---|
| chr-fra-pe00 | Linode | Frankfurt 2 (`de-fra-2`) | Nanode 1 GB ($5/mo) |
| chr-lax-pe00 | Linode | Los Angeles, CA (`us-lax`) | Nanode 1 GB ($5/mo) |
| chr-tyo-pe00 | Linode | Tokyo 3 (`jp-tyo-3`) | Nanode 1 GB ($5/mo) |
| chr-syd-pe00 | Linode | Sydney, AU (`ap-southeast`) | Nanode 1 GB ($5/mo) |

BIRD and WireGuard run comfortably in 1 GB. If you use the providers' cloud
firewalls, allow UDP 42001–42015, UDP 20000–29999 (external peers), UDP 51820 (road
warriors) and TCP 22.

**chr-adl-pe00 (home):** a Rocky 9 VM or mini PC. It dials every tunnel itself, so the home
router only needs to forward UDP if you want dn42 peers to connect *to* you
(forward the `listen_port` of each external peer).

## 2. Fill in the inventory

- `inventory/hosts.yml` — `ansible_host` for each PoP.
- `inventory/host_vars/rN.yml` — `public_ipv4`, `public_interface` (check with
  `ip -br link`), and later `ebgp_peers`.

## 2b. Or let the playbooks create the servers

`provision.yml` creates the Linode servers through its API (Vultr is still supported: set `provider: Vultr` on a PoP) (Rocky Linux 9,
the $5 / 1 GB plan, your SSH key), finds any that already exist by their label
(`dn42-r2`, ...) and learns each public IP at run time, so no IP is ever committed.
The home PoP is never touched. `deploy.yml` does that and then builds the routers.

```bash
export VULTR_API_KEY=...  LINODE_TOKEN=...            # API tokens, never stored in the repo
export DN42_ADMIN_SSH_PUBKEY="$(cat ~/.ssh/id_ed25519.pub)"   # gets root on every server
ansible-playbook provision.yml --check     # shows what would be created, creates nothing
ansible-playbook deploy.yml                # create what is missing, then build everything
```

- **It costs money**: about $5/month per server, billed from creation. Run `--check` first.
- It is idempotent and only ever *creates*; it never deletes or resizes. Destroy
  servers in the provider's console, then re-run `deploy.yml` to rebuild.
- A rebuilt server gets a new WireGuard key and IP. A full `deploy.yml` run updates
  the mesh; then re-commit `pubkeys/` and give any external dn42 peers the new key and endpoint.
- Only if you put a PoP at Vultr: allow the runner's IP under *Account > API > Access Control* or every call
  returns 401. Tokens need read/write on servers (Linode) / the default key (Vultr).
- A fresh server accepts the provider's generated root password until the first run
  hardens SSH (a few minutes); `deploy.yml` does that straight away.
- **VLANs and custom prefixes**: Linode PoPs can use a VLAN (`vpc_type: vlan`) and Vultr a
  VPC whose subnet is your own dn42 `/29` (`vpc_dn42_net4`), giving dual-stack networks behind
  each router with addresses from your IPAM; see `docs/HOSTING.md`.
- **VPCs (opt-in)**: set `vpc_enabled: true` in a PoP's `host_vars` and `provision.yml`
  also creates that PoP's VPC (Vultr VPC 2.0 / Linode VPC, private subnet
  `10.20x.0.0/24`) and attaches it to the server **when the server is created**.
  Attaching to an existing server is not automated: it is skipped with a notice, so
  attach it in the console or rebuild the server. `site.yml` then brings the NIC up
  and lets hosts behind it use the router as their dn42 gateway. See
  [`docs/HOSTING.md`](docs/HOSTING.md#vlans-behind-every-router).
- `provision_create: false` (or `-e provision_create=false`) turns it into a lookup
  that fails if a server is missing, which is a safe mode for routine runs.

## 3. Run

```bash
pip install ansible-core            # ansible-core only; no collections needed
ansible-playbook site.yml           # first run: all PoPs together (keys are exchanged)
ansible-playbook site.yml --limit chr-lax-pe00   # later: one PoP at a time
```

Configs are syntax-checked on the router before they are installed
(`bird -p`, `nft -c`), so a bad change is refused rather than applied.

## 4. Add an external dn42 peer

Add an entry to that PoP's `ebgp_peers` (see the commented example in
`host_vars/r2.yml`), then `ansible-playbook site.yml --limit chr-fra-pe00`. Give the peer:
your ASN, the PoP's public IP and the `listen_port`, your link-local
`fe80::3420`, and the router's public key (`pubkeys/r2.pub`).

## 5. Road warriors (phones and laptops joining dn42 from anywhere)

Every PoP runs a WireGuard server (`rw0`, UDP 51820). A device connects to
whichever PoP is closest and can then reach everything in dn42, IPv4 and IPv6.
It is **split-tunnel and dn42-only**: the public Internet never goes through the
routers, and the firewall never forwards between dn42 and the Internet.

| | IPv6 | IPv4 |
|---|---|---|
| At each PoP | a `/64`: `fdcd:f79a:c76b:<N>f00::/64`, from that PoP's `/52` | one `/32`: the PoP's `/29` `+3` (`rw_addr4`) |
| Gateway | `::1` | none needed: it is routed straight to the tunnel |
| Client with `id: 5` | `...::5/128` | the `/32` goes to the one device with `ipv4: true` |

Every device gets IPv6. Because a PoP has a single IPv4 `/32`, **at most one device per
PoP** sets `ipv4: true` (a preflight enforces it; use `pops:` if two devices want IPv4 at
different PoPs). The PoP's `/29` is already announced, so the rest of dn42 and your other
PoPs reach a connected device with no extra routing.

**Add a device**

1. On the device, create a tunnel in the WireGuard app and copy its *public* key.
   The private key never leaves the device and is never stored here.
2. Add it to `rw_clients` in `inventory/group_vars/dn42_routers/main.yml`:
   ```yaml
   rw_clients:
     - name: laptop
       id: 2                  # unique; the IPv6 host number
       ipv4: true             # optional: this device gets the PoP's IPv4 /32
       wg_pubkey: "<public key from the device>"
       pops: [chr-adl-pe00, chr-fra-pe00, chr-lax-pe00]     # optional; default is every PoP
   ```
3. `ansible-playbook site.yml` applies it. Connected devices keep their session.
4. `ansible-playbook rw-clients.yml -e only=laptop` prints one config per PoP (and
   writes `build/clients/`). Paste it into the app and replace the
   `PrivateKey` placeholder with the device's own key.

To remove a device, delete its entry and run `site.yml`. To switch the feature
off, set `rw_enabled: false`. The home PoP (chr-adl-pe00) needs a DDNS name in
`wg_endpoint` and a UDP 51820 port-forward before clients can use it.

Further reading: [`docs/SEMAPHORE.md`](docs/SEMAPHORE.md) to run this from Semaphore,
[`docs/HOSTING.md`](docs/HOSTING.md) to put services (web pages, an auto-peering
system) behind the routers.

## 6. Check it

```bash
birdc show protocols                 # OSPF, BFD, ibgp_r*, ebgp_* all Established/Running
birdc show ospf neighbors            # Full on every mesh link
birdc show bfd sessions              # Up on every mesh link
birdc show route count               # dn42 table size
birdc show route for 172.20.0.53 all # path, communities, next hop
wg show                              # latest handshake < 2 min on every tunnel (rw0 shows connected devices)
journalctl -u dn42-roa -n 20         # ROA refresh results
```

## Test locally

`tests/provision-test.sh` exercises provisioning against a local mock of the Linode and
Vultr APIs (no account or cost; needs ansible-core and python3).

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
