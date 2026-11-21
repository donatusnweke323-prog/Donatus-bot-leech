# @title ⚡ Zilong Bot — Colab Launcher
# @markdown Credentials go here OR in the Colab 🔑 Secrets panel.
# @markdown Secrets override form values.

API_ID    = 0      # @param {type:"integer"}
API_HASH  = ""     # @param {type:"string"}
BOT_TOKEN = ""     # @param {type:"string"}
OWNER_ID  = 0      # @param {type:"integer"}

FILE_LIMIT_MB = 2048   # @param {type:"integer"}
LOG_CHANNEL   = 0      # @param {type:"integer"}

# @markdown ---
# @markdown ### 👥 Multi-user protection
# @markdown These let allowed users use the bot without blocking your workflow.
OWNER_RESERVED_SLOTS = 1  # @param {type:"integer"}
GUEST_MAX_CONCURRENT = 3  # @param {type:"integer"}
USER_MAX_CONCURRENT  = 2  # @param {type:"integer"}
OWNER_MAX_CONCURRENT = 5  # @param {type:"integer"}

CC_API_KEY        = ""  # @param {type:"string"}
CC_ENGINE_MODE    = "balanced"  # @param ["balanced", "economy"]
FC_API_KEY        = ""  # @param {type:"string"}
FC_WEBHOOK_SECRET = ""  # @param {type:"string"}
SEEDR_USERNAME    = ""  # @param {type:"string"}
SEEDR_PASSWORD    = ""  # @param {type:"string"}
SEEDR_PROXY       = ""  # @param {type:"string"}
WEBHOOK_BASE_URL  = ""  # @param {type:"string"}
CC_WEBHOOK_SECRET = ""  # @param {type:"string"}
NGROK_TOKEN       = ""  # @param {type:"string"}
# @markdown ### 🔒 Private repository access
# @markdown This repo is private: create a GitHub personal access token with
# @markdown repo read access (github.com → Settings → Developer settings →
# @markdown Tokens) and put it in the Colab 🔑 Secrets panel as GITHUB_TOKEN,
# @markdown or paste it here.
GITHUB_TOKEN      = ""  # @param {type:"string"}

# ─────────────────────────────────────────────────────────────────────────────
import glob
import hashlib
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

REPO_URL = "https://github.com/vicMenma/Zilong_multiusage.git"
BASE_DIR = Path("/content/zilong")
DEFAULT_BOT_DATA_DIR = "/content/drive/MyDrive/ZilongBot/data"
DEFAULT_DOWNLOAD_DIR = "/tmp/zilong_dl"
PID_FILE = Path("/tmp/zilong_bot.pid")


def _log(level: str, msg: str) -> None:
    icons = {"INFO": "ℹ️", "OK": "✅", "WARN": "⚠️", "ERR": "❌", "STEP": "🔧"}
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {icons.get(level, '')} {msg}", flush=True)


def _secret(name: str, default: str = "") -> str:
    try:
        from google.colab import userdata  # type: ignore
        val = userdata.get(name)
        if val is not None and str(val).strip():
            return str(val).strip()
    except Exception:
        pass
    return os.environ.get(name, default).strip()


def _secret_int(name: str, default: int = 0, *, minimum: int = 0, maximum: int = 10**18) -> int:
    try:
        val = int(_secret(name, str(default)) or default)
    except Exception:
        val = default
    return max(minimum, min(maximum, val))


def _mount_drive() -> None:
    try:
        from google.colab import drive  # type: ignore
        _log("STEP", "Mounting Google Drive for persistent bot data…")
        drive.mount("/content/drive", force_remount=False)
        _log("OK", "Google Drive mounted")
    except Exception as exc:
        _log("WARN", f"Drive mount skipped/failed: {exc}")


def _github_token() -> str:
    return _secret("GITHUB_TOKEN", GITHUB_TOKEN) or _secret("GH_TOKEN")


