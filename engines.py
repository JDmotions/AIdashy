"""Adapter fuer fremde Inferenz-Engines (nur Standardbibliothek).

Jede Engine wird beim Port-Scan erkannt (detect) und bei jeder Abfrage in dasselbe Node-Format
uebersetzt, das die eigenen Engines (Maya/Basalt/Strata) liefern (poll). Prometheus-Zaehler werden
zwischen zwei Abfragen zu Raten (tok/s); ein fallender Zaehler bedeutet Neustart der Engine.

Unterstuetzt: llama.cpp (llama-server), vLLM, SGLang, Hugging Face TGI, TabbyAPI, Ollama, LM Studio,
KoboldCpp, NVIDIA Triton, Aphrodite (vLLM-artig) und als Rueckfall jede OpenAI-kompatible API (/v1/models).
Quellen der Endpunkte und Metriknamen: siehe README, Abschnitt "Andere Engines".
"""
import json
import re
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from os.path import basename

TIMEOUT = 1.5

# Anzeige, wenn in config.json -> services nichts eingetragen ist
DEFAULTS = {
    "llamacpp": ("llama.cpp", "#f59e0b", "/"),
    "vllm": ("vLLM", "#60a5fa", "/docs"),
    "sglang": ("SGLang", "#f472b6", "/docs"),
    "tgi": ("TGI", "#facc15", "/docs"),
    "tabbyapi": ("TabbyAPI", "#a78bfa", "/docs"),
    "ollama": ("Ollama", "#e5e7eb", "/"),
    "lmstudio": ("LM Studio", "#818cf8", "/"),
    "koboldcpp": ("KoboldCpp", "#fb923c", "/"),
    "triton": ("Triton", "#76b900", "/v2"),
    "aphrodite": ("Aphrodite", "#2dd4bf", "/docs"),
    "openai": ("OpenAI-API", "#9d9daa", "/v1/models"),
}


# Standard-Ports der Engines (Vorbelegung im Konfig-Schritt "Andere Engines")
PORTS = {
    "llamacpp": "8080-8099", "vllm": "8000", "sglang": "30000", "tgi": "3000,8080", "tabbyapi": "5000",
    "ollama": "11434", "lmstudio": "1234", "koboldcpp": "5001", "triton": "8000", "aphrodite": "2242",
    "openai": "",
}
ORDER = list(PORTS)


# ------------------------------------------------------------------ HTTP
def _get(base, path, key=None, raw=False, timeout=TIMEOUT):
    req = urllib.request.Request(base + path, headers={"Accept": "application/json, text/plain"})
    if key:
        req.add_header("Authorization", "Bearer " + key)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read(4_000_000).decode("utf-8", "replace")
    return body if raw else json.loads(body)


def get_any(base, path, keys, raw=False, timeout=TIMEOUT):
    """Erst ohne Key, bei 401/403 der Reihe nach mit den Keys. -> (Antwort, benutzter Key)."""
    last = None
    for k in [None] + list(keys or []):
        try:
            return _get(base, path, k, raw, timeout), k
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (401, 403):
                continue
            raise
    raise last


def _try(base, path, keys=(), raw=False, timeout=TIMEOUT):
    try:
        return get_any(base, path, keys, raw, timeout)
    except Exception:
        return None, None


# ------------------------------------------------------------------ Prometheus
_LINE = re.compile(r'^([a-zA-Z_:][\w:]*)(\{.*\})?\s+(\S+)')
_LABEL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


def parse_prom(text):
    """{name: [(labels_dict, value), ...]} - nur Werte, keine Typen."""
    out = {}
    for ln in (text or "").splitlines():
        if not ln or ln[0] == "#":
            continue
        m = _LINE.match(ln)
        if not m:
            continue
        try:
            v = float(m.group(3))
        except ValueError:
            continue
        if v != v:          # NaN
            continue
        out.setdefault(m.group(1), []).append((dict(_LABEL.findall(m.group(2) or "")), v))
    return out


def psum(P, *names):
    """Summe ueber alle Label-Kombinationen der ersten vorhandenen Metrik."""
    for n in names:
        if n in P:
            return sum(v for _, v in P[n])
    return None


def plabel(P, label, *names):
    for n in names:
        for lab, _ in P.get(n, []):
            if lab.get(label):
                return lab[label]
    return None


def pmean(P, base):
    """Mittelwert eines Histogramms/Summary seit Start: _sum/_count."""
    s, c = psum(P, base + "_sum"), psum(P, base + "_count")
    return s / c if s is not None and c else None


