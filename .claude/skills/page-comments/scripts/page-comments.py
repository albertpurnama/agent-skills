#!/usr/bin/env python3
"""Comment on a web page in the browser; each comment reaches the agent session as an event.

  page-comments.py start (--proxy URL | --serve DIR) [--port N] [--allow-origin ORIGIN ...]
  page-comments.py watch PORT         print each new comment as one line (run it under Claude Code's Monitor)
  page-comments.py stop [PORT]        stop one helper, or all of them
  page-comments.py list               running helpers
  page-comments.py inbox [-n N]       the latest comments
  page-comments.py serve ...          the helper itself (what `start` runs in its pane)

start
  --proxy URL   add the Comment button to a running site, such as a dev server at http://localhost:3000.
                Open the printed address instead of the site's own. Everything passes through,
                live reload included.
  --serve DIR   serve a folder of static pages (reports, HTML exports) with the button added.
  --allow-origin  also accept comments from another site that loads <helper>/__page-comments/overlay.js

The helper runs in a pane to the right of the focused one (Rex or tmux), or in the background elsewhere.
Comments are saved to ~/.cache/page-comments/comments.jsonl; `watch` turns them into events.
"""
import argparse
import errno
import gzip
import http.client
import http.server
import json
import mimetypes
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
OVERLAY_FILE = os.path.join(HERE, "overlay.js")
STATE = os.environ.get("PAGE_COMMENTS_HOME") or os.path.expanduser("~/.cache/page-comments")
COMMENTS = os.path.join(STATE, "comments.jsonl")
PREFIX = "/__page-comments/"
TAG = b'<script src="/__page-comments/overlay.js" async></script>'
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection",
       "te", "trailer", "trailers", "transfer-encoding", "upgrade"}


