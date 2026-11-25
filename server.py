#!/usr/bin/env python3
"""Cluster dashboard: aggregates the telemetry of Maya, Basalt and Strata.

Queries /metrics and /v1/status per node (Maya also /api/savings), normalizes
to a common schema and serves it at /api/all. The API keys stay here.
Stdlib only.
"""
import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CFG_PATH = ROOT / "config.json"
if not CFG_PATH.exists():
    # New system: create the default config as a starting point (then adjust in the browser at /config)
    CFG_PATH.write_text((ROOT / "config.default.json").read_text())
    CFG_PATH.chmod(0o600)
    print("config.json was missing - created from config.default.json", flush=True)
CFG = json.loads(CFG_PATH.read_text())
CFG.pop("_comment", None)
CFG.pop("_kommentar", None)

# Adapters for third-party engines (llama.cpp, vLLM, SGLang, Ollama ...). Load explicitly by path:
# the service runs with `python3 -I`, so its own directory is not on the search path.
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("engines", ROOT / "engines.py")
engines = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(engines)


import socket
import threading


I18N_DIR = ROOT / "i18n"
_I18N = {}


def i18n_langs():
    out = []
    for f in sorted(I18N_DIR.glob("*.json")):
        try:
            out.append({"code": f.stem, "name": json.loads(f.read_text()).get("_meta.name", f.stem)})
        except Exception:
            pass
    return out


def T(lang, key, **p):
    """Text in language lang (fallback English, then the key)."""
    for code in (lang, "en"):
        if code not in _I18N:
            try:
                _I18N[code] = json.loads((I18N_DIR / f"{code}.json").read_text())
            except Exception:
                _I18N[code] = {}
        if key in _I18N[code]:
            s = _I18N[code][key]
            for k, v in p.items():
                s = s.replace("{" + k + "}", str(v))
            return s
    return key


def M(key, **p):
    """Message as (key, params) - translated into the client's language only when responding."""
    return (key, p)


def all_keys():
    keys = list(CFG.get("keys") or [])
    for f in CFG.get("key_files") or []:
        try:
            k = json.loads(Path(f).read_text()).get("api_key")
            if k and k not in keys:
                keys.append(k)
        except Exception:
            pass
    return keys


def http_json(url, key=None, timeout=None):
    req = urllib.request.Request(url)
    if key:
        req.add_header("Authorization", "Bearer " + key)
    with urllib.request.urlopen(req, timeout=timeout or CFG.get("timeout_s", 4)) as r:
        return json.loads(r.read())


def get(node, path):
    return http_json(node["url"] + path, node.get("key"))


def parse_ports(spec):
    """"8080-8099,1234" -> [8080, ..., 8099, 1234]; empty -> []."""
    out = []
    for part in str(spec or "").replace(" ", "").split(","):
        if not part:
            continue
        a, _, b = part.partition("-")
        a, b = int(a), int(b or a)
        if not (0 < a <= b <= 65535):
            raise ValueError(part)
        out += list(range(a, b + 1))
    return out


def enabled_engines(cfg=None):
    """Config step "Other engines": enabled third-party engines -> {service: ports}."""
    e = (cfg or CFG).get("engines") or {}
    return {k: v.get("ports", "") for k, v in e.items() if isinstance(v, dict) and v.get("enabled")}


def scan_ports(cfg=None):
    """Port range of our own engines plus the ports of enabled third-party engines."""
    cfg = cfg or CFG
    ports = parse_ports(cfg.get("scan_ports", "8080-8099"))
    for spec in enabled_engines(cfg).values():
        ports += parse_ports(spec)
    return sorted(set(ports))


def port_open(host, port):
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except OSError:
        return False


def private_host(host):
    """LAN/Tailscale targets only: API keys are sent there (like the v28 proxy: RFC1918)."""
    import ipaddress
    try:
        ips = {ai[4][0] for ai in socket.getaddrinfo(host, None)}
    except OSError:
        return False
    cgnat = ipaddress.ip_network("100.64.0.0/10")
    return bool(ips) and all(ipaddress.ip_address(i).is_private or ipaddress.ip_address(i) in cgnat for i in ips)


NEG = {}             # (host, port) -> time: no engine was running there (don't re-probe every round)
NEG_S = 600          # Ports that answer but are not an engine (gateways, websites): 10 min back-off
_GATEWAY = __import__("re").compile(r"gateway|proxy|router|balancer", __import__("re").I)


def probe(host, port):
    """Own engine (/health with max_context + /v1/status), otherwise a known third-party engine."""
    if time.time() - NEG.get((host, port), 0) < NEG_S:
        return None
    base = f"http://{host}:{port}"
    try:
        h = http_json(base + "/health", timeout=1.5)
    except Exception:
        h = None
    if isinstance(h, dict) and "max_context" not in h and (
            "backends" in h or _GATEWAY.search(str(h.get("service") or h.get("name") or ""))):
        NEG[(host, port)] = time.time()          # Gateway/proxy in front of other engines: no card of its own
        return None
    if not isinstance(h, dict) or "max_context" not in h:
        try:
            found = engines.detect(base, all_keys(), set(enabled_engines()))
        except Exception:
            found = None
        if not found:
            NEG[(host, port)] = time.time()
            return None
        return {"host": host, "port": port, "url": base, "key": found[1], "service": found[0], "adapter": True}
    for key in [None] + all_keys():
        try:
            st = http_json(base + "/v1/status", key, timeout=2)
        except Exception:
            continue
        service = (st.get("service") or h.get("service") or "llm").lower()
        return {"host": host, "port": port, "url": base, "key": key, "service": service}
    return {"host": host, "port": port, "url": base, "key": None, "service": (h.get("service") or "llm").lower(),
            "auth_error": "kein passender API-Key (config.json: keys / key_files)", "auth_code": "err.no_key"}


DISC = {}            # host -> {"at": t, "servers": [...]}
DISC_LOCK = threading.Lock()


DISC_BUSY = set()


def discover(machine, force=False):
    """Engines of a host. A stale result is returned immediately and refreshed in the background,
    so a slow scan (e.g. gateways with timeouts) does not hold up the query."""
    host = machine["host"]
    with DISC_LOCK:
        d = DISC.get(host)
        if d and not force:
            if time.time() - d["at"] >= CFG.get("rediscover_s", 30) and host not in DISC_BUSY:
                DISC_BUSY.add(host)
                threading.Thread(target=_rediscover, args=(machine,), daemon=True).start()
            return d["servers"]
    return _scan(machine, d["servers"] if d else [])


