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
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from auth_grind import verifier_session
from models import ClasseMembre

router = APIRouter()

LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET")
LIVEKIT_URL = os.getenv("LIVEKIT_URL")

DUREE_JETON_SECONDES = 3600


class LiveKitTokenRequest(BaseModel):
    session_token: str
    classe_id: int


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
            "canPublish": True,
            "canSubscribe": True,
            "canPublishData": True,
        },
    }
    jeton = jwt.encode(charge, LIVEKIT_API_SECRET, algorithm="HS256")

    return {"token": jeton, "url": LIVEKIT_URL, "room": salle}
