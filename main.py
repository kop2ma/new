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
    "current_saturday": None,  # تاریخ شنبه هفته جاری
    "last_login_time": None    # آخرین زمان ثبت لاگین
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
            chunks
