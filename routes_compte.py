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

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from pluton_client import verifier_cle
from schemas import CompteGrindSignup, CompteGrindLogin
from models import CompteGrind
from auth_grind import hacher_mot_de_passe, verifier_mot_de_passe, creer_session

router = APIRouter()


@router.post("/api/compte/signup")
async def signup(data: CompteGrindSignup, db: Session = Depends(get_db)):
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
    )
    db.add(compte)
    db.commit()

    return {"success": True, "message": "Compte GRIND créé — connecte-toi maintenant"}


@router.post("/api/compte/login")
async def login(data: CompteGrindLogin, db: Session = Depends(get_db)):
    compte = db.get(CompteGrind, data.username)
    if compte is None or not verifier_mot_de_passe(data.password, compte.password_hash):
        raise HTTPException(status_code=401, detail="Identifiants GRIND incorrects")

    token = creer_session(compte.username, db)
    return {"success": True, "session_token": token, "username": compte.username}
