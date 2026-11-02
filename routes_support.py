"""
routes_support.py — « Contactez-nous » : un chat d'aide avec un assistant IA et l'équipe.

Principe :
- L'élève décrit son problème. L'assistant IA lui répond tout de suite.
- À tout moment l'élève peut demander à parler à l'équipe.
- Un membre de l'équipe (contrôleur, associé ou admin) peut lire, répondre, et
  désactiver / réactiver l'assistant dans CETTE conversation. Dès qu'un humain répond,
  l'assistant se met en pause tout seul (pour ne pas parler par-dessus), et l'équipe
  peut le réactiver.
- Les messages signalant un danger (harcèlement, menace, mal-être...) passent en
  « urgent » pour que l'équipe les voie en premier.

Comme partout dans GRIND : identité = session, droits = rôle en base, jeton dans
le corps de la requête (POST).
"""

from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import assistant_ia
from database import get_db
from auth_grind import verifier_session
from moderation import a_permission, role_de, badge_de
from schemas import (
    SessionRequest,
    SupportOuvrir,
    SupportConv,
    SupportEnvoyer,
    AdminSupportListe,
    AdminSupportRepondre,
    AdminSupportIA,
)
from models import SupportConversation, SupportMessage
from ws_manager import manager

router = APIRouter()

MAX_MESSAGES_PAR_JOUR = 40
MAX_LONGUEUR = 1500

# Mots qui font passer une conversation en « urgent ». Volontairement large : mieux vaut
# une fausse alerte lue par l'équipe qu'un élève en détresse ignoré.
MOTS_URGENTS = (
    "suicid", "me tuer", "me faire du mal", "mourir", "envie de mourir",
    "harcel", "harcèl", "menac", "viol", "agress", "pédo", "pedo",
    "danger", "frappe", "frapper", "peur de",
)


def _identite(session_token: str, db: Session) -> str:
    username = verifier_session(session_token, db)
    if username is None:
        raise HTTPException(status_code=401, detail="Session GRIND invalide ou expirée — reconnecte-toi")
    return username


def _equipe(session_token: str, db: Session):
    username = _identite(session_token, db)
    role = role_de(db, username)
    if role is None or not a_permission(role, "support"):
        raise HTTPException(status_code=403, detail="Réservé à l'équipe d'administration")
    return username, role


def _iso(dt):
    return dt.isoformat() if dt else None


def _maintenant():
    return datetime.now(timezone.utc)


def _ajouter(db: Session, conv: SupportConversation, auteur_type: str, auteur: str | None, contenu: str) -> SupportMessage:
    message = SupportMessage(conversation_id=conv.id, auteur_type=auteur_type, auteur_username=auteur, contenu=contenu)
    db.add(message)
    conv.updated_at = _maintenant()
    return message


def _messages(db: Session, conv_id: int) -> list[dict]:
    lignes = (
        db.query(SupportMessage)
        .filter(SupportMessage.conversation_id == conv_id)
        .order_by(SupportMessage.id.asc())
        .all()
    )
    return [
        {
            "id": m.id,
            "auteur_type": m.auteur_type,  # "utilisateur" / "ia" / "equipe" / "systeme"
            "auteur": m.auteur_username,
            "contenu": m.contenu,
            "created_at": _iso(m.created_at),
        }
        for m in lignes
    ]


def _etat(conv: SupportConversation) -> dict:
    return {
        "id": conv.id,
        "statut": conv.statut,
        "ia_active": bool(conv.ia_active),
        "humain_demande": bool(conv.humain_demande),
        "urgent": bool(conv.urgent),
        "attente_equipe": bool(conv.attente_equipe),
        "created_at": _iso(conv.created_at),
        "updated_at": _iso(conv.updated_at),
    }


def _conversation_de(db: Session, conv_id: int, username: str) -> SupportConversation:
    conv = db.get(SupportConversation, conv_id)
    if conv is None or conv.username != username:
        raise HTTPException(status_code=404, detail="Conversation introuvable")
    return conv