def _rediscover(machine):
    try:
        with DISC_LOCK:
            old = (DISC.get(machine["host"]) or {}).get("servers", [])
        _scan(machine, old)
    finally:
        DISC_BUSY.discard(machine["host"])


def _scan(machine, known):
    host = machine["host"]
    if not private_host(host):
        with DISC_LOCK:
            DISC[host] = {"at": time.time(), "servers": []}
        return []
    ports = scan_ports()
    keep = {s["port"]: s for s in known if not s.get("auth_error")}     # don't re-identify already known engines
    with ThreadPoolExecutor(16) as ex:
        open_ports = [p for p, ok in zip(ports, ex.map(lambda p: port_open(host, p), ports)) if ok]
        servers = [s for s in ex.map(lambda p: keep.get(p) or probe(host, p), open_ports) if s]
    with DISC_LOCK:
        DISC[host] = {"at": time.time(), "servers": servers}
    return servers


def num(v, default=None):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def gpus_of(m, st):
    """Per-GPU list in one format: Maya/Basalt provide hardware.gpus, Strata only flat fields."""
    hw = m.get("hardware") or {}
    hs = m.get("hardware_static") or {}
    names = {}
    for g in hs.get("gpus") or []:
        names[g.get("index")] = g.get("name")
    for g in m.get("gpu_map") or []:
        names.setdefault(g.get("index"), g.get("name"))
    out = []
    if hw.get("gpus"):
        for g in hw["gpus"]:
            i = g.get("index", len(out))
            out.append({
                "index": i,
                "name": names.get(i) or (hs.get("gpu_name") or "").split(" + ")[0] or "GPU",
                "util": num(g.get("util")), "mem_used": num(g.get("mem_used")),
                "mem_total": num(g.get("mem_total")), "temp": num(g.get("temp")),
                "power": num(g.get("power")), "power_limit": num(g.get("power_limit")),
                "pcie": f"Gen{g.get('pcie_gen')} x{g.get('pcie_width')}" if g.get("pcie_gen") else None,
                "pcie_max": g.get("pcie_gen_max"),
            })
    elif "gpu_util" in hw:
        out.append({
            "index": 0, "name": hs.get("gpu_name") or "GPU",
            "util": num(hw.get("gpu_util")), "mem_used": num(hw.get("gpu_mem_used")),
            "mem_total": num(hw.get("gpu_mem_total")), "temp": num(hw.get("gpu_temp")),
            "power": num(hw.get("gpu_power")), "power_limit": num(hw.get("gpu_power_limit")),
            "pcie": f"Gen{hw.get('gpu_pcie_gen')} x{hw.get('gpu_pcie_width')}" if hw.get("gpu_pcie_gen") else None,
            "pcie_max": hw.get("gpu_pcie_gen_max"),
        })
    elif (st.get("machine") or {}).get("gpu"):
        g = st["machine"]["gpu"]
        out.append({"index": 0, "name": g.get("name"), "util": g.get("util_pct"),
                    "mem_used": (g.get("used_mib") or 0) * 2**20, "mem_total": (g.get("total_mib") or 0) * 2**20,
                    "temp": g.get("temp_c"), "power": g.get("power_w"), "power_limit": None, "pcie": None})
    return out


def history_of(m):
    h = m.get("history") or {}
    res = {"tok_s": h.get("tok_s") or [], "prefill": h.get("prefill_tok_s_mean") or [],
           "cpu": h.get("cpu") or [], "gpu_util": h.get("gpu_util") or [],
           "gpu_temp": h.get("gpu_temp") or [], "gpu_power": h.get("gpu_power") or []}
    if not res["gpu_util"] and h.get("gpus"):          # Basalt: per GPU
        gs = h["gpus"]
        n = min(len(g.get("util") or []) for g in gs)
        res["gpu_util"] = [sum(g["util"][i] for g in gs) / len(gs) for i in range(n)]
        res["gpu_temp"] = [max(g["temp"][i] for g in gs) for i in range(n)]
        res["gpu_power"] = [sum(g["power"][i] for g in gs) for i in range(n)]
    return res


