#!/usr/bin/env python3
"""GPU agent for the cluster dashboard (standard library only, Linux and Windows).

For machines running a third-party engine (llama.cpp, vLLM, Ollama ...) that reports no GPU data
itself. Serves GET /gpu as JSON: GPUs (nvidia-smi), CPU load and RAM.
The dashboard queries it automatically on port 9835 (config.json: gpu_agent_port).

  python3 gpu-agent.py [--port 9835] [--bind 0.0.0.0]

Read-only, no control. Run it on the LAN only (no authentication).
"""
import argparse
import json
import os
import platform
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FIELDS = ("index,name,utilization.gpu,memory.used,memory.total,power.draw,power.limit,temperature.gpu,"
          "pcie.link.gen.current,pcie.link.width.current,pcie.link.gen.max,driver_version")


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def gpus():
    smi = shutil.which("nvidia-smi") or (r"C:\Windows\System32\nvidia-smi.exe" if os.name == "nt" else None)
    if not smi:
        return [], None
    try:
        out = subprocess.run([smi, f"--query-gpu={FIELDS}", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return [], None
    res, driver = [], None
    for line in out.strip().splitlines():
        c = [x.strip() for x in line.split(",")]
        if len(c) < 12:
            continue
        driver = c[11]
        res.append({"index": int(c[0]), "name": c[1].replace("NVIDIA ", ""), "util": _f(c[2]),
                    "mem_used": (_f(c[3]) or 0) * 2**20, "mem_total": (_f(c[4]) or 0) * 2**20,
                    "power": _f(c[5]), "power_limit": _f(c[6]), "temp": _f(c[7]),
                    "pcie": f"Gen{c[8]} x{c[9]}" if c[8] not in ("", "[N/A]") else None,
                    "pcie_max": int(c[10]) if c[10].isdigit() else None})
    return res, driver


_CPU = {"prev": None, "pct": None}


def cpu_linux():
    with open("/proc/stat") as f:
        v = [int(x) for x in f.readline().split()[1:]]
    idle, total = v[3] + (v[4] if len(v) > 4 else 0), sum(v)
    p = _CPU["prev"]
    _CPU["prev"] = (idle, total)
    if p and total > p[1]:
        _CPU["pct"] = 100 * (1 - (idle - p[0]) / (total - p[1]))
    return _CPU["pct"]


def ram_linux():
    m = {}
    with open("/proc/meminfo") as f:
        for ln in f:
            k, v = ln.split(":", 1)
            m[k] = int(v.split()[0]) * 1024
    return m["MemTotal"] - m.get("MemAvailable", m["MemFree"]), m["MemTotal"]


def cpu_name():
    if platform.system() == "Linux":
        try:
            with open("/proc/cpuinfo") as f:
                for ln in f:
                    if ln.startswith("model name"):
                        return ln.split(":", 1)[1].strip()
        except OSError:
            pass
    return platform.processor() or None


def ram_windows():
    import ctypes

    class MS(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("a", ctypes.c_ulonglong), ("b", ctypes.c_ulonglong), ("c", ctypes.c_ulonglong),
                    ("d", ctypes.c_ulonglong), ("e", ctypes.c_ulonglong)]
    s = MS()
    s.dwLength = ctypes.sizeof(MS)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s))
    return s.ullTotalPhys - s.ullAvailPhys, s.ullTotalPhys


def snapshot():
    g, driver = gpus()
    d = {"agent": "cluster-dashboard-gpu-agent", "time": time.time(), "gpus": g, "driver": driver,
         "cpu_name": cpu_name(), "threads": os.cpu_count(), "cpu": None, "ram_used": None, "ram_total": None}
    try:
        if platform.system() == "Linux":
            d["cpu"] = cpu_linux()
            d["ram_used"], d["ram_total"] = ram_linux()
        elif os.name == "nt":
            d["ram_used"], d["ram_total"] = ram_windows()
    except Exception:
        pass
    return d


CACHE = {"at": 0, "data": None}
LOCK = threading.Lock()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.split("?")[0] != "/gpu":
            self.send_response(404)
            self.end_headers()
            return
        with LOCK:
            if time.time() - CACHE["at"] > 1.0:
                CACHE.update(at=time.time(), data=snapshot())
            body = json.dumps(CACHE["data"]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=9835)
    ap.add_argument("--bind", default="0.0.0.0")
    a = ap.parse_args()
    if platform.system() == "Linux":
        cpu_linux()                       # first sample as baseline
    print(f"gpu-agent auf :{a.port}", flush=True)
    ThreadingHTTPServer((a.bind, a.port), H).serve_forever()
