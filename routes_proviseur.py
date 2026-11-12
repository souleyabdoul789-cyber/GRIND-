"""Candidatures proviseur : téléphone de contact non vérifié, justificatifs chiffrés et décisions admin.

Les pièces ne passent jamais par Cloudinary ni par un répertoire public : elles sont chiffrées
avec Fernet avant insertion en BLOB dans la base privée. GRIND_PRIVATE_DOCS_KEY doit être
configurée en production ; sans elle, l'upload et la lecture des pièces sont refusés.
"""
import hashlib
import os
import re
import unicodedata
from datetime import datetime, timezone
from io import BytesIO

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from auth_grind import hacher_mot_de_passe, verifier_session
from database import get_db
from models import CompteGrind
from models_proviseur import Etablissement, ProviseurAutorisation, ProviseurCandidature, ProviseurDocument
from moderation import ROLE_ADMIN, role_de
from limiteur import autoriser
from schemas_proviseur import ProviseurDecision, ProviseurLogo
from validation import valider_mot_de_passe

router = APIRouter()
MAX_FICHIER = 5 * 1024 * 1024
MAX_JUSTIFICATIFS = 5
MIMES_IMAGES = {"image/jpeg": (b"\xff\xd8\xff",), "image/png": (b"\x89PNG\r\n\x1a\n",)}
MIMES_DOCUMENTS = {**MIMES_IMAGES, "application/pdf": (b"%PDF-",)}


def _cipher():
    cle = os.getenv("GRIND_PRIVATE_DOCS_KEY", "").strip()
    if not cle:
        raise HTTPException(status_code=503, detail="Le stockage privé des justificatifs n'est pas configuré côté serveur.")
    try:
        return Fernet(cle.encode("ascii"))
    except Exception:
        raise HTTPException(status_code=503, detail="La clé de chiffrement des justificatifs est invalide.")


def _normaliser_nom(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").strip()
    value = re.sub(r"\s+", " ", value)
    if not value or len(value) > 180 or any(ord(c) < 32 for c in value):
        raise HTTPException(status_code=400, detail="Nom d'établissement invalide.")
    return value


def _auth(session_token: str, db: Session):
    username = verifier_session(session_token, db, autoriser_proviseur=True)
    if not username:
        raise HTTPException(status_code=401, detail="Session GRIND invalide ou expirée.")
    return username


def _admin(session_token: str, db: Session):
    username = _auth(session_token, db)
    if role_de(db, username) != ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="Réservé à l'administrateur GRIND.")
    return username


def _lire_upload(file: UploadFile, type_document: str) -> tuple[bytes, str, str]:
    contenu = file.file.read(MAX_FICHIER + 1)
    if not contenu or len(contenu) > MAX_FICHIER:
        raise HTTPException(status_code=400, detail="Chaque fichier doit être non vide et ne pas dépasser 5 Mo.")
    mime = (file.content_type or "").lower().split(";")[0].strip()
    autorises = MIMES_IMAGES if type_document == "photo_personnelle" else MIMES_DOCUMENTS
    signatures = autorises.get(mime)
    if not signatures or not any(contenu.startswith(signature) for signature in signatures):
        raise HTTPException(status_code=400, detail="Format non accepté. Utilise JPEG, PNG ou PDF (photo personnelle : image uniquement).")
    nom = re.sub(r"[^A-Za-z0-9_. ()-]", "_", os.path.basename(file.filename or "document"))[:180] or "document"
    return contenu, mime, nom


def _enregistrer_document(db, candidature, type_document, upload, cipher):
    contenu, mime, nom = _lire_upload(upload, type_document)
    db.add(ProviseurDocument(
        candidature_id=candidature.id,
        type_document=type_document,
        nom_fichier=nom,
        mime_type=mime,
        taille_octets=len(contenu),
        sha256=hashlib.sha256(contenu).hexdigest(),
        contenu_chiffre=cipher.encrypt(contenu),
    ))