def normalize(node, m, st, sav):
    e = m.get("engine") or {}
    live = m.get("live") or {}
    hw = m.get("hardware") or {}
    hs = m.get("hardware_static") or {}
    t = m.get("totals") or {}
    prompt = num(t.get("prompt_tokens"), num(t.get("prompt"), 0))
    reused = num(t.get("reused"), 0)
    output = num(t.get("output_tokens"), num(t.get("written"), 0))
    pms, dms = num(t.get("prompt_ms"), 0), num(t.get("decode_ms"), 0)
    reqs = m.get("requests") or []
    # Drafts: Strata has totals; otherwise from the last requests
    d_off = num(t.get("drafts_offered"))
    d_acc = num(t.get("drafts_accepted"))
    drafts_scope = "gesamt"
    if d_off is None:
        d_off = sum(num(r.get("drafts_offered"), 0) for r in reqs)
        d_acc = sum(num(r.get("drafts_accepted"), 0) for r in reqs)
        drafts_scope = f"letzte {len(reqs)}"
    drafts_n = None if drafts_scope == "gesamt" else len(reqs)
    hits = [r["hit_rate"] for r in reqs if num(r.get("hit_rate")) is not None]
    tiers = (m.get("tiers") or {}).get("now") or {}
    peak = (t.get("peak_decode") or {}).get("tok_s")
    peak_scope = "seit Start" if peak is not None else "beobachtet"
    # Peaks as in Strata Fleet Monitor v28: only directly observed rates.
    # Prefill: prefill_tok_s_mean (live + history) or what the server itself reports as best/prefill_tok_s
    # - not derived as prompt_tokens minus reused.
    hist = m.get("history") or {}
    pf_obs = [num(live.get("prefill_tok_s_mean"), 0), num(((m.get("best") or {}).get("prefill") or {}).get("tok_s"), 0)]
    pf_obs += [num(x, 0) for x in hist.get("prefill_tok_s_mean") or []]
    pf_obs += [num(r.get("prefill_tok_s"), 0) for r in reqs]
    best_pf = max(pf_obs) or None
    dec_obs = [num(peak, 0), num(live.get("tok_s"), 0)] + [num(x, 0) for x in hist.get("tok_s") or []]
    dec_obs += [num(r.get("decode_tok_s"), 0) for r in reqs]
    peak = max(dec_obs) or None
    lt = st.get("last_timings") or {}
    cc = m.get("conversation_cache") or {}
    return {
        "id": node["id"], "name": node["name"], "role": node.get("role"), "host": node.get("host"),
        "ui": node.get("ui"), "online": True,
        "service": st.get("service"), "model": e.get("model") or st.get("model"),
        "model_label": e.get("model_label"), "engine": e.get("maya_version") or e.get("engine") or e.get("version") or st.get("engine"),
        "quant": e.get("quant"), "kv": e.get("kv"), "context": e.get("context") or e.get("max_context"),
        "speculation": e.get("speculation") or ([f"MTP ≤{e['mtp_max']}"] if e.get("mtp_max") else []),
        "expert_cache_mib": e.get("expert_cache_mib"), "experts": e.get("experts") or e.get("expert_total"),
        "vision": (st.get("vision") or {}).get("available"),
        "uptime_s": st.get("uptime_s"), "started": st.get("started"),
        "state": live.get("state") or ("loaded" if st.get("loaded") else "?"),
        "phase": live.get("phase"), "live_tok_s": num(live.get("tok_s")),
        "live_prefill": num(live.get("prefill_tok_s_mean")),
        "live_prompt": [live.get("prompt_read"), live.get("prompt_total") or live.get("prompt_tokens")],
        "live_generated": live.get("generated"),
        "queued": num(live.get("queued"), 0), "in_flight": (st.get("activity") or {}).get("in_flight", 0),
        "last_request_at": (st.get("activity") or {}).get("last_request_at"),
        "gpus": gpus_of(m, st),
        "cpu": num(hw.get("cpu")), "cpu_name": hs.get("cpu_name"), "threads": hs.get("threads"),
        "driver": hs.get("driver"), "cuda": hs.get("cuda"),
        "ram_used": num(hw.get("ram_used")), "ram_total": num(hw.get("ram_total")),
        "storage": m.get("storage"),
        "history": history_of(m),
        "totals": {
            "since": t.get("since"), "requests": num(t.get("requests"), 0),
            "prompt": prompt, "reused": reused, "output": output,
            "reuse_pct": reused / prompt * 100 if prompt else None,
            "decode_avg": output / dms * 1000 if dms else None,
            "prefill_avg": (prompt - reused) / pms * 1000 if pms else None,
            "drafts_offered": d_off, "drafts_accepted": d_acc,
            "draft_acc_pct": d_acc / d_off * 100 if d_off else None, "drafts_scope": drafts_scope, "drafts_n": drafts_n,
            "peak_decode": peak, "peak_scope": peak_scope, "best_prefill": best_pf,
        },
        "expert_hit": tiers.get("vram_hit") if tiers else (sum(hits) / len(hits) if hits else None),
        "expert_hit_scope": "jetzt (Tiers)" if tiers else f"Ø letzte {len(hits)}",
        "expert_hit_n": None if tiers else len(hits),
        "tiers": ({"vram_used": tiers.get("vram_used"), "vram_slots": tiers.get("vram_slots"),
                   "ram_used": tiers.get("ram_used"), "ram_slots": tiers.get("ram_slots"),
                   "ram_fetch": tiers.get("ram_fetch"), "disk": tiers.get("disk"),
                   "history_hit": (m.get("tiers") or {}).get("history", {}).get("vram_hit", [])[-60:]}
                  if tiers else None),
        "conv_cache": ({"requests": cc.get("requests"), "reused": cc.get("requests_reused")} if cc else None),
        "loop_guard": m.get("loop_guard"),
        "last": {"prompt_n": lt.get("prompt_n"), "cache_n": lt.get("cache_n"),
                 "prefill": lt.get("prompt_per_second"), "decode": lt.get("predicted_per_second"),
                 "at": lt.get("at")},
        "requests": [{
            "time": r.get("time"), "duration_s": r.get("duration_s"), "finish": r.get("finish"),
            "prompt": r.get("prompt_tokens"), "reused": r.get("reused"), "output": r.get("output_tokens"),
            "decode": r.get("decode_tok_s"),
            "prefill": r.get("prefill_tok_s") or (
                round((num(r.get("prompt_tokens"), 0) - num(r.get("reused"), 0)) / r["prompt_ms"] * 1000, 1)
                if num(r.get("prompt_ms")) else None),
            "hit": r.get("hit_rate"),
            "drafts": [r.get("drafts_accepted"), r.get("drafts_offered")],
        } for r in reqs[:8]],
        "savings": ({"usd": sav.get("usd"), "since": sav.get("since"),
                     "start_usd": (sav.get("start") or {}).get("usd"),
                     "prices": sav.get("prices")} if sav else None),
    }


def collect(machine, srv):
    t0 = time.time()
    svc = (CFG.get("services") or {}).get(srv["service"], {})
    dn, dc, du = engines.DEFAULTS.get(srv["service"], (srv["service"].title(), "#9d9daa", "/"))
    node = {"id": f'{srv["service"]}@{srv["host"]}:{srv["port"]}', "name": svc.get("name") or dn,
            "machine": machine.get("name"), "role": machine.get("role"), "host": srv["host"], "port": srv["port"],
            "color": svc.get("color") or dc, "ui": srv["url"] + svc.get("ui", du),
            "url": srv["url"], "key": srv["key"], "service": srv["service"]}
    pub = {k: v for k, v in node.items() if k not in ("url", "key")}
    if srv.get("auth_error"):
        return {**pub, "online": False, "error": srv["auth_error"], "error_code": "err.no_key", "latency_ms": 0}
    if srv.get("adapter"):
        try:
            d = {**engines.poll(srv, pub), **{k: pub[k] for k in ("machine", "port", "color", "service")}}
        except Exception as ex:
            d = {**pub, "online": False, "error": f"{type(ex).__name__}: {ex}"}
        d["latency_ms"] = round((time.time() - t0) * 1000)
        return d
    try:
        with ThreadPoolExecutor(3) as ex:
            fm = ex.submit(get, node, "/metrics")
            fs = ex.submit(get, node, "/v1/status")
            fv = ex.submit(get, node, "/api/savings")
            m, st = fm.result(), fs.result()
            try:
                sav = fv.result()
                sav = sav if isinstance(sav, dict) and "usd" in sav else None
            except Exception:
                sav = None
        d = {**normalize(pub, m, st, sav), **{k: pub[k] for k in ("machine", "port", "color", "service")}}
    except Exception as ex:
        d = {**pub, "online": False, "error": f"{type(ex).__name__}: {ex}"}
    d["latency_ms"] = round((time.time() - t0) * 1000)
    return d


