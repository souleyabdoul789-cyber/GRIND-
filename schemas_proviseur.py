from pydantic import BaseModel
from typing import Optional


class ProviseurDecision(BaseModel):
    session_token: str
    statut: Optional[str] = "en_attente"
    candidature_id: Optional[int] = None
    decision: Optional[str] = None
    etablissement: Optional[str] = None
    etablissements: Optional[list[str]] = None
    note_admin: Optional[str] = None
    message_candidat: Optional[str] = None
    moderation_locale: bool = False


class ProviseurLogo(BaseModel):
    session_token: str
    logo_url: Optional[str] = None
