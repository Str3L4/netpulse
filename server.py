import asyncio
import json
import logging
import os
import re
import socket
import sys
import threading
import time
import urllib.request
from aiohttp import web
import paramiko
import requests
import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("NETPULSE")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.yaml")
CONFIG_EXAMPLE_FILE = os.path.join(BASE_DIR, "config.example.yaml")
ENV_FILE = os.path.join(BASE_DIR, ".env")
CONTEXT_FILE = os.path.join(BASE_DIR, "ANTIGRAVITY_EMERGENCY_CONTEXT.md")
CHAT_CONFIG_FILE = os.path.join(BASE_DIR, "chat_config.json")

def load_dotenv(path=ENV_FILE):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("'\"")
                        if k not in os.environ:
                            os.environ[k] = v
        except Exception as e:
            logger.warning(f"Error loading .env file: {e}")

load_dotenv()

def interpolate_env(text):
    def replace(match):
        expr = match.group(1)
        if ":" in expr:
            var_name, default_val = expr.split(":", 1)
            return os.environ.get(var_name, default_val)
        return os.environ.get(expr, "")
    return re.sub(r"\$\{([^}]+)\}", replace, text)

def load_config():
    target = CONFIG_FILE if os.path.exists(CONFIG_FILE) else CONFIG_EXAMPLE_FILE
    if not os.path.exists(target):
        logger.error("No config.yaml found!")
        return {}
    with open(target, "r", encoding="utf-8") as f:
        raw = f.read()
    interpolated = interpolate_env(raw)
    return yaml.safe_load(interpolated) or {}

APP_CONFIG = load_config()

# Global telemetry state initialized dynamically
telemetry_data = {
    "status": "initializing",
    "timestamp": time.time(),
    "app": APP_CONFIG.get("app", {}),
    "nodes": {},
    "bypasses": {},
    "pi_hardware": {
        "uptime": "N/A",
        "cpu_load": "0.00",
        "temperature": 0.0,
        "ram_used_mb": 0,
        "ram_total_mb": 0,
        "ram_percent": 0,
        "disk_used_mb": 0,
        "disk_total_mb": 0,
        "disk_percent": 0,
        "services": {}
    },
    "mikrotik_hardware": {
        "cpu_load": 0,
        "ram_used_mb": 0,
        "ram_total_mb": 0,
        "uptime": "N/A",
        "ether1_wan": False,
        "ether2_pc": False,
        "ether3_pi": False
    },
    "history": {
        "labels": [],
        "pi_temp": [],
        "pi_cpu": [],
        "yt_latency": [],
        "vpn_latency": []
    },
    "logs": []
}

# Populate nodes from config
for n in APP_CONFIG.get("nodes", []):
    telemetry_data["nodes"][n["id"]] = {
        "name": n.get("name", n["id"]),
        "ip": n.get("host", ""),
        "type": n.get("type", "device"),
        "role": n.get("role", ""),
        "status": "unknown",
        "latency": 0
    }

# Populate services from config
services_cfg = APP_CONFIG.get("services", {})
if services_cfg.get("zapret", {}).get("enabled", True):
    telemetry_data["bypasses"]["zapret"] = {
        "status": "unknown",
        "name": services_cfg["zapret"].get("name", "Zapret DPI"),
        "mode": services_cfg["zapret"].get("mode", "ALT11"),
        "youtube_status": "checking",
        "youtube_code": 0,
        "youtube_latency": 0
    }

if services_cfg.get("vless", {}).get("enabled", True):
    v = services_cfg["vless"]
    telemetry_data["bypasses"]["vless"] = {
        "status": "unknown",
        "name": v.get("name", "VLESS Reality"),
        "protocol": v.get("protocol", "VLESS + Reality"),
        "external_ip": "unknown",
        "socks_port": v.get("socks_port", 10808),
        "http_port": v.get("http_port", 10809),
        "latency": 0,
        "proxybridge_status": "unknown"
    }

if services_cfg.get("telegram", {}).get("enabled", False):
    tg = services_cfg["telegram"]
    telemetry_data["bypasses"]["telegram"] = {
        "status": "unknown",
        "name": tg.get("name", "Telegram Proxy"),
        "port_ws": tg.get("port_ws", 1443),
        "port_socks": tg.get("port_socks", 10808),
        "latency": 0
    }

telemetry_lock = threading.Lock()

def load_golden_xray_config():
    repair_cfg = APP_CONFIG.get("repair", {}).get("linux_gateway", {})
    golden_filename = repair_cfg.get("xray_golden_file", "xray_golden.json")
    golden_path = os.path.join(BASE_DIR, golden_filename)
    if os.path.exists(golden_path):
        try:
            with open(golden_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Error reading {golden_filename}: {e}")
    return None

def add_log(msg, level="info"):
    now_str = time.strftime("%H:%M:%S")
    with telemetry_lock:
        telemetry_data["logs"].insert(0, {"time": now_str, "text": msg, "level": level})
        if len(telemetry_data["logs"]) > 50:
            telemetry_data["logs"].pop()

def probe_tcp(host, port, timeout=0.5):
    t0 = time.time()
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True, round((time.time() - t0) * 1000, 1)
    except Exception:
        return False, 0

def test_socks5(host, port, target_host="api.ipify.org", target_port=80, timeout=3.0):
    t0 = time.time()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, port))
        s.sendall(b"\x05\x01\x00")
        resp = s.recv(2)
        if resp != b"\x05\x00":
            s.close()
            return False, 0, "Handshake failed"
        domain_bytes = target_host.encode("utf-8")
        req = b"\x05\x01\x00\x03" + bytes([len(domain_bytes)]) + domain_bytes + target_port.to_bytes(2, "big")
        s.sendall(req)
        resp = s.recv(10)
        if len(resp) < 4 or resp[1] != 0:
            s.close()
            return False, 0, f"Connect error: {resp[1] if len(resp)>1 else 'unknown'}"
        http_req = f"GET / HTTP/1.1\r\nHost: {target_host}\r\nUser-Agent: curl/7.88\r\nConnection: close\r\n\r\n".encode()
        s.sendall(http_req)
        http_resp = s.recv(1024).decode("latin1", errors="replace")
        s.close()
        latency = round((time.time() - t0) * 1000, 1)
        body = http_resp.split("\r\n\r\n")[-1].strip()
        return True, latency, body
    except Exception as e:
        return False, 0, str(e)

