"""
models.py — schéma de la base GRIND.

Les comptes, mots de passe et sessions sont gérés nativement par GRIND.
Les colonnes historiques liées à G-SOCIETY restent conservées pour ne pas
casser les comptes créés avant la migration.
"""

from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.sql import func
from database import Base


class CompteGrind(Base):
    """Compte autonome GRIND. Les valeurs historiques de gsociety_username
    sont conservées ; les nouveaux comptes utilisent le marqueur GRIND natif."""
    __tablename__ = "comptes_grind"

    username = Column(String(64), primary_key=True)
    password_hash = Column(String(128), nullable=False)
    gsociety_username = Column(String(64), nullable=False)  # historique conservé ; "__GRIND_NATIVE__" pour les nouveaux comptes
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
    # Identité civile facultative issue du profil Google (non utilisée comme identifiant public).
    prenom = Column(String(80), nullable=True)
    nom = Column(String(80), nullable=True)
    google_sub = Column(String(128), nullable=True, unique=True, index=True)
    pin_hash = Column(String(128), nullable=True)
    pin_echecs = Column(Integer, nullable=True, default=0)
    pin_bloque_jusqua = Column(DateTime(timezone=True), nullable=True)
    premium_until = Column(DateTime(timezone=True), nullable=True)  # entitlement premium vérifié côté serveur
    bienvenue_vue = Column(Integer, nullable=True, default=1)  # 0 uniquement pour un nouveau compte à qui montrer l’accueil
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
    pin_verifie_le = Column(DateTime(timezone=True), nullable=True)


