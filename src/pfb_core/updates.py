"""Controllo degli aggiornamenti: confronta la versione dell'app con l'ultima release GitHub.

Una sola chiamata di rete, all'API pubblica delle release, senza inviare dati personali.
Solo libreria standard (serve anche nell'APK). Il download resta all'utente: l'app apre il
link dell'APK e Android lo installa. Il fetch è iniettabile, così i test non usano la rete.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

REPO = "Kelbec/personal-finance-bro"
API_URL = "https://api.github.com/repos/{repo}/releases/latest"
# gli APK sono uno per architettura (split_per_abi): quello dei telefoni recenti
PREFERRED_ABI = "arm64-v8a"


class UpdateError(RuntimeError):
    pass


@dataclass
class UpdateInfo:
    version: str  # "0.3.0"
    tag: str  # "v0.3.0"
    apk_url: str | None  # link diretto all'APK; None se la release non ne ha
    page_url: str  # pagina della release, se l'APK non c'è


def parse_version(text: str) -> tuple[int, ...]:
    """'v0.3.0' → (0, 3, 0). Ignora il prefisso 'v' e i suffissi (es. '-rc1')."""
    m = re.match(r"v?(\d+(?:\.\d+)*)", text.strip())
    if not m:
        raise UpdateError(f"versione non riconosciuta: {text!r}")
    return tuple(int(p) for p in m.group(1).split("."))


def pick_apk(assets: list[dict]) -> str | None:
    """Link dell'APK per arm64-v8a, altrimenti il primo APK della release."""
    apks = [a for a in assets if str(a.get("name", "")).endswith(".apk")]
    for a in apks:
        if PREFERRED_ABI in a["name"]:
            return a["browser_download_url"]
    return apks[0]["browser_download_url"] if apks else None


def github_fetch(url: str) -> dict:
    req = urllib.request.Request(
        url, headers={"User-Agent": "personal-finance-bro", "Accept": "application/vnd.github+json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateError("nessuna release trovata (repo privato o senza release)") from exc
        raise UpdateError(f"GitHub ha risposto {exc.code}") from exc
    except OSError as exc:  # rete assente, timeout, DNS
        raise UpdateError(f"rete non raggiungibile ({exc})") from exc


def check_update(
    current: str, repo: str = REPO, fetch: Callable[[str], dict] = github_fetch
) -> UpdateInfo | None:
    """Le informazioni sulla release più recente se è più nuova di `current`, altrimenti None."""
    data = fetch(API_URL.format(repo=repo))
    tag = data.get("tag_name") or ""
    if parse_version(tag) <= parse_version(current):
        return None
    return UpdateInfo(
        version=".".join(map(str, parse_version(tag))),
        tag=tag,
        apk_url=pick_apk(data.get("assets", [])),
        page_url=data.get("html_url", f"https://github.com/{repo}/releases"),
    )
