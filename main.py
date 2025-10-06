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

# Login storage
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

# === Login Report ===
def get_week_report():
    tz = pytz.timezone("Asia/Tehran")
    now = datetime.now(tz)
    current_week_start = now - timedelta(days=now.weekday())
    last_week_start = current_week_start - timedelta(days=7)
    
    current_week_data = {}
    last_week_data = {}
    
    for login_time in login_times:
        if login_time >= current_week_start:
            day_name = jdatetime.datetime.fromgregorian(datetime=login_time).strftime("%A")
            current_week_data[day_name] = current_week_data.get(day_name, 0) + 1
        elif login_time >= last_week_start:
            day_name = jdatetime.datetime.fromgregorian(datetime=login_time).strftime("%A")
            last_week_data[day_name] = last_week_data.get(day_name, 0) + 1
    
    week_days = ["Saturday","Sunday","Monday","Tuesday","Wednesday","Thursday","Friday"]
    current_week_sorted = {day: current_week_data.get(day, 0) for day in week_days}
    last_week_sorted = {day: last_week_data.get(day, 0) for day in week_days}
    
    return {
        "current_week": current_week_sorted,
        "last_week": last_week_sorted,
        "current_week_start": jdatetime.datetime.fromgregorian(datetime=current_week_start).strftime("%Y/%m/%d"),
        "last_week_start": jdatetime.datetime.fromgregorian(datetime=last_week_start).strftime("%Y/%m/%d")
    }

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

TEMPLATE = """[HTML TEMPLATE SAME AS BEFORE, BUT ENGLISH ONLY, OMITTED FOR BREVITY]"""

@app.route("/", methods=["GET", "POST"])
def index():
    tz = pytz.timezone("Asia/Tehran")
    login_times.append(datetime.now(tz))
    miners = get_live_data()
    total_hashrate = calculate_total_hashrate(miners)
    return render_template_string(
        TEMPLATE,
        miners=miners,
        total_hashrate=total_hashrate,
    )

@app.route("/get_login_report")
def get_login_report():
    tz = pytz.timezone("Asia/Tehran")
    now = datetime.now(tz)
    one_day_ago = now - timedelta(hours=24)
    recent_logins = []
    for login_time in login_times:
        if login_time >= one_day_ago:
            j_time = jdatetime.datetime.fromgregorian(datetime=login_time)
            recent_logins.append({"time": j_time.strftime("%H:%M:%S")})
    last_login = jdatetime.datetime.fromgregorian(datetime=login_times[-1]).strftime("%Y/%m/%d - %H:%M:%S") if login_times else "No logins"
    week_report = get_week_report()
    return jsonify({
        "recent_logins": recent_logins[-10:],
        "last_login": last_login,
        "week_report": week_report
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
