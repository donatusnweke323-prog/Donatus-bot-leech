"""
Zilong Bot — main.py
Entry point. Loads config, builds client, registers plugins, starts.

FIXES
─────
MAIN-01: FC_API_KEY checked via os.environ directly (not just cfg) to handle
  any dotenv load-order edge cases.
MAIN-02: data/ directory created at startup before any service writes to it.
MAIN-03: on_tunnel_ready called with explicit cc_path="/webhook/cloudconvert"
  so CC webhooks always register to the correct URL.
MAIN-04: FC import errors logged at WARNING instead of silently swallowed.
MAIN-05: Webhook server started when FC_API_KEY is set, not just CC_API_KEY.
MAIN-06: Seedr monitor started when both SEEDR_USERNAME and CC_API_KEY are set.
MAIN-07: Explicitly imports CC mode/house-style patches before client.start()
  so CloudConvert buttons always show Economy/Balanced/NVIDIA choices.
MAIN-08: Preloads smart-thumbnail and multi-user isolation patches before
  plugin startup so allowed users cannot block the owner workflow.
"""
import asyncio
import logging
import os
import glob
import sys

# ── Single-instance guard ──────────────────────────────────────────────────
_PID_FILE = "/tmp/zilong_bot.pid"

def _acquire_pid_lock() -> None:
    if os.path.exists(_PID_FILE):
        try:
            old_pid = int(open(_PID_FILE).read().strip())
            os.kill(old_pid, 0)
            print(f"❌ Another instance running (PID {old_pid}).\n   Kill: kill {old_pid}", file=sys.stderr)
            sys.exit(1)
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    with open(_PID_FILE, "w") as _pf:
        _pf.write(str(os.getpid()))

def _release_pid_lock() -> None:
    try:
        os.remove(_PID_FILE)
    except OSError:
        pass

_acquire_pid_lock()
import atexit
atexit.register(_release_pid_lock)

try:
    import uvloop
    uvloop.install()
    _UVLOOP = True
except ImportError:
    _UVLOOP = False

