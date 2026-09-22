import os
from dotenv import load_dotenv

load_dotenv()  # charge le fichier .env AVANT tout le reste — doit rester la 1ère ligne utile

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from database import Base, engine, get_db
from fastapi import Depends
from sqlalchemy.orm import Session
from auth_grind import verifier_session
from schemas import SessionRequest
from routes_compte import router as router_compte
from routes_profil_classes import router as router_profil_classes
from routes_aide import router as router_aide
from routes_posts import router as router_posts
from websocket_routes import router as router_ws

METERED_TURN_USERNAME = os.getenv("METERED_TURN_USERNAME")
METERED_TURN_CREDENTIAL = os.getenv("METERED_TURN_CREDENTIAL")

app = FastAPI(title="GRIND")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # à restreindre au domaine du frontend GRIND une fois connu
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router_compte)
app.include_router(router_profil_classes)
app.include_router(router_aide)
app.include_router(router_posts)
app.include_router(router_ws)


@app.on_event("startup")
def creer_tables():
    Base.metadata.create_all(bind=engine)


@app.get("/")
async def racine():
    return {"status": "GRIND en ligne"}


@app.post("/api/check-access")
async def check_access(data: SessionRequest, db: Session = Depends(get_db)):
    username = verifier_session(data.session_token, db)
    if username is None:
        raise HTTPException(status_code=401, detail="Session GRIND invalide ou expirée")
    return {"success": True, "username": username}


@app.post("/api/turn-credentials")
async def turn_credentials(data: SessionRequest, db: Session = Depends(get_db)):
    username = verifier_session(data.session_token, db)
    if username is None:
        raise HTTPException(status_code=401, detail="Session GRIND invalide ou expirée")

    if not METERED_TURN_USERNAME or not METERED_TURN_CREDENTIAL:
        raise HTTPException(status_code=500, detail="Identifiants TURN non configurés côté serveur")

    return {
        "iceServers": [
            {"urls": "stun:stun.relay.metered.ca:80"},
            {
                "urls": "turn:standard.relay.metered.ca:80",
                "username": METERED_TURN_USERNAME,
                "credential": METERED_TURN_CREDENTIAL,
            },
            {
                "urls": "turn:standard.relay.metered.ca:443?transport=tcp",
                "username": METERED_TURN_USERNAME,
                "credential": METERED_TURN_CREDENTIAL,
            },
        ]
    }
