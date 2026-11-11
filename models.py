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
    cgu_version = Column(String(16), nullable=True)  # version des CGU acceptée
    cgu_acceptees_le = Column(DateTime(timezone=True), nullable=True)
    role = Column(String(16), nullable=True)  # None = élève ; "admin" / "associe" / "controleur"
    nom_affiche = Column(String(64), nullable=True)  # surtout pour l'admin (choisi à la première entrée)
    certifie = Column(Integer, nullable=True)  # 1 = badge de certification
    # E-mail : unique par compte. email_verifie : 1 = vérifié ; 0 = inscription récente non vérifiée
    # (supprimée au bout d'1 h) ; NULL = ancien compte sans e-mail vérifié (invité à le faire à la connexion).
    email = Column(String(254), nullable=True)
    email_canonique = Column(String(254), nullable=True, index=True)  # sert à l'unicité (gmail : points/+ ignorés)
    email_verifie = Column(Integer, nullable=True)
    email_verifie_le = Column(DateTime(timezone=True), nullable=True)
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
    niveau = Column(String(32), nullable=True)  # catalogue scolaire (None pour les anciennes classes)
    matiere = Column(String(64), nullable=True)
    complement = Column(String(40), nullable=True)  # partie libre du nom, contrôlée
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
    media_url = Column(String(512), nullable=True)
    media_type = Column(String(8), nullable=True)  # "image" ou "video"
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


class AvatarConfig(Base):
    """Config d'avatar d'un utilisateur — un identifiant d'objet par
    catégorie, référencé dans le catalogue (avatar_catalogue.py)."""
    __tablename__ = "avatar_configs"

    username = Column(String(64), primary_key=True)
    genre = Column(String(32), default="genre_neutre")
    corps = Column(String(32), default="corps_base")
    visage = Column(String(32), default="visage_base")
    cheveux = Column(String(32), default="cheveux_base")
    vetement = Column(String(32), default="vetement_base")
    chapeau = Column(String(32), nullable=True)  # optionnel, aucun par défaut
    lunettes = Column(String(32), nullable=True)
    couleur_aura = Column(String(16), default="brand")
    chaussures = Column(String(32), default="chaussures_base")
    gants = Column(String(32), nullable=True)
    peau = Column(String(9), default="#e8c4a0")
    couleur_cheveux = Column(String(9), default="#2a2a35")
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AvatarDeblocage(Base):
    """Objets verrouillés qu'un utilisateur a débloqués — vide pour tout
    le monde tant que le système de monnaie G n'existe pas encore."""
    __tablename__ = "avatar_deblocages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(64), nullable=False, index=True)
    item_id = Column(String(32), nullable=False)
    debloque_le = Column(DateTime(timezone=True), server_default=func.now())


