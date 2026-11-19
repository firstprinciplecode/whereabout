"""Drop-in provider protocol (~/.config/whereabout/providers.d/*)."""

from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .cache import config_dir
from .protocol import SCHEMA_VERSION

PROVIDER_TIMEOUT = 0.4


def providers_dir() -> str:
    return os.environ.get("WHEREABOUT_PROVIDERS_DIR") or os.path.join(config_dir(), "providers.d")


def _run_provider(path: str, env: dict[str, str], timeout: float) -> tuple[str, dict[str, Any] | None]:
    name = os.path.basename(path)
    if "." in name:
        name = name.rsplit(".", 1)[0]
    try:
        proc = subprocess.run(
            [path],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return name, None
        parsed = json.loads(proc.stdout)
        if isinstance(parsed, dict):
            return name, parsed
    except Exception:
        pass
    return name, None


def run_providers(
    context_env: dict[str, str],
    timeout: float = PROVIDER_TIMEOUT,
) -> dict[str, dict[str, Any]]:
    """Run executable drop-in providers in parallel and collect JSON dicts."""
    pdir = providers_dir()
    if not os.path.isdir(pdir):
        return {}
    executables: list[str] = []
    try:
        for entry in sorted(os.listdir(pdir)):
            if entry.startswith("."):
                continue
            full = os.path.join(pdir, entry)
            if os.path.isfile(full) and os.access(full, os.X_OK):
                executables.append(full)
    except Exception:
        return {}
    if not executables:
        return {}

    env = dict(os.environ)
    env["WHEREABOUT_SCHEMA"] = SCHEMA_VERSION
    for k, v in context_env.items():
        if v:
            env[k] = str(v)

    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(4, len(executables))) as pool:
        futs = [pool.submit(_run_provider, p, env, timeout) for p in executables]
        for fut in as_completed(futs, timeout=timeout + 0.15):
            try:
                name, data = fut.result()
                if data:
                    results[name] = data
            except Exception:
                pass
    return results
