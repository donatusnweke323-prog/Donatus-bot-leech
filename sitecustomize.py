"""
sitecustomize.py

Runtime bootstrap for Colab persistence and FreeConvert quota handling.
Python imports this module automatically at startup when the repository root
is on sys.path, which is the normal `python main.py` / Colab launcher flow.

Goals:
- keep data/*.json outside ephemeral /content by linking data/ to Google Drive;
- preserve FreeConvert exhausted-key and job-store state after a Colab wipe;
- harden FreeConvert quota detection for documented task-level error codes such
  as out_of_conversion_minutes and out_of_conversion_minutes_per_task;
- avoid writing raw API keys into the exhaustion cache by storing fingerprints.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Coroutine

log = logging.getLogger("sitecustomize")


def _repo_root() -> Path:
    return Path(__file__).resolve().parent


def _preferred_data_dir() -> Path | None:
    """Return a persistent data directory when one is available."""
    for env_name in ("BOT_DATA_DIR", "FC_CACHE_DIR"):
        raw = os.environ.get(env_name, "").strip()
        if raw:
            return Path(raw).expanduser()

    # Default Google Drive location for Colab. It exists only after drive.mount().
    drive_root = Path("/content/drive/MyDrive")
    if drive_root.exists():
        return drive_root / "ZilongBot" / "data"

    return None


def _copy_tree_contents(src: Path, dst: Path) -> None:
    if not src.exists() or not src.is_dir():
        return
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dst / item.name
        try:
            if item.is_dir():
                if not target.exists():
                    shutil.copytree(item, target)
            elif item.is_file() and not target.exists():
                shutil.copy2(item, target)
        except Exception as exc:  # never block bot startup for persistence migration
            log.debug("Could not migrate %s -> %s: %s", item, target, exc)


def _ensure_persistent_data_link() -> None:
    """
    On Colab, make repo/data point to Google Drive.

    Existing local data is copied once to Drive and then moved aside. If symlink
    creation fails, we still export BOT_DATA_DIR/FC_CACHE_DIR for newer code.
    """
    persistent = _preferred_data_dir()
    if persistent is None:
        return

    persistent.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("BOT_DATA_DIR", str(persistent))
    os.environ.setdefault("FC_CACHE_DIR", str(persistent))

    root = _repo_root()
    data_path = root / "data"

    try:
        if data_path.is_symlink():
            current = Path(os.readlink(data_path))
            if current == persistent:
                return
            data_path.unlink()

        if data_path.exists() and not data_path.is_symlink():
            _copy_tree_contents(data_path, persistent)
            backup = root / f"data.local_backup_{int(time.time())}"
            try:
                data_path.rename(backup)
                log.info("Moved local data directory to %s", backup)
            except Exception:
                # If renaming fails, do not delete user data.
                return

        if not data_path.exists():
            os.symlink(str(persistent), str(data_path), target_is_directory=True)
            log.info("Linked %s -> %s", data_path, persistent)
    except Exception as exc:
        log.debug("Persistent data link setup skipped: %s", exc)


_FC_QUOTA_ERROR_CODES = {
    "out_of_conversion_minutes",
    "out_of_conversion_minutes_per_task",
}

_FC_QUOTA_TEXT_MARKERS = (
    "insufficient cpu",
    "cpu minute",
    "cpu minutes",
    "out of conversion minutes",
    "out_of_conversion_minutes",
    "out_of_conversion_minutes_per_task",
    "not enough minutes",
    "quota",
    "credit",
    "credits",
    "402",
)


def _fc_key_id(api_key: str) -> str:
    return hashlib.sha256((api_key or "").strip().encode("utf-8")).hexdigest()[:24]


def _looks_like_key_fingerprint(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{24}", str(value or "")))


def _walk_fc_error_codes(obj: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(obj, dict):
        for key in ("errorCode", "code"):
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                found.add(value.strip())
        for value in obj.values():
            found.update(_walk_fc_error_codes(value))
    elif isinstance(obj, list):
        for item in obj:
            found.update(_walk_fc_error_codes(item))
    return found


def _is_fc_quota_payload(obj: Any) -> bool:
    codes = _walk_fc_error_codes(obj)
    if codes & _FC_QUOTA_ERROR_CODES:
        return True
    try:
        text = json.dumps(obj, default=str).lower()
    except Exception:
        text = str(obj or "").lower()
    return any(marker in text for marker in _FC_QUOTA_TEXT_MARKERS)


def _patch_freeconvert_api() -> None:
    """Patch FreeConvert helpers without requiring an intrusive source rewrite."""
    try:
        import services.freeconvert_api as fc
    except Exception as exc:
        # Dependencies may not be installed during some setup/compile commands.
        log.debug("FreeConvert patch deferred/skipped: %s", exc)
        return

    if getattr(fc, "_ZILONG_COLAB_PERSISTENCE_PATCHED", False):
        return

    # Longer default for daily FreeConvert reset cycles; override from env when needed.
    try:
        fc._FC_EXHAUSTION_TTL = float(os.environ.get("FC_EXHAUSTION_TTL", "86400"))
    except Exception:
        fc._FC_EXHAUSTION_TTL = 86400.0

    # Migrate raw-key cache entries to fingerprints before saving to Drive.
    try:
        migrated: dict[str, float] = {}
        now = time.time()
        for key, ts in dict(getattr(fc, "_fc_exhausted", {})).items():
            if not isinstance(ts, (int, float)):
                continue
            if now - float(ts) >= float(fc._FC_EXHAUSTION_TTL):
                continue
            cache_key = key if _looks_like_key_fingerprint(key) else _fc_key_id(str(key))
            migrated[cache_key] = float(ts)
        fc._fc_exhausted = migrated
        fc._save_exhaustion_cache(fc._fc_exhausted)
    except Exception as exc:
        log.debug("FC cache migration skipped: %s", exc)

    original_is_fc_quota_error = fc.is_fc_quota_error
    original_fc_get_usage = fc._fc_get_usage

    def patched_is_fc_quota_error(exc: object) -> bool:
        return bool(original_is_fc_quota_error(exc) or _is_fc_quota_payload(exc))

    async def patched_fc_get_usage(api_key: str) -> float:
        cache_key = _fc_key_id(api_key)
        exhausted_at = getattr(fc, "_fc_exhausted", {}).get(cache_key)
        if exhausted_at is not None:
            age = time.time() - float(exhausted_at)
            if age < float(fc._FC_EXHAUSTION_TTL):
                fc.log.info(
                    "[FC-API] Key ...%s cached as exhausted via persistent fingerprint "
                    "(%.0f min ago) — skipping usage check",
                    api_key[-6:], age / 60,
                )
                return 0.0
            try:
                del fc._fc_exhausted[cache_key]
                fc._save_exhaustion_cache(fc._fc_exhausted)
            except Exception:
                pass

        value = await original_fc_get_usage(api_key)
        try:
            if float(value) == 0.0:
                fc._fc_exhausted[cache_key] = time.time()
                fc._save_exhaustion_cache(fc._fc_exhausted)
        except Exception:
            pass
        return float(value)

    async def patched_wait_for_job(
        api_key: str,
        job_id: str,
        timeout_s: int = 7200,
        poll_interval: float = 5.0,
        progress_cb=None,
    ) -> dict:
        deadline = time.time() + timeout_s
        poll_errors = 0

        while time.time() < deadline:
            try:
                job = await fc._fc_get_job(api_key, job_id)
                poll_errors = 0
            except Exception as exc:
                poll_errors += 1
                fc.log.warning(
                    "[FC-API] Poll error for job %s (attempt %d): %s — retrying",
                    job_id, poll_errors, exc,
                )
                if poll_errors >= 5:
                    raise RuntimeError(
                        f"[FC] Job {job_id}: 5 consecutive poll failures — last: {exc}"
                    ) from exc
                await asyncio.sleep(min(poll_interval * poll_errors, 30.0))
                continue

            status = (job.get("status") or "").lower()

            if status == "completed":
                fc.log.info("[FC-API] Job %s completed", job_id)
                if progress_cb:
                    try:
                        await progress_cb(100.0, "✅ Complete")
                    except Exception:
                        pass
                return job

            if status in ("failed", "cancelled", "error"):
                err_msg = fc.describe_fc_failure(job, status)
                if _is_fc_quota_payload(job):
                    # Persist the key fingerprint immediately so future Colab restarts skip it.
                    try:
                        fc._fc_exhausted[_fc_key_id(api_key)] = time.time()
                        fc._save_exhaustion_cache(fc._fc_exhausted)
                    except Exception:
                        pass
                    raise fc.FcQuotaError(
                        f"[FC] Job {job_id} quota exhausted: {err_msg or 'Unknown quota error'}"
                    )
                raise RuntimeError(f"[FC] Job {job_id} {status}: {err_msg or 'Unknown error'}")

            if progress_cb:
                tasks = job.get("tasks") or []
                try:
                    pct, detail = fc._task_progress(tasks)
                    await progress_cb(pct, detail)
                except Exception:
                    pass

            await asyncio.sleep(poll_interval)

        raise RuntimeError(f"[FC] Job {job_id} timed out after {timeout_s}s")

    async def patched_submit_with_fc_key_fallback(
        keys: list[str],
        submit_cb: Callable[[str], Coroutine[object, object, str]],
        *,
        action: str = "job",
    ) -> tuple[str, str, float]:
        ranked = await fc._rank_fc_keys(keys)
        errors: list[str] = []

        for pos, (key, minutes) in enumerate(ranked, start=1):
            cache_key = _fc_key_id(key)
            try:
                fc.log.info(
                    "[FC-API] Trying key %d/%d (...%s) for %s (~%s available)",
                    pos, len(ranked), key[-6:], action,
                    "many" if minutes >= 1e5 else f"{minutes:.1f}",
                )
                job_id = await submit_cb(key)
                fc.log.info("[FC-API] %s accepted by key ...%s", action, key[-6:])
                removed = False
                for stored_key in (cache_key, key):
                    if stored_key in fc._fc_exhausted:
                        del fc._fc_exhausted[stored_key]
                        removed = True
                if removed:
                    fc._save_exhaustion_cache(fc._fc_exhausted)
                return key, str(job_id), minutes

            except fc.FcQuotaError as exc:
                err = str(exc).replace("\n", " ")[:180]
                fc.log.warning("[FC-API] Key ...%s quota exhausted for %s: %s", key[-6:], action, err)
                errors.append(f"...{key[-6:]}: {err}")
                fc._fc_exhausted[cache_key] = time.time()
                fc._save_exhaustion_cache(fc._fc_exhausted)
                continue

            except Exception as exc:
                if not patched_is_fc_quota_error(exc):
                    raise
                err = str(exc).replace("\n", " ")[:180]
                fc.log.warning("[FC-API] Key ...%s skipped for %s (quota detected): %s", key[-6:], action, err)
                errors.append(f"...{key[-6:]}: {err}")
                fc._fc_exhausted[cache_key] = time.time()
                fc._save_exhaustion_cache(fc._fc_exhausted)
                continue

        joined = " | ".join(errors) if errors else "no usable key"
        raise RuntimeError(
            f"[FC] All {len(ranked)} FreeConvert API key(s) are out of CPU minutes "
            f"or credits for {action}: {joined}"
        )

    fc.is_fc_quota_error = patched_is_fc_quota_error
    fc._fc_get_usage = patched_fc_get_usage
    fc.wait_for_job = patched_wait_for_job
    fc.submit_with_fc_key_fallback = patched_submit_with_fc_key_fallback
    fc._ZILONG_COLAB_PERSISTENCE_PATCHED = True
    fc.log.info("[FC-API] Colab persistence/quota patch active; data dir=%s", _preferred_data_dir())


_ensure_persistent_data_link()
_patch_freeconvert_api()
