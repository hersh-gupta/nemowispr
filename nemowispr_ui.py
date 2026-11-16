"""
nemowispr_ui.py -- the look (NVIDIA green on near-black), the result card's artwork and the
control panel. Imported by nemowispr.py. Every Tk call here runs on the Overlay's thread (one Tk
interpreter, one thread); slow work (health checks, nvidia-smi, history, Bolt start/stop) runs
on a worker thread that only hands plain data back.

Gates (code, not prompts):
- The panel is the user's window, not the model's: nemowispr_agent.blocked() refuses every
  window of this process, so the agent can't read or click "Stop Bolt" or "Forget".
- Stop needs a second click within 4 s, is refused while the agent runs, and only ever kills the
  llama-server.exe that owns the server's port -- never another process, never by name.
- Start warns (second click) when graphics memory is already too full for the chosen launch profile
  to fit under the ~92% WDDM cliff; Restart re-checks after the old server has let go of its VRAM.
- Launch profiles only offer Bolt GGUFs found on disk (x DFlash on/off): dictation needs Bolt's
  audio encoder, and the DFlash drafter is trained against Bolt.
- Start/Stop only exist for a server on this machine (127.0.0.1 / localhost).
"""
import ctypes
import ctypes.wintypes as wt
import json
import math
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from statistics import median

from PIL import Image, ImageDraw, ImageFilter, ImageGrab, ImageTk

import logging

log = logging.getLogger("nemowispr.ui")    # child of nemowispr.py's logger, so it lands in dictate.log

# --------------------------------------------------------------------------- palette
GREEN, GREEN_HI, GREEN_DEEP = "#76B900", "#8FD61A", "#4A7400"
BG, SURFACE, SURFACE2, LINE = "#0A0B09", "#121410", "#1A1D17", "#272B22"
FG, MUTED, FAINT = "#EEF2E8", "#8D9585", "#5B6253"
AMBER, RED, BLUE, VIOLET = "#F5B83D", "#FF5F56", "#5CC8FF", "#B67CFF"
KIND = {"work": GREEN, "ok": GREEN, "warn": AMBER, "ask": BLUE, "fail": RED, "info": MUTED}

# Segoe Fluent Icons (Win 11) / Segoe MDL2 Assets (Win 10) code points
ICON = dict(mic="", check="", cancel="", warn="", ask="", info="",
            play="", stop="", retry="", copy="", folder="", view="",
            hide="", robot="", lock="", forget="", sync="")
GLYPH = {"ok": ICON["check"], "warn": ICON["warn"], "ask": ICON["ask"], "fail": ICON["cancel"], "info": ICON["info"]}

CLIFF = 0.92              # past this share of VRAM, WDDM pages graphics memory and speed collapses
NO_WINDOW = 0x08000000    # CREATE_NO_WINDOW: pythonw must never flash a console


TK_THREAD = None          # ident of the overlay's Tk thread (set by Overlay._run)


class Photo(ImageTk.PhotoImage):
    """PhotoImage that only deletes itself on the Tk thread. MEASURED: without this the process hangs
    on exit -- garbage collection at shutdown runs __del__ on the main thread, and that cross-thread
    Tcl call waits forever for the (frozen, daemon) Tk thread."""
    def __del__(self):
        if threading.get_ident() == TK_THREAD:
            super().__del__()
        else:
            try:
                self._PhotoImage__photo.name = None   # and stop the inner tkinter image's __del__ too
            except Exception:
                pass


def photo(img, master):
    return Photo(img, master=master)


def rgb(h: str) -> tuple:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def mix(a: str, b: str, t: float) -> str:
    """t=0 -> a, t=1 -> b."""
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(rgb(a), rgb(b)))


def fonts(root) -> dict:
    have = set(root.tk.call("font", "families"))
    pick = lambda *names: next((n for n in names if n in have), "Segoe UI")
    return dict(display=pick("Segoe UI Variable Display Semib", "Segoe UI Semibold"),
                text=pick("Segoe UI Variable Text", "Segoe UI"),
                textb=pick("Segoe UI Variable Text Semibold", "Segoe UI Semibold"),
                caps=pick("Bahnschrift SemiBold", "Segoe UI Semibold"),
                num=pick("Bahnschrift", "Segoe UI"),
                mono=pick("Cascadia Mono", "Consolas"),
                icon=pick("Segoe Fluent Icons", "Segoe MDL2 Assets"))


# --------------------------------------------------------------------------- Windows bits
_dwm = ctypes.WinDLL("dwmapi")
_dwm.DwmSetWindowAttribute.argtypes = (wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD)
_dwm.DwmGetWindowAttribute.argtypes = (wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD)
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenProcess.argtypes = (wt.DWORD, wt.BOOL, wt.DWORD); _k32.OpenProcess.restype = wt.HANDLE
_k32.QueryFullProcessImageNameW.argtypes = (wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD))
_k32.CloseHandle.argtypes = (wt.HANDLE,)
_iph = ctypes.WinDLL("iphlpapi")
_u32 = ctypes.WinDLL("user32")
_u32.SystemParametersInfoW.argtypes = (wt.UINT, wt.UINT, ctypes.c_void_p, wt.UINT)


def colorref(h: str) -> int:
    r, g, b = rgb(h)
    return r | g << 8 | b << 16


def dwm_style(hwnd, border=None, caption=None, text=None, corners=2, dark=True) -> bool:
    """Windows 11 frame: dark mode, rounded corners (2 = round, 3 = small), border/caption colours.
    Silently a no-op on Windows 10."""
    def put(attr, val):
        v = ctypes.c_int(val)
        return _dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), 4) == 0
    ok = True
    if dark:
        ok &= put(20, 1)                       # DWMWA_USE_IMMERSIVE_DARK_MODE
    if corners is not None:
        ok &= put(33, corners)                 # DWMWA_WINDOW_CORNER_PREFERENCE
    if border:
        ok &= put(34, colorref(border))        # DWMWA_BORDER_COLOR
    if caption:
        ok &= put(35, colorref(caption))       # DWMWA_CAPTION_COLOR
    if text:
        ok &= put(36, colorref(text))          # DWMWA_TEXT_COLOR
    return ok


def window_rect(hwnd) -> tuple:
    r = wt.RECT()
    if _dwm.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(r), ctypes.sizeof(r)) != 0:   # EXTENDED_FRAME_BOUNDS
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def grab(hwnd, path):
    """Screenshot one window (call on a per-monitor-DPI-aware thread, i.e. the overlay's)."""
    ImageGrab.grab(window_rect(hwnd), all_screens=True, include_layered_windows=True).save(path)


def work_area() -> tuple:
    wa = (ctypes.c_long * 4)()
    _u32.SystemParametersInfoW(0x30, 0, wa, 0)     # SPI_GETWORKAREA
    return tuple(wa)


# --------------------------------------------------------------------------- Bolt status + control
def _run(args, timeout=5):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW)


def is_local(server: str) -> bool:
    return urllib.parse.urlparse(server).hostname in ("127.0.0.1", "localhost", "::1")


def health(server: str) -> str:
    """'online', 'loading' (llama-server answers 503 while it loads the model) or 'offline'."""
    try:
        urllib.request.urlopen(server + "/health", timeout=1.5).read()
        return "online"
    except urllib.error.HTTPError as e:
        return "loading" if e.code == 503 else "offline"
    except Exception:
        return "offline"


def props(server: str) -> dict:
    """Model file and quant from llama-server's /props, e.g. {'name': 'Bolt E4B', 'quant': 'Q8_K_XL'}."""
    try:
        p = json.loads(urllib.request.urlopen(server + "/props", timeout=2).read())
        path = Path(str(p.get("model_path", ""))).name
    except Exception:
        return {}
    m = re.search(r"(?:UD-)?((?:IQ|Q)\d\w*|BF16|F16|F32)\.gguf$", path, re.I)
    name = "Bolt E4B" if "bolt" in path.lower() else (path.rsplit(".", 1)[0][:28] or "model")
    return {"name": name, "quant": m.group(1).upper() if m else "", "file": path}


