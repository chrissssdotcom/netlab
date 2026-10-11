#!/usr/bin/env python3
"""Minimal in-memory stand-in for the Semaphore UI 2.16 REST API (stdlib only).

Covers just what tools/semaphore_setup.py uses and rejects bodies that would be
rejected by the real schema (wrong types, dangling ids, bad secret operations).
GET /_stats shows how many objects exist and how many writes happened.
"""
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "semtoken"
S = {"projects": [], "writes": 0, "next": 0}
COLL = ("keys", "repositories", "environment", "inventory", "templates")


def nid():
    S["next"] += 1
    return S["next"]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def out(self, code, obj):
        d = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(d)))
        self.end_headers()
        self.wfile.write(d)

    def proj(self, pid):
        return next((p for p in S["projects"] if p["id"] == int(pid)), None)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def auth(self):
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self.out(401, {"error": "unauthorized"})
            return False
        return True

    def check(self, kind, p, b):
        def isint(x): return isinstance(x, int) and not isinstance(x, bool)
        if b.get("project_id") not in (None, p["id"]) or not b.get("name"):
            return "name/project_id"
        ids = lambda coll: {x["id"] for x in p[coll]}
        if kind == "keys":
            if b.get("type") not in ("none", "ssh", "login_password"):
                return "key type"
            if b["type"] == "ssh" and not (b.get("ssh") or {}).get("private_key"):
                return "ssh.private_key"
        if kind == "repositories":
            if not b.get("git_url") or not b.get("git_branch") or b.get("ssh_key_id") not in ids("keys"):
                return "repository fields / ssh_key_id"
        if kind == "environment":
            try:
                json.loads(b.get("env") or "{}"); json.loads(b.get("json") or "{}")
            except ValueError:
                return "env/json must be JSON strings"
            old = next((e for e in p["environment"] if e["id"] == b.get("id")), {"secrets": []})
            for s in b.get("secrets") or []:
                if s.get("type") not in ("env", "var") or s.get("operation") not in ("create", "update", "delete"):
                    return "secret type/operation"
                if s["operation"] != "create" and s.get("id") not in {x["id"] for x in old["secrets"]}:
                    return "secret id for update/delete"
        if kind == "inventory":
            if b.get("type") not in ("static", "static-yaml", "file", "terraform-workspace") or not b.get("inventory"):
                return "inventory type/body"
            if b.get("repository_id") not in ids("repositories") or b.get("ssh_key_id") not in ids("keys"):
                return "inventory repository_id/ssh_key_id"
        if kind == "templates":
            if not b.get("playbook") or b.get("inventory_id") not in ids("inventory") \
               or b.get("repository_id") not in ids("repositories") or b.get("environment_id") not in ids("environment"):
                return "template ids/playbook"
            try:
                assert isinstance(json.loads(b.get("arguments") or "[]"), list)
            except Exception:
                return "arguments must be a JSON array string"
        return None

    def route(self, method):
        if self.path == "/_stats":
            return self.out(200, {"writes": S["writes"], "projects": len(S["projects"]),
                                  **{c: sum(len(p[c]) for p in S["projects"]) for c in COLL}})
        if not self.auth():
            return
        path = self.path.split("?")[0]
        if path == "/api/projects":
            if method == "GET":
                return self.out(200, [{"id": p["id"], "name": p["name"]} for p in S["projects"]])
            b = self.body()
            if not b.get("name"):
                return self.out(400, {"error": "name"})
            p = {"id": nid(), "name": b["name"], **{c: [] for c in COLL}}
            S["projects"].append(p); S["writes"] += 1
            return self.out(201, {"id": p["id"], "name": p["name"]})
        m = re.fullmatch(r"/api/project/(\d+)/(keys|repositories|environment|inventory|templates)(?:/(\d+))?", path)
        if not m:
            return self.out(404, {"error": "no such route " + path})
        p, kind, oid = self.proj(m[1]), m[2], m[3]
        if not p:
            return self.out(404, {"error": "project"})
        if method == "GET" and not oid:
            return self.out(200, p[kind])
        b = self.body()
        why = self.check(kind, p, b)
        if why:
            return self.out(400, {"error": "invalid: " + why})
        if kind == "environment":
            old = next((e for e in p[kind] if str(e["id"]) == (oid or "")), {"secrets": []})
            secrets = [s for s in old["secrets"]]
            for s in b.get("secrets") or []:
                if s["operation"] == "create":
                    secrets.append({"id": nid(), "name": s["name"], "type": s["type"]})
            b = {k: v for k, v in b.items() if k != "secrets"} | {"secrets": secrets}
        if kind == "keys":
            b = {k: v for k, v in b.items() if k not in ("ssh", "login_password")}
        S["writes"] += 1
        if method == "POST":
            o = {**b, "id": nid(), "project_id": p["id"]}
            p[kind].append(o)
            return self.out(201, o)
        i = next((n for n, x in enumerate(p[kind]) if str(x["id"]) == oid), None)
        if i is None:
            return self.out(404, {"error": "object"})
        p[kind][i] = {**b, "id": int(oid), "project_id": p["id"]}
        return self.out(204, {})

    def do_GET(self): self.route("GET")
    def do_POST(self): self.route("POST")
    def do_PUT(self): self.route("PUT")


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 8098), H).serve_forever()
