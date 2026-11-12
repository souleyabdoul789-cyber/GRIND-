"""Discussion privée type groupe, liée à chaque classe GRIND.

La classe scolaire garde son plafond historique de 10 membres. Le groupe de
messages est une entité séparée, plafonnée à 200 membres, et peut donc inclure
des personnes qui ne prennent pas de siège dans la classe réelle.
"""
from datetime import datetime, timedelta, timezone
import re
import secrets
from urllib.parse import urlparse, quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from auth_grind import verifier_session
from database import get_db
from discussion_service import assurer_discussion, MAX_MEMBRES_DISCUSSION
from models import (
    Classe, ClasseMembre, ClasseDiscussion, ClasseDiscussionMembre,
    ClasseDiscussionMessage, ClasseDiscussionMention, CompteGrind,
    NotificationGrind,
)
from ws_manager import manager
from moderation import sanction_active
from push_notifications import envoyer_notifications_push, envoyer_push_a_utilisateur

router = APIRouter()
MAX_MESSAGE = 5000
MAX_PAGE = 60


def _identite(session_token: str, db: Session) -> str:
    username = verifier_session(session_token, db)
    if not username:
        raise HTTPException(status_code=401, detail="Session GRIND invalide ou expirée.")
    return username


def _classe_et_discussion(db: Session, classe_id: int):
    classe = db.get(Classe, classe_id)
    if classe is None:
        raise HTTPException(status_code=404, detail="Classe introuvable.")
    discussion = assurer_discussion(db, classe)
    db.flush()
    return classe, discussion


def _membre(db: Session, discussion_id: int, username: str) -> ClasseDiscussionMembre:
    membre = db.query(ClasseDiscussionMembre).filter_by(
        discussion_id=discussion_id, username=username
    ).first()
    if membre is None or membre.actif == 0:
        raise HTTPException(status_code=403, detail="Tu n'es pas membre de cette discussion.")
    return membre


def _nom_compte(db: Session, username: str) -> str:
    compte = db.get(CompteGrind, username)
    if not compte:
        return username
    return " ".join(x for x in [compte.prenom, compte.nom] if x).strip() or username


def _serialiser_message(db: Session, classe_id: int, message: ClasseDiscussionMessage) -> dict:
    auteur = db.get(CompteGrind, message.auteur_username)
    parent = db.get(ClasseDiscussionMessage, message.reply_to_id) if message.reply_to_id else None
    return {
        "id": message.id,
        "auteur": message.auteur_username,
        "auteur_nom": _nom_compte(db, message.auteur_username),
        "contenu": message.contenu or "",
        "reply_to_id": message.reply_to_id,
        "reply": ({"id": parent.id, "auteur": parent.auteur_username,
                   "auteur_nom": _nom_compte(db, parent.auteur_username),
                   "contenu": (parent.contenu or "")[:180]} if parent else None),
        "style_texte": message.style_texte or "normal",
        # Ne jamais exposer l'URL publique Cloudinary d'une pièce privée.
        # Chaque client construit sa propre URL proxy avec son jeton de session.
        "piece": ({"url": f"/api/classes/{classe_id}/discussion/messages/{message.id}/piece",
                   "type": message.piece_type, "nom": message.piece_nom,
                   "mime": message.piece_mime, "taille": message.piece_taille}
                  if message.piece_url else None),
        "created_at": message.created_at.isoformat() if message.created_at else None,
    }


def _serialiser_membre(db: Session, classe_id: int, membre: ClasseDiscussionMembre) -> dict:
    return {
        "username": membre.username,
        "nom": _nom_compte(db, membre.username),
        "role": membre.role,
        "restreint": bool(membre.restreint),
        "restriction_jusqua": membre.restriction_jusqua.isoformat() if membre.restriction_jusqua else None,
        "est_en_classe": db.query(ClasseMembre.id).filter_by(
            classe_id=classe_id, username=membre.username
        ).first() is not None,
    }


