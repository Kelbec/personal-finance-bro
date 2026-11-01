"""Import di config.toml e della chiave Google dal PC al telefono.

Sul telefono la cartella dell'app è privata: la schermata Dati copia qui dentro i due file
che sul PC stanno nella radice del progetto. Entrambi vengono controllati prima di sostituire
quelli esistenti.
"""

from __future__ import annotations

import json
import shutil
import tomllib
from pathlib import Path

CREDENTIALS_NAME = "google-credentials.json"


class InvalidSetupFile(ValueError):
    pass


def import_config(src: str | Path, dest: str | Path) -> Path:
    """Copia un config.toml valido in `dest`; il vecchio diventa `<dest>.bak`."""
    src = Path(src)
    try:
        tomllib.loads(src.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise InvalidSetupFile(f"{src.name}: non è un config.toml valido ({exc})") from exc
    return _install(src, Path(dest))


def import_credentials(src: str | Path, dest_dir: str | Path) -> Path:
    """Copia la chiave dell'account di servizio come google-credentials.json in `dest_dir`."""
    src = Path(src)
    try:
        key = json.loads(src.read_text(encoding="utf-8"))
        ok = (
            key.get("type") == "service_account"
            and key.get("client_email")
            and key.get("private_key")
        )
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError) as exc:
        raise InvalidSetupFile(f"{src.name}: non è un file JSON") from exc
    if not ok:
        raise InvalidSetupFile(f"{src.name}: non è la chiave di un account di servizio Google")
    return _install(src, Path(dest_dir) / CREDENTIALS_NAME)


def _install(src: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.copy2(dest, dest.with_name(dest.name + ".bak"))
    shutil.copyfile(src, dest)
    return dest