def collect_machine(machine):
    servers = discover(machine)
    if not servers or not all(_alive(s) for s in servers):
        servers = discover(machine, force=True)
    if not servers:
        return [{"id": "none@" + machine["host"], "name": machine.get("name"), "machine": machine.get("name"),
                 "role": machine.get("role"), "host": machine["host"], "color": "#9d9daa", "ui": None,
                 "online": False, "latency_ms": 0,
                 "error": (f'kein LLM-Server gefunden (Ports {CFG.get("scan_ports", "8080-8099")})'
                           if private_host(machine["host"]) else "keine private LAN-/Tailscale-Adresse – nicht abgefragt"),
                 "error_code": "err.no_server" if private_host(machine["host"]) else "err.not_private",
                 "error_params": {"ports": CFG.get("scan_ports", "8080-8099")}}]
    with ThreadPoolExecutor(len(servers)) as ex:
        out = list(ex.map(lambda s: collect(machine, s), servers))
    fill_hardware(machine, out)
    if any(not d["online"] and not d.get("error_code") for d in out):
        with DISC_LOCK:
            DISC.pop(machine["host"], None)        # rescan on the next tick
    return out


AGENT = {}           # host -> {"at": t, "data": {...} | None}


def gpu_agent(host):
    """Optional GPU agent (bin/gpu-agent.py) on the host; remember a failure for 30 s."""
    port = CFG.get("gpu_agent_port", 9835)
    a = AGENT.get(host)
    if a and time.time() - a["at"] < (1.5 if a["data"] else CFG.get("rediscover_s", 30)):
        return a["data"]
    try:
        data = http_json(f"http://{host}:{port}/gpu", timeout=1.5)
    except Exception:
        data = None
    AGENT[host] = {"at": time.time(), "data": data if isinstance(data, dict) else None}
    return AGENT[host]["data"]


def fill_hardware(machine, nodes):
    """Third-party engines report no hardware. Take values from our own engine server on the same host,
    otherwise from the GPU agent."""
    need = [d for d in nodes if d.get("online") and d.get("adapter") and not d.get("gpus")]
    if not need:
        return
    donor = next((d for d in nodes if d.get("online") and not d.get("adapter") and d.get("gpus")), None)
    if donor:
        hw = {k: donor.get(k) for k in ("gpus", "cpu", "cpu_name", "threads", "driver", "cuda", "ram_used", "ram_total")}
        hist = {k: (donor.get("history") or {}).get(k, []) for k in ("cpu", "gpu_util", "gpu_temp", "gpu_power")}
        src = donor.get("name")
    else:
        a = gpu_agent(machine["host"])
        if not a:
            return
        hw = {k: a.get(k) for k in ("gpus", "cpu", "cpu_name", "threads", "driver", "cuda", "ram_used", "ram_total")}
        g = a.get("gpus") or []
        h = engines.push_hist("agent@" + machine["host"], cpu=a.get("cpu"),
                              gpu_util=sum(x.get("util") or 0 for x in g) / len(g) if g else 0,
                              gpu_temp=max((x.get("temp") or 0) for x in g) if g else 0,
                              gpu_power=sum(x.get("power") or 0 for x in g))
        hist = {k: h[k] for k in ("cpu", "gpu_util", "gpu_temp", "gpu_power")}
        src = "gpu-agent"
    for d in need:
        d.update(hw)
        d["history"] = {**d.get("history", {}), **hist}
        d["hw_source"] = src


def _alive(srv):
    return port_open(srv["host"], srv["port"])


PEAKS_FILE = ROOT / "peaks.json"
try:
    PEAKS = json.loads(PEAKS_FILE.read_text())
except Exception:
    PEAKS = {}


def remember_peaks(d):
    """Persist observed peaks per node (like v28 in localStorage, here server-side).
    A server restart (different `started`) resets nothing - these are the dashboard's all-time peaks."""
    if not d.get("online"):
        return
    t = d["totals"]
    p = PEAKS.setdefault(f'{d["host"]}:{d.get("port")}|{d.get("model")}', {})
    changed = False
    for k in ("peak_decode", "best_prefill"):
        v = t.get(k)
        if v and v > (p.get(k) or 0):
            p[k], p[k + "_at"], changed = v, time.time(), True
    t["peak_decode_all"] = p.get("peak_decode")
    t["best_prefill_all"] = p.get("best_prefill")
    if changed:
        try:
            PEAKS_FILE.write_text(json.dumps(PEAKS, indent=1))
        except Exception:
            pass


COLLECT = {"at": 0, "data": None}
COLLECT_LOCK = threading.Lock()


def collect_all():
    """Query all hosts in parallel. The result is cached briefly so several open browsers
    (phone, desktop, tunnel) don't each trigger their own query wave to all nodes."""
    with COLLECT_LOCK:
        if COLLECT["data"] and time.time() - COLLECT["at"] < min(1.5, CFG.get("poll_s", 2) * 0.75):
            return json.loads(json.dumps(COLLECT["data"]))
        with ThreadPoolExecutor(max(1, min(64, len(CFG["machines"])))) as ex:
            nodes = [d for lst in ex.map(collect_machine, CFG["machines"]) for d in lst]
        # OpenAI fallback whose model a properly detected engine already serves = proxy in front -> no card
        known = {str(d.get("model") or "").lower() for d in nodes if d.get("online") and d.get("service") != "openai"}
        nodes = [d for d in nodes if not (d.get("service") == "openai" and d.get("online")
                                          and str(d.get("model") or "").lower() in known)]
        for d in nodes:
            remember_peaks(d)
        COLLECT.update(at=time.time(), data={"time": time.time(), "nodes": nodes})
        return json.loads(json.dumps(COLLECT["data"]))



# ---------------------------------------------------------------- Miner (SRBMiner)
import subprocess
SHOT_LOCK = threading.Lock()   # only one headless Chrome at a time

MINER = {}             # host -> status
MINER_LOCK = threading.Lock()
MINER_BUSY = set()     # hosts with a refresh/action in progress


def _miner_run(host, cfg, what):
    """Run a command: systemd locally (polkit rule), otherwise via SSH key. Returns (rc, stdout)."""
    if cfg["kind"] == "systemd":
        if what == "status":
            try:
                with urllib.request.urlopen(cfg.get("api", "http://127.0.0.1:21550/"), timeout=3) as r:
                    return 0, r.read().decode("utf-8", "replace")
            except Exception:
                return 1, ""
        if what == "start":   # frozen by miner-gate? thaw first
            subprocess.run(["pkill", "-CONT", "-x", "SRBMiner-MULTI"], capture_output=True)
        # sudo -n: sudoers entry /etc/sudoers.d/miner-dashboard; if missing, fail instead of
        # asking for a password on the desktop (as polkit would)
        r = subprocess.run(["sudo", "-n", "/usr/bin/systemctl", what, cfg["unit"]], capture_output=True, text=True,
                           timeout=30)
        return r.returncode, r.stdout + r.stderr
    key = cfg.get("key", "")
    key = str(ROOT / key) if key and not key.startswith("/") else key
    cmd = ["ssh", "-i", key, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4",
           "-o", "StrictHostKeyChecking=accept-new", f'{cfg.get("user", "user")}@{host}', cfg[what]]
    r = subprocess.run(cmd, capture_output=True, timeout=30)
    return r.returncode, r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")