def clean(value, limit):
    """One line of plain text: control characters (newlines, escape sequences) become spaces."""
    text = re.sub(r"[\x00-\x1f\x7f-\x9f  ]", " ", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def inject(html):
    """Add the overlay script tag, in <head> when there is one."""
    low = html.lower()
    i = low.find(b"</head>")
    if i == -1:
        i = low.rfind(b"</body>")
    return html + TAG if i == -1 else html[:i] + TAG + html[i:]


def as_line(c):
    where = f'{c["tag"]} "{c["snippet"]}"' if c.get("snippet") else c["tag"]
    return f'[page comment] {c["text"]}  (on {where} · {c["selector"]} · {c["page"]})'


def rex_bin():
    return shutil.which("rex") or os.path.join(os.environ.get("REX_BIN_DIR", ""), "rex")


# ---------------------------------------------------------------- the server

def make_handler(cfg):
    target = urllib.parse.urlsplit(cfg.proxy) if cfg.proxy else None
    target_origin = f"{target.scheme}://{target.netloc}" if target else None
    serve_root = os.path.realpath(cfg.serve) if cfg.serve else None
    allowed = {f"http://127.0.0.1:{cfg.port}", f"http://localhost:{cfg.port}", f"http://[::1]:{cfg.port}", *cfg.allow_origin}
    base = f"http://127.0.0.1:{cfg.port}{PREFIX}"
    lock = threading.Lock()

    def save(record):
        with lock:
            if not cfg.dry_run:
                os.makedirs(STATE, exist_ok=True)
                with open(COMMENTS, "a") as f:
                    f.write(json.dumps(record) + "\n")
            print(time.strftime("%H:%M:%S"), as_line(record), flush=True)

    def upstream():
        port = target.port or (443 if target.scheme == "https" else 80)
        if target.scheme == "https":  # dev servers often use self-signed certificates
            return http.client.HTTPSConnection(target.hostname, port, context=ssl._create_unverified_context())
        return http.client.HTTPConnection(target.hostname, port)

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"  # one request per connection keeps streaming and proxying simple
        server_version = "page-comments"

        def log_message(self, *a):
            pass

        def here(self):
            return "http://" + (self.headers.get("Host") or f"localhost:{cfg.port}")

        def reply(self, code, body=b"", ctype="text/plain; charset=utf-8", cors=False):
            self.send_response(code)
            origin = self.headers.get("Origin")
            if cors and origin in allowed:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Comment-Token")
                self.send_header("Access-Control-Allow-Methods", "POST")
                self.send_header("Vary", "Origin")
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def route(self):
            path = urllib.parse.urlsplit(self.path).path
            try:
                if path.startswith(PREFIX):
                    return self.own(path[len(PREFIX):])
                if target:
                    return self.tunnel() if self.headers.get("Upgrade") else self.proxy()
                if self.command in ("GET", "HEAD"):
                    return self.static(path)
                self.reply(405, b"method not allowed")
            except (BrokenPipeError, ConnectionResetError):
                pass

        do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = route

        # -- the helper's own endpoints
        def own(self, name):
            if name == "overlay.js" and self.command == "GET":
                # Only to <script> tags: page code fetching the file can't read the token from it.
                if self.headers.get("Sec-Fetch-Dest", "script") != "script":
                    return self.reply(403, b"load this with a script tag")
                with open(OVERLAY_FILE) as f:
                    js = f.read().replace("__TOKEN__", cfg.token).replace("__BASE__", base)
                return self.reply(200, js.encode(), "text/javascript; charset=utf-8", cors=True)
            if name == "comment" and self.command == "OPTIONS":
                return self.reply(204, cors=True)
            if name == "comment" and self.command == "POST":
                return self.comment()
            self.reply(404, b"not found")

        def comment(self):
            origin = self.headers.get("Origin")
            if origin not in allowed or not secrets.compare_digest(self.headers.get("X-Comment-Token", ""), cfg.token):
                print(f"refused a comment from {origin or 'no origin'}", flush=True)
                return self.reply(403, b"not allowed", cors=True)
            n = int(self.headers.get("Content-Length") or 0)
            if n > 20000:
                return self.reply(413, b"too long", cors=True)
            try:
                d = json.loads(self.rfile.read(n))
            except ValueError:
                return self.reply(400, b"bad json", cors=True)
            text = clean(d.get("text"), 2000)
            if not text:
                return self.reply(400, b"empty comment", cors=True)
            url = urllib.parse.urlsplit(clean(d.get("url"), 500))
            save({"t": time.time(), "port": cfg.port, "text": text,
                  "page": url.path + (f"?{url.query}" if url.query else ""),
                  "tag": clean(d.get("tag"), 20), "snippet": clean(d.get("snippet"), 120),
                  "selector": clean(d.get("selector"), 200), "site": cfg.proxy or serve_root})
            self.reply(200, b"sent", cors=True)

        # -- a folder of static pages
        def static(self, path):
            full = os.path.realpath(os.path.join(serve_root, urllib.parse.unquote(path).lstrip("/")))
            if os.path.isdir(full):
                full = os.path.join(full, "index.html")
            if not (full == serve_root or full.startswith(serve_root + os.sep)) or not os.path.isfile(full):
                return self.reply(404, b"not found")
            with open(full, "rb") as f:
                data = f.read()
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            if ctype == "text/html":
                data, ctype = inject(data), "text/html; charset=utf-8"
            self.reply(200, data, ctype)

        # -- in front of a running site
        def forwarded_headers(self):
            out = []
            for k, v in self.headers.items():
                kl = k.lower()
                if kl in HOP or kl == "accept-encoding":
                    continue
                if kl == "host":
                    v = target.netloc
                elif kl in ("origin", "referer") and v.startswith(self.here()):
                    v = target_origin + v[len(self.here()):]  # the site sees its own address, not ours
                out.append((k, v))
            return out

        def proxy(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else None
            conn = upstream()
            try:
                conn.putrequest(self.command, self.path, skip_host=True, skip_accept_encoding=True)
                for k, v in self.forwarded_headers():
                    conn.putheader(k, v)
                conn.putheader("Accept-Encoding", "identity")  # so pages can be edited on the way through
                conn.endheaders(body)
                resp = conn.getresponse()
            except OSError as err:
                msg = f"page-comments: nothing answered at {cfg.proxy} ({err}). Is the dev server running?"
                return self.reply(502, msg.encode())
            ctype = (resp.getheader("Content-Type") or "").lower()
            is_html = self.command != "HEAD" and resp.status not in (204, 304) and ctype.startswith("text/html")
            data, enc = None, ""
            if is_html:
                data = resp.read()
                enc = (resp.getheader("Content-Encoding") or "").lower()
                try:
                    data = gzip.decompress(data) if enc == "gzip" else zlib.decompress(data) if enc == "deflate" else data
                    if enc in ("", "identity", "gzip", "deflate"):
                        data, enc = inject(data), None
                except (OSError, zlib.error):
                    pass  # leave a body we can't decode as it is
            self.send_response_only(resp.status, resp.reason)
            for k, v in resp.getheaders():
                kl = k.lower()
                if kl in HOP:
                    continue
                if is_html and (kl == "content-length" or kl.startswith("content-security-policy")
                                or (kl == "content-encoding" and enc is None)):
                    continue  # the page changed; a CSP would also block the button
                if kl == "location" and v.startswith(target_origin):
                    v = self.here() + v[len(target_origin):]
                self.send_header(k, v)
            if is_html:
                self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            if is_html:
                self.wfile.write(data)
            elif self.command != "HEAD":
                while True:  # stream, so event streams and large files flow as they arrive
                    chunk = resp.read1(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            conn.close()

        def tunnel(self):
            """WebSocket (live reload): pass the bytes through both ways."""
            port = target.port or (443 if target.scheme == "https" else 80)
            try:
                up = socket.create_connection((target.hostname, port), timeout=10)
                if target.scheme == "https":
                    up = ssl._create_unverified_context().wrap_socket(up, server_hostname=target.hostname)
            except OSError:
                return self.reply(502, b"dev server not answering")
            lines = [f"{self.command} {self.path} HTTP/1.1"]
            for k, v in self.headers.items():
                kl = k.lower()
                if kl == "host":
                    v = target.netloc
                elif kl == "origin" and v.startswith(self.here()):
                    v = target_origin + v[len(self.here()):]
                lines.append(f"{k}: {v}")
            up.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
            up.settimeout(None)
            client = self.connection  # nothing is left in rfile: a WebSocket client waits for the 101 first

            def pump(src, dst):
                try:
                    while True:
                        b = src.recv(65536)
                        if not b:
                            break
                        dst.sendall(b)
                except OSError:
                    pass
                for s in (src, dst):
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

            t = threading.Thread(target=pump, args=(up, client), daemon=True)
            t.start()
            pump(client, up)
            t.join()
            up.close()
            self.close_connection = True

    return Handler, allowed


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Server6(Server):
    address_family = socket.AF_INET6


def pidfile(port):
    return os.path.join(STATE, "servers", f"{port}.pid")


def cursor_file(port):
    return os.path.join(STATE, "servers", f"{port}.cursor")


def cmd_serve(cfg):
    handler, allowed = make_handler(cfg)
    servers = [Server(("127.0.0.1", cfg.port), handler)]
    try:  # browsers try ::1 first for "localhost"
        servers.append(Server6(("::1", cfg.port), handler))
    except OSError:
        pass
    os.makedirs(os.path.join(STATE, "servers"), exist_ok=True)
    with open(pidfile(cfg.port), "w") as f:
        f.write(str(os.getpid()))

    def bye(*_):
        try:
            os.remove(pidfile(cfg.port))
        except OSError:
            pass
        os._exit(0)

    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGHUP, bye)
    what = f"in front of {cfg.proxy}" if cfg.proxy else f"serving {cfg.serve.replace(os.path.expanduser('~'), '~')}"
    print(f"page-comments on http://localhost:{cfg.port}/, {what}", flush=True)
    print(f"accepting comments from {', '.join(sorted(allowed))}{' (dry run: not saved)' if cfg.dry_run else ''}", flush=True)
    for s in servers[1:]:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        bye()


# ---------------------------------------------------------------- start / watch / stop / list / inbox

def port_free(port):
    for fam, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        s = socket.socket(fam, socket.SOCK_STREAM)
        try:
            s.bind((host, port))
        except OSError as err:
            if fam == socket.AF_INET6 and err.errno == errno.EADDRNOTAVAIL:  # no IPv6 loopback: fine
                continue
            return False
        finally:
            s.close()
    return True


def ancestors(pid):
    out = []
    while pid > 1:
        out.append(pid)
        r = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True)
        pid = int(r.stdout.strip() or 0)
    return out


def rex_anchor():
    """The Rex pane to open the helper beside: this session's own pane when we can confirm it,
    otherwise the focused pane of the window the user is looking at. $REX_BLOCK alone isn't
    trusted: a background Claude Code session inherits it from whichever terminal started
    Claude Code's background service, which is usually another session's pane."""
    rex, block = rex_bin(), os.environ.get("REX_BLOCK")
    if block and not os.environ.get("CLAUDE_JOB_DIR"):
        r = subprocess.run([rex, "block", "call", "-b", block, "process"], capture_output=True, text=True)
        try:
            if json.loads(r.stdout)["foreground"]["pid"] in ancestors(os.getpid()):
                return ["-b", block]
        except (ValueError, KeyError, TypeError):
            pass
    r = subprocess.run([rex, "window", "ls", "--json"], capture_output=True, text=True)
    try:
        d = json.loads(r.stdout)
        win = next(w for w in d["windows"] if w["window_id"] == d["active_window_id"])
        return ["-b", win["focused_block_id"]]
    except (ValueError, KeyError, StopIteration):
        return []


def cmd_start(a):
    if a.proxy:
        a.proxy = a.proxy.rstrip("/")
        if not urllib.parse.urlsplit(a.proxy).scheme:
            a.proxy = "http://" + a.proxy
    if a.serve and not os.path.isdir(a.serve):
        sys.exit(f"no such folder: {a.serve}")
    port = a.port or next((p for p in range(8765, 8800) if port_free(p)), None)
    if not port or not port_free(port):
        sys.exit(f"port {port} is taken")
    me = os.path.abspath(__file__)
    cmd = [sys.executable, me, "serve", "--port", str(port),
           *(["--proxy", a.proxy] if a.proxy else ["--serve", os.path.abspath(a.serve)]),
           *[x for o in a.allow_origin for x in ("--allow-origin", o)]]
    state = {"port": port, "site": a.proxy or os.path.abspath(a.serve), "started": time.time()}

    if os.environ.get("REX_SERVER") and os.access(rex_bin(), os.X_OK):
        r = subprocess.run([rex_bin(), "split", *rex_anchor(), "--split=right", "--ratio", "35", "--focus=false", "--keep-open",
                            "--label", f"page-comments-{port}", "--json", "--", *cmd], capture_output=True, text=True)
        if r.returncode:
            sys.exit("rex split failed: " + r.stderr.strip())
        state["pane"] = "rex:" + json.loads(r.stdout)["block_ids"][0]
    elif os.environ.get("TMUX") and shutil.which("tmux"):
        r = subprocess.run(["tmux", "split-window", "-h", "-d", "-l", "35%", "-P", "-F", "#{pane_id}",
                            shlex.join(cmd) + "; echo '[page-comments stopped]'; read -r _"], capture_output=True, text=True)
        if r.returncode:
            sys.exit("tmux split-window failed: " + r.stderr.strip())
        state["pane"] = "tmux:" + r.stdout.strip()
    else:
        os.makedirs(STATE, exist_ok=True)
        log = open(os.path.join(STATE, f"server-{port}.log"), "a")
        subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        state["log"] = log.name

    for _ in range(50):  # up to 10 s
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}{PREFIX}overlay.js", timeout=1)
            break
        except OSError:
            time.sleep(0.2)
    else:
        sys.exit("the helper didn't start; check its pane" + (f" or {state['log']}" if state.get("log") else ""))
    os.makedirs(os.path.join(STATE, "servers"), exist_ok=True)
    with open(os.path.join(STATE, "servers", f"{port}.json"), "w") as f:
        json.dump(state, f)
    with open(cursor_file(port), "w") as f:  # `watch` reports comments from now on
        f.write(str(os.path.getsize(COMMENTS) if os.path.exists(COMMENTS) else 0))

    url = f"http://localhost:{port}/"
    print(f"Open {url}")
    if a.serve:
        pages = sorted(p for p in os.listdir(a.serve) if p.endswith(".html"))
        if pages and "index.html" not in pages:
            print("Pages: " + ", ".join(url + p for p in pages[:8]))
    if a.proxy:
        try:
            urllib.request.urlopen(a.proxy, timeout=3)
        except urllib.error.HTTPError:
            pass
        except OSError:
            print(f"Note: nothing answers at {a.proxy} yet. Start the dev server; the address above works once it does.")
    print("Comment: ⌥-click anything on the page, or press C, or use the Comment button.")
    print(f"Watch for comments (Monitor, 30 min, re-arm when it expires): python3 {shlex.quote(me)} watch {port}")
    print(f"Stop: python3 {shlex.quote(me)} stop {port}")


