from pydantic import BaseModel
from typing import Optional

# Aucun schéma ci-dessous ne contient de champ "username" : l'identité
# de l'appelant est TOUJOURS déterminée par Pluton via api_key, jamais
# fournie par le client. Voir pluton_client.verifier_cle().


class VerifKeyRequest(BaseModel):
    api_key: str


class ProfilCreate(BaseModel):
    api_key: str
    age: int
    pays: str
    niveau: str
    difficultes: Optional[str] = None


class ClasseCreate(BaseModel):
    api_key: str
    nom: str


class ClasseRejoindre(BaseModel):
    api_key: str
    code_invitation: str


class DemandeAideCreate(BaseModel):
    api_key: str
    sujet: str
    description: Optional[str] = None


class AideAccepter(BaseModel):
    api_key: str
    demande_id: int


class PostCreate(BaseModel):
    api_key: str
    categorie: str  # "demonstration" ou "probleme"
    contenu: str
    date_limite: Optional[str] = None  # ISO 8601, requis si categorie == "probleme"


class CommentaireCreate(BaseModel):
    api_key: str
    contenu: str


class LikeToggle(BaseModel):
    api_key: str


class ResoudrePost(BaseModel):
    api_key: str  # doit appartenir à l'auteur du post


class ExpirerPost(BaseModel):
    api_key: str  # doit appartenir à l'auteur du post
    raison: str