# ------------------------------------------------------------------ Raten + Verlauf
_STATE = {}
_HIST = {}
_LOCK = threading.Lock()
HIST_KEYS = ("tok_s", "prefill", "cpu", "gpu_util", "gpu_temp", "gpu_power")


def rates(node_id, counters):
    """Raten pro Sekunde seit der letzten Abfrage; Zaehler-Ruecksprung = Neustart -> keine Rate."""
    now = time.time()
    with _LOCK:
        prev = _STATE.get(node_id)
        if prev and now - prev["t"] < 0.8:          # mehrfacher Aufruf kurz hintereinander
            return prev.get("r", {})
        r = {}
        if prev:
            dt = now - prev["t"]
            for k, v in counters.items():
                pv = prev["c"].get(k)
                if v is not None and pv is not None and v >= pv:
                    r[k] = (v - pv) / dt
        _STATE[node_id] = {"t": now, "c": counters, "r": r}
        return r


def push_hist(node_id, **vals):
    with _LOCK:
        h = _HIST.setdefault(node_id, {k: deque(maxlen=60) for k in HIST_KEYS})
        for k, v in vals.items():
            if k in h:
                h[k].append(v if v is not None else 0)
        return {k: list(v) for k, v in h.items()}


# ------------------------------------------------------------------ leeres Node-Geruest
def blank(pub, engine=None):
    return {
        "id": pub["id"], "name": pub["name"], "role": pub.get("role"), "host": pub.get("host"),
        "ui": pub.get("ui"), "online": True, "service": pub.get("service"), "adapter": pub.get("service"),
        "model": None, "model_label": None, "engine": engine, "quant": None, "kv": None, "context": None,
        "speculation": [], "expert_cache_mib": None, "experts": None, "vision": None,
        "uptime_s": None, "started": None, "state": "idle", "phase": None,
        "live_tok_s": None, "live_prefill": None, "live_prompt": [None, None], "live_generated": None,
        "queued": 0, "in_flight": 0, "last_request_at": None,
        "gpus": [], "cpu": None, "cpu_name": None, "threads": None, "driver": None, "cuda": None,
        "ram_used": None, "ram_total": None, "storage": None,
        "history": {k: [] for k in HIST_KEYS},
        "totals": {"since": None, "requests": None, "prompt": None, "reused": None, "output": None,
                   "reuse_pct": None, "decode_avg": None, "prefill_avg": None,
                   "drafts_offered": None, "drafts_accepted": None, "draft_acc_pct": None,
                   "drafts_scope": "gesamt", "drafts_n": None,
                   "peak_decode": None, "peak_scope": "beobachtet", "best_prefill": None},
        "expert_hit": None, "expert_hit_scope": "", "expert_hit_n": None, "tiers": None,
        "conv_cache": None, "loop_guard": None,
        "last": {"prompt_n": None, "cache_n": None, "prefill": None, "decode": None, "at": None},
        "requests": [], "savings": None,
        "kv_usage": None, "ttft_ms": None, "models_loaded": None,
    }


def _finish(d, busy, dec_rate, pf_rate=None):
    """Live-Zustand, Verlauf und beobachtete Peaks setzen."""
    d["state"] = "busy" if busy else "idle"
    d["phase"] = "decode" if busy and dec_rate else ("prefill" if busy else None)
    d["live_tok_s"] = dec_rate if busy and dec_rate else None
    d["live_prefill"] = pf_rate if busy and pf_rate else None
    h = push_hist(d["id"], tok_s=dec_rate or 0, prefill=pf_rate or 0)
    d["history"].update({k: h[k] for k in ("tok_s", "prefill")})
    d["totals"]["peak_decode"] = max(h["tok_s"]) or None
    d["totals"]["best_prefill"] = max(h["prefill"]) or None
    return d


def _counter_totals(d, prompt, output, requests=None, p_sec=None, o_sec=None, reused=None):
    t = d["totals"]
    t.update(prompt=prompt, output=output, requests=requests, reused=reused)
    if reused is not None and prompt:
        t["reuse_pct"] = reused / prompt * 100
    if o_sec:
        t["decode_avg"] = (output or 0) / o_sec
    if p_sec:
        t["prefill_avg"] = (prompt or 0) / p_sec