class _NvMem(ctypes.Structure):
    _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]


class _NvMem2(ctypes.Structure):     # v2 splits out driver-reserved memory: its "used" matches nvidia-smi
    _fields_ = [("version", ctypes.c_uint), ("total", ctypes.c_ulonglong), ("reserved", ctypes.c_ulonglong),
                ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]


class _NvUtil(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


_nvml = None


def gpu() -> dict | None:
    """GPU 0 through NVML (nvml.dll, in-process, ~1 ms). MEASURED: nvidia-smi takes ~2.4 s when the
    laptop's dGPU is idle, and polling it every couple of seconds keeps waking the GPU. Falls back
    to nvidia-smi if NVML can't load."""
    global _nvml
    if _nvml is None:
        try:
            lib = ctypes.CDLL("nvml.dll")
            dev = ctypes.c_void_p()
            if lib.nvmlInit_v2() != 0 or lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev)) != 0:
                raise OSError("nvml init failed")
            buf = ctypes.create_string_buffer(96)
            lib.nvmlDeviceGetName(dev, buf, 96)
            _nvml = (lib, dev, re.sub(r"^NVIDIA\s+(GeForce\s+)?|\s+GPU$", "", buf.value.decode(errors="replace")))
        except Exception:
            _nvml = False
    if _nvml:
        lib, dev, name = _nvml
        mem, util = _NvMem2(), _NvUtil()
        mem.version = ctypes.sizeof(_NvMem2) | 2 << 24
        # MEASURED at idle: v1 used=283 MiB, v2 used=0 + reserved=283, nvidia-smi 0. The 84%/92% numbers
        # (and run-bolt.ps1's) are nvidia-smi's, so use v2; v1 only if the driver lacks v2.
        if lib.nvmlDeviceGetMemoryInfo_v2(dev, ctypes.byref(mem)) != 0:
            mem = _NvMem()
            if lib.nvmlDeviceGetMemoryInfo(dev, ctypes.byref(mem)) != 0:
                mem = None
        if mem is not None:
            lib.nvmlDeviceGetUtilizationRates(dev, ctypes.byref(util))
            return {"name": name, "used": mem.used // 2**20, "total": mem.total // 2**20, "util": util.gpu}
    try:
        r = _run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu",
                  "--format=csv,noheader,nounits"], timeout=4)
        name, used, total, util = [x.strip() for x in r.stdout.strip().splitlines()[0].split(",")]
        name = re.sub(r"^NVIDIA\s+(GeForce\s+)?|\s+GPU$", "", name)
        return {"name": name, "used": int(used), "total": int(total), "util": int(util)}
    except Exception:
        return None


class _TcpRow(ctypes.Structure):
    _fields_ = [(n, wt.DWORD) for n in ("state", "laddr", "lport", "raddr", "rport", "pid")]


def port_owner(port: int) -> int | None:
    """PID listening on 127.0.0.1/0.0.0.0:port (IPv4), from the TCP table -- no netstat parsing."""
    size = wt.DWORD(0)
    _iph.GetExtendedTcpTable(None, ctypes.byref(size), False, 2, 3, 0)     # AF_INET, TCP_TABLE_OWNER_PID_LISTENER
    buf = ctypes.create_string_buffer(size.value + 4096)
    size = wt.DWORD(len(buf))
    if _iph.GetExtendedTcpTable(buf, ctypes.byref(size), False, 2, 3, 0) != 0:
        return None
    n = wt.DWORD.from_buffer(buf).value
    for row in (_TcpRow * n).from_buffer(buf, 4):
        if ((row.lport & 0xFF) << 8 | (row.lport >> 8) & 0xFF) == port:
            return row.pid
    return None


def proc_name(pid: int) -> str:
    h = _k32.OpenProcess(0x1000, False, pid)         # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf, size = ctypes.create_unicode_buffer(520), wt.DWORD(520)
        return os.path.basename(buf.value).lower() if _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) else ""
    finally:
        _k32.CloseHandle(h)


# Launch profiles: Bolt GGUF x DFlash on/off. VRAM estimate = run-bolt.ps1's: GPU weights scale with the
# file (4942 MiB of the Q8_K_XL; the rest is the per-layer-embedding table, kept in RAM), + KV @128K 2088,
# compute 692, measured extra 65, mmproj 945, DFlash 1500. Q8_K_XL + DFlash -> 10232 (measured 10234).
Q8_BYTES, Q8_GPU_MIB, REST_MIB, DFLASH_MIB = 8192955584, 4942, 2088 + 692 + 65 + 945, 1500
HF_SNAPSHOTS = Path.home() / ".cache" / "huggingface" / "hub" / "models--unsloth--NVIDIA-Nemotron-3.5-Bolt-E4B-GGUF" / "snapshots"


def quant_of(name: str) -> str:
    m = re.search(r"(?:UD-)?((?:IQ|Q)\d\w*|BF16|F16|F32)\.gguf$", name, re.I)
    return m.group(1).upper() if m else name.rsplit(".", 1)[0]


def bolt_files() -> tuple[list, bool]:
    """Bolt GGUFs on disk (newest snapshot wins per file name), largest first, and whether a DFlash
    drafter is there. Only Bolt: dictation needs its audio encoder (mmproj), and the drafter is
    trained against it -- another model family would break both."""
    if os.environ.get("BOLT_MODEL_DIR"):
        dirs = [Path(os.environ["BOLT_MODEL_DIR"])]
    else:
        dirs = sorted(HF_SNAPSHOTS.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True) if HF_SNAPSHOTS.exists() else []
    seen, models, dflash = set(), [], False
    for d in dirs:
        for f in d.glob("*.gguf"):
            n = f.name.lower()
            if n.startswith("dflash"):
                dflash = True
            elif not n.startswith(("mmproj", "imatrix")) and "bolt" in n and f.name not in seen:
                seen.add(f.name)
                models.append(f)
    models.sort(key=lambda f: f.stat().st_size, reverse=True)
    return models, dflash


def launch_profiles() -> list:
    models, has_dflash = bolt_files()
    out = []
    for f in models:
        for df in ((True, False) if has_dflash else (False,)):
            q = quant_of(f.name)
            out.append({"file": f, "name": f.name, "quant": q, "dflash": df,
                        "est": round(Q8_GPU_MIB * f.stat().st_size / Q8_BYTES) + REST_MIB + (DFLASH_MIB if df else 0),
                        "label": f"{q} · {'DFlash' if df else 'no DFlash'}"})
    return out


def start_bolt(script: Path, logfile: Path, model: Path | None = None, dflash: bool = True) -> subprocess.Popen:
    """run-bolt.ps1, hidden and detached (it outlives the tray app), output appended to logfile."""
    args = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]
    if model:
        args += ["-Model", str(model)]
    if not dflash:
        args += ["-NoDFlash"]
    base = NO_WINDOW | 0x00000200                   # + CREATE_NEW_PROCESS_GROUP
    with open(logfile, "ab") as f:
        f.write(f"\n==== {datetime.now():%Y-%m-%d %H:%M:%S} start from the control panel: {' '.join(args[5:])} ====\n".encode())
        f.flush()
        for flags in (base | 0x01000000, base):     # CREATE_BREAKAWAY_FROM_JOB, if uv's job object allows it
            try:
                return subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT,
                                        creationflags=flags, cwd=str(script.parent))
            except OSError:
                continue
    raise OSError("couldn't start PowerShell")