def _mask(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text


def _clone_or_update_repo() -> Path:
    # The repository is private: anonymous clones fail with exit 128
    # ("could not read Username"). A GitHub token in Colab Secrets fixes it.
    token = _github_token()
    url = REPO_URL
    if token:
        url = REPO_URL.replace("https://", f"https://x-access-token:{token}@", 1)

    if BASE_DIR.exists() and (BASE_DIR / ".git").exists():
        _log("STEP", "Updating repository…")
        if token:
            subprocess.run(
                ["git", "-C", str(BASE_DIR), "remote", "set-url", "origin", url],
                check=False, capture_output=True,
            )
        pulled = subprocess.run(
            ["git", "-C", str(BASE_DIR), "pull", "--ff-only"],
            check=False, capture_output=True, text=True,
        )
        if pulled.returncode != 0:
            _log("WARN", f"git pull failed: {_mask((pulled.stderr or '').strip(), token)[-300:]}")
        return BASE_DIR

    if BASE_DIR.exists():
        shutil.rmtree(BASE_DIR)
    _log("STEP", "Cloning repository…")
    result = subprocess.run(
        ["git", "clone", "--depth=1", url, str(BASE_DIR)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        stderr = _mask((result.stderr or "").strip(), token)
        _log("ERR", f"git clone failed: {stderr[-400:]}")
        if not token:
            _log("ERR",
                 "This repository is PRIVATE. Create a GitHub personal access "
                 "token with repo read access and add it to the Colab 🔑 Secrets "
                 "panel as GITHUB_TOKEN, then re-run this cell.")
        else:
            _log("ERR",
                 "A GITHUB_TOKEN was provided but GitHub rejected it — check "
                 "that it has read access to vicMenma/Zilong_multiusage and "
                 "has not expired.")
        raise SystemExit("Repository clone failed — see the messages above.")
    return BASE_DIR


def _install_system_packages() -> None:
    _log("STEP", "Installing system packages…")
    subprocess.run(
        "apt-get update -qq && apt-get install -y -qq ffmpeg aria2 mediainfo p7zip-full unrar 2>/dev/null",
        shell=True,
        check=False,
    )
    _log("OK", "System packages ready")


# Marker lives in /tmp: it survives a "Restart runtime" (same VM) but is wiped
# by "Disconnect and delete runtime" (fresh VM) — exactly when a reinstall is
# genuinely needed.
_PIP_MARKER = Path("/tmp/zilong_pip_ok")


def _requirements_hash(repo: Path) -> str:
    try:
        return hashlib.md5((repo / "requirements.txt").read_bytes()).hexdigest()
    except Exception:
        return ""


def _key_packages_present() -> bool:
    """The heavy non-Colab packages already importable? pyrofork provides the
    `pyrogram` module at our pinned version; seedrcc is the other must-have."""
    try:
        import pyrogram  # noqa: F401  (pyrofork)
        import seedrcc   # noqa: F401
        return getattr(pyrogram, "__version__", "") == "2.3.69"
    except Exception:
        return False


def _install_python_packages(repo: Path) -> None:
    if _secret("ZILONG_SKIP_PIP", "0") == "1":
        _log("INFO", "Skipping pip install because ZILONG_SKIP_PIP=1")
        return

    want = _requirements_hash(repo)
    # Fast path: same VM, requirements unchanged, packages still present →
    # skip the whole ~30s reinstall.
    if (
        want
        and _PIP_MARKER.exists()
        and _PIP_MARKER.read_text(encoding="utf-8").strip() == want
        and _key_packages_present()
    ):
        _log("OK", "Requirements unchanged and packages present — skipping install")
        return

    _log("STEP", "Installing Python packages…")
    subprocess.run([sys.executable, "-m", "pip", "uninstall", "-q", "-y", "pyrogram"], check=False)

    req = str(repo / "requirements.txt")
    installed = False
    # uv is a drop-in, much faster installer (parallel downloads + fast
    # resolver). Try it; fall back to plain pip if unavailable.
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "uv"], check=False)
        subprocess.check_call(
            [sys.executable, "-m", "uv", "pip", "install", "--system", "-r", req]
        )
        installed = True
        _log("OK", "Python packages installed (uv)")
    except Exception as exc:
        _log("WARN", f"uv install failed ({exc}); falling back to pip")

    if not installed:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "-r", req])
        _log("OK", "Python packages installed")

    if want:
        try:
            _PIP_MARKER.write_text(want, encoding="utf-8")
        except Exception:
            pass


def _start_aria2() -> None:
    _log("STEP", "Starting aria2c daemon…")
    subprocess.Popen(
        "aria2c --enable-rpc --rpc-listen-all=true --rpc-allow-origin-all "
        "--max-connection-per-server=16 --split=16 --seed-time=0 --daemon 2>/dev/null",
        shell=True,
    )
    time.sleep(2)
    _log("OK", "aria2c started")


def _clean_pid() -> None:
    if not PID_FILE.exists():
        return
    try:
        old_pid = int(PID_FILE.read_text(encoding="utf-8").strip())
        os.kill(old_pid, signal.SIGTERM)
        time.sleep(2)
        PID_FILE.unlink(missing_ok=True)
        _log("OK", f"Stopped previous bot process PID {old_pid}")
    except Exception:
        PID_FILE.unlink(missing_ok=True)
        _log("OK", "Removed stale PID file")