# ------------------------------------------------------------------ Engines
def _prom_family(d, P, srv, pre):
    """llama.cpp und TabbyAPI teilen sich dieses Zaehlerschema (Praefix llamacpp: / tabbyapi:)."""
    pt, gen = psum(P, pre + "prompt_tokens_total"), psum(P, pre + "tokens_predicted_total")
    ps, gs = psum(P, pre + "prompt_seconds_total"), psum(P, pre + "tokens_predicted_seconds_total")
    cached = psum(P, pre + "prompt_tokens_cached_total")
    _counter_totals(d, pt, gen, psum(P, pre + "requests_total"), ps, gs, cached)
    acc, off = psum(P, pre + "spec_decode_accepted_tokens_total", pre + "n_drafted_accepted_total"), \
        psum(P, pre + "spec_decode_draft_tokens_total", pre + "n_drafted_total")
    if off:
        d["totals"].update(drafts_offered=off, drafts_accepted=acc, draft_acc_pct=(acc or 0) / off * 100)
    r = rates(d["id"], {"gen": gen, "pt": pt, "gs": gs, "ps": ps})
    # reine Rechen-Rate (Tokens pro Rechensekunde) statt Wanduhr-Rate, wenn verfuegbar
    dec = r["gen"] / r["gs"] if r.get("gs") else r.get("gen")
    pf = r["pt"] / r["ps"] if r.get("ps") else r.get("pt")
    running = psum(P, pre + "requests_processing") or 0
    d["queued"] = psum(P, pre + "requests_deferred") or 0
    return running, dec, pf


def poll_llamacpp(srv, pub):
    u, k = srv["url"], srv.get("key")
    d = blank(pub, "llama.cpp")
    props = _get(u, "/props", k)
    dgs = props.get("default_generation_settings") or {}
    d["context"] = dgs.get("n_ctx") or props.get("n_ctx")
    name = str(props.get("model_alias") or props.get("model_path") or "")
    d["model"] = basename(name.replace("\\", "/")) or None          # Alias ist oft der volle Pfad
    if props.get("build_info"):
        d["engine"] = "llama.cpp " + str(props["build_info"])
    slots, _ = _try(u, "/slots", [k] if k else [])
    busy_slots = sum(1 for s in slots if isinstance(s, dict) and s.get("is_processing")) if isinstance(slots, list) else 0
    if isinstance(slots, list):
        d["speculation"] = [f"{len(slots)} Slots"]
    text, _ = _try(u, "/metrics", [k] if k else [], raw=True)
    running, dec, pf = _prom_family(d, parse_prom(text), srv, "llamacpp:") if text else (0, None, None)
    d["in_flight"] = max(int(running), busy_slots)
    return _finish(d, d["in_flight"] > 0, dec, pf)


def poll_tabbyapi(srv, pub):
    u, k = srv["url"], srv.get("key")
    d = blank(pub, "TabbyAPI")
    m, _ = _try(u, "/v1/model", [k] if k else [])
    if isinstance(m, dict):
        d["model"] = m.get("id")
        p = m.get("parameters") or {}
        d["context"] = p.get("max_seq_len")
        d["kv"] = p.get("cache_mode")
    st, _ = _try(u, "/v1/status", [k] if k else [])
    if isinstance(st, dict) and isinstance(st.get("backend"), dict):
        d["engine"] = "TabbyAPI · " + str(st["backend"].get("name", ""))
    text, _ = _try(u, "/metrics", [k] if k else [], raw=True)
    running, dec, pf = _prom_family(d, parse_prom(text), srv, "tabbyapi:") if text else (0, None, None)
    d["in_flight"] = int(running)
    return _finish(d, running > 0, dec, pf)


def _vllm_like(d, P, pre):
    running = psum(P, pre + "num_requests_running") or 0
    d["queued"] = psum(P, pre + "num_requests_waiting") or 0
    kv = psum(P, pre + "kv_cache_usage_perc", pre + "gpu_cache_usage_perc")
    d["kv_usage"] = kv * 100 if kv is not None else None
    pt, gen = psum(P, pre + "prompt_tokens_total"), psum(P, pre + "generation_tokens_total")
    hits = psum(P, pre + "prefix_cache_hits_total", pre + "gpu_prefix_cache_hits_total")
    q = psum(P, pre + "prefix_cache_queries_total", pre + "gpu_prefix_cache_queries_total")
    _counter_totals(d, pt, gen, psum(P, pre + "request_success_total"))
    if hits is not None and q:
        d["totals"]["reuse_pct"] = hits / q * 100
    ttft = pmean(P, pre + "time_to_first_token_seconds")
    d["ttft_ms"] = ttft * 1000 if ttft is not None else None
    acc, drafts = psum(P, pre + "spec_decode_num_accepted_tokens_total"), psum(P, pre + "spec_decode_num_draft_tokens_total")
    if drafts:
        d["totals"].update(drafts_offered=drafts, drafts_accepted=acc, draft_acc_pct=(acc or 0) / drafts * 100)
    d["model"] = plabel(P, "model_name", pre + "num_requests_running", pre + "generation_tokens_total")
    r = rates(d["id"], {"gen": gen, "pt": pt})
    d["in_flight"] = int(running)
    return running, r.get("gen"), r.get("pt")