def cmd_watch(a):
    """Print each new comment for this helper as one line; exit when the helper stops."""
    if not os.path.exists(pidfile(a.port)):
        sys.exit(f"no helper running on port {a.port}")
    try:
        pos = int(open(cursor_file(a.port)).read())
    except (OSError, ValueError):
        pos = os.path.getsize(COMMENTS) if os.path.exists(COMMENTS) else 0
    last_check = 0
    while True:
        size = os.path.getsize(COMMENTS) if os.path.exists(COMMENTS) else 0
        if size < pos:
            pos = 0  # the file was replaced
        if size > pos:
            with open(COMMENTS, "rb") as f:
                f.seek(pos)
                chunk = f.read()
            done = chunk.rfind(b"\n") + 1  # only whole lines
            for line in chunk[:done].splitlines():
                try:
                    c = json.loads(line)
                except ValueError:
                    continue
                if c.get("port") == a.port:
                    print(as_line(c), flush=True)
            pos += done
            with open(cursor_file(a.port), "w") as f:
                f.write(str(pos))
        if time.time() - last_check > 2:
            last_check = time.time()
            if not os.path.exists(pidfile(a.port)):
                print(f"page-comments helper on port {a.port} stopped", flush=True)
                return
        time.sleep(0.5)


def servers():
    d = os.path.join(STATE, "servers")
    out = []
    for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if name.endswith(".json"):
            with open(os.path.join(d, name)) as f:
                st = json.load(f)
            try:
                st["pid"] = int(open(pidfile(st["port"])).read())
                os.kill(st["pid"], 0)
            except (OSError, ValueError):
                st["pid"] = None
            out.append(st)
    return out


