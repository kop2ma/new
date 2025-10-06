#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import socket
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import pytz
from flask import Flask, render_template_string, request, jsonify
import jdatetime

# === CONFIG ===
MINER_IP = os.environ.get("MINER_IP")
MINER_NAMES = ["131", "132", "133", "65", "66", "70"]
MINER_PORTS = [204, 205, 206, 304, 305, 306]

# Login storage - ساختار جدید
login_data = {
    "current_week": {},  # { "1405/10/10": ["12:30:45", "14:20:15"], ... }
    "current_saturday": None  # تاریخ شنبه هفته جاری
}

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

# === Login Report ===
def get_current_saturday():
    """پیدا کردن شنبه هفته جاری بر اساس تقویم جلالی"""
    tz = pytz.timezone("Asia/Tehran")
    now = datetime.now(tz)
    j_now = jdatetime.datetime.fromgregorian(datetime=now)
    
    # پیدا کردن شنبه (روز 0 در هفته جلالی)
    days_since_saturday = j_now.weekday()
    current_saturday = j_now - timedelta(days=days_since_saturday)
    
    return current_saturday.strftime("%Y/%m/%d")

def update_login_data():
    """آپدیت داده‌های لاگین با سیستم هفته‌ای جدید"""
    tz = pytz.timezone("Asia/Tehran")
    current_time = datetime.now(tz)
    j_current = jdatetime.datetime.fromgregorian(datetime=current_time)
    current_date = j_current.strftime("%Y/%m/%d")
    current_time_str = j_current.strftime("%H:%M:%S")
    
    # بررسی آیا شنبه جدید شده؟
    current_saturday = get_current_saturday()
    
    if login_data["current_saturday"] != current_saturday:
        # شنبه جدید - پاک کردن داده‌های قدیم و شروع جدید
        login_data["current_week"] = {}
        login_data["current_saturday"] = current_saturday
    
    # اضافه کردن لاگین جدید
    if current_date not in login_data["current_week"]:
        login_data["current_week"][current_date] = []
    
    # اضافه کردن زمان اگر تکراری نیست
    if current_time_str not in login_data["current_week"][current_date]:
        login_data["current_week"][current_date].append(current_time_str)
        login_data["current_week"][current_date].sort()

def get_week_report():
    """گزارش هفته جاری به صورت درختی"""
    update_login_data()
    
    week_days_persian = ["شنبه", "یکشنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه"]
    
    # ساختار درختی برای گزارش
    tree_report = {
        "saturday": login_data["current_saturday"],
        "days": []
    }
    
    # تولید روزهای هفته از شنبه تا جمعه
    current_saturday = jdatetime.datetime.strptime(login_data["current_saturday"], "%Y/%m/%d")
    
    for i in range(7):
        current_date = current_saturday + timedelta(days=i)
        date_str = current_date.strftime("%Y/%m/%d")
        day_name = week_days_persian[i]
        
        day_data = {
            "date": date_str,
            "day_name": day_name,
            "logins": login_data["current_week"].get(date_str, []),
            "count": len(login_data["current_week"].get(date_str, []))
        }
        
        tree_report["days"].append(day_data)
    
    return tree_report

