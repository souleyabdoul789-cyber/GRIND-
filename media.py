"""Upload GRIND de médias et de documents vers Cloudinary.

Les fichiers sont reçus par l'API, contrôlés (taille, extension, MIME et signature
binaire), puis relayés à Cloudinary avec une signature calculée côté serveur.
Aucun secret Cloudinary n'est envoyé au navigateur.
"""

import hashlib
import math
import os
import re
import time
import zipfile
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
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
POIDS_MAX_DOCUMENT_MO = 15
WATERMARK = "l_grind_watermark,g_south_east,x_24,y_24,fl_layer_apply"

IMAGES = {
    ".jpg": ("image/jpeg", (b"\xff\xd8\xff",)),
    ".jpeg": ("image/jpeg", (b"\xff\xd8\xff",)),
    ".png": ("image/png", (b"\x89PNG\r\n\x1a\n",)),
    ".webp": ("image/webp", (b"RIFF",)),
}
VIDEOS = {
    ".mp4": ("video/mp4", (b"ftyp",)),
    ".mov": ("video/quicktime", (b"ftyp",)),
    ".webm": ("video/webm", (b"\x1aE\xdf\xa3",)),
}
DOCUMENTS = {
    ".pdf": ("application/pdf", (b"%PDF-",)),
    ".docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", (b"PK\x03\x04",)),
}


def _signer(params: dict) -> str:
    chaine = "&".join(f"{k}={v}" for k, v in sorted(params.items())) + CLOUDINARY_API_SECRET
    return hashlib.sha1(chaine.encode()).hexdigest()


def _verifier_compte_upload(session_token: str, db: Session) -> str:
    username = verifier_session(session_token, db, autoriser_proviseur=True)
    if username is None:
        raise HTTPException(status_code=401, detail="Session GRIND invalide ou expirée")
    from models_proviseur import ProviseurCandidature
    candidature = db.query(ProviseurCandidature).filter_by(username=username).first()
    if candidature and candidature.statut != "approuve":
        raise HTTPException(status_code=403, detail="Le compte établissement est en attente de validation.")
    return username


def _informations_fichier(upload: UploadFile, groupe: str, limite_octets: int):
    nom = Path(upload.filename or "fichier").name
    extension = Path(nom).suffix.lower()
    catalogue = IMAGES if groupe == "image" else VIDEOS if groupe == "video" else DOCUMENTS
    specification = catalogue.get(extension)
    if specification is None:
        raise HTTPException(status_code=415, detail="Format de fichier non accepté.")
    try:
        upload.file.seek(0, os.SEEK_END)
        taille = upload.file.tell()
        upload.file.seek(0)
        entete = upload.file.read(512)
        upload.file.seek(0)
    except Exception:
        raise HTTPException(status_code=400, detail="Fichier illisible.")
    if taille <= 0 or taille > limite_octets:
        raise HTTPException(status_code=413, detail=f"Le fichier doit être non vide et ne pas dépasser {limite_octets // (1024 * 1024)} Mo.")
    mime, signatures = specification
    signature_ok = any(entete.startswith(sig) for sig in signatures)
    if groupe == "video" and extension in (".mp4", ".mov"):
        signature_ok = len(entete) >= 12 and entete[4:8] == b"ftyp"
    if groupe == "image" and extension == ".webp":
        signature_ok = len(entete) >= 12 and entete[:4] == b"RIFF" and entete[8:12] == b"WEBP"
    if not signature_ok:
        raise HTTPException(status_code=415, detail="Le contenu du fichier ne correspond pas à son extension.")
    mime_declare = (upload.content_type or "").split(";", 1)[0].strip().lower()
    if mime_declare and mime_declare != "application/octet-stream" and mime_declare != mime:
        raise HTTPException(status_code=415, detail="Le type MIME déclaré ne correspond pas au fichier.")
    if groupe == "document" and extension == ".docx":
        try:
            with zipfile.ZipFile(upload.file) as archive:
                noms = set(archive.namelist())
                if "[Content_Types].xml" not in noms or "word/document.xml" not in noms:
                    raise HTTPException(status_code=415, detail="Le fichier DOCX n'est pas un document Word valide.")
        except zipfile.BadZipFile:
            raise HTTPException(status_code=415, detail="Le fichier DOCX est endommagé ou invalide.")
        finally:
            upload.file.seek(0)
    return nom[:180], mime, taille