_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("zilong.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

if _UVLOOP:
    log.info("⚡ uvloop active")
else:
    log.warning("⚠️  uvloop not installed")

# MAIN-02: always ensure data/ exists before any service that writes to it
_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(_DATA_DIR, exist_ok=True)

for _f in (
    glob.glob("*.session") + glob.glob("*.session-journal") +
    glob.glob(os.path.join(_DATA_DIR, "*.session-journal"))
):
    try:
        os.remove(_f)
        log.info("Removed stale session artifact: %s", _f)
    except OSError:
        pass

# MAIN-07/08: load patches before Client.start() triggers plugin loading.
# This guarantees early callback interception, CC house-style rewriting, and
# multi-user resource isolation. (Smart thumbnails are now built directly into
# services/uploader.py — no runtime patch needed.)
try:
    import plugins.cc_engine_modes  # noqa: F401
    import plugins.cc_house_style_patch  # noqa: F401
    import plugins.multiuser_isolation  # noqa: F401
    log.info("☁️  Startup patches preloaded: CC modes, house style, isolation")
except Exception as exc:
    log.warning("Startup patch preloader failed: %s", exc)

from pyrogram import Client, idle, filters, handlers, enums
from core.config import cfg
from core.bot_name import get_bot_name, set_bot_name, is_name_configured
from services.task_runner import runner, MAX_CONCURRENT
from services.tg_transport import (
    enable_alt_port_globally,
    is_alt_port_disabled,
    should_use_alt_port,
)


def build_client() -> Client:
    _session_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(_session_dir, exist_ok=True)
    kwargs: dict = dict(
        name="ZilongBot",
        api_id=cfg.api_id,
        api_hash=cfg.api_hash,
        bot_token=cfg.bot_token,
        plugins={"root": "plugins"},
        workdir=_session_dir,
    )
    # Parallel MTProto chunk transmissions. Sequential 512 KB parts cap
    # uploads at roughly part_size/RTT (~7 MiB/s from Colab to Telegram's
    # DC), which made upload the slowest stage of every job. pyrofork's
    # max_concurrent_transmissions pipelines several chunks at once and
    # lifts that ceiling several-fold. Guarded by a signature check so a
    # fork without the parameter still starts.
    try:
        import inspect as _inspect
        if "max_concurrent_transmissions" in _inspect.signature(Client.__init__).parameters:
            raw = os.environ.get("TG_CONCURRENT_TRANSMISSIONS", "6").strip()
            kwargs["max_concurrent_transmissions"] = max(1, min(16, int(raw or 6)))
            log.info("⚡ Parallel Telegram transmissions: %d",
                     kwargs["max_concurrent_transmissions"])
        if should_use_alt_port():
            _has_alt_param = "alt_port" in _inspect.signature(Client.__init__).parameters
            if enable_alt_port_globally() or _has_alt_param:
                if _has_alt_param:
                    kwargs["alt_port"] = True
                log.info("🔌 Using Telegram alternative port 5222 (alt_port=True)")
    except Exception as exc:
        log.warning("Parallel transmissions unavailable: %s", exc)
    return Client(**kwargs)


async def _ask_bot_name(client) -> None:
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    async def _on_name(_, msg):
        name = msg.text.strip()
        if name and not name.startswith("/") and not fut.done():
            fut.set_result(name)
            try:
                await msg.delete()
            except Exception:
                pass

    handler = handlers.MessageHandler(
        _on_name,
        filters.user(cfg.owner_id) & filters.text & filters.private,
    )
    client.add_handler(handler, group=-99)

    try:
        prompt_msg = await client.send_message(
            cfg.owner_id,
            "👋 <b>Welcome! First-time setup</b>\n\nWhat do you want to call this bot?\n"
            "Send just the name — e.g. <code>Kitagawa</code>\n\n<i>Change later with /botname.</i>",
            parse_mode=enums.ParseMode.HTML,
        )
        name = await asyncio.wait_for(fut, timeout=300)
    except asyncio.TimeoutError:
        log.warning("Bot-name setup timed out — using default Zilong")
        name = "Zilong"
    finally:
        client.remove_handler(handler, group=-99)

    set_bot_name(name)
    try:
        await prompt_msg.delete()
    except Exception:
        pass

    confirm = await client.send_message(
        cfg.owner_id,
        f"✅ <b>Name saved: {name.upper()} MULTIUSAGE BOT</b>\n🟢 <i>Starting…</i>",
        parse_mode=enums.ParseMode.HTML,
    )

    async def _del():
        await asyncio.sleep(15)
        try:
            await confirm.delete()
        except Exception:
            pass
    asyncio.create_task(_del())
    log.info("Bot name configured: %s", name)


async def main() -> None:
    if os.environ.get("KOYEB", "").strip() == "1":
        from koyeb_server import start_health_server
        port = int(os.environ.get("PORT", 8000))
        start_health_server(port)
        log.info("🌐 Koyeb health server on port %d", port)

    client = build_client()
    import core.session as _cs
    _cs._client = client

    from pyrogram.errors import FloodWait as _FloodWait
    _alt_fallback_tried = False
    for _attempt in range(1, 6):
        try:
            await client.start()
            break
        except _FloodWait as _fw:
            log.warning("🚦 FloodWait %ds on auth (attempt %d/5)", _fw.value, _attempt)
            print(f"FLOOD_WAIT_SECONDS={_fw.value}", flush=True)
            if _attempt >= 5:
                raise
            await asyncio.sleep(_fw.value + 5)
        except (KeyError, ConnectionError) as _conn_exc:
            if _alt_fallback_tried or is_alt_port_disabled():
                raise
            _alt_fallback_tried = True
            log.warning(
                "⚠️  Telegram port 443 handshake failed (%r); retrying on port 5222",
                _conn_exc,
            )
            enable_alt_port_globally()
            if hasattr(client, "alt_port"):
                client.alt_port = True
            await asyncio.sleep(1)

    me = await client.get_me()
    log.info("✅ @%s (id=%d) started", me.username or me.first_name, me.id)

    runner.start()
    log.info("🚀 Task runner started (max %d concurrent)", MAX_CONCURRENT)

    # MAIN-01: check env directly too, not just cfg (handles dotenv timing)
    cc_api_key = cfg.cc_api_key or os.environ.get("CC_API_KEY", "").strip()
    fc_api_key = cfg.fc_api_key or os.environ.get("FC_API_KEY", "").strip()
    has_webhook_config = bool(
        os.environ.get("WEBHOOK_BASE_URL", "").strip() or cfg.ngrok_token
    )

    # MAIN-05: start webhook server for EITHER CC or FC key
    webhook_url = ""
    if cc_api_key or fc_api_key or has_webhook_config:
        import services.cloudconvert_hook as cc_hook
        if cfg.cc_webhook_secret:
            cc_hook.WEBHOOK_SECRET = cfg.cc_webhook_secret

        webhook_url = await cc_hook.start_webhook_server(port=8765)
        if webhook_url:
            log.info("☁️  Webhook server: %s", webhook_url)
        else:
            log.info("☁️  Webhook server on port 8765 (no public URL)")

        # MAIN-03: explicit cc_path to avoid any default value confusion.
        try:
            from services.webhook_sync import on_tunnel_ready, poll_pending_jobs
            from core.config import get_tunnel_url
            _turl = get_tunnel_url()
            if _turl:
                await on_tunnel_ready(_turl, cc_path="/webhook/cloudconvert")
                log.info("🔄 Webhook sync complete → %s", _turl)
            else:
                log.warning("⚠️  No tunnel URL — webhook sync skipped")
            await poll_pending_jobs()
            log.info("🔄 Pending jobs recovery complete")
        except Exception as exc:
            log.warning("webhook_sync failed: %s", exc)
    else:
        log.info("ℹ️  No API keys — webhook server not started")

    # FC job store startup
    if fc_api_key:
        try:
            import plugins.fc_webhook as fc_webhook
            await fc_webhook.startup_load()
            try:
                import plugins.fc_seedr  # noqa: F401
                log.info("🆓 FreeConvert Seedr pipeline loaded")
            except Exception as exc:
                log.warning("fc_seedr import: %s", exc)
            log.info("🆓 FreeConvert job store loaded")
        except ImportError as exc:
            log.warning(
                "⚠️  plugins/fc_webhook.py missing — FreeConvert disabled.\n"
                "    Ensure fc_webhook.py and fc_job_store.py are in your repo.\n"
                "    Error: %s", exc,
            )
        except Exception as exc:
            log.warning("FC webhook startup failed: %s", exc)
    else:
        log.info("ℹ️  No FC_API_KEY — FreeConvert disabled")

    # CC status poller
    if cc_api_key:
        try:
            from plugins.ccstatus import _ensure_poller
            _ensure_poller()
            log.info("📡 ccstatus auto-poller started")
        except Exception as exc:
            log.warning("ccstatus poller failed: %s", exc)

    # ── MAIN-06: Seedr monitor — auto-hardsub for new Seedr folders ───────────
    _seedr_user = os.environ.get("SEEDR_USERNAME", "").strip()
    if _seedr_user and cc_api_key:
        try:
            from services.seedr_monitor import start_monitor as _sm_start
            _sm_start(cfg.owner_id)
            log.info("🌱 Seedr monitor started (polling every 60 s for new folders)")
        except Exception as exc:
            log.warning("Seedr monitor failed to start: %s", exc)
    else:
        if not _seedr_user:
            log.info("ℹ️  Seedr monitor disabled (SEEDR_USERNAME not set)")
        else:
            log.info("ℹ️  Seedr monitor disabled (CC_API_KEY not set)")

    if not is_name_configured():
        await _ask_bot_name(client)

    bot_name = get_bot_name()
    log.info("🤖 Bot name: %s", bot_name.upper())

    # Nyaa tracker
    try:
        from plugins.nyaa_tracker import start_nyaa_poller
        start_nyaa_poller()
    except Exception as exc:
        log.warning("Nyaa poller failed: %s", exc)

    # Keep-alive (health server + Serveo tunnel + UptimeRobot) removed: it
    # froze the event loop for 20s at startup, killed the Telegram session,
    # and never reliably kept Colab awake anyway.

    log.info("📡 Bot is running. Ctrl+C to stop.")
    await idle()

    log.info("👋 Shutting down…")
    if cc_api_key or fc_api_key or has_webhook_config:
        try:
            from services.cloudconvert_hook import stop_webhook_server
            await stop_webhook_server()
        except Exception:
            pass
    # ── Stop Seedr monitor ────────────────────────────────────────────────────
    try:
        from services.seedr_monitor import stop_monitor as _sm_stop
        _sm_stop()
    except Exception:
        pass
    runner.stop()
    await client.stop()
    log.info("✅ Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
