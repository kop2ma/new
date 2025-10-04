#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import socket
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import pytz
from flask import Flask, render_template_string, request

# === CONFIG (from environment variables) ===
# On Railway set these in project > Variables:
#   MINER_IP     - the shared IP for all miners (no IP in code)
#   MINER_USER   - username (if your protocol needs it)
#   MINER_PASS   - password (if your protocol needs it)
# Optional:
#   CACHE_INTERVAL_SECONDS - seconds between auto-refresh (default 3600)
# Railway will provide PORT variable for web binding.
MINER_IP = os.environ.get("MINER_IP")         # required to be provided by Railway env
MINER_USER = os.environ.get("MINER_USER", "")
MINER_PASS = os.environ.get("MINER_PASS", "")
CACHE_INTERVAL = int(os.environ.get("CACHE_INTERVAL_SECONDS", 60 * 60))

# MINER names & ports: ports remain hardcoded as requested
MINER_NAMES = ["131", "132", "133", "65", "66", "70"]
MINER_PORTS = [204, 205, 206, 304, 305, 306]

# Build MINERS list at runtime from MINER_IP and the fixed ports
def build_miners():
    ip = MINER_IP
    miners = []
    for name, port in zip(MINER_NAMES, MINER_PORTS):
        miners.append({"name": name, "ip": ip, "port": port})
    return miners

# Fallback if MINER_IP not set: keep empty list but UI will show warning
MINERS = build_miners() if MINER_IP else []

# Other config
SOCKET_TIMEOUT = 3.0
MAX_WORKERS = 6
COMMANDS = [{"command": "summary"}, {"command": "devs"}]

# === TCP JSON sender ===
def send_tcp_json(ip, port, payload):
    if not ip:
        return None
    data = json.dumps(payload).encode("utf-8")
    try:
        with socket.create_connection((ip, port), timeout=SOCKET_TIMEOUT) as s:
            s.settimeout(SOCKET_TIMEOUT)
            s.sendall(data)
            chunks = []
            while True:
                try:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
                except socket.timeout:
                    break
            raw = b"".join(chunks).decode("utf-8", errors="ignore").strip()
            if not raw:
                return None
            try:
                return json.loads(raw)
            except Exception:
                first = raw.find("{")
                last = raw.rfind("}")
                if first != -1 and last != -1 and last > first:
                    sub = raw[first:last+1]
                    try:
                        return json.loads(sub)
                    except Exception:
                        return None
            return None
    except Exception:
        return None

# === Helpers ===
def format_seconds_pretty(sec: int):
    days, rem = divmod(sec, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if not parts and seconds:
        parts.append(f"{seconds}s")
    return " ".join(parts)

def parse_summary(summary_json):
    if not summary_json:
        return {}
    data = None
    if "SUMMARY" in summary_json and summary_json["SUMMARY"]:
        data = summary_json["SUMMARY"][0]
    elif "Msg" in summary_json:
        data = summary_json["Msg"]
    else:
        return {}
    if not data:
        return {}
    mhs_av = data.get("MHS av")
    uptime = data.get("Uptime") or data.get("Elapsed")
    power = data.get("Power")
    temp = data.get("Temperature")
    hashrate = None
    if mhs_av is not None:
        # same heuristic as original: if huge, convert to TH/s
        if mhs_av > 1_000_000:
            hashrate = round(mhs_av / 1_000_000, 2)
        else:
            hashrate = mhs_av
    uptime_str = format_seconds_pretty(int(uptime)) if uptime else None
    return {
        "uptime": uptime_str,
        "hashrate": hashrate,
        "power": int(power) if power else None,
        "temp_avg": round(temp, 1) if temp else None,
    }

def parse_devs(devs_json):
    board_temps = []
    if not devs_json or "DEVS" not in devs_json:
        return board_temps
    for board in devs_json["DEVS"]:
        temp = board.get("Temperature")
        if temp is not None:
            board_temps.append(round(temp, 1))
    return board_temps

def poll_miner(miner):
    ip = miner["ip"]
    port = miner["port"]
    result = {
        "name": f"{miner['name']} ({port})",
        "alive": False,
        "hashrate": None,
        "uptime": None,
        "power": None,
        "board_temps": [],
    }
    if not ip:
        return result
    responses = {}
    any_response = False
    for cmd in COMMANDS:
        resp = send_tcp_json(ip, port, cmd)
        if resp:
            any_response = True
            responses[cmd["command"]] = resp
    if not any_response:
        return result
    result["alive"] = True
    if "summary" in responses:
        summary = parse_summary(responses["summary"])
        result.update(
            {
                "hashrate": summary.get("hashrate"),
                "uptime": summary.get("uptime"),
                "power": summary.get("power"),
            }
        )
    if "devs" in responses:
        boards = parse_devs(responses["devs"])
        result["board_temps"] = boards
    return result

# === Cache & refresher ===
CACHE = {"miners": [], "last_update": None, "next_update": None}
CACHE_LOCK = threading.Lock()

def calculate_total_hashrate(miners):
    total = 0
    for miner in miners:
        if miner.get("alive") and miner.get("hashrate") is not None:
            total += miner["hashrate"]
    return round(total, 2)

def refresh_all():
    global MINERS
    # rebuild miners in case MINER_IP changed in env at runtime
    MINERS = build_miners() if MINER_IP else []
    out = []
    if not MINERS:
        with CACHE_LOCK:
            CACHE["miners"] = []
            CACHE["last_update"] = None
            CACHE["next_update"] = None
        return
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(poll_miner, m): m for m in MINERS}
        for fut in futures:
            try:
                res = fut.result()
            except Exception:
                res = {"name": f"{futures[fut]['name']} ({futures[fut]['port']})", "alive": False}
            out.append(res)
    with CACHE_LOCK:
        CACHE["miners"] = sorted(out, key=lambda x: x["name"])
        tz = pytz.timezone("Asia/Tehran")
        now = datetime.now(tz)
        CACHE["last_update"] = now.strftime("%Y-%m-%d %H:%M:%S")
        next_update = now.timestamp() + CACHE_INTERVAL
        CACHE["next_update"] = next_update