def _verifier_piece(piece: dict | None):
    if not piece:
        return None
    url = str(piece.get("url") or "").strip()
    genre = str(piece.get("type") or "").strip().lower()
    nom = str(piece.get("nom") or "fichier").replace("\\", "/").split("/")[-1][:180]
    mime = str(piece.get("mime") or "")[:120]
    try:
        taille = int(piece.get("taille") or 0)
    except (TypeError, ValueError):
        taille = 0
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "res.cloudinary.com":
        raise HTTPException(status_code=400, detail="La pièce jointe doit provenir du stockage GRIND.")
    limites = {"image": ("/image/upload/", 25), "video": ("/video/upload/", 300), "document": ("/raw/upload/", 15)}
    if genre not in limites:
        raise HTTPException(status_code=400, detail="Type de pièce jointe non accepté.")
    chemin, max_mo = limites[genre]
    if chemin not in parsed.path or taille <= 0 or taille > max_mo * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"La pièce jointe est invalide ou dépasse {max_mo} Mo.")
    extension = nom.rsplit(".", 1)[-1].lower() if "." in nom else ""
    extensions = {
        "image": {"jpg", "jpeg", "png", "webp"},
        "video": {"mp4", "mov", "webm"},
        "document": {"pdf", "docx"},
    }
    if extension not in extensions[genre]:
        raise HTTPException(status_code=415, detail="Extension de pièce jointe non prise en charge.")
    return {"url": url, "type": genre, "nom": nom, "mime": mime, "taille": taille}


def _restriction_active(db: Session, membre: ClasseDiscussionMembre) -> bool:
    if not membre.restreint:
        return False
    if membre.restriction_jusqua is not None:
        fin = membre.restriction_jusqua
        fin = fin if fin.tzinfo else fin.replace(tzinfo=timezone.utc)
        if fin <= datetime.now(timezone.utc):
            membre.restreint = 0
            membre.restriction_jusqua = None
            db.commit()
            return False
    return True


def _exiger_admin(membre: ClasseDiscussionMembre):
    if membre.role not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail="Réservé au propriétaire et aux administrateurs du groupe.")


@router.get("/api/classes/{classe_id}/discussion")
async def obtenir_discussion(classe_id: int, session_token: str, db: Session = Depends(get_db)):
    username = _identite(session_token, db)
    classe, discussion = _classe_et_discussion(db, classe_id)
    moi = _membre(db, discussion.id, username)
    membres = db.query(ClasseDiscussionMembre).filter_by(discussion_id=discussion.id, actif=1).order_by(
        ClasseDiscussionMembre.role.asc(), ClasseDiscussionMembre.joined_at.asc()
    ).all()
    messages = db.query(ClasseDiscussionMessage).filter_by(discussion_id=discussion.id).order_by(
        ClasseDiscussionMessage.id.desc()
    ).limit(50).all()
    mentions = db.query(ClasseDiscussionMention).join(
        ClasseDiscussionMessage, ClasseDiscussionMessage.id == ClasseDiscussionMention.message_id
    ).filter(
        ClasseDiscussionMention.username == username,
        ClasseDiscussionMention.lu == 0,
        ClasseDiscussionMessage.discussion_id == discussion.id,
    ).count()
    db.commit()  # persiste la création lazy éventuelle pour les anciennes classes
    return {
        "discussion": {
            "id": discussion.id, "classe_id": classe.id,
            "nom": discussion.nom or classe.nom, "photo_url": discussion.photo_url,
            "code_invitation": discussion.code_invitation,
            "owner_username": discussion.owner_username,
            "nb_membres": len(membres), "limite_membres": MAX_MEMBRES_DISCUSSION,
            "mon_role": moi.role, "restreint": _restriction_active(db, moi),
            "restriction_jusqua": moi.restriction_jusqua.isoformat() if moi.restriction_jusqua else None,
            "mentions_non_lues": mentions,
            "est_membre_classe": db.query(ClasseMembre.id).filter_by(classe_id=classe.id, username=username).first() is not None,
            "code_classe": classe.code_invitation,
            "nom_classe": classe.nom,
        },
        "membres": [_serialiser_membre(db, classe.id, m) for m in membres],
        "messages": [_serialiser_message(db, classe.id, m) for m in reversed(messages)],
    }


