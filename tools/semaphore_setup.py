#!/usr/bin/env python3
"""Configure Semaphore UI (tested against the 2.16 API) for this repository.

Idempotent: every object is found by name and updated, or created if missing, so it
is safe to run again. Only the Python standard library is used.

  export SEMAPHORE_URL=https://semaphore.example.net
  export SEMAPHORE_TOKEN=...                 # an API token you can revoke afterwards
  python tools/semaphore_setup.py --dry-run  # reads only, prints what it would write
  python tools/semaphore_setup.py

Optional environment (anything unset is skipped, never blanked):
  DN42_ROUTERS_SSH_KEY_FILE   private key that has root on every router
  DN42_GIT_KEY_FILE           read-only deploy key for the Git repository (SSH URLs)
  VULTR_API_KEY, LINODE_TOKEN stored as *secret* environment variables
  DN42_ADMIN_SSH_PUBKEY       public key given to new servers (or ..._FILE with a path)

Secrets are only ever sent to your own Semaphore, and never printed.
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import urllib.error
import urllib.request

def install_resolver_fallback():
    """Some resolvers (notably WSL's DNS proxy) fail when A and AAAA are asked together
    although each works alone. If the normal lookup fails, retry one family at a time."""
    real = socket.getaddrinfo

    def getaddrinfo(host, port, family=0, *args, **kw):
        try:
            return real(host, port, family, *args, **kw)
        except socket.gaierror as first:
            if family not in (0, socket.AF_UNSPEC):
                raise
            for fam in (socket.AF_INET, socket.AF_INET6):
                try:
                    return real(host, port, fam, *args, **kw)
                except socket.gaierror:
                    pass
            raise first

    socket.getaddrinfo = getaddrinfo


SECRET_KEYS = {"private_key", "passphrase", "secret", "password"}


def mask(o):
    if isinstance(o, dict):
        return {k: ("***" if k in SECRET_KEYS and v else mask(v)) for k, v in o.items()}
    if isinstance(o, list):
        return [mask(x) for x in o]
    return o


class Api:
    def __init__(self, base, token, dry):
        self.base, self.token, self.dry = base.rstrip("/"), token, dry

    def call(self, method, path, body=None, write=False):
        if write and self.dry:
            print(f"  [dry-run] {method} {path} {json.dumps(mask(body), ensure_ascii=False)[:300]}")
            return {"id": -1}
        if self.dry and method == "GET" and path.startswith("/project/-1/"):
            return []          # dry run, project not created yet: nothing can exist in it
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + "/api" + path, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self.token)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            if e.code == 401:
                sys.exit("Semaphore says 401: the token is wrong or expired.")
            sys.exit(f"{method} {path} failed: HTTP {e.code} {detail}")
        except urllib.error.URLError as e:
            sys.exit(f"Cannot reach {self.base}: {e.reason}")

    def ensure(self, label, path, name, body):
        """Create or update the object called `name` in the collection at `path`."""
        existing = next((x for x in self.call("GET", path) if x.get("name") == name), None)
        if existing:
            body = dict(body, id=existing["id"])
            self.call("PUT", f"{path}/{existing['id']}", body, write=True)
            print(f"  {'would update' if self.dry else 'updated':<12} {label}: {name}")
            return existing["id"], existing
        made = self.call("POST", path, body, write=True)
        print(f"  {'would create' if self.dry else 'created':<12} {label}: {name}")
        return made.get("id"), None


def read(path):
    with open(os.path.expanduser(path), encoding="utf-8") as f:
        return f.read()


def git_remote():
    try:
        return subprocess.check_output(["git", "remote", "get-url", "origin"], text=True,
                                       stderr=subprocess.DEVNULL, cwd=os.path.dirname(os.path.abspath(__file__))).strip()
    except Exception:
        return ""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="read from Semaphore, write nothing")
    ap.add_argument("--project", default=os.environ.get("SEMAPHORE_PROJECT", "dn42"))
    ap.add_argument("--git-url", default=os.environ.get("DN42_GIT_URL") or git_remote())
    ap.add_argument("--branch", default="main")
    a = ap.parse_args()

    install_resolver_fallback()
    url, token = os.environ.get("SEMAPHORE_URL", ""), os.environ.get("SEMAPHORE_TOKEN", "")
    if not url or not token:
        sys.exit("Set SEMAPHORE_URL and SEMAPHORE_TOKEN in this shell first.")
    if not a.git_url:
        sys.exit("No Git URL: add an 'origin' remote or pass --git-url.")
    api = Api(url, token, a.dry_run)
    print(("DRY RUN against " if a.dry_run else "Configuring ") + url)

    # --- project -----------------------------------------------------------------
    proj = next((p for p in api.call("GET", "/projects") if p.get("name") == a.project), None)
    if proj:
        pid = proj["id"]
        print(f"  found    project: {a.project} (id {pid})")
    else:
        pid = api.call("POST", "/projects", {"name": a.project}, write=True).get("id")
        print(f"  {'would create' if a.dry_run else 'created':<12} project: {a.project}")
    base = f"/project/{pid}"

    # --- keys ----------------------------------------------------------------------
    def ssh_key(name, env_file):
        path = os.environ.get(env_file, "")
        if not path:
            return None
        body = {"name": name, "type": "ssh", "project_id": pid, "override_secret": True,
                "ssh": {"login": "", "passphrase": "", "private_key": read(path)}}
        return api.ensure("key", f"{base}/keys", name, body)[0]

    routers_key = ssh_key("dn42-routers-ssh", "DN42_ROUTERS_SSH_KEY_FILE")
    git_key = ssh_key("dn42-git", "DN42_GIT_KEY_FILE")
    none_key, _ = api.ensure("key", f"{base}/keys", "dn42-none", {"name": "dn42-none", "type": "none", "project_id": pid})
    if routers_key is None:
        existing = next((k for k in api.call("GET", f"{base}/keys") if k.get("name") == "dn42-routers-ssh"), None)
        routers_key = existing["id"] if existing else None
    if git_key is None:
        existing = next((k for k in api.call("GET", f"{base}/keys") if k.get("name") == "dn42-git"), None)
        git_key = existing["id"] if existing else None
    uses_ssh = a.git_url.startswith(("git@", "ssh://"))
    if uses_ssh and git_key is None:
        print("  WARNING  SSH Git URL but no deploy key: set DN42_GIT_KEY_FILE (or create 'dn42-git' in Semaphore), "
              "otherwise tasks cannot clone the repository.")
    if routers_key is None:
        print("  WARNING  no router SSH key: set DN42_ROUTERS_SSH_KEY_FILE (or create 'dn42-routers-ssh' in Semaphore).")

    # --- repository ----------------------------------------------------------------
    repo_id, _ = api.ensure("repository", f"{base}/repositories", "dn42-ansible", {
        "name": "dn42-ansible", "project_id": pid, "git_url": a.git_url, "git_branch": a.branch,
        "ssh_key_id": (git_key if uses_ssh else none_key) or none_key})

    # --- environment ---------------------------------------------------------------
    pub = os.environ.get("DN42_ADMIN_SSH_PUBKEY", "")
    if not pub and os.environ.get("DN42_ADMIN_SSH_PUBKEY_FILE"):
        pub = read(os.environ["DN42_ADMIN_SSH_PUBKEY_FILE"]).strip()
    plain = {"ANSIBLE_HOST_KEY_CHECKING": "False"}
    if pub:
        plain["DN42_ADMIN_SSH_PUBKEY"] = pub
    env_list = api.call("GET", f"{base}/environment")
    cur = next((e for e in env_list if e.get("name") == "dn42"), None)
    have = {s["name"]: s["id"] for s in (cur or {}).get("secrets") or []}
    secrets = []
    for name in ("VULTR_API_KEY", "LINODE_TOKEN"):
        val = os.environ.get(name, "")
        if val:
            secrets.append({"name": name, "secret": val, "type": "env",
                            **({"operation": "update", "id": have[name]} if name in have else {"operation": "create"})})
        elif name not in have:
            print(f"  note     {name} is not set: add it as a secret in the 'dn42' environment before provisioning.")
    env_id, _ = api.ensure("environment", f"{base}/environment", "dn42", {
        "name": "dn42", "project_id": pid, "json": "{}", "env": json.dumps(plain), "secrets": secrets})

    # --- inventory -----------------------------------------------------------------
    inv_id, _ = api.ensure("inventory", f"{base}/inventory", "dn42-routers", {
        "name": "dn42-routers", "project_id": pid, "type": "file", "inventory": "inventory/hosts.yml",
        "repository_id": repo_id, "ssh_key_id": routers_key or none_key, "become_key_id": none_key})

    # --- task templates ------------------------------------------------------------
    def template(name, playbook, desc, args=None, allow_args=False):
        api.ensure("template", f"{base}/templates", name, {
            "name": name, "project_id": pid, "playbook": playbook, "description": desc,
            "inventory_id": inv_id, "repository_id": repo_id, "environment_id": env_id,
            "arguments": json.dumps(args or []), "allow_override_args_in_task": allow_args,
            "app": "ansible", "type": "", "autorun": False, "suppress_success_alerts": False})

    template("dn42 - deploy all", "site.yml", "Build or update every PoP (idempotent)")
    template("dn42 - deploy one PoP", "site.yml", "Pass --limit chr-<loc>-pe00 as CLI args when running", allow_args=True)
    template("dn42 - create servers + deploy", "deploy.yml", "Create missing VPSes via provider APIs, then build")
    template("dn42 - servers dry run", "provision.yml", "Shows which servers would be created; creates nothing", ["--check"])
    template("dn42 - road warrior configs", "rw-clients.yml", "Client configs per device and PoP; -e only=<name>", allow_args=True)

    print("\nDone." if not a.dry_run else "\nDry run finished; nothing was written.")
    if not a.dry_run:
        print(f"Project: {url.rstrip('/')}/project/{pid}/templates")
    print("Remember: the first full run should come from your workstation so pubkeys/ can be committed (docs/SEMAPHORE.md).")


if __name__ == "__main__":
    main()