async def _envoyer_cloudinary(upload: UploadFile, nom: str, mime: str, ressource: str, params: dict):
    if not all([CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET]):
        raise HTTPException(status_code=503, detail="Le stockage Cloudinary n'est pas configuré côté serveur.")
    timestamp = int(time.time())
    params_signes = {"timestamp": timestamp, **params}
    donnees = {
        "api_key": CLOUDINARY_API_KEY,
        "timestamp": str(timestamp),
        "signature": _signer(params_signes),
        **{k: str(v) for k, v in params.items()},
    }
    url = f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/{ressource}/upload"
    try:
        upload.file.seek(0)
        timeout = httpx.Timeout(120.0, connect=15.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.post(
                url,
                data=donnees,
                files={"file": (nom, upload.file, mime)},
            )
        resultat = response.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(status_code=502, detail="Le stockage de fichiers est temporairement indisponible.")
    if response.status_code >= 400:
        # Ne renvoie pas les détails internes de configuration de Cloudinary au navigateur.
        raise HTTPException(status_code=502, detail="Cloudinary a refusé le fichier. Vérifie le format puis réessaie.")
    return resultat


@router.post("/api/media/upload")
async def uploader_media(
    session_token: str = Form(...),
    file: UploadFile = File(...),
    debut_video: Optional[float] = Form(None),
    fin_video: Optional[float] = Form(None),
    db: Session = Depends(get_db),
):
    _verifier_compte_upload(session_token, db)
    extension = Path(file.filename or "").suffix.lower()
    groupe = "video" if extension in VIDEOS else "image"
    nom, mime, _ = _informations_fichier(file, groupe, POIDS_MAX_MO * 1024 * 1024)
    if groupe == "video":
        if debut_video is None or fin_video is None:
            raise HTTPException(status_code=400, detail="Une découpe vidéo est requise : clip de 120 secondes maximum.")
        if not math.isfinite(debut_video) or not math.isfinite(fin_video) or debut_video < 0 or fin_video <= debut_video or fin_video > DUREE_MAX_VIDEO_SECONDES:
            raise HTTPException(status_code=400, detail="Découpe vidéo invalide : durée maximale de 120 secondes.")
    transformation = "q_auto,w_1080,h_1080,c_limit"
    if groupe == "video" and debut_video is not None:
        transformation = f"so_{debut_video},eo_{fin_video},{transformation}"
    params = {
        "folder": "grind_posts",
        "eager": f"{transformation}/{WATERMARK}",
        "allowed_formats": "jpg,jpeg,png,webp,mp4,mov,webm",
    }
    resultat = await _envoyer_cloudinary(file, nom, mime, "video" if groupe == "video" else "image", params)
    eager = resultat.get("eager") or []
    url = next((item.get("secure_url") for item in eager if item.get("secure_url")), None) or resultat.get("secure_url")
    if not url or not url.startswith("https://res.cloudinary.com/"):
        raise HTTPException(status_code=502, detail="Cloudinary n'a pas retourné de média exploitable.")
    return {"media_url": url, "media_type": "video" if groupe == "video" else "image"}


@router.post("/api/media/document")
async def uploader_document(
    session_token: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    _verifier_compte_upload(session_token, db)
    nom, mime, _ = _informations_fichier(file, "document", POIDS_MAX_DOCUMENT_MO * 1024 * 1024)
    params = {"folder": "grind_documents", "allowed_formats": "pdf,docx"}
    resultat = await _envoyer_cloudinary(file, nom, mime, "raw", params)
    url = resultat.get("secure_url")
    if not url or not url.startswith("https://res.cloudinary.com/"):
        raise HTTPException(status_code=502, detail="Cloudinary n'a pas retourné de document exploitable.")
    return {"document_url": url, "document_name": nom, "document_type": mime}


@router.post("/api/proviseur/logo/upload")
async def uploader_logo_proviseur(
    session_token: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    username = _verifier_compte_upload(session_token, db)
    from models_proviseur import ProviseurCandidature
    candidature = db.query(ProviseurCandidature).filter_by(username=username, statut="approuve").first()
    if candidature is None:
        raise HTTPException(status_code=403, detail="Un compte proviseur approuvé est requis.")
    if Path(file.filename or "").suffix.lower() not in (".jpg", ".jpeg", ".png"):
        raise HTTPException(status_code=415, detail="Le logo doit être une image JPEG ou PNG.")
    nom, mime, _ = _informations_fichier(file, "image", 5 * 1024 * 1024)
    params = {
        "folder": "grind_establishments",
        "eager": "q_auto,w_512,h_512,c_limit",
        "allowed_formats": "jpg,jpeg,png",
    }
    resultat = await _envoyer_cloudinary(file, nom, mime, "image", params)
    eager = resultat.get("eager") or []
    url = next((item.get("secure_url") for item in eager if item.get("secure_url")), None) or resultat.get("secure_url")
    if not url or not url.startswith("https://res.cloudinary.com/"):
        raise HTTPException(status_code=502, detail="Cloudinary n'a pas retourné de logo exploitable.")
    return {"logo_url": url}
