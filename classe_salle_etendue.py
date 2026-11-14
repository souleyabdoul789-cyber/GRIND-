"""Historique versionné et délégation temporaire du tableau de classe.

Ces modèles sont séparés de models.py afin de ne pas réécrire le schéma
historique des comptes et des classes. Ils sont importés par les routes avant
Base.metadata.create_all au démarrage.
"""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.sql import func

from database import Base


class ClasseFeuilleVersion(Base):
    __tablename__ = "classe_feuille_versions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    classe_id = Column(Integer, ForeignKey("classes.id", ondelete="CASCADE"), nullable=False, index=True)
    username = Column(String(64), nullable=False, index=True)
    contenu = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)


class ClasseTableauVersion(Base):
    __tablename__ = "classe_tableau_versions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    classe_id = Column(Integer, ForeignKey("classes.id", ondelete="CASCADE"), nullable=False, index=True)
    auteur_username = Column(String(64), nullable=False, index=True)
    contenu = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)


class ClasseTableauDelegation(Base):
    """Une seule invitation/prise de main temporaire par classe."""
    __tablename__ = "classe_tableau_delegations"

    classe_id = Column(Integer, ForeignKey("classes.id", ondelete="CASCADE"), primary_key=True)
    eleve_username = Column(String(64), nullable=False, index=True)
    prof_username = Column(String(64), nullable=False)
    statut = Column(String(16), nullable=False, default="invite")  # invite / actif
    duree_secondes = Column(Integer, nullable=False, default=180)
    invitation_expire_at = Column(DateTime(timezone=True), nullable=True)
    expire_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


def utc_naive(value=None):
    """Normalise les dates SQL en UTC naïf pour compatibilité SQLite/PostgreSQL."""
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def duree_tableau_securisee(value):
    try:
        duration = int(value)
    except (TypeError, ValueError):
        duration = 180
    return max(60, min(duration, 300))


def delegation_tableau_active(delegation, username, professeur, now=None):
    if delegation is None or delegation.statut != "actif":
        return False
    if delegation.eleve_username != username or delegation.prof_username != professeur:
        return False
    expiration = utc_naive(delegation.expire_at) if delegation.expire_at else None
    return expiration is not None and expiration > utc_naive(now)


def eleve_connecte_dans_salle(session_connections, canal_id, username):
    """Vérifie la présence WebSocket, pas seulement l'adhésion en base."""
    if not username or not isinstance(session_connections, dict):
        return False
    return bool(session_connections.get(canal_id, {}).get(username))

def raison_fin_delegation(delegation, username, professeur):
    """Retourne le motif de révocation quand un titulaire quitte le canal."""
    if delegation is None:
        return None
    if username == professeur:
        return "professeur_deconnecte"
    if username == delegation.eleve_username:
        return "eleve_deconnecte"
    return None

