"""État initial sécurisé de la salle de classe virtuelle."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from auth_grind import verifier_session
from database import get_db
from models import (
    Classe, ClasseMembre, ClasseFeuille, ClasseFeuillePermission, ClasseTableau,
)
from classe_salle_service import assurer_prof_et_sieges
from classe_salle_etendue import (
    ClasseFeuilleVersion, ClasseTableauVersion, ClasseTableauDelegation,
    delegation_tableau_active, eleve_connecte_dans_salle, utc_naive,
)
from ws_manager import manager

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

    tableau_delegation = None
    tableau_invitation = None
    delegation = db.query(ClasseTableauDelegation).filter_by(
        classe_id=classe_id
    ).with_for_update().first()
    if delegation is not None:
        professeur_actuel = classe.prof_username or classe.createur_username
        expiration = delegation.invitation_expire_at if delegation.statut == "invite" else delegation.expire_at
        expiration = utc_naive(expiration) if expiration else None
        professeur_connecte = eleve_connecte_dans_salle(
            manager.session_connections, -classe_id, professeur_actuel
        )
        if (delegation.prof_username != professeur_actuel or expiration is None
                or expiration <= utc_naive() or not professeur_connecte):
            db.delete(delegation)
        else:
            tableau_delegation = {
                "eleve_username": delegation.eleve_username,
                "prof_username": delegation.prof_username,
                "statut": delegation.statut,
                "duree_secondes": int(delegation.duree_secondes or 180),
                "invitation_expire_at": delegation.invitation_expire_at.isoformat() if delegation.invitation_expire_at else None,
                "expire_at": delegation.expire_at.isoformat() if delegation.expire_at else None,
            }
            if delegation.statut == "invite" and delegation.eleve_username == username:
                tableau_invitation = {
                    "sender": delegation.prof_username,
                    "duree_secondes": int(delegation.duree_secondes or 180),
                    "expire_at": delegation.invitation_expire_at.isoformat() if delegation.invitation_expire_at else None,
                }

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
        "tableau_delegation": tableau_delegation,
        "tableau_invitation": tableau_invitation,
    }


# ---------- Historique des feuilles et du tableau ----------

@router.post("/api/classes/{classe_id}/salle/historique/{type_contenu}/capturer")
async def capturer_historique_salle(
    classe_id: int, type_contenu: str, data: dict, db: Session = Depends(get_db)
):
    """Capture le contenu déjà validé en base après une pause de saisie.

    Le client ne transmet jamais le texte à archiver : la source de vérité est
    la base. Les captures identiques sont dédupliquées et l'historique est borné.
    """
    username, _classe, _membre, _membres = _auth_et_membre(classe_id, data.get("session_token", ""), db)

    if type_contenu == "feuille":
        feuille = db.query(ClasseFeuille).filter_by(classe_id=classe_id, username=username).first()
        contenu = feuille.contenu if feuille else ""
        if not contenu.strip():
            return {"success": True, "captured": False, "reason": "empty"}
        derniere = db.query(ClasseFeuilleVersion).filter_by(
            classe_id=classe_id, username=username
        ).order_by(ClasseFeuilleVersion.id.desc()).first()
        if derniere is not None and derniere.contenu == contenu:
            return {"success": True, "captured": False, "reason": "unchanged"}
        version = ClasseFeuilleVersion(classe_id=classe_id, username=username, contenu=contenu)
        db.add(version)
        db.flush()
        anciennes = db.query(ClasseFeuilleVersion).filter_by(
            classe_id=classe_id, username=username
        ).order_by(ClasseFeuilleVersion.id.desc()).offset(100).all()
        for ancienne in anciennes:
            db.delete(ancienne)
        db.commit()
        return {"success": True, "captured": True, "id": version.id}

    if type_contenu == "tableau":
        # L'archivage ne modifie aucun droit ni contenu : tout membre de la classe
        # peut demander une capture de l'état déjà enregistré, même après la fin
        # de sa délégation. Le texte n'est jamais accepté depuis le client.
        tableau = db.get(ClasseTableau, classe_id)
        contenu = tableau.contenu if tableau else ""
        if not contenu.strip():
            return {"success": True, "captured": False, "reason": "empty"}
        derniere = db.query(ClasseTableauVersion).filter_by(
            classe_id=classe_id
        ).order_by(ClasseTableauVersion.id.desc()).first()
        if derniere is not None and derniere.contenu == contenu:
            return {"success": True, "captured": False, "reason": "unchanged"}
        version = ClasseTableauVersion(classe_id=classe_id, auteur_username=username, contenu=contenu)
        db.add(version)
        db.flush()
        anciennes = db.query(ClasseTableauVersion).filter_by(
            classe_id=classe_id
        ).order_by(ClasseTableauVersion.id.desc()).offset(150).all()
        for ancienne in anciennes:
            db.delete(ancienne)
        db.commit()
        return {"success": True, "captured": True, "id": version.id}

    raise HTTPException(status_code=400, detail="Type d'historique invalide.")


@router.get("/api/classes/{classe_id}/salle/historique/{type_contenu}")
async def lister_historique_salle(
    classe_id: int, type_contenu: str, session_token: str, db: Session = Depends(get_db)
):
    username, _classe, _membre, _membres = _auth_et_membre(classe_id, session_token, db)
    if type_contenu == "feuille":
        versions = db.query(ClasseFeuilleVersion).filter_by(
            classe_id=classe_id, username=username
        ).order_by(ClasseFeuilleVersion.id.desc()).limit(50).all()
        return {"versions": [
            {"id": v.id, "created_at": v.created_at.isoformat() if v.created_at else None,
             "caracteres": len(v.contenu or ""), "auteur": username}
            for v in versions
        ]}
    if type_contenu == "tableau":
        versions = db.query(ClasseTableauVersion).filter_by(
            classe_id=classe_id
        ).order_by(ClasseTableauVersion.id.desc()).limit(50).all()
        return {"versions": [
            {"id": v.id, "created_at": v.created_at.isoformat() if v.created_at else None,
             "caracteres": len(v.contenu or ""), "auteur": v.auteur_username}
            for v in versions
        ]}
    raise HTTPException(status_code=400, detail="Type d'historique invalide.")


@router.get("/api/classes/{classe_id}/salle/historique/{type_contenu}/{version_id}")
async def obtenir_version_historique_salle(
    classe_id: int, type_contenu: str, version_id: int,
    session_token: str, db: Session = Depends(get_db)
):
    username, _classe, _membre, _membres = _auth_et_membre(classe_id, session_token, db)
    if type_contenu == "feuille":
        version = db.query(ClasseFeuilleVersion).filter_by(
            id=version_id, classe_id=classe_id, username=username
        ).first()
    elif type_contenu == "tableau":
        version = db.query(ClasseTableauVersion).filter_by(
            id=version_id, classe_id=classe_id
        ).first()
    else:
        raise HTTPException(status_code=400, detail="Type d'historique invalide.")
    if version is None:
        raise HTTPException(status_code=404, detail="Cette version n'existe pas ou n'est pas accessible.")
    auteur = getattr(version, "auteur_username", None) or getattr(version, "username", None)
    return {
        "id": version.id,
        "created_at": version.created_at.isoformat() if version.created_at else None,
        "auteur": auteur,
        "contenu": version.contenu or "",
    }


@router.post("/api/classes/{classe_id}/salle/historique/{type_contenu}/{version_id}/restaurer")
async def restaurer_version_historique_salle(
    classe_id: int, type_contenu: str, version_id: int,
    data: dict, db: Session = Depends(get_db)
):
    username, classe, _membre, _membres = _auth_et_membre(classe_id, data.get("session_token", ""), db)

    if type_contenu == "feuille":
        version = db.query(ClasseFeuilleVersion).filter_by(
            id=version_id, classe_id=classe_id, username=username
        ).first()
        if version is None:
            raise HTTPException(status_code=404, detail="Cette version de feuille n'est pas accessible.")
        feuille = db.query(ClasseFeuille).filter_by(classe_id=classe_id, username=username).first()
        if feuille is None:
            feuille = ClasseFeuille(classe_id=classe_id, username=username, contenu="")
            db.add(feuille)
        actuel = feuille.contenu or ""
        if actuel != (version.contenu or ""):
            derniere = db.query(ClasseFeuilleVersion).filter_by(
                classe_id=classe_id, username=username
            ).order_by(ClasseFeuilleVersion.id.desc()).first()
            if derniere is None or derniere.contenu != actuel:
                db.add(ClasseFeuilleVersion(classe_id=classe_id, username=username, contenu=actuel))
                db.flush()
                for ancienne in db.query(ClasseFeuilleVersion).filter_by(
                    classe_id=classe_id, username=username
                ).order_by(ClasseFeuilleVersion.id.desc()).offset(100).all():
                    db.delete(ancienne)
        feuille.contenu = version.contenu or ""
        db.commit()
        permissions = db.query(ClasseFeuillePermission.lecteur_username).filter_by(
            classe_id=classe_id, proprietaire_username=username
        ).all()
        payload = {"type": "sheet-update", "sender": username, "contenu": feuille.contenu}
        for (lecteur,) in permissions:
            await manager.envoyer_session(-classe_id, lecteur, payload)
        return {"success": True, "contenu": feuille.contenu}

    if type_contenu == "tableau":
        professeur = classe.prof_username or classe.createur_username
        delegation = db.query(ClasseTableauDelegation).filter_by(
            classe_id=classe_id
        ).with_for_update().first()
        professeur_connecte = eleve_connecte_dans_salle(
            manager.session_connections, -classe_id, professeur
        )
        if username != professeur and not (
            delegation_tableau_active(delegation, username, professeur) and professeur_connecte
        ):
            raise HTTPException(status_code=403, detail="Le tableau ne peut être restauré que par son professeur ou l'élève qui a la main.")
        version = db.query(ClasseTableauVersion).filter_by(
            id=version_id, classe_id=classe_id
        ).first()
        if version is None:
            raise HTTPException(status_code=404, detail="Cette version du tableau est introuvable.")
        tableau = db.get(ClasseTableau, classe_id)
        if tableau is None:
            tableau = ClasseTableau(classe_id=classe_id, contenu="", updated_by=username)
            db.add(tableau)
        actuel = tableau.contenu or ""
        if actuel != (version.contenu or ""):
            derniere = db.query(ClasseTableauVersion).filter_by(
                classe_id=classe_id
            ).order_by(ClasseTableauVersion.id.desc()).first()
            if derniere is None or derniere.contenu != actuel:
                db.add(ClasseTableauVersion(classe_id=classe_id, auteur_username=username, contenu=actuel))
                db.flush()
                for ancienne in db.query(ClasseTableauVersion).filter_by(
                    classe_id=classe_id
                ).order_by(ClasseTableauVersion.id.desc()).offset(150).all():
                    db.delete(ancienne)
        tableau.contenu = version.contenu or ""
        tableau.updated_by = username
        delegation_terminee = None
        if username == professeur and delegation is not None:
            delegation_terminee = delegation.eleve_username
            db.delete(delegation)
        db.commit()
        if delegation_terminee:
            await manager.relay(-classe_id, username, {
                "type": "tableau-delegation-terminee",
                "raison": "reprise_professeur",
                "eleve_username": delegation_terminee,
            })
        await manager.relay(-classe_id, username, {"type": "blackboard-update", "contenu": tableau.contenu})
        return {"success": True, "contenu": tableau.contenu}

    raise HTTPException(status_code=400, detail="Type d'historique invalide.")