def _verifier_message(db: Session, username: str, texte: str) -> str:
    texte = (texte or "").strip()
    if not texte:
        raise HTTPException(status_code=400, detail="Écris un message")
    if len(texte) > MAX_LONGUEUR:
        raise HTTPException(status_code=400, detail=f"Message trop long ({MAX_LONGUEUR} caractères maximum)")
    hier = _maintenant() - timedelta(hours=24)
    nb = (
        db.query(SupportMessage)
        .join(SupportConversation, SupportConversation.id == SupportMessage.conversation_id)
        .filter(
            SupportConversation.username == username,
            SupportMessage.auteur_type == "utilisateur",
            SupportMessage.created_at > hier,
        )
        .count()
    )
    if nb >= MAX_MESSAGES_PAR_JOUR:
        raise HTTPException(status_code=429, detail="Tu as beaucoup écrit aujourd'hui — réessaie demain")
    return texte


async def _traiter_message_eleve(db: Session, conv: SupportConversation, username: str, texte: str):
    """Enregistre le message de l'élève, repère l'urgence, puis laisse l'assistant répondre."""
    _ajouter(db, conv, "utilisateur", username, texte)

    bas = texte.lower()
    if not conv.urgent and any(mot in bas for mot in MOTS_URGENTS):
        conv.urgent = 1
        conv.humain_demande = 1
        conv.attente_equipe = 1
        _ajouter(db, conv, "systeme", None,
                 "Ton message a été marqué comme important : un membre de l'équipe va le lire en priorité.")
    elif conv.humain_demande or not conv.ia_active:
        conv.attente_equipe = 1
    db.commit()

    if not conv.ia_active:
        return  # un humain a la main : l'assistant se tait

    historique = []
    for m in _messages(db, conv.id):
        if m["auteur_type"] == "utilisateur":
            historique.append({"role": "user", "content": m["contenu"]})
        elif m["auteur_type"] in ("ia", "equipe"):
            historique.append({"role": "assistant", "content": m["contenu"]})

    reponse = await assistant_ia.repondre(historique)
    if reponse is None:
        conv.humain_demande = 1
        conv.attente_equipe = 1
        _ajouter(db, conv, "systeme", None,
                 "L'assistant n'est pas disponible pour le moment. Un membre de l'équipe te répondra ici.")
    else:
        _ajouter(db, conv, "ia", None, reponse)
    db.commit()


# ------------------------------------------------------------------ côté élève

@router.post("/api/support/conversation")
async def ma_conversation(data: SessionRequest, db: Session = Depends(get_db)):
    username = _identite(data.session_token, db)
    conv = (
        db.query(SupportConversation)
        .filter(SupportConversation.username == username)
        .order_by((SupportConversation.statut == "ouvert").desc(), SupportConversation.id.desc())
        .first()
    )
    if conv is None:
        return {"conversation": None, "messages": []}
    return {"conversation": _etat(conv), "messages": _messages(db, conv.id)}


@router.post("/api/support/ouvrir")
async def ouvrir(data: SupportOuvrir, db: Session = Depends(get_db)):
    username = _identite(data.session_token, db)
    texte = _verifier_message(db, username, data.message)

    conv = (
        db.query(SupportConversation)
        .filter(SupportConversation.username == username, SupportConversation.statut == "ouvert")
        .first()
    )
    if conv is None:
        conv = SupportConversation(username=username)
        db.add(conv)
        db.commit()
        db.refresh(conv)

    await _traiter_message_eleve(db, conv, username, texte)
    return {"conversation": _etat(conv), "messages": _messages(db, conv.id)}


@router.post("/api/support/envoyer")
async def envoyer(data: SupportEnvoyer, db: Session = Depends(get_db)):
    username = _identite(data.session_token, db)
    conv = _conversation_de(db, data.conversation_id, username)
    if conv.statut != "ouvert":
        raise HTTPException(status_code=400, detail="Cette discussion est terminée — ouvre une nouvelle demande")
    texte = _verifier_message(db, username, data.message)

    await _traiter_message_eleve(db, conv, username, texte)
    return {"conversation": _etat(conv), "messages": _messages(db, conv.id)}


@router.post("/api/support/demander-humain")
async def demander_humain(data: SupportConv, db: Session = Depends(get_db)):
    username = _identite(data.session_token, db)
    conv = _conversation_de(db, data.conversation_id, username)
    if conv.statut != "ouvert":
        raise HTTPException(status_code=400, detail="Cette discussion est terminée")
    if not conv.humain_demande:
        conv.humain_demande = 1
        _ajouter(db, conv, "systeme", None, "Tu as demandé à parler à l'équipe : un membre te répondra ici dès que possible.")
    conv.attente_equipe = 1
    db.commit()
    return {"conversation": _etat(conv), "messages": _messages(db, conv.id)}


