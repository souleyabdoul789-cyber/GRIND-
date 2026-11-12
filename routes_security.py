"""Sécurité de session : PIN 48 h, verrouillage anti-bruteforce et comptes mémorisés."""
import hashlib
import re
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Request
from sqlalchemy.orm import Session
from database import get_db
from auth_grind import verifier_session, verifier_mot_de_passe, hacher_mot_de_passe
from models import CompteGrind, GrindSession, GrindDeviceAccount, VerificationEmail
from schemas import PinRequest, PinResetRequest, PinResetEmailStart, PinResetEmailFinish, DeviceRememberRequest, DeviceForgetRequest, SessionRequest
import verification as vf
import mailer
from limiteur import autoriser

router = APIRouter()
DUREE_PIN_HEURES = 48
NB_ESSAIS_PIN = 5
DUREE_BLOCAGE_MINUTES = 15


def _session(data_token, db, ignorer_pin=False):
    username = verifier_session(data_token, db, autoriser_proviseur=True, ignorer_pin=ignorer_pin)
    if not username:
        raise HTTPException(status_code=401, detail="Session GRIND invalide ou expirée.")
    return username


def _pin_valide(pin):
    return bool(re.fullmatch(r"\d{4}", pin or ""))


def _hash_device(device_id):
    if not isinstance(device_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{24,100}", device_id):
        raise HTTPException(status_code=400, detail="Identifiant d'appareil invalide.")
    return hashlib.sha256(device_id.encode()).hexdigest()


@router.post("/api/security/pin/status")
async def pin_status(data: SessionRequest, db: Session = Depends(get_db)):
    username = _session(data.session_token, db, ignorer_pin=True)
    compte = db.get(CompteGrind, username)
    session = db.query(GrindSession).filter_by(token_hash=hashlib.sha256(data.session_token.encode()).hexdigest()).first()
    if compte.pin_hash is None:
        return {"configured": False, "required": True, "remaining_hours": 0}
    checked = session.pin_verifie_le
    if checked and checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    remaining_seconds = max(0, int((checked + timedelta(hours=DUREE_PIN_HEURES) - datetime.now(timezone.utc)).total_seconds())) if checked else 0
    remaining = remaining_seconds // 3600
    return {"configured": True, "required": checked is None or remaining_seconds <= 0,
            "remaining_hours": remaining, "remaining_seconds": remaining_seconds,
            "locked_until": compte.pin_bloque_jusqua.isoformat() if compte.pin_bloque_jusqua else None}


@router.post("/api/security/pin/configurer")
async def pin_configurer(data: PinRequest, db: Session = Depends(get_db)):
    username = _session(data.session_token, db, ignorer_pin=True)
    if not _pin_valide(data.pin):
        raise HTTPException(status_code=400, detail="Le PIN doit contenir exactement 4 chiffres.")
    compte = db.get(CompteGrind, username)
    if compte.pin_hash:
        raise HTTPException(status_code=409, detail="Un PIN existe déjà. Utilise la procédure de réinitialisation.")
    session = db.query(GrindSession).filter_by(token_hash=hashlib.sha256(data.session_token.encode()).hexdigest()).first()
    compte.pin_hash = hacher_mot_de_passe(data.pin)
    compte.pin_echecs = 0
    compte.pin_bloque_jusqua = None
    session.pin_verifie_le = datetime.now(timezone.utc)
    db.commit()
    return {"success": True, "required": False}


@router.post("/api/security/pin/verifier")
async def pin_verifier(data: PinRequest, db: Session = Depends(get_db)):
    username = _session(data.session_token, db, ignorer_pin=True)
    if not _pin_valide(data.pin):
        raise HTTPException(status_code=400, detail="Le PIN doit contenir exactement 4 chiffres.")
    compte = db.get(CompteGrind, username)
    if not compte.pin_hash:
        raise HTTPException(status_code=409, detail="Aucun PIN n'est configuré.")
    now = datetime.now(timezone.utc)
    blocked = compte.pin_bloque_jusqua
    if blocked and blocked.tzinfo is None:
        blocked = blocked.replace(tzinfo=timezone.utc)
    if blocked and blocked > now:
        raise HTTPException(status_code=429, detail="Trop de tentatives. Réessaie après la période de verrouillage.")
    if not verifier_mot_de_passe(data.pin, compte.pin_hash):
        compte.pin_echecs = (compte.pin_echecs or 0) + 1
        remaining = max(0, NB_ESSAIS_PIN - compte.pin_echecs)
        if compte.pin_echecs >= NB_ESSAIS_PIN:
            compte.pin_bloque_jusqua = now + timedelta(minutes=DUREE_BLOCAGE_MINUTES)
            compte.pin_echecs = 0
        db.commit()
        raise HTTPException(status_code=401, detail="PIN incorrect." if remaining else "Trop de tentatives. PIN temporairement verrouillé.")
    compte.pin_echecs = 0
    compte.pin_bloque_jusqua = None
    session = db.query(GrindSession).filter_by(token_hash=hashlib.sha256(data.session_token.encode()).hexdigest()).first()
    session.pin_verifie_le = now
    db.commit()
    return {"success": True, "pin_valide_le": now.isoformat()}


@router.post("/api/security/pin/reinitialiser")
async def pin_reinitialiser(data: PinResetRequest, db: Session = Depends(get_db)):
    username = _session(data.session_token, db, ignorer_pin=True)
    if not _pin_valide(data.nouveau_pin):
        raise HTTPException(status_code=400, detail="Le nouveau PIN doit contenir exactement 4 chiffres.")
    compte = db.get(CompteGrind, username)
    if not verifier_mot_de_passe(data.password, compte.password_hash):
        raise HTTPException(status_code=401, detail="Mot de passe incorrect.")
    compte.pin_hash = hacher_mot_de_passe(data.nouveau_pin)
    compte.pin_echecs = 0
    compte.pin_bloque_jusqua = None
    now = datetime.now(timezone.utc)
    db.query(GrindSession).filter(GrindSession.username == username).update({GrindSession.pin_verifie_le: None})
    current_session = db.query(GrindSession).filter_by(token_hash=hashlib.sha256(data.session_token.encode()).hexdigest()).first()
    if current_session:
        current_session.pin_verifie_le = now
    db.commit()
    return {"success": True}


@router.post("/api/security/pin/reinitialiser/demander")
async def pin_reinitialiser_demander(data: PinResetEmailStart, request: Request, taches: BackgroundTasks, db: Session = Depends(get_db)):
    username = _session(data.session_token, db, ignorer_pin=True)
    compte = db.get(CompteGrind, username)
    # Les comptes de direction n'ont volontairement pas d'e-mail : ils réauthentifient leur mot de passe.
    from models_proviseur import ProviseurCandidature
    if db.query(ProviseurCandidature).filter_by(username=username).first():
        raise HTTPException(status_code=409, detail="Ce compte de direction doit réinitialiser son PIN avec son mot de passe.")
    if not compte.email or compte.email_verifie != 1:
        raise HTTPException(status_code=409, detail="Aucune adresse e-mail vérifiée n'est associée à ce compte.")
    if not mailer.configuration_ok():
        raise HTTPException(status_code=503, detail="La récupération par e-mail est temporairement indisponible.")
    ip = request.client.host if request.client else "inconnu"
    if not autoriser(f"pin-reset:{username}", 3, 3600) or not autoriser(f"pin-reset-ip:{ip}", 10, 3600):
        raise HTTPException(status_code=429, detail="Trop de demandes. Réessaie plus tard.")
    flux, jeton = vf.creer_flux(db, VerificationEmail, "pin_reinitialisation", compte.email, username, ip)
    _, code = vf.preparer_envoi(flux, vf.maintenant())
    db.commit()
    from routes_compte import _envoyer_code
    taches.add_task(_envoyer_code, compte.email, code, "reinitialisation")
    from validation import masquer_email
    return {"verification_requise": True, "email_masque": masquer_email(compte.email), "jeton_verification": jeton}


@router.post("/api/security/pin/reinitialiser/email")
async def pin_reinitialiser_email(data: PinResetEmailFinish, db: Session = Depends(get_db)):
    username = _session(data.session_token, db, ignorer_pin=True)
    if not _pin_valide(data.nouveau_pin):
        raise HTTPException(status_code=400, detail="Le nouveau PIN doit contenir exactement 4 chiffres.")
    flux = vf.trouver_flux(db, VerificationEmail, data.jeton_verification)
    if flux is None or flux.username != username or not vf.reinit_autorisee(flux, vf.maintenant()):
        raise HTTPException(status_code=400, detail="Le code e-mail n'a pas été validé ou le délai de récupération a expiré.")
    compte = db.get(CompteGrind, username)
    compte.pin_hash = hacher_mot_de_passe(data.nouveau_pin)
    compte.pin_echecs = 0
    compte.pin_bloque_jusqua = None
    now = datetime.now(timezone.utc)
    db.query(GrindSession).filter(GrindSession.username == username).update({GrindSession.pin_verifie_le: None})
    current = db.query(GrindSession).filter_by(token_hash=hashlib.sha256(data.session_token.encode()).hexdigest()).first()
    if current:
        current.pin_verifie_le = now
    flux.utilise_le = now
    db.commit()
    return {"success": True}


@router.post("/api/compte/appareils/associer")
async def appareil_associer(data: DeviceRememberRequest, db: Session = Depends(get_db)):
    username = _session(data.session_token, db)
    device_hash = _hash_device(data.device_id)
    token_hash = hashlib.sha256(data.session_token.encode()).hexdigest()
    existing = db.query(GrindDeviceAccount).filter_by(device_hash=device_hash, username=username).first()
    compte = db.get(CompteGrind, username)
    premium_until = compte.premium_until
    if premium_until and premium_until.tzinfo is None:
        premium_until = premium_until.replace(tzinfo=timezone.utc)
    max_accounts = 4 if premium_until and premium_until > datetime.now(timezone.utc) else 2
    accounts = db.query(GrindDeviceAccount).filter_by(device_hash=device_hash).all()
    # Ne comptabilise que des sessions encore valides ; purge les associations orphelines.
    now = datetime.now(timezone.utc)
    actives = []
    for row in accounts:
        session = db.query(GrindSession).filter_by(token_hash=row.token_hash, username=row.username).first()
        expiry = session.expires_at if session else None
        if expiry and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if session and expiry > now:
            actives.append(row)
        else:
            db.delete(row)
    accounts = actives
    if existing is not None and existing not in accounts:
        existing = None
    if existing is None and len(accounts) >= max_accounts:
        raise HTTPException(status_code=409, detail=f"Limite de {max_accounts} comptes mémorisés sur cet appareil atteinte.")
    if existing is None:
        db.add(GrindDeviceAccount(device_hash=device_hash, username=username, token_hash=token_hash))
    else:
        existing.token_hash = token_hash
        existing.last_used_at = datetime.now(timezone.utc)
    db.commit()
    return {"success": True, "max_comptes": max_accounts, "comptes_memorises": len(accounts) + (1 if existing is None else 0)}


@router.post("/api/compte/appareils/oublier")
async def appareil_oublier(data: DeviceForgetRequest, db: Session = Depends(get_db)):
    current = _session(data.session_token, db)
    if current != data.username:
        raise HTTPException(status_code=403, detail="Tu ne peux retirer qu'un compte de cet appareil après authentification sur ce compte.")
    device_hash = _hash_device(data.device_id)
    row = db.query(GrindDeviceAccount).filter_by(device_hash=device_hash, username=current).first()
    if row is None:
        return {"success": True}
    db.query(GrindSession).filter(GrindSession.token_hash == row.token_hash, GrindSession.username == current).delete()
    db.delete(row)
    db.commit()
    return {"success": True}

@router.post("/api/security/pin/relocker")
async def pin_relocker(data: SessionRequest, db: Session = Depends(get_db)):
    """Force une nouvelle saisie du PIN lors d'un changement de compte sur le même appareil."""
    username = _session(data.session_token, db, ignorer_pin=True)
    session = db.query(GrindSession).filter_by(token_hash=hashlib.sha256(data.session_token.encode()).hexdigest()).first()
    if session:
        session.pin_verifie_le = None
        db.commit()
    return {"success": True}