def stop_bolt(port: int) -> tuple[bool, str]:
    """Kill the llama-server.exe that owns the port -- and nothing else."""
    pid = port_owner(port)
    if not pid:
        return True, f"Nothing is listening on port {port}."
    name = proc_name(pid)
    if name != "llama-server.exe":
        return False, f"Port {port} belongs to {name or 'an unknown process'}, not llama-server; left it alone."
    r = _run(["taskkill", "/PID", str(pid), "/F"])
    return (True, "Bolt stopped.") if r.returncode == 0 else (False, f"taskkill failed: {r.stderr.strip()[:100]}")


# --------------------------------------------------------------------------- history
def load_history(home: Path, keep=3000) -> list:
    try:
        lines = (home / "history.jsonl").read_text(encoding="utf-8").splitlines()[-keep:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("source") != "cli":                # test runs (tests\run-tests.ps1) aren't usage
            out.append(e)
    return out


def latency(e) -> float | None:
    return round(e["t_transcribe"] + e.get("t_cleanup", 0), 2) if "t_transcribe" in e else None


def summarize(entries: list) -> dict:
    today = datetime.now().date().isoformat()
    t = [e for e in entries if str(e.get("ts", "")).startswith(today)]
    dic = [e for e in t if e.get("type") != "command" and e.get("final")]
    runs = [e for e in t if e.get("type") == "command" and isinstance(e.get("agent"), dict)]
    lats = [x for x in map(latency, dic) if x is not None]
    recent = [e for e in entries if e.get("final")][-5:][::-1]
    return {"words": sum(len(e["final"].split()) for e in dic), "dictations": len(dic),
            "latency": median(lats) if lats else None, "runs": len(runs),
            "verified": sum(1 for e in runs if e["agent"].get("status") == "done" and e["agent"].get("verified")),
            "recent": recent}


# --------------------------------------------------------------------------- artwork (Pillow, 4x supersampled)
SS = 4


def _img(w, h, bg=None):
    return Image.new("RGBA", (w * SS, h * SS), rgb(bg) + (255,) if bg else (0, 0, 0, 0))


def _down(img, w, h):
    return img.resize((w, h), Image.LANCZOS)


def rounded(w, h, r, fill, outline=None, bg=None, width=1):
    img = _img(w, h, bg)
    ImageDraw.Draw(img).rounded_rectangle((0, 0, w * SS - 1, h * SS - 1), r * SS, fill=fill, outline=outline,
                                          width=max(1, int(width * SS)) if outline else 0)
    return _down(img, w, h)


def circle(d, fill, outline=None, bg=None, width=1.0):
    img = _img(d, d, bg)
    ImageDraw.Draw(img).ellipse((0, 0, d * SS - 1, d * SS - 1), fill=fill, outline=outline,
                                width=max(1, int(width * SS)) if outline else 0)
    return _down(img, d, d)


def spinner_frames(d, color, bg, n=24):
    """Badge for 'working': a tinted disc with a rotating comet arc."""
    frames = []
    for i in range(n):
        img = _img(d, d, bg)
        dr = ImageDraw.Draw(img)
        D = d * SS
        dr.ellipse((0, 0, D - 1, D - 1), fill=mix(color, bg, 0.84), outline=mix(color, bg, 0.6), width=SS)
        p, w = int(D * 0.24), max(SS, int(D * 0.085))
        a0 = i * 360 / n
        for k in range(10):                        # comet tail: fading segments
            dr.arc((p, p, D - p, D - p), a0 + k * 22, a0 + k * 22 + 24, fill=mix(bg, color, (k + 1) / 10), width=w)
        frames.append(_down(img, d, d))
    return frames


def gauge(d, pct, bg):
    """270-degree ring: track, value arc with round caps and a soft glow, red tick at the 92% cliff."""
    color = GREEN if pct < 0.85 else AMBER if pct < CLIFF else RED
    img = _img(d, d, bg)
    dr = ImageDraw.Draw(img)
    D, t = d * SS, int(d * SS * 0.075)
    pad = int(D * 0.07)
    box = (pad, pad, D - pad, D - pad)
    R = (D - 2 * pad) / 2 - t / 2                  # centre line of the ring
    cx = cy = D / 2

    def at(a, r=R):
        return cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a))

    def cap(a, fill):
        x, y = at(a)
        dr.ellipse((x - t / 2, y - t / 2, x + t / 2, y + t / 2), fill=fill)

    dr.arc(box, 135, 405, fill=LINE, width=t)
    cap(135, LINE); cap(405, LINE)
    if pct > 0.004:
        end = 135 + 270 * min(pct, 1)
        dr.arc(box, 135, end, fill=color, width=t)
        cap(135, color); cap(end, color)
    a = 135 + 270 * CLIFF                         # the cliff tick, across the ring
    dr.line((*at(a, R - t * 1.05), *at(a, R + t * 1.05)), fill=RED, width=max(SS, int(SS * 1.6)))
    out = _down(img, d, d)
    if pct > 0.004:                                # glow: blurred copy of the value arc, screened on top
        g = Image.new("L", (d, d), 0)
        s = d / D
        ImageDraw.Draw(g).arc(tuple(v * s for v in box), 135, 135 + 270 * min(pct, 1), fill=150, width=max(2, int(t * s * 1.6)))
        g = g.filter(ImageFilter.GaussianBlur(d * 0.035))
        out = Image.composite(Image.new("RGBA", (d, d), rgb(color) + (255,)), out, g.point(lambda v: v * 0.45))
    return out


def logo(d):
    """App mark: a rounded square in an NVIDIA-green gradient (the mic glyph goes on top as text)."""
    grad = Image.new("RGBA", (d * SS, d * SS))
    top, bot = rgb(GREEN_HI), rgb(GREEN_DEEP)
    dr = ImageDraw.Draw(grad)
    for y in range(d * SS):
        t = y / (d * SS)
        dr.line((0, y, d * SS, y), fill=tuple(round(a + (b - a) * t) for a, b in zip(top, bot)) + (255,))
    mask = Image.new("L", grad.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, d * SS - 1, d * SS - 1), int(d * SS * 0.27), fill=255)
    out = Image.new("RGBA", grad.size, (0, 0, 0, 0))   # transparent corners: Tk blends alpha onto the canvas
    out.paste(grad, (0, 0), mask)
    return _down(out, d, d)


def tray_icon(state: str):
    """Tray: dark disc + green mic when idle; solid state colour (green = agent) otherwise."""
    fill = {"idle": "#14170F", "rec": RED, "busy": AMBER, "error": VIOLET, "agent": GREEN}[state]
    ink = {"idle": GREEN, "rec": "#FFFFFF", "busy": BG, "error": "#FFFFFF", "agent": BG}[state]
    n = 64 * SS
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2 * SS, 2 * SS, n - 2 * SS, n - 2 * SS), fill=fill, outline=GREEN if state == "idle" else None,
              width=4 * SS if state == "idle" else 0)
    s = SS
    d.rounded_rectangle((25 * s, 13 * s, 39 * s, 37 * s), radius=7 * s, fill=ink)
    d.arc((18 * s, 22 * s, 46 * s, 46 * s), start=0, end=180, fill=ink, width=4 * s)
    d.line((32 * s, 46 * s, 32 * s, 52 * s), fill=ink, width=4 * s)
    return img.resize((64, 64), Image.LANCZOS)