def _miner_parse(out):
    i = out.find("{")
    if i < 0:
        return None
    try:
        d = json.loads(out[i:out.rfind("}") + 1])
    except Exception:
        return None
    a = (d.get("algorithms") or [{}])[0]
    hr = a.get("hashrate") or {}
    return {"running": True, "algo": a.get("name"), "hashrate": hr.get("1min") or hr.get("now"),
            "hashrate_5m": hr.get("5min"), "shares": a.get("shares"),
            "pool": (a.get("pool") or {}).get("pool"), "latency": (a.get("pool") or {}).get("latency"),
            "uptime": d.get("mining_time"), "rig": d.get("rig_name"), "version": d.get("miner_version"),
            "gpus": [{"model": g.get("model"), "temp": g.get("temperature"), "power": g.get("asic_power"),
                      "fan": g.get("fan_speed_percent")} for g in d.get("gpu_devices") or []]}


def miner_refresh(host):
    cfg = (CFG.get("miners") or {}).get(host)
    if not cfg:
        return
    if cfg.get("enabled") is False:
        st = {"available": False, "note": cfg.get("note") or "deaktiviert", **({} if cfg.get("note") else {"note_code": "miner.disabled"})}
    else:
        try:
            rc, out = _miner_run(host, cfg, "status")
            p = _miner_parse(out)
            st = {"available": True, **(p or {"running": False})}
            # process running, API not up yet (SRBMiner needs a few seconds to start)
            if not p and cfg.get("process") and cfg["process"].lower() in out.lower():
                st.update(running=True, starting=True)
            if cfg["kind"] == "systemd":
                r = subprocess.run(["systemctl", "is-active", cfg["unit"]], capture_output=True, text=True)
                st["service"] = r.stdout.strip()
                st["running"] = st["running"] or st["service"] in ("active", "activating")
                # miner-gate freezes the miner via SIGSTOP (unit stays 'active'): report as paused
                r = subprocess.run(["ps", "-o", "stat=", "-C", "SRBMiner-MULTI"], capture_output=True, text=True)
                if r.stdout.strip().startswith("T"):
                    st.update(running=False, paused=True)
        except Exception as ex:
            st = {"available": False, "note": f"{type(ex).__name__}: {ex}"}
    st["at"] = time.time()
    with MINER_LOCK:
        MINER[host] = {**st, "pending": MINER.get(host, {}).get("pending")}
        MINER_BUSY.discard(host)


def miner_status():
    """Return the cache, refresh stale entries in the background (SSH takes ~0.5-1 s)."""
    now = time.time()
    for host in CFG.get("miners") or {}:
        with MINER_LOCK:
            st = MINER.get(host)
            stale = not st or now - st.get("at", 0) > 8
            if stale and host not in MINER_BUSY:
                MINER_BUSY.add(host)
                threading.Thread(target=miner_refresh, args=(host,), daemon=True).start()
    with MINER_LOCK:
        return {h: v for h, v in MINER.items() if h in (CFG.get("miners") or {})}


def miner_action(host, action):
    cfg = (CFG.get("miners") or {}).get(host)
    if not cfg or action not in ("start", "stop"):
        return False, M("srv.unknown_miner")
    if cfg.get("enabled") is False:
        return False, cfg.get("note") or M("miner.disabled")
    with MINER_LOCK:
        MINER.setdefault(host, {})["pending"] = action
    try:
        rc, out = _miner_run(host, cfg, action)
    except Exception as ex:
        rc, out = 1, str(ex)
    want = action == "start"
    for _ in range(15 if want else 6):                       # up to 30 s (start) or 12 s (stop)
        time.sleep(2)
        miner_refresh(host)
        with MINER_LOCK:
            if bool(MINER[host].get("running")) == want:
                break
    with MINER_LOCK:
        MINER[host]["pending"] = None
        st = dict(MINER[host])
    ok = st.get("running") == (action == "start")
    return ok, ("" if ok else f"rc={rc} {out.strip()[-300:]}")



# ---------------------------------------------------------------- Login (for the Cloudflare tunnel)
import hashlib
import hmac
from http.cookies import SimpleCookie

FAILS = {}          # client -> [timestamps]
FAIL_LOCK = threading.Lock()
TUNNEL_URL_FILE = ROOT / "tunnel.url"


def _sign(user, exp):
    msg = f"{user}|{exp}".encode()
    return hmac.new(bytes.fromhex(CFG["auth"]["secret"]), msg, hashlib.sha256).hexdigest()


def make_session(user):
    exp = int(time.time() + 3600 * CFG["auth"].get("session_h", 168))
    return f"{user}|{exp}|{_sign(user, exp)}"


def check_session(val):
    try:
        user, exp, sig = val.split("|")
        return (hmac.compare_digest(sig, _sign(user, int(exp))) and int(exp) > time.time()
                and user == CFG["auth"]["user"])
    except Exception:
        return False


def check_password(user, pw):
    a = CFG["auth"]
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(a["salt"]), int(a["iterations"])).hex()
    return hmac.compare_digest(h, a["hash"]) and hmac.compare_digest(user.encode(), a["user"].encode())


def locked(client):
    now = time.time()
    with FAIL_LOCK:
        f = [t for t in FAILS.get(client, []) if now - t < 600]
        FAILS[client] = f
        return len(f) >= 5          # 5 failed attempts in 10 min -> locked


def fail(client):
    with FAIL_LOCK:
        FAILS.setdefault(client, []).append(time.time())


LOGIN_HTML = """<!doctype html><html lang="%LANG%"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>%T:login.title%</title><style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#19191d;color:#ececf1;font:14px system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
form{background:#222227;border:1px solid #2e2e35;border-radius:16px;padding:28px;width:min(340px,90vw);display:grid;gap:12px}
h1{margin:0 0 4px;font-size:18px;font-weight:600} input{background:#1b1b20;border:1px solid #3d3d46;border-radius:8px;color:#ececf1;padding:9px 11px;font-size:14px}
input:focus{outline:none;border-color:#8b7cff} button{background:#8b7cff;color:#0b0c18;border:0;border-radius:999px;padding:9px;font-weight:600;font-size:14px;cursor:pointer}
.e{color:#fb7185;font-size:13px;min-height:1em} .l{text-align:center;font-size:12px} .l a{color:#9d9daa;margin:0 6px}</style></head><body>
<form method="post" action="login"><h1>%T:app.title%</h1>
<input name="user" placeholder="%T:login.user%" autocomplete="username" autofocus required>
<input name="password" type="password" placeholder="%T:login.password%" autocomplete="current-password" required>
<div class="e">%ERR%</div><button>%T:login.submit%</button><div class="l">%LANGS%</div></form></body></html>"""


