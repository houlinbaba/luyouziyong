#!/usr/bin/env python3
"""NeoServer-style card panel, v2 (2026-10-27, rebuilt from NeoServer 7.9.1 analysis).

Changes vs v1 (which died on .1 with "Incompatible ssh peer"):
- NO paramiko at all. v1 used paramiko, whose key list has no plain
  ssh-rsa, so it could not handshake with Xiaomi dropbear (ssh-rsa only).
  v2 polls and opens terminals through the system OpenSSH client
  (ssh + sshpass), with HostKeyAlgorithms=+ssh-rsa, exactly the kind
  of client NeoServer's libssh2 is: it accepts legacy ssh-rsa.
  Needs: apt install sshpass (system ssh is already on Debian).
- Temperature: NeoServer's fallback chain, not thermal_zone0 only:
  hwmon0 -> thermal zones -> /proc/dmu/temperature (Xiaomi) -> hwmon*.
  This is why NeoServer shows a router temperature where v1 showed blank.
- Extra card info from NeoServer's own reads: CPU model (cpuinfo),
  gateway interface (/proc/net/route, dest 00000000), OS (os-release
  PRETTY_NAME + uname). Disk still df -k /, network still net/dev.

Design (unchanged):
- No agent on the monitored device. Local host is read from /proc.
  Routers are polled over SSH (one command per poll, ash/busybox
  compatible: cat /proc/*, df, no bash, no python on the router).
- Terminal per host: local pty for the VPS itself, SSH pty for routers.
- Login: PANEL_PASSWORD env var. If unset, the panel still runs but
  prints a loud warning; do NOT expose an unset-password panel to the
  public internet. Set PANEL_PASSWORD before starting on the VPS.

Hosts are configured in hosts.json next to this file (see
hosts.example.json). Passwords are NOT hardcoded here: put them in
hosts.json on the VPS only, or use key_file. hosts.json is git-ignored
by convention - never send it to anyone.

Run on VPS (host, not docker, so local /proc and local terminal are
the VPS itself):
  apt install sshpass; pip install -r requirements.txt
  PANEL_PASSWORD='...' python3 app.py   # listens 0.0.0.0:8080
"""
import json, os, re, secrets, subprocess, sys, threading, time
from pathlib import Path

from flask import Flask, jsonify, request, session, redirect

try:
    from flask_sock import Sock
except Exception:  # flask_sock missing -> terminal disabled, cards still work
    Sock = None

BASE = Path(__file__).resolve().parent
app = Flask(__name__)
app.secret_key = os.environ.get("PANEL_SECRET", secrets.token_hex(16))
sock = Sock(app) if Sock else None
PASSWORD = os.environ.get("PANEL_PASSWORD", "")
PORT = int(os.environ.get("PANEL_PORT", "8080"))

# ---------------- hosts ----------------
def load_hosts():
    p = BASE / "hosts.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception as e:
            print(f"[panel] hosts.json parse error: {e}", file=sys.stderr)
    return [{"id": "vps", "name": "甲骨文", "type": "local"}]

HOSTS = load_hosts()
HOST_MAP = {h["id"]: h for h in HOSTS}

