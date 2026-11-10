#
# HTTP file server: browse, upload and download files on /flash and /sd from
# a Mac or phone. See docs/HTTPD.md.
#
#   GET    /                       web page (needs ?token=)
#   GET    /api/list?path=/sd      JSON directory listing ("/" lists the roots)
#   GET    /files/<root>/<path>    download            (root = flash | sd)
#   PUT    /files/<root>/<path>    upload (Content-Length required)
#   DELETE /files/<root>/<path>    delete a file
#   GET    /backup                 encrypted settings backup (X-Passphrase header)
#
# Authentication is off by default. With `httpd auth on` every request needs
# the token: "Authorization: Bearer <token>" or ?token=.
# Like clipd there is no hardware timer: boot.py calls tick(env) from its
# 30 ms loop and a zero-timeout select drives one connection at a time.
#

import json
import os
import select
import socket
import time

import network

CONFIG = "/flash/.http.json"
DEFAULT_PORT = 8080
MAX_HEAD = 2048
CHUNK = 2048
BUDGET_MS = 12        # max time spent moving data per tick
IDLE_MS = 15000
ROOTS = {"flash": "/flash", "sd": "/sd"}

_REASONS = {
    200: "OK", 201: "Created", 400: "Bad Request", 401: "Unauthorized",
    403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
    411: "Length Required", 431: "Request Header Fields Too Large",
    500: "Internal Server Error", 507: "Insufficient Storage",
}

