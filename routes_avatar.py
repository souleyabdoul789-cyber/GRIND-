"""
routes_avatar.py — lecture/écriture de la config d'avatar.

Sécurité : même si le frontend n'affiche que les objets débloqués, on
revérifie TOUJOURS côté serveur qu'un item choisi est bien gratuit ou
débloqué pour cet utilisateur avant d'accepter — sinon n'importe qui
pourrait poser un item verrouillé en appelant l'API directement,
contournant complètement le futur système de monnaie G.
"""

import re
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from database import get_db
from auth_grind import verifier_session
from models import AvatarConfig, AvatarDeblocage
from avatar_catalogue import CATALOGUE, CATEGORIES_OBLIGATOIRES, CATEGORIES_OPTIONNELLES, item_existe, item_est_gratuit

router = APIRouter()


class AvatarUpdate(BaseModel):
    session_token: str
    genre: str
    corps: str
    visage: str
    cheveux: str
    vetement: str
    couleur_aura: str
    chaussures: str
    peau: str
    couleur_cheveux: str
    chapeau: Optional[str] = None
    lunettes: Optional[str] = None
    gants: Optional[str] = None

    @field_validator("peau", "couleur_cheveux")
    @classmethod
    def valider_hex(cls, v):
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", v):
            raise ValueError("Couleur invalide — format attendu #rrggbb")
        return v


def _identite(session_token: str, db: Session) -> str:
    username = verifier_session(session_token, db)
    if username is None:
        raise HTTPException(status_code=401, detail="Session GRIND invalide ou expirée")
    return username


def _est_debloque(db: Session, username: str, item_id: str) -> bool:
    if item_est_gratuit(item_id):
        return True
    return db.query(AvatarDeblocage).filter(
        AvatarDeblocage.username == username, AvatarDeblocage.item_id == item_id
    ).first() is not None


def _serialiser(config: AvatarConfig) -> dict:
    """Renvoie toujours une config complète et valide, même pour d'anciennes
    lignes de la base (colonnes ajoutées après coup = NULL, ou genre stocké
    sans le préfixe "genre_" par une ancienne version)."""
    genre = config.genre or "genre_neutre"
    if not genre.startswith("genre_"):
        genre = f"genre_{genre}"

    return {
        "genre": genre,
        "corps": config.corps or "corps_base",
        "visage": config.visage or "visage_base",
        "cheveux": config.cheveux or "cheveux_base",
        "vetement": config.vetement or "vetement_base",
        "chaussures": config.chaussures or "chaussures_base",
        "chapeau": config.chapeau,
        "lunettes": config.lunettes,
        "gants": config.gants,
        "couleur_aura": config.couleur_aura or "brand",
        "peau": config.peau or "#e8c4a0",
        "couleur_cheveux": config.couleur_cheveux or "#2a2a35",
    }


@router.get("/api/avatar/catalogue")
async def obtenir_catalogue():
    return {"catalogue": CATALOGUE}


@router.get("/api/avatar/moi")
async def obtenir_mon_avatar(session_token: str, db: Session = Depends(get_db)):
    username = _identite(session_token, db)

    config = db.get(AvatarConfig, username)
    if config is None:
        config = AvatarConfig(username=username)
        db.add(config)
        db.commit()

    deblocages = db.query(AvatarDeblocage).filter(AvatarDeblocage.username == username).all()
    items_debloques = [d.item_id for d in deblocages] + [i["id"] for i in CATALOGUE if i["gratuit"]]

    return {"config": _serialiser(config), "items_debloques": items_debloques}


@router.get("/api/avatar/de/{username}")
async def obtenir_avatar_de(username: str, db: Session = Depends(get_db)):
    """Public — nécessaire pour afficher l'avatar des autres membres
    dans la salle de classe, pas de donnée sensible dedans."""
    config = db.get(AvatarConfig, username)
    if config is None:
        return {"config": None}
    return {"config": _serialiser(config)}


@router.post("/api/avatar")
async def enregistrer_avatar(data: AvatarUpdate, db: Session = Depends(get_db)):
    username = _identite(data.session_token, db)

    choix = {
        "genre": data.genre, "corps": data.corps, "visage": data.visage,
        "cheveux": data.cheveux, "vetement": data.vetement, "couleur_aura": data.couleur_aura,
        "chaussures": data.chaussures,
    }
    choix_optionnels = {"chapeau": data.chapeau, "lunettes": data.lunettes, "gants": data.gants}

    for categorie, item_id in choix.items():
        if not item_existe(item_id, categorie):
            raise HTTPException(status_code=400, detail=f"Objet invalide pour {categorie}: {item_id}")
        if not _est_debloque(db, username, item_id):
            raise HTTPException(status_code=403, detail=f"Objet verrouillé, pas encore débloqué: {item_id}")

    for categorie, item_id in choix_optionnels.items():
        if item_id is None:
            continue
        if not item_existe(item_id, categorie):
            raise HTTPException(status_code=400, detail=f"Objet invalide pour {categorie}: {item_id}")
        if not _est_debloque(db, username, item_id):
            raise HTTPException(status_code=403, detail=f"Objet verrouillé, pas encore débloqué: {item_id}")

    config = db.get(AvatarConfig, username)
    if config is None:
        config = AvatarConfig(username=username)
        db.add(config)

    config.genre = data.genre
    config.corps = data.corps
    config.visage = data.visage
    config.cheveux = data.cheveux
    config.vetement = data.vetement
    config.couleur_aura = data.couleur_aura
    config.chapeau = data.chapeau
    config.lunettes = data.lunettes
    config.gants = data.gants
    config.chaussures = data.chaussures
    config.peau = data.peau
    config.couleur_cheveux = data.couleur_cheveux
    db.commit()

    return {"success": True, "config": _serialiser(config)}
