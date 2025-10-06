#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import socket
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import pytz
from flask import Flask, render_template_string, jsonify
import jdatetime

# === CONFIG ===
MINER_IP = os.environ.get("MINER_IP")
MINER_NAMES = ["131", "132", "133", "65", "66", "70"]
MINER_PORTS = [204, 205, 206, 304, 305, 306]

# ذخیره لاگین‌ها
login_times = []

def build_miners():
    ip = MINER_IP
    miners = []
    for name, port in zip(MINER_NAMES, MINER_PORTS):
        miners.append({"name": name, "ip": ip, "port": port})
    return miners

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

# === گزارش هفته و روز ===
def get_week_report():
    now = jdatetime.datetime.now()
    # هفته جاری از شنبه شروع می‌شود
    current_week_start = now - jdatetime.timedelta(days=now.weekday() + 1 if now.weekday() < 6 else 0)
    last_week_start = current_week_start - jdatetime.timedelta(days=7)
    
    current_week_data = {}
    last_week_data = {}
    
    for login_time in login_times:
        if login_time >= current_week_start:
            day_name = login_time.strftime("%A")  # روز هفته انگلیسی
            current_week_data[day_name] = current_week_data.get(day_name, 0) + 1
        elif login_time >= last_week_start:
            day_name = login_time.strftime("%A")
            last_week_data[day_name] = last_week_data.get(day_name, 0) + 1
    
    week_days = ["Saturday","Sunday","Monday","Tuesday","Wednesday","Thursday","Friday"]
    current_week_sorted = {day: current_week_data.get(day,0) for day in week_days}
    last_week_sorted = {day: last_week_data.get(day,0) for day in week_days}
    
    return {
        "current_week": current_week_sorted,
        "last_week": last_week_sorted,
        "current_week_start": current_week_start.strftime("%Y/%m/%d"),
        "last_week_start": last_week_start.strftime("%Y/%m/%d")
    }

# === LIVE DATA (بدون کش) ===
def get_live_data():
    miners = build_miners() if MINER_IP else []
    out = []
    if not miners:
        return [], "No miners configured", None
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(poll_miner, m): m for m in miners}
        for fut in futures:
            try:
                res = fut.result()
            except Exception:
                res = {"name": f"{futures[fut]['name']} ({futures[fut]['port']})", "alive": False}
            out.append(res)
    
    tz = pytz.timezone("Asia/Tehran")
    now = datetime.now(tz)
    last_update = now.strftime("%Y-%m-%d %H:%M:%S")
    
    return sorted(out, key=lambda x: x["name"]), last_update, now.timestamp()

def calculate_total_hashrate(miners):
    total = 0
    for miner in miners:
        if miner.get("alive") and miner.get("hashrate") is not None:
            total += miner["hashrate"]
    return round(total, 2)

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
.total-hashrate{background:#e0e7ff; padding:8px 16px; border-radius:8px; font-weight:bold; font-size:16px; color:#1e40af;}
.control-row{display:flex; justify-content:space-between; align-items:center; margin-bottom:15px; gap:15px;}
.control-left{display:flex; align-items:center; gap:15px;}
.modal {display:none; position:fixed; top:50%; left:50%; transform:translate(-50%,-50%); background:white; padding:20px; border:3px solid #2ecc71; border-radius:10px; box-shadow:0 0 20px rgba(0,0,0,0.3); z-index:1000; width:90%; max-width:500px; max-height:80vh; overflow-y:auto;}
.report-btn {background:#9b59b6; color:white; padding:15px; border:none; border-radius:8px; cursor:pointer; font-size:20px;}
.report-btn:hover {background:#8e44ad;}
.day-branch {margin-left:10px;}
.login-time {margin-left:20px; font-size:14px; color:#333;}
@media(max-width:600px){th,td{font-size:16px;padding:8px;}}
</style>
</head>
<body>
<div class="card">
<div class="control-row">
    <div class="control-left">
        <form method="POST" action="/">
            <button type="submit" class="button">Refresh Now</button>
        </form>
        <div class="total-hashrate">
            Total Hashrate: {{ total_hashrate }} TH/s
        </div>
    </div>
    <button class="report-btn" onclick="showLoginReport()">📊</button>
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

<!-- پنجره پاپ‌آپ گزارش لاگین‌ها -->
<div id="reportModal" class="modal">
    <h3>📋 گزارش لاگین‌ها</h3>
    <div id="reportContent"></div>
    <button onclick="closeModal()" style="margin-top:15px;background:#e74c3c;color:white;padding:8px 15px;border:none;border-radius:5px;">❌ بستن</button>
</div>

<script>
function showLoginReport() {
    fetch('/get_login_report')
        .then(r => r.json())
        .then(data => {
            let content = '';
            // هفته جاری
            content += `<h4>هفته جاری (${data.week_report.current_week_start})</h4>`;
            Object.entries(data.week_report.current_week).forEach(([day, count]) => {
                if(count>0){
                    content += `<div class="day-branch">${day}: ${count} لاگین <button onclick="toggleTimes('${day}')">+</button><div id="times-${day}" style="display:none;"></div></div>`;
                }
            });

            // هفته قبل
            content += `<h4>هفته قبل (${data.week_report.last_week_start})</h4>`;
            Object.entries(data.week_report.last_week).forEach(([day, count]) => {
                if(count>0){
                    content += `<div class="day-branch">${day}: ${count} لاگین <button onclick="toggleTimes('${day}')">+</button><div id="times-${day}" style="display:none;"></div></div>`;
                }
            });

            document.getElementById('reportContent').innerHTML = content;

            // ذخیره لاگین‌های دقیق
            window.loginTimes = data.login_times;

            document.getElementById('reportModal').style.display = 'block';
        });
}

// تابع باز و بسته کردن ساعت‌های لاگین
function toggleTimes(day){
    const container = document.getElementById(`times-${day}`);
    if(container.style.display==='none'){
        container.innerHTML = '';
        window.loginTimes.forEach(l=>{
            if(l.day===day){
                container.innerHTML += `<div class="login-time">${l.time}</div>`;
            }
        });
        container.style.display='block';
    } else {
        container.style.display='none';
    }
}

function closeModal(){
    document.getElementById('reportModal').style.display='none';
}
</script>
</body>
</html>
"""

@app.route("/", methods=["GET", "POST"])
def index():
    # ذخیره زمان لاگین
    login_times.append(jdatetime.datetime.now())
    
    miners, last_update, _ = get_live_data()
    total_hashrate = calculate_total_hashrate(miners)
    
    return render_template_string(
        TEMPLATE,
        miners=miners,
        total_hashrate=total_hashrate
    )

@app.route("/get_login_report")
def get_login_report():
    now = jdatetime.datetime.now()
    week_report = get_week_report()

    # ساخت آرایه دقیق لاگین‌ها برای پاپ‌آپ
    login_list = []
    for t in login_times:
        day = t.strftime("%A")
        time_str = t.strftime("%H:%M:%S")
        login_list.append({"day": day, "time": time_str})

    return jsonify({
        "week_report": week_report,
        "login_times": login_list
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