def panel_background(W, H, cards, s):
    """Near-black canvas, a soft green glow top-left, a faint dot grid under the header, and the
    rounded section cards. W, H, cards in device pixels."""
    img = Image.new("RGBA", (W, H), rgb(BG) + (255,))
    q = 8
    g = Image.new("L", (W // q + 1, H // q + 1), 0)
    ImageDraw.Draw(g).ellipse((-W // q // 4, -H // q // 2, int(W / q * 0.55), int(H / q * 0.32)), fill=58)
    g = g.filter(ImageFilter.GaussianBlur(W / q / 14)).resize((W, H), Image.BILINEAR)
    img = Image.composite(Image.new("RGBA", (W, H), rgb(GREEN) + (255,)), img, g)
    d = ImageDraw.Draw(img)
    step = max(6, int(16 * s))
    for y in range(step // 2, int(80 * s), step):
        fade = 1 - y / (80 * s)
        for x in range(step // 2, W, step):
            d.point((x, y), fill=mix(BG, GREEN, 0.10 * fade + 0.03))
    for (x0, y0, x1, y1) in cards:
        w, h = x1 - x0, y1 - y0
        card = rounded(w, h, 14 * s / 1.0, SURFACE, LINE, width=1 * s)
        img.alpha_composite(card, (x0, y0))
        d.line((x0 + int(14 * s), y0 + 1, x1 - int(14 * s), y0 + 1), fill=mix(SURFACE, GREEN, 0.10))   # top sheen
    return img


# --------------------------------------------------------------------------- the control panel
class Panel:
    """The tray app's control panel. Build and use only on the Overlay thread."""
    W, H = 800, 576
    CARDS = {"server": (24, 92, 392, 332), "today": (24, 344, 392, 448), "settings": (408, 92, 776, 332),
             "recent": (408, 344, 776, 508)}
    ACTIONS_Y, FOOTER_Y = 460, 532

    def __init__(self, root, app, home: Path, server: str, script: Path, on_close=None):
        self.root, self.app, self.home, self.server, self.script = root, app, home, server, script
        self.on_close = on_close
        self.port = urllib.parse.urlparse(server).port or 80
        self.local = is_local(server)
        self.top = None
        self.visible = False
        self.state = {"health": "offline", "gpu": None, "model": {}, "hist": summarize([])}
        self.kick, self.polled = threading.Event(), threading.Event()
        self.worker = None
        self.cache, self.buttons, self.toggles, self.fontobj = {}, {}, [], {}
        self.armed, self.armed_until, self.starting_until = None, 0.0, 0.0
        self.msg, self.msg_until, self.msg_color = "", 0.0, MUTED
        self.privacy = False
        self.g_shown, self.g_drawn = 0.0, -1.0
        self.flash = {}
        self.profiles, self.sel = [], 0        # launch profiles (model x DFlash) and the chosen one
        self.launched = None                   # (file name, dflash) of the Bolt this panel started
        self.proc = None                       # its run-bolt.ps1 process; exiting while still offline = failed start
        self.pop, self.pop_hit = False, False  # profile popover open / a click landed on it

    # ---- helpers
    def S(self, v):
        return int(round(v * self.s))

    def font(self, fam, size, *style):
        key = (fam, size, style)
        if key not in self.fontobj:
            import tkinter.font as tkfont
            f = tkfont.Font(root=self.root, family=self.F[fam], size=size,
                            weight="normal", slant="italic" if "italic" in style else "roman")
            f.delete_font = False          # same exit hang as Photo: never delete from the wrong thread
            self.fontobj[key] = f
        return self.fontobj[key]

    def photo(self, key, make):
        if key not in self.cache:
            self.cache[key] = photo(make(), self.root)
        return self.cache[key]

    def fit(self, text, fnt, maxw):
        if fnt.measure(text) <= maxw:
            return text
        while text and fnt.measure(text + "…") > maxw:
            text = text[:-1]
        return text.rstrip() + "…"

    def text(self, x, y, s="", fam="text", size=10, fill=FG, anchor="nw", tags=(), style=()):
        return self.c.create_text(self.S(x), self.S(y), text=s, font=self.font(fam, size, *style), fill=fill,
                                  anchor=anchor, tags=tags)

    def _hit(self, tag, on_click, hover=None):
        c = self.c
        c.tag_bind(tag, "<Button-1>", lambda e: on_click())
        c.tag_bind(tag, "<Enter>", lambda e: (c.config(cursor="hand2"), hover and hover(True)))
        c.tag_bind(tag, "<Leave>", lambda e: (c.config(cursor=""), hover and hover(False)))

    # ---- buttons (canvas: rounded image + glyph + label)
    STYLES = {"primary": (GREEN, None, BG), "primary_h": (GREEN_HI, None, BG),
              "ghost": (SURFACE2, LINE, FG), "ghost_h": (mix(SURFACE2, GREEN, 0.08), GREEN, FG),
              "danger": (RED, None, BG), "danger_h": (mix(RED, "#FFFFFF", 0.12), None, BG),
              "warn": (AMBER, None, BG), "warn_h": (mix(AMBER, "#FFFFFF", 0.12), None, BG),
              "off": (SURFACE2, LINE, FAINT), "off_h": (SURFACE2, LINE, FAINT)}

    def button(self, tag, x, y, w, h, cmd, size=10, radius=10):
        b = self.buttons[tag] = dict(x=x, y=y, w=w, h=h, cmd=cmd, size=size, radius=radius, hover=False,
                                     style="ghost", label="", icon="", painted=None)
        b["img"] = self.c.create_image(self.S(x), self.S(y), anchor="nw", tags=(tag,))
        b["ico"] = self.c.create_text(0, 0, anchor="w", font=self.font("icon", size), tags=(tag,))
        b["txt"] = self.c.create_text(0, 0, anchor="w", font=self.font("textb", size), tags=(tag,))

        def hover(on):
            b["hover"] = on
            self.paint(tag)
        self._hit(tag, lambda: b["style"] != "off" and b["cmd"](), hover)

    def set_button(self, tag, label, icon, style):
        b = self.buttons[tag]
        b.update(label=label, icon=icon, style=style)
        self.paint(tag)

    def paint(self, tag):
        b = self.buttons[tag]
        st = b["style"] + ("_h" if b["hover"] else "")
        key = (st, b["label"], b["icon"])
        if b["painted"] == key:
            return
        b["painted"] = key
        fill, outline, ink = self.STYLES[st]
        w, h = self.S(b["w"]), self.S(b["h"])
        img = self.photo(("btn", w, h, fill, outline),
                         lambda: rounded(w, h, b["radius"] * self.s, fill, outline, width=self.s))
        c = self.c
        c.itemconfig(b["img"], image=img)
        fi, ft = self.font("icon", b["size"]), self.font("textb", b["size"])
        gap = self.S(8) if b["icon"] and b["label"] else 0
        wi, wl = (fi.measure(b["icon"]) if b["icon"] else 0), ft.measure(b["label"])
        x0 = self.S(b["x"]) + (w - wi - gap - wl) // 2
        cy = self.S(b["y"]) + h // 2
        c.itemconfig(b["ico"], text=b["icon"], fill=ink)
        c.coords(b["ico"], x0, cy)
        c.itemconfig(b["txt"], text=b["label"], fill=ink)
        c.coords(b["txt"], x0 + wi + gap, cy)

    # ---- toggles
    def toggle(self, x, y, label, desc, get, flip):
        tag = f"tg{len(self.toggles)}"
        w = 168
        self.c.create_rectangle(self.S(x - 6), self.S(y - 6), self.S(x + w), self.S(y + 36), fill=SURFACE, outline="",
                                tags=(tag,))
        self.text(x, y, label, "textb", 10, FG, tags=(tag,))
        self.text(x, y + 19, desc, "text", 8, MUTED, tags=(tag,))
        img = self.c.create_image(self.S(x + w - 44), self.S(y + 6), anchor="nw", tags=(tag,))
        t = dict(tag=tag, img=img, get=get, shown=None)
        self.toggles.append(t)

        def click():
            flip()
            self.app_changed()
            self.render()
        self._hit(tag, click)

    def toggle_img(self, on):
        w, h = self.S(40), self.S(22)

        def make():
            img = _img(w, h, SURFACE)
            d = ImageDraw.Draw(img)
            W, H = w * SS, h * SS
            d.rounded_rectangle((0, 0, W - 1, H - 1), H // 2, fill=GREEN if on else "#22261D",
                                outline=None if on else "#3A4131", width=SS)
            k = int(H * 0.72)
            m = (H - k) // 2
            x = W - m - k if on else m
            d.ellipse((x, m, x + k, m + k), fill=BG if on else MUTED)
            return _down(img, w, h)
        return self.photo(("tg", on), make)

    def app_changed(self):
        icon = getattr(self.app, "icon", None)
        if icon is not None:
            try:
                icon.update_menu()             # keep the tray menu's check marks in step
            except Exception:
                pass

    # ---- build
    def build(self):
        import tkinter as tk
        self.s = self.root.winfo_fpixels("1i") / 96
        self.F = fonts(self.root)
        S = self.S
        top = self.top = tk.Toplevel(self.root, bg=BG)
        top.withdraw()
        top.title("NemoWispr")
        top.resizable(False, False)
        top.protocol("WM_DELETE_WINDOW", self.hide)
        top.bind("<Escape>", lambda e: self.hide())
        try:
            top.iconphoto(False, photo(tray_icon("idle"), self.root))
        except Exception:
            pass
        c = self.c = tk.Canvas(top, width=S(self.W), height=S(self.H), bg=BG, highlightthickness=0, bd=0)
        c.pack()
        cards = [tuple(S(v) for v in r) for r in self.CARDS.values()]
        self.bg = photo(panel_background(S(self.W), S(self.H), cards, self.s), self.root)
        c.create_image(0, 0, image=self.bg, anchor="nw")

        # header
        self.logo = photo(logo(S(44)), self.root)
        c.create_image(S(24), S(22), image=self.logo, anchor="nw")
        self.text(46, 44, ICON["mic"], "icon", 17, BG, anchor="center")
        self.text(80, 22, "NemoWispr", "display", 18, FG)
        self.text(81, 54, "Local voice dictation + desktop agent  ·  Nemotron 3.5 Bolt  ·  on this PC",
                  "text", 9, MUTED)
        self.pill_img = c.create_image(S(776), S(30), anchor="ne")
        self.pill_dot = c.create_oval(0, 0, 0, 0, fill=GREEN, outline="")
        self.pill_txt = self.text(0, 0, "", "caps", 9, GREEN, anchor="w")

        # server card
        x0, y0 = self.CARDS["server"][:2]
        self.text(x0 + 16, y0 + 14, "BOLT SERVER", "caps", 8, MUTED)
        self.gauge_img = c.create_image(S(x0 + 12), S(y0 + 34), anchor="nw")
        gx, gy = x0 + 12 + 70, y0 + 34 + 70
        self.g_pct = self.text(gx, gy - 4, "–", "num", 22, FG, anchor="center")
        self.g_lbl = self.text(gx, gy + 22, "VRAM", "caps", 8, MUTED, anchor="center")
        self.text(gx - 30, y0 + 186, "▍", "text", 8, RED, anchor="center")
        self.text(gx - 22, y0 + 186, "92% paging cliff", "text", 7, FAINT, anchor="w")
        self.text(self.CARDS["server"][2] - 16, y0 + 15, urllib.parse.urlparse(self.server).netloc, "mono", 7, FAINT, anchor="ne")
        dx = x0 + 176
        self.rows = {}
        for i, k in enumerate(("MODEL", "GPU", "MEMORY")):
            y = y0 + 34 + i * 30
            self.text(dx, y, k, "caps", 7, FAINT)
            self.rows[k] = self.text(dx, y + 11, "–", "textb", 9, FG)
        self.text(dx, y0 + 126, "LAUNCH PROFILE", "caps", 7, FAINT)
        self.dropdown(dx, y0 + 139, 176, 30)
        by = y0 + 177
        self.button("srv", dx, by, 176, 34, self.on_start)
        self.button("stop", dx, by, 84, 34, self.on_stop, size=9)
        self.button("restart", dx + 92, by, 84, 34, self.on_restart, size=9)
        self.msg_item = self.text(dx, y0 + 217, "", "text", 7, MUTED)
        self.load_profiles()
        c.bind("<Button-1>", lambda e: self.c.after_idle(self._click_away), add="+")

        # today
        x0, y0 = self.CARDS["today"][:2]
        self.text(x0 + 16, y0 + 14, "TODAY", "caps", 8, MUTED)
        self.stats = []
        for i, (lbl, color) in enumerate((("WORDS DICTATED", GREEN), ("MEDIAN LATENCY", FG), ("AGENT RUNS", FG))):
            x = x0 + 16 + i * 120
            v = self.text(x, y0 + 32, "–", "num", 24, color)
            self.text(x, y0 + 68, lbl, "caps", 7, MUTED)
            sub = self.text(x, y0 + 82, "", "text", 7, FAINT)
            self.stats.append((v, sub))

        # actions
        y = self.ACTIONS_Y
        acts = (("retry", "Retry", ICON["retry"], self.do_retry), ("copy", "Copy raw", ICON["copy"], self.do_copy),
                ("forget", "Forget", ICON["forget"], self.do_forget), ("logs", "Logs", ICON["folder"], self.do_logs))
        for i, (tag, lbl, ico, cmd) in enumerate(acts):
            self.button(tag, 24 + i * 94, y, 86, 48, cmd, size=9, radius=12)
            self.set_button(tag, lbl, ico, "ghost")

        # settings
        x0, y0 = self.CARDS["settings"][:2]
        self.text(x0 + 16, y0 + 14, "SETTINGS", "caps", 8, MUTED)
        a = self.app
        cols = ((x0 + 16, "DICTATION", (
                    ("Clean up", "Bolt fixes fillers", lambda: a.clean, lambda: setattr(a, "clean", not a.clean)),
                    ("Warm mic", "No clipped first words", lambda: a.rec.warm, self.flip_warm),
                    ("Sounds", "Start / stop tones", lambda: a.sounds, lambda: setattr(a, "sounds", not a.sounds)))),
                (x0 + 196, "AGENT", (
                    ("Result cards", "On-screen results", lambda: a.cards, lambda: setattr(a, "cards", not a.cards)),
                    ("Speak", "Read results aloud", lambda: a.speak, lambda: setattr(a, "speak", not a.speak)),
                    ("Toasts", "Windows notifications", lambda: a.toasts, lambda: setattr(a, "toasts", not a.toasts)))))
        for x, head, items in cols:
            self.text(x, y0 + 40, head, "caps", 7, GREEN)
            for i, (lbl, desc, get, flip) in enumerate(items):
                self.toggle(x, y0 + 66 + i * 54, lbl, desc, get, flip)

        # recent
        x0, y0, x1, _ = self.CARDS["recent"]
        self.text(x0 + 16, y0 + 14, "RECENT", "caps", 8, MUTED)
        self.eye = self.text(x1 - 16, y0 + 12, ICON["view"], "icon", 10, MUTED, anchor="ne", tags=("eye",))
        self.c.create_rectangle(S(x1 - 40), S(y0 + 6), S(x1 - 8), S(y0 + 30), fill=SURFACE, outline="", tags=("eye",))
        self.c.tag_raise(self.eye)
        self._hit("eye", self.flip_privacy)
        self.recent = []
        for i in range(5):
            y = y0 + 40 + i * 24
            self.recent.append((self.text(x0 + 16, y, "", "mono", 8, FAINT),
                                self.text(x0 + 66, y + 1, "", "icon", 9, GREEN),
                                self.text(x0 + 86, y - 1, "", "text", 9, FG),
                                self.text(x1 - 16, y, "", "text", 8, FAINT, anchor="ne")))
        self.empty = self.text((x0 + x1) / 2, y0 + 92, "", "text", 9, FAINT, anchor="center")

        # footer: keycaps
        self.footer()
        self.render()

    def keycap(self, x, y, label):
        f = self.font("caps", 8)
        w = max(self.S(26), f.measure(label) + self.S(14))
        h = self.S(22)
        img = self.photo(("kc", w), lambda: rounded(w, h, 5 * self.s, SURFACE2, "#353A2E", width=self.s))
        self.c.create_image(self.S(x), self.S(y), image=img, anchor="nw")
        self.c.create_text(self.S(x) + w // 2, self.S(y) + h // 2, text=label, font=f, fill=FG)
        return x + w / self.s

    def footer(self):
        x, y = 24, self.FOOTER_Y
        for keys, what in ((("Ctrl", "Win"), "dictate"), (("Ctrl", "Win", "Shift"), "command"), (("Esc",), "cancel")):
            for i, k in enumerate(keys):
                if i:
                    self.text(x + 4, y + 2, "+", "text", 9, FAINT)
                    x += 14
                x = self.keycap(x, y, k)
            self.text(x + 8, y + 2, what, "text", 9, MUTED)
            x += 8 + self.font("text", 9).measure(what) / self.s + 22
        self.text(776, y + 2, "audio and screen never leave this PC", "text", 8, FAINT, anchor="ne")
        self.text(776 - self.font("text", 8).measure("audio and screen never leave this PC") / self.s - 8, y + 3,
                  ICON["lock"], "icon", 8, GREEN, anchor="ne")

    # ---- launch profile: dropdown + popover (canvas items, no extra window)
    def load_profiles(self):
        self.profiles = launch_profiles()
        try:
            pref = json.loads((self.home / "bolt-profile.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pref = {}
        want = (pref.get("name", "NVIDIA-Nemotron-3.5-Bolt-E4B-UD-Q8_K_XL.gguf"), pref.get("dflash", True))
        self.sel = next((i for i, p in enumerate(self.profiles) if (p["name"], p["dflash"]) == want), 0)

    def profile(self):
        return self.profiles[self.sel] if self.profiles else None

    def running(self):
        """(file name, dflash) of the Bolt that's up, from /props; DFlash is known only if we started it
        (run-bolt.ps1's default is on)."""
        f = (self.state.get("model") or {}).get("file")
        if not f:
            return None
        return f, self.launched[1] if self.launched and self.launched[0] == f else None   # None: not known

    def fit_color(self, mib):
        g = self.state.get("gpu")
        share = mib / (g["total"] if g else 12227)
        return GREEN if share < 0.85 else AMBER if share < CLIFF else RED

    def dropdown(self, x, y, w, h):
        self.dd = dict(x=x, y=y, w=w, h=h, hover=False, painted=None)
        self.dd_img = self.c.create_image(self.S(x), self.S(y), anchor="nw", tags=("dd",))
        self.dd_txt = self.text(x + 11, y + h / 2, "", "textb", 9, FG, anchor="w", tags=("dd",))
        self.dd_est = self.text(x + w, y - 13, "", "caps", 7, MUTED, anchor="ne")    # beside "LAUNCH PROFILE"
        self.dd_chev = self.text(x + w - 11, y + h / 2 + 1, "", "icon", 8, MUTED, anchor="e", tags=("dd",))

        def hover(on):
            self.dd["hover"] = on
            self.paint_dd()

        def click():
            self.pop_hit = True
            self.close_pop() if self.pop else self.open_pop()
        self._hit("dd", click, hover)

    def paint_dd(self):
        d, p = self.dd, self.profile()
        lit = d["hover"] or self.pop
        w, h = self.S(d["w"]), self.S(d["h"])
        self.c.itemconfig(self.dd_img, image=self.photo(("dd", lit), lambda: rounded(
            w, h, 8 * self.s, SURFACE2, mix(GREEN, SURFACE2, 0.25) if lit else LINE, width=self.s)))
        self.c.itemconfig(self.dd_txt, text=self.fit(p["label"], self.font("textb", 9), self.S(d["w"] - 40)) if p
                          else "no Bolt GGUF found", fill=FG if p else FAINT)
        g = self.state.get("gpu")
        self.c.itemconfig(self.dd_est, text=f"~{p['est'] / 1024:.1f} GB · {p['est'] / (g['total'] if g else 12227):.0%}"
                          if p else "", fill=self.fit_color(p["est"]) if p else MUTED)
        self.c.itemconfig(self.dd_chev, text="" if self.pop else "", fill=GREEN if lit else MUTED)

    def open_pop(self):
        if not self.profiles:
            return
        self.pop = True
        S, c, d = self.S, self.c, self.dd
        rows = self.profiles[:8]
        x, w = d["x"] - 92, d["w"] + 92               # wider than the box: floats over the gauge
        y, rh = d["y"] + d["h"] + 6, 34
        h = 16 + 18 + len(rows) * rh
        sh = self.photo(("popshadow", len(rows)), lambda: self._shadow(S(w), S(h)))
        c.create_image(S(x) - S(14), S(y) - S(8), image=sh, anchor="nw", tags=("pop",))
        img = self.photo(("pop", len(rows)), lambda: rounded(S(w), S(h), 12 * self.s, SURFACE2, mix(GREEN, BG, 0.5), width=self.s))
        c.create_image(S(x), S(y), image=img, anchor="nw", tags=("pop",))
        self.text(x + 14, y + 10, "MODEL  ·  DECODING", "caps", 7, FAINT, tags=("pop",))
        self.text(x + w - 14, y + 10, "EST. VRAM", "caps", 7, FAINT, anchor="ne", tags=("pop",))
        run = self.running()
        for i, p in enumerate(rows):
            ry, tag = y + 30 + i * rh, f"pr{i}"
            bg = c.create_rectangle(S(x + 6), S(ry), S(x + w - 6), S(ry + rh - 4), fill=SURFACE2, outline="", tags=(tag, "pop"))
            sel = i == self.sel
            self.text(x + 16, ry + (rh - 4) / 2, ICON["check"] if sel else "", "icon", 9, GREEN, anchor="w", tags=(tag, "pop"))
            self.text(x + 38, ry + 3, p["label"], "textb", 9, FG if sel else mix(FG, MUTED, 0.3), tags=(tag, "pop"))
            sub = ("faster generation, +1.5 GB" if p["dflash"] else "faster prefill, less VRAM") + \
                  ("   ·   RUNNING" if run == (p["name"], p["dflash"]) else "")
            self.text(x + 38, ry + 17, sub, "text", 7, MUTED, tags=(tag, "pop"))
            g = self.state.get("gpu")
            share = f"  {p['est'] / (g['total'] if g else 12227):.0%}"
            self.text(x + w - 16, ry + (rh - 4) / 2, f"~{p['est'] / 1024:.1f} GB{share}", "num", 9,
                      self.fit_color(p["est"]), anchor="e", tags=(tag, "pop"))

            def pick(i=i):
                self.pop_hit = True
                self.choose(i)

            def hov(on, bg=bg):
                c.itemconfig(bg, fill=mix(SURFACE2, GREEN, 0.12) if on else SURFACE2)
            self._hit(tag, pick, hov)
        c.tag_raise("pop")
        self.paint_dd()

    def _shadow(self, w, h):
        pad = int(14 * self.s)
        m = Image.new("L", (w + 2 * pad, h + 2 * pad), 0)
        ImageDraw.Draw(m).rounded_rectangle((pad, pad + int(6 * self.s), pad + w, pad + h), int(12 * self.s), fill=150)
        m = m.filter(ImageFilter.GaussianBlur(9 * self.s))
        out = Image.new("RGBA", m.size, (0, 0, 0, 0))
        out.putalpha(m)
        return out

    def close_pop(self):
        self.pop = False
        self.c.delete("pop")
        self.c.config(cursor="")
        self.paint_dd()

    def _click_away(self):
        if self.pop and not self.pop_hit:
            self.close_pop()
        self.pop_hit = False

    def choose(self, i):
        self.sel = i
        p = self.profile()
        try:
            (self.home / "bolt-profile.json").write_text(json.dumps({"name": p["name"], "dflash": p["dflash"]}), encoding="utf-8")
        except OSError:
            pass
        self.close_pop()
        run = self.running()
        if run and (run[0] != p["name"] or run[1] not in (None, p["dflash"])):
            self.say(f"Restart to switch to {p['label']}.", GREEN, 10)
        self.render()

    # ---- show / hide / loops
    def show(self):
        first = self.top is None
        if first:
            self.build()
        top = self.top
        if first:
            S = self.S
            l, t, r, b = work_area()
            w, h = S(self.W), S(self.H)
            top.geometry(f"+{l + max(0, (r - l - w) // 2)}+{t + max(0, (b - t - h) // 2)}")
        top.deiconify()
        top.attributes("-topmost", True)       # in front even when Windows refuses us the foreground...
        top.lift()
        top.focus_force()
        top.after(400, lambda: top.attributes("-topmost", False))   # ...then an ordinary window again
        if first:
            top.update_idletasks()
            self.hwnd = int(top.wm_frame(), 16)
            dwm_style(self.hwnd, border=mix(GREEN, BG, 0.35), caption=BG, text=FG)
        if not self.visible:
            self.visible = True
            self.kick.set()
            if not (self.worker and self.worker.is_alive()):
                self.worker = threading.Thread(target=self._work, daemon=True, name="panel-status")
                self.worker.start()
            self._tick()
            self._anim()

    def hide(self):
        self.visible = False
        if self.top is not None:
            self.top.withdraw()
        if self.on_close:
            self.on_close()

    def _work(self):
        meta, hist_mtime = {}, None
        while self.visible:
            try:
                st = {"health": health(self.server), "gpu": gpu()}
                if st["health"] == "online":
                    meta = meta or props(self.server)
                else:
                    meta = {}
                st["model"] = meta
                p = self.home / "history.jsonl"
                m = p.stat().st_mtime if p.exists() else None
                if m != hist_mtime:
                    hist_mtime, self.hist = m, summarize(load_history(self.home))
                st["hist"] = getattr(self, "hist", summarize([]))
                self.state = st
                self.polled.set()
            except Exception:
                log.exception("panel status poll failed")
            self.kick.wait(1.5)
            self.kick.clear()

    def _tick(self):
        if not self.visible:
            return
        try:
            self.render()
        finally:
            self.top.after(300, self._tick)

    def _anim(self):
        if not self.visible:
            return
        t = time.time()
        h = self.state.get("health")
        col = {"online": GREEN, "loading": AMBER}.get(h, RED)
        if time.time() < self.starting_until and h == "offline":
            col = AMBER
        k = 0.5 + 0.5 * math.sin(t * 3.2)
        self.c.itemconfig(self.pill_dot, fill=mix(col, BG, 0.55 * k) if col != RED else col)
        g = self.state.get("gpu")
        target = g["used"] / g["total"] if g else 0.0
        self.g_shown += (target - self.g_shown) * 0.16
        if abs(self.g_shown - self.g_drawn) > 0.0015:
            self.g_drawn = self.g_shown
            d = self.S(140)
            self._gauge = photo(gauge(d, self.g_shown, SURFACE), self.root)
            self.c.itemconfig(self.gauge_img, image=self._gauge)
        self.top.after(40, self._anim)

    # ---- render: state -> canvas items (cheap; images are cached)
    def render(self):
        st, a, c, S = self.state, self.app, self.c, self.S
        now = time.time()
        h, g, model = st.get("health", "offline"), st.get("gpu"), st.get("model") or {}
        starting = now < self.starting_until
        if h != "offline":
            self.starting_until = 0.0
            starting = False
        elif starting and self.proc is not None and self.proc.poll() is not None:   # run-bolt.ps1 died early
            self.starting_until, starting = 0.0, False
            self.say(f"Bolt exited (code {self.proc.returncode}) before it came up. See bolt.log (Logs).", RED, 20)
            self.proc = None
        elif self.starting_until and not starting and h == "offline":
            self.starting_until = 0.0
            self.say("Bolt didn't come up. See bolt.log (Logs).", RED)
        if self.armed and now > self.armed_until:
            self.armed = None

        # pill
        label, col = {"online": ("ONLINE", GREEN), "loading": ("LOADING", AMBER)}.get(h, ("OFFLINE", RED))
        if starting:
            label, col = "STARTING", AMBER
        pw, ph = S(118), S(30)
        c.itemconfig(self.pill_img, image=self.photo(("pill", col), lambda: rounded(
            pw, ph, 15 * self.s, mix(col, BG, 0.86), mix(col, BG, 0.5), width=self.s)))
        px, py = S(776) - pw, S(30)
        c.coords(self.pill_dot, px + S(14), py + ph // 2 - S(4), px + S(22), py + ph // 2 + S(4))
        c.itemconfig(self.pill_txt, text=label, fill=col)
        tw = self.font("caps", 9).measure(label)
        c.coords(self.pill_txt, px + S(14) + (pw - S(14) - tw) // 2, py + ph // 2)

        # server details
        pct = g["used"] / g["total"] if g else None
        c.itemconfig(self.g_pct, text=f"{pct * 100:.0f}%" if pct is not None else "–",
                     fill=(GREEN if pct < 0.85 else AMBER if pct < CLIFF else RED) if pct is not None else FAINT)
        run = self.running()
        mtxt = " · ".join(x for x in (model.get("name"), model.get("quant"),
                                      {True: "DFlash", False: "no DFlash"}.get(run[1]) if run else None) if x) \
            or {"online": "running (model unknown)", "loading": "loading…"}.get(h, "not running")
        vals = {"MODEL": mtxt, "GPU": g["name"] if g else "no NVIDIA GPU found",
                "MEMORY": f"{g['used'] / 1024:.1f} / {g['total'] / 1024:.1f} GB  ·  {g['util']}% busy" if g else "–"}
        f = self.font("textb", 9)
        for k, item in self.rows.items():
            c.itemconfig(item, text=self.fit(vals[k], f, S(184)))
        self.paint_dd()

        # server buttons: one wide Start, or Stop + Restart while Bolt is up
        p = self.profile()
        pending = bool(run and p and (run[0] != p["name"] or run[1] not in (None, p["dflash"])))
        busy = a.agent is not None
        up = h in ("online", "loading") and not starting
        for tag, vis in (("srv", not up), ("stop", up), ("restart", up)):
            c.itemconfig(tag, state="normal" if vis else "hidden")
        if not self.local:
            self.set_button("srv", "Remote server", ICON["lock"], "off")
        elif not self.script.exists():
            self.set_button("srv", "run-bolt.ps1 missing", ICON["warn"], "off")
        elif not p:
            self.set_button("srv", "No Bolt GGUF found", ICON["warn"], "off")
        elif starting:
            dots = "." * (int(now * 2.5) % 4)
            self.set_button("srv", f"Starting{dots:<3}", ICON["sync"], "off")
        elif self.armed == "start":
            self.set_button("srv", "VRAM busy · start anyway?", ICON["warn"], "warn")
        elif not up:
            self.set_button("srv", "Start Bolt", ICON["play"], "primary")
        if up:
            self.set_button("stop", "Confirm?" if self.armed == "stop" else "Stop", ICON["stop"],
                            "danger" if self.armed == "stop" else "off" if busy else "ghost")
            if self.armed == "restart":
                self.set_button("restart", "Confirm?", ICON["retry"], "warn")
            elif h == "loading":
                dots = "." * (int(now * 2.5) % 4)
                self.set_button("restart", f"Loading{dots:<3}", ICON["sync"], "off")
            else:
                self.set_button("restart", "Switch" if pending else "Restart", ICON["retry"],
                                "off" if busy or not p else "primary" if pending else "ghost")
        if now > self.msg_until:
            self.msg = ""
        c.itemconfig(self.msg_item, text=self.fit(self.msg, self.font("text", 7), S(184)), fill=self.msg_color)

        # today
        hs = st.get("hist") or summarize([])
        vals = (f"{hs['words']:,}", f"{hs['latency']:.2f}s" if hs["latency"] is not None else "–", f"{hs['runs']}")
        subs = (f"{hs['dictations']} dictation{'s' * (hs['dictations'] != 1)}", "speech → clean text",
                f"{hs['verified']} verified on screen")
        for (v, sub), val, s2 in zip(self.stats, vals, subs):
            c.itemconfig(v, text=val)
            c.itemconfig(sub, text=s2)

        # recent
        rec = hs["recent"]
        f = self.font("text", 9)
        for (t_i, g_i, x_i, m_i), e in zip(self.recent, rec + [None] * (5 - len(rec))):
            if e is None:
                for it in (t_i, g_i, x_i, m_i):
                    c.itemconfig(it, text="")
                continue
            ts = str(e.get("ts", ""))[11:16]
            if e.get("type") == "command" and isinstance(e.get("agent"), dict):
                o = e["agent"]
                stt = o.get("status")
                kind = ("ok" if o.get("verified") else "warn") if stt == "done" else \
                    {"question": "ask", "aborted": "info"}.get(stt, "fail")
                glyph, color = GLYPH[kind], KIND[kind]
                meta = f"agent · {o.get('steps', 0)} step{'s' * (o.get('steps', 0) != 1)}"
            else:
                glyph, color = ICON["mic"], GREEN if e.get("used") == "cleaned" else MUTED
                lat = latency(e)
                meta = f"{lat:.1f}s" if lat is not None else ""
            txt = e.get("final", "")
            if self.privacy:
                txt = " ".join("•" * min(len(w), 8) for w in txt.split())
            mw = self.font("text", 8).measure(meta)
            c.itemconfig(t_i, text=ts)
            c.itemconfig(g_i, text=glyph, fill=color)
            c.itemconfig(x_i, text=self.fit(txt, f, S(368 - 86 - 24) - mw - S(10)))
            c.itemconfig(m_i, text=meta)
        c.itemconfig(self.empty, text="" if rec else "Nothing yet. Hold Ctrl+Win and speak.")
        c.itemconfig(self.eye, text=ICON["hide"] if self.privacy else ICON["view"], fill=GREEN if self.privacy else MUTED)

        # toggles + actions
        for t in self.toggles:
            on = bool(t["get"]())
            if t["shown"] != on:
                t["shown"] = on
                c.itemconfig(t["img"], image=self.toggle_img(on))
        self.set_button("retry", "Retry", ICON["retry"], "ghost" if a.last_failed is not None else "off")
        copied = now < self.flash.get("copy", 0)
        self.set_button("copy", "Copied" if copied else "Copy raw", ICON["check"] if copied else ICON["copy"],
                        "ghost" if a.last_raw else "off")

    def say(self, text, color=MUTED, secs=8):
        self.msg, self.msg_color, self.msg_until = text, color, time.time() + secs

    # ---- actions
    def _armed(self, what):
        return self.armed == what and time.time() < self.armed_until

    def _usable(self):
        if not self.local or not self.script.exists():
            return False
        if self.app.agent is not None:
            self.say("The agent is running. Stop it first (any key).", AMBER)
            return False
        return True

    def on_start(self):
        now, p = time.time(), self.profile()
        if not self._usable() or not p or now < self.starting_until or self.state.get("health") != "offline":
            return
        g = self.state.get("gpu")
        risky = bool(g) and g["used"] + p["est"] > CLIFF * g["total"]
        if risky and not self._armed("start"):
            self.armed, self.armed_until = "start", now + 5
            self.say(f"{g['used'] / 1024:.1f} GB already in use; this profile needs ~{p['est'] / 1024:.1f} GB. "
                     "Over 92%, Windows pages VRAM.", AMBER, 5)
        else:
            self.armed = None
            self.starting_until = now + 180
            threading.Thread(target=self._start, daemon=True).start()
        self.render()

    def on_stop(self):
        if not self._usable():
            return
        if self._armed("stop"):
            self.armed = None
            self.say("Stopping Bolt…")
            threading.Thread(target=self._stop, daemon=True).start()
        else:
            self.armed, self.armed_until = "stop", time.time() + 4
            self.say("Dictation and the agent stop working until Bolt is started again.", MUTED, 4)
        self.render()

    def on_restart(self):
        p = self.profile()
        if not self._usable() or not p or self.state.get("health") != "online":
            return
        if self._armed("restart"):
            self.armed = None
            self.say(f"Restarting with {p['label']}…")
            threading.Thread(target=self._restart, daemon=True).start()
        else:
            self.armed, self.armed_until = "restart", time.time() + 4
            self.say(f"Restart with {p['label']} (~{p['est'] / 1024:.1f} GB)? Dictation pauses ~20 s.", MUTED, 4)
        self.render()

    def _start(self):
        p = self.profile()
        try:
            self.proc = start_bolt(self.script, self.home / "bolt.log", p["file"], p["dflash"])
            self.launched = (p["name"], p["dflash"])
            self.say(f"Starting {p['label']}… output goes to bolt.log", MUTED, 20)
        except Exception as e:
            self.starting_until = 0.0
            self.say(f"Couldn't start Bolt: {e}", RED)
        self.kick.set()

    def _stop(self):
        try:
            ok, why = stop_bolt(self.port)
        except Exception as e:
            ok, why = False, f"Stop failed: {e}"
        self.say(why, MUTED if ok else RED)
        self.kick.set()
        return ok

    def _restart(self):
        """Stop, wait for the port and VRAM to be released, re-check the fit, start the chosen profile."""
        if not self._stop():
            return
        t0 = time.time()
        while port_owner(self.port) and time.time() - t0 < 15:
            time.sleep(0.3)
        time.sleep(1.5)                          # the driver frees the old allocation a moment later
        p, g = self.profile(), gpu()
        if g and g["used"] + p["est"] > CLIFF * g["total"]:
            self.say(f"Stopped, but {g['used'] / 1024:.1f} GB is still in use: press Start to start anyway.", AMBER, 15)
            self.kick.set()
            return
        self.starting_until = time.time() + 180
        self._start()

    def flip_warm(self):
        threading.Thread(target=self.app.toggle_warm, daemon=True).start()   # opens/closes the mic: not on Tk's thread

    def flip_privacy(self):
        self.privacy = not self.privacy
        self.render()

    def do_retry(self):
        self.app.retry()
        self.say("Retrying the last failed dictation…")

    def do_copy(self):
        self.app.copy_raw()                     # through the job thread: keeps the private-clipboard marking
        self.flash["copy"] = time.time() + 1.6

    def do_forget(self):
        self.app.forget()
        self.say("Conversation forgotten. The next command starts fresh.")

    def do_logs(self):
        os.startfile(self.home)

    def screenshot(self, path):
        self.render()
        self.top.update()
        grab(self.hwnd, path)
