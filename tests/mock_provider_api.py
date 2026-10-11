#!/usr/bin/env python3
"""Tiny stand-in for the Linode v4 and Vultr v2 instance APIs (stdlib only).

Lets tests/provision-test.sh exercise provision.yml without a real account or any
cost. It enforces the request fields the real APIs require, so a malformed request
fails here too. New servers report "provisioning"/"pending" for two polls, then
become running/active with an address from the documentation ranges.

  GET /_stats  ->  {"creates": N, "ssh_keys": N, "unauthorised": N, "servers": [...]}
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "testtoken"
state = {"linode": [], "vultr": [], "keys": [], "ln_vpcs": [], "vu_vpcs": [], "attached": 0, "vlans": 0, "deletes": 0,
         "creates": 0, "unauth": 0, "next_id": 1000}


def need(body, fields):
    return [f for f in fields if not body.get(f)]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def authed(self):
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            state["unauth"] += 1
            self.send(401, {"error": "unauthorised"})
            return False
        return True

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/_stats":
            return self.send(200, {"creates": state["creates"], "ssh_keys": len(state["keys"]),
                                   "unauthorised": state["unauth"], "vpcs": len(state["ln_vpcs"]) + len(state["vu_vpcs"]),
                                   "attached": state["attached"], "vlans": state["vlans"], "deletes": state["deletes"],
                                   "subnets": sorted([f'{v["v4_subnet"]}/{v["v4_subnet_mask"]}' for v in state["vu_vpcs"]]
                                                     + [sn["ipv4"] for v in state["ln_vpcs"] for sn in v["subnets"]]),
                                   "servers": [s["label"] for s in state["linode"] + state["vultr"]]})
        if not self.authed():
            return
        if p == "/v4/linode/instances":
            return self.send(200, {"data": state["linode"], "page": 1, "pages": 1, "results": len(state["linode"])})
        if p.startswith("/v4/linode/instances/"):
            s = next((x for x in state["linode"] if str(x["id"]) == p.rsplit("/", 1)[1]), None)
            if not s:
                return self.send(404, {"errors": [{"reason": "Not found"}]})
            s["polls"] += 1
            s["status"] = "running" if s["polls"] > 2 else "provisioning"
            return self.send(200, s)
        if p == "/v4/vpcs":
            return self.send(200, {"data": state["ln_vpcs"], "page": 1, "pages": 1, "results": len(state["ln_vpcs"])})
        if p == "/v2/vpcs":
            return self.send(200, {"vpcs": state["vu_vpcs"], "meta": {"total": len(state["vu_vpcs"])}})
        if p == "/v4/networking/vlans":
            return self.send(200, {"data": [], "page": 1, "pages": 1, "results": 0})
        if p == "/v2/ssh-keys":
            return self.send(200, {"ssh_keys": state["keys"], "meta": {"total": len(state["keys"])}})
        if p == "/v2/instances":
            return self.send(200, {"instances": state["vultr"], "meta": {"total": len(state["vultr"])}})
        if p.startswith("/v2/instances/"):
            s = next((x for x in state["vultr"] if x["id"] == p.rsplit("/", 1)[1]), None)
            if not s:
                return self.send(404, {"error": "not found"})
            s["polls"] += 1
            if s["polls"] > 2:
                s.update(status="active", power_status="running", main_ip=s["ip"])
            return self.send(200, {"instance": s})
        self.send(404, {"error": "unknown path " + p})

    def do_DELETE(self):
        state["deletes"] += 1          # a real DELETE would remove things: the mock only counts them
        self.send(204, {})

    def do_POST(self):
        p = self.path.split("?")[0]
        if not self.authed():
            return
        b = self.body()
        state["next_id"] += 1
        i = state["next_id"]
        if p == "/v4/linode/instances":
            miss = need(b, ["label", "region", "type", "image", "root_pass", "authorized_keys"])
            if miss or len(b["root_pass"]) < 12:
                return self.send(400, {"errors": [{"reason": f"missing/invalid {miss or 'root_pass'}"}]})
            for itf in b.get("interfaces", []):
                if itf.get("purpose") == "vpc":
                    sid = itf.get("subnet_id")
                    if not any(sn["id"] == sid for v in state["ln_vpcs"] for sn in v["subnets"]):
                        return self.send(400, {"errors": [{"reason": "unknown subnet_id"}]})
                    state["attached"] += 1
                if itf.get("purpose") == "vlan":
                    if not itf.get("label"):
                        return self.send(400, {"errors": [{"reason": "vlan interface needs a label"}]})
                    state["vlans"] += 1
            if b.get("interfaces") and b["interfaces"][0].get("purpose") != "public":
                return self.send(400, {"errors": [{"reason": "eth0 must stay public"}]})
            s = {"id": i, "label": b["label"], "region": b["region"], "type": b["type"], "image": b["image"],
                 "tags": b.get("tags", []), "status": "provisioning", "polls": 0,
                 "ipv4": [f"198.51.100.{i % 250}"], "ipv6": "2001:db8::/128"}
            state["linode"].append(s)
            state["creates"] += 1
            return self.send(200, s)
        if p == "/v4/vpcs":
            miss = need(b, ["label", "region", "subnets"])
            if miss or not all(sn.get("ipv4") and sn.get("label") for sn in b["subnets"]):
                return self.send(400, {"errors": [{"reason": f"missing/invalid {miss or 'subnets'}"}]})
            v = {"id": i, "label": b["label"], "region": b["region"],
                 "subnets": [{"id": i + 500 + n, "label": sn["label"], "ipv4": sn["ipv4"]} for n, sn in enumerate(b["subnets"])]}
            state["ln_vpcs"].append(v)
            return self.send(200, v)
        if p == "/v2/vpcs":
            miss = need(b, ["region", "description", "v4_subnet", "v4_subnet_mask"])
            if miss or not isinstance(b["v4_subnet_mask"], int):
                return self.send(400, {"error": f"missing/invalid {miss or 'v4_subnet_mask (integer)'}"})
            v = {"id": f"vpc-{i}", "region": b["region"], "description": b["description"],
                 "v4_subnet": b["v4_subnet"], "v4_subnet_mask": b["v4_subnet_mask"]}
            state["vu_vpcs"].append(v)
            return self.send(202, {"vpc": v})
        if p == "/v2/ssh-keys":
            miss = need(b, ["name", "ssh_key"])
            if miss:
                return self.send(400, {"error": f"missing {miss}"})
            k = {"id": f"key-{i}", "name": b["name"], "ssh_key": b["ssh_key"]}
            state["keys"].append(k)
            return self.send(201, {"ssh_key": k})
        if p == "/v2/instances":
            miss = need(b, ["label", "region", "plan", "os_id", "sshkey_id"])
            if miss or not isinstance(b["os_id"], int):
                return self.send(400, {"error": f"missing/invalid {miss or 'os_id (must be an integer)'}"})
            if not all(any(k["id"] == x for k in state["keys"]) for x in b["sshkey_id"]):
                return self.send(400, {"error": "unknown sshkey_id"})
            for vid in b.get("attach_vpc", []):
                if not any(v["id"] == vid for v in state["vu_vpcs"]):
                    return self.send(400, {"error": "unknown vpc id"})
                state["attached"] += 1
            s = {"id": f"vu-{i}", "label": b["label"], "region": b["region"], "plan": b["plan"],
                 "os_id": b["os_id"], "status": "pending", "power_status": "stopped",
                 "server_status": "none", "main_ip": "0.0.0.0", "ip": f"192.0.2.{i % 250}", "polls": 0}
            state["vultr"].append(s)
            state["creates"] += 1
            return self.send(202, {"instance": s})
        self.send(404, {"error": "unknown path " + p})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