def mask(k):
    k = str(k)
    return "•" * max(0, min(len(k), 12) - 2) + k[-2:] if len(k) > 2 else "•" * len(k)


def public_config():
    c = json.loads(json.dumps(CFG))
    c["keys"] = [{"index": i, "mask": mask(k)} for i, k in enumerate(CFG.get("keys") or [])]
    c.pop("auth", None)
    c.pop("miners", None)
    # all known third-party engines with default ports (for the config step "Other engines")
    c["engine_catalog"] = [{"id": k, "name": engines.DEFAULTS[k][0], "ports": engines.PORTS[k]} for k in engines.ORDER]
    c.setdefault("engines", {})
    c["rev"] = config_rev()
    return c


def config_rev():
    """Version of config.json - the config page sends it back on save (guards against stale tabs)."""
    import hashlib as _hl
    return _hl.sha256(CFG_PATH.read_bytes()).hexdigest()[:16]


EXPORT_VERSION = 1


def export_config(secrets):
    """Configuration as a file. Without secrets: no API keys, no login (password hash/secret).
    SSH keys themselves are never included, only their paths."""
    c = json.loads(json.dumps(CFG))
    if not secrets:
        c["keys"] = []
        c.pop("auth", None)
    c = {"_export": {"version": EXPORT_VERSION, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                     "secrets": bool(secrets), "source": "cluster-dashboard"}, **c}
    return c


def check_miners(miners):
    """Validate miner entries (they run commands - allow only known forms)."""
    import re
    out, err = {}, []
    if not isinstance(miners, dict):
        return {}, [M("srv.miners_obj")]
    for host, m in miners.items():
        if not isinstance(m, dict):
            err.append(M("srv.miner_obj", host=host)); continue
        kind = m.get("kind")
        if kind == "systemd":
            if not re.fullmatch(r"[\w@.-]+\.service", str(m.get("unit", ""))):
                err.append(M("srv.miner_unit", host=host)); continue
        elif kind == "ssh":
            miss = [k for k in ("user", "start", "stop", "status") if not str(m.get(k, "")).strip()]
            if miss:
                err.append(M("srv.miner_missing", host=host, fields=", ".join(miss))); continue
        else:
            err.append(M("srv.miner_kind", host=host)); continue
        out[str(host)] = m
    return out, err


def import_config(data, allow_privileged):
    """Apply an export file. Missing keys -> keep existing ones. Login (auth) is never imported,
    so you can't lock yourself out. Miner commands only from the LAN (not via the tunnel)."""
    if not isinstance(data, dict) or not isinstance(data.get("machines"), list):
        return False, [M("srv.no_config")], []
    notes = []
    new = {k: data.get(k, CFG.get(k)) for k in ("port", "host", "timeout_s", "poll_s", "rediscover_s",
                                                  "scan_ports", "key_files", "machines", "services", "engines")}
    imp_keys = [k for k in data.get("keys") or [] if isinstance(k, str) and k.strip()]
    if imp_keys:
        new["keys"] = [{"value": k} for k in imp_keys]
    else:
        new["keys"] = [{"keep": i} for i in range(len(CFG.get("keys") or []))]
        notes.append(M("srv.keys_kept"))
    miners = None
    if "miners" in data:
        if allow_privileged:
            miners, merr = check_miners(data["miners"])
            if merr:
                return False, merr, []
        else:
            notes.append(M("srv.miners_tunnel"))
    if "auth" in data:
        notes.append(M("srv.auth_never"))
    new["machines"] = [m for m in new.get("machines") or [] if isinstance(m, dict)]
    new["services"] = {k: v for k, v in (new.get("services") or {}).items() if isinstance(v, dict)} \
        if isinstance(new.get("services"), dict) else {}
    new["key_files"] = [f for f in new.get("key_files") or [] if isinstance(f, str)]
    try:
        ok, msgs = apply_config(new, miners=miners)
    except (TypeError, ValueError) as ex:
        return False, [M("srv.invalid_value", e=ex)], []
    return ok, msgs, notes + (msgs if ok else [])


