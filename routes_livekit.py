"""
routes_livekit.py — jetons d'accès pour la vraie salle audio à N personnes.

Notre WebRTC fait main (étape précédente) ne marchait bien qu'à deux —
au-delà, il faudrait que chacun ouvre une connexion séparée vers tous
les autres, ingérable sur un téléphone. LiveKit joue le rôle de relais
central (SFU) : chacun n'ouvre qu'UNE connexion, vers leur serveur, qui
redistribue à tout le monde dans la salle — jusqu'à 10 personnes sans
souci chez nous.

Le principe reste le même que Cloudinary/TURN : le secret ne sort
jamais d'ici, le client reçoit juste un jeton signé, à durée limitée,
qui ne donne accès qu'à LA classe dont il est membre.
"""

import os
import time
import jwt
import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from auth_grind import verifier_session
from models import ClasseMembre, Classe
from moderation import sanction_active
from ws_manager import manager

router = APIRouter()

LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET")
LIVEKIT_URL = os.getenv("LIVEKIT_URL")

DUREE_JETON_SECONDES = 600  # 10 min : limite l’usage d’un ancien jeton après changement de droits


class LiveKitTokenRequest(BaseModel):
    session_token: str
    classe_id: int


class LiveKitModerationRequest(BaseModel):
    session_token: str
    mute: bool


@router.post("/api/livekit/token")
async def obtenir_jeton_livekit(data: LiveKitTokenRequest, db: Session = Depends(get_db)):
    username = verifier_session(data.session_token, db)
    if username is None:
        raise HTTPException(status_code=401, detail="Session GRIND invalide ou expirée")

    # On vérifie que la personne est bien membre de CETTE classe avant de
    # lui donner un accès — sinon n'importe qui avec un compte GRIND
    # pourrait rejoindre l'audio de n'importe quelle classe en devinant l'id.
    est_membre = db.query(ClasseMembre).filter(
        ClasseMembre.classe_id == data.classe_id, ClasseMembre.username == username
    ).first()
    if not est_membre:
        raise HTTPException(status_code=403, detail="Tu n'es pas membre de cette classe")

    classe = db.get(Classe, data.classe_id)
    if classe is not None and sanction_active(db, classe.createur_username) is not None:
        raise HTTPException(status_code=403, detail="Cette classe est suspendue")

    if not all([LIVEKIT_API_KEY, LIVEKIT_API_SECRET, LIVEKIT_URL]):
        raise HTTPException(status_code=500, detail="LiveKit non configuré côté serveur")

    salle = f"classe-{data.classe_id}"
    maintenant = int(time.time())
    charge = {
        "iss": LIVEKIT_API_KEY,
        "sub": username,
        "iat": maintenant,
        "nbf": maintenant,
        "exp": maintenant + DUREE_JETON_SECONDES,
        "name": username,
        "video": {
            "room": salle,
            "roomJoin": True,
            "canPublish": est_membre.micro_autorise is None or bool(est_membre.micro_autorise),
            "canSubscribe": True,
            "canPublishData": True,
        },
    }
    jeton = jwt.encode(charge, LIVEKIT_API_SECRET, algorithm="HS256")

    return {"token": jeton, "url": LIVEKIT_URL, "room": salle}


def _jeton_admin_livekit(salle: str) -> str:
    maintenant = int(time.time())
    charge = {
        "iss": LIVEKIT_API_KEY,
        "sub": "GRIND-CLASS-MODERATION",
        "iat": maintenant,
        "nbf": maintenant,
        "exp": maintenant + 60,
        "video": {"room": salle, "roomAdmin": True},
    }
    return jwt.encode(charge, LIVEKIT_API_SECRET, algorithm="HS256")


@router.post("/api/livekit/classes/{classe_id}/micro/{cible}")
async def moderer_micro_classe(
    classe_id: int, cible: str, data: LiveKitModerationRequest, db: Session = Depends(get_db)
):
    """Coupe/rétablit la publication audio d'un élève via l'API LiveKit serveur."""
    username = verifier_session(data.session_token, db)
    if username is None:
        raise HTTPException(status_code=401, detail="Session GRIND invalide ou expirée")

    classe = db.get(Classe, classe_id)
    if classe is None:
        raise HTTPException(status_code=404, detail="Classe introuvable")
    professeur = classe.prof_username or classe.createur_username
    if username != professeur:
        raise HTTPException(status_code=403, detail="Seul le professeur actuel peut gérer les micros")
    if cible == username:
        raise HTTPException(status_code=400, detail="Le professeur ne peut pas modérer son propre micro")
    membre = db.query(ClasseMembre).filter_by(classe_id=classe_id, username=cible).first()
    if membre is None:
        raise HTTPException(status_code=404, detail="Cet élève ne fait pas partie de la classe")
    if not all([LIVEKIT_API_KEY, LIVEKIT_API_SECRET, LIVEKIT_URL]):
        raise HTTPException(status_code=503, detail="La modération audio LiveKit n'est pas configurée")

    base = LIVEKIT_URL.replace("wss://", "https://", 1).replace("ws://", "http://", 1).rstrip("/")
    endpoint = f"{base}/twirp/livekit.RoomService/UpdateParticipant"
    permission = {
        "canSubscribe": True,
        "canPublish": not data.mute,
        "canPublishData": True,
    }
    payload = {
        "room": f"classe-{classe_id}",
        "identity": cible,
        "permission": permission,
    }
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.post(
                endpoint, json=payload, headers={"Authorization": f"Bearer {_jeton_admin_livekit(f'classe-{classe_id}')}"}
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Impossible de joindre le service audio LiveKit") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="LiveKit n'a pas pu appliquer la modération audio")

    # La restriction doit survivre à une reconnexion : le prochain jeton
    # LiveKit reprendra ce droit depuis la base, au lieu de le réactiver.
    membre.micro_autorise = 0 if data.mute else 1
    db.commit()
    await manager.envoyer_session(-classe_id, cible, {
        "type": "micro-permission", "autorise": not data.mute,
    })
    return {"success": True, "username": cible, "micro_coupe": data.mute}