def _collect_env() -> dict[str, str]:
    api_id = str(_secret_int("API_ID", API_ID, minimum=0))
    owner_id = str(_secret_int("OWNER_ID", OWNER_ID, minimum=0))
    bot_data_dir = _secret("BOT_DATA_DIR", DEFAULT_BOT_DATA_DIR)
    download_dir = _secret("DOWNLOAD_DIR", DEFAULT_DOWNLOAD_DIR)
    Path(bot_data_dir).mkdir(parents=True, exist_ok=True)
    Path(download_dir).mkdir(parents=True, exist_ok=True)

    env = {
        "API_ID": api_id,
        "API_HASH": _secret("API_HASH", API_HASH),
        "BOT_TOKEN": _secret("BOT_TOKEN", BOT_TOKEN),
        "OWNER_ID": owner_id,
        "ADMINS": _secret("ADMINS"),
        "FILE_LIMIT_MB": str(_secret_int("FILE_LIMIT_MB", FILE_LIMIT_MB, minimum=1)),
        "LOG_CHANNEL": str(_secret_int("LOG_CHANNEL", LOG_CHANNEL, minimum=-10**18, maximum=10**18)),
        "CC_API_KEY": _secret("CC_API_KEY", CC_API_KEY),
        "CC_ENGINE_MODE": _secret("CC_ENGINE_MODE", CC_ENGINE_MODE),
        "FC_API_KEY": _secret("FC_API_KEY", FC_API_KEY),
        "FC_WEBHOOK_SECRET": _secret("FC_WEBHOOK_SECRET", FC_WEBHOOK_SECRET),
        "CC_WEBHOOK_SECRET": _secret("CC_WEBHOOK_SECRET", CC_WEBHOOK_SECRET),
        "WEBHOOK_BASE_URL": _secret("WEBHOOK_BASE_URL", WEBHOOK_BASE_URL),
        "NGROK_TOKEN": _secret("NGROK_TOKEN", NGROK_TOKEN) or _secret("NGROK_AUTHTOKEN"),
        "SEEDR_USERNAME": _secret("SEEDR_USERNAME", SEEDR_USERNAME),
        "SEEDR_PASSWORD": _secret("SEEDR_PASSWORD", SEEDR_PASSWORD),
        "SEEDR_PROXY": _secret("SEEDR_PROXY", SEEDR_PROXY),
        # Optional: parallel Telegram chunk transmissions (default 6 in the
        # bot). Higher can upload faster; too high risks network errors.
        "TG_CONCURRENT_TRANSMISSIONS": _secret("TG_CONCURRENT_TRANSMISSIONS"),
        # Optional upload-boost tuning (see plugins/zz_fast_upload.py):
        # workers per file (default 12), media sessions (default 3),
        # TG_UPLOAD_BOOST=0 disables the patch entirely.
        "TG_UPLOAD_WORKERS": _secret("TG_UPLOAD_WORKERS"),
        "TG_UPLOAD_SESSIONS": _secret("TG_UPLOAD_SESSIONS"),
        "TG_UPLOAD_BOOST": _secret("TG_UPLOAD_BOOST"),
        "BOT_DATA_DIR": bot_data_dir,
        "FC_CACHE_DIR": bot_data_dir,
        "FC_EXHAUSTION_TTL": _secret("FC_EXHAUSTION_TTL", "86400"),
        "DOWNLOAD_DIR": download_dir,
        "ARIA2_HOST": _secret("ARIA2_HOST", "http://localhost"),
        "ARIA2_PORT": _secret("ARIA2_PORT", "6800"),
        "ARIA2_SECRET": _secret("ARIA2_SECRET"),
        "ZILONG_RUNTIME": "colab",
        "PYTHONUNBUFFERED": "1",
        "OWNER_RESERVED_SLOTS": str(_secret_int("OWNER_RESERVED_SLOTS", OWNER_RESERVED_SLOTS, minimum=0, maximum=10)),
        "GUEST_MAX_CONCURRENT": str(_secret_int("GUEST_MAX_CONCURRENT", GUEST_MAX_CONCURRENT, minimum=1, maximum=20)),
        "USER_MAX_CONCURRENT": str(_secret_int("USER_MAX_CONCURRENT", USER_MAX_CONCURRENT, minimum=1, maximum=20)),
        "OWNER_MAX_CONCURRENT": str(_secret_int("OWNER_MAX_CONCURRENT", OWNER_MAX_CONCURRENT, minimum=1, maximum=20)),
    }
    for i in range(2, 10):
        val = _secret(f"FC_API_KEY_{i}")
        if val:
            env[f"FC_API_KEY_{i}"] = val
    for opt in ("GDRIVE_SA_JSON",):
        val = _secret(opt)
        if val:
            env[opt] = val
    return env