def _openai_model(d, u, k):
    m, _ = _try(u, "/v1/models", [k] if k else [])
    if isinstance(m, dict) and m.get("data"):
        first = m["data"][0]
        d["model"] = d["model"] or first.get("id")
        d["context"] = d["context"] or first.get("max_model_len") or first.get("context_length")
        if len(m["data"]) > 1:
            d["models_loaded"] = [x.get("id") for x in m["data"]][:8]


def poll_vllm(srv, pub, pre="vllm:", label="vLLM"):
    u, k = srv["url"], srv.get("key")
    d = blank(pub, label)
    text, _ = get_any(u, "/metrics", [k] if k else [], raw=True)
    P = parse_prom(text)
    running, dec, pf = _vllm_like(d, P, pre)
    _openai_model(d, u, k)
    v, _ = _try(u, "/version")
    if isinstance(v, dict) and v.get("version"):
        d["engine"] = f"{label} {v['version']}"
    return _finish(d, running > 0, dec, pf)


def poll_aphrodite(srv, pub):
    return poll_vllm(srv, pub, "aphrodite:", "Aphrodite")


def poll_sglang(srv, pub):
    u, k = srv["url"], srv.get("key")
    d = blank(pub, "SGLang")
    info, _ = _try(u, "/get_server_info", [k] if k else [])
    if isinstance(info, dict):
        d["model"] = basename(str(info.get("served_model_name") or info.get("model_path") or "")) or None
        d["context"] = info.get("context_length")
        if info.get("version"):
            d["engine"] = f"SGLang {info['version']}"
        d["kv"] = info.get("kv_cache_dtype")
    text, _ = _try(u, "/metrics", [k] if k else [], raw=True)
    running = dec = pf = 0
    if text:
        P = parse_prom(text)
        running = psum(P, "sglang:num_running_reqs") or 0
        d["queued"] = psum(P, "sglang:num_queue_reqs") or 0
        tu = psum(P, "sglang:token_usage")
        d["kv_usage"] = tu * 100 if tu is not None else None
        pt, gen = psum(P, "sglang:prompt_tokens_total"), psum(P, "sglang:generation_tokens_total")
        _counter_totals(d, pt, gen, psum(P, "sglang:num_requests_total"))
        chr_ = psum(P, "sglang:cache_hit_rate")
        if chr_ is not None:
            d["totals"]["reuse_pct"] = chr_ * 100
        ttft = pmean(P, "sglang:time_to_first_token_seconds")
        d["ttft_ms"] = ttft * 1000 if ttft is not None else None
        r = rates(d["id"], {"gen": gen, "pt": pt})
        dec = psum(P, "sglang:gen_throughput") or r.get("gen")
        pf = r.get("pt")
    _openai_model(d, u, k)
    d["in_flight"] = int(running)
    return _finish(d, running > 0, dec, pf)


def poll_tgi(srv, pub):
    u, k = srv["url"], srv.get("key")
    d = blank(pub, "TGI")
    info, _ = _try(u, "/info", [k] if k else [])
    if isinstance(info, dict):
        d["model"] = info.get("model_id")
        d["context"] = info.get("max_total_tokens") or info.get("max_input_tokens")
        if info.get("version"):
            d["engine"] = f"TGI {info['version']}"
        d["quant"] = info.get("model_dtype")
    text, _ = _try(u, "/metrics", [k] if k else [], raw=True)
    running = dec = pf = 0
    if text:
        P = parse_prom(text)
        running = psum(P, "tgi_batch_current_size") or 0
        d["queued"] = psum(P, "tgi_queue_size") or 0
        gen = psum(P, "tgi_request_generated_tokens_sum")
        pt = psum(P, "tgi_request_input_length_sum")
        _counter_totals(d, pt, gen, psum(P, "tgi_request_success", "tgi_request_count"))
        tpt = pmean(P, "tgi_request_mean_time_per_token_duration")
        if tpt:
            d["totals"]["decode_avg"] = 1 / tpt
        r = rates(d["id"], {"gen": gen, "pt": pt})
        dec, pf = r.get("gen"), r.get("pt")
    d["in_flight"] = int(running)
    return _finish(d, running > 0, dec, pf)


