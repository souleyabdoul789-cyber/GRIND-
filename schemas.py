from pydantic import BaseModel
from typing import Optional

# session_token = identité GRIND (obtenue au login GRIND), utilisée
# partout SAUF à l'inscription, où api_key = clé G-SOCIETY catégorie
# "grind", vérifiée une seule fois comme preuve de sécurité.


class CompteGrindSignup(BaseModel):
    username: str
    password: str
    api_key: str  # clé G-SOCIETY catégorie "grind", générée sur le site G-SOCIETY


class CompteGrindLogin(BaseModel):
    username: str
    password: str


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