def test_youtube():
    t0 = time.time()
    try:
        handler = urllib.request.ProxyHandler({})
        opener = urllib.request.build_opener(handler)
        req = urllib.request.Request("https://www.youtube.com/generate_204", headers={"User-Agent": "Mozilla/5.0"})
        with opener.open(req, timeout=3.0) as resp:
            lat = round((time.time() - t0) * 1000, 1)
            return True, lat, resp.status
    except Exception as e:
        return False, 0, str(e)

def test_cloudcode_api(timeout=4.0):
    t0 = time.time()
    try:
        vless_cfg = APP_CONFIG.get("services", {}).get("vless", {})
        socks_h = vless_cfg.get("socks_host", "127.0.0.1")
        socks_p = vless_cfg.get("socks_port", 10808)
        proxies = {
            "http": f"socks5h://{socks_h}:{socks_p}",
            "https": f"socks5h://{socks_h}:{socks_p}"
        }
        resp = requests.get("https://daily-cloudcode-pa.googleapis.com", proxies=proxies, timeout=timeout)
        lat = round((time.time() - t0) * 1000, 1)
        return True, lat, resp.status_code
    except Exception as e:
        return False, 0, str(e)

class PiCollector:
    def __init__(self, cfg):
        self.cfg = cfg
        self.client = None

    def get_client(self):
        if not self.cfg.get("enabled", True):
            return None
        host = self.cfg.get("host")
        if not host:
            return None
        if self.client:
            try:
                self.client.exec_command("echo ping", timeout=1)
                return self.client
            except Exception:
                try:
                    self.client.close()
                except Exception:
                    pass
                self.client = None

        try:
            c = paramiko.SSHClient()
            c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            c.connect(
                host,
                username=self.cfg.get("user", "root"),
                password=self.cfg.get("password", ""),
                timeout=2
            )
            self.client = c
            return self.client
        except Exception as e:
            logger.warning(f"Linux SSH connect failed ({host}): {e}")
            return None

    def query(self):
        c = self.get_client()
        if not c:
            return None
        svcs_list = " ".join(self.cfg.get("services", ["zapret", "xray"]))
        cmd = f"""
TEMP=$(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null)
MEM=$(free -m | awk 'NR==2{{print $2,$3,$7}}')
DISK=$(df -m / | awk 'NR==2{{print $2,$3,$4}}')
SVCS=$(systemctl is-active {svcs_list} 2>/dev/null | tr '\\n' ' ')
UP=$(cat /proc/uptime | awk '{{print $1}}')
LOAD=$(cat /proc/loadavg | awk '{{print $1,$2,$3}}')
echo "$UP|$LOAD|$TEMP|$MEM|$DISK|$SVCS"
"""
        try:
            stdin, stdout, stderr = c.exec_command(cmd, timeout=3)
            res = stdout.read().decode("utf-8").strip()
            return res
        except Exception as e:
            logger.warning(f"Linux query error: {e}")
            return None

class MikroTikCollector:
    def __init__(self, cfg):
        self.cfg = cfg
        self.client = None

    def get_client(self):
        if not self.cfg.get("enabled", True):
            return None
        host = self.cfg.get("host")
        if not host:
            return None
        if self.client:
            try:
                self.client.exec_command(":put 1", timeout=1)
                return self.client
            except Exception:
                try:
                    self.client.close()
                except Exception:
                    pass
                self.client = None

        try:
            c = paramiko.SSHClient()
            c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            c.connect(
                host,
                username=self.cfg.get("user", "admin"),
                password=self.cfg.get("password", ""),
                timeout=2
            )
            self.client = c
            return self.client
        except Exception as e:
            logger.warning(f"MikroTik SSH connect failed ({host}): {e}")
            return None

    def query(self):
        c = self.get_client()
        if not c:
            return None
        cmd = ':put ([/system resource get cpu-load] . "|" . [/system resource get free-memory] . "|" . [/system resource get total-memory] . "|" . [/system resource get uptime] . "|" . [/interface get ether1 running] . "|" . [/interface get ether2 running] . "|" . [/interface get ether3 running])'
        try:
            stdin, stdout, stderr = c.exec_command(cmd, timeout=3)
            res = stdout.read().decode("utf-8").strip()
            return res
        except Exception as e:
            logger.warning(f"MikroTik query error: {e}")
            return None

pi_collector = PiCollector(APP_CONFIG.get("collectors", {}).get("linux_ssh", {}))
mt_collector = MikroTikCollector(APP_CONFIG.get("collectors", {}).get("mikrotik", {}))

def check_process_running(proc_name):
    try:
        import subprocess
        out = subprocess.check_output(f'tasklist /FI "IMAGENAME eq {proc_name}"', shell=True).decode('cp866', errors='replace')
        return proc_name.lower() in out.lower()
    except Exception:
        return False