@router.get("/api/classes/{classe_id}/discussion/messages")
async def lister_messages(classe_id: int, session_token: str, before_id: int | None = None,
                          after_id: int | None = None, limit: int = 50, db: Session = Depends(get_db)):
    username = _identite(session_token, db)
    classe, discussion = _classe_et_discussion(db, classe_id)
    _membre(db, discussion.id, username)
    limite = max(1, min(int(limit or 50), MAX_PAGE))
    q = db.query(ClasseDiscussionMessage).filter_by(discussion_id=discussion.id)
    if before_id:
        q = q.filter(ClasseDiscussionMessage.id < before_id)
        rows = q.order_by(ClasseDiscussionMessage.id.desc()).limit(limite).all()
        rows = list(reversed(rows))
    elif after_id:
        q = q.filter(ClasseDiscussionMessage.id > after_id)
        rows = q.order_by(ClasseDiscussionMessage.id.asc()).limit(limite).all()
    else:
        rows = q.order_by(ClasseDiscussionMessage.id.desc()).limit(limite).all()
        rows = list(reversed(rows))
    return {"messages": [_serialiser_message(db, classe.id, m) for m in rows], "has_more": len(rows) == limite}


@router.get("/api/classes/{classe_id}/discussion/messages/{message_id}/piece")
async def obtenir_piece_message(
    classe_id: int, message_id: int, request: Request, session_token: str,
    db: Session = Depends(get_db),
):
    """Diffuse une pièce jointe uniquement aux membres actifs du groupe.

    Le proxy relaie Range/206 pour la lecture vidéo et ne charge pas le média
    complet en mémoire. L'URL d'origine Cloudinary n'est jamais envoyée au client.
    """
    username = _identite(session_token, db)
    classe, discussion = _classe_et_discussion(db, classe_id)
    _membre(db, discussion.id, username)
    message = db.query(ClasseDiscussionMessage).filter_by(
        id=message_id, discussion_id=discussion.id
    ).first()
    if message is None or not message.piece_url:
        raise HTTPException(status_code=404, detail="Pièce jointe introuvable.")
    parsed = urlparse(message.piece_url)
    if parsed.scheme != "https" or parsed.hostname != "res.cloudinary.com":
        raise HTTPException(status_code=502, detail="Stockage de pièce jointe invalide.")

    client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=20.0), follow_redirects=False)
    entetes = {}
    range_header = request.headers.get("range")
    if range_header and re.fullmatch(r"bytes=\d*-\d*(?:,\d*-\d*)*", range_header):
        entetes["Range"] = range_header
    upstream = None
    try:
        upstream_request = client.build_request("GET", message.piece_url, headers=entetes)
        upstream = await client.send(upstream_request, stream=True)
        if upstream.status_code not in (200, 206):
            await upstream.aclose()
            await client.aclose()
            raise HTTPException(status_code=502, detail="Impossible de récupérer la pièce jointe.")
        reponse_headers = {
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": (
                f"inline; filename*=UTF-8''{quote(message.piece_nom or 'piece-jointe')}"
                if message.piece_type in ("image", "video")
                else f"attachment; filename*=UTF-8''{quote(message.piece_nom or 'document')}"
            ),
        }
        for header in ("content-length", "content-range"):
            if upstream.headers.get(header):
                reponse_headers[header.title()] = upstream.headers[header]

        async def flux():
            try:
                async for morceau in upstream.aiter_bytes():
                    yield morceau
            finally:
                await upstream.aclose()
                await client.aclose()

        media_type = message.piece_mime or upstream.headers.get("content-type") or "application/octet-stream"
        return StreamingResponse(
            flux(), status_code=upstream.status_code, media_type=media_type, headers=reponse_headers
        )
    except HTTPException:
        raise
    except httpx.HTTPError:
        if upstream is not None:
            await upstream.aclose()
        await client.aclose()
        raise HTTPException(status_code=502, detail="Le stockage de pièce jointe est temporairement indisponible.")