class Signalement(Base):
    """Un signalement fait par un utilisateur (post, commentaire ou profil).
    On garde un EXTRAIT du contenu au moment du signalement : même si l'auteur
    le supprime ensuite, l'administration voit ce qui a été signalé."""
    __tablename__ = "signalements"

    id = Column(Integer, primary_key=True, autoincrement=True)
    signaleur_username = Column(String(64), nullable=False, index=True)
    type_cible = Column(String(16), nullable=False)  # "post" / "commentaire" / "profil"
    cible_id = Column(String(64), nullable=False, index=True)
    cible_username = Column(String(64), nullable=False, index=True)  # auteur du contenu signalé
    motif = Column(String(24), nullable=False)
    details = Column(Text, nullable=True)
    extrait = Column(Text, nullable=True)
    statut = Column(String(16), default="ouvert")  # "ouvert" / "traite"
    decision = Column(String(40), nullable=True)
    note_admin = Column(Text, nullable=True)
    traite_par = Column(String(64), nullable=True)
    traite_le = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Sanction(Base):
    """Suspension (temporaire) ou bannissement (définitif) d'un compte.
    levee_le rempli = sanction annulée par l'administration."""
    __tablename__ = "sanctions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(64), nullable=False, index=True)
    type = Column(String(16), nullable=False)  # "suspension" / "bannissement"
    motif = Column(Text, nullable=False)
    jusqu_a = Column(DateTime(timezone=True), nullable=True)  # None pour un bannissement
    par = Column(String(64), nullable=False)
    examen_possible = Column(Integer, nullable=True)  # 1 = l'administration autorise une demande d'examen
    jeton_examen_hash = Column(String(64), nullable=True)  # empreinte du jeton donné à la connexion pour demander l'examen
    levee_le = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Annonce(Base):
    """Annonce officielle publiée par l'administrateur : notification à tous."""
    __tablename__ = "annonces"

    id = Column(Integer, primary_key=True, autoincrement=True)
    auteur_username = Column(String(64), nullable=False)
    titre = Column(String(120), nullable=False)
    contenu = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class SupportConversation(Base):
    """Une discussion « Contactez-nous » entre un élève, l'assistant IA et l'équipe."""
    __tablename__ = "support_conversations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(64), nullable=False, index=True)
    statut = Column(String(12), default="ouvert")  # "ouvert" / "clos"
    ia_active = Column(Integer, default=1)  # 1 = l'assistant répond ; 0 = en pause (un humain a la main)
    humain_demande = Column(Integer, default=0)  # l'élève veut parler à l'équipe
    urgent = Column(Integer, default=0)  # message signalant un danger : à lire en premier
    attente_equipe = Column(Integer, default=0)  # 1 = l'équipe doit répondre
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())


class SupportMessage(Base):
    __tablename__ = "support_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(Integer, nullable=False, index=True)
    auteur_type = Column(String(12), nullable=False)  # "utilisateur" / "ia" / "equipe" / "systeme"
    auteur_username = Column(String(64), nullable=True)
    contenu = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Examen(Base):
    """Demande d'examen d'une sanction. Une seule par décision : si elle est refusée,
    la décision est définitive."""
    __tablename__ = "examens"

    id = Column(Integer, primary_key=True, autoincrement=True)
    sanction_id = Column(Integer, nullable=False, index=True)
    username = Column(String(64), nullable=False, index=True)
    message = Column(Text, nullable=False)
    statut = Column(String(12), default="en_attente")  # "en_attente" / "accepte" / "refuse"
    note_admin = Column(Text, nullable=True)
    traite_par = Column(String(64), nullable=True)
    traite_le = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Blocage(Base):
    """« Ne plus voir ses contenus » : décision personnelle d'un utilisateur, sans effet sur l'autre."""
    __tablename__ = "blocages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    bloqueur_username = Column(String(64), nullable=False, index=True)
    bloque_username = Column(String(64), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class VerificationEmail(Base):
    """Un « flux » de vérification d'e-mail (inscription) ou de réinitialisation de mot de passe.
    Voir verification.py : le code n'est gardé que sous forme d'empreinte salée."""
    __tablename__ = "verifications_email"

    id = Column(Integer, primary_key=True, autoincrement=True)
    objet = Column(String(20), nullable=False)  # "inscription" / "reinitialisation"
    username = Column(String(64), nullable=True, index=True)
    email = Column(String(254), nullable=True)
    jeton_hash = Column(String(64), unique=True, nullable=False, index=True)
    code_hash = Column(String(64), nullable=True)
    code_sel = Column(String(32), nullable=True)
    tentatives = Column(Integer, default=0)
    nb_envois = Column(Integer, default=0)
    dernier_envoi_le = Column(DateTime(timezone=True), nullable=True)
    expire_le = Column(DateTime(timezone=True), nullable=True)  # expiration du code courant
    flux_expire_le = Column(DateTime(timezone=True), nullable=False)  # expiration de la démarche entière
    code_valide_le = Column(DateTime(timezone=True), nullable=True)  # réinitialisation : code validé
    utilise_le = Column(DateTime(timezone=True), nullable=True)
    fantome = Column(Integer, default=0)  # 1 = faux flux (adresse inconnue / déjà utilisée) : ne peut jamais aboutir
    ip = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
