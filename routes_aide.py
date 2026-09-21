from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from pluton_client import verifier_cle
from schemas import DemandeAideCreate, AideAccepter
from models import DemandeAide, AideParticipant
from ws_manager import manager
from matching import trouver_meilleurs_candidats

router = APIRouter()


@router.post("/api/aide/demander")
async def demander_aide(data: DemandeAideCreate, db: Session = Depends(get_db)):
    valide, username = await verifier_cle(data.api_key)
    if not valide:
        raise HTTPException(status_code=401, detail="Clé API invalide ou expirée")

    demande = DemandeAide(
        demandeur_username=username,
        sujet=data.sujet,
        description=data.description,
    )
    db.add(demande)
    db.commit()
    db.refresh(demande)

    candidats = trouver_meilleurs_candidats(db, username, data.sujet)
    await manager.notifier_nouvelle_demande(demande.id, demande.sujet, username, candidats)

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
    valide, username = await verifier_cle(data.api_key)
    if not valide:
        raise HTTPException(status_code=401, detail="Clé API invalide ou expirée")

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

    await manager.notifier_participant_rejoint(data.demande_id, username, role)

    return {"success": True, "role": role}
