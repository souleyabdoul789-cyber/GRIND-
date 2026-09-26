import secrets
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from auth_grind import verifier_session
from schemas import ProfilCreate, ClasseCreate, ClasseRejoindre, ClasseRenommer
from models import Profil, Classe, ClasseMembre
from ws_manager import manager

router = APIRouter()


def _identite(session_token: str, db: Session) -> str:
    username = verifier_session(session_token, db)
    if username is None:
        raise HTTPException(status_code=401, detail="Session GRIND invalide ou expirée — reconnecte-toi")
    return username


@router.post("/api/profil")
async def creer_ou_maj_profil(data: ProfilCreate, db: Session = Depends(get_db)):
    username = _identite(data.session_token, db)

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


@router.get("/api/profil/moi")
async def lire_mon_profil(session_token: str, db: Session = Depends(get_db)):
    username = _identite(session_token, db)
    profil = db.get(Profil, username)

    return {
        "username": username,
        "profil_existe": profil is not None,
        "age": profil.age if profil else None,
        "pays": profil.pays if profil else None,
        "niveau": profil.niveau if profil else None,
        "difficultes": profil.difficultes if profil else None,
    }


@router.post("/api/classes")
async def creer_classe(data: ClasseCreate, db: Session = Depends(get_db)):
    username = _identite(data.session_token, db)

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
    username = _identite(data.session_token, db)

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

    await manager.notifier_nouveau_membre_classe(classe.id, classe.nom, classe.createur_username, username)

    return {"success": True, "classe_id": classe.id, "nom": classe.nom}


@router.get("/api/classes/mes-classes")
async def lister_mes_classes(session_token: str, db: Session = Depends(get_db)):
    username = _identite(session_token, db)

    memberships = db.query(ClasseMembre).filter(ClasseMembre.username == username).all()
    resultat = []
    for m in memberships:
        classe = db.get(Classe, m.classe_id)
        if classe is None:
            continue
        nb_membres = db.query(ClasseMembre).filter(ClasseMembre.classe_id == classe.id).count()
        resultat.append({
            "classe_id": classe.id,
            "nom": classe.nom,
            "role": m.role,
            "code_invitation": classe.code_invitation,
            "nb_membres": nb_membres,
        })

    return {"classes": resultat}


@router.patch("/api/classes/{classe_id}")
async def renommer_classe(classe_id: int, data: ClasseRenommer, db: Session = Depends(get_db)):
    username = _identite(data.session_token, db)

    classe = db.get(Classe, classe_id)
    if classe is None:
        raise HTTPException(status_code=404, detail="Classe introuvable")
    if classe.createur_username != username:
        raise HTTPException(status_code=403, detail="Seul le créateur peut renommer cette classe")

    classe.nom = data.nom
    db.commit()

    return {"success": True, "nom": classe.nom}


@router.delete("/api/classes/{classe_id}")
async def supprimer_classe(classe_id: int, session_token: str, db: Session = Depends(get_db)):
    username = _identite(session_token, db)

    classe = db.get(Classe, classe_id)
    if classe is None:
        raise HTTPException(status_code=404, detail="Classe introuvable")
    if classe.createur_username != username:
        raise HTTPException(status_code=403, detail="Seul le créateur peut supprimer cette classe")

    db.query(ClasseMembre).filter(ClasseMembre.classe_id == classe_id).delete()
    db.delete(classe)
    db.commit()

    return {"success": True}