@router.post("/api/classes/{classe_id}/discussion/messages")
async def envoyer_message(classe_id: int, data: dict, db: Session = Depends(get_db)):
    username = _identite(data.get("session_token", ""), db)
    classe, discussion = _classe_et_discussion(db, classe_id)
    membre = _membre(db, discussion.id, username)
    if _restriction_active(db, membre):
        raise HTTPException(status_code=403, detail="Tu ne peux pas envoyer de message pour le moment.")
    contenu = str(data.get("contenu") or "").strip()
    if len(contenu) > MAX_MESSAGE:
        raise HTTPException(status_code=400, detail=f"Un message ne peut pas dépasser {MAX_MESSAGE} caractères.")
    piece = _verifier_piece(data.get("piece"))
    if not contenu and not piece:
        raise HTTPException(status_code=400, detail="Écris un message ou ajoute une pièce jointe.")
    style = str(data.get("style_texte") or "normal")
    if style not in ("normal", "serif", "mono"):
        style = "normal"
    parent_id = data.get("reply_to_id")
    parent = None
    if parent_id:
        parent = db.get(ClasseDiscussionMessage, int(parent_id))
        if parent is None or parent.discussion_id != discussion.id:
            raise HTTPException(status_code=404, detail="Le message auquel tu réponds est introuvable.")

    message = ClasseDiscussionMessage(
        discussion_id=discussion.id, auteur_username=username, contenu=contenu,
        reply_to_id=parent.id if parent else None, style_texte=style,
        piece_url=piece["url"] if piece else None,
        piece_type=piece["type"] if piece else None,
        piece_nom=piece["nom"] if piece else None,
        piece_mime=piece["mime"] if piece else None,
        piece_taille=piece["taille"] if piece else None,
    )
    db.add(message)
    db.flush()

    destinataires = []
    for pseudo in dict.fromkeys(re.findall(r"(?<![A-Za-z0-9_])@([A-Za-z_]{3,24})", contenu)):
        compte = db.query(CompteGrind).filter(func.lower(CompteGrind.username) == pseudo.lower()).first()
        if not compte or compte.username == username:
            continue
        membre_cible = db.query(ClasseDiscussionMembre).filter_by(
            discussion_id=discussion.id, username=compte.username
        ).first()
        if not membre_cible:
            continue  # un tag n'accorde jamais l'accès à un groupe privé
        db.add(ClasseDiscussionMention(message_id=message.id, username=compte.username))
        notification = NotificationGrind(
            destinataire_username=compte.username,
            acteur_username=username,
            type="mention_classe",
            resource_type="classe_discussion_message",
            resource_id=message.id,
            classe_id=classe.id,
            extrait=(contenu[:180] or "T'a mentionné dans une discussion"),
        )
        db.add(notification)
        destinataires.append((compte.username, notification))

    notifications_push = [notification for _, notification in destinataires]
    db.commit()
    await envoyer_notifications_push(db, notifications_push)
    db.refresh(message)
    payload = _serialiser_message(db, classe.id, message)
    await manager.diffuser_discussion(discussion.id, {"type": "message", "message": payload})
    for cible, notification in destinataires:
        await manager.notifier_utilisateur(cible, {
            "type": "persistante", "id": f"notification-{notification.id}",
            "notificationId": notification.id, "eventType": "mention_classe",
            "acteur": username, "acteur_nom": _nom_compte(db, username),
            "resource_type": "classe_discussion_message", "resource_id": message.id,
            "classe_id": classe.id, "extrait": payload["contenu"][:180], "lu": False,
        })
    return {"success": True, "message": payload}


