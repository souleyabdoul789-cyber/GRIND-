"""Registre les ressources Cloudinary approuvées par GRIND et leur propriétaire."""
from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse, unquote

from fastapi import HTTPException
from sqlalchemy.orm import Session

from media import CLOUDINARY_CLOUD_NAME
from models import GrindMediaAsset


def extraire_reference_cloudinary(url: str):
    parsed = urlparse(str(url or ""))
    if (parsed.scheme != "https" or parsed.hostname != "res.cloudinary.com"
            or parsed.port not in (None, 443) or not CLOUDINARY_CLOUD_NAME):
        return None
    segments = [unquote(part) for part in parsed.path.strip("/").split("/")]
    if len(segments) < 6 or segments[0] != CLOUDINARY_CLOUD_NAME:
        return None
    resource_type, delivery = segments[1], segments[2]
    if resource_type not in {"image", "video", "raw"} or delivery != "upload":
        return None
    version_index = next((i for i, part in enumerate(segments[3:], start=3)
                          if re.fullmatch(r"v[0-9]+", part)), None)
    if version_index is None or version_index >= len(segments) - 1:
        return None
    public_id = "/".join(segments[version_index + 1:])
    if not public_id or any(part in {".", ".."} or "\\" in part for part in public_id.split("/")):
        return None
    return resource_type, public_id, segments[version_index]


def _cle_asset(resource_type: str, public_id: str) -> str:
    return hashlib.sha256(f"{resource_type}\0{public_id}".encode("utf-8")).hexdigest()


def enregistrer_media_asset(db: Session, *, username: str, url: str, mime_type: str | None, size_bytes: int):
    reference = extraire_reference_cloudinary(url)
    if reference is None:
        raise HTTPException(status_code=502, detail="La référence du fichier Cloudinary n'est pas valide.")
    resource_type, public_id, version = reference
    cle = _cle_asset(resource_type, public_id)
    row = db.query(GrindMediaAsset).filter_by(asset_key=cle).first()
    if row is not None and row.username != username:
        raise HTTPException(status_code=409, detail="Cette ressource Cloudinary appartient déjà à un autre compte GRIND.")
    if row is None:
        row = GrindMediaAsset(
            asset_key=cle, username=username, resource_type=resource_type, public_id=public_id,
            version=version, mime_type=(mime_type or "")[:120] or None, size_bytes=max(0, int(size_bytes or 0)),
        )
        db.add(row)
    else:
        row.version = version
        row.mime_type = (mime_type or "")[:120] or row.mime_type
        row.size_bytes = max(0, int(size_bytes or row.size_bytes or 0))
    return row


def verifier_proprietaire_media(db: Session, *, username: str, url: str):
    reference = extraire_reference_cloudinary(url)
    if reference is None:
        raise HTTPException(status_code=400, detail="La référence Cloudinary n'est pas reconnue.")
    resource_type, public_id, version = reference
    row = db.query(GrindMediaAsset).filter_by(asset_key=_cle_asset(resource_type, public_id)).first()
    if row is None or row.username != username or row.version != version:
        raise HTTPException(status_code=403, detail="Tu ne peux pas joindre un fichier qui n'a pas été confirmé par ton compte GRIND.")
    return row