@router.post("/api/proviseur/candidature")
async def deposer_candidature(
    request: Request,
    username: str = Form(...), prenom: str = Form(...), nom: str = Form(...),
    telephone: str = Form(...), password: str = Form(...), etablissement: str = Form(...),
    photo_personnelle: UploadFile = File(...), piece_identite: UploadFile = File(...),
    justificatifs: list[UploadFile] = File(default=[]), db: Session = Depends(get_db),
):
    """Cree un compte sans e-mail ; le téléphone n'est qu'une coordonnée manuelle de contact."""
    ip = request.client.host if request.client else "inconnue"
    if not autoriser(f"proviseur-candidature:{ip}", 3, 86400):
        raise HTTPException(status_code=429, detail="Trop de candidatures depuis cette connexion aujourd'hui.")
    username = (username or "").strip()
    if not re.fullmatch(r"[A-Za-z](?:[A-Za-z_]{1,22}[A-Za-z])", username):
        raise HTTPException(status_code=400, detail="Le nom de profil doit contenir 3 à 24 lettres ou underscores uniquement.")
    if db.query(CompteGrind).filter(func.lower(CompteGrind.username) == username.lower()).first():
        raise HTTPException(status_code=409, detail="Ce nom de profil est déjà utilisé.")
    prenom = (prenom or "").strip()
    nom = (nom or "").strip()
    if (not prenom or len(prenom) > 80 or not nom or len(nom) > 80
            or any(ord(c) < 32 for c in prenom + nom)):
        raise HTTPException(status_code=400, detail="Prénom ou nom invalide.")
    telephone = re.sub(r"[\s().-]", "", telephone or "")
    if not re.fullmatch(r"\+[1-9]\d{7,14}", telephone):
        raise HTTPException(status_code=400, detail="Saisis le numéro au format international, avec + et l'indicatif du pays (E.164).")
    if db.query(ProviseurCandidature).filter(ProviseurCandidature.telephone_e164 == telephone).first():
        raise HTTPException(status_code=409, detail="Ce numéro est déjà associé à une candidature.")
    etablissement = _normaliser_nom(etablissement)
    try:
        valider_mot_de_passe(password, username)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if len(justificatifs or []) > MAX_JUSTIFICATIFS:
        raise HTTPException(status_code=400, detail=f"Tu peux joindre au maximum {MAX_JUSTIFICATIFS} justificatifs supplémentaires.")

    cipher = _cipher()
    # Validation complète des fichiers avant de créer le compte.
    files = [("photo_personnelle", photo_personnelle), ("piece_identite", piece_identite)]
    files.extend(("justificatif", f) for f in (justificatifs or []))
    taille_totale = 0
    for kind, f in files:
        f.file.seek(0)
        contenu_test, _, _ = _lire_upload(f, kind)
        taille_totale += len(contenu_test)
        f.file.seek(0)
    if taille_totale > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="La taille totale des pièces ne doit pas dépasser 20 Mo.")

    compte = CompteGrind(
        username=username,
        password_hash=hacher_mot_de_passe(password),
        gsociety_username="__GRIND_NATIVE__",
        role=None,
        email=None,
        email_canonique=None,
        email_verifie=None,  # aucune adresse e-mail ; le statut du dossier fait foi pour le parcours direction
    )
    db.add(compte)
    db.flush()
    candidature = ProviseurCandidature(
        username=username, prenom=prenom, nom=nom, telephone_e164=telephone,
        etablissement_declare=etablissement, statut="en_attente",
    )
    db.add(candidature)
    db.flush()
    try:
        for kind, f in files:
            f.file.seek(0)
            _enregistrer_document(db, candidature, kind, f, cipher)
        db.commit()
    except Exception:
        db.rollback()
        raise

    from auth_grind import creer_session
    token = creer_session(username, db)
    return {"success": True, "session_token": token, "username": username, "role": None,
            "type_compte": "proviseur", "proviseur_statut": "en_attente", "nom_affiche": f"{prenom} {nom}"}


@router.post("/api/proviseur/moi")
async def ma_candidature(data: ProviseurLogo, db: Session = Depends(get_db)):
    username = _auth(data.session_token, db)
    candidature = db.query(ProviseurCandidature).filter_by(username=username).first()
    if candidature is None:
        raise HTTPException(status_code=404, detail="Aucune candidature proviseur pour ce compte.")
    autorisations = db.query(ProviseurAutorisation, Etablissement).join(
        Etablissement, Etablissement.id == ProviseurAutorisation.etablissement_id
    ).filter(ProviseurAutorisation.candidature_id == candidature.id, ProviseurAutorisation.actif == 1).all()
    return {
        "username": username, "prenom": candidature.prenom, "nom": candidature.nom,
        "telephone": candidature.telephone_e164, "etablissement_declare": candidature.etablissement_declare,
        "statut": candidature.statut, "message_candidat": candidature.message_candidat,
        "logo_url": candidature.logo_url,
        "etablissements": [{"id": e.id, "nom": e.nom, "moderation_locale": bool(a.moderation_locale)} for a, e in autorisations],
    }


@router.post("/api/proviseur/logo")
async def changer_logo(data: ProviseurLogo, db: Session = Depends(get_db)):
    username = _auth(data.session_token, db)
    candidature = db.query(ProviseurCandidature).filter_by(username=username, statut="approuve").first()
    if candidature is None:
        raise HTTPException(status_code=403, detail="Un compte proviseur approuvé est requis.")
    url = (data.logo_url or "").strip()
    if not re.fullmatch(r"https://res\.cloudinary\.com/[A-Za-z0-9_-]+/image/upload/.+", url):
        raise HTTPException(status_code=400, detail="Le logo doit être une image HTTPS hébergée sur Cloudinary.")
    candidature.logo_url = url[:512]
    db.commit()
    return {"success": True, "logo_url": candidature.logo_url}


