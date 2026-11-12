"""État initial sécurisé de la salle de classe virtuelle."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from auth_grind import verifier_session
from database import get_db
from models import (
    Classe, ClasseMembre, ClasseFeuille, ClasseFeuillePermission, ClasseTableau,
)
from classe_salle_service import assurer_prof_et_sieges

router = APIRouter()


def _auth_et_membre(classe_id: int, session_token: str, db: Session):
    username = verifier_session(session_token, db)
    if username is None:
        raise HTTPException(status_code=401, detail="Session GRIND invalide ou expirée")
    classe = db.get(Classe, classe_id)
    if classe is None:
        raise HTTPException(status_code=404, detail="Classe introuvable")
    membres = assurer_prof_et_sieges(db, classe)
    membre = next((m for m in membres if m.username == username), None)
    if membre is None:
        db.rollback()
        raise HTTPException(status_code=403, detail="Tu ne fais pas partie de cette classe")
    return username, classe, membre, membres


@router.get("/api/classes/{classe_id}/salle/etat")
async def obtenir_etat_salle(classe_id: int, session_token: str, db: Session = Depends(get_db)):
    """Retourne uniquement la feuille personnelle et les feuilles explicitement partagées."""
    username, classe, membre, membres = _auth_et_membre(classe_id, session_token, db)

    feuille = db.query(ClasseFeuille).filter_by(classe_id=classe_id, username=username).first()
    if feuille is None:
        feuille = ClasseFeuille(classe_id=classe_id, username=username, contenu="")
        db.add(feuille)

    tableau = db.get(ClasseTableau, classe_id)
    if tableau is None:
        tableau = ClasseTableau(classe_id=classe_id, contenu="", updated_by=classe.prof_username or classe.createur_username)
        db.add(tableau)

    permissions_donnees = db.query(ClasseFeuillePermission).filter_by(
        classe_id=classe_id, proprietaire_username=username
    ).all()
    viewers = [p.lecteur_username for p in permissions_donnees]

    permissions_recues = db.query(ClasseFeuillePermission).filter_by(
        classe_id=classe_id, lecteur_username=username
    ).all()
    feuilles_partagees = {}
    for permission in permissions_recues:
        contenu = db.query(ClasseFeuille.contenu).filter_by(
            classe_id=classe_id, username=permission.proprietaire_username
        ).scalar()
        feuilles_partagees[permission.proprietaire_username] = contenu or ""

    db.commit()
    return {
        "classe_id": classe_id,
        "owner_username": classe.createur_username,
        "prof_username": classe.prof_username or classe.createur_username,
        "est_owner": username == classe.createur_username,
        "est_prof": username == (classe.prof_username or classe.createur_username),
        "ma_place": membre.siege_index,
        "membres": [
            {"username": m.username, "role": m.role, "siege_index": m.siege_index, "micro_autorise": m.micro_autorise is None or bool(m.micro_autorise)}
            for m in membres
        ],
        "ma_feuille": feuille.contenu or "",
        "mes_viewers": viewers,
        "feuilles_autorisees": feuilles_partagees,
        "tableau": tableau.contenu or "",
    }