# === LIVE DATA ===
def get_live_data():
    miners = build_miners() if MINER_IP else []
    out = []
    if not miners:
        return []
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(poll_miner, m): m for m in miners}
        for fut in futures:
            try:
                res = fut.result()
            except Exception:
                res = {"name": f"{futures[fut]['name']} ({futures[fut]['port']})", "alive": False}
            out.append(res)
    return sorted(out, key=lambda x: x["name"])

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
.modal{display:none;position:fixed;top:50%;left:50%;transform:translate(-50%, -50%);background:white;padding:20px;border:3px solid #2ecc71;border-radius:10px;box-shadow:0 0 20px rgba(0,0,0,0.3);z-index:1000;width:90%;max-width:600px;max-height:80vh;overflow-y:auto;}
.modal-overlay{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.5);z-index:999;}
.report-btn{background:#9b59b6;color:white;padding:10px 15px;border:none;border-radius:8px;cursor:pointer;font-size:18px;}
.report-btn:hover{background:#8e44ad;}
.modal h3{margin-top:0;color:#2c3e50;text-align:center;border-bottom:2px solid #ecf0f1;padding-bottom:10px;}
.modal h4{color:#34495e;margin-bottom:8px;margin-top:20px;}
.modal p{margin:5px 0;padding:5px;background:#f8f9fa;border-radius:5px;}
.tree-item{margin:5px 0;padding:8px;background:#f8f9fa;border-radius:8px;border:1px solid #e9ecef;}
.tree-header{display:flex; justify-content:space-between; align-items:center; cursor:pointer; font-weight:bold;}
.tree-content{margin-top:8px; padding-right:20px; display:none;}
.tree-time{margin:2px 0; padding:3px 8px; background:white; border-radius:4px; font-family:monospace;}
.expand-btn{background:none; border:none; font-size:16px; cursor:pointer; margin-left:10px;}
.week-title{text-align:center; color:#2c3e50; margin-bottom:15px; padding:10px; background:#e8f5e8; border-radius:8px;}
@media(max-width:600px){th,td{font-size:16px;padding:8px;}}
</style>
</head>
<body>
<div class="card">
<div class="control-row">
    <div class="control-left">
        <form method="POST" action="/">
            <button type="submit" class="button">Refresh</button>
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
<th>Name</th>
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

<div id="modalOverlay" class="modal-overlay" onclick="closeModal()"></div>
<div id="reportModal" class="modal">
    <h3>📋 گزارش ورودهای هفته جاری</h3>
    <div id="reportContent">
        <p>در حال بارگذاری...</p>
    </div>
    <div style="text-align: center; margin-top: 20px;">
        <button onclick="closeModal()" style="background: #e74c3c; color: white; padding: 10px 20px; border: none; border-radius: 8px; cursor: pointer; font-size: 16px;">
            ❌ بستن
        </button>
    </div>
</div>

<script>
function showLoginReport() {
    document.getElementById('modalOverlay').style.display = 'block';
    document.getElementById('reportModal').style.display = 'block';
    
    fetch('/get_login_report')
        .then(response => response.json())
        .then(data => {
            let content = '';
            
            content += `<div class="week-title">
                <h4>📅 هفته شروع شده از شنبه ${data.saturday}</h4>
            </div>`;
            
            data.days.forEach(day => {
                content += `<div class="tree-item">
                    <div class="tree-header" onclick="toggleDay('day-${day.date}')">
                        <span>${day.day_name} - ${day.date} (${day.count} ورود)</span>
                        <button class="expand-btn">➕</button>
                    </div>
                    <div id="day-${day.date}" class="tree-content">
                `;
                
                if (day.logins.length > 0) {
                    day.logins.forEach(login => {
                        content += `<div class="tree-time">🕐 ${login}</div>`;
                    });
                } else {
                    content += `<div style="text-align:center; color:#666; padding:10px;">بدون رکورد</div>`;
                }
                
                content += `</div></div>`;
            });
            
            document.getElementById('reportContent').innerHTML = content;
        })
        .catch(error => {
            console.error('Error fetching report:', error);
            document.getElementById('reportContent').innerHTML = '<p>خطا در بارگذاری گزارش</p>';
        });
}

function toggleDay(dayId) {
    const content = document.getElementById(dayId);
    const btn = content.previousElementSibling.querySelector('.expand-btn');
    
    if (content.style.display === 'block') {
        content.style.display = 'none';
        btn.textContent = '➕';
    } else {
        content.style.display = 'block';
        btn.textContent = '➖';
    }
}

function closeModal() {
    document.getElementById('modalOverlay').style.display = 'none';
    document.getElementById('reportModal').style.display = 'none';
}

// Close modal when clicking outside
window.onclick = function(event) {
    const modal = document.getElementById('reportModal');
    const overlay = document.getElementById('modalOverlay');
    if (event.target === overlay) {
        closeModal();
    }
}
</script>
</body>
</html>
"""

@app.route("/", methods=["GET", "POST"])
def index():
    update_login_data()  # آپدیت داده‌های لاگین در هر بار رفرش
    miners = get_live_data()
    total_hashrate = calculate_total_hashrate(miners)
    return render_template_string(
        TEMPLATE,
        miners=miners,
        total_hashrate=total_hashrate,
    )

@app.route("/get_login_report")
def get_login_report():
    try:
        week_report = get_week_report()
        return jsonify(week_report)
    except Exception as e:
        print(f"Error in get_login_report: {e}")
        return jsonify({
            "saturday": "Error",
            "days": []
        })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
