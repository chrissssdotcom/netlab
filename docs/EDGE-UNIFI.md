# Adelaide: BGP between the Rocky router and the UniFi edge

`chr-adl-pe00` (Rocky) peers with the UniFi gateway over the LAN they share. It is a **separate
eBGP session from dn42**, between two private ASNs, carrying only this site's public addresses:

| Direction | What | Why |
|---|---|---|
| Rocky → UniFi | `103.155.36.71/32` | the UniFi routes it to Rocky, which binds it on a dummy interface (`pub0`) and uses it as its public address and WireGuard endpoint |
| Rocky → UniFi | `2402:3120:20fe:10::/60` (placeholder) | a slice of your `/48`; the first `/64` is the VLAN behind Rocky, the rest is spare |
| UniFi → Rocky | `0.0.0.0/0` and `::/0` | Rocky's Internet gateway, no static default route |

Nothing learned from the UniFi ever reaches the dn42 mesh or a dn42 peer, and dn42 traffic is
never forwarded to the Internet: both are enforced in the BIRD filters and the firewall, and
covered by tests.

```
Internet ── UniFi (AS64512) ══ LAN ══ Rocky chr-adl-pe00 (AS64513) ══ VLAN 42 ══ hosts
            default ─────────────────►        ◄───────── 103.155.36.71/32, 2402:3120:20fe:10::/60
                                              └─ WireGuard mesh + dn42 peers over pub0 / 103.155.36.71
```

## 1. Rocky side (this repo)

In `inventory/host_vars/chr-adl-pe00.yml` (the shipped values are placeholders, marked TODO):

```yaml
edge_bgp_enabled: true
edge_local4: 192.168.88.10        # Rocky's address on the LAN it shares with the UniFi
edge_neighbor4: 192.168.88.1      # the UniFi's address on that LAN
edge_local6: "fd00:88::10"        # optional IPv6 session: both ends, global or ULA (not link-local)
edge_neighbor6: "fd00:88::1"
edge_public_ipv4: 103.155.36.71
edge_site_net6: 2402:3120:20fe::/48
edge_public_net6: 2402:3120:20fe:10::/60   # an aligned /60 you are not already using
```

A preflight rejects: a private/dn42 `edge_public_ipv4`, equal or non-private ASNs, a slice that is not an
aligned `/60` inside the `/48`, or an IPv6 neighbour without its local address. Optional MD5 password:
set `EDGE_BGP_PASSWORD` in the environment (Semaphore: a secret environment variable).

Enable it only after the UniFi side below is ready: the session coming up is what installs the default
route, and a wrong prefix is announced exactly as written.

## 2. UniFi side

Which menu you use depends on your UniFi Network version. Recent gateways (Network 9.x and later) offer
BGP under **Settings → Routing**, where you upload an FRR-format configuration; I could not test against
your gateway, so treat this as a starting point and check it against your version's documentation.

```
router bgp 64512
 bgp router-id 192.168.88.1
 no bgp default ipv4-unicast
 neighbor 192.168.88.10 remote-as 64513
 neighbor 192.168.88.10 description chr-adl-pe00
 neighbor fd00:88::10 remote-as 64513
 neighbor fd00:88::10 description chr-adl-pe00-v6
 address-family ipv4 unicast
  neighbor 192.168.88.10 activate
  neighbor 192.168.88.10 default-originate
  neighbor 192.168.88.10 prefix-list ROCKY4-IN in
  neighbor 192.168.88.10 prefix-list DEFAULT-ONLY out
 exit-address-family
 address-family ipv6 unicast
  neighbor fd00:88::10 activate
  neighbor fd00:88::10 default-originate
  neighbor fd00:88::10 prefix-list ROCKY6-IN in
  neighbor fd00:88::10 prefix-list DEFAULT-ONLY6 out
 exit-address-family
!
ip prefix-list ROCKY4-IN seq 5 permit 103.155.36.71/32
ip prefix-list DEFAULT-ONLY seq 5 permit 0.0.0.0/0
ipv6 prefix-list ROCKY6-IN seq 5 permit 2402:3120:20fe:10::/60
ipv6 prefix-list DEFAULT-ONLY6 seq 5 permit ::/0
```

The inbound prefix lists mean the UniFi accepts **only** your `/32` and the `/60`, so a mistake on the
Rocky side cannot announce anything else. Also on the UniFi:

- **No NAT for these addresses.** `103.155.36.71` and the `/60` are routed, not translated: exempt them from
  masquerade/source NAT, or Rocky's traffic leaves from the wrong address.
- **Firewall.** Allow inbound to `103.155.36.71`: UDP `42001-42015` (WireGuard mesh), UDP `20000-29999`
  (external dn42 peers) and UDP `51820` (road warriors); the Rocky host firewall is default-deny on its own.
  Keep the `/60` closed to unsolicited inbound unless you want hosts exposed.
- **If your UniFi cannot do BGP**, use static routes instead and leave `edge_bgp_enabled: false`:
  `103.155.36.71/32 via 192.168.88.10` and `2402:3120:20fe:10::/60 via fd00:88::10`. Rocky then needs its
  default route from DHCP/RA as before.

## 3. Check it

On the Rocky router:

```bash
birdc show protocols edge4 edge6        # Established
birdc show route protocol edge4         # 0.0.0.0/0 learned
birdc show route export edge4           # exactly 103.155.36.71/32
birdc show route export edge6           # exactly the /60
ip route show default                   # via the UniFi, proto bird, src 103.155.36.71
curl -4 --interface 103.155.36.71 https://ifconfig.me   # prints 103.155.36.71
```

Leak check: `birdc show route export ebgp_<peer>` and `birdc show route protocol ibgp_r2` must never show
`0.0.0.0/0`, `::/0` or `103.155.36.71/32`.

## 4. What changes for the home PoP once it is on

- **Public address.** `public_ipv4` becomes `103.155.36.71`, so the other PoPs can dial **in** to the home
  router (a better mesh) and road warriors can connect to it. No port-forwards needed: the address is routed.
- **Hosts on the VLAN** get the public `/64` (`…:10::/64`, router `::1`) alongside their dn42 addresses and can
  reach the Internet from it; inbound is allowed only for connections they started.
- **Addressing the hosts.** The router does not send IPv6 router advertisements; give hosts static addresses from the `/64` (gateway `::1`) or run `radvd` on the VLAN yourself.
- **Fallback.** The LAN's own default route (DHCP/RA) stays as a backup; BGP's default has the lower metric.