@router.get("/api/classes/{classe_id}/discussion/membres")
async def lister_membres(classe_id: int, session_token: str, q: str = "", db: Session = Depends(get_db)):
    username = _identite(session_token, db)
    classe, discussion = _classe_et_discussion(db, classe_id)
    _membre(db, discussion.id, username)
    membres = db.query(ClasseDiscussionMembre).filter_by(discussion_id=discussion.id, actif=1).all()
    terme = (q or "").strip().lstrip("@").lower()
    resultat = []
    for m in membres:
        compte = db.get(CompteGrind, m.username)
        nom = _nom_compte(db, m.username)
        if terme and terme not in m.username.lower() and terme not in nom.lower():
            continue
        resultat.append({"username": m.username, "nom": nom, "role": m.role})
    resultat.sort(key=lambda x: (0 if x["role"] == "owner" else 1 if x["role"] == "admin" else 2, x["nom"].lower()))
    return {"membres": resultat[:30]}


@router.get("/api/classes/{classe_id}/discussion/mentions")
async def lister_mentions(classe_id: int, session_token: str, db: Session = Depends(get_db)):
    username = _identite(session_token, db)
    classe, discussion = _classe_et_discussion(db, classe_id)
    _membre(db, discussion.id, username)
    rows = db.query(ClasseDiscussionMention, ClasseDiscussionMessage).join(
        ClasseDiscussionMessage, ClasseDiscussionMessage.id == ClasseDiscussionMention.message_id
    ).filter(
        ClasseDiscussionMention.username == username,
        ClasseDiscussionMention.lu == 0,
        ClasseDiscussionMessage.discussion_id == discussion.id,
    ).order_by(ClasseDiscussionMention.id.asc()).all()
    return {"mentions": [{
        "id": mention.id, "message_id": msg.id, "auteur": msg.auteur_username,
        "auteur_nom": _nom_compte(db, msg.auteur_username),
        "extrait": msg.contenu[:180], "created_at": msg.created_at.isoformat() if msg.created_at else None,
    } for mention, msg in rows]}


@router.post("/api/classes/{classe_id}/discussion/mentions/{mention_id}/lue")
async def marquer_mention_lue(classe_id: int, mention_id: int, data: dict, db: Session = Depends(get_db)):
    username = _identite(data.get("session_token", ""), db)
    classe, discussion = _classe_et_discussion(db, classe_id)
    _membre(db, discussion.id, username)
    mention = db.query(ClasseDiscussionMention).join(
        ClasseDiscussionMessage, ClasseDiscussionMessage.id == ClasseDiscussionMention.message_id
    ).filter(
        ClasseDiscussionMention.id == mention_id,
        ClasseDiscussionMention.username == username,
        ClasseDiscussionMessage.discussion_id == discussion.id,
    ).first()
    if mention is None:
        raise HTTPException(status_code=404, detail="Mention introuvable.")
    mention.lu = 1
    db.commit()
    count = db.query(ClasseDiscussionMention).join(
        ClasseDiscussionMessage, ClasseDiscussionMessage.id == ClasseDiscussionMention.message_id
    ).filter(ClasseDiscussionMention.username == username, ClasseDiscussionMention.lu == 0,
             ClasseDiscussionMessage.discussion_id == discussion.id).count()
    return {"success": True, "mentions_non_lues": count}


@router.post("/api/classes/{classe_id}/discussion/photo")
async def modifier_photo(classe_id: int, data: dict, db: Session = Depends(get_db)):
    username = _identite(data.get("session_token", ""), db)
    classe, discussion = _classe_et_discussion(db, classe_id)
    membre = _membre(db, discussion.id, username)
    _exiger_admin(membre)
    url = str(data.get("photo_url") or "").strip()
    parsed = urlparse(url)
    if url and not (parsed.scheme == "https" and parsed.hostname == "res.cloudinary.com" and "/image/upload/" in parsed.path):
        raise HTTPException(status_code=400, detail="La photo doit provenir du stockage image GRIND.")
    discussion.photo_url = url or None
    db.commit()
    await manager.diffuser_discussion(discussion.id, {"type": "group_update", "photo_url": discussion.photo_url})
    return {"success": True, "photo_url": discussion.photo_url}