@router.post("/api/admin/proviseurs/liste")
async def liste_candidatures(data: ProviseurDecision, db: Session = Depends(get_db)):
    _admin(data.session_token, db)
    query = db.query(ProviseurCandidature)
    if data.statut in {"en_attente", "approuve", "rejete"}:
        query = query.filter(ProviseurCandidature.statut == data.statut)
    rows = query.order_by(ProviseurCandidature.created_at.asc()).limit(100).all()
    return [{
        "id": c.id, "username": c.username, "prenom": c.prenom, "nom": c.nom,
        "telephone": c.telephone_e164, "etablissement_declare": c.etablissement_declare,
        "statut": c.statut, "created_at": c.created_at.isoformat() if c.created_at else None,
        "note_admin": c.note_admin,
        "documents": [{"id": d[0], "type": d[1], "nom": d[2], "mime": d[3], "taille": d[4]}
                      for d in db.query(ProviseurDocument.id, ProviseurDocument.type_document,
                                        ProviseurDocument.nom_fichier, ProviseurDocument.mime_type,
                                        ProviseurDocument.taille_octets)
                          .filter_by(candidature_id=c.id).all()],
    } for c in rows]


@router.post("/api/admin/proviseurs/document/{document_id}")
async def lire_document_prive(document_id: int, data: ProviseurDecision, db: Session = Depends(get_db)):
    _admin(data.session_token, db)
    cipher = _cipher()
    document = db.get(ProviseurDocument, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document introuvable.")
    try:
        contenu = cipher.decrypt(document.contenu_chiffre)
    except InvalidToken:
        raise HTTPException(status_code=500, detail="Impossible de déchiffrer le document ; vérifie la clé privée configurée.")
    return StreamingResponse(BytesIO(contenu), media_type=document.mime_type,
        headers={"Content-Disposition": f'attachment; filename="{document.nom_fichier.replace(chr(34), "")}"', "Cache-Control": "no-store, private"})


@router.post("/api/admin/proviseurs/traiter")
async def traiter_candidature(data: ProviseurDecision, db: Session = Depends(get_db)):
    admin = _admin(data.session_token, db)
    candidature = db.get(ProviseurCandidature, data.candidature_id)
    if candidature is None:
        raise HTTPException(status_code=404, detail="Candidature introuvable.")
    if data.decision not in {"approuver", "rejeter"}:
        raise HTTPException(status_code=400, detail="Décision invalide.")
    candidature.examine_par = admin
    candidature.examine_le = datetime.now(timezone.utc)
    candidature.note_admin = (data.note_admin or "").strip()[:2000] or None
    candidature.message_candidat = (data.message_candidat or "").strip()[:2000] or None
    if data.decision == "rejeter":
        candidature.statut = "rejete"
        for auth in db.query(ProviseurAutorisation).filter_by(candidature_id=candidature.id).all():
            auth.actif = 0
        db.commit()
        return {"success": True, "statut": candidature.statut}

    noms_etablissements = data.etablissements or [data.etablissement or candidature.etablissement_declare]
    noms_etablissements = [_normaliser_nom(v) for v in noms_etablissements if v and v.strip()]
    if not noms_etablissements or len(noms_etablissements) > 10:
        raise HTTPException(status_code=400, detail="Il faut autoriser entre 1 et 10 établissements.")
    etablissements_autorises = []
    for nom_etab in noms_etablissements:
        cle = unicodedata.normalize("NFKD", nom_etab).encode("ascii", "ignore").decode().lower()
        cle = re.sub(r"[^a-z0-9]+", " ", cle).strip()
        if not cle:
            raise HTTPException(status_code=400, detail="Nom d'établissement non valide.")
        etab = db.query(Etablissement).filter_by(nom_canonique=cle).first()
        if etab is None:
            etab = Etablissement(nom=nom_etab, nom_canonique=cle, cree_par=admin, actif=1)
            db.add(etab)
            db.flush()
        etablissements_autorises.append(etab)
    candidature.etablissement_id = etablissements_autorises[0].id
    candidature.statut = "approuve"
    compte = db.get(CompteGrind, candidature.username)
    if compte is None:
        raise HTTPException(status_code=409, detail="Compte associé à cette candidature introuvable.")
    compte.nom_affiche = f"{candidature.prenom} {candidature.nom}"[:64]
    autorisations = db.query(ProviseurAutorisation).filter_by(candidature_id=candidature.id).all()
    by_id = {a.etablissement_id: a for a in autorisations}
    ids_autorises = {e.id for e in etablissements_autorises}
    for ancienne in autorisations:
        if ancienne.etablissement_id not in ids_autorises:
            ancienne.actif = 0
    for etab in etablissements_autorises:
        existing = by_id.get(etab.id)
        if existing:
            existing.actif = 1
            existing.moderation_locale = int(bool(data.moderation_locale))
            existing.peut_publier = 1
        else:
            db.add(ProviseurAutorisation(candidature_id=candidature.id, etablissement_id=etab.id,
                                        moderation_locale=int(bool(data.moderation_locale)), peut_publier=1, actif=1))
    db.commit()
    return {"success": True, "statut": candidature.statut,
            "etablissements": [e.nom for e in etablissements_autorises]}
