import os
import io
import re
import time
import json
import glob
import shlex
import base64
import shutil
import configparser
import subprocess
import threading
import mimetypes
from pathlib import Path

from flask import (
    Flask,
    render_template,
    request,
    jsonify,
    Response,
    stream_with_context,
    send_file,
)
from mss import mss
from PIL import Image

try:
    import psutil
except Exception:
    psutil = None

app = Flask(__name__)

DISPLAY = ":1"
SCREEN_W = int(os.environ.get("EMULATOR_WIDTH", "1280"))
SCREEN_H = int(os.environ.get("EMULATOR_HEIGHT", "720"))

FRAME_QUALITY = int(os.environ.get("EMULATOR_JPEG_QUALITY", "68"))
CAPTURE_FPS = int(os.environ.get("EMULATOR_FPS", "24"))

latest_frame = None
frame_condition = threading.Condition()
ready_event = threading.Event()

apps_cache = []
apps_lock = threading.Lock()
apps_state_lock = threading.Lock()
apps_revision = 0
apps_signature = None

APP_STATE_DIR = Path.home() / ".config" / "FireCloudSE"
LOCAL_APPS_DIR = Path.home() / ".local/share/applications"
DOWNLOADS_DIR = Path.home() / "Downloads"
WALLPAPER_FILE = str(APP_STATE_DIR / "wallpaper.png")
WALLPAPER_STATE_FILE = APP_STATE_DIR / "wallpaper.json"

FILE_ROOTS = [
    Path.home().resolve(),
    Path("/usr/share/applications").resolve(),
    Path("/usr/bin").resolve(),
    Path("/usr/local/bin").resolve(),
    Path("/tmp").resolve(),
    DOWNLOADS_DIR.resolve(),
    Path(WALLPAPER_FILE).parent.resolve(),
]

EDIT_ROOTS = [
    Path.home().resolve(),
    Path("/tmp").resolve(),
    DOWNLOADS_DIR.resolve(),
    Path(WALLPAPER_FILE).parent.resolve(),
]

DELETABLE_ROOTS = [
    Path.home().resolve(),
    DOWNLOADS_DIR.resolve(),
    Path("/tmp").resolve(),
    Path(WALLPAPER_FILE).parent.resolve(),
]

ICON_CACHE = {}
WALLPAPER_STATE = {"mode": "preset", "name": "classic", "image_path": ""}


def sanitize_filename(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name or "")
    name = name.strip("._-")
    return name or "download"


def env_display(extra=None):
    env = os.environ.copy()
    env["DISPLAY"] = DISPLAY
    env.setdefault("XDG_RUNTIME_DIR", "/tmp/runtime-codespace")
    env.setdefault("QT_QPA_PLATFORM", "xcb")
    if extra:
        env.update(extra)
    return env


def ensure_dirs():
    os.makedirs("/tmp/.X11-unix", exist_ok=True)
    try:
        os.chmod("/tmp/.X11-unix", 0o1777)
    except Exception:
        pass
    os.makedirs("/tmp/runtime-codespace", exist_ok=True)
    try:
        os.chmod("/tmp/runtime-codespace", 0o700)
    except Exception:
        pass
    APP_STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOCAL_APPS_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)


def popen_bg(cmd, session=False, extra_env=None):
    env = env_display(extra_env)
    if session:
        cmd = ["dbus-run-session", "--"] + cmd
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)


def run_fg(cmd):
    return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env_display(), check=False)


def clean_exec_cmd(exec_cmd: str) -> str:
    if not exec_cmd:
        return ""
    cleaned = re.sub(r"%[A-Za-z]", "", exec_cmd)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def extract_program_from_exec(exec_cmd: str) -> str | None:
    cmd = clean_exec_cmd(exec_cmd)
    if not cmd:
        return None
    try:
        tokens = shlex.split(cmd)
    except Exception:
        tokens = cmd.split()
    if not tokens:
        return None
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t == "env":
            i += 1
            continue
        if "=" in t and not t.startswith("/"):
            i += 1
            continue
        break
    if i >= len(tokens):
        return None
    return tokens[i]


def resolve_binary_from_exec(exec_cmd: str) -> str | None:
    program = extract_program_from_exec(exec_cmd)
    if not program:
        return None
    if os.path.isabs(program) and os.path.exists(program):
        return program
    found = shutil.which(program)
    if found:
        return found
    return None


def guess_icon_name(text: str):
    t = (text or "").lower()
    rules = [
        ("firefox", "firefox"),
        ("chromium", "chromium"),
        ("chrome", "google-chrome"),
        ("brave", "brave-browser"),
        ("blender", "blender"),
        ("xterm", "utilities-terminal"),
        ("uxterm", "utilities-terminal"),
        ("terminal", "utilities-terminal"),
        ("konsole", "utilities-terminal"),
        ("gnome-terminal", "utilities-terminal"),
        ("files", "system-file-manager"),
        ("nautilus", "system-file-manager"),
        ("dolphin", "system-file-manager"),
        ("thunar", "system-file-manager"),
        ("caja", "system-file-manager"),
        ("nemo", "system-file-manager"),
        ("gedit", "accessories-text-editor"),
        ("mousepad", "accessories-text-editor"),
        ("leafpad", "accessories-text-editor"),
        ("kate", "accessories-text-editor"),
        ("geany", "accessories-text-editor"),
        ("libreoffice", "libreoffice-writer"),
        ("code", "code"),
        ("vscode", "code"),
        ("editor", "accessories-text-editor"),
        ("notes", "accessories-text-editor"),
        ("notas", "accessories-text-editor"),
    ]
    for k, icon in rules:
        if k in t:
            return icon
    return "application-x-executable"


def icon_search_paths():
    return [
        Path("/usr/share/icons"),
        Path("/usr/share/pixmaps"),
        Path.home() / ".local/share/icons",
        Path("/usr/share/icons/hicolor"),
        Path("/usr/share/icons/Adwaita"),
    ]


