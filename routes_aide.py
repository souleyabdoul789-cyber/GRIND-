from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from auth_grind import verifier_session
from schemas import DemandeAideCreate, AideAccepter
from models import DemandeAide, AideParticipant
from ws_manager import manager
from push_notifications import envoyer_push_a_utilisateur, envoyer_push_aux_utilisateurs
from matching import trouver_meilleurs_candidats
from scolaire import ErreurValidation, valider_matiere

router = APIRouter()


def _identite(session_token: str, db: Session) -> str:
    username = verifier_session(session_token, db)
    if username is None:
        raise HTTPException(status_code=401, detail="Session GRIND invalide ou expirée — reconnecte-toi")
    return username


@router.post("/api/aide/demander")
async def demander_aide(data: DemandeAideCreate, db: Session = Depends(get_db)):
    username = _identite(data.session_token, db)

    try:
        sujet = valider_matiere(data.sujet)  # matière du catalogue, normalisée (pas de texte absurde)
    except ErreurValidation as erreur:
        raise HTTPException(status_code=400, detail=str(erreur))

    demande = DemandeAide(
        demandeur_username=username,
        sujet=sujet,
        description=data.description,
    )
    db.add(demande)
    db.commit()
    db.refresh(demande)

    candidats = trouver_meilleurs_candidats(db, username, sujet)
    await manager.notifier_nouvelle_demande(demande.id, demande.sujet, username, candidats)
    await envoyer_push_aux_utilisateurs(
        db, candidats, "Nouvelle demande d'aide GRIND", f"Une demande vient d'être publiée en {demande.sujet}.",
        {"event_type": "nouvelle_demande", "demande_id": demande.id},
    )

    return {"success": True, "demande_id": demande.id, "candidats_notifies": len(candidats)}


@router.get("/api/aide/ouvertes")
async def lister_demandes_ouvertes(db: Session = Depends(get_db)):
    demandes = db.query(DemandeAide).filter(DemandeAide.statut == "ouverte").all()
    return {
        "demandes": [
            {"id": d.id, "sujet": d.sujet, "description": d.description, "demandeur": d.demandeur_username}
            for d in demandes
        ]
    }


@router.post("/api/aide/accepter")
async def accepter_demande(data: AideAccepter, db: Session = Depends(get_db)):
    username = _identite(data.session_token, db)

    demande = db.get(DemandeAide, data.demande_id)
    if demande is None:
        raise HTTPException(status_code=404, detail="Demande introuvable")

    deja_prof = db.query(AideParticipant).filter(
        AideParticipant.demande_id == data.demande_id,
        AideParticipant.role == "prof",
    ).first()
    role = "aidant" if deja_prof else "prof"

    db.add(AideParticipant(demande_id=data.demande_id, username=username, role=role))
    demande.statut = "en_cours"
    db.commit()

    await manager.notifier_participant_rejoint(data.demande_id, username, role, demande.demandeur_username)
    await envoyer_push_a_utilisateur(
        db, demande.demandeur_username, "Un membre a rejoint ta demande d'aide",
        f"{username} a rejoint ta demande en {demande.sujet}.",
        {"event_type": "participant_rejoint", "demande_id": demande.id},
    )

    return {"success": True, "role": role}
