import secrets
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from pluton_client import verifier_cle
from schemas import ProfilCreate, ClasseCreate, ClasseRejoindre
from models import Profil, Classe, ClasseMembre

router = APIRouter()


@router.post("/api/profil")
async def creer_ou_maj_profil(data: ProfilCreate, db: Session = Depends(get_db)):
    valide, username = await verifier_cle(data.api_key)
    if not valide:
        raise HTTPException(status_code=401, detail="Clé API invalide ou expirée")

    profil = db.get(Profil, username)
    if profil is None:
        profil = Profil(username=username)
        db.add(profil)

    profil.age = data.age
    profil.pays = data.pays
    profil.niveau = data.niveau
    profil.difficultes = data.difficultes
    db.commit()

    return {"success": True, "message": "Profil enregistré"}


@router.post("/api/classes")
async def creer_classe(data: ClasseCreate, db: Session = Depends(get_db)):
    valide, username = await verifier_cle(data.api_key)
    if not valide:
        raise HTTPException(status_code=401, detail="Clé API invalide ou expirée")

    code = secrets.token_urlsafe(8)
    classe = Classe(nom=data.nom, code_invitation=code, createur_username=username)
    db.add(classe)
    db.commit()
    db.refresh(classe)

    db.add(ClasseMembre(classe_id=classe.id, username=username, role="prof"))
    db.commit()

    return {
        "success": True,
        "classe_id": classe.id,
        "nom": classe.nom,
        "lien_invitation": f"/rejoindre/{code}",
    }


@router.post("/api/classes/rejoindre")
async def rejoindre_classe(data: ClasseRejoindre, db: Session = Depends(get_db)):
    valide, username = await verifier_cle(data.api_key)
    if not valide:
        raise HTTPException(status_code=401, detail="Clé API invalide ou expirée")

    classe = db.query(Classe).filter(Classe.code_invitation == data.code_invitation).first()
    if classe is None:
        raise HTTPException(status_code=404, detail="Lien d'invitation invalide")

    deja_membre = db.query(ClasseMembre).filter(
        ClasseMembre.classe_id == classe.id,
        ClasseMembre.username == username,
    ).first()
    if deja_membre:
        return {"success": True, "message": "Déjà membre", "classe_id": classe.id}

    db.add(ClasseMembre(classe_id=classe.id, username=username, role="membre"))
    db.commit()

    return {"success": True, "classe_id": classe.id, "nom": classe.nom}
