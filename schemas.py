from pydantic import BaseModel
from typing import Optional

# session_token = identité GRIND (obtenue au login GRIND), utilisée
# partout SAUF à l'inscription, où api_key = clé G-SOCIETY catégorie
# "grind", vérifiée une seule fois comme preuve de sécurité.


class CompteGrindSignup(BaseModel):
    username: str
    password: str
    api_key: str  # clé G-SOCIETY catégorie "grind", générée sur le site G-SOCIETY
    cgu_acceptees: bool = False  # case "J'accepte les CGU" cochée à l'inscription


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
    difficultes: Optional[str] = None


class ClasseCreate(BaseModel):
    session_token: str
    nom: str


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


class CommentaireCreate(BaseModel):
    session_token: str
    contenu: str


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


class AdminRechercheUtilisateurs(BaseModel):
    session_token: str
    q: Optional[str] = None


class AdminSanctionner(BaseModel):
    session_token: str
    username: str
    type: str  # "suspension" / "bannissement"
    duree_jours: Optional[int] = None
    motif: str


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
    contenu: str


class AdminAnnonceSupprimer(BaseModel):
    session_token: str
    annonce_id: int
