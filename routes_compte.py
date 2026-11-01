"""
routes_compte.py — inscription et connexion PROPRES à GRIND.

Signup : demande un username/password GRIND (choisis ici, pas ceux de
G-SOCIETY) + une clé API G-SOCIETY catégorie "grind" (générée par
l'utilisateur sur le site G-SOCIETY au préalable) — vérifiée une seule
fois auprès de Pluton, uniquement comme preuve de légitimité.

Login : juste username/password GRIND — aucun appel à Pluton, tout est
vérifié localement, ce qui rend GRIND indépendant de la disponibilité
de Pluton une fois le compte créé.
"""

import secrets
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from database import get_db
from pluton_client import verifier_cle
from schemas import CompteGrindSignup, CompteGrindLogin, ChangerMotDePasse
from models import CompteGrind
from auth_grind import hacher_mot_de_passe, verifier_mot_de_passe, creer_session
from moderation import (
    ADMIN_USERNAME, CGU_VERSION, ROLE_ADMIN,
    cle_admin_valide, nom_reserve, sanction_active, message_sanction,
)
from limiteur import trop_d_echecs, noter_echec, oublier
from datetime import datetime, timezone

router = APIRouter()


@router.post("/api/compte/signup")
async def signup(data: CompteGrindSignup, db: Session = Depends(get_db)):
    if nom_reserve(data.username):
        raise HTTPException(status_code=400, detail="Ce nom d'utilisateur est réservé — choisis-en un autre")
    if not data.cgu_acceptees:
        raise HTTPException(status_code=400, detail="Tu dois accepter les conditions d'utilisation (CGU) pour créer un compte")

    valide, gsociety_username = await verifier_cle(data.api_key)
    if not valide:
        raise HTTPException(
            status_code=401,
            detail="Clé API G-SOCIETY invalide ou expirée — génère-en une (catégorie grind) sur G-SOCIETY d'abord",
        )

    if db.get(CompteGrind, data.username):
        raise HTTPException(status_code=400, detail="Ce nom d'utilisateur GRIND est déjà pris")

    compte = CompteGrind(
        username=data.username,
        password_hash=hacher_mot_de_passe(data.password),
        gsociety_username=gsociety_username,
        cgu_version=CGU_VERSION,
        cgu_acceptees_le=datetime.now(timezone.utc),
    )
    db.add(compte)
    db.commit()

    return {"success": True, "message": "Compte GRIND créé — connecte-toi maintenant"}


def _ip(request: Request) -> str:
    transmis = request.headers.get("x-forwarded-for", "")
    if transmis:
        return transmis.split(",")[0].strip()
    return request.client.host if request.client else "inconnue"


def _compte_admin(db: Session) -> CompteGrind:
    """Le compte admin existe dès la première entrée avec la clé. Son mot de
    passe est aléatoire et jeté : la SEULE façon d'y entrer est la clé."""
    compte = db.get(CompteGrind, ADMIN_USERNAME)
    if compte is None:
        compte = CompteGrind(
            username=ADMIN_USERNAME,
            password_hash=hacher_mot_de_passe(secrets.token_urlsafe(48)),
            gsociety_username=ADMIN_USERNAME,
            role=ROLE_ADMIN,
            cgu_version=CGU_VERSION,
            cgu_acceptees_le=datetime.now(timezone.utc),
        )
        db.add(compte)
        db.commit()
    elif compte.role != ROLE_ADMIN:
        compte.role = ROLE_ADMIN
        db.commit()
    return compte


@router.post("/api/compte/login")
async def login(data: CompteGrindLogin, request: Request, db: Session = Depends(get_db)):
    ip = _ip(request)
    if trop_d_echecs(ip):
        raise HTTPException(status_code=429, detail="Trop de tentatives échouées — réessaie dans quelques minutes")

    # Clé d'administration : elle tient lieu de mot de passe, quel que soit le pseudo saisi.
    if cle_admin_valide(data.password):
        compte = _compte_admin(db)
        oublier(ip)
        token = creer_session(compte.username, db)
        return {
            "success": True,
            "session_token": token,
            "username": compte.username,
            "role": ROLE_ADMIN,
            "nom_affiche": compte.nom_affiche,
        }

    compte = db.get(CompteGrind, data.username)
    if (
        compte is None
        or compte.username == ADMIN_USERNAME
        or not verifier_mot_de_passe(data.password, compte.password_hash)
    ):
        noter_echec(ip)
        raise HTTPException(status_code=401, detail="Identifiants GRIND incorrects")

    sanction = sanction_active(db, compte.username)
    if sanction is not None:
        raise HTTPException(status_code=403, detail=message_sanction(sanction))

    oublier(ip)
    token = creer_session(compte.username, db)
    return {
        "success": True,
        "session_token": token,
        "username": compte.username,
        "role": compte.role,
        "nom_affiche": compte.nom_affiche,
    }


@router.post("/api/compte/changer-mot-de-passe")
async def changer_mot_de_passe(data: ChangerMotDePasse, db: Session = Depends(get_db)):
    from auth_grind import verifier_session
    username = verifier_session(data.session_token, db)
    if username is None:
        raise HTTPException(status_code=401, detail="Session GRIND invalide ou expirée")

    compte = db.get(CompteGrind, username)
    if not verifier_mot_de_passe(data.ancien_mot_de_passe, compte.password_hash):
        raise HTTPException(status_code=401, detail="Ancien mot de passe incorrect")

    compte.password_hash = hacher_mot_de_passe(data.nouveau_mot_de_passe)
    db.commit()
    return {"success": True, "message": "Mot de passe changé"}