def resolve_icon_path(icon_name: str):
    if not icon_name:
        return None
    if icon_name in ICON_CACHE:
        return ICON_CACHE[icon_name] or None
    p = Path(icon_name)
    if p.is_absolute() and p.exists():
        ICON_CACHE[icon_name] = str(p)
        return str(p)
    stem = p.stem if p.suffix else icon_name
    candidates = []
    for root in icon_search_paths():
        if not root.exists():
            continue
        try:
            for ext in (".png", ".svg", ".xpm"):
                for found in root.rglob(stem + ext):
                    if found.is_file():
                        candidates.append(found)
            for found in root.rglob(icon_name):
                if found.is_file():
                    candidates.append(found)
        except Exception:
            continue
    if candidates:
        def score(path):
            s = str(path).lower()
            bonus = 0
            if "/48x48/" in s or "/64x64/" in s or "/128x128/" in s or "/scalable/" in s:
                bonus += 10
            if "/apps/" in s:
                bonus += 5
            if path.suffix.lower() == ".png":
                bonus += 2
            return bonus
        candidates.sort(key=score, reverse=True)
        ICON_CACHE[icon_name] = str(candidates[0])
        return str(candidates[0])
    ICON_CACHE[icon_name] = ""
    return None


def compute_apps_signature(items):
    try:
        return tuple(sorted((a.get("name", ""), a.get("exec", ""), a.get("path", "")) for a in items))
    except Exception:
        return None


def load_apps():
    locations = ["/usr/share/applications", str(Path.home() / ".local/share/applications")]
    seen = set()
    items = []
    for folder in locations:
        for path in glob.glob(os.path.join(folder, "*.desktop")):
            cp = configparser.ConfigParser(interpolation=None)
            try:
                cp.read(path, encoding="utf-8")
                if "Desktop Entry" not in cp:
                    continue
                entry = cp["Desktop Entry"]
                name = entry.get("Name", "").strip()
                exec_cmd = entry.get("Exec", "").strip()
                icon = entry.get("Icon", "").strip()
                no_display = entry.get("NoDisplay", "false").lower() == "true"
                hidden = entry.get("Hidden", "false").lower() == "true"
                terminal = entry.get("Terminal", "false").lower() == "true"
                if not name or not exec_cmd or no_display or hidden:
                    continue
                key = name.lower()
                if key in seen:
                    continue
                seen.add(key)
                path_resolved = str(Path(path).resolve())
                items.append({
                    "name": name,
                    "exec": exec_cmd,
                    "terminal": terminal,
                    "icon": icon or guess_icon_name(name),
                    "path": path_resolved,
                    "deletable": path_resolved.startswith(str(LOCAL_APPS_DIR.resolve())),
                })
            except Exception:
                continue
    items.sort(key=lambda x: x["name"].lower())
    for i, a in enumerate(items):
        if a["name"].lower() == "terminal":
            items.insert(0, items.pop(i))
            break
    else:
        items.insert(0, {
            "name": "Terminal",
            "exec": "xterm",
            "terminal": True,
            "icon": "utilities-terminal",
            "path": "",
            "deletable": False,
        })
    return items


def refresh_apps_cache():
    global apps_cache, apps_revision, apps_signature
    with apps_lock:
        new_items = load_apps()
        new_sig = compute_apps_signature(new_items)
        changed = new_sig != apps_signature
        apps_cache = new_items
        apps_signature = new_sig
        if changed:
            with apps_state_lock:
                apps_revision += 1


def watch_apps_loop():
    last_mtime = None
    while True:
        try:
            newest = 0.0
            for folder in ("/usr/share/applications", str(Path.home() / ".local/share/applications")):
                if os.path.isdir(folder):
                    for f in glob.glob(os.path.join(folder, "*.desktop")):
                        try:
                            newest = max(newest, os.path.getmtime(f))
                        except Exception:
                            pass
            if last_mtime is None:
                last_mtime = newest
            elif newest != last_mtime:
                last_mtime = newest
                refresh_apps_cache()
        except Exception:
            pass
        time.sleep(3)