# ---------------- parsing ----------------
def parse_proc_blob(text):
    """Parse one combined /proc dump. Returns dict of raw values."""
    out = {}
    sections = re.split(r"^--- (\w+) ---\s*$", text, flags=re.M)
    # sections: [pre, name, body, name, body, ...]
    d = {}
    for i in range(1, len(sections) - 1, 2):
        d[sections[i]] = sections[i + 1]
    if "stat" in d:
        m = re.search(r"^cpu\s+([\d\s]+)$", d["stat"], flags=re.M)
        if m:
            vals = [int(x) for x in m.group(1).split()]
            out["cpu_total"] = sum(vals)
            out["cpu_idle"] = vals[3] + (vals[4] if len(vals) > 4 else 0)
    if "meminfo" in d:
        mi = {}
        for line in d["meminfo"].splitlines():
            m = re.match(r"(\w+):\s+(\d+)", line)
            if m:
                mi[m.group(1)] = int(m.group(2))  # kB
        if "MemTotal" in mi and "MemAvailable" in mi:
            out["mem_total"] = mi["MemTotal"] * 1024
            out["mem_used"] = (mi["MemTotal"] - mi["MemAvailable"]) * 1024
    if "uptime" in d:
        m = re.search(r"([\d.]+)\s+([\d.]+)", d["uptime"])
        if m:
            out["uptime"] = float(m.group(1))
    if "loadavg" in d:
        m = re.search(r"([\d.]+)\s+([\d.]+)\s+([\d.]+)", d["loadavg"])
        if m:
            out["load"] = float(m.group(1))
    if "cores" in d:
        m = re.search(r"(\d+)", d["cores"])
        if m:
            out["cores"] = int(m.group(1))
    if "temp" in d:
        m = re.search(r"(\d+)", d["temp"])
        if m:
            v = int(m.group(1))
            # hwmon/thermal give millidegrees (45000 -> 45.0); dmu gives
            # degrees directly (45 -> 45). >200 must be millidegrees.
            out["temp"] = round(v / 1000, 1) if v > 200 else float(v)
    if "cpumodel" in d:
        m = re.search(r":\s*(.+)", d["cpumodel"])
        if m:
            out["cpu_model"] = m.group(1).strip()[:80]
    if "gateway" in d:
        g = d["gateway"].strip().splitlines()
        if g and g[0].strip():
            out["gateway"] = g[0].strip()[:32]
    if "osinfo" in d:
        m = re.search(r'PRETTY_NAME="([^"]+)"', d["osinfo"])
        out["os_info"] = m.group(1)[:80] if m else d["osinfo"].strip().splitlines()[0][:80] if d["osinfo"].strip() else None
    if "df" in d:
        # df -k: Filesystem 1K-blocks Used Available Use% Mounted
        for line in d["df"].splitlines():
            parts = line.split()
            if len(parts) >= 6 and parts[-1] == "/":
                try:
                    out["disk_total"] = int(parts[1]) * 1024
                    out["disk_used"] = int(parts[2]) * 1024
                except ValueError:
                    pass
                break
    if "netdev" in d:
        rx = tx = 0
        for line in d["netdev"].splitlines():
            if ":" in line and not line.strip().startswith("Inter-") and not line.strip().startswith("face"):
                fields = line.split(":")[1].split()
                if len(fields) >= 9:
                    try:
                        rx += int(fields[0]); tx += int(fields[8])
                    except ValueError:
                        pass
        out["net_rx"] = rx; out["net_tx"] = tx
    if "diskstats" in d:
        # fields: reads completed, sectors read, writes completed, sectors written
        sr = sw = 0
        for line in d["diskstats"].splitlines():
            f = line.split()
            if len(f) >= 14:
                try:
                    sr += int(f[5]) * 512; sw += int(f[9]) * 512
                except ValueError:
                    pass
        out["io_read"] = sr; out["io_write"] = sw
    return out

LOCAL_CMD = ("echo '--- stat ---'; cat /proc/stat; echo '--- meminfo ---'; cat /proc/meminfo; "
             "echo '--- uptime ---'; cat /proc/uptime; echo '--- loadavg ---'; cat /proc/loadavg; "
             "echo '--- df ---'; df -k / 2>/dev/null; df -k 2>/dev/null; echo '--- netdev ---'; cat /proc/net/dev; "
             "echo '--- diskstats ---'; cat /proc/diskstats; "
             "echo '--- cores ---'; grep -c ^processor /proc/cpuinfo 2>/dev/null; "
             "echo '--- cpumodel ---'; cat /proc/cpuinfo | grep -E '(model name)|(cpu model)' | head -n 1; "
             "echo '--- gateway ---'; cat /proc/net/route | awk '$2 == \"00000000\" {print $1}' | head -n 1; "
             "echo '--- osinfo ---'; cat /etc/os-release 2>/dev/null | grep PRETTY_NAME; uname -a 2>/dev/null; "
             "echo '--- temp ---'; cat /sys/class/hwmon/hwmon0/temp1_input 2>/dev/null || "
             "cat /sys/class/hwmon/hwmon0/temp*input 2>/dev/null || "
             "cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null || "
             "cat /sys/class/thermal/thermal_*/temp 2>/dev/null || "
             "grep -oE '[0-9]+' /proc/dmu/temperature 2>/dev/null | head -n 1 || "
             "cat /sys/class/hwmon/hwmon*/temp*_input 2>/dev/null | head -n 1")