# initial refresh (if IP present)
if MINER_IP:
    try:
        refresh_all()
    except Exception:
        pass

def periodic_refresher(interval=CACHE_INTERVAL):
    while True:
        try:
            refresh_all()
        except Exception:
            pass
        time.sleep(interval)

if MINER_IP:
    t = threading.Thread(target=periodic_refresher, daemon=True)
    t.start()

# === Flask UI ===
app = Flask(__name__)

TEMPLATE = """
<!doctype html>
<html lang="en" dir="ltr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Miner Panel</title>
<style>
body{font-family:sans-serif; background:#f0f4f8; color:#0f172a; padding:5px; margin:5px;}
.card{background:white;border-radius:12px;padding:10px;margin-bottom:10px;box-shadow:0 4px 16px rgba(0,0,0,0.08);}
table{width:100%;border-collapse:collapse;margin-top:10px;}
th,td{padding:6px 4px;text-align:center;font-size:18px;}
th{background:#e0e7ff;color:#1e40af;}
tr:nth-child(even){background:#f8fafc;}
.status-online{color:#10b981; font-weight:600; font-size:12px; display:block;}
.status-offline{color:#dc2626; font-weight:600; font-size:12px; display:block;}
.button{padding:8px 16px;background:#2563eb;color:white;border:none;border-radius:8px;cursor:pointer;font-weight:600;font-size:16px;}
.button:hover{background:#1e40af;}
.temp-low{color:#10b981; font-weight:bold;}
.temp-high{color:#dc2626; font-weight:bold;}
.temp-container{display:flex; justify-content:center; gap:8px; flex-wrap:wrap;}
.countdown{font-size:14px;color:#64748b;margin-top:5px;}
.total-hashrate{background:#e0e7ff; padding:8px 16px; border-radius:8px; font-weight:bold; font-size:16px; color:#1e40af;}
.control-row{display:flex; justify-content:space-between; align-items:center; margin-bottom:15px; gap:15px;}
.control-left{display:flex; align-items:center; gap:15px;}
@media(max-width:600px){th,td{font-size:16px;padding:8px;}}
</style>
<script>
function updateCountdown() {
    const nextUpdateTime = {{ next_update_timestamp }} * 1000;
    const countdownElement = document.getElementById('countdown');
    
    function update() {
        const now = new Date().getTime();
        const distance = nextUpdateTime - now;
        
        if (distance < 0) {
            countdownElement.innerHTML = "Updating...";
            location.reload();
            return;
        }
        
        const hours = Math.floor(distance / (1000 * 60 * 60));
        const minutes = Math.floor((distance % (1000 * 60 * 60)) / (1000 * 60));
        const seconds = Math.floor((distance % (1000 * 60)) / 1000);
        
        countdownElement.innerHTML = `Next update in: ${hours}h ${minutes}m ${seconds}s`;
    }
    
    update();
    const countdownInterval = setInterval(update, 1000);
}

document.addEventListener('DOMContentLoaded', updateCountdown);
</script>
</head>
<body>
<div class="card">
<p style="font-size:16px;color:#64748b;">Last Update: {{ last_update }}</p>
<div id="countdown" class="countdown">Next update in: --</div>

<div class="control-row">
    <div class="control-left">
        <form method="POST" action="/">
            <button type="submit" class="button">Manual Update</button>
        </form>
        <div class="total-hashrate">
            Total Hashrate: {{ total_hashrate }} TH/s
        </div>
    </div>
</div>

<table>
<thead>
<tr>
<th>Summary</th>
<th>Uptime</th>
<th>Board Temp (°C)</th>
<th>Hashrate</th>
<th>Power (W)</th>
</tr>
</thead>
<tbody>
{% for m in miners %}
<tr>
<td>
{{ m.name }}
{% if m.alive %}
<span class="status-online">Online</span>
{% else %}
<span class="status-offline">Offline</span>
{% endif %}
</td>
<td>{{ m.uptime or "-" }}</td>
<td>
{% if m.board_temps %}
<div class="temp-container">
  {% for temp in m.board_temps %}
    {% if temp < 60 %}
      <span class="temp-low">{{ temp }}</span>
    {% else %}
      <span class="temp-high">{{ temp }}</span>
    {% endif %}
  {% endfor %}
</div>
{% else %}
-
{% endif %}
</td>
<td>{{ m.hashrate or "-" }}</td>
<td>{{ m.power or "-" }}</td>
</tr>
{% endfor %}
</tbody>
</table>
</div>
</body>
</html>
"""

@app.route("/", methods=["GET", "POST"])
def index():
    # ✅ این ۲ خط رو اضافه کن:
    is_cron_job = request.args.get('source') == 'cron'
    if request.method == "POST" or is_cron_job:
        refresh_all()
    
    # بقیه کد مثل قبل...
    with CACHE_LOCK:
        miners = CACHE["miners"]
        last_update = CACHE["last_update"]
        next_update_timestamp = CACHE.get("next_update", None)
        total_hashrate = calculate_total_hashrate(miners)
    
    return render_template_string(
        TEMPLATE,
        miners=miners,
        last_update=last_update,
        next_update_timestamp=next_update_timestamp,
        total_hashrate=total_hashrate,
        miner_ip=MINER_IP,
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    # bind to 0.0.0.0 for Railway
    app.run(host="0.0.0.0", port=port, debug=False)
