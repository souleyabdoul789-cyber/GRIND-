"""Enregistrement sécurisé des appareils Android pour les notifications FCM."""
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from auth_grind import verifier_session
from database import get_db
from models import GrindPushToken

router = APIRouter()


def _username(token, db):
    username = verifier_session(token or "", db, autoriser_proviseur=True)
    if not username:
        raise HTTPException(status_code=401, detail="Session GRIND invalide ou expirée.")
    return username


@router.post("/api/push/register")
async def enregistrer_appareil(data: dict, db: Session = Depends(get_db)):
    username = _username(data.get("session_token"), db)
    token = (data.get("token") or "").strip()
    platform = (data.get("platform") or "android").strip().lower()
    if len(token) < 20 or len(token) > 512:
        raise HTTPException(status_code=400, detail="Jeton de notification invalide.")
    if platform != "android":
        raise HTTPException(status_code=400, detail="Plateforme non prise en charge.")
    row = db.query(GrindPushToken).filter_by(token=token).first()
    if row:
        row.username = username
        row.platform = platform
        row.updated_at = datetime.now(timezone.utc)
    else:
        db.add(GrindPushToken(token=token, username=username, platform=platform))
    db.commit()
    return {"success": True}


@router.post("/api/push/unregister")
async def retirer_appareil(data: dict, db: Session = Depends(get_db)):
    username = _username(data.get("session_token"), db)
    token = (data.get("token") or "").strip()
    if token:
        db.query(GrindPushToken).filter_by(token=token, username=username).delete(synchronize_session=False)
        db.commit()
    return {"success": True}