# Same command works over SSH on Xiaomi official firmware (ash + busybox).
SSH_CMD = LOCAL_CMD

def local_sample():
    try:
        r = subprocess.run(["sh", "-c", LOCAL_CMD], capture_output=True, text=True, timeout=10)
        return parse_proc_blob(r.stdout)
    except Exception as e:
        return {"error": str(e)}

# System OpenSSH, not paramiko: OpenSSH with +ssh-rsa accepts the
# dropbear host key that paramiko 5 refuses ("no acceptable host key").
SSH_BASE_OPTS = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                 "-o", "HostKeyAlgorithms=+ssh-rsa", "-o", "PubkeyAcceptedKeyTypes=+ssh-rsa",
                 "-o", "ConnectTimeout=8", "-o", "LogLevel=ERROR"]

def ssh_cmd_prefix(host):
    """Return (argv_prefix, env_extra) for a system-ssh invocation."""
    target = f"{host.get('username', 'root')}@{host.get('host', '127.0.0.1')}"
    args = ["ssh"] + SSH_BASE_OPTS + ["-p", str(int(host.get("port", 22)))]
    if host.get("key_file"):
        args += ["-i", host["key_file"]]
    args.append(target)
    if host.get("key_file") or not host.get("password"):
        return args, {}
    # sshpass -e reads the password from SSHPASS env, so it never
    # appears in the process list (unlike sshpass -p).
    return ["sshpass", "-e"] + args, {"SSHPASS": host.get("password", "")}

def ssh_sample(host):
    args, env_extra = ssh_cmd_prefix(host)
    env = dict(os.environ); env.update(env_extra)
    r = subprocess.run(args + [SSH_CMD], capture_output=True, text=True, timeout=15, env=env)
    if r.returncode != 0 and not r.stdout.strip():
        raise RuntimeError((r.stderr or "ssh 失败").strip()[:120])
    return parse_proc_blob(r.stdout)

# Cache previous sample per host for rate calculation
_prev = {}
_lock = threading.Lock()

def fmt_bytes(n):
    if n is None: return "-"
    for u in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024 or u == "T":
            return f"{n:.1f} {u}" if u != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} T"