def _validate(env: dict[str, str]) -> None:
    missing = [k for k in ("API_ID", "API_HASH", "BOT_TOKEN", "OWNER_ID") if not env.get(k) or env.get(k) == "0"]
    if missing:
        for m in missing:
            print(f"  ❌ {m} is required")
        raise SystemExit("Missing required credentials. Add them to Colab Secrets or the form.")


def _write_env(repo: Path, env: dict[str, str]) -> None:
    lines = ["# Auto-generated by colab_launcher.py"]
    for k, v in env.items():
        if v is None or v == "":
            continue
        # Newlines must be escaped so multiline secrets survive the .env
        # round-trip;
        # python-dotenv unescapes \n inside double-quoted values.
        escaped = (
            str(v)
            .replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\r", "")
            .replace("\n", "\\n")
        )
        lines.append(f'{k}="{escaped}"')
    (repo / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    _log("OK", ".env written")


def _clear_sessions(repo: Path) -> None:
    for sf in glob.glob(str(repo / "*.session*")):
        try:
            os.remove(sf)
        except OSError:
            pass


def _heartbeat() -> None:
    """Print a periodic 💓 so the Colab log visibly shows the bot is alive.

    The old browser-side JS keep-alive (auto-clicking Colab's connect button)
    was removed along with the health server / Serveo tunnel / UptimeRobot
    setup: it never reliably beat Colab's idle limits and cost a 20s
    event-loop freeze plus a dropped Telegram session at every startup.
    """
    threading.Thread(
        target=lambda: [time.sleep(300) or print(f"\r[{datetime.now().strftime('%H:%M')}] 💓", end="", flush=True) for _ in iter(int, 1)],
        daemon=True,
    ).start()


def _print_summary(env: dict[str, str]) -> None:
    _log("OK", f"OWNER_ID={env['OWNER_ID']}")
    _log("OK", "Multi-user protection: "
         f"owner_reserved={env['OWNER_RESERVED_SLOTS']}, guest_max={env['GUEST_MAX_CONCURRENT']}, "
         f"user_max={env['USER_MAX_CONCURRENT']}, owner_max={env['OWNER_MAX_CONCURRENT']}")
    _log("OK", f"BOT_DATA_DIR={env['BOT_DATA_DIR']}")
    if env.get("CC_API_KEY"):
        _log("OK", f"CC_API_KEY: {len(env['CC_API_KEY'].split(','))} key(s)")
    if env.get("FC_API_KEY"):
        _log("OK", "FC_API_KEY: set")
    if env.get("SEEDR_USERNAME") and not env.get("SEEDR_PROXY"):
        _log("WARN", "SEEDR_PROXY is empty. On Colab, Seedr add_torrent may fail from cloud IPs.")


def _run_bot(repo: Path, env: dict[str, str]) -> None:
    bot_env = {**os.environ, **env}
    max_restarts = _secret_int("ZILONG_MAX_RESTARTS", 50, minimum=1, maximum=500)
    restart_count = 0
    flood_re = re.compile(r"(?:FLOOD_WAIT_SECONDS=(\d+)|A wait of (\d+) seconds is required)")

    _log("OK", "Starting bot…\n" + "─" * 50)
    while restart_count < max_restarts:
        start = time.time()
        proc = subprocess.Popen(
            [sys.executable, "-u", "main.py"],
            cwd=str(repo),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=bot_env,
        )
        captured: list[str] = []
        assert proc.stdout is not None
        for line in proc.stdout:
            print(line, end="", flush=True)
            captured.append(line)
        code = proc.wait()
        if code == 0:
            _log("OK", "Bot stopped cleanly")
            return
        if time.time() - start > 300:
            restart_count = 0
        restart_count += 1
        wait = min(5 * restart_count, 30)
        for line in reversed(captured):
            m = flood_re.search(line)
            if m:
                wait = int(m.group(1) or m.group(2)) + 5
                break
        _log("WARN", f"Bot crashed/exited code={code}. Restarting in {wait}s [{restart_count}/{max_restarts}]")
        time.sleep(wait)
    _log("ERR", "Too many restarts. Launcher stopped.")


print("⚡ Zilong Bot — Colab Launcher")
print("─" * 50)
_mount_drive()
repo = _clone_or_update_repo()
_install_system_packages()
_install_python_packages(repo)
_start_aria2()
_clean_pid()
env = _collect_env()
_validate(env)
_write_env(repo, env)
_clear_sessions(repo)
_heartbeat()
_print_summary(env)
_run_bot(repo, env)