PAGE = b"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>vtOS files</title><style>
:root{--bg:#f5f6f8;--fg:#1c2230;--mu:#6b7385;--ln:#dde1e8;--ac:#2f6fed;--cd:#fff}
@media(prefers-color-scheme:dark){:root{--bg:#14171d;--fg:#e6e9ef;--mu:#8a93a6;--ln:#2a303c;--ac:#6b9bff;--cd:#1c2029}}
*{box-sizing:border-box}body{margin:0;font:14px system-ui,sans-serif;background:var(--bg);color:var(--fg)}
header{display:flex;gap:.6em;align-items:center;flex-wrap:wrap;padding:.7em 1em;background:var(--cd);border-bottom:1px solid var(--ln)}
h1{font-size:15px;margin:0 .6em 0 0}#bc{flex:1;min-width:10em}#bc a{color:var(--ac);cursor:pointer;text-decoration:none}
#bc span{color:var(--mu);margin:0 .3em}button,label.b{font:inherit;padding:.35em .8em;border:1px solid var(--ln);background:var(--cd);color:var(--fg);border-radius:6px;cursor:pointer}
button:hover,label.b:hover{border-color:var(--ac)}main{max-width:56em;margin:1em auto;padding:0 1em}
#dz{background:var(--cd);border:1px solid var(--ln);border-radius:8px;overflow:hidden}
#dz.over{outline:2px dashed var(--ac)}table{width:100%;border-collapse:collapse}
th{text-align:left;font-weight:500;color:var(--mu);font-size:12px;padding:.5em .8em;border-bottom:1px solid var(--ln)}
td{padding:.5em .8em;border-bottom:1px solid var(--ln)}tr:last-child td{border:0}tr:hover td{background:var(--bg)}
td.s,th.s{text-align:right;color:var(--mu);white-space:nowrap}td.a{text-align:right;white-space:nowrap}
td.a a{color:var(--mu);margin-left:.8em;cursor:pointer;text-decoration:none}td.a a:hover{color:var(--ac)}
.n a{color:var(--fg);text-decoration:none;cursor:pointer}.n a:hover{color:var(--ac)}.i{display:inline-block;width:1.6em}
#st{color:var(--mu);padding:.6em .2em;min-height:2em}progress{width:10em;vertical-align:middle}
.e{padding:2em;text-align:center;color:var(--mu)}</style>
<header><h1>vtOS files</h1><div id=bc></div>
<label class=b>Upload<input type=file id=f multiple hidden></label><button id=nf>New folder</button>
<button id=bk>Backup</button></header>
<main><div id=dz><table><thead><tr><th>Name<th class=s>Size<th></thead><tbody id=l></tbody></table></div>
<div id=st></div></main><script>
var Q=new URLSearchParams(location.search).get('token')||'',H=Q?{Authorization:'Bearer '+Q}:{},P='/',$=id=>document.getElementById(id),
E=encodeURIComponent,tk=Q?'?token='+Q:'';
function J(p,o){return fetch(p,Object.assign({headers:H},o))}
function sz(n){return n<1024?n+' B':n<1048576?(n/1024).toFixed(1)+' KB':(n/1048576).toFixed(1)+' MB'}
function say(t){$('st').textContent=t}
function ju(p){return p=='/'?'':p}
function crumbs(){var b=$('bc'),h='<a data-p="/">device</a>',a='',parts=P.split('/').filter(Boolean);
parts.forEach(x=>{a+='/'+x;h+='<span>/</span><a data-p="'+a+'">'+x+'</a>'});b.innerHTML=h;
b.querySelectorAll('a').forEach(a=>a.onclick=()=>ls(a.dataset.p))}
function ls(p){P=p;crumbs();J('/api/list?path='+E(p)).then(r=>r.ok?r.json():Promise.reject(r.status)).then(d=>{
d.sort((a,b)=>b.d-a.d||a.n.localeCompare(b.n));var t=$('l');t.innerHTML='';
if(!d.length)t.innerHTML='<tr><td colspan=3 class=e>Empty folder';
d.forEach(e=>{var q=ju(p)+'/'+e.n,r=t.insertRow();
r.innerHTML='<td class=n><span class=i>'+(e.d?'&#128193;':'&#128196;')+'</span><a></a><td class=s>'+(e.d?'':sz(e.s))+'<td class=a>';
var a=r.querySelector('.n a');a.textContent=e.n;
if(e.d)a.onclick=()=>ls(q);else a.href='/files'+q+tk;
var c=r.querySelector('.a'),m=(t,f)=>{var x=document.createElement('a');x.textContent=t;x.onclick=f;c.appendChild(x)};
if(!e.d)m('Download',()=>location='/files'+q+tk);
m('Rename',()=>{var n=prompt('New name',e.n);if(n&&n!=e.n)J('/api/rename?path='+E(q)+'&to='+E(ju(p)+'/'+n),{method:'POST'}).then(r=>r.ok?ls(p):say('Rename failed ('+r.status+')'))});
m('Delete',()=>{if(confirm('Delete '+e.n+'?'))J('/files'+q,{method:'DELETE'}).then(r=>r.ok?ls(p):say(r.status==400?'Folder not empty':'Delete failed ('+r.status+')'))})});
say(d.length+' item'+(d.length==1?'':'s'))}).catch(e=>say('Could not list: '+e))}
function up(files){if(P=='/')return say('Open flash or sd first');var i=0,next=()=>{if(i>=files.length){say('Uploaded');return ls(P)}
var f=files[i++],x=new XMLHttpRequest();x.open('PUT','/files'+ju(P)+'/'+E(f.name));if(Q)x.setRequestHeader('Authorization','Bearer '+Q);
x.upload.onprogress=e=>say('Uploading '+f.name+' '+Math.round(100*e.loaded/e.total)+'%');
x.onload=()=>x.status<300?next():say('Upload failed ('+x.status+')');x.onerror=()=>say('Upload failed');x.send(f)};next()}
$('f').onchange=e=>{up(e.target.files);e.target.value=''};
$('nf').onclick=()=>{if(P=='/')return say('Open flash or sd first');var n=prompt('Folder name');
if(n)J('/api/mkdir?path='+E(ju(P)+'/'+n),{method:'POST'}).then(r=>r.ok?ls(P):say('Failed ('+r.status+')'))};
$('bk').onclick=()=>{var w=prompt('Backup passphrase');if(!w)return;
J('/backup',{headers:Object.assign({'X-Passphrase':w},H)}).then(r=>r.ok?r.blob():Promise.reject(r.status)).then(b=>{
var a=document.createElement('a');a.href=URL.createObjectURL(b);a.download='vtos-backup.vtbk';a.click()}).catch(e=>say('Backup failed ('+e+')'))};
var dz=$('dz');dz.ondragover=e=>{e.preventDefault();dz.className='over'};dz.ondragleave=()=>dz.className='';
dz.ondrop=e=>{e.preventDefault();dz.className='';up(e.dataTransfer.files)};ls('/')</script>"""


def _new_token():
    return "".join("%02x" % b for b in os.urandom(16))


def load_config():
    cfg = {}
    try:
        with open(CONFIG) as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            cfg = {}
    except (OSError, ValueError):
        cfg = {}
    changed = False
    if not isinstance(cfg.get("token"), str) or len(cfg["token"]) != 32:
        cfg["token"] = _new_token()
        changed = True
    if not isinstance(cfg.get("port"), int):
        cfg["port"] = DEFAULT_PORT
        changed = True
    if not isinstance(cfg.get("auth"), bool):
        cfg["auth"] = False          # off by default; `httpd auth on` requires the token
        changed = True
    if not isinstance(cfg.get("autostart"), bool):
        cfg["autostart"] = False
        changed = True
    if changed:
        save_config(cfg)
    return cfg


def save_config(cfg):
    with open(CONFIG, "w") as f:
        json.dump(cfg, f)


def get_ip():
    sta = network.WLAN(network.STA_IF)
    if sta.active() and sta.isconnected():
        return sta.ifconfig()[0]
    return None


def _same(a, b):
    if len(a) != len(b):
        return False
    diff = 0
    for x, y in zip(a, b):
        diff |= ord(x) ^ ord(y)
    return diff == 0


def unquote(s):
    if "%" not in s:
        return s
    out = bytearray()
    i = 0
    raw = s.encode()
    while i < len(raw):
        c = raw[i]
        if c == 0x25 and i + 2 < len(raw):
            try:
                out.append(int(raw[i + 1:i + 3], 16))
                i += 3
                continue
            except ValueError:
                pass
        out.append(c)
        i += 1
    return out.decode()


def resolve(url_path):
    """"/flash/a/b" -> "/flash/a/b" on the device; None if not allowed.

    Only the flash and sd roots are reachable, and ".." never is."""
    parts = [p for p in unquote(url_path).split("/") if p]
    if not parts or parts[0] not in ROOTS or ".." in parts or "." in parts:
        return None
    return ROOTS[parts[0]] + ("/" + "/".join(parts[1:]) if len(parts) > 1 else "")


def parse_head(buf):
    """None while incomplete; else (method, path, query, headers, body_start),
    or an int HTTP status for a malformed request."""
    end = buf.find(b"\r\n\r\n")
    if end < 0:
        return 431 if len(buf) > MAX_HEAD else None
    if end > MAX_HEAD:
        return 431
    try:
        lines = bytes(buf[:end]).decode().split("\r\n")
    except UnicodeError:
        return 400
    parts = lines[0].split(" ")
    if len(parts) < 2:
        return 400
    path, _, query = parts[1].partition("?")
    headers = {}
    for ln in lines[1:]:
        k, sep, v = ln.partition(":")
        if sep:
            headers[k.strip().lower()] = v.strip()
    return parts[0], path, query, headers, end + 4


def _param(query, name):
    for pair in query.split("&"):
        k, _, v = pair.partition("=")
        if k == name:
            return unquote(v)
    return None


def route(method, path, query, headers, token):
    """Decide what to do with a request: (action, arg...) or ("err", code, msg)."""
    if token is not None:        # None = authentication switched off
        auth = headers.get("authorization", "")
        given = auth[7:] if auth.startswith("Bearer ") else (_param(query, "token") or "")
        if not _same(given, token):
            return ("err", 401, "unauthorized")
    if path == "/" and method == "GET":
        return ("page",)
    if path == "/backup" and method == "GET":
        return ("backup", headers.get("x-passphrase", ""))
    if path == "/api/list" and method == "GET":
        p = _param(query, "path") or "/"
        if p.strip("/") == "":
            return ("roots",)
        real = resolve(p)
        return ("list", real) if real else ("err", 403, "forbidden")
    if path in ("/api/mkdir", "/api/rename") and method == "POST":
        real = resolve(_param(query, "path") or "")
        if real is None or real in ROOTS.values():
            return ("err", 403, "forbidden")
        if path == "/api/mkdir":
            return ("mkdir", real)
        dest = resolve(_param(query, "to") or "")
        if dest is None or dest in ROOTS.values():
            return ("err", 403, "forbidden")
        return ("rename", real, dest)
    if path.startswith("/files/"):
        real = resolve(path[6:])
        if real is None or real in ROOTS.values():
            return ("err", 403, "forbidden")
        if method == "GET":
            return ("get", real)
        if method == "DELETE":
            return ("delete", real)
        if method in ("PUT", "POST"):
            try:
                length = int(headers.get("content-length", ""))
            except ValueError:
                return ("err", 411, "content-length required")
            if length < 0:
                return ("err", 400, "bad content-length")
            return ("put", real, length)
        return ("err", 405, "method not allowed")
    return ("err", 404, "not found")


def head(code, length, ctype="application/json", extra=""):
    return ("HTTP/1.1 %d %s\r\nContent-Type: %s\r\nContent-Length: %d\r\n%sConnection: close\r\n\r\n"
            % (code, _REASONS.get(code, "Error"), ctype, length, extra)).encode()


def response(code, body=b"", ctype="application/json", extra=""):
    if isinstance(body, str):
        body = body.encode()
    return head(code, len(body), ctype, extra) + body


def err_response(code, msg):
    return response(code, json.dumps({"error": msg}))


def list_dir(real):
    out = []
    for name in sorted(os.listdir(real)):
        try:
            st = os.stat(real + "/" + name)
        except OSError:
            continue
        out.append({"n": name, "d": bool(st[0] & 0x4000), "s": st[6]})
    return json.dumps(out)


def _mkdirs(path):
    cur = ""
    for seg in [p for p in path.split("/") if p][:-1]:
        cur += "/" + seg
        try:
            os.stat(cur)
        except OSError:
            try:
                os.mkdir(cur)
            except OSError:
                pass


class _Conn:
    def __init__(self, sock, now):
        self.sock = sock
        self.buf = bytearray()
        self.out = b""         # bytes waiting to be sent
        self.src = None        # file being downloaded
        self.dst = None        # file being uploaded
        self.dst_path = None
        self.remaining = 0
        self.last = now
        self.continued = False
        self.done = False      # drop once self.out and self.src are drained


class HttpServer:
    def __init__(self, env, cfg):
        self.env = env
        self.port = cfg["port"]
        self.token = cfg["token"]
        self.auth = cfg.get("auth", False)
        self.sock = None
        self.conn = None
        self.next_bind = 0

    @property
    def listening(self):
        return self.sock is not None

    def _bind(self):
        if get_ip() is None:
            return
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            except (OSError, AttributeError):
                pass
            s.bind(("0.0.0.0", self.port))
            s.listen(2)
            s.setblocking(False)
        except OSError:
            s.close()
            return
        self.sock = s

    def _drop(self):
        c = self.conn
        if c is None:
            return
        for f in (c.src, c.dst):
            if f is not None:
                try:
                    f.close()
                except OSError:
                    pass
        if c.dst_path is not None:
            try:
                os.remove(c.dst_path + ".part")   # aborted upload
            except OSError:
                pass
        try:
            c.sock.close()
        except OSError:
            pass
        self.conn = None

    def close(self):
        self._drop()
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    # -- request handling -------------------------------------------------

    def _reply(self, c, code, msg):
        c.out = err_response(code, msg)
        c.done = True

    def _start(self, c, parsed):
        method, path, query, headers, body_start = parsed
        act = route(method, path, query, headers, self.token if self.auth else None)
        kind = act[0]
        try:
            if kind == "err":
                self._reply(c, act[1], act[2])
            elif kind == "page":
                c.out = response(200, PAGE, "text/html")
                c.done = True
            elif kind == "roots":
                c.out = response(200, json.dumps(
                    [{"n": n, "d": True, "s": 0} for n in ROOTS if n != "sd" or self._has("/sd")]))
                c.done = True
            elif kind == "list":
                c.out = response(200, list_dir(act[1]))
                c.done = True
            elif kind == "get":
                st = os.stat(act[1])
                if st[0] & 0x4000:
                    return self._reply(c, 400, "is a directory")
                c.src = open(act[1], "rb")
                name = act[1].rsplit("/", 1)[-1]
                c.out = head(200, st[6], "application/octet-stream",
                             'Content-Disposition: attachment; filename="%s"\r\n' % name)
                c.done = True
            elif kind == "delete":
                if os.stat(act[1])[0] & 0x4000:
                    try:
                        os.rmdir(act[1])
                    except OSError:
                        return self._reply(c, 400, "folder not empty")
                else:
                    os.remove(act[1])
                c.out = response(200, '{"ok": true}')
                c.done = True
            elif kind == "mkdir":
                os.mkdir(act[1])
                c.out = response(201, '{"ok": true}')
                c.done = True
            elif kind == "rename":
                if self._has(act[2]):
                    return self._reply(c, 400, "already exists")
                os.rename(act[1], act[2])
                c.out = response(200, '{"ok": true}')
                c.done = True
            elif kind == "backup":
                import backup
                try:
                    blob = backup.export_bytes(act[1])
                except backup.BackupError as e:
                    return self._reply(c, 400, str(e))
                c.out = response(200, blob, "application/octet-stream",
                                 'Content-Disposition: attachment; filename="vtos-backup.vtbk"\r\n')
                c.done = True
            elif kind == "put":
                path_, length = act[1], act[2]
                _mkdirs(path_)
                c.dst = open(path_ + ".part", "wb")
                c.dst_path = path_
                c.remaining = length
                self._write_body(c, bytes(c.buf[body_start:]))
        except OSError as e:
            if e.args and e.args[0] == 2:
                self._reply(c, 404, "not found")
            elif e.args and e.args[0] in (28, 12):
                self._reply(c, 507, "no space")
            else:
                self._reply(c, 500, "io error")

    @staticmethod
    def _has(path):
        try:
            os.stat(path)
            return True
        except OSError:
            return False

    def _write_body(self, c, data):
        if len(data) > c.remaining:
            data = data[:c.remaining]
        c.dst.write(data)
        c.remaining -= len(data)
        if c.remaining == 0:
            c.dst.close()
            c.dst = None
            os.rename(c.dst_path + ".part", c.dst_path)
            c.dst_path = None
            c.out = response(201, '{"ok": true}')
            c.done = True

    def _send(self, c):
        """Push pending bytes, then file chunks, within the time budget."""
        start = time.ticks_ms()
        while True:
            if c.out:
                try:
                    n = c.sock.send(c.out)
                except OSError as e:
                    if e.args and e.args[0] in (11, 119):    # EAGAIN: try next tick
                        return True
                    return False
                if not n:
                    return True
                c.out = c.out[n:]
                c.last = time.ticks_ms()
                if c.out:
                    return True
            elif c.src is not None:
                data = c.src.read(CHUNK)
                if not data:
                    c.src.close()
                    c.src = None
                    return False
                c.out = data
            else:
                return False   # all sent
            if time.ticks_diff(time.ticks_ms(), start) >= BUDGET_MS:
                return True

    def poll(self):
        now = time.ticks_ms()
        if self.sock is None:
            if time.ticks_diff(now, self.next_bind) >= 0:
                self.next_bind = time.ticks_add(now, 1000)
                self._bind()
            return

        c = self.conn
        if c is None:
            try:
                ready, _, _ = select.select([self.sock], [], [], 0)
            except OSError:
                self.close()
                return
            if self.sock in ready:
                try:
                    cs, _ = self.sock.accept()
                except OSError:
                    return
                cs.setblocking(False)
                self.conn = _Conn(cs, now)
            return

        if time.ticks_diff(now, c.last) >= IDLE_MS:
            self._drop()
            return

        if c.done:
            if not self._send(c):
                self._drop()
            return

        start = now
        while self.conn is c and not c.done:
            try:
                ready, _, _ = select.select([c.sock], [], [], 0)
            except OSError:
                self._drop()
                return
            if c.sock not in ready:
                return
            try:
                data = c.sock.recv(CHUNK)
            except OSError:
                return
            if not data:
                self._drop()
                return
            c.last = time.ticks_ms()
            try:
                if c.dst is not None:
                    self._write_body(c, data)
                else:
                    c.buf += data
                    parsed = parse_head(c.buf)
                    if isinstance(parsed, int):
                        self._reply(c, parsed, "bad request")
                    elif parsed is not None:
                        if (not c.continued
                                and parsed[3].get("expect", "").lower() == "100-continue"
                                and len(c.buf) == parsed[4]):
                            c.continued = True
                            c.sock.send(b"HTTP/1.1 100 Continue\r\n\r\n")
                        self._start(c, parsed)
                        c.buf = bytearray()
            except OSError as e:
                if e.args and e.args[0] in (28, 12):
                    self._reply(c, 507, "no space")
                else:
                    self._drop()
                    return
            if time.ticks_diff(time.ticks_ms(), start) >= BUDGET_MS:
                return
        if c.done and self.conn is c:
            if not self._send(c):
                self._drop()


def start(env, port=None):
    if env.httpd is not None:
        return env.httpd
    cfg = load_config()
    if port is not None:
        cfg["port"] = port
        save_config(cfg)
    env.httpd = HttpServer(env, cfg)
    return env.httpd


def stop(env):
    if env.httpd is not None:
        env.httpd.close()
        env.httpd = None


def tick(env):
    s = env.httpd
    if s is not None:
        try:
            s.poll()
        except Exception:
            pass