@router.post("/api/classes/{classe_id}/discussion/admins")
async def ajouter_admin(classe_id: int, data: dict, db: Session = Depends(get_db)):
    username = _identite(data.get("session_token", ""), db)
    classe, discussion = _classe_et_discussion(db, classe_id)
    acteur = _membre(db, discussion.id, username)
    if acteur.role != "owner":
        raise HTTPException(status_code=403, detail="Seul le propriétaire du groupe peut nommer un administrateur.")
    cible_username = str(data.get("username") or "").strip()
    cible = db.query(ClasseDiscussionMembre).filter_by(discussion_id=discussion.id, username=cible_username).first()
    if cible is None or cible.actif != 1 or cible.banni == 1:
        raise HTTPException(status_code=404, detail="Cette personne n'est pas membre actif du groupe.")
    if cible.role == "owner":
        raise HTTPException(status_code=400, detail="Le propriétaire ne peut pas être remplacé ici.")
    cible.role = "admin"
    db.commit()
    await manager.diffuser_discussion(discussion.id, {"type": "member_update", "username": cible.username, "role": "admin"})
    return {"success": True}


@router.delete("/api/classes/{classe_id}/discussion/admins/{cible_username}")
async def retirer_admin(classe_id: int, cible_username: str, session_token: str, db: Session = Depends(get_db)):
    username = _identite(session_token, db)
    classe, discussion = _classe_et_discussion(db, classe_id)
    acteur = _membre(db, discussion.id, username)
    if acteur.role != "owner":
        raise HTTPException(status_code=403, detail="Seul le propriétaire peut retirer un administrateur.")
    cible = db.query(ClasseDiscussionMembre).filter_by(discussion_id=discussion.id, username=cible_username).first()
    if cible is None or cible.actif != 1 or cible.role != "admin":
        raise HTTPException(status_code=404, detail="Administrateur introuvable.")
    cible.role = "membre"
    db.commit()
    await manager.diffuser_discussion(discussion.id, {"type": "member_update", "username": cible.username, "role": "membre"})
    return {"success": True}


@router.patch("/api/classes/{classe_id}/discussion/membres/{cible_username}/restriction")
async def restreindre_membre(classe_id: int, cible_username: str, data: dict, db: Session = Depends(get_db)):
    username = _identite(data.get("session_token", ""), db)
    classe, discussion = _classe_et_discussion(db, classe_id)
    acteur = _membre(db, discussion.id, username)
    _exiger_admin(acteur)
    cible = db.query(ClasseDiscussionMembre).filter_by(discussion_id=discussion.id, username=cible_username).first()
    if cible is None or cible.actif != 1:
        raise HTTPException(status_code=404, detail="Membre actif introuvable.")
    if cible.role == "owner" or (cible.role == "admin" and acteur.role != "owner"):
        raise HTTPException(status_code=403, detail="Tu ne peux pas restreindre ce rôle.")
    forever = bool(data.get("permanent", False))
    try:
        minutes = int(data.get("minutes") or 0)
    except (TypeError, ValueError):
        minutes = 0
    if not forever and minutes not in (15, 60, 360, 1440, 10080):
        raise HTTPException(status_code=400, detail="Choisis une durée autorisée ou une restriction jusqu'à levée manuelle.")
    from datetime import timedelta
    cible.restreint = 1
    cible.restriction_jusqua = None if forever else datetime.now(timezone.utc) + timedelta(minutes=minutes)
    db.commit()
    await manager.diffuser_discussion(discussion.id, {"type": "member_restriction", "username": cible.username,
        "restreint": True, "restriction_jusqua": cible.restriction_jusqua.isoformat() if cible.restriction_jusqua else None})
    return {"success": True, "restriction_jusqua": cible.restriction_jusqua.isoformat() if cible.restriction_jusqua else None}