def poll_ollama(srv, pub):
    u = srv["url"]
    d = blank(pub, "Ollama")
    v, _ = _try(u, "/api/version")
    if isinstance(v, dict) and v.get("version"):
        d["engine"] = f"Ollama {v['version']}"
    ps = _get(u, "/api/ps")
    models = ps.get("models") or []
    if models:
        m0 = models[0]
        d["model"] = m0.get("name") or m0.get("model")
        d["context"] = m0.get("context_length")
        det = m0.get("details") or {}
        d["quant"] = det.get("quantization_level")
        d["models_loaded"] = [m.get("name") for m in models][:8]
        vram = sum((m.get("size_vram") or 0) for m in models)
        d["speculation"] = [f"VRAM {vram / 2**30:.1f} GiB"] if vram else []
        d["state"] = "loaded"
    # Ollama hat keine Zaehler (kein /metrics) - Durchsatz nur ueber einen Proxy messbar
    _finish(d, False, None)
    d["state"] = "loaded" if models else "idle"
    return d


def poll_lmstudio(srv, pub):
    u, k = srv["url"], srv.get("key")
    d = blank(pub, "LM Studio")
    m, _ = get_any(u, "/api/v0/models", [k] if k else [])
    loaded = [x for x in (m.get("data") or []) if x.get("state") == "loaded"]
    if loaded:
        x = loaded[0]
        d["model"] = x.get("id")
        d["context"] = x.get("loaded_context_length") or x.get("max_context_length")
        d["quant"] = x.get("quantization")
        d["models_loaded"] = [y.get("id") for y in loaded][:8]
    _finish(d, False, None)
    d["state"] = "loaded" if loaded else "idle"
    return d


def poll_koboldcpp(srv, pub):
    u, k = srv["url"], srv.get("key")
    d = blank(pub, "KoboldCpp")
    v, _ = _try(u, "/api/extra/version", [k] if k else [])
    if isinstance(v, dict) and v.get("version"):
        d["engine"] = f"KoboldCpp {v['version']}"
    m, _ = _try(u, "/api/v1/model", [k] if k else [])
    if isinstance(m, dict):
        d["model"] = str(m.get("result") or "").replace("koboldcpp/", "") or None
    c, _ = _try(u, "/api/extra/true_max_context_length", [k] if k else [])
    if isinstance(c, dict):
        d["context"] = c.get("value")
    perf, _ = _try(u, "/api/extra/perf", [k] if k else [])
    busy, dec = False, None
    if isinstance(perf, dict):
        busy = perf.get("idle") in (0, False)
        d["queued"] = perf.get("queue") or 0
        d["totals"]["requests"] = perf.get("total_gens")
        if perf.get("last_eval") and perf.get("last_token_count"):
            d["last"]["decode"] = perf["last_token_count"] / perf["last_eval"] if perf["last_eval"] > 0 else None
        d["uptime_s"] = perf.get("uptime")
        dec = d["last"]["decode"] if busy else None
    d["in_flight"] = 1 if busy else 0
    return _finish(d, busy, dec)


def poll_triton(srv, pub):
    u = srv["url"]
    d = blank(pub, "Triton")
    v, _ = _try(u, "/v2")
    if isinstance(v, dict):
        d["engine"] = f"Triton {v.get('version', '')}".strip()
    host = srv["host"]
    text, _ = _try(f"http://{host}:{srv.get('metrics_port', 8002)}", "/metrics", raw=True)
    running = dec = 0
    if text:
        P = parse_prom(text)
        running = psum(P, "nv_inference_pending_request_count") or 0
        _counter_totals(d, None, None, psum(P, "nv_inference_request_success"))
        r = rates(d["id"], {"req": psum(P, "nv_inference_request_success")})
        d["last"]["decode"] = None
        gpus = {}
        for name, field in (("nv_gpu_utilization", "util"), ("nv_gpu_memory_used_bytes", "mem_used"),
                            ("nv_gpu_memory_total_bytes", "mem_total"), ("nv_gpu_power_usage", "power"),
                            ("nv_gpu_power_limit", "power_limit")):
            for lab, val in P.get(name, []):
                g = gpus.setdefault(lab.get("gpu_uuid", "0"), {"index": len(gpus), "name": "GPU", "pcie": None})
                g[field] = val * 100 if field == "util" else val
        d["gpus"] = list(gpus.values())
        d["speculation"] = [f"{r.get('req', 0):.1f} req/s"] if r.get("req") else []
    d["in_flight"] = int(running)
    return _finish(d, running > 0, dec)


