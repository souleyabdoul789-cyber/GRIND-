"""
media.py — upload direct vers Cloudinary (photos/vidéos des posts).

Principe : le client demande une signature ici (avec sa session GRIND
valide), on la génère avec le secret Cloudinary — qui ne quitte JAMAIS
le serveur — puis le client upload le fichier lui-même, en direct,
vers l'API Cloudinary. Notre serveur ne voit jamais le fichier,
donc pas de souci de taille/bande passante côté Render.

Cloudinary fait tout le travail lourd : compression, redimensionnement,
incrustation du watermark, et découpe vidéo (début/fin) — rien de ça
ne tourne sur le téléphone de l'utilisateur.
"""

import os
import time
import hashlib
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from auth_grind import verifier_session

router = APIRouter()

CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME")
CLOUDINARY_API_KEY = os.getenv("CLOUDINARY_API_KEY")
CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET")

DUREE_MAX_VIDEO_SECONDES = 120
POIDS_MAX_MO = 25

WATERMARK = "l_grind_watermark,g_south_east,x_24,y_24,fl_layer_apply"


class SignatureUploadRequest(BaseModel):
    session_token: str
    debut_video: Optional[float] = None  # secondes — découpe faite par Cloudinary, pas le téléphone
    fin_video: Optional[float] = None


def _signer(params: dict) -> str:
    chaine = "&".join(f"{k}={v}" for k, v in sorted(params.items())) + CLOUDINARY_API_SECRET
    return hashlib.sha1(chaine.encode()).hexdigest()


@router.post("/api/media/signature-upload")
async def signature_upload(data: SignatureUploadRequest, db: Session = Depends(get_db)):
    username = verifier_session(data.session_token, db)
    if username is None:
        raise HTTPException(status_code=401, detail="Session GRIND invalide ou expirée")

    if not all([CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET]):
        raise HTTPException(status_code=500, detail="Cloudinary non configuré côté serveur")

    # Étape 1 : découpe vidéo (si demandée) + compression/redimensionnement.
    # Étape 2 (après "/") : incrustation du watermark.
    etape1 = "q_auto,w_1080,h_1080,c_limit"
    if data.debut_video is not None and data.fin_video is not None:
        etape1 = f"so_{data.debut_video},eo_{data.fin_video},{etape1}"

    timestamp = int(time.time())
    params_a_signer = {
        "timestamp": timestamp,
        "folder": "grind_posts",
        "eager": f"{etape1}/{WATERMARK}",
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
