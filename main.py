import os
from dotenv import load_dotenv

load_dotenv()  # charge le fichier .env AVANT tout le reste — doit rester la 1ère ligne utile

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from database import Base, engine, get_db, migrer_colonnes_manquantes, migrer_index_manquants
from fastapi import Depends
from sqlalchemy.orm import Session
from auth_grind import verifier_session
from schemas import SessionRequest
from routes_compte import router as router_compte
from routes_profil_classes import router as router_profil_classes
from routes_aide import router as router_aide
from routes_posts import router as router_posts
from websocket_routes import router as router_ws
from media import router as router_media
from routes_avatar import router as router_avatar
from routes_livekit import router as router_livekit
from routes_classe_salle import router as router_classe_salle
from routes_moderation import router as router_moderation
from routes_support import router as router_support
from routes_proviseur import router as router_proviseur
from routes_security import router as router_security
from routes_library import router as router_library
from routes_access import router as router_access, detecter_pays
from routes_discussion import router as router_discussion
from models import CountryAccessBlock

METERED_TURN_USERNAME = os.getenv("METERED_TURN_USERNAME")
METERED_TURN_CREDENTIAL = os.getenv("METERED_TURN_CREDENTIAL")

app = FastAPI(title="GRIND")
from fastapi.responses import JSONResponse

@app.middleware("http")
async def bloquer_pays_suspendus(request: Request, call_next):
    chemin = request.url.path
    chemins_autorises = {
        "/api/health", "/api/country-access", "/api/compte/login",
        "/api/compte/logout", "/api/moderation/moi",
    }
    acces_securite = chemin.startswith("/api/security/pin/")
    gestion_pays_admin = chemin.startswith("/api/admin/countries")
    if chemin.startswith("/api/") and chemin not in chemins_autorises and not acces_securite and not gestion_pays_admin:
        code = detecter_pays(request)
        if code:
            from database import SessionLocal
            db = SessionLocal()
            try:
                bloc = db.query(CountryAccessBlock).filter_by(code=code, actif=1).first()
                if bloc:
                    return JSONResponse(status_code=451, content={"detail": {"code":"country_unavailable", "message": bloc.message or "GRIND est temporairement indisponible dans ce pays.", "country": bloc.nom}})
            finally:
                db.close()
    return await call_next(request)

# Origines explicites autorisées. L'origine de production actuellement utilisée par
# le frontend est également un défaut sûr : en cas d'oubli de la variable Render,
# toutes les requêtes navigateur ne doivent pas échouer silencieusement en CORS.
_origines_defaut = [
    "https://grind-school-7agz.onrender.com",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]
_origines_env = [
    origine.strip().rstrip("/")
    for origine in os.getenv("GRIND_FRONTEND_ORIGINS", "").split(",")
    if origine.strip()
]
_origines_frontend = list(dict.fromkeys(_origines_defaut + _origines_env))
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origines_frontend,
    # PATCH est utilisé par le renommage de classe et le transfert du professeur.
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "Accept", "X-Requested-With"],
)

app.include_router(router_compte)
app.include_router(router_profil_classes)
app.include_router(router_aide)
app.include_router(router_posts)
app.include_router(router_ws)
app.include_router(router_media)
app.include_router(router_avatar)
app.include_router(router_livekit)
app.include_router(router_classe_salle)
app.include_router(router_moderation)
app.include_router(router_support)
app.include_router(router_proviseur)
app.include_router(router_security)
app.include_router(router_library)
app.include_router(router_access)
app.include_router(router_discussion)


@app.on_event("startup")
def creer_tables():
    Base.metadata.create_all(bind=engine)
    migrer_colonnes_manquantes(Base)
    migrer_index_manquants(Base)


@app.get("/")
async def racine():
    return {"status": "GRIND en ligne"}


@app.get("/api/health")
async def health():
    """Contrôle de disponibilité sans exposer la configuration ni les secrets."""
    return {"status": "ok", "service": "grind-api"}


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