def cmd_stop(a):
    found = [s for s in servers() if a.port in (None, s["port"])]
    if not found:
        print("no helper running" + (f" on port {a.port}" if a.port else ""))
    for st in found:
        if st["pid"]:
            os.kill(st["pid"], signal.SIGTERM)
        kind, _, pane = st.get("pane", "").partition(":")
        if kind == "rex":
            subprocess.run([rex_bin(), "block", "close", pane], capture_output=True)
        elif kind == "tmux":
            subprocess.run(["tmux", "kill-pane", "-t", pane], capture_output=True)
        for ext in ("json", "pid", "cursor"):
            try:
                os.remove(os.path.join(STATE, "servers", f"{st['port']}.{ext}"))
            except OSError:
                pass
        print(f"stopped the helper on port {st['port']}")


def cmd_list(_):
    rows = servers()
    if not rows:
        print("no helper running")
    for st in rows:
        print(f"http://localhost:{st['port']}/  {'running' if st['pid'] else 'stopped'}  {st['site']}")


def cmd_inbox(a):
    try:
        lines = open(COMMENTS).read().splitlines()[-a.n:]
    except OSError:
        lines = []
    if not lines:
        print("no comments yet")
    for line in lines:
        c = json.loads(line)
        print(time.strftime("%m-%d %H:%M  ", time.localtime(c["t"])) + as_line(c))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("start", "serve"):
        p = sub.add_parser(name)
        g = p.add_mutually_exclusive_group(required=True)
        g.add_argument("--proxy")
        g.add_argument("--serve")
        p.add_argument("--port", type=int, required=name == "serve")
        p.add_argument("--allow-origin", action="append", default=[])
        if name == "serve":
            p.add_argument("--token", default=secrets.token_urlsafe(18))
            p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("watch")
    p.add_argument("port", type=int)
    p = sub.add_parser("stop")
    p.add_argument("port", nargs="?", type=int)
    sub.add_parser("list")
    p = sub.add_parser("inbox")
    p.add_argument("-n", type=int, default=20)
    a = ap.parse_args()
    {"start": cmd_start, "serve": cmd_serve, "watch": cmd_watch, "stop": cmd_stop,
     "list": cmd_list, "inbox": cmd_inbox}[a.cmd](a)


if __name__ == "__main__":
    main()