class GrindDeviceAccount(Base):
    """Association d'un compte à un navigateur/appareil, sans stocker de jeton en clair."""
    __tablename__ = "grind_device_accounts"
    __table_args__ = (UniqueConstraint("device_hash", "username", name="uq_grind_device_username"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    device_hash = Column(String(64), nullable=False, index=True)
    username = Column(String(64), ForeignKey("comptes_grind.username"), nullable=False)
    token_hash = Column(String(64), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_used_at = Column(DateTime(timezone=True), server_default=func.now())


class Profil(Base):
    """Rempli via le questionnaire obligatoire avant la première
    utilisation. Sert à l'algorithme de matching et à l'IA de structuration."""
    __tablename__ = "profils"

    username = Column(String(64), primary_key=True)  # identifiant public GRIND
    age = Column(Integer, nullable=False)
    pays = Column(String(64), nullable=False)
    niveau = Column(String(128), nullable=False)  # classe / niveau saisi librement
    difficultes = Column(Text, nullable=True)  # matières libres, JSON stringifié
    filiere = Column(String(100), nullable=True)  # général, professionnel, technique, etc.
    etablissement = Column(String(160), nullable=True)  # établissement déclaré par l'élève
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
    createur_username = Column(String(64), nullable=False)  # propriétaire immuable de la classe
    prof_username = Column(String(64), nullable=True, index=True)  # enseignant actuel, transférable sans changer le propriétaire
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ClasseMembre(Base):
    __tablename__ = "classe_membres"

    id = Column(Integer, primary_key=True, autoincrement=True)
    classe_id = Column(Integer, ForeignKey("classes.id"), nullable=False, index=True)
    username = Column(String(64), nullable=False)
    role = Column(String(16), default="membre")  # "membre" ou "prof"
    siege_index = Column(Integer, nullable=True, index=True)  # 0..8 ; NULL pour le prof au bureau avant
    micro_autorise = Column(Integer, nullable=True, default=1)  # 0 si le professeur a suspendu le droit de parole
    joined_at = Column(DateTime(timezone=True), server_default=func.now())


class ClasseFeuille(Base):
    """Feuille personnelle persistante ; privée tant qu’aucune permission n’est accordée."""
    __tablename__ = "classe_feuilles"
    __table_args__ = (UniqueConstraint("classe_id", "username", name="uq_classe_feuille_auteur"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    classe_id = Column(Integer, ForeignKey("classes.id", ondelete="CASCADE"), nullable=False, index=True)
    username = Column(String(64), nullable=False, index=True)
    contenu = Column(Text, nullable=False, default="")
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ClasseFeuillePermission(Base):
    """Autorisation explicite et persistante de consulter une feuille."""
    __tablename__ = "classe_feuille_permissions"
    __table_args__ = (UniqueConstraint("classe_id", "proprietaire_username", "lecteur_username", name="uq_classe_feuille_permission"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    classe_id = Column(Integer, ForeignKey("classes.id", ondelete="CASCADE"), nullable=False, index=True)
    proprietaire_username = Column(String(64), nullable=False, index=True)
    lecteur_username = Column(String(64), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ClasseTableau(Base):
    """Texte du tableau noir partagé, modifiable uniquement par le professeur actuel."""
    __tablename__ = "classe_tableaux"

    classe_id = Column(Integer, ForeignKey("classes.id", ondelete="CASCADE"), primary_key=True)
    contenu = Column(Text, nullable=False, default="")
    updated_by = Column(String(64), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


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
    media_type = Column(String(8), nullable=True)  # "image", "video" ou "document"
    medias_json = Column(Text, nullable=True)  # galerie de médias (jusqu’à 10), rétrocompatible avec media_url
    document_url = Column(String(512), nullable=True)
    document_name = Column(String(180), nullable=True)
    document_type = Column(String(80), nullable=True)
    etablissement_id = Column(Integer, nullable=True, index=True)  # post institutionnel, NULL = fil général
    auteur_type = Column(String(16), nullable=True, default="eleve")
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
    parent_id = Column(Integer, nullable=True, index=True)
    media_url = Column(String(512), nullable=True)
    media_type = Column(String(16), nullable=True)
    document_url = Column(String(512), nullable=True)
    document_name = Column(String(180), nullable=True)
    document_type = Column(String(100), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class PostCommentaireLike(Base):
    __tablename__ = "post_commentaire_likes"
    __table_args__ = (UniqueConstraint("commentaire_id", "username", name="uq_post_commentaire_like"),)
    id = Column(Integer, primary_key=True, autoincrement=True)
    commentaire_id = Column(Integer, ForeignKey("post_commentaires.id"), nullable=False, index=True)
    username = Column(String(64), nullable=False, index=True)
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
    media_url = Column(String(512), nullable=True)
    media_type = Column(String(8), nullable=True)  # image / video
    document_url = Column(String(512), nullable=True)
    document_name = Column(String(180), nullable=True)
    document_type = Column(String(80), nullable=True)
    important = Column(Integer, nullable=True, default=0)
    epinglee = Column(Integer, nullable=True, default=0)
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


class NotificationGrind(Base):
    """Notification persistante liée à une mention dans une publication ou un commentaire."""
    __tablename__ = "notifications_grind"
    id = Column(Integer, primary_key=True, autoincrement=True)
    destinataire_username = Column(String(64), nullable=False, index=True)
    acteur_username = Column(String(64), nullable=False)
    type = Column(String(24), nullable=False)
    post_id = Column(Integer, nullable=True)
    commentaire_id = Column(Integer, nullable=True)
    resource_type = Column(String(32), nullable=True)
    resource_id = Column(Integer, nullable=True)
    classe_id = Column(Integer, nullable=True, index=True)
    extrait = Column(String(240), nullable=True)
    lu = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class LibraryPlaylist(Base):
    __tablename__ = "library_playlists"
    id = Column(Integer, primary_key=True, autoincrement=True)
    titre = Column(String(120), nullable=False)
    description = Column(Text, nullable=True)
    categorie = Column(String(64), nullable=False, index=True)
    createur_username = Column(String(64), nullable=False, index=True)
    est_defaut = Column(Integer, nullable=False, default=0)
    public = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class LibraryDocument(Base):
    __tablename__ = "library_documents"
    id = Column(Integer, primary_key=True, autoincrement=True)
    playlist_id = Column(Integer, ForeignKey("library_playlists.id"), nullable=False, index=True)
    auteur_username = Column(String(64), nullable=False, index=True)
    titre = Column(String(180), nullable=False)
    description = Column(Text, nullable=True)
    fichier_url = Column(String(700), nullable=False)
    fichier_nom = Column(String(180), nullable=False)
    fichier_type = Column(String(120), nullable=False)
    taille_octets = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class LibraryDocumentVote(Base):
    __tablename__ = "library_document_votes"
    __table_args__ = (UniqueConstraint("document_id", "username", name="uq_library_document_vote"),)
    id = Column(Integer, primary_key=True, autoincrement=True)
    document_id = Column(Integer, ForeignKey("library_documents.id"), nullable=False, index=True)
    username = Column(String(64), nullable=False, index=True)
    valeur = Column(Integer, nullable=False)  # +1 / -1
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class LibraryDocumentComment(Base):
    __tablename__ = "library_document_comments"
    id = Column(Integer, primary_key=True, autoincrement=True)
    document_id = Column(Integer, ForeignKey("library_documents.id"), nullable=False, index=True)
    username = Column(String(64), nullable=False, index=True)
    contenu = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class CountryAccessBlock(Base):
    __tablename__ = "country_access_blocks"
    code = Column(String(8), primary_key=True)
    nom = Column(String(100), nullable=False)
    message = Column(Text, nullable=True)
    actif = Column(Integer, nullable=False, default=1)
    modifie_par = Column(String(64), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ClasseDiscussion(Base):
    """Discussion de groupe associée à une classe réelle, mais avec son propre plafond."""
    __tablename__ = "classe_discussions"
    __table_args__ = (UniqueConstraint("classe_id", name="uq_classe_discussion_classe"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    classe_id = Column(Integer, ForeignKey("classes.id", ondelete="CASCADE"), nullable=False, index=True)
    owner_username = Column(String(64), nullable=False)
    nom = Column(String(128), nullable=False)
    photo_url = Column(String(700), nullable=True)
    code_invitation = Column(String(48), nullable=False, unique=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ClasseDiscussionMembre(Base):
    """Adhésion au groupe de discussion (200 max), indépendante des 10 sièges de la classe."""
    __tablename__ = "classe_discussion_membres"
    __table_args__ = (UniqueConstraint("discussion_id", "username", name="uq_discussion_membre"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    discussion_id = Column(Integer, ForeignKey("classe_discussions.id", ondelete="CASCADE"), nullable=False, index=True)
    username = Column(String(64), nullable=False, index=True)
    role = Column(String(16), nullable=False, default="membre")  # owner / admin / membre
    restreint = Column(Integer, nullable=False, default=0)
    restriction_jusqua = Column(DateTime(timezone=True), nullable=True)  # NULL + restreint=1 : jusqu'à levée manuelle
    # actif=0 conserve le fait qu'un membre a quitté ou a été retiré.
    # banni=1 interdit une nouvelle entrée via le code d'invitation.
    actif = Column(Integer, nullable=True, default=1)
    banni = Column(Integer, nullable=True, default=0)
    dernier_message_lu_id = Column(Integer, nullable=True, default=0)
    joined_at = Column(DateTime(timezone=True), server_default=func.now())


class ClasseDiscussionMessage(Base):
    """Message persistant du groupe de classe."""
    __tablename__ = "classe_discussion_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    discussion_id = Column(Integer, ForeignKey("classe_discussions.id", ondelete="CASCADE"), nullable=False, index=True)
    auteur_username = Column(String(64), nullable=False, index=True)
    contenu = Column(Text, nullable=False, default="")
    reply_to_id = Column(Integer, ForeignKey("classe_discussion_messages.id", ondelete="SET NULL"), nullable=True, index=True)
    style_texte = Column(String(16), nullable=False, default="normal")  # normal / serif / mono
    piece_url = Column(String(900), nullable=True)
    piece_type = Column(String(16), nullable=True)  # image / video / document
    piece_nom = Column(String(180), nullable=True)
    piece_mime = Column(String(120), nullable=True)
    piece_taille = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ClasseDiscussionMention(Base):
    """Une occurrence de mention par message et destinataire."""
    __tablename__ = "classe_discussion_mentions"
    __table_args__ = (UniqueConstraint("message_id", "username", name="uq_discussion_mention"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    message_id = Column(Integer, ForeignKey("classe_discussion_messages.id", ondelete="CASCADE"), nullable=False, index=True)
    username = Column(String(64), nullable=False, index=True)
    lu = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
