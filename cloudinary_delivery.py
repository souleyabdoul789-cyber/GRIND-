"""Helpers de livraison sécurisée de fichiers Cloudinary pour GRIND."""
from __future__ import annotations

import logging
import time
from urllib.parse import unquote, urlparse

from media import CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET

logger = logging.getLogger("grind.cloudinary.delivery")


def url_privee_telechargement_raw(source: str, extensions_autorisees: set[str]) -> str | None:
    """Crée une URL Cloudinary de téléchargement à durée courte pour un fichier raw.

    Appeler uniquement après authentification GRIND et contrôle d'accès à la ressource.
    L'URL signée ne doit jamais être renvoyée au client ni journalisée.
    """
    parsed = urlparse(source or "")
    if parsed.scheme != "https" or parsed.hostname != "res.cloudinary.com":
        return None
    if not all((CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET)):
        return None

    parts = [unquote(part) for part in parsed.path.strip("/").split("/")]
    if len(parts) < 5 or parts[0] != CLOUDINARY_CLOUD_NAME or parts[1:3] != ["raw", "upload"]:
        return None
    version_index = next(
        (index for index, part in enumerate(parts[3:], start=3)
         if part.startswith("v") and part[1:].isdigit()),
        None,
    )
    if version_index is None or version_index >= len(parts) - 1:
        return None

    public_id = "/".join(parts[version_index + 1:])
    extension = public_id.rsplit(".", 1)[-1].lower() if "." in public_id else ""
    if extension not in extensions_autorisees:
        return None

    try:
        from cloudinary.utils import private_download_url
        return private_download_url(
            public_id,
            extension,
            resource_type="raw",
            type="upload",
            expires_at=int(time.time()) + 180,
            attachment=True,
            cloud_name=CLOUDINARY_CLOUD_NAME,
            api_key=CLOUDINARY_API_KEY,
            api_secret=CLOUDINARY_API_SECRET,
        )
    except Exception as erreur:
        logger.warning("Signature Cloudinary indisponible: %s", type(erreur).__name__)
        return None