def apply_config(new, miners=None):
    """Validate, assemble keys ({"keep": i} = old key i, {"value": "..."} = new), write, reload."""
    err = []
    machines = []
    for m in new.get("machines") or []:
        host = str(m.get("host", "")).strip()
        if not host:
            continue
        machines.append({"host": host, "name": str(m.get("name") or host).strip(), "role": str(m.get("role") or "").strip()})
    if not machines:
        err.append(M("srv.min_machine"))
    ports = str(new.get("scan_ports", "")).replace(" ", "")
    eng = {}
    for sid, ev in (new.get("engines") or {}).items():
        if sid in engines.PORTS and isinstance(ev, dict):
            eng[sid] = {"enabled": bool(ev.get("enabled")), "ports": str(ev.get("ports", "")).replace(" ", "")}
    try:
        n = len(scan_ports({"scan_ports": ports, "engines": eng}))
        if not 0 < n <= 2000:
            err.append(M("srv.port_range"))
    except Exception:
        err.append(M("srv.port_invalid"))
    keys = []
    for k in new.get("keys") or []:
        if isinstance(k, dict) and "keep" in k and 0 <= int(k["keep"]) < len(CFG.get("keys") or []):
            keys.append(CFG["keys"][int(k["keep"])])
        elif isinstance(k, dict) and str(k.get("value", "")).strip():
            keys.append(str(k["value"]).strip())
    services = {}
    for sid, sv in (new.get("services") or {}).items():
        sid = str(sid).strip().lower()
        if sid:
            services[sid] = {"name": str(sv.get("name") or sid.title()), "color": str(sv.get("color") or "#9d9daa"),
                             "ui": str(sv.get("ui") or "/")}
    if err:
        return False, err
    restart = int(new.get("port", CFG["port"])) != CFG["port"] or str(new.get("host", CFG["host"])) != CFG["host"]
    cfg = {**CFG, "port": int(new.get("port", CFG["port"])), "host": str(new.get("host", CFG["host"])),
           "timeout_s": max(1, min(30, float(new.get("timeout_s", CFG.get("timeout_s", 4))))),
           "poll_s": max(1, min(60, float(new.get("poll_s", CFG.get("poll_s", 2))))),
           "rediscover_s": max(5, min(3600, int(new.get("rediscover_s", CFG.get("rediscover_s", 30))))),
           "scan_ports": ports, "keys": keys, "engines": eng,
           "key_files": [str(f).strip() for f in new.get("key_files") or [] if str(f).strip()],
           "machines": machines, "services": services,
           "miners": miners if miners is not None else (CFG.get("miners") or {}),
           **({"auth": CFG["auth"]} if CFG.get("auth") else {})}
    path = ROOT / "config.json"
    try:   # backup contains keys + password hash -> readable by the owner only
        bak = ROOT / "config.json.bak"
        bak.write_text(path.read_text())
        bak.chmod(0o600)
    except Exception:
        pass
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
    tmp.chmod(0o600)
    tmp.replace(path)
    CFG.clear(); CFG.update(cfg)
    with DISC_LOCK:
        DISC.clear()
    return True, ([M("srv.restart")] if restart else [])


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _screenshot(self, q):
        """Full page as PNG: bin/shot.mjs (headless Chrome via CDP) measures the page height in the
        rendered layout itself and captures everything. Phone: real width, all tabs stacked."""
        import shutil
        import tempfile
        import urllib.parse
        a = urllib.parse.parse_qs(q)

        def num(k, d, lo, hi):
            v = (a.get(k) or [str(d)])[0]
            return max(lo, min(hi, int(v))) if v.isdigit() else d
        w = num("w", 1600, 320, 3840)
        dpr = num("dpr", 1, 1, 3)
        stil, hide = num("stil", 0, 0, 2), num("hideip", 0, 0, 1)
        mobile = 1 if w <= 640 else 0
        chrome = shutil.which("google-chrome") or shutil.which("chromium")
        node = shutil.which("node")
        if not chrome or not node:
            return self._send(500, T(self._lang(), "srv.shot_missing").encode(), "text/plain; charset=utf-8")
        with SHOT_LOCK, tempfile.TemporaryDirectory(prefix="cd-shot-") as tmp:
            out = Path(tmp) / "shot.png"
            url = f"http://127.0.0.1:{CFG.get('port', 8095)}/?stil={stil}&hideip={hide}&alle=1&kopf=1"
            try:
                r = subprocess.run([node, str(ROOT / "bin" / "shot.mjs"), url, str(w), str(dpr), str(mobile), str(out)],
                                   capture_output=True, text=True, timeout=90, env={**os.environ, "CHROME": chrome})
            except subprocess.TimeoutExpired:
                return self._send(504, T(self._lang(), "srv.shot_timeout").encode(), "text/plain; charset=utf-8")
            if r.returncode or not out.is_file():
                return self._send(500, T(self._lang(), "srv.shot_failed", e=r.stderr[-300:]).encode(), "text/plain; charset=utf-8")
            body = out.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Disposition",
                         f'attachment; filename="cluster-dashboard-{time.strftime("%Y-%m-%d_%H-%M-%S")}.png"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _log_cfg(self, what, ok):
        try:
            with open(ROOT / "config-changes.log", "a") as f:
                f.write(f'{time.strftime("%Y-%m-%d %H:%M:%S")} {what} ok={ok} von={self._client()}'
                        f'{" (Tunnel)" if self._via_tunnel() else ""} {self.headers.get("User-Agent", "")[:80]}\n')
        except Exception:
            pass

    def _lang(self):
        """Client language: ?lang= > cookie cd_lang > Accept-Language > English."""
        import re as _re
        codes = {l["code"] for l in i18n_langs()}
        q = dict(x.split("=", 1) for x in self.path.partition("?")[2].split("&") if "=" in x)
        c = SimpleCookie(self.headers.get("Cookie") or "")
        for cand in [q.get("lang"), c["cd_lang"].value if c.get("cd_lang") else None,
                     *_re.findall(r"([a-z]{2})", (self.headers.get("Accept-Language") or "").lower())]:
            if cand in codes:
                return cand
        return "en" if "en" in codes else (sorted(codes) or ["en"])[0]

    def _tr(self, x):
        if isinstance(x, (tuple, list)) and len(x) == 2 and isinstance(x[0], str) and isinstance(x[1], dict):
            return T(self._lang(), x[0], **x[1])
        return str(x)

    def _json(self, code, obj):
        if isinstance(obj, dict):   # messages (key + params) into the client's language
            for k in ("errors", "notes"):
                if isinstance(obj.get(k), list):
                    obj[k] = [self._tr(x) for x in obj[k]]
            if isinstance(obj.get("error"), (tuple, list)):
                obj["error"] = self._tr(obj["error"])
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    # --- Login
    def _client(self):
        return self.headers.get("Cf-Connecting-IP") or self.client_address[0]

    def _via_tunnel(self):
        return bool(self.headers.get("Cf-Ray") or self.headers.get("Cf-Connecting-IP"))

    def _need_login(self):
        a = CFG.get("auth")
        if not a:
            return False
        if a.get("require_for", "tunnel") == "tunnel" and not self._via_tunnel():
            return False
        c = SimpleCookie(self.headers.get("Cookie") or "")
        return not (c.get("cd_session") and check_session(c["cd_session"].value))

    def _deny(self, p):
        if p.startswith("/api/"):
            return self._json(401, {"ok": False, "errors": [M("srv.login_required")]})
        self.send_response(302)
        self.send_header("Location", "login")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _login_page(self, err=""):
        import html as _h
        import re as _re
        lang = self._lang()
        page = _re.sub(r"%T:([\w.]+)%", lambda m: _h.escape(T(lang, m.group(1))), LOGIN_HTML)
        langs = " ".join(f'<a href="login?lang={l["code"]}">{_h.escape(l["name"])}</a>' for l in i18n_langs())
        page = page.replace("%LANG%", lang).replace("%LANGS%", langs).replace("%ERR%", _h.escape(T(lang, err)) if err else "")
        self.send_response(200 if not err else 401)
        if "lang=" in (self.path.partition("?")[2]):
            self.send_header("Set-Cookie", f"cd_lang={lang}; Path=/; Max-Age=31536000; SameSite=Lax")
        body = page.encode()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _login_post(self):
        import urllib.parse
        n = int(self.headers.get("Content-Length") or 0)
        f = urllib.parse.parse_qs(self.rfile.read(min(n, 4096)).decode("utf-8", "replace"))
        client = self._client()
        if locked(client):
            return self._login_page("login.locked")
        if not check_password((f.get("user") or [""])[0], (f.get("password") or [""])[0]):
            fail(client)
            time.sleep(1)
            return self._login_page("login.wrong")
        self.send_response(302)
        secure = "; Secure" if self._via_tunnel() else ""
        self.send_header("Set-Cookie", f"cd_session={make_session(CFG['auth']['user'])}; Path=/; HttpOnly; SameSite=Lax; "
                                       f"Max-Age={3600 * CFG['auth'].get('session_h', 168)}{secure}")
        self.send_header("Location", "./")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        p, _, q = self.path.partition("?")
        if p == "/login":
            return self._login_page()
        if p == "/logout":
            self.send_response(302)
            self.send_header("Set-Cookie", "cd_session=; Path=/; Max-Age=0")
            self.send_header("Location", "login")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if p == "/i18n.js":
            return self._send(200, (ROOT / "i18n.js").read_bytes(), "text/javascript; charset=utf-8")
        if p == "/i18n/index.json":
            return self._json(200, i18n_langs())
        if p.startswith("/i18n/") and p.endswith(".json"):
            import re as _re
            code = p[6:-5]
            f = I18N_DIR / f"{code}.json"
            if _re.fullmatch(r"[a-z]{2}(-[A-Z]{2})?", code) and f.is_file():
                return self._send(200, f.read_bytes(), "application/json; charset=utf-8")
            return self._send(404, b"not found", "text/plain")
        if self._need_login():
            return self._deny(p)
        if p == "/api/screenshot":
            return self._screenshot(q)
        if p == "/api/all":
            d = collect_all()
            d["poll_s"] = CFG.get("poll_s", 2)
            d["miners"] = miner_status()
            d["remote"] = self._via_tunnel()
            self._json(200, d)
        elif p == "/api/config":
            self._json(200, public_config())
        elif p == "/api/config/export":
            secrets = "secrets=1" in q
            if secrets and self._via_tunnel():
                return self._json(403, {"ok": False, "errors": [M("srv.export_lan")]})
            body = json.dumps(export_config(secrets), indent=2, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Disposition", f'attachment; filename="cluster-dashboard-config'
                             f'{"-WITH-SECRETS" if secrets else ""}-{time.strftime("%Y-%m-%d")}.json"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif p in ("/", "/index.html"):
            self._send(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif p in ("/config", "/config.html"):
            self._send(200, (ROOT / "config.html").read_bytes(), "text/html; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path.split("?")[0] == "/login":
            return self._login_post()
        if self._need_login():
            return self._deny(self.path.split("?")[0])
        # CSRF: only from this page (reject foreign origins; curl without Origin is ok)
        origin = self.headers.get("Origin")
        if origin and origin.split("://", 1)[-1] != self.headers.get("Host", ""):
            return self._json(403, {"ok": False, "errors": [M("srv.foreign_origin")]})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(n, 1 << 20)) or b"{}")
        except Exception:
            return self._json(400, {"ok": False, "errors": [M("srv.no_json")]})
        p = self.path.split("?")[0]
        if p == "/api/config":
            if body.get("rev") and body["rev"] != config_rev():
                return self._json(409, {"ok": False, "errors": [M("srv.stale")], "config": public_config()})
            ok, msgs = apply_config(body)
            self._log_cfg("save", ok)
            return self._json(200 if ok else 400, {"ok": ok, ("notes" if ok else "errors"): msgs,
                                                   "config": public_config()})
        if p == "/api/config/import":
            ok, msgs, notes = import_config(body, allow_privileged=not self._via_tunnel())
            self._log_cfg("import", ok)
            return self._json(200 if ok else 400, {"ok": ok, ("notes" if ok else "errors"): (notes if ok else msgs),
                                                   "config": public_config()})
        if p == "/api/miner":
            host, action = str(body.get("host", "")), str(body.get("action", ""))
            ok, err = miner_action(host, action)
            err = self._tr(err) if isinstance(err, tuple) else err
            try:
                with open(ROOT / "miner-actions.log", "a") as f:
                    f.write(f'{time.strftime("%Y-%m-%d %H:%M:%S")} {action} {host} ok={ok} von={self._client()}'
                            f'{" (Tunnel)" if self._via_tunnel() else ""} {err[:120]}\n')
            except Exception:
                pass
            return self._json(200 if ok else 500, {"ok": ok, "error": err, "miners": miner_status()})
        if p == "/api/discover":
            host = str(body.get("host", "")).strip()
            if not host:
                return self._json(400, {"ok": False, "errors": [M("srv.host_missing")]})
            m = next((x for x in CFG["machines"] if x["host"] == host), {"host": host, "name": host})
            t0 = time.time()
            for key in [k for k in NEG if k[0] == host]:     # "Scan now" = really scan afresh
                NEG.pop(key, None)
            with DISC_LOCK:
                DISC.pop(host, None)
            srv = discover(m, force=True)
            return self._json(200, {"ok": True, "host": host, "ms": round((time.time() - t0) * 1000),
                                    "servers": [{"port": s["port"], "service": s["service"],
                                                 "auth": "fehlt" if s.get("auth_error") else
                                                 ("ohne Key" if not s["key"] else mask(s["key"])),
                                                 "auth_state": "missing" if s.get("auth_error") else ("none" if not s["key"] else "ok"),
                                                 "key_mask": mask(s["key"]) if s.get("key") else None,
                                                 "error": s.get("auth_error")} for s in srv]})
        self._json(404, {"ok": False})


def set_password(user):
    """Create/change the login for tunnel access: python3 server.py --set-password USER"""
    import getpass
    import secrets as _s
    pw = getpass.getpass("New password: ")
    if len(pw) < 10 or pw != getpass.getpass("Repeat: "):
        raise SystemExit("aborted: password too short (min. 10) or mismatch")
    salt = _s.token_bytes(16)
    it = 310000
    CFG["auth"] = {**(CFG.get("auth") or {"session_h": 168, "require_for": "tunnel"}), "user": user,
                   "salt": salt.hex(), "iterations": it,
                   "hash": hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, it).hex(),
                   "secret": _s.token_hex(32)}          # new secret = old sessions invalid
    tmp = CFG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(CFG, indent=2, ensure_ascii=False))
    tmp.chmod(0o600)
    tmp.replace(CFG_PATH)
    print(f"Login for '{user}' saved. Restart the service: systemctl --user restart cluster-dashboard")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--set-password":
        set_password(sys.argv[2])
        raise SystemExit(0)
    srv = ThreadingHTTPServer((CFG.get("host", "0.0.0.0"), CFG.get("port", 8095)), H)
    print(f"cluster-dashboard on :{CFG.get('port', 8095)}", flush=True)
    srv.serve_forever()