def poll_openai(srv, pub):
    d = blank(pub, "OpenAI-API")
    _openai_model(d, srv["url"], srv.get("key"))
    _finish(d, False, None)
    d["state"] = "loaded"
    return d


POLL = {"llamacpp": poll_llamacpp, "vllm": poll_vllm, "sglang": poll_sglang, "tgi": poll_tgi,
        "tabbyapi": poll_tabbyapi, "ollama": poll_ollama, "lmstudio": poll_lmstudio,
        "koboldcpp": poll_koboldcpp, "triton": poll_triton, "aphrodite": poll_aphrodite, "openai": poll_openai}


# ------------------------------------------------------------------ Erkennung
def detect(base, keys, allowed=None):
    """Welche Engine laeuft unter base? -> (service, key) oder None. Nur GET-Anfragen, kurze Timeouts.
    allowed: Menge eingeschalteter Engines (None = alle). Reihenfolge: eindeutige Endpunkte zuerst,
    Metrik-Praefix und /v1/models zuletzt."""
    on = (lambda svc: True) if allowed is None else (lambda svc: svc in allowed)
    if allowed is not None and not allowed:
        return None

    def j(path, use_keys=True):
        return _try(base, path, keys if use_keys else (), timeout=1.0)

    v, k = j("/get_server_info") if on("sglang") else (None, None)
    if isinstance(v, dict) and ("model_path" in v or "tp_size" in v or "served_model_name" in v):
        return "sglang", k
    v, k = j("/.well-known/serviceinfo") if on("tabbyapi") else (None, None)
    if isinstance(v, dict) and re.search(r"tabby|exllama", json.dumps(v), re.I):
        return "tabbyapi", k
    v, k = j("/props") if on("llamacpp") else (None, None)
    if isinstance(v, dict) and "default_generation_settings" in v:
        return "llamacpp", k
    v, k = j("/info") if on("tgi") else (None, None)
    if isinstance(v, dict) and ("router" in v or ("model_id" in v and "max_total_tokens" in v)):
        return "tgi", k
    v, k = j("/api/extra/version") if on("koboldcpp") else (None, None)
    if isinstance(v, dict) and str(v.get("result", "")).lower() == "koboldcpp":
        return "koboldcpp", k
    v, _ = j("/api/version", False) if on("ollama") else (None, None)
    if isinstance(v, dict) and "version" in v and len(v) <= 3:
        t, _ = j("/api/tags", False)
        if isinstance(t, dict) and "models" in t:
            return "ollama", None
    v, k = j("/api/v0/models") if on("lmstudio") else (None, None)
    if isinstance(v, dict) and isinstance(v.get("data"), list) and \
            all(isinstance(x, dict) and ("state" in x or "compatibility_type" in x) for x in v["data"]):
        return "lmstudio", k
    v, _ = j("/v2", False) if on("triton") else (None, None)
    if isinstance(v, dict) and str(v.get("name", "")).lower() == "triton":
        return "triton", None
    text, k = _try(base, "/metrics", keys, raw=True, timeout=1.0) \
        if any(on(x) for x in ("vllm", "sglang", "llamacpp", "tabbyapi", "aphrodite", "tgi")) else (None, None)
    if isinstance(text, str):
        for prefix, svc in (("vllm:", "vllm"), ("sglang:", "sglang"), ("llamacpp:", "llamacpp"),
                            ("tabbyapi:", "tabbyapi"), ("aphrodite:", "aphrodite"), ("tgi_", "tgi")):
            if on(svc) and re.search(r"^" + re.escape(prefix), text, re.M):
                return svc, k
    v, k = j("/v1/models") if on("openai") else (None, None)
    # Rueckfall nur mit mindestens einem Modell - Gateways/Proxys mit leerer Liste sind keine Engine
    if isinstance(v, dict) and v.get("object") == "list" and isinstance(v.get("data"), list) and v["data"]:
        return "openai", k
    return None


def poll(srv, pub):
    return POLL[srv["service"]](srv, pub)