@router.delete("/api/classes/{classe_id}/discussion/membres/{cible_username}/restriction")
async def lever_restriction(classe_id: int, cible_username: str, session_token: str, db: Session = Depends(get_db)):
    username = _identite(session_token, db)
    classe, discussion = _classe_et_discussion(db, classe_id)
    acteur = _membre(db, discussion.id, username)
    _exiger_admin(acteur)
    cible = db.query(ClasseDiscussionMembre).filter_by(discussion_id=discussion.id, username=cible_username).first()
    if cible is None or cible.actif != 1:
        raise HTTPException(status_code=404, detail="Membre actif introuvable.")
    cible.restreint = 0
    cible.restriction_jusqua = None
    db.commit()
    await manager.diffuser_discussion(discussion.id, {"type": "member_restriction", "username": cible.username, "restreint": False})
    return {"success": True}


@router.delete("/api/classes/{classe_id}/discussion/membres/{cible_username}")
async def retirer_membre(classe_id: int, cible_username: str, session_token: str, db: Session = Depends(get_db)):
    username = _identite(session_token, db)
    classe, discussion = _classe_et_discussion(db, classe_id)
    acteur = _membre(db, discussion.id, username)
    _exiger_admin(acteur)
    cible = db.query(ClasseDiscussionMembre).filter_by(discussion_id=discussion.id, username=cible_username).first()
    if cible is None:
        raise HTTPException(status_code=404, detail="Membre introuvable.")
    if cible.role == "owner" or (cible.role == "admin" and acteur.role != "owner"):
        raise HTTPException(status_code=403, detail="Tu ne peux pas retirer ce membre.")
    cible.actif = 0
    cible.banni = 1
    db.commit()
    await manager.retirer_connexions_discussion(discussion.id, cible_username)
    await manager.diffuser_discussion(discussion.id, {"type": "member_removed", "username": cible_username})
    return {"success": True}


@router.post("/api/classes/discussions/rejoindre")
async def rejoindre_discussion(data: dict, db: Session = Depends(get_db)):
    username = _identite(data.get("session_token", ""), db)
    code = str(data.get("code_invitation") or "").strip()
    # Verrouille la ligne du groupe pendant le contrôle de capacité afin que
    # deux adhésions simultanées ne dépassent pas les 200 places sur MySQL.
    discussion = db.query(ClasseDiscussion).filter_by(code_invitation=code).with_for_update().first()
    if discussion is None:
        raise HTTPException(status_code=404, detail="Lien de discussion invalide.")
    classe = db.get(Classe, discussion.classe_id)
    if classe is None:
        raise HTTPException(status_code=404, detail="Classe associée introuvable.")
    membre = db.query(ClasseDiscussionMembre).filter_by(discussion_id=discussion.id, username=username).first()
    if membre and membre.actif == 1:
        return {"success": True, "classe_id": classe.id, "discussion_id": discussion.id, "deja_membre": True}
    if membre and membre.banni == 1:
        raise HTTPException(status_code=403, detail="Tu as été exclu de cette discussion. Contacte un administrateur du groupe.")
    count = db.query(ClasseDiscussionMembre).filter_by(discussion_id=discussion.id, actif=1).count()
    if sanction_active(db, discussion.owner_username) is not None:
        raise HTTPException(status_code=403, detail="Cette discussion est temporairement suspendue.")
    if count >= MAX_MEMBRES_DISCUSSION:
        raise HTTPException(status_code=400, detail="Cette discussion a atteint sa limite de 200 membres.")
    if membre:
        membre.actif = 1
    else:
        db.add(ClasseDiscussionMembre(discussion_id=discussion.id, username=username, role="membre", actif=1, banni=0))
    db.commit()
    await manager.diffuser_discussion(discussion.id, {"type": "member_joined", "username": username, "nom": _nom_compte(db, username)})
    return {"success": True, "classe_id": classe.id, "discussion_id": discussion.id}


