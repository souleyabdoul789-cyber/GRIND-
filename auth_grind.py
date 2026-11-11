"""
auth_grind.py — identité PROPRE à GRIND.

Distinct de pluton_client.py : ici on gère le compte et les sessions
internes à GRIND (username/password choisis dans GRIND, pas ceux de
G-SOCIETY). La clé G-SOCIETY (pluton_client.verifier_cle) n'intervient
qu'une seule fois, à l'inscription, comme preuve que le compte
correspond bien à un utilisateur G-SOCIETY légitime.
"""

import bcrypt
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session

from models import GrindSession
from moderation import sanction_active

DUREE_SESSION_JOURS = 30


def hacher_mot_de_passe(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verifier_mot_de_passe(password: str, hash_stocke: str) -> bool:
    return bcrypt.checkpw(password.encode(), hash_stocke.encode())


def creer_session(username: str, db: Session) -> str:
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expiration = datetime.now(timezone.utc) + timedelta(days=DUREE_SESSION_JOURS)

    # Ménage : sessions expirées de ce compte, et au plus 10 appareils connectés (les plus anciens sont déconnectés).
    maintenant = datetime.now(timezone.utc)
    db.query(GrindSession).filter(GrindSession.username == username, GrindSession.expires_at < maintenant).delete()
    actives = (
        db.query(GrindSession).filter(GrindSession.username == username).order_by(GrindSession.created_at.asc()).all()
    )
    for ancienne in actives[: max(0, len(actives) - 9)]:
        db.delete(ancienne)

    db.add(GrindSession(token_hash=token_hash, username=username, expires_at=expiration))
    db.commit()
    return token


def supprimer_session(token: str, db: Session) -> None:
    """Déconnexion RÉELLE : la session disparaît côté serveur (le jeton ne sert plus à rien, même volé)."""
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    db.query(GrindSession).filter(GrindSession.token_hash == token_hash).delete()
    db.commit()


def verifier_session(token: str, db: Session) -> str | None:
    """Renvoie le username GRIND propriétaire de ce token, ou None si
    invalide/expiré. Vérification locale, pas d'appel réseau à Pluton —
    c'est justement le but : Pluton n'est sollicité qu'à l'inscription."""
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    session = db.query(GrindSession).filter(
        GrindSession.token_hash == token_hash,
        GrindSession.expires_at > datetime.now(timezone.utc),
    ).first()
    if session is None:
        return None
    # Un compte suspendu ou banni n'a plus accès à rien : sa session ne vaut plus rien.
    # (Le message précis lui est donné quand il essaie de se reconnecter.)
    if sanction_active(db, session.username) is not None:
        return None
    return session.username
