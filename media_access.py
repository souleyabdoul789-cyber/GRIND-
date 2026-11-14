"""Tickets média à durée limitée : le token de session ne passe plus dans les URL média."""
from __future__ import annotations

import hashlib
import secrets
import time
import threading
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException
from sqlalchemy.orm import Session

from models import GrindMediaAccessTicket

DUREE_TICKET_SECONDES = 3600
_DERNIER_NETTOYAGE = 0.0
_VERROU_NETTOYAGE = threading.Lock()


def _nettoyer_tickets_expires(db: Session, maintenant: datetime) -> None:
    global _DERNIER_NETTOYAGE
    instant = time.monotonic()
    if instant - _DERNIER_NETTOYAGE < 300:
        return
    with _VERROU_NETTOYAGE:
        instant = time.monotonic()
        if instant - _DERNIER_NETTOYAGE >= 300:
            db.query(GrindMediaAccessTicket).filter(
                GrindMediaAccessTicket.expires_at <= maintenant
            ).delete(synchronize_session=False)
            _DERNIER_NETTOYAGE = instant

def creer_ticket_media(db: Session, *, username: str, resource_type: str, resource_id: int,
                       container_id: int | None = None, purpose: str, duree: int = DUREE_TICKET_SECONDES) -> str:
    """Crée un bearer token aléatoire ; seule son empreinte SHA-256 est persistée."""
    maintenant = datetime.now(timezone.utc)
    _nettoyer_tickets_expires(db, maintenant)
    token = secrets.token_urlsafe(32)
    db.add(GrindMediaAccessTicket(
        token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
        username=username, resource_type=resource_type, resource_id=int(resource_id),
        container_id=int(container_id) if container_id is not None else None,
        purpose=purpose, expires_at=maintenant + timedelta(seconds=max(60, min(int(duree), 7200))),
    ))
    return token

def verifier_ticket_media(db: Session, token: str | None, *, resource_type: str, resource_id: int,
                          container_id: int | None = None, purpose: str) -> GrindMediaAccessTicket:
    if not token or len(token) > 128:
        raise HTTPException(status_code=401, detail="Lien média invalide ou expiré.")
    empreinte = hashlib.sha256(token.encode("utf-8")).hexdigest()
    ticket = db.query(GrindMediaAccessTicket).filter(
        GrindMediaAccessTicket.token_hash == empreinte,
        GrindMediaAccessTicket.resource_type == resource_type,
        GrindMediaAccessTicket.resource_id == int(resource_id),
        GrindMediaAccessTicket.purpose == purpose,
        GrindMediaAccessTicket.expires_at > datetime.now(timezone.utc),
    ).first()
    if ticket is None or (container_id is not None and ticket.container_id != int(container_id)):
        raise HTTPException(status_code=401, detail="Lien média invalide ou expiré.")
    return ticket