@router.post("/api/classes/{classe_id}/discussion/rejoindre-classe")
async def rejoindre_classe_depuis_discussion(classe_id: int, data: dict, db: Session = Depends(get_db)):
    username = _identite(data.get("session_token", ""), db)
    # Verrouille la classe pendant la vérification des 10 places réelles.
    classe = db.query(Classe).filter_by(id=classe_id).with_for_update().first()
    if classe is None:
        raise HTTPException(status_code=404, detail="Classe introuvable.")
    discussion = assurer_discussion(db, classe)
    _membre(db, discussion.id, username)
    deja = db.query(ClasseMembre).filter_by(classe_id=classe.id, username=username).first()
    if deja:
        return {"success": True, "deja_membre": True, "classe_id": classe.id}
    if sanction_active(db, classe.createur_username) is not None:
        raise HTTPException(status_code=403, detail="Cette classe est suspendue.")
    count = db.query(ClasseMembre).filter_by(classe_id=classe.id).count()
    if count >= 10:
        raise HTTPException(status_code=400, detail="La classe réelle a atteint sa limite de 10 membres. La discussion reste accessible.")
    db.add(ClasseMembre(classe_id=classe.id, username=username, role="membre"))
    db.flush()
    assurer_discussion(db, classe)
    db.commit()
    await manager.notifier_nouveau_membre_classe(classe.id, classe.nom, classe.createur_username, username)
    await envoyer_push_a_utilisateur(
        db, classe.createur_username, "Nouveau membre dans ta classe",
        f"{username} a rejoint la classe « {classe.nom} ».",
        {"event_type": "nouveau_membre_classe", "classe_id": classe.id},
    )
    return {"success": True, "classe_id": classe.id, "deja_membre": False}


@router.post("/api/classes/{classe_id}/discussion/proprietaire")
async def transferer_proprietaire(classe_id: int, data: dict, db: Session = Depends(get_db)):
    username = _identite(data.get("session_token", ""), db)
    classe, discussion = _classe_et_discussion(db, classe_id)
    actuel = _membre(db, discussion.id, username)
    if actuel.role != "owner":
        raise HTTPException(status_code=403, detail="Seul le propriétaire actuel peut transférer le groupe.")
    cible_username = str(data.get("username") or "").strip()
    cible = db.query(ClasseDiscussionMembre).filter_by(
        discussion_id=discussion.id, username=cible_username
    ).first()
    if cible is None or cible.actif != 1 or cible.banni == 1 or cible.username == username:
        raise HTTPException(status_code=400, detail="Choisis un autre membre actif de cette discussion.")
    cible.role = "owner"
    actuel.role = "admin"
    discussion.owner_username = cible.username
    db.commit()
    await manager.diffuser_discussion(discussion.id, {"type": "owner_changed", "username": cible.username})
    return {"success": True, "owner_username": cible.username}


@router.post("/api/classes/{classe_id}/discussion/invitation/renouveler")
async def renouveler_invitation(classe_id: int, data: dict, db: Session = Depends(get_db)):
    username = _identite(data.get("session_token", ""), db)
    classe, discussion = _classe_et_discussion(db, classe_id)
    membre = _membre(db, discussion.id, username)
    _exiger_admin(membre)
    discussion.code_invitation = secrets.token_urlsafe(18)
    db.commit()
    await manager.diffuser_discussion(discussion.id, {"type": "invitation_changed"})
    return {"success": True, "code_invitation": discussion.code_invitation}


@router.post("/api/classes/{classe_id}/discussion/quitter")
async def quitter_discussion(classe_id: int, data: dict, db: Session = Depends(get_db)):
    username = _identite(data.get("session_token", ""), db)
    classe, discussion = _classe_et_discussion(db, classe_id)
    membre = _membre(db, discussion.id, username)
    if membre.role == "owner":
        raise HTTPException(status_code=400, detail="Le propriétaire doit transférer le groupe avant de le quitter.")
    membre.actif = 0
    db.commit()
    await manager.retirer_connexions_discussion(discussion.id, username)
    return {"success": True}