def load_wallpaper_state():
    global WALLPAPER_STATE
    try:
        if WALLPAPER_STATE_FILE.exists():
            data = json.loads(WALLPAPER_STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                WALLPAPER_STATE.update({
                    "mode": data.get("mode", "preset"),
                    "name": data.get("name", "classic"),
                    "image_path": data.get("image_path", ""),
                })
    except Exception:
        pass


def save_wallpaper_state():
    try:
        WALLPAPER_STATE_FILE.write_text(json.dumps(WALLPAPER_STATE, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def set_solid_wallpaper(color: str):
    run_fg(["xsetroot", "-solid", color])


def set_image_wallpaper(file_path: str):
    if shutil.which("feh") and file_path and os.path.exists(file_path):
        subprocess.Popen(["feh", "--bg-fill", file_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env_display())


def apply_wallpaper(name: str):
    presets = {
        "classic": "#d6d6d6",
        "light": "#e7e7e7",
        "gray": "#bdbdbd",
        "dark": "#2b2b2b",
        "blue": "#8ba8c9",
        "green": "#98bfa2",
        "sunset": "#d8a08f",
    }
    color = presets.get(name, "#d6d6d6")
    set_solid_wallpaper(color)
    WALLPAPER_STATE["mode"] = "preset"
    WALLPAPER_STATE["name"] = name
    WALLPAPER_STATE["image_path"] = ""
    save_wallpaper_state()
    return color


def apply_saved_wallpaper():
    mode = WALLPAPER_STATE.get("mode", "preset")
    if mode == "image":
        img = WALLPAPER_STATE.get("image_path", "")
        if img and os.path.exists(img):
            set_image_wallpaper(img)
            return
    apply_wallpaper(WALLPAPER_STATE.get("name", "classic"))


def save_wallpaper_image(data_url: str):
    if "," not in data_url:
        return False
    _, b64data = data_url.split(",", 1)
    try:
        raw = base64.b64decode(b64data)
    except Exception:
        return False
    try:
        with open(WALLPAPER_FILE, "wb") as f:
            f.write(raw)
        set_image_wallpaper(WALLPAPER_FILE)
        WALLPAPER_STATE["mode"] = "image"
        WALLPAPER_STATE["name"] = ""
        WALLPAPER_STATE["image_path"] = WALLPAPER_FILE
        save_wallpaper_state()
        return True
    except Exception:
        return False


def ensure_pulseaudio():
    if shutil.which("pulseaudio"):
        try:
            subprocess.Popen(["pulseaudio", "--start", "--exit-idle-time=-1"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env_display())
            time.sleep(0.6)
        except Exception:
            pass
    if not shutil.which("pactl"):
        return
    try:
        sinks = subprocess.run(["pactl", "list", "short", "sinks"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env_display(), check=False).stdout
        if "cloudsink" not in sinks:
            subprocess.run(
                ["pactl", "load-module", "module-null-sink", "sink_name=cloudsink", "sink_properties=device.description=cloudsink"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env_display(),
                check=False,
            )
            time.sleep(0.3)
        subprocess.run(["pactl", "set-default-sink", "cloudsink"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env_display(), check=False)
        subprocess.run(["pactl", "set-default-source", "cloudsink.monitor"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env_display(), check=False)
    except Exception:
        pass


def browser_command(browser: str):
    browser = browser.lower()
    if browser in ("chromium", "chromium-browser", "google-chrome", "brave-browser"):
        return (
            f'{browser} --new-window --disable-gpu --disable-dev-shm-usage '
            f'--no-sandbox --disable-software-rasterizer '
            f'--enable-features=UseOzonePlatform --ozone-platform=x11'
        )
    if browser == "firefox":
        return "firefox --new-window"
    return browser


def _popen_command(cmd: str, extra_env=None):
    env = env_display(extra_env)
    cmd = cmd.strip()
    if not cmd:
        return False
    try:
        subprocess.Popen(["bash", "-lc", f"nohup {cmd} >/dev/null 2>&1 &"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, start_new_session=True)
        return True
    except Exception as e:
        print("launch error:", e)
        return False


def launch_raw_command(cmd: str):
    cmd = clean_exec_cmd(cmd)
    if not cmd:
        return False
    extra_env = {"PULSE_SINK": "cloudsink", "PULSE_SOURCE": "cloudsink.monitor"}
    low = cmd.lower()
    if any(x in low for x in ("firefox", "chromium", "google-chrome", "brave")):
        extra_env["MOZ_DISABLE_CONTENT_SANDBOX"] = "1"
        extra_env["NO_AT_BRIDGE"] = "1"
    return _popen_command(cmd, extra_env=extra_env)



def launch_terminal_script(title: str, script: str):
    env = env_display({"PULSE_SINK": "cloudsink", "PULSE_SOURCE": "cloudsink.monitor"})
    script = script.strip()
    if not script:
        return False

    script_dir = APP_STATE_DIR / "terminal_scripts"
    try:
        script_dir.mkdir(parents=True, exist_ok=True)
        script_file = script_dir / f"{sanitize_filename(title or 'terminal')}_{time.time_ns()}.sh"
        script_file.write_text("#!/usr/bin/env bash\nset -u\n" + script + "\n", encoding="utf-8")
        script_file.chmod(0o755)
    except Exception as e:
        print("terminal script write error:", e)
        return False

    terminal_candidates = [
        ["xterm", "-T", title or "Terminal", "-hold", "-e", "bash", str(script_file)],
        ["konsole", "-p", f'tabtitle={title or "Terminal"}', "--hold", "-e", "bash", str(script_file)],
        ["xfce4-terminal", "-T", title or "Terminal", "--hold", "-x", "bash", str(script_file)],
        ["lxterminal", "-t", title or "Terminal", "-e", "bash", str(script_file)],
        ["gnome-terminal", "--title", title or "Terminal", "--", "bash", str(script_file)],
    ]

    for cmd in terminal_candidates:
        if shutil.which(cmd[0]):
            try:
                subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=env,
                    start_new_session=True,
                )
                return True
            except Exception as e:
                print("terminal launch error:", e)

    return _popen_command(f'bash "{script_file}"', extra_env={"PULSE_SINK": "cloudsink", "PULSE_SOURCE": "cloudsink.monitor"})


def focus_windows_by_name(name: str):
    deadline = time.time() + 10
    env = env_display()
    while time.time() < deadline:
        try:
            res = subprocess.run(["xdotool", "search", "--onlyvisible", "--name", name], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env, check=False)
            ids = [line.strip() for line in res.stdout.splitlines() if line.strip()]
            if ids:
                win = ids[-1]
                run_fg(["xdotool", "windowactivate", "--sync", win])
                run_fg(["xdotool", "windowraise", win])
                return
        except Exception:
            pass
        time.sleep(0.35)


def launch_and_focus(app_name: str, exec_cmd: str):
    lower = (app_name or "").lower()
    override = None
    if "krita" in lower:
        override = "krita --nosplash"
    elif "terminal" in lower or "xterm" in lower:
        override = "xterm"
    elif "firefox" in lower:
        override = browser_command("firefox")
    elif "chromium" in lower or "chrome" in lower or "brave" in lower:
        override = browser_command("chromium")
    elif "libreoffice" in lower:
        override = "libreoffice"
    elif "code" in lower or "vscode" in lower:
        override = "code"
    elif "blender" in lower:
        override = "blender"
    command_to_run = override or exec_cmd
    ok = launch_raw_command(command_to_run)
    if ok:
        threading.Thread(target=focus_windows_by_name, args=(app_name,), daemon=True).start()
    return ok


def start_desktop():
    ensure_dirs()
    load_wallpaper_state()
    ensure_pulseaudio()
    popen_bg(["Xvfb", DISPLAY, "-screen", "0", f"{SCREEN_W}x{SCREEN_H}x24", "-ac", "-nolisten", "tcp"])
    time.sleep(1.2)
    popen_bg(["openbox"], session=True)
    time.sleep(0.9)
    if shutil.which("xterm"):
        popen_bg(["xterm"], session=True)
    apply_saved_wallpaper()
    refresh_apps_cache()
    ready_event.set()
    threading.Thread(target=watch_apps_loop, daemon=True).start()


def capture_loop():
    global latest_frame
    os.environ["DISPLAY"] = DISPLAY
    frame_delay = 1.0 / CAPTURE_FPS
    try:
        with mss(display=DISPLAY) as sct:
            monitor = {"top": 0, "left": 0, "width": SCREEN_W, "height": SCREEN_H}
            while True:
                start = time.time()
                try:
                    shot = sct.grab(monitor)
                    img = Image.frombytes("RGB", shot.size, shot.rgb)
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=FRAME_QUALITY, subsampling=2, optimize=False)
                    data = buf.getvalue()
                    with frame_condition:
                        latest_frame = data
                        frame_condition.notify(1)
                except Exception as e:
                    print("capture error:", e)
                elapsed = time.time() - start
                sleep_for = frame_delay - elapsed
                if sleep_for > 0:
                    time.sleep(sleep_for)
    except Exception as e:
        print("capture init error:", e)


def window_icon_guess(wclass: str, title: str):
    return guess_icon_name(f"{wclass} {title}")


def is_fullscreen_window(wid: str):
    try:
        res = subprocess.run(["xprop", "-id", wid, "_NET_WM_STATE"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env_display(), check=False).stdout.lower()
        return "fullscreen" in res
    except Exception:
        return False


def get_open_windows():
    windows = []
    try:
        res = subprocess.run(["wmctrl", "-l", "-x"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env_display(), check=False)
        for line in res.stdout.splitlines():
            parts = line.split(None, 3)
            if len(parts) < 4:
                continue
            wid, desktop, wclass, title = parts
            if not title.strip():
                continue
            if "openbox" in wclass.lower() or "xfdesktop" in wclass.lower():
                continue
            minimized = False
            try:
                check = subprocess.run(["xwininfo", "-id", wid], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env_display(), check=False)
                for cline in check.stdout.splitlines():
                    if "Map State:" in cline:
                        if "IsUnMapped" in cline or "IsUnviewable" in cline:
                            minimized = True
                        break
            except Exception:
                pass
            windows.append({"id": wid, "title": title, "class": wclass, "minimized": minimized, "fullscreen": is_fullscreen_window(wid), "icon": window_icon_guess(wclass, title)})
    except Exception as e:
        print("window list error:", e)
    return windows


def allowed_root(path: Path, roots):
    try:
        rp = path.expanduser().resolve()
        return any(str(rp).startswith(str(root)) for root in roots)
    except Exception:
        return False


def list_files(path_str: str):
    if not path_str or path_str == "~":
        path = Path.home()
    else:
        path = Path(path_str).expanduser()
    if not path.exists():
        path = Path.home()
    if not allowed_root(path, FILE_ROOTS):
        return None
    try:
        entries = []
        for entry in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            try:
                st = entry.stat()
                entries.append({"name": entry.name, "path": str(entry), "is_dir": entry.is_dir(), "size": st.st_size if entry.is_file() else None, "icon": "folder" if entry.is_dir() else guess_icon_name(entry.name), "can_delete": allowed_root(entry, DELETABLE_ROOTS)})
            except Exception:
                continue
        return {"path": str(path), "entries": entries}
    except Exception:
        return None


def read_text_file(path_str: str):
    path = Path(path_str).expanduser()
    if not path.exists() or not path.is_file():
        return None
    if not allowed_root(path, EDIT_ROOTS) and not allowed_root(path, [Path("/usr/share/applications").resolve()]):
        return None
    try:
        raw = path.read_bytes()
        if b"\x00" in raw[:4096]:
            return None
        text = raw.decode("utf-8", errors="replace")
        return {"path": str(path), "content": text}
    except Exception:
        return None


def save_text_file(path_str: str, content: str):
    path = Path(path_str).expanduser()
    if not path.exists() or not path.is_file():
        return False
    if not allowed_root(path, EDIT_ROOTS):
        return False
    try:
        path.write_text(content, encoding="utf-8")
        return True
    except Exception:
        return False


def is_previewable_media(path_str: str):
    ext = Path(path_str).suffix.lower()
    return ext in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".mp4", ".webm", ".ogg", ".mp3", ".wav", ".m4a", ".pdf", ".txt", ".md", ".json", ".csv"}


def open_with_default(path_str: str):
    path = Path(path_str).expanduser()
    if not path.exists():
        return False
    if not allowed_root(path, FILE_ROOTS):
        return False
    safe = str(path).replace('"', '\\"')
    return launch_raw_command(f'xdg-open "{safe}"')


def delete_app_launcher(path_str: str):
    try:
        path = Path(path_str).expanduser().resolve()
        if not path.exists() or not path.is_file():
            return False, "not found"
        if not str(path).startswith(str(LOCAL_APPS_DIR.resolve())):
            return False, "only local launchers can be deleted"
        path.unlink()
        return True, None
    except Exception as e:
        return False, str(e)


def build_app_info(name: str, exec_cmd: str, path: str):
    launcher_path = Path(path).expanduser() if path else None
    launcher_size = None
    launcher_mtime = None
    deletable = False
    if launcher_path and launcher_path.exists():
        try:
            st = launcher_path.stat()
            launcher_size = st.st_size
            launcher_mtime = st.st_mtime
            deletable = str(launcher_path.resolve()).startswith(str(LOCAL_APPS_DIR.resolve()))
        except Exception:
            pass
    binary_path = resolve_binary_from_exec(exec_cmd)
    binary_size = None
    binary_exists = False
    if binary_path and os.path.exists(binary_path):
        binary_exists = True
        try:
            binary_size = os.path.getsize(binary_path)
        except Exception:
            pass
    process_info = None
    if psutil is not None and binary_path:
        target_base = os.path.basename(binary_path).lower()
        best = None
        try:
            for proc in psutil.process_iter(["pid", "name", "exe", "cmdline", "memory_info", "status"]):
                try:
                    pname = (proc.info.get("name") or "").lower()
                    pexe = os.path.basename(proc.info.get("exe") or "").lower()
                    cmdline = " ".join(proc.info.get("cmdline") or []).lower()
                    if target_base and (target_base == pname or target_base == pexe or target_base in pname or target_base in pexe or target_base in cmdline):
                        best = proc
                        break
                except Exception:
                    continue
        except Exception:
            best = None
        if best is not None:
            try:
                process_info = {
                    "pid": best.pid,
                    "name": best.name(),
                    "exe": best.exe() if best.exe() else None,
                    "cpu": best.cpu_percent(interval=0.0),
                    "rss": best.memory_info().rss if best.memory_info() else None,
                    "status": best.status(),
                }
            except Exception:
                process_info = None
    return {
        "name": name,
        "exec": exec_cmd,
        "path": path,
        "launcher_size": launcher_size,
        "launcher_mtime": launcher_mtime,
        "binary_path": binary_path,
        "binary_exists": binary_exists,
        "binary_size": binary_size,
        "deletable": deletable,
        "process": process_info,
    }


def runtime_status():
    return {"wine": bool(shutil.which("wine")), "waydroid": bool(shutil.which("waydroid")), "pkexec": bool(shutil.which("pkexec")), "sudo": bool(shutil.which("sudo"))}


def scan_exec_files(kind: str):
    kind = (kind or "").lower().strip()
    ext = ".exe" if kind == "exe" else ".apk"
    roots = [DOWNLOADS_DIR.expanduser().resolve(), Path.home().expanduser().resolve()]
    seen = set()
    items = []
    for root in roots:
        if not root.exists():
            continue
        for base, dirs, files in os.walk(root):
            try:
                rel = Path(base).resolve().relative_to(root)
                if len(rel.parts) > 4:
                    dirs[:] = []
                    continue
            except Exception:
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "__pycache__")]
            for fname in files:
                if not fname.lower().endswith(ext):
                    continue
                p = Path(base) / fname
                try:
                    rp = str(p.resolve())
                except Exception:
                    continue
                if rp in seen:
                    continue
                seen.add(rp)
                try:
                    st = p.stat()
                except Exception:
                    continue
                items.append({
                    "name": p.name,
                    "path": str(p),
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                    "icon": "application-x-executable" if kind == "exe" else "application-vnd.android.package-archive",
                })
    items.sort(key=lambda x: x.get("mtime", 0), reverse=True)
    return items



def launch_exec_file(path_str: str, kind: str):
    path = Path(path_str).expanduser()
    if not path.exists() or not path.is_file():
        return False, "file not found"

    kind = (kind or "").lower().strip()
    safe = shlex.quote(str(path))
    ext = path.suffix.lower()

    if kind == "exe" or ext == ".exe":
        if not (shutil.which("wine64") or shutil.which("wine")):
            return False, "Wine no está instalado"

        wine_cmd = "wine64" if shutil.which("wine64") else "wine"
        wine_prefix = shlex.quote(str(APP_STATE_DIR / "wineprefix"))

        script = (
            'echo "Preparando Wine..."; '
            f'export WINEPREFIX={wine_prefix}; '
            'export WINEARCH=win64; '
            'export WINEDEBUG=-all; '
            'mkdir -p "$WINEPREFIX"; '
            f'{wine_cmd} wineboot -u >/dev/null 2>&1 || true; '
            'if command -v winetricks >/dev/null 2>&1; then '
            '  echo "Instalando runtimes de Visual C++ en el prefijo..."; '
            '  winetricks -q vcrun2022 vcrun2019 corefonts >/dev/null 2>&1 || true; '
            'fi; '
            f'echo "Ejecutando EXE: {safe}"; '
            f'{wine_cmd} {safe}; status=$?; '
            'echo; echo "Código de salida: $status"; '
            'read -n 1 -s -r -p "Pulsa una tecla para cerrar..."'
        )
        ok = launch_terminal_script("Ejecutando EXE", script)
        return ok, None if ok else "No se pudo ejecutar el .exe"

    if kind == "apk" or ext == ".apk":
        if not shutil.which("waydroid"):
            return False, "Waydroid no está instalado"

        script = (
            'echo "Preparando Waydroid..."; '
            'waydroid container start >/dev/null 2>&1 || true; '
            'sleep 2; '
            'waydroid session start >/dev/null 2>&1 || true; '
            'sleep 2; '
            f'echo "Instalando APK: {safe}"; '
            f'waydroid app install {safe}; status=$?; '
            'echo; echo "Código de salida: $status"; '
            'read -n 1 -s -r -p "Pulsa una tecla para cerrar..."'
        )
        ok = launch_terminal_script("Instalando APK", script)
        return ok, None if ok else "No se pudo instalar el .apk"

    return False, "tipo no soportado"


def install_runtime_helper(runtime: str):
    runtime = (runtime or "").lower().strip()
    if runtime == "wine":
        pkg_title = "Instalando Wine"
        pkg_label = "Wine"
        extra = (
            'export DEBIAN_FRONTEND=noninteractive; '
            'if ! command -v apt-get >/dev/null 2>&1; then '
            'echo "apt-get no está disponible en este sistema."; exit 1; fi; '
            'if command -v add-apt-repository >/dev/null 2>&1; then '
            '  add-apt-repository -y universe >/dev/null 2>&1 || true; '
            'fi; '
            'dpkg --add-architecture i386 >/dev/null 2>&1 || true; '
            'apt-get update; '
            'apt-get install -y wine64 wine32 winetricks || apt-get install -y wine winetricks || true'
        )
    elif runtime == "waydroid":
        pkg_title = "Instalando Waydroid"
        pkg_label = "Waydroid"
        extra = (
            'export DEBIAN_FRONTEND=noninteractive; '
            'if ! command -v apt-get >/dev/null 2>&1; then '
            'echo "apt-get no está disponible en este sistema."; exit 1; fi; '
            'if ! command -v curl >/dev/null 2>&1; then '
            '  apt-get update && apt-get install -y curl ca-certificates lsb-release gnupg; '
            'fi; '
            'dist="$(lsb_release -cs 2>/dev/null || true)"; '
            'if [ -n "$dist" ]; then '
            '  curl -fsSL https://repo.waydro.id | bash -s -- -s "$dist"; '
            'else '
            '  curl -fsSL https://repo.waydro.id | bash; '
            'fi; '
            'apt-get update; '
            'apt-get install -y waydroid || { echo "Waydroid no está disponible en los repositorios configurados."; exit 2; }; '
            'systemctl enable --now waydroid-container >/dev/null 2>&1 || true; '
            'waydroid init >/dev/null 2>&1 || true'
        )
    else:
        return False

    if shutil.which("pkexec"):
        script = (
            f'echo "Se ha abierto la instalación de {pkg_label}."; '
            f'echo "Si el gestor te pide contraseña, acéptala."; '
            f'pkexec bash -lc {shlex.quote(extra)}; status=$?; '
            f'echo; echo "Código de salida: $status"; '
            f'read -n 1 -s -r -p "Pulsa una tecla para cerrar..."'
        )
        return launch_terminal_script(pkg_title, script)

    if shutil.which("sudo"):
        script = (
            f'echo "Se ha abierto la instalación de {pkg_label}."; '
            f'sudo bash -lc {shlex.quote(extra)}; status=$?; '
            f'echo; echo "Código de salida: $status"; '
            f'read -n 1 -s -r -p "Pulsa una tecla para cerrar..."'
        )
        return launch_terminal_script(pkg_title, script)

    script = (
        f'echo "No hay pkexec/sudo disponible para instalar {pkg_label}."; '
        f'echo "Abre una terminal y ejecuta manualmente el instalador."; '
        f'read -n 1 -s -r -p "Pulsa una tecla para cerrar..."'
    )
    return launch_terminal_script(pkg_title, script)

SPECIAL_KEYS =SPECIAL_KEYS = {
    "Enter": "Return",
    "Backspace": "BackSpace",
    "Tab": "Tab",
    "Escape": "Escape",
    "Delete": "Delete",
    "Home": "Home",
    "End": "End",
    "PageUp": "Page_Up",
    "PageDown": "Page_Down",
    "ArrowLeft": "Left",
    "ArrowRight": "Right",
    "ArrowUp": "Up",
    "ArrowDown": "Down",
    " ": "space",
}


def xdotool_run(*args):
    subprocess.run(["xdotool", *args], env=env_display(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def xdotool_key_combo(data):
    key = str(data.get("key", "")).strip()
    if not key:
        return None
    mods = []
    if data.get("ctrl"):
        mods.append("ctrl")
    if data.get("alt"):
        mods.append("alt")
    if data.get("shift"):
        mods.append("shift")
    if data.get("meta"):
        mods.append("super")
    mapped = SPECIAL_KEYS.get(key, key)
    return "+".join(mods + [mapped]) if mods else mapped


@app.route("/")
def FireCloudServer():
    return render_template("FireCloudServer.html", wallpaper_state=WALLPAPER_STATE, screen_w=SCREEN_W, screen_h=SCREEN_H)


@app.route("/ready")
def ready():
    return jsonify(ready=ready_event.is_set())


@app.route("/stream")
def stream():
    def generate():
        while True:
            with frame_condition:
                frame_condition.wait()
                if latest_frame is None:
                    continue
                data = latest_frame
            yield (b"--frame\r\n" b"Content-Type: image/jpeg\r\n\r\n" + data + b"\r\n")
    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/audio")
def audio_stream():
    if not shutil.which("ffmpeg"):
        return jsonify(ok=False, error="ffmpeg not installed"), 503
    def get_monitor_name():
        try:
            res = subprocess.run(["pactl", "list", "short", "sinks"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env_display(), check=False).stdout
            if "cloudsink" in res:
                return "cloudsink.monitor"
        except Exception:
            pass
        return "@DEFAULT_SINK@.monitor"
    def generate():
        env = env_display({"PULSE_SINK": "cloudsink", "PULSE_SOURCE": "cloudsink.monitor"})
        monitor_name = get_monitor_name()
        cmd = [
            "ffmpeg",
            "-loglevel", "quiet",
            "-fflags", "nobuffer",
            "-flags", "low_delay",
            "-analyzeduration", "0",
            "-probesize", "32",
            "-f", "pulse",
            "-i", monitor_name,
            "-vn",
            "-ac", "2",
            "-ar", "48000",
            "-f", "s16le",
            "pipe:1",
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
        try:
            while True:
                chunk = proc.stdout.read(8192)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                proc.kill()
            except Exception:
                pass
    return Response(stream_with_context(generate()), mimetype="application/octet-stream")


@app.route("/api/apps")
def api_apps():
    with apps_lock:
        return jsonify(apps_cache)


@app.route("/api/apps-revision")
def api_apps_revision():
    with apps_state_lock:
        return jsonify(ok=True, revision=apps_revision)


@app.route("/api/refresh-apps", methods=["POST"])
def api_refresh_apps():
    refresh_apps_cache()
    with apps_lock:
        return jsonify(ok=True, apps=apps_cache)


@app.route("/api/launch", methods=["POST"])
def api_launch():
    data = request.get_json(force=True)
    name = data.get("name", "").strip()
    exec_cmd = data.get("exec", "").strip()
    if name and not exec_cmd:
        with apps_lock:
            for a in apps_cache:
                if a["name"].lower() == name.lower():
                    exec_cmd = a["exec"]
                    name = a["name"]
                    break
    if not exec_cmd:
        return jsonify(ok=False, error="missing exec"), 400
    ok = launch_and_focus(name or "app", exec_cmd)
    refresh_apps_cache()
    return jsonify(ok=ok)


@app.route("/api/apps/delete", methods=["POST"])
def api_apps_delete():
    data = request.get_json(force=True)
    path = data.get("path", "").strip()
    if not path:
        return jsonify(ok=False, error="missing path"), 400
    ok, err = delete_app_launcher(path)
    if not ok:
        return jsonify(ok=False, error=err or "cannot delete"), 403
    refresh_apps_cache()
    return jsonify(ok=True)


@app.route("/api/app-info", methods=["POST"])
def api_app_info():
    data = request.get_json(force=True) or {}
    info = build_app_info(data.get("name", ""), data.get("exec", ""), data.get("path", ""))
    return jsonify(ok=True, **info)


@app.route("/api/download-icon", methods=["POST"])
def api_download_icon():
    data = request.get_json(force=True) or {}
    icon = data.get("icon", "")
    name = data.get("name", "icon")
    src_path = resolve_icon_path(icon)
    if not src_path:
        return jsonify(ok=False, error="icon not found"), 404
    src = Path(src_path)
    ext = src.suffix or ".png"
    dest_name = sanitize_filename(name) + ext
    dest = DOWNLOADS_DIR / dest_name
    try:
        shutil.copy2(src, dest)
        return jsonify(ok=True, path=str(dest), filename=dest.name)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


@app.route("/api/run", methods=["POST"])
def api_run():
    data = request.get_json(force=True)
    cmd = data.get("cmd", "").strip()
    if not cmd:
        return jsonify(ok=False, error="missing cmd"), 400
    ok = launch_raw_command(cmd)
    refresh_apps_cache()
    return jsonify(ok=ok)


@app.route("/api/toolbar-action", methods=["POST"])
def toolbar_action():
    data = request.get_json(force=True)
    action = data.get("action", "").strip()
    if action == "files":
        return jsonify(ok=True, launched="virtual-files")
    elif action == "browser":
        for browser in ("firefox", "chromium-browser", "chromium", "google-chrome", "brave-browser", "epiphany", "midori"):
            if shutil.which(browser):
                launch_raw_command(browser_command(browser))
                return jsonify(ok=True, launched=browser)
        return jsonify(ok=False, error="No browser found"), 400
    elif action == "editor":
        return jsonify(ok=True, launched="virtual-notes")
    elif action == "terminal":
        launch_raw_command("xterm")
        return jsonify(ok=True, launched="xterm")
    return jsonify(ok=False, error="Invalid action"), 400


@app.route("/api/key", methods=["POST"])
def api_key():
    data = request.get_json(force=True)
    combo = xdotool_key_combo(data)
    if not combo:
        return jsonify(ok=False, error="missing key"), 400
    xdotool_run("key", "--clearmodifiers", combo)
    return jsonify(ok=True)


@app.route("/api/type", methods=["POST"])
def api_type():
    data = request.get_json(force=True)
    text = str(data.get("text", ""))
    if text == "":
        return jsonify(ok=False, error="missing text"), 400
    subprocess.run(["xdotool", "type", "--clearmodifiers", "--delay", "0", text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env_display(), check=False)
    return jsonify(ok=True)


@app.route("/api/click", methods=["POST"])
def api_click():
    data = request.get_json(force=True)
    x = int(float(data.get("x", 0)))
    y = int(float(data.get("y", 0)))
    button = int(data.get("button", 1) or 1)
    xdotool_run("mousemove", str(x), str(y))
    xdotool_run("click", str(button))
    return jsonify(ok=True)


@app.route("/api/mousedown", methods=["POST"])
def api_mousedown():
    data = request.get_json(force=True)
    x = int(float(data.get("x", 0)))
    y = int(float(data.get("y", 0)))
    button = int(data.get("button", 1) or 1)
    xdotool_run("mousemove", str(x), str(y))
    xdotool_run("mousedown", str(button))
    return jsonify(ok=True)


@app.route("/api/mouseup", methods=["POST"])
def api_mouseup():
    data = request.get_json(silent=True) or {}
    if "x" in data and "y" in data:
        x = int(float(data["x"]))
        y = int(float(data["y"]))
        xdotool_run("mousemove", str(x), str(y))
    button = int(data.get("button", 1) or 1)
    xdotool_run("mouseup", str(button))
    return jsonify(ok=True)


@app.route("/api/mousemove", methods=["POST"])
def api_mousemove():
    data = request.get_json(force=True)
    x = int(float(data.get("x", 0)))
    y = int(float(data.get("y", 0)))
    xdotool_run("mousemove", str(x), str(y))
    return jsonify(ok=True)


@app.route("/api/scroll", methods=["POST"])
def api_scroll():
    data = request.get_json(force=True)
    direction = data.get("direction", "down")
    button = "4" if direction == "up" else "5"
    xdotool_run("click", button)
    return jsonify(ok=True)


@app.route("/api/windows")
def api_windows():
    return jsonify(get_open_windows())


@app.route("/api/focus", methods=["POST"])
def api_focus():
    data = request.get_json(force=True)
    wid = data.get("id")
    if not wid:
        return jsonify(ok=False), 400
    xdotool_run("windowactivate", "--sync", wid)
    xdotool_run("windowraise", wid)
    return jsonify(ok=True)


@app.route("/api/minimize", methods=["POST"])
def api_minimize():
    data = request.get_json(force=True)
    wid = data.get("id")
    action = data.get("action", "minimize")
    if not wid:
        return jsonify(ok=False), 400
    if action == "minimize":
        xdotool_run("windowminimize", wid)
    else:
        subprocess.run(["wmctrl", "-i", "-r", wid, "-b", "remove,hidden"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env_display(), check=False)
        subprocess.run(["wmctrl", "-i", "-r", wid, "-b", "remove,maximized_vert,maximized_horz"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env_display(), check=False)
        xdotool_run("windowmap", wid)
        time.sleep(0.1)
        xdotool_run("windowactivate", wid)
    return jsonify(ok=True)


@app.route("/api/maximize", methods=["POST"])
def api_maximize():
    data = request.get_json(force=True)
    wid = data.get("id")
    if not wid:
        return jsonify(ok=False), 400
    try:
        res = subprocess.run(
            ["xprop", "-id", wid, "_NET_WM_STATE"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=env_display(),
            check=False,
        ).stdout.lower()
        if "maximized_vert" in res or "maximized_horz" in res:
            subprocess.run(
                ["wmctrl", "-i", "-r", wid, "-b", "remove,maximized_vert,maximized_horz"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env_display(),
                check=False,
            )
        else:
            subprocess.run(
                ["wmctrl", "-i", "-r", wid, "-b", "add,maximized_vert,maximized_horz"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env_display(),
                check=False,
            )
        return jsonify(ok=True)
    except Exception:
        xdotool_run("key", "--window", wid, "alt+F10")
        return jsonify(ok=True)


@app.route("/api/close", methods=["POST"])
def api_close():
    data = request.get_json(force=True)
    wid = data.get("id")
    if not wid:
        return jsonify(ok=False), 400
    xdotool_run("windowactivate", "--sync", wid)
    time.sleep(0.05)
    xdotool_run("key", "alt+F4")
    return jsonify(ok=True)


@app.route("/api/files")
def api_files():
    path = request.args.get("path", "~")
    data = list_files(path)
    if data is None:
        return jsonify(ok=False, error="forbidden or invalid path"), 403
    return jsonify(ok=True, **data)


@app.route("/api/file/read")
def api_file_read():
    path = request.args.get("path", "")
    data = read_text_file(path)
    if data is None:
        return jsonify(ok=False, error="cannot read file"), 400
    return jsonify(ok=True, **data)


@app.route("/api/file/save", methods=["POST"])
def api_file_save():
    data = request.get_json(force=True)
    ok = save_text_file(data.get("path", ""), data.get("content", ""))
    return jsonify(ok=ok)


@app.route("/api/file/open", methods=["POST"])
def api_file_open():
    data = request.get_json(force=True)
    ok = open_with_default(data.get("path", ""))
    return jsonify(ok=ok)


@app.route("/api/file/delete", methods=["POST"])
def api_file_delete():
    data = request.get_json(force=True)
    ok, err = delete_file_or_dir(data.get("path", ""))
    if not ok:
        return jsonify(ok=False, error=err or "cannot delete"), 400
    return jsonify(ok=True)


@app.route("/api/file/raw")
def api_file_raw():
    path = request.args.get("path", "")
    if not path:
        return jsonify(ok=False, error="missing path"), 400
    p = Path(path).expanduser()
    try:
        p = p.resolve()
    except Exception:
        return jsonify(ok=False, error="invalid path"), 400
    if not p.exists() or not p.is_file():
        return jsonify(ok=False, error="not found"), 404
    if not allowed_root(p, FILE_ROOTS) and not allowed_root(p, EDIT_ROOTS):
        return jsonify(ok=False, error="forbidden"), 403
    try:
        mime, _ = mimetypes.guess_type(str(p))
        return send_file(str(p), mimetype=mime or "application/octet-stream")
    except Exception:
        return jsonify(ok=False, error="cannot serve file"), 404

@app.route("/api/icon")
def api_icon():
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify(ok=False, error="missing name"), 400
    path = resolve_icon_path(name)
    if not path:
        return jsonify(ok=False, error="not found"), 404
    try:
        return send_file(path)
    except Exception:
        return jsonify(ok=False, error="cannot serve icon"), 404


@app.route("/api/system")
def api_system():
    disk = shutil.disk_usage("/")
    data = {
        "disk": {
            "total": disk.total,
            "used": disk.used,
            "free": disk.free,
            "percent": round((disk.used / disk.total) * 100, 1) if disk.total else 0,
        },
        "display": f"{SCREEN_W}x{SCREEN_H}",
    }
    if psutil is not None:
        vm = psutil.virtual_memory()
        data["ram"] = {"total": vm.total, "used": vm.used, "free": vm.available, "percent": vm.percent}
        data["cpu"] = psutil.cpu_percent(interval=None)
    else:
        data["ram"] = None
        data["cpu"] = None
    return jsonify(data)


@app.route("/api/wallpaper", methods=["POST"])
def api_wallpaper():
    data = request.get_json(force=True)
    mode = data.get("mode", "preset")
    if mode == "preset":
        name = data.get("name", "classic")
        color = apply_wallpaper(name)
        return jsonify(ok=True, mode="preset", color=color)
    if mode == "image":
        ok = save_wallpaper_image(data.get("dataUrl", ""))
        return jsonify(ok=ok, mode="image")
    return jsonify(ok=False, error="invalid mode"), 400


@app.route("/api/wallpaper-state")
def api_wallpaper_state():
    return jsonify(ok=True, **WALLPAPER_STATE)


@app.route("/api/wallpaper-current")
def api_wallpaper_current():
    if WALLPAPER_STATE.get("mode") == "image" and WALLPAPER_STATE.get("image_path") and os.path.exists(WALLPAPER_STATE["image_path"]):
        return send_file(WALLPAPER_STATE["image_path"])
    return jsonify(ok=False, error="no current image"), 404


if __name__ == "__main__":
    start_desktop()
    threading.Thread(target=capture_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False, threaded=True)