def fmt_uptime(s):
    if s is None: return "-"
    d = int(s // 86400); h = int((s % 86400) // 3600)
    return f"{d}d {h}h" if d else f"{h}h"

def sample_host(h):
    hid = h["id"]
    now = time.time()
    raw = local_sample() if h["type"] == "local" else None
    if raw is None:
        try:
            raw = ssh_sample(h)
        except Exception as e:
            return {"id": hid, "name": h["name"], "online": False, "error": str(e)[:120]}
    if "error" in raw:
        return {"id": hid, "name": h["name"], "online": False, "error": raw["error"][:120]}
    with _lock:
        prev = _prev.get(hid)
        _prev[hid] = (now, raw)
    cpu_pct = mem_pct = disk_pct = None
    net_up = net_down = io_up = io_down = 0
    if raw.get("mem_total"):
        mem_pct = round(100 * raw["mem_used"] / raw["mem_total"], 1)
    if raw.get("disk_total"):
        disk_pct = round(100 * raw["disk_used"] / raw["disk_total"], 1)
    if prev:
        pt, praw = prev
        dt = max(now - pt, 0.5)
        if "cpu_total" in raw and "cpu_total" in praw:
            dtot = raw["cpu_total"] - praw["cpu_total"]
            didle = raw["cpu_idle"] - praw["cpu_idle"]
            if dtot > 0:
                cpu_pct = round(100 * (1 - didle / dtot), 1)
        if "net_rx" in raw and "net_rx" in praw:
            net_down = (raw["net_rx"] - praw["net_rx"]) / dt
            net_up = (raw["net_tx"] - praw["net_tx"]) / dt
        if "io_read" in raw and "io_read" in praw:
            io_down = (raw["io_read"] - praw["io_read"]) / dt
            io_up = (raw["io_write"] - praw["io_write"]) / dt
    return {
        "id": hid, "name": h["name"], "online": True,
        "cpu_pct": cpu_pct, "cpu_cores": raw.get("cores") or (os.cpu_count() if h["type"] == "local" else None),
        "cpu_model": raw.get("cpu_model"), "gateway": raw.get("gateway"), "os_info": raw.get("os_info"),
        "temp": raw.get("temp"),
        "mem_pct": mem_pct, "mem_used": raw.get("mem_used"), "mem_total": raw.get("mem_total"),
        "disk_pct": disk_pct, "disk_used": raw.get("disk_used"), "disk_total": raw.get("disk_total"),
        "net_up": net_up, "net_down": net_down,
        "net_tx_total": raw.get("net_tx"), "net_rx_total": raw.get("net_rx"),
        "io_up": io_up, "io_down": io_down,
        "io_write_total": raw.get("io_write"), "io_read_total": raw.get("io_read"),
        "uptime": fmt_uptime(raw.get("uptime")), "load": raw.get("load"),
    }

# ---------------- web ----------------
@app.route("/")
def index():
    if PASSWORD and not session.get("ok"):
        return redirect("/login")
    return (BASE / "index.html").read_text()

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if not PASSWORD or request.form.get("password") == PASSWORD:
            session["ok"] = True
            return redirect("/")
        return "<p>密码不对</p><a href='/login'>重试</a>", 401
    return """<form method=post style="font-family:sans-serif;margin:80px auto;width:280px">
      <h3>路由器面板登录</h3><input name=password type=password placeholder=面板密码 style="width:100%;padding:8px">
      <button style="margin-top:8px;padding:8px 16px">登录</button></form>"""

@app.route("/api/hosts")
def api_hosts():
    if PASSWORD and not session.get("ok"):
        return jsonify({"error": "unauthorized"}), 401
    return jsonify([{"id": h["id"], "name": h["name"], "type": h["type"], "remark": h.get("remark", "")} for h in HOSTS])

@app.route("/api/hosts/add", methods=["POST"])
def api_add_host():
    if PASSWORD and not session.get("ok"):
        return jsonify({"error": "unauthorized"}), 401
    d = request.get_json(force=True, silent=True) or {}
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"error": "请填主机名"}), 400
    # Chinese names strip to nothing/digits only (e.g. "主路由 .1" -> "-1"),
    # so fall back to a timestamp id instead of a meaningless "-1".
    hid = re.sub(r"[^a-z0-9_-]", "", name.lower().replace(" ", "-")).strip("-")
    if not hid or hid.isdigit():
        hid = f"h{int(time.time())}"
    if hid in HOST_MAP:
        hid = f"{hid}-{int(time.time()) % 10000}"
    h = {"id": hid, "name": name, "type": d.get("type") or "ssh",
         "host": d.get("host") or "127.0.0.1", "port": int(d.get("port") or 22),
         "username": d.get("username") or "root", "password": d.get("password") or ""}
    if h["type"] == "local":
        h.pop("host", None); h.pop("port", None); h.pop("username", None); h.pop("password", None)
    HOSTS.append(h); HOST_MAP[hid] = h
    try:
        (BASE / "hosts.json").write_text(json.dumps(HOSTS, ensure_ascii=False, indent=2))
    except Exception as e:
        return jsonify({"error": f"保存 hosts.json 失败: {e}"}), 500
    return jsonify({"ok": True, "id": hid})

