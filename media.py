"""Upload GRIND de médias et de documents vers Cloudinary.

Les fichiers sont reçus par l'API, contrôlés (taille, extension, MIME et signature
binaire), puis relayés à Cloudinary avec une signature calculée côté serveur.
Aucun secret Cloudinary n'est envoyé au navigateur.
"""

import hashlib
import logging
import math
import os
import re
import time
import uuid
import hmac
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
logger = logging.getLogger("grind.media")

CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME")
CLOUDINARY_API_KEY = os.getenv("CLOUDINARY_API_KEY")
CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET")

DUREE_MAX_VIDEO_SECONDES = 3540  # 59 minutes : format cours, pas clip court
POIDS_MAX_IMAGE_MO = 25
POIDS_MAX_VIDEO_MO = 300
SEUIL_CHUNK_CLOUDINARY_OCTETS = 90 * 1024 * 1024
TAILLE_CHUNK_CLOUDINARY_OCTETS = 20 * 1024 * 1024  # Cloudinary exige > 5 Mo sauf dernier chunk
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
AUDIO = {
    ".webm": ("audio/webm", (b"\x1aE\xdf\xa3",)),
    ".ogg": ("audio/ogg", (b"OggS",)),
    ".m4a": ("audio/mp4", (b"ftyp",)),
    ".mp3": ("audio/mpeg", (b"ID3",)),
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
    catalogue = IMAGES if groupe == "image" else VIDEOS if groupe == "video" else AUDIO if groupe == "audio" else DOCUMENTS
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
    if groupe == "audio" and extension == ".m4a":
        signature_ok = len(entete) >= 12 and entete[4:8] == b"ftyp"
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


def _donnees_signature_cloudinary(params: dict) -> tuple[int, dict]:
    timestamp = int(time.time())
    params_signes = {"timestamp": timestamp, **params}
    donnees = {
        "api_key": CLOUDINARY_API_KEY,
        "timestamp": str(timestamp),
        "signature": _signer(params_signes),
        **{k: str(v) for k, v in params.items()},
    }
    return timestamp, donnees


async def _envoyer_cloudinary_chunked(upload: UploadFile, nom: str, mime: str, ressource: str, donnees: dict, url: str, taille_totale: int):
    """Relaye un UploadFile vers Cloudinary en chunks sans charger tout le fichier en RAM."""
    upload_id = str(uuid.uuid4())
    resultat_final = None
    debut = 0
    timeout = httpx.Timeout(900.0, connect=30.0)
    try:
        upload.file.seek(0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            while debut < taille_totale:
                morceau = upload.file.read(TAILLE_CHUNK_CLOUDINARY_OCTETS)
                if not morceau:
                    raise HTTPException(status_code=400, detail="Le fichier reçu est incomplet.")
                fin = debut + len(morceau) - 1
                headers = {
                    "X-Unique-Upload-Id": upload_id,
                    "Content-Range": f"bytes {debut}-{fin}/{taille_totale}",
                }
                response = await client.post(
                    url,
                    data=donnees,
                    files={"file": (nom, morceau, mime)},
                    headers=headers,
                )
                if response.status_code >= 400:
                    raise HTTPException(status_code=502, detail="Cloudinary a refusé un segment du fichier. Vérifie les limites de ton compte Cloudinary puis réessaie.")
                resultat = response.json()
                if fin + 1 < taille_totale and resultat.get("done") is not False:
                    raise HTTPException(status_code=502, detail="Cloudinary n'a pas confirmé la réception d'un segment intermédiaire.")
                if fin + 1 == taille_totale:
                    if resultat.get("done") is not True:
                        raise HTTPException(status_code=502, detail="Cloudinary n'a pas confirmé la fin du transfert.")
                    resultat_final = resultat
                debut = fin + 1
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError, OSError):
        raise HTTPException(status_code=502, detail="Le transfert Cloudinary a échoué. Vérifie la connexion puis réessaie.")
    finally:
        try:
            upload.file.seek(0)
        except Exception:
            pass
    if not resultat_final:
        raise HTTPException(status_code=502, detail="Cloudinary n'a pas confirmé le fichier complet.")
    return resultat_final


async def _envoyer_cloudinary(upload: UploadFile, nom: str, mime: str, ressource: str, params: dict):
    if not all([CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET]):
        raise HTTPException(status_code=503, detail="Le stockage Cloudinary n'est pas configuré côté serveur.")
    try:
        upload.file.seek(0, os.SEEK_END)
        taille = upload.file.tell()
        upload.file.seek(0)
    except Exception:
        raise HTTPException(status_code=400, detail="Fichier illisible.")
    _, donnees = _donnees_signature_cloudinary(params)
    url = f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/{ressource}/upload"
    if taille > SEUIL_CHUNK_CLOUDINARY_OCTETS:
        return await _envoyer_cloudinary_chunked(upload, nom, mime, ressource, donnees, url, taille)
    try:
        timeout = httpx.Timeout(900.0, connect=30.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.post(url, data=donnees, files={"file": (nom, upload.file, mime)})
        resultat = response.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(status_code=502, detail="Le stockage de fichiers est temporairement indisponible.")
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Cloudinary a refusé le fichier. Vérifie le format puis réessaie.")
    return resultat


def _parametres_upload_media(groupe: str, debut_video: Optional[float], fin_video: Optional[float]) -> dict:
    transformation = "q_auto,w_1080,h_1080,c_limit"
    if groupe == "video":
        transformation = f"so_{debut_video},eo_{fin_video},{transformation}"
    return {
        "folder": "grind_posts",
        "eager": f"{transformation}/{WATERMARK}",
        "allowed_formats": "jpg,jpeg,png,webp,mp4,mov,webm",
    }


def _valider_decoupe_video(debut_video: Optional[float], fin_video: Optional[float]):
    if debut_video is None or fin_video is None:
        raise HTTPException(status_code=400, detail="Une découpe vidéo est requise : vidéo de 59 minutes maximum.")
    if not math.isfinite(debut_video) or not math.isfinite(fin_video) or debut_video < 0 or fin_video <= debut_video or fin_video > DUREE_MAX_VIDEO_SECONDES:
        raise HTTPException(status_code=400, detail="Découpe vidéo invalide : durée maximale de 59 minutes.")


@router.post("/api/media/upload/signature")
async def signer_upload_media(data: dict, db: Session = Depends(get_db)):
    """Prépare un upload direct Cloudinary : le secret reste sur le serveur."""
    _verifier_compte_upload(data.get("session_token", ""), db)
    if not all([CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET]):
        raise HTTPException(status_code=503, detail="Le stockage Cloudinary n'est pas configuré côté serveur.")
    nom = Path(str(data.get("file_name") or "media")).name[:180]
    extension = Path(nom).suffix.lower()
    taille = data.get("file_size")
    mime_declare = str(data.get("content_type") or "").split(";", 1)[0].strip().lower()
    try:
        taille = int(taille)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Taille du fichier invalide.")
    groupe = "video" if extension in VIDEOS else "image" if extension in IMAGES else None
    if not groupe:
        raise HTTPException(status_code=415, detail="Format média non accepté.")
    limite_mo = POIDS_MAX_VIDEO_MO if groupe == "video" else POIDS_MAX_IMAGE_MO
    if taille <= 0 or taille > limite_mo * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"Le fichier ne doit pas dépasser {limite_mo} Mo.")
    mime_attendu = (VIDEOS if groupe == "video" else IMAGES)[extension][0]
    if mime_declare and mime_declare not in (mime_attendu, "application/octet-stream"):
        raise HTTPException(status_code=415, detail="Le type MIME ne correspond pas au format du fichier.")
    debut_video = data.get("debut_video")
    fin_video = data.get("fin_video")
    if groupe == "video":
        try:
            debut_video = float(debut_video)
            fin_video = float(fin_video)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Une découpe vidéo valide est requise.")
        _valider_decoupe_video(debut_video, fin_video)
    params = _parametres_upload_media(groupe, debut_video, fin_video)
    # Un identifiant unique évite d'écraser une publication déjà envoyée sous le même nom.
    params["public_id"] = uuid.uuid4().hex
    _, donnees = _donnees_signature_cloudinary(params)
    return {
        "upload_url": f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/{ 'video' if groupe == 'video' else 'image' }/upload",
        "resource_type": "video" if groupe == "video" else "image",
        "api_key": CLOUDINARY_API_KEY,
        "timestamp": donnees["timestamp"],
        "signature": donnees["signature"],
        "params": {k: str(v) for k, v in params.items()},
        "taille_max_octets": limite_mo * 1024 * 1024,
        "taille_chunk_octets": TAILLE_CHUNK_CLOUDINARY_OCTETS,
    }


@router.post("/api/media/upload/confirm")
async def confirmer_upload_media(data: dict, db: Session = Depends(get_db)):
    username = _verifier_compte_upload(data.get("session_token", ""), db)
    if not CLOUDINARY_API_SECRET:
        raise HTTPException(status_code=503, detail="La validation du stockage n'est pas configurée.")
    asset = data.get("asset") or {}
    public_id = str(asset.get("public_id") or "")
    version = str(asset.get("version") or "")
    signature = str(asset.get("signature") or "")
    secure_url = str(asset.get("secure_url") or "")
    resource_type = str(asset.get("resource_type") or "")
    try:
        taille = int(asset.get("bytes") or 0)
    except (TypeError, ValueError):
        taille = 0
    attendu = hashlib.sha1(f"public_id={public_id}&version={version}{CLOUDINARY_API_SECRET}".encode()).hexdigest()
    from urllib.parse import urlparse
    parsed = urlparse(secure_url)
    chemin_upload = f"/{resource_type}/upload/"
    host_ok = parsed.scheme == "https" and parsed.hostname == "res.cloudinary.com"
    limite_octets = (POIDS_MAX_VIDEO_MO if resource_type == "video" else POIDS_MAX_IMAGE_MO) * 1024 * 1024
    if (not public_id.startswith("grind_posts/") or not version.isdigit() or
            not hmac.compare_digest(signature, attendu) or not host_ok or
            resource_type not in ("image", "video") or chemin_upload not in parsed.path or
            taille <= 0 or taille > limite_octets):
        raise HTTPException(status_code=400, detail="La réponse du stockage n'a pas pu être vérifiée.")

    # Recrée côté serveur l'URL eager prévue et signée à l'origine : le clip
    # publié garde son cadrage et son watermark, sans faire confiance à une URL eager fournie par le navigateur.
    if resource_type == "video":
        try:
            debut_video = float(data.get("debut_video"))
            fin_video = float(data.get("fin_video"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Découpe vidéo manquante pour confirmer le média.")
        _valider_decoupe_video(debut_video, fin_video)
        transformation = _parametres_upload_media("video", debut_video, fin_video)["eager"]
    else:
        transformation = _parametres_upload_media("image", None, None)["eager"]
    media_url = secure_url.replace(chemin_upload, f"{chemin_upload}{transformation}/", 1)
    from media_assets import enregistrer_media_asset
    formats_mime = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp",
        "mp4": "video/mp4", "mov": "video/quicktime", "webm": "video/webm",
    }
    mime_verifie = formats_mime.get(str(asset.get("format") or "").lower())
    if not mime_verifie:
        raise HTTPException(status_code=415, detail="Le format réel du média n'est pas reconnu.")
    enregistrer_media_asset(
        db, username=username, url=secure_url,
        mime_type=mime_verifie, size_bytes=taille,
    )
    db.commit()
    return {"media_url": media_url, "media_type": "video" if resource_type == "video" else "image"}


@router.post("/api/media/upload")
async def uploader_media(
    session_token: str = Form(...),
    file: UploadFile = File(...),
    debut_video: Optional[float] = Form(None),
    fin_video: Optional[float] = Form(None),
    db: Session = Depends(get_db),
):
    username = _verifier_compte_upload(session_token, db)
    extension = Path(file.filename or "").suffix.lower()
    groupe = "video" if extension in VIDEOS else "image"
    limite_media = POIDS_MAX_VIDEO_MO if groupe == "video" else POIDS_MAX_IMAGE_MO
    nom, mime, _ = _informations_fichier(file, groupe, limite_media * 1024 * 1024)
    if groupe == "video":
        _valider_decoupe_video(debut_video, fin_video)
    params = _parametres_upload_media(groupe, debut_video, fin_video)
    resultat = await _envoyer_cloudinary(file, nom, mime, "video" if groupe == "video" else "image", params)
    eager = resultat.get("eager") or []
    url = next((item.get("secure_url") for item in eager if item.get("secure_url")), None) or resultat.get("secure_url")
    if not url or not url.startswith("https://res.cloudinary.com/"):
        raise HTTPException(status_code=502, detail="Cloudinary n'a pas retourné de média exploitable.")
    from media_assets import enregistrer_media_asset
    enregistrer_media_asset(
        db, username=username, url=resultat.get("secure_url") or url,
        mime_type=mime, size_bytes=int(resultat.get("bytes") or 0),
    )
    db.commit()
    return {"media_url": url, "media_type": "video" if groupe == "video" else "image"}


@router.post("/api/media/document")
async def uploader_document(
    session_token: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    username = _verifier_compte_upload(session_token, db)
    nom, mime, taille = _informations_fichier(file, "document", POIDS_MAX_DOCUMENT_MO * 1024 * 1024)
    params = {"folder": "grind_documents", "allowed_formats": "pdf,docx"}
    resultat = await _envoyer_cloudinary(file, nom, mime, "raw", params)
    url = resultat.get("secure_url")
    if not url or not url.startswith("https://res.cloudinary.com/"):
        raise HTTPException(status_code=502, detail="Cloudinary n'a pas retourné de document exploitable.")
    from media_assets import enregistrer_media_asset
    enregistrer_media_asset(db, username=username, url=url, mime_type=mime, size_bytes=taille)
    db.commit()
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


def supprimer_assets_cloudinary(urls: list[str]) -> dict:
    """Supprime les assets Cloudinary GRIND connus après une purge DB réussie.

    Retourne des compteurs ; l'absence de credentials ne bloque jamais la purge SQL.
    """
    if not all([CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET]):
        logger.warning("Purge Cloudinary ignorée : identifiants serveur absents (%s fichiers)", len(urls))
        return {"supprimes": 0, "non_supprimes": len(urls)}
    from urllib.parse import urlparse
    import re
    uniques = list(dict.fromkeys(u for u in urls if isinstance(u, str) and u.startswith("https://res.cloudinary.com/")))
    supprimes = 0
    non_supprimes = 0
    with httpx.Client(timeout=httpx.Timeout(20.0, connect=8.0), follow_redirects=False) as client:
        for url in uniques:
            try:
                parsed = urlparse(url)
                morceaux = [x for x in parsed.path.split("/") if x]
                upload_index = morceaux.index("upload")
                resource_type = morceaux[upload_index - 1]
                if resource_type not in ("image", "video", "raw"):
                    non_supprimes += 1
                    continue
                reste = morceaux[upload_index + 1:]
                # Les dossiers GRIND servent de borne fiable après les éventuelles transformations.
                debut = next((i for i, part in enumerate(reste) if part.startswith("grind_")), None)
                if debut is None:
                    version = next((i for i, part in enumerate(reste) if re.fullmatch(r"v\d+", part)), None)
                    debut = version + 1 if version is not None else 0
                public_id = "/".join(reste[debut:])
                if not public_id:
                    non_supprimes += 1
                    continue
                if resource_type in ("image", "video"):
                    public_id = re.sub(r"\.[A-Za-z0-9]+$", "", public_id)
                timestamp = int(time.time())
                params = {"invalidate": "true", "public_id": public_id, "timestamp": timestamp}
                donnees = {
                    "api_key": CLOUDINARY_API_KEY,
                    "timestamp": str(timestamp),
                    "public_id": public_id,
                    "invalidate": "true",
                    "signature": _signer(params),
                }
                endpoint = f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/{resource_type}/destroy"
                response = client.post(endpoint, data=donnees)
                resultat = response.json() if response.content else {}
                if response.status_code == 200 and resultat.get("result") in ("ok", "not found"):
                    supprimes += 1
                else:
                    non_supprimes += 1
            except Exception:
                non_supprimes += 1
    logger.info("Purge Cloudinary compte GRIND : %s asset(s) supprimé(s), %s non supprimé(s)", supprimes, non_supprimes)
    return {"supprimes": supprimes, "non_supprimes": non_supprimes}
