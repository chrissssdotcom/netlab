# Running this from Semaphore

Everything is plain `ansible-core` (`ansible.builtin` only), so Semaphore needs no
collections or `requirements.yml`. Steps use Semaphore UI's current layout; label
names move slightly between versions.

## Before the first Semaphore run (once, from your workstation)

The WireGuard role caches each router's **public** key in `pubkeys/` so that a run
limited to one PoP still knows its mesh peers. Semaphore works on a checkout, so
anything written there is lost unless it is in git. Public keys are not secret.

```bash
ansible-playbook site.yml          # first full run, from a machine with SSH to every PoP
git add pubkeys/*.pub && git commit -m "Cache router public keys" && git push
```

After that, `--limit` runs from Semaphore work. (A full unlimited run always works,
even without the cache.)

## Automated setup (steps 2-6 below in one command)

`tools/semaphore_setup.py` creates or updates the project, keys, repository,
environment, inventory and all five task templates through Semaphore's API. It is
idempotent, uses only the Python standard library and never prints a secret.

```bash
export SEMAPHORE_URL=https://semaphore.example.net
export SEMAPHORE_TOKEN=...                      # API token; revoke it when done
export DN42_ROUTERS_SSH_KEY_FILE=~/.ssh/dn42_routers      # private key with root on the PoPs
export DN42_GIT_KEY_FILE=~/.ssh/dn42_deploy               # read-only GitHub deploy key (SSH URLs)
export VULTR_API_KEY=... LINODE_TOKEN=...                 # optional; stored as secret env vars
export DN42_ADMIN_SSH_PUBKEY_FILE=~/.ssh/dn42_routers.pub # optional; public key for new servers
python tools/semaphore_setup.py --dry-run       # reads only, shows what it would write
python tools/semaphore_setup.py
```

Anything left unset is skipped, never blanked, so it can be re-run as you add more.
`tests/semaphore-setup-test.sh` exercises it against a local mock of the API.

## 1. Push the repo somewhere Semaphore can clone

Any Git host works. Nothing secret is in the repo: private keys are generated on
the routers and stay there.

```bash
git remote add origin git@<your-git-host>:<you>/dn42-ansible.git
git push -u origin main
```

## 2. Key Store

| Name | Type | Used for |
|---|---|---|
| `dn42-routers-ssh` | SSH Key | the private key whose public half is in `root`'s `authorized_keys` on every PoP |
| `dn42-git` | SSH Key (or Login with password / None) | cloning the repo; *None* if it is public |

## 3. Repository

URL `git@<your-git-host>:<you>/dn42-ansible.git`, branch `main`, access key `dn42-git`.

## 4. Environment

Name `dn42`. Under **Environment variables** add:

```json
{ "ANSIBLE_HOST_KEY_CHECKING": "False" }
```

Without it the first connection to each new VPS waits for a host-key prompt that
nobody can answer, and the task hangs. (If you would rather pin host keys, add them
to the `known_hosts` of the user Semaphore runs as and drop this line.)

Provisioning also needs secrets. In the same Environment, add them under
**Environment variables** and mark them as secrets (they must not be committed):

```json
{ "VULTR_API_KEY": "...", "LINODE_TOKEN": "...", "DN42_ADMIN_SSH_PUBKEY": "ssh-ed25519 AAAA... admin" }
```

`DN42_ADMIN_SSH_PUBKEY` is the *public* half of `dn42-routers-ssh`. Semaphore's
runner needs outbound HTTPS to `api.vultr.com` / `api.linode.com`, and Vultr must
have the runner's IP in its API access control list.

## 5. Inventory

Type **File**, path `inventory/hosts.yml`, repository above, user credentials
`dn42-routers-ssh`. `ansible_user: root` is already set in `group_vars`.

`r1` has a LAN address (`192.168.88.10`). Semaphore must run on that LAN, or reach
it over a VPN, for r1 to be managed. Once the road-warrior tunnel to a cloud PoP is
up, a laptop can do this from anywhere.

## 6. Task templates

| Template | Playbook | Notes |
|---|---|---|
| `dn42 - deploy all` | `site.yml` | the normal one; idempotent, safe to re-run |
| `dn42 - deploy one PoP` | `site.yml` | enable *Allow CLI args in task* (or use the template's *Limit* field if your version has one) and pass `--limit r3`; roll out risky changes one PoP at a time |
| `dn42 - create servers + deploy` | `deploy.yml` | creates any missing VPS, then builds everything; needs the secret variables from section 4 |
| `dn42 - servers dry run` | `provision.yml` | add `--check` as the template's CLI args: lists what would be created and creates nothing |
| `dn42 - road warrior configs` | `rw-clients.yml` | prints the client config for every device and PoP in the task log; add `-e only=laptop` for one device |

Leave out `--check`: the first-run tasks generate keys and read them back, so a dry
run cannot get far on a fresh router.

For every template pick Repository, Inventory `inventory/hosts.yml` and Environment
`dn42`.

## Day-to-day: adding a road-warrior device

1. Edit `rw_clients` in `inventory/group_vars/dn42_routers/main.yml`, commit, push.
2. Run **dn42 — deploy all** (or **deploy one PoP** with `--limit rN` for a single
   PoP; every other PoP ignores a client with a `pops:` list that excludes it).
3. Run **dn42 — road warrior configs** with `-e only=<name>`, copy the config for the
   PoP you want from the log into the device, then paste in its own private key.

The `rw-clients.yml` output holds no secrets (public keys, endpoints, addresses and a
placeholder), so it is fine in Semaphore's task history.

## Things that bite

- **`pubkeys/` not committed** → `No public key cached for rN` on `--limit` runs.
  Fix as above.
- **Hung task on first connect** → missing `ANSIBLE_HOST_KEY_CHECKING`.
- **Changing a tunnel address or MTU** needs `systemctl restart wg-quick@<if>` on the
  router; Semaphore's run applies keys/peers live but not address changes.
- **A bad config never reaches the router**: `bird -p` and `nft -c` validate before
  install, so a failed task leaves the old config running. Read the task log before
  re-running.
- **Do not hand-edit tunnels on a router**: the wireguard role deletes `wg-r*` and
  `dn42-*` configs that are not in the inventory (see `docs/HOSTING.md` for how an
  auto-peering system should therefore work).
