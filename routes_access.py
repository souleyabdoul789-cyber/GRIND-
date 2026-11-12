"""Accueil première connexion et contrôle administratif des pays."""
import os
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from database import get_db
from auth_grind import verifier_session
from models import CompteGrind, CountryAccessBlock
from moderation import role_de, ROLE_ADMIN

router = APIRouter()


def detecter_pays(request: Request) -> str | None:
    # Un en-tête de pays n'est accepté que si l'admin l'a explicitement déclaré fiable.
    if os.getenv("GRIND_TRUST_COUNTRY_HEADER") == "1":
        code = (request.headers.get("cf-ipcountry") or request.headers.get("x-country-code") or "").upper()
        if len(code) == 2 and code.isalpha() and code != "XX":
            return code
    db_path = os.getenv("GEOIP_DB_PATH")
    if not db_path or not Path(db_path).is_file():
        return None
    try:
        import geoip2.database
        ip = request.client.host if request.client else None
        # Render/proxy: les en-têtes IP ne sont considérés fiables que si la
        # configuration indique explicitement que l'application est derrière
        # un proxy maîtrisé. Le premier élément X-Forwarded-For est l'IP source.
        if os.getenv("GRIND_TRUST_PROXY_HEADERS") == "1":
            ip = request.headers.get("cf-connecting-ip") or request.headers.get("x-real-ip") or (request.headers.get("x-forwarded-for", "").split(",")[0].strip() or ip)
        if not ip:
            return None
        with geoip2.database.Reader(db_path) as reader:
            return reader.country(ip).country.iso_code
    except Exception:
        return None


def _admin(token: str, db: Session) -> str:
    username = verifier_session(token, db)
    if not username or role_de(db, username) != ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="Réservé à l'administrateur GRIND.")
    return username


@router.get("/api/country-access")
async def verifier_acces_pays(request: Request, db: Session = Depends(get_db)):
    code = detecter_pays(request)
    bloc = db.query(CountryAccessBlock).filter_by(code=code, actif=1).first() if code else None
    if not bloc:
        return {"allowed": True, "country_code": code}
    return {"allowed": False, "country_code": code, "country_name": bloc.nom,
            "message": bloc.message or "GRIND est temporairement indisponible dans ce pays.",
            "brand": "GRIND"}


@router.get("/api/admin/countries")
async def lister_pays_bloques(session_token: str, db: Session = Depends(get_db)):
    _admin(session_token, db)
    rows = db.query(CountryAccessBlock).order_by(CountryAccessBlock.nom.asc()).all()
    return {"pays": [{"code":x.code,"nom":x.nom,"message":x.message,"actif":bool(x.actif)} for x in rows]}


@router.post("/api/admin/countries")
async def definir_pays_bloque(data: dict, db: Session = Depends(get_db)):
    admin = _admin(data.get("session_token", ""), db)
    code = (data.get("code") or "").strip().upper()
    nom = (data.get("nom") or "").strip()
    message = (data.get("message") or "").strip()
    actif = bool(data.get("actif", True))
    if not (len(code) == 2 and code.isalpha()) or not nom or len(nom) > 100:
        raise HTTPException(status_code=400, detail="Indique un code pays ISO à 2 lettres et un nom valide.")
    bloc = db.get(CountryAccessBlock, code)
    if bloc is None:
        bloc = CountryAccessBlock(code=code, nom=nom, message=message[:1200] or None, actif=1 if actif else 0, modifie_par=admin)
        db.add(bloc)
    else:
        bloc.nom=nom; bloc.message=message[:1200] or None; bloc.actif=1 if actif else 0; bloc.modifie_par=admin
    db.commit()
    return {"success":True,"code":code,"nom":nom,"actif":actif}


@router.delete("/api/admin/countries/{code}")
async def supprimer_pays_bloque(code: str, session_token: str, db: Session = Depends(get_db)):
    _admin(session_token, db)
    bloc = db.get(CountryAccessBlock, code.upper())
    if bloc:
        db.delete(bloc); db.commit()
    return {"success":True}


@router.get("/api/compte/bienvenue")
async def statut_bienvenue(session_token: str, db: Session = Depends(get_db)):
    username = verifier_session(session_token, db, autoriser_proviseur=True)
    if not username:
        raise HTTPException(status_code=401, detail="Session GRIND invalide.")
    compte = db.get(CompteGrind, username)
    return {"a_afficher": bool(compte and compte.bienvenue_vue == 0)}


@router.post("/api/compte/bienvenue/terminer")
async def terminer_bienvenue(data: dict, db: Session = Depends(get_db)):
    username = verifier_session(data.get("session_token", ""), db, autoriser_proviseur=True)
    if not username:
        raise HTTPException(status_code=401, detail="Session GRIND invalide.")
    compte = db.get(CompteGrind, username)
    if compte:
        compte.bienvenue_vue = 1
        db.commit()
    return {"success":True}
