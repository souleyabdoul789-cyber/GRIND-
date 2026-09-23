"""
media.py — upload direct vers Cloudinary (photos/vidéos des posts).

Principe : le client demande une signature ici (avec sa session GRIND
valide), on la génère avec le secret Cloudinary — qui ne quitte JAMAIS
le serveur — puis le client upload le fichier lui-même, en direct,
vers l'API Cloudinary. Notre serveur ne voit jamais le fichier lui-même,
donc pas de souci de taille/bande passante côté Render.

Cloudinary gère automatiquement la compression et le redimensionnement
via les "transformations" passées au moment de l'upload — pas besoin
de traiter la vidéo nous-mêmes.
"""

import os
import time
import hashlib
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session

from database import get_db
from auth_grind import verifier_session
from schemas import SessionRequest

router = APIRouter()

CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME")
CLOUDINARY_API_KEY = os.getenv("CLOUDINARY_API_KEY")
CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET")

# Limites appliquées à l'upload — Cloudinary les respecte côté serveur,
# donc même si quelqu'un bidouille le client, la limite tient.
DUREE_MAX_VIDEO_SECONDES = 120
POIDS_MAX_MO = 25


def _signer(params: dict) -> str:
    """Signature Cloudinary : tri alphabétique des params + secret, SHA-1."""
    chaine = "&".join(f"{k}={v}" for k, v in sorted(params.items())) + CLOUDINARY_API_SECRET
    return hashlib.sha1(chaine.encode()).hexdigest()


@router.post("/api/media/signature-upload")
async def signature_upload(data: SessionRequest, db: Session = Depends(get_db)):
    username = verifier_session(data.session_token, db)
    if username is None:
        raise HTTPException(status_code=401, detail="Session GRIND invalide ou expirée")

    if not all([CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET]):
        raise HTTPException(status_code=500, detail="Cloudinary non configuré côté serveur")

    timestamp = int(time.time())
    params_a_signer = {
        "timestamp": timestamp,
        "folder": "grind_posts",
        # Compression/qualité automatique + limite de résolution — Cloudinary
        # fait le travail de redimensionnement/compression demandé.
        "eager": "q_auto,w_1080,h_1080,c_limit",
    }
    signature = _signer(params_a_signer)

    return {
        "cloud_name": CLOUDINARY_CLOUD_NAME,
        "api_key": CLOUDINARY_API_KEY,
        "timestamp": timestamp,
        "signature": signature,
        "folder": params_a_signer["folder"],
        "eager": params_a_signer["eager"],
        "duree_max_secondes": DUREE_MAX_VIDEO_SECONDES,
        "poids_max_mo": POIDS_MAX_MO,
    }