@router.post("/api/support/cloturer")
async def cloturer(data: SupportConv, db: Session = Depends(get_db)):
    username = _identite(data.session_token, db)
    conv = _conversation_de(db, data.conversation_id, username)
    if conv.statut == "ouvert":
        conv.statut = "clos"
        conv.attente_equipe = 0
        _ajouter(db, conv, "systeme", None, "Discussion terminée.")
        db.commit()
    return {"conversation": _etat(conv), "messages": _messages(db, conv.id)}


# ------------------------------------------------------------------ côté équipe

def _resume(db: Session, conv: SupportConversation) -> dict:
    dernier = (
        db.query(SupportMessage)
        .filter(SupportMessage.conversation_id == conv.id, SupportMessage.auteur_type != "systeme")
        .order_by(SupportMessage.id.desc())
        .first()
    )
    return {
        **_etat(conv),
        "username": conv.username,
        "badge": badge_de(db, conv.username),
        "dernier_message": (dernier.contenu[:140] if dernier else None),
        "dernier_auteur": dernier.auteur_type if dernier else None,
    }


@router.post("/api/admin/support/liste")
async def liste(data: AdminSupportListe, db: Session = Depends(get_db)):
    _equipe(data.session_token, db)
    if data.statut not in ("ouvert", "clos", "tous"):
        raise HTTPException(status_code=400, detail="Statut invalide")

    requete = db.query(SupportConversation)
    if data.statut != "tous":
        requete = requete.filter(SupportConversation.statut == data.statut)
    convs = requete.order_by(
        SupportConversation.urgent.desc(),
        SupportConversation.attente_equipe.desc(),
        SupportConversation.updated_at.desc(),
    ).limit(100).all()
    return {"conversations": [_resume(db, c) for c in convs]}


@router.post("/api/admin/support/lire")
async def lire(data: SupportConv, db: Session = Depends(get_db)):
    _equipe(data.session_token, db)
    conv = db.get(SupportConversation, data.conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation introuvable")
    return {"conversation": _resume(db, conv), "messages": _messages(db, conv.id)}


@router.post("/api/admin/support/repondre")
async def repondre(data: AdminSupportRepondre, db: Session = Depends(get_db)):
    membre, _ = _equipe(data.session_token, db)
    conv = db.get(SupportConversation, data.conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation introuvable")
    if conv.statut != "ouvert":
        raise HTTPException(status_code=400, detail="Cette discussion est terminée")
    texte = (data.message or "").strip()
    if not texte or len(texte) > MAX_LONGUEUR:
        raise HTTPException(status_code=400, detail=f"Message vide ou trop long ({MAX_LONGUEUR} caractères maximum)")

    _ajouter(db, conv, "equipe", membre, texte)
    conv.attente_equipe = 0
    if conv.ia_active:
        # Un humain parle : l'assistant se met en pause (l'équipe peut le réactiver).
        conv.ia_active = 0
        _ajouter(db, conv, "systeme", None, "L'équipe a pris la main : l'assistant est en pause.")
    db.commit()

    await manager.notifier_utilisateur(conv.username, {"type": "support_reponse", "conversation_id": conv.id})
    return {"conversation": _resume(db, conv), "messages": _messages(db, conv.id)}


@router.post("/api/admin/support/ia")
async def basculer_ia(data: AdminSupportIA, db: Session = Depends(get_db)):
    membre, _ = _equipe(data.session_token, db)
    conv = db.get(SupportConversation, data.conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation introuvable")

    voulu = 1 if data.active else 0
    if conv.ia_active != voulu:
        conv.ia_active = voulu
        _ajouter(db, conv, "systeme", None,
                 "L'assistant a été réactivé par l'équipe." if voulu else "L'assistant a été désactivé par l'équipe.")
        db.commit()
    return {"conversation": _resume(db, conv), "messages": _messages(db, conv.id)}


@router.post("/api/admin/support/cloturer")
async def cloturer_equipe(data: SupportConv, db: Session = Depends(get_db)):
    _equipe(data.session_token, db)
    conv = db.get(SupportConversation, data.conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation introuvable")
    if conv.statut == "ouvert":
        conv.statut = "clos"
        conv.attente_equipe = 0
        _ajouter(db, conv, "systeme", None, "Discussion terminée par l'équipe.")
        db.commit()
    return {"conversation": _resume(db, conv), "messages": _messages(db, conv.id)}
