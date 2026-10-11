#!/usr/bin/env python3
"""List what this project has created at Linode and Vultr. READ-ONLY: it never deletes anything.

Run it before a teardown or a rebuild to see exactly what exists and roughly what it costs, then
delete things yourself in the provider console or CLI (the commands are printed at the end).

  export LINODE_TOKEN=...  VULTR_API_KEY=...      # either one is optional
  python tools/list_cloud_resources.py

Matches anything tagged "dn42", or labelled dn42-* or chr-* (the old and new naming).
Python standard library only.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

MONTHLY = {"g6-nanode-1": 5, "vc2-1c-1gb": 5}   # the $5 plans this repo provisions


def ours(label, tags=()):
    label = label or ""
    return "dn42" in (tags or []) or label.startswith(("dn42-", "chr-"))


def get(base, path, token):
    req = urllib.request.Request(base.rstrip("/") + path)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        sys.exit(f"GET {base}{path} failed: HTTP {e.code} {e.read().decode(errors='replace')[:200]}")
    except urllib.error.URLError as e:
        sys.exit(f"Cannot reach {base}: {e.reason}")


def table(title, rows, cols):
    print(f"\n{title}")
    if not rows:
        print("  (none)")
        return
    w = [max(len(str(c)), *(len(str(r[i])) for r in rows)) for i, c in enumerate(cols)]
    print("  " + "  ".join(str(c).ljust(w[i]) for i, c in enumerate(cols)))
    for r in rows:
        print("  " + "  ".join(str(v).ljust(w[i]) for i, v in enumerate(r)))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--linode-url", default="https://api.linode.com/v4")
    ap.add_argument("--vultr-url", default="https://api.vultr.com/v2")
    a = ap.parse_args()
    lt, vt = os.environ.get("LINODE_TOKEN", ""), os.environ.get("VULTR_API_KEY", "")
    if not lt and not vt:
        sys.exit("Set LINODE_TOKEN and/or VULTR_API_KEY in this shell first.")
    total, cmds = 0, []

    if lt:
        inst = [i for i in get(a.linode_url, "/linode/instances?page_size=500", lt).get("data", []) if ours(i.get("label"), i.get("tags"))]
        table("Linode servers", [(i["id"], i["label"], i["region"], i["type"], i["status"], ",".join(i.get("tags", []))) for i in inst],
              ("id", "label", "region", "type", "status", "tags"))
        total += sum(MONTHLY.get(i["type"], 0) for i in inst)
        cmds += [f"linode-cli linodes delete {i['id']}      # {i['label']}" for i in inst]
        vpcs = [v for v in get(a.linode_url, "/vpcs?page_size=500", lt).get("data", []) if ours(v.get("label"))]
        table("Linode VPCs", [(v["id"], v["label"], v["region"], ", ".join(s["ipv4"] for s in v.get("subnets", []))) for v in vpcs],
              ("id", "label", "region", "subnets"))
        cmds += [f"linode-cli vpcs delete {v['id']}      # {v['label']} (after its server is gone)" for v in vpcs]
        try:
            vl = [v for v in get(a.linode_url, "/networking/vlans?page_size=500", lt).get("data", []) if ours(v.get("label"))]
        except SystemExit:
            vl = []
        table("Linode VLANs", [(v["label"], v.get("region", "?"), len(v.get("linodes", []))) for v in vl], ("label", "region", "servers"))
        if vl:
            print("  (a VLAN with no servers attached is removed by Linode automatically)")

    if vt:
        inst = [i for i in get(a.vultr_url, "/instances?per_page=500", vt).get("instances", []) if ours(i.get("label"), i.get("tags"))]
        table("Vultr servers", [(i["id"], i["label"], i["region"], i["plan"], i["status"], i.get("main_ip", "")) for i in inst],
              ("id", "label", "region", "plan", "status", "ip"))
        total += sum(MONTHLY.get(i["plan"], 0) for i in inst)
        cmds += [f"vultr-cli instance delete {i['id']}      # {i['label']}" for i in inst]
        vpcs = [v for v in get(a.vultr_url, "/vpcs?per_page=500", vt).get("vpcs", []) if ours(v.get("description"))]
        table("Vultr VPCs", [(v["id"], v["description"], v["region"], f"{v.get('v4_subnet', '')}/{v.get('v4_subnet_mask', '')}") for v in vpcs],
              ("id", "description", "region", "subnet"))
        cmds += [f"vultr-cli vpc2 delete {v['id']}      # {v['description']} (after its server is gone)" for v in vpcs]
        keys = [k for k in get(a.vultr_url, "/ssh-keys?per_page=500", vt).get("ssh_keys", []) if k.get("name") == "dn42-ansible"]
        table("Vultr SSH keys named dn42-ansible", [(k["id"], k["name"]) for k in keys], ("id", "name"))
        cmds += [f"vultr-cli ssh-key delete {k['id']}      # optional: rebuilds re-register it" for k in keys]

    print(f"\nEstimated running cost of the servers above: about ${total}/month (the $5 plans).")
    if cmds:
        print("\nThis tool deleted nothing. To tear down, review the lists, then (servers first):")
        for c in cmds:
            print("  " + c)
        print("  # or use the Linode / Vultr web console. Deleting a server is permanent and its data is gone.")
    print("\nThen rebuild with deploy.yml (Semaphore: 'dn42 - create servers + deploy').")


if __name__ == "__main__":
    main()
