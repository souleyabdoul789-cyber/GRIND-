"""
auth_grind.py — identité propre à GRIND.

Les comptes, mots de passe et sessions sont gérés localement par GRIND.
Aucun appel à G-SOCIETY/Pluton n'est nécessaire pour se connecter ou
maintenir une session.
"""

import bcrypt
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session

from models import GrindSession, CompteGrind
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


def verifier_session(token: str, db: Session, autoriser_proviseur: bool = False, ignorer_pin: bool = False) -> str | None:
    """Renvoie le username GRIND propriétaire de ce token, ou None si
    invalide/expiré. La vérification est locale et ne dépend d'aucun service externe."""
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    session = db.query(GrindSession).filter(
        GrindSession.token_hash == token_hash,
        GrindSession.expires_at > datetime.now(timezone.utc),
    ).first()
    if session is None:
        return None
    compte = db.get(CompteGrind, session.username)
    if compte is None:
        return None
    # Un PIN configuré est exigé à la connexion et au moins toutes les 48 h.
    # Les seules routes qui ignorent cette barrière sont les routes PIN elles-mêmes.
    if compte.pin_hash and not ignorer_pin:
        pin_valide_le = session.pin_verifie_le
        if pin_valide_le is None:
            return None
        pin_valide_le = pin_valide_le if pin_valide_le.tzinfo else pin_valide_le.replace(tzinfo=timezone.utc)
        if pin_valide_le + timedelta(hours=48) <= datetime.now(timezone.utc):
            return None
    # Un compte suspendu ou banni n'a plus accès à rien : sa session ne vaut plus rien.
    # (Le message précis lui est donné quand il essaie de se reconnecter.)
    if sanction_active(db, session.username) is not None:
        return None
    # Un compte candidat/proviseur reste isolé de l'API élève tant que le
    # parcours institutionnel (publication cloisonnée et permissions) n'est pas appelé.
    # Seules les routes dédiées proviseur passent explicitement autoriser_proviseur=True.
    if not autoriser_proviseur:
        from models_proviseur import ProviseurCandidature
        candidature = db.query(ProviseurCandidature).filter_by(username=session.username).first()
        if candidature is not None:
            return None
    return session.username
