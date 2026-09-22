"""
models.py — schéma de la base GRIND.

Rien ici ne concerne l'identité G-SOCIETY (compte, mot de passe) : ça
reste géré par Pluton. Ici on stocke uniquement ce qui est propre à
GRIND — le username G-SOCIETY sert juste de clé étrangère textuelle.
"""

from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.sql import func
from database import Base


class CompteGrind(Base):
    """Compte propre à GRIND — distinct du compte G-SOCIETY. La clé API
    G-SOCIETY (catégorie "grind") n'est vérifiée qu'UNE FOIS, à
    l'inscription, comme preuve de sécurité — pas à chaque requête."""
    __tablename__ = "comptes_grind"

    username = Column(String(64), primary_key=True)
    password_hash = Column(String(128), nullable=False)
    gsociety_username = Column(String(64), nullable=False)  # traçabilité : quel compte G-SOCIETY a validé l'inscription
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class GrindSession(Base):
    """Session GRIND, créée au login. Le session_token (pas la clé
    G-SOCIETY) est ce que le frontend envoie ensuite à chaque requête."""
    __tablename__ = "grind_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    token_hash = Column(String(128), unique=True, nullable=False, index=True)
    username = Column(String(64), ForeignKey("comptes_grind.username"), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)


class Profil(Base):
    """Rempli via le questionnaire obligatoire avant la première
    utilisation. Sert à l'algorithme de matching et à l'IA de structuration."""
    __tablename__ = "profils"

    username = Column(String(64), primary_key=True)  # username G-SOCIETY
    age = Column(Integer, nullable=False)
    pays = Column(String(64), nullable=False)
    niveau = Column(String(128), nullable=False)  # classe / filière
    difficultes = Column(Text, nullable=True)  # texte libre ou JSON stringifié
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Classe(Base):
    """Une "classe" GRIND — équivalent conceptuel d'un salon, mais
    schéma totalement séparé de celui de GLITCH. Rejoint uniquement
    par lien d'invitation, jamais en tapant un nom."""
    __tablename__ = "classes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    nom = Column(String(128), nullable=False)  # affichage seulement, pas une clé
    code_invitation = Column(String(32), unique=True, nullable=False, index=True)
    createur_username = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ClasseMembre(Base):
    __tablename__ = "classe_membres"

    id = Column(Integer, primary_key=True, autoincrement=True)
    classe_id = Column(Integer, ForeignKey("classes.id"), nullable=False, index=True)
    username = Column(String(64), nullable=False)
    role = Column(String(16), default="membre")  # "membre" ou "prof"
    joined_at = Column(DateTime(timezone=True), server_default=func.now())


class DemandeAide(Base):
    """Une demande d'aide ponctuelle sur un exercice — déclenche la
    notification aux utilisateurs expérimentés du sujet concerné."""
    __tablename__ = "demandes_aide"

    id = Column(Integer, primary_key=True, autoincrement=True)
    demandeur_username = Column(String(64), nullable=False)
    sujet = Column(String(128), nullable=False)  # ex: "Maths - dérivées"
    description = Column(Text, nullable=True)
    statut = Column(String(16), default="ouverte")  # ouverte / en_cours / resolue
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AideParticipant(Base):
    """Qui a rejoint une demande d'aide, et avec quel rôle
    (le premier à prendre le rôle "prof" anime le tableau noir)."""
    __tablename__ = "aide_participants"

    id = Column(Integer, primary_key=True, autoincrement=True)
    demande_id = Column(Integer, ForeignKey("demandes_aide.id"), nullable=False, index=True)
    username = Column(String(64), nullable=False)
    role = Column(String(16), default="aidant")  # "aidant" ou "prof"
    joined_at = Column(DateTime(timezone=True), server_default=func.now())


class Post(Base):
    """Fil d'actu GRIND. Deux catégories :
    - "demonstration" : partage libre, pas de deadline, pas d'expiration
    - "probleme"       : un exercice non compris, avec une deadline
                          (date_limite) à respecter pour être résolu,
                          sinon il passe automatiquement "expiree"
    """
    __tablename__ = "posts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    auteur_username = Column(String(64), nullable=False)
    categorie = Column(String(16), nullable=False)  # "demonstration" ou "probleme"
    contenu = Column(Text, nullable=False)
    date_limite = Column(DateTime(timezone=True), nullable=True)  # requis si categorie == "probleme"
    statut = Column(String(16), default="ouverte")  # ouverte / resolue / expiree / publiee (demonstration)
    raison_expiration = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class PostLike(Base):
    __tablename__ = "post_likes"
    __table_args__ = (UniqueConstraint("post_id", "username", name="uq_post_like"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    post_id = Column(Integer, ForeignKey("posts.id"), nullable=False, index=True)
    username = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class PostCommentaire(Base):
    __tablename__ = "post_commentaires"

    id = Column(Integer, primary_key=True, autoincrement=True)
    post_id = Column(Integer, ForeignKey("posts.id"), nullable=False, index=True)
    username = Column(String(64), nullable=False)
    contenu = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
