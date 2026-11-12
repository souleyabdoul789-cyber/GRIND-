"""Modèles isolés du parcours de candidature et des autorisations des proviseurs."""
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, LargeBinary, UniqueConstraint
from sqlalchemy.sql import func
from sqlalchemy.dialects.mysql import LONGBLOB
from database import Base


class Etablissement(Base):
    __tablename__ = "etablissements_grind"
    id = Column(Integer, primary_key=True, autoincrement=True)
    nom = Column(String(180), nullable=False)
    nom_canonique = Column(String(180), nullable=False, unique=True, index=True)
    cree_par = Column(String(64), nullable=False)
    actif = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ProviseurCandidature(Base):
    __tablename__ = "proviseur_candidatures"
    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(64), ForeignKey("comptes_grind.username"), nullable=False, unique=True, index=True)
    prenom = Column(String(80), nullable=False)
    nom = Column(String(80), nullable=False)
    telephone_e164 = Column(String(16), nullable=False, unique=True, index=True)
    etablissement_declare = Column(String(180), nullable=False)
    statut = Column(String(16), nullable=False, default="en_attente", index=True)
    etablissement_id = Column(Integer, ForeignKey("etablissements_grind.id"), nullable=True)
    logo_url = Column(String(512), nullable=True)
    note_admin = Column(Text, nullable=True)  # note interne, jamais exposée au candidat
    message_candidat = Column(Text, nullable=True)
    examine_par = Column(String(64), nullable=True)
    examine_le = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ProviseurDocument(Base):
    __tablename__ = "proviseur_documents"
    id = Column(Integer, primary_key=True, autoincrement=True)
    candidature_id = Column(Integer, ForeignKey("proviseur_candidatures.id", ondelete="CASCADE"), nullable=False, index=True)
    type_document = Column(String(24), nullable=False)  # photo_personnelle / piece_identite / justificatif
    nom_fichier = Column(String(180), nullable=False)
    mime_type = Column(String(48), nullable=False)
    taille_octets = Column(Integer, nullable=False)
    sha256 = Column(String(64), nullable=False)
    contenu_chiffre = Column(LargeBinary().with_variant(LONGBLOB(), "mysql"), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ProviseurAutorisation(Base):
    __tablename__ = "proviseur_autorisations"
    __table_args__ = (UniqueConstraint("candidature_id", "etablissement_id", name="uq_prov_etab"),)
    id = Column(Integer, primary_key=True, autoincrement=True)
    candidature_id = Column(Integer, ForeignKey("proviseur_candidatures.id", ondelete="CASCADE"), nullable=False, index=True)
    etablissement_id = Column(Integer, ForeignKey("etablissements_grind.id"), nullable=False, index=True)
    moderation_locale = Column(Integer, nullable=False, default=0)
    peut_publier = Column(Integer, nullable=False, default=1)
    actif = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
