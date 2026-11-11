import json
import re
import secrets
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from auth_grind import verifier_session
from schemas import ProfilCreate, ClasseCreate, ClasseRejoindre, ClasseRenommer, ClasseTransfererProf
from moderation import badge_de, sanction_active
from scolaire import (
    ErreurValidation, valider_age, valider_niveau, valider_difficultes, valider_texte_libre,
    valider_classe, cle_doublon_classe, composer_nom_classe,
)
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

    # Validation SERVEUR de toutes les données scolaires : rien d'absurde n'entre dans la base.
    try:
        age = valider_age(data.age)
        niveau = valider_niveau(data.niveau)
        pays = (data.pays or "").strip()
        pays = pays.lower() if re.fullmatch(r"[A-Za-z]{2}", pays) else valider_texte_libre(pays, min_len=2, max_len=40, nom="Le pays")
        difficultes = valider_difficultes(data.difficultes, niveau)
    except ErreurValidation as erreur:
        raise HTTPException(status_code=400, detail=str(erreur))

    profil.age = age
    profil.pays = pays
    profil.niveau = niveau["label"]
    profil.difficultes = json.dumps(difficultes, ensure_ascii=False)
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


@router.get("/api/profil/de/{username}")
async def lire_profil_de(username: str, db: Session = Depends(get_db)):
    """Public — nécessaire pour qu'un pseudo cliqué dans le fil ouvre
    le profil de son auteur. Rien de sensible dedans (mêmes infos que
    chacun affiche déjà sur son propre profil)."""
    profil = db.get(Profil, username)
    return {
        "username": username,
        "badge": badge_de(db, username),
        "sanctionne": sanction_active(db, username) is not None,
        "profil_existe": profil is not None,
        "age": profil.age if profil else None,
        "pays": profil.pays if profil else None,
        "niveau": profil.niveau if profil else None,
        "difficultes": profil.difficultes if profil else None,
    }


@router.post("/api/classes")
async def creer_classe(data: ClasseCreate, db: Session = Depends(get_db)):
    username = _identite(data.session_token, db)

    try:
        valide = valider_classe(data.niveau or "", data.matiere or "", data.complement)
    except ErreurValidation as erreur:
        raise HTTPException(status_code=400, detail=str(erreur))

    existantes = db.query(Classe).filter(Classe.createur_username == username).all()
    if len(existantes) >= 10:
        raise HTTPException(status_code=400, detail="Tu as atteint la limite de 10 classes.")
    if any(cle_doublon_classe(c.nom) == cle_doublon_classe(valide["nom"]) for c in existantes):
        raise HTTPException(status_code=400, detail="Tu as déjà une classe avec ce nom.")

    code = secrets.token_urlsafe(8)
    classe = Classe(
        nom=valide["nom"], niveau=valide["niveau"], matiere=valide["matiere"], complement=valide["complement"],
        code_invitation=code, createur_username=username,
    )
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

    if sanction_active(db, classe.createur_username) is not None:
        raise HTTPException(status_code=403, detail="Cette classe est suspendue")

    deja_membre = db.query(ClasseMembre).filter(
        ClasseMembre.classe_id == classe.id,
        ClasseMembre.username == username,
    ).first()
    if deja_membre:
        return {"success": True, "message": "Déjà membre", "classe_id": classe.id}

    nb_membres_actuels = db.query(ClasseMembre).filter(ClasseMembre.classe_id == classe.id).count()
    if nb_membres_actuels >= 10:
        raise HTTPException(status_code=400, detail="Cette classe a déjà atteint sa limite de 10 élèves")

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
            "prof_actuel": classe.createur_username,
            "suspendue": sanction_active(db, classe.createur_username) is not None,
            "niveau": classe.niveau,
            "matiere": classe.matiere,
            "complement": classe.complement,
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

    try:
        if classe.niveau and classe.matiere:
            # Classe structurée : on ne change que le complément ; niveau et matière restent cohérents.
            complement = valider_texte_libre(data.nom, min_len=2, max_len=30, nom="Le complément du nom") if (data.nom or "").strip() else None
            nouveau_nom = composer_nom_classe(classe.matiere, classe.niveau, complement)
        else:
            complement = None
            nouveau_nom = valider_texte_libre(data.nom, min_len=3, max_len=60, nom="Le nom de la classe")
    except ErreurValidation as erreur:
        raise HTTPException(status_code=400, detail=str(erreur))

    autres = db.query(Classe).filter(Classe.createur_username == username, Classe.id != classe.id).all()
    if any(cle_doublon_classe(c.nom) == cle_doublon_classe(nouveau_nom) for c in autres):
        raise HTTPException(status_code=400, detail="Tu as déjà une classe avec ce nom.")

    classe.nom = nouveau_nom
    if classe.niveau and classe.matiere:
        classe.complement = complement
    db.commit()

    return {"success": True, "nom": classe.nom}


@router.patch("/api/classes/{classe_id}/transferer-prof")
async def transferer_prof(classe_id: int, data: ClasseTransfererProf, db: Session = Depends(get_db)):
    """Exécuté par le prof ACTUEL une fois qu'il a accepté une demande
    (l'échange de la demande elle-même se fait en direct, côté websocket —
    ceci ne fait que persister la décision en base)."""
    username = _identite(data.session_token, db)

    classe = db.get(Classe, classe_id)
    if classe is None:
        raise HTTPException(status_code=404, detail="Classe introuvable")
    if classe.createur_username != username:
        raise HTTPException(status_code=403, detail="Seul le prof actuel peut céder sa place")

    nouveau = db.query(ClasseMembre).filter(
        ClasseMembre.classe_id == classe_id, ClasseMembre.username == data.nouveau_prof
    ).first()
    if nouveau is None:
        raise HTTPException(status_code=404, detail="Cette personne n'est pas membre de la classe")

    ancien = db.query(ClasseMembre).filter(
        ClasseMembre.classe_id == classe_id, ClasseMembre.username == username
    ).first()

    classe.createur_username = data.nouveau_prof
    nouveau.role = "prof"
    if ancien:
        ancien.role = "membre"
    db.commit()

    return {"success": True, "nouveau_prof": data.nouveau_prof}


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