def telemetry_worker():
    add_log("NETPULSE служба телеметрии запущена", "info")
    refresh_sec = APP_CONFIG.get("app", {}).get("refresh_seconds", 7)
    
    while True:
        try:
            # 1. Probe configured nodes
            for n in APP_CONFIG.get("nodes", []):
                nid = n["id"]
                host = n.get("host", "")
                port = n.get("probe_port", 80)
                if nid == "pc" or host in ["127.0.0.1", "localhost"]:
                    is_ok = True
                    lat = 0.1
                else:
                    is_ok, lat = probe_tcp(host, port)
                
                with telemetry_lock:
                    if nid in telemetry_data["nodes"]:
                        telemetry_data["nodes"][nid]["status"] = "online" if is_ok else "offline"
                        telemetry_data["nodes"][nid]["latency"] = lat

            # 2. Probe VLESS Reality
            v_cfg = APP_CONFIG.get("services", {}).get("vless", {})
            vless_ok, vless_lat, vless_ip = False, 0, ""
            if v_cfg.get("enabled", True):
                v_host = v_cfg.get("socks_host", "127.0.0.1")
                v_port = v_cfg.get("socks_port", 10808)
                vless_ok, vless_lat, vless_ip = test_socks5(v_host, v_port)

            # 3. Probe YouTube DPI
            z_cfg = APP_CONFIG.get("services", {}).get("zapret", {})
            yt_ok, yt_lat, yt_code = False, 0, 0
            if z_cfg.get("enabled", True):
                yt_ok, yt_lat, yt_code = test_youtube()

            # 4. Probe Telegram
            tg_cfg = APP_CONFIG.get("services", {}).get("telegram", {})
            tg_ok, tg_lat = False, 0
            if tg_cfg.get("enabled", False):
                tg_host = tg_cfg.get("host") or v_cfg.get("socks_host", "127.0.0.1")
                tg_ok, tg_lat = probe_tcp(tg_host, tg_cfg.get("port_ws", 1443))

            # 5. Check ProxyBridge
            pb_running = check_process_running("ProxyBridge.exe")

            # 6. Query Linux/Pi
            pi_res = pi_collector.query()
            pi_data = {}
            if pi_res and "|" in pi_res:
                parts = pi_res.split("|")
                if len(parts) >= 6:
                    uptime_sec = float(parts[0]) if parts[0] else 0
                    hours = int(uptime_sec // 3600)
                    minutes = int((uptime_sec % 3600) // 60)
                    uptime_str = f"{hours} ч {minutes} мин"
                    
                    load_parts = parts[1].split()
                    temp_c = round(float(parts[2]) / 1000.0, 1) if parts[2].isdigit() else 0.0
                    
                    mem_parts = parts[3].split()
                    mem_total = int(mem_parts[0]) if len(mem_parts) > 0 else 0
                    mem_used = int(mem_parts[1]) if len(mem_parts) > 1 else 0
                    mem_pct = round((mem_used / mem_total * 100), 1) if mem_total else 0
                    
                    disk_parts = parts[4].split()
                    disk_total = int(disk_parts[0]) if len(disk_parts) > 0 else 0
                    disk_used = int(disk_parts[1]) if len(disk_parts) > 1 else 0
                    disk_pct = round((disk_used / disk_total * 100), 1) if disk_total else 0
                    
                    cfg_svcs = APP_CONFIG.get("collectors", {}).get("linux_ssh", {}).get("services", ["zapret", "xray"])
                    svc_states = parts[5].strip().split()
                    services = {name: (svc_states[i] if i < len(svc_states) else "unknown") for i, name in enumerate(cfg_svcs)}
                    
                    pi_data = {
                        "uptime": uptime_str,
                        "cpu_load": parts[1],
                        "cpu_val": float(load_parts[0]) if load_parts else 0.0,
                        "temperature": temp_c,
                        "ram_used_mb": mem_used,
                        "ram_total_mb": mem_total,
                        "ram_percent": mem_pct,
                        "disk_used_mb": disk_used,
                        "disk_total_mb": disk_total,
                        "disk_percent": disk_pct,
                        "services": services
                    }

            # 7. Query MikroTik
            mt_res = mt_collector.query()
            mt_data = {}
            if mt_res and "|" in mt_res:
                parts = mt_res.split("|")
                if len(parts) >= 7:
                    cpu_load = int(parts[0]) if parts[0].isdigit() else 0
                    free_mem = int(parts[1]) if parts[1].isdigit() else 0
                    total_mem = int(parts[2]) if parts[2].isdigit() else 0
                    used_mem_mb = round((total_mem - free_mem) / (1024*1024), 1)
                    total_mem_mb = round(total_mem / (1024*1024), 1)
                    uptime = parts[3]
                    e1 = parts[4].lower() == "true"
                    e2 = parts[5].lower() == "true"
                    e3 = parts[6].lower() == "true"
                    mt_data = {
                        "cpu_load": cpu_load,
                        "ram_used_mb": used_mem_mb,
                        "ram_total_mb": total_mem_mb,
                        "uptime": uptime,
                        "ether1_wan": e1,
                        "ether2_pc": e2,
                        "ether3_pi": e3
                    }

            with telemetry_lock:
                telemetry_data["status"] = "online"
                telemetry_data["timestamp"] = time.time()

                if "zapret" in telemetry_data["bypasses"]:
                    telemetry_data["bypasses"]["zapret"]["status"] = "online" if (pi_data.get("services", {}).get("zapret") == "active" and yt_ok) else "degraded"
                    telemetry_data["bypasses"]["zapret"]["youtube_status"] = "OK" if yt_ok else "Fail"
                    telemetry_data["bypasses"]["zapret"]["youtube_code"] = yt_code
                    telemetry_data["bypasses"]["zapret"]["youtube_latency"] = yt_lat

                if "vless" in telemetry_data["bypasses"]:
                    telemetry_data["bypasses"]["vless"]["status"] = "online" if vless_ok else "offline"
                    telemetry_data["bypasses"]["vless"]["latency"] = vless_lat
                    telemetry_data["bypasses"]["vless"]["external_ip"] = vless_ip if vless_ok else "Ошибка"
                    telemetry_data["bypasses"]["vless"]["proxybridge_status"] = "Активен" if pb_running else "Не запущен"

                if "telegram" in telemetry_data["bypasses"]:
                    telemetry_data["bypasses"]["telegram"]["status"] = "online" if tg_ok else "offline"
                    telemetry_data["bypasses"]["telegram"]["latency"] = tg_lat

                if pi_data:
                    telemetry_data["pi_hardware"].update(pi_data)
                if mt_data:
                    telemetry_data["mikrotik_hardware"].update(mt_data)

                t_label = time.strftime("%H:%M:%S")
                hist = telemetry_data["history"]
                hist["labels"].append(t_label)
                hist["pi_temp"].append(telemetry_data["pi_hardware"].get("temperature", 0))
                hist["pi_cpu"].append(pi_data.get("cpu_val", 0) * 10)
                hist["yt_latency"].append(yt_lat)
                hist["vpn_latency"].append(vless_lat)

                max_points = 25
                for key in ["labels", "pi_temp", "pi_cpu", "yt_latency", "vpn_latency"]:
                    if len(hist[key]) > max_points:
                        hist[key] = hist[key][-max_points:]

            add_log(f"Проверка: YouTube={yt_lat}ms, VPN={vless_lat}ms ({vless_ip})", "success" if (yt_ok and vless_ok) else "warning")

        except Exception as e:
            logger.error(f"Error in telemetry loop: {e}")
            add_log(f"Ошибка сбора метрик: {e}", "error")

        time.sleep(refresh_sec)

def enforce_windows_direct_proxy():
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Internet Settings', 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, 'ProxyEnable', 0, winreg.REG_DWORD, 0)
        try:
            winreg.DeleteValue(key, 'ProxyServer')
        except FileNotFoundError:
            pass

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Internet Settings\Connections', 0, winreg.KEY_ALL_ACCESS) as conn_key:
        for val_name in ['DefaultConnectionSettings', 'SavedLegacySettings']:
            try:
                val, val_type = winreg.QueryValueEx(conn_key, val_name)
                if len(val) >= 9 and val[8] != 1:
                    ba = bytearray(val)
                    ba[8] = 1
                    winreg.SetValueEx(conn_key, val_name, 0, val_type, bytes(ba))
            except Exception:
                pass

def enforce_proxybridge_profile():
    pb_cfg = APP_CONFIG.get("repair", {}).get("proxybridge", {})
    raw_path = pb_cfg.get("profile_path", r"%APPDATA%\ProxyBridge\profiles\Default.pbprofile")
    pb_path = os.path.expandvars(raw_path)
    
    if not os.path.exists(pb_path):
        os.makedirs(os.path.dirname(pb_path), exist_ok=True)
        cfg = {"Version": "1.0", "Name": "Default", "ProxyConfigs": [], "ProxyRules": []}
    else:
        with open(pb_path, 'r', encoding='utf-8') as f:
            cfg = json.load(f)

    updated = False
    p_configs = cfg.setdefault('ProxyConfigs', [])
    target_host = pb_cfg.get("proxy_host", "127.0.0.1")
    target_port = str(pb_cfg.get("proxy_port", "10808"))

    if not p_configs:
        p_configs.append({
            "Id": 1,
            "Name": "Pi-Germany",
            "Type": "SOCKS5",
            "Host": target_host,
            "Port": target_port,
            "Username": "",
            "Password": "",
            "SendDomainToProxy": True
        })
        updated = True
    else:
        p = p_configs[0]
        if p.get('Host') != target_host or str(p.get('Port')) != target_port or p.get('Type') != 'SOCKS5':
            p['Host'] = target_host
            p['Port'] = target_port
            p['Type'] = 'SOCKS5'
            p['SendDomainToProxy'] = True
            updated = True

    rules = cfg.setdefault('ProxyRules', [])
    ag_rule = next((r for r in rules if 'antigravity' in r.get('ProcessName', '').lower()), None)
    if not ag_rule:
        rules.append({
            "Name": "AGRule",
            "ProcessName": "Antigravity.exe; antigravity.exe",
            "TargetHosts": "*",
            "TargetPorts": "*",
            "TargetDomains": "*",
            "Protocol": "BOTH",
            "Action": "PROXY",
            "IsEnabled": False,
            "LocalhostViaProxy": False,
            "ProxyConfigId": 1
        })
        updated = True
    else:
        if ag_rule.get('IsEnabled') is not False or ag_rule.get('LocalhostViaProxy') is not False:
            ag_rule['IsEnabled'] = False
            ag_rule['LocalhostViaProxy'] = False
            updated = True

    lr_rule = next((r for r in rules if 'language_server' in r.get('ProcessName', '').lower()), None)
    if not lr_rule:
        rules.append({
            "Name": "LRAG",
            "ProcessName": "language_server.exe",
            "TargetHosts": "*",
            "TargetPorts": "*",
            "TargetDomains": "*",
            "Protocol": "BOTH",
            "Action": "PROXY",
            "IsEnabled": True,
            "LocalhostViaProxy": False,
            "ProxyConfigId": 1
        })
        updated = True
    else:
        if lr_rule.get('IsEnabled') is not True or lr_rule.get('LocalhostViaProxy') is not False or lr_rule.get('ProxyConfigId') != 1:
            lr_rule['IsEnabled'] = True
            lr_rule['LocalhostViaProxy'] = False
            lr_rule['ProxyConfigId'] = 1
            updated = True

    pb_running = check_process_running("ProxyBridge.exe")
    if updated or not pb_running:
        with open(pb_path, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, indent=2)
        import subprocess
        subprocess.run(['powershell', '-Command', 'Stop-Process -Name ProxyBridge -Force -ErrorAction SilentlyContinue; Start-Sleep -Milliseconds 400; Start-Process "C:\\Program Files\\ProxyBridge\\ProxyBridge.exe" -ArgumentList "--minimized" -ErrorAction SilentlyContinue'], capture_output=True)
        return True, "Профиль обновлен и ProxyBridge перезапущен (LRAG=вкл, AGRule=выкл)"
    return False, "ProxyBridge уже в эталонном состоянии (LRAG=вкл, AGRule=выкл)"

def enforce_pi_golden_config():
    linux_cfg = APP_CONFIG.get("collectors", {}).get("linux_ssh", {})
    if not linux_cfg.get("enabled", True):
        return False
    host = linux_cfg.get("host")
    if not host:
        return False
    c_pi = paramiko.SSHClient()
    c_pi.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c_pi.connect(host, username=linux_cfg.get("user", "root"), password=linux_cfg.get("password", ""), timeout=3)
    
    deployed = False
    golden_cfg = load_golden_xray_config()
    if golden_cfg:
        stdin, stdout, stderr = c_pi.exec_command("cat /opt/xray/config.json 2>/dev/null")
        cur_cfg = stdout.read().decode("utf-8")
        first_outbound = golden_cfg.get("outbounds", [{}])[0]
        vnext = first_outbound.get("settings", {}).get("vnext", [{}])[0]
        target_addr = vnext.get("address", "")
        if target_addr and target_addr not in cur_cfg:
            import base64
            cfg_str = json.dumps(golden_cfg, indent=2)
            b64_cfg = base64.b64encode(cfg_str.encode("utf-8")).decode("ascii")
            cmd = f"echo '{b64_cfg}' | base64 -d | sudo tee /opt/xray/config.json > /dev/null"
            c_pi.exec_command(cmd)
            deployed = True

    svcs = " ".join(APP_CONFIG.get("repair", {}).get("linux_gateway", {}).get("restart_services", ["zapret", "xray"]))
    stdin, stdout, stderr = c_pi.exec_command(f"sudo systemctl restart {svcs}")
    stdout.channel.recv_exit_status()
    c_pi.close()
    return deployed

def run_self_repair():
    logs = []
    
    # 1. Windows: Reset System Proxy & WinINet
    if sys.platform == "win32":
        try:
            enforce_windows_direct_proxy()
            logs.append({"step": "Windows", "status": "ok", "msg": "Системный прокси и WinINet сброшены в прямое подключение (Direct, 0x01)"})
        except Exception as e:
            logs.append({"step": "Windows", "status": "warning", "msg": f"Ошибка сброса системного прокси: {e}"})

    # 2. Windows: ProxyBridge Profile
    if sys.platform == "win32":
        try:
            changed, msg = enforce_proxybridge_profile()
            logs.append({"step": "ProxyBridge", "status": "ok", "msg": msg})
        except Exception as e:
            logs.append({"step": "ProxyBridge", "status": "warning", "msg": f"Ошибка настройки ProxyBridge: {e}"})

    # 3. Linux Gateway Services & Golden Config
    try:
        deployed = enforce_pi_golden_config()
        msg = "Конфиг Xray переразвернут на эталон и службы перезапущены" if deployed else "Службы шлюза перезапущены"
        logs.append({"step": "Шлюз Linux", "status": "ok", "msg": msg})
    except Exception as e:
        logs.append({"step": "Шлюз Linux", "status": "error", "msg": f"Ошибка связи со шлюзом: {e}"})

    # 4. MikroTik Rules & FastTrack
    mt_repair = APP_CONFIG.get("repair", {}).get("router_rules", {})
    if mt_repair.get("enabled", False):
        try:
            mt_cfg = APP_CONFIG.get("collectors", {}).get("mikrotik", {})
            host = mt_cfg.get("host")
            if not host:
                logs.append({"step": "Маршрутизатор", "status": "warning", "msg": "Хост маршрутизатора не настроен (пропущено)"})
            else:
                c_mt = paramiko.SSHClient()
                c_mt.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                c_mt.connect(host, username=mt_cfg.get("user", "admin"), password=mt_cfg.get("password", ""), timeout=3)
                
                if mt_repair.get("ggc_capture_sni", True):
                    c_mt.exec_command(':if ([:len [/ip firewall mangle find comment~"Auto-Add GGC googlevideo"]] = 0) do={/ip firewall mangle add chain=prerouting protocol=tcp dst-port=443 tls-host="*.googlevideo.com" action=add-dst-to-address-list address-list=UNBLOCK-ZAPRET address-list-timeout=1w comment="Auto-Add GGC googlevideo" place-before=[/ip firewall mangle find comment~"Zapret: Mark Connection TCP"]}')
                    c_mt.exec_command(':if ([:len [/ip firewall mangle find comment~"Auto-Add YouTube CDNs"]] = 0) do={/ip firewall mangle add chain=prerouting protocol=tcp dst-port=443 tls-host="*youtube*" action=add-dst-to-address-list address-list=UNBLOCK-ZAPRET address-list-timeout=1w comment="Auto-Add YouTube CDNs" place-before=[/ip firewall mangle find comment~"Zapret: Mark Connection TCP"]}')
                
                ggc_subnets = mt_repair.get("ggc_subnets", [])
                for ggc_net in ggc_subnets:
                    c_mt.exec_command(f':if ([:len [/ip firewall address-list find list="UNBLOCK-ZAPRET" address="{ggc_net}"]] = 0) do={{/ip firewall address-list add list=UNBLOCK-ZAPRET address="{ggc_net}" comment="Google/GGC"}}')
                
                if mt_repair.get("discord_mangle", True):
                    c_mt.exec_command('/ip firewall mangle set [find comment~"Discord Voice"] dst-address-list=UNBLOCK-ZAPRET')
                if mt_repair.get("fasttrack_enabled", True):
                    c_mt.exec_command('/ip firewall filter set [find comment~"fasttrack"] connection-mark=no-mark disabled=no')
                
                c_mt.exec_command('/ip firewall connection remove [find dying=yes]')
                
                flush_subnets = mt_repair.get("flush_subnets", [])
                for net in flush_subnets:
                    c_mt.exec_command(f'/ip firewall connection remove [find dst-address~"{net}" and connection-mark="no-mark"]')
                
                c_mt.close()
                logs.append({"step": "Маршрутизатор", "status": "ok", "msg": "Mangle, FastTrack и правила маршрутизатора приведены к эталону"})
        except Exception as e:
            logs.append({"step": "Маршрутизатор", "status": "warning", "msg": f"Ошибка настройки маршрутизатора: {e}"})

    # 5. Immediate Verification
    time.sleep(1)
    yt_ok, yt_lat, yt_code = test_youtube()
    vless_cfg = APP_CONFIG.get("services", {}).get("vless", {})
    vless_ok, vless_lat, vless_ip = test_socks5(vless_cfg.get("socks_host", "127.0.0.1"), vless_cfg.get("socks_port", 10808))
    cc_ok, cc_lat, cc_status = test_cloudcode_api()

    logs.append({
        "step": "Диагностика",
        "status": "ok" if (yt_ok and vless_ok and cc_ok) else "warning",
        "msg": f"YouTube: {'OK (' + str(yt_lat) + 'ms)' if yt_ok else 'FAIL'}, VLESS: {'OK (' + str(vless_lat) + 'ms)' if vless_ok else 'FAIL'}, CloudCode Gemini: {'OK (' + str(cc_lat) + 'ms, HTTP ' + str(cc_status) + ')' if cc_ok else 'FAIL: ' + str(cc_status)}"
    })
    return logs

# Chat & AI Integration
def load_chat_config():
    config = {"api_key": "", "model": "gemini-2.5-flash"}
    if os.path.exists(CHAT_CONFIG_FILE):
        try:
            with open(CHAT_CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                config.update(saved)
        except Exception:
            pass
    env_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not config.get("api_key") and env_key:
        config["api_key"] = env_key
    return config

def save_chat_config(api_key, model="gemini-2.5-flash"):
    cfg = load_chat_config()
    if api_key is not None:
        cfg["api_key"] = api_key.strip()
    if model:
        cfg["model"] = model.strip()
    with open(CHAT_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return cfg

def get_context_document():
    if os.path.exists(CONTEXT_FILE):
        try:
            with open(CONTEXT_FILE, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            pass
    return "Файл контекста ANTIGRAVITY_EMERGENCY_CONTEXT.md недоступен на диске."

def execute_emergency_command(cmd_text):
    cmd = cmd_text.strip().lower()
    if cmd in ["/repair", "/fix"]:
        logs = run_self_repair()
        res = "### Результат экстренного восстановления\n\n"
        for item in logs:
            icon = "✓" if item["status"] == "ok" else ("▲" if item["status"] == "warning" else "✕")
            res += f"- **[{icon}] {item['step']}**: {item['msg']}\n"
        return res

    elif cmd == "/status":
        with telemetry_lock:
            snap = json.loads(json.dumps(telemetry_data))
        v = snap["bypasses"].get("vless", {})
        z = snap["bypasses"].get("zapret", {})
        pi = snap["pi_hardware"]
        mt = snap["mikrotik_hardware"]
        return (
            "### Состояние сетевой инфраструктуры\n\n"
            f"- **MikroTik**: Загрузка CPU {mt.get('cpu_load', 0)}%, ОЗУ {mt.get('ram_used_mb', 0)}MB / {mt.get('ram_total_mb', 0)}MB (Аптайм: {mt.get('uptime', 'N/A')})\n"
            f"- **Шлюз Linux**: {pi.get('temperature', 0)}°C, Нагрузка: {pi.get('cpu_load', '0')}, ОЗУ: {pi.get('ram_percent', 0)}% (Аптайм: {pi.get('uptime', 'N/A')})\n"
            f"- **Службы**: `{', '.join([f'{k}:{v}' for k, v in pi.get('services', {}).items()])}`\n"
            f"- **VLESS Reality**: {v.get('status', 'unknown')} | IP: `{v.get('external_ip', 'unknown')}` | Пинг: {v.get('latency', 0)} ms\n"
            f"- **ProxyBridge**: {v.get('proxybridge_status', 'unknown')}\n"
            f"- **YouTube Zapret**: {z.get('youtube_status', 'unknown')} ({z.get('youtube_latency', 0)} ms, HTTP {z.get('youtube_code', 0)})\n"
        )

    elif cmd in ["/test-ai", "/test-gemini"]:
        ok, lat, code = test_cloudcode_api()
        if ok:
            return f"✓ **Google CloudCode / Gemini Backend ДОСТУПЕН**\n- Задержка: `{lat} ms`\n- HTTP статус: `{code}` (штатный ответ CloudCode API)\n- Маршрут: через SOCKS5-туннель шлюза."
        else:
            return f"✕ **Сбой соединения с Google CloudCode API**\n- Ошибка: `{code}`\n- Рекомендация: выполните `/repair` для проверки туннеля."

    elif cmd == "/fix-pb":
        try:
            _, msg = enforce_proxybridge_profile()
            return f"✓ **ProxyBridge:** {msg}"
        except Exception as e:
            return f"✕ **Ошибка ProxyBridge:** {e}"

    elif cmd == "/fix-win":
        try:
            enforce_windows_direct_proxy()
            return "✓ **Windows Proxy:** Системный прокси отключен, WinINet сброшен в Direct (0x01)."
        except Exception as e:
            return f"✕ **Ошибка сброса Windows Proxy:** {e}"

    elif cmd == "/context":
        if os.path.exists(CONTEXT_FILE):
            try:
                with open(CONTEXT_FILE, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception:
                pass
        return (
            "### Справочный контекст сети\n\n"
            "1. **Клиент / IDE:** Прямое подключение к шлюзу без системного прокси.\n"
            "2. **Language Server:** Маршрутизация через SOCKS5 туннель шлюза.\n"
            "3. **Шлюз:** Zapret (DPI обход) + Xray (VLESS Reality).\n"
            "4. **Маршрутизатор:** Разделение трафика через FastTrack и Mangle-маркировку."
        )

    elif cmd == "/help":
        return (
            "### Доступные команды\n\n"
            "| Команда | Назначение |\n"
            "| :--- | :--- |\n"
            "| `/repair` | Полная экстренная починка всех узлов |\n"
            "| `/status` | Сводка телеметрии в реальном времени |\n"
            "| `/test-ai` | Проверка связи с Google CloudCode API |\n"
            "| `/fix-pb` | Восстановление правил ProxyBridge |\n"
            "| `/fix-win` | Сброс системного прокси Windows |\n"
            "| `/context` | Вывод эталонной шпаргалки |\n"
        )

    return None

def offline_diagnostic_reply(user_msg):
    with telemetry_lock:
        snap = json.loads(json.dumps(telemetry_data))
    
    msg_low = user_msg.lower()
    v = snap["bypasses"].get("vless", {})
    z = snap["bypasses"].get("zapret", {})
    pb_status = v.get("proxybridge_status", "")

    issues = []
    if "не запущен" in pb_status.lower():
        issues.append("▲ **ProxyBridge не запущен на ПК.** Antigravity не может связаться с AI-моделями Google без активного ProxyBridge.")
    if v.get("status") != "online":
        issues.append(f"✕ **VLESS Reality на шлюзе недоступен** (Статус: {v.get('status')}). Проверьте службу xray.")
    if z.get("youtube_status") != "OK":
        issues.append("▲ **YouTube DPI bypass сбоит.** Служба Zapret на шлюзе может требовать перезапуска.")

    diag_text = ""
    if issues:
        diag_text = "### Обнаруженные проблемы:\n" + "\n".join(issues) + "\n\n"

    if any(k in msg_low for k in ["недоступн", "antigravity", "модел", "working"]):
        return (
            diag_text +
            "### Почему Antigravity пишет «Модель недоступна»:\n\n"
            "Главная причина — **ловушка перехвата Electron**:\n"
            "1. Графическая оболочка `Antigravity.exe` общается только локально (`localhost:<port>`). Если ProxyBridge перехватывает её (`AGRule`), запрос улетает на внешний шлюз, возвращая `Connection Refused`.\n"
            "2. Работать с Google Gemini должен **ТОЛЬКО** бэкенд `language_server.exe` (`LRAG` в ProxyBridge).\n\n"
            "**Решение прямо сейчас:**\n"
            "- Нажмите кнопку **«Починить всё»** в шапке или отправьте `/repair`.\n"
            "- Проверьте связь с бэкендом командой `/test-ai`."
        )

    return (
        diag_text +
        "### Автономный ассистент NETPULSE:\n\n"
        "Телеметрия сети собрана. Вы можете:\n"
        "- Нажать кнопку **«Починить всё»** или отправить команду `/repair`\n"
        "- Проверить доступность серверов Google командой `/test-ai`\n"
        "- Посмотреть все команды с помощью `/help`\n\n"
        "*Совет:* Чтобы общаться в режиме свободного диалога через живую модель, укажите свой **Gemini API Key** в настройках (кнопка параметров в шапке чата)."
    )

def call_gemini_api(api_key, model, messages, system_prompt):
    v_cfg = APP_CONFIG.get("services", {}).get("vless", {})
    socks_h = v_cfg.get("socks_host", "127.0.0.1")
    socks_p = v_cfg.get("socks_port", 10808)
    proxies = {
        "http": f"socks5h://{socks_h}:{socks_p}",
        "https": f"socks5h://{socks_h}:{socks_p}"
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    
    contents = []
    for m in messages:
        role = "user" if m.get("role") == "user" else "model"
        contents.append({
            "role": role,
            "parts": [{"text": m.get("text", "")}]
        })

    payload = {
        "system_instruction": {
            "parts": [{"text": system_prompt}]
        },
        "contents": contents,
        "generationConfig": {
            "temperature": 0.4,
            "maxOutputTokens": 2048
        }
    }

    resp = requests.post(url, json=payload, proxies=proxies, timeout=25)
    if resp.status_code != 200:
        raise Exception(f"Gemini API returned HTTP {resp.status_code}: {resp.text}")

    data = resp.json()
    candidates = data.get("candidates", [])
    if not candidates:
        raise Exception("Gemini API вернул пустой ответ (нет кандидатов).")
    
    parts = candidates[0].get("content", {}).get("parts", [])
    if not parts:
        raise Exception("Пустое тело ответа от модели.")
    
    return parts[0].get("text", "")

# Web Handlers
async def handle_config(request):
    safe_cfg = {
        "app": APP_CONFIG.get("app", {}),
        "nodes": APP_CONFIG.get("nodes", []),
        "services": {k: {"enabled": v.get("enabled", True), "name": v.get("name", k)} for k, v in APP_CONFIG.get("services", {}).items()},
        "collectors": {k: {"enabled": v.get("enabled", False)} for k, v in APP_CONFIG.get("collectors", {}).items()}
    }
    return web.json_response(safe_cfg)

async def handle_status(request):
    with telemetry_lock:
        return web.json_response(telemetry_data)

async def handle_refresh(request):
    add_log("Запрос обновления телеметрии", "info")
    with telemetry_lock:
        return web.json_response(telemetry_data)

async def handle_repair(request):
    add_log("Запущено восстановление эталонной конфигурации", "warning")
    loop = asyncio.get_event_loop()
    logs = await loop.run_in_executor(None, run_self_repair)
    all_ok = all(l.get("status") in ["ok", "warning"] for l in logs)
    add_log("Восстановление завершено успешно" if all_ok else "Восстановление завершено с предупреждениями", "success" if all_ok else "error")
    return web.json_response({"success": all_ok, "logs": logs})

async def handle_chat(request):
    try:
        body = await request.json()
        user_msg = body.get("message", "").strip()
        history = body.get("history", [])

        if not user_msg:
            return web.json_response({"error": "Empty message"}, status=400)

        # 1. Check for emergency slash command
        if user_msg.startswith("/"):
            loop = asyncio.get_event_loop()
            cmd_reply = await loop.run_in_executor(None, execute_emergency_command, user_msg)
            if cmd_reply is not None:
                return web.json_response({"reply": cmd_reply, "source": "command"})

        # 2. Check for Gemini API key
        cfg = load_chat_config()
        api_key = cfg.get("api_key", "").strip()
        model = cfg.get("model", "gemini-2.5-flash").strip()

        if api_key:
            try:
                context_doc = get_context_document()
                with telemetry_lock:
                    snap_str = json.dumps(telemetry_data, ensure_ascii=False)

                sys_prompt = (
                    "Ты — встроенный сетевой ассистент и инженер Antigravity для панели NETPULSE.\n"
                    "Пользователь обращается к тебе во время сбоев сети, Antigravity IDE, ProxyBridge или прокси.\n\n"
                    "ЭТАЛОННЫЙ ДОКУМЕНТ СЕТИ:\n"
                    f"{context_doc}\n\n"
                    "ТЕКУЩАЯ ЖИВАЯ ТЕЛЕМЕТРИЯ (СНИМОК):\n"
                    f"{snap_str}\n\n"
                    "ИНСТРУКЦИИ:\n"
                    "1. Отвечай кратко, инженерно точно и строго по делу на русском языке в формате Markdown.\n"
                    "2. Если пользователь жалуется на 'модель недоступна', всегда проверяй: не включен ли случайно AGRule для Antigravity.exe (должен быть выключен!), запущен ли ProxyBridge, активен ли LRAG для language_server.exe, и работает ли туннель VLESS Reality.\n"
                    "3. Ты можешь предлагать пользователю выполнить команды: /repair, /status, /test-ai, /fix-pb, /fix-win."
                )

                messages = list(history)
                messages.append({"role": "user", "text": user_msg})

                loop = asyncio.get_event_loop()
                ai_reply = await loop.run_in_executor(None, call_gemini_api, api_key, model, messages, sys_prompt)
                return web.json_response({"reply": ai_reply, "source": "ai"})
            except Exception as e:
                logger.error(f"Gemini API call failed: {e}")
                fallback = (
                    f"▲ **Сбой вызова Gemini API:** `{e}`\n\n"
                    "Автономная диагностика:\n\n" +
                    offline_diagnostic_reply(user_msg)
                )
                return web.json_response({"reply": fallback, "source": "offline_fallback"})

        # 3. No API key configured -> Offline expert diagnostic engine
        offline_reply = offline_diagnostic_reply(user_msg)
        return web.json_response({"reply": offline_reply, "source": "offline"})

    except Exception as e:
        logger.error(f"Error handling chat: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def handle_chat_config(request):
    if request.method == "GET":
        cfg = load_chat_config()
        key = cfg.get("api_key", "")
        masked = (key[:6] + "..." + key[-4:]) if len(key) > 10 else ("Configured" if key else "")
        return web.json_response({
            "has_key": bool(key),
            "masked_key": masked,
            "model": cfg.get("model", "gemini-2.5-flash")
        })
    elif request.method == "POST":
        data = await request.json()
        api_key = data.get("api_key")
        model = data.get("model", "gemini-2.5-flash")
        save_chat_config(api_key, model)
        return web.json_response({"success": True})

async def handle_chat_context(request):
    content = get_context_document()
    return web.json_response({"context": content})

async def handle_index(request):
    index_path = os.path.join(os.path.dirname(__file__), "public", "index.html")
    with open(index_path, "r", encoding="utf-8") as f:
        html = f.read()
    return web.Response(text=html, content_type="text/html")

def run_server():
    app = web.Application()
    app.router.add_get("/api/config", handle_config)
    app.router.add_get("/api/status", handle_status)
    app.router.add_post("/api/refresh", handle_refresh)
    app.router.add_post("/api/repair", handle_repair)
    app.router.add_post("/api/chat", handle_chat)
    app.router.add_get("/api/chat/config", handle_chat_config)
    app.router.add_post("/api/chat/config", handle_chat_config)
    app.router.add_get("/api/chat/context", handle_chat_context)
    app.router.add_get("/", handle_index)
    
    t = threading.Thread(target=telemetry_worker, daemon=True)
    t.start()

    srv_port = APP_CONFIG.get("app", {}).get("port", 8888)
    srv_host = APP_CONFIG.get("app", {}).get("host", "127.0.0.1")
    logger.info(f"Starting NETPULSE web server on http://{srv_host}:{srv_port}")
    web.run_app(app, host=srv_host, port=srv_port)

if __name__ == "__main__":
    run_server()
