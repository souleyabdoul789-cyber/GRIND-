from pydantic import BaseModel
from typing import Any, Optional

# Les comptes GRIND sont autonomes : l'inscription ne dépend pas d'une clé G-SOCIETY.
# La vérification par e-mail reste le contrôle d'activation des comptes ordinaires.


class CompteGrindSignup(BaseModel):
    username: str
    email: str
    password: str
    cgu_acceptees: bool = False


class CompteGrindLogin(BaseModel):
    username: str
    password: str


class ChangerMotDePasse(BaseModel):
    session_token: str
    ancien_mot_de_passe: str
    nouveau_mot_de_passe: str


class SessionRequest(BaseModel):
    session_token: str


class ProfilCreate(BaseModel):
    session_token: str
    age: int
    pays: str
    niveau: str
    filiere: Optional[str] = None
    etablissement: Optional[str] = None
    difficultes: Optional[Any] = None  # matières libres, liste ou texte séparé par des virgules


class ClasseCreate(BaseModel):
    session_token: str
    niveau: Optional[str] = None  # catalogue scolaire (obligatoire côté serveur)
    matiere: Optional[str] = None
    complement: Optional[str] = None  # partie libre du nom, facultative


class ClasseRejoindre(BaseModel):
    session_token: str
    code_invitation: str


class ClasseRenommer(BaseModel):
    session_token: str
    nom: str


class ClasseTransfererProf(BaseModel):
    session_token: str
    nouveau_prof: str


class DemandeAideCreate(BaseModel):
    session_token: str
    sujet: str
    description: Optional[str] = None


class AideAccepter(BaseModel):
    session_token: str
    demande_id: int


class PostCreate(BaseModel):
    session_token: str
    categorie: str  # "demonstration" ou "probleme"
    contenu: str
    date_limite: Optional[str] = None  # ISO 8601, requis si categorie == "probleme"
    media_url: Optional[str] = None
    media_type: Optional[str] = None  # "image" ou "video"
    media_items: Optional[list[dict]] = None  # galerie jusqu’à 10 médias
    document_url: Optional[str] = None
    document_name: Optional[str] = None
    document_type: Optional[str] = None
    etablissement_id: Optional[int] = None


class CommentaireCreate(BaseModel):
    session_token: str
    contenu: str
    parent_id: Optional[int] = None
    media_url: Optional[str] = None
    media_type: Optional[str] = None
    document_url: Optional[str] = None
    document_name: Optional[str] = None
    document_type: Optional[str] = None


class LikeToggle(BaseModel):
    session_token: str


class ResoudrePost(BaseModel):
    session_token: str  # doit appartenir à l'auteur du post


class ExpirerPost(BaseModel):
    session_token: str  # doit appartenir à l'auteur du post
    raison: str


# ---------- Modération ----------

class SignalementCreate(BaseModel):
    session_token: str
    type_cible: str  # "post" / "commentaire" / "profil"
    cible_id: str
    motif: str  # "inapproprie" / "harcelement" / "spam" / "danger" / "autre"
    details: Optional[str] = None


class AdminListeSignalements(BaseModel):
    session_token: str
    statut: str = "ouvert"  # "ouvert" / "traite" / "tous"


class AdminTraiter(BaseModel):
    session_token: str
    action: str  # "ignorer" / "supprimer" / "suspendre" / "bannir"
    supprimer_contenu: bool = False
    duree_jours: Optional[int] = None
    note: Optional[str] = None
    examen_possible: bool = False  # suspension / bannissement : autoriser une demande d'examen


class AdminRechercheUtilisateurs(BaseModel):
    session_token: str
    q: Optional[str] = None


class AdminSanctionner(BaseModel):
    session_token: str
    username: str
    type: str  # "suspension" / "bannissement"
    duree_jours: Optional[int] = None
    motif: str
    examen_possible: bool = False


class AdminLever(BaseModel):
    session_token: str
    username: str


class AdminNom(BaseModel):
    session_token: str
    nom_affiche: str


class AdminEquipeNommer(BaseModel):
    session_token: str
    username: str
    role: str  # "associe" / "controleur"


class AdminCertifier(BaseModel):
    session_token: str
    username: str
    certifie: bool


class AdminAnnoncePublier(BaseModel):
    session_token: str
    titre: str
    contenu: str = ""
    media_url: Optional[str] = None
    media_type: Optional[str] = None
    document_url: Optional[str] = None
    document_name: Optional[str] = None
    document_type: Optional[str] = None
    important: bool = False
    epinglee: bool = False


class AdminAnnonceSupprimer(BaseModel):
    session_token: str
    annonce_id: int


# ---------- Support (« Contactez-nous ») ----------

class SupportOuvrir(BaseModel):
    session_token: str
    message: str


class SupportConv(BaseModel):
    session_token: str
    conversation_id: int


class SupportEnvoyer(BaseModel):
    session_token: str
    conversation_id: int
    message: str


class AdminSupportListe(BaseModel):
    session_token: str
    statut: str = "ouvert"  # "ouvert" / "clos" / "tous"


class AdminSupportRepondre(BaseModel):
    session_token: str
    conversation_id: int
    message: str


class AdminSupportIA(BaseModel):
    session_token: str
    conversation_id: int
    active: bool


# ---------- Blocage, examen, contenus ----------

class BlocageAction(BaseModel):
    session_token: str
    username: str


class ExamenEtat(BaseModel):
    username: str
    jeton_examen: str


class ExamenDemander(BaseModel):
    username: str
    jeton_examen: str
    message: str


class AdminExamens(BaseModel):
    session_token: str
    statut: str = "en_attente"  # "en_attente" / "traite" / "tous"


class AdminExamenDossier(BaseModel):
    session_token: str
    examen_id: int


class AdminExamenTraiter(BaseModel):
    session_token: str
    examen_id: int
    decision: str  # "accepter" / "refuser"
    note: Optional[str] = None


class AdminSupprimerContenu(BaseModel):
    session_token: str
    type_cible: str  # "post" / "commentaire"
    cible_id: str


class AdminToutSupprimer(BaseModel):
    session_token: str
    username: str


class AdminSupprimerClasse(BaseModel):
    session_token: str
    classe_id: int


class AdminSupportDiag(BaseModel):
    session_token: str
    tester: bool = False  # True = faire un vrai essai auprès de Groq


# ---------- Authentification : e-mail, vérification, mot de passe oublié ----------

class VerifierCode(BaseModel):
    jeton_verification: str
    code: str


class JetonVerification(BaseModel):
    jeton_verification: str


class AssocierEmail(BaseModel):
    jeton_verification: str
    email: str


class MotDePasseOublie(BaseModel):
    email: str


class ReinitialiserMotDePasse(BaseModel):
    jeton_verification: str
    mot_de_passe: str


class GoogleAuthRequest(BaseModel):
    credential: str
    username: Optional[str] = None
    cgu_acceptees: bool = False


class PinRequest(BaseModel):
    session_token: str
    pin: str


class PinResetRequest(BaseModel):
    session_token: str
    password: str
    nouveau_pin: str


class PinResetEmailStart(BaseModel):
    session_token: str


class PinResetEmailFinish(BaseModel):
    session_token: str
    jeton_verification: str
    nouveau_pin: str


class DeviceRememberRequest(BaseModel):
    session_token: str
    device_id: str


class DeviceForgetRequest(BaseModel):
    session_token: str
    device_id: str
    username: str