@app.route("/api/hosts/<hid>", methods=["DELETE"])
def api_delete_host(hid):
    if PASSWORD and not session.get("ok"):
        return jsonify({"error": "unauthorized"}), 401
    h = HOST_MAP.get(hid)
    if not h:
        return jsonify({"error": "not found"}), 404
    if h["type"] == "local":
        return jsonify({"error": "甲骨文本机不能删"}), 400
    HOSTS[:] = [x for x in HOSTS if x["id"] != hid]
    HOST_MAP.pop(hid, None)
    try:
        (BASE / "hosts.json").write_text(json.dumps(HOSTS, ensure_ascii=False, indent=2))
    except Exception as e:
        return jsonify({"error": f"保存 hosts.json 失败: {e}"}), 500
    return jsonify({"ok": True})

@app.route("/api/hosts/<hid>", methods=["PATCH"])
def api_patch_host(hid):
    if PASSWORD and not session.get("ok"):
        return jsonify({"error": "unauthorized"}), 401
    h = HOST_MAP.get(hid)
    if not h:
        return jsonify({"error": "not found"}), 404
    d = request.get_json(force=True, silent=True) or {}
    if "remark" in d:
        h["remark"] = str(d["remark"])[:200]
    if "name" in d and str(d["name"]).strip():
        h["name"] = str(d["name"]).strip()[:100]
    try:
        (BASE / "hosts.json").write_text(json.dumps(HOSTS, ensure_ascii=False, indent=2))
    except Exception as e:
        return jsonify({"error": f"保存 hosts.json 失败: {e}"}), 500
    return jsonify({"ok": True, "host": h})

@app.route("/api/metrics/<hid>")
def api_metrics(hid):
    if PASSWORD and not session.get("ok"):
        return jsonify({"error": "unauthorized"}), 401
    h = HOST_MAP.get(hid)
    if not h:
        return jsonify({"error": "not found"}), 404
    return jsonify(sample_host(h))

# ---------------- terminal ----------------
if sock:
    @sock.route("/ws/terminal/<hid>")
    def ws_terminal(ws, hid):
        if PASSWORD and not session.get("ok"):
            ws.close(); return
        h = HOST_MAP.get(hid)
        if not h:
            ws.close(); return
        import pty, select
        master = None; proc = None
        try:
            master, slave = pty.openpty()
            if h["type"] == "local":
                proc = subprocess.Popen(["bash", "--login"], stdin=slave, stdout=slave, stderr=slave, close_fds=True)
                env = None
            else:
                # System ssh over a pty (-tt): same +ssh-rsa acceptance as
                # polling, so the terminal works on .1 where paramiko died.
                args, env_extra = ssh_cmd_prefix(h)
                env = dict(os.environ); env.update(env_extra)
                proc = subprocess.Popen(args + ["-tt"], stdin=slave, stdout=slave, stderr=slave, close_fds=True, env=env)
            os.close(slave)
            while True:
                r, _, _ = select.select([master], [], [], 0.1)
                if r:
                    try: data = os.read(master, 65536)
                    except OSError: break
                    if not data: break
                    ws.send(data.decode(errors="ignore"))
                try:
                    msg = ws.receive(timeout=0.05)
                except Exception:
                    msg = None
                if msg is not None:
                    os.write(master, msg.encode())
                if proc is not None and proc.poll() is not None: break
        except Exception as e:
            try: ws.send(f"\r\n[终端断开: {e}]\r\n")
            except Exception: pass
        finally:
            try:
                if master is not None: os.close(master)
                if proc is not None: proc.terminate()
            except Exception: pass

if __name__ == "__main__":
    if not PASSWORD:
        print("[panel] WARNING: PANEL_PASSWORD 未设置，面板无登录保护！不要把它暴露到公网。")
    else:
        print("[panel] 登录保护已启用。")
    print(f"[panel] hosts: {[h['name'] for h in HOSTS]}  listening 0.0.0.0:{PORT}")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
