"""Envoi best-effort des notifications persistantes vers Android via Firebase FCM HTTP v1.

Configuration Render : GRIND_FIREBASE_SERVICE_ACCOUNT_JSON (JSON du compte de service Firebase).
Aucune clé Firebase n'est embarquée dans le frontend ou le dépôt.
"""
import asyncio
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from models import GrindPushToken, CompteGrind

logger = logging.getLogger("grind.push")


def _message_data(notification):
    return {
        "notification_id": str(notification.id or ""),
        "event_type": str(notification.type or ""),
        "post_id": str(notification.post_id or ""),
        "commentaire_id": str(notification.commentaire_id or ""),
        "resource_type": str(notification.resource_type or ""),
        "resource_id": str(notification.resource_id or ""),
        "classe_id": str(notification.classe_id or ""),
    }


def _envoyer_batch_fcm(project_id, service_account_info, messages):
    from google.oauth2.service_account import Credentials
    from google.auth.transport.requests import Request
    import requests

    credentials = Credentials.from_service_account_info(
        service_account_info,
        scopes=["https://www.googleapis.com/auth/firebase.messaging"],
    )
    credentials.refresh(Request())
    url = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
    headers = {"Authorization": f"Bearer {credentials.token}", "Content-Type": "application/json"}
    invalid_tokens = set()

    def envoyer(item):
        token, payload = item
        body = {"message": {
            "token": token,
            "notification": {"title": payload["title"], "body": payload["body"]},
            "data": payload["data"],
            "android": {"priority": "HIGH", "notification": {"channel_id": "grind-notifications"}},
        }}
        try:
            response = requests.post(url, json=body, headers=headers, timeout=12)
            details = response.text[:500]
            if response.status_code in (404, 410) or "UNREGISTERED" in details:
                return token, True, None
            if response.status_code >= 400:
                return token, False, f"FCM a refusé un message ({response.status_code}): {details}"
        except Exception as exc:
            return token, False, f"Envoi FCM impossible pour un appareil : {exc}"
        return token, False, None

    # Les notifications à plusieurs appareils partent en parallèle avec un plafond
    # pour éviter de ralentir une requête GRIND lorsqu'une annonce touche beaucoup de comptes.
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(messages)))) as pool:
        futures = [pool.submit(envoyer, item) for item in messages]
        for future in as_completed(futures):
            token, est_invalide, erreur = future.result()
            if est_invalide:
                invalid_tokens.add(token)
            elif erreur:
                logger.warning(erreur)
    return invalid_tokens


def _configuration_fcm():
    raw = os.getenv("GRIND_FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        return None
    try:
        info = json.loads(raw)
        project_id = info.get("project_id")
        if not project_id or not info.get("client_email") or not info.get("private_key"):
            raise ValueError("Le JSON du compte de service Firebase est incomplet.")
        return project_id, info
    except Exception as exc:
        logger.error("Configuration FCM invalide : %s", exc)
        return None


async def _envoyer_aux_destinataires(db, usernames, title, body, data=None, exclure=None):
    configuration = _configuration_fcm()
    if not configuration:
        return
    project_id, info = configuration
    destinations = {u for u in (usernames or []) if u and u != exclure}
    if not destinations:
        return
    rows = db.query(GrindPushToken).filter(GrindPushToken.username.in_(destinations)).all()
    if not rows:
        return
    payload_data = {str(k): str(v if v is not None else "") for k, v in (data or {}).items()}
    payload = {"title": (title or "GRIND")[:80], "body": (body or "Nouvelle activité sur GRIND")[:240], "data": payload_data}
    messages = [(row.token, payload) for row in rows]
    try:
        invalid = await asyncio.to_thread(_envoyer_batch_fcm, project_id, info, messages)
        if invalid:
            db.query(GrindPushToken).filter(GrindPushToken.token.in_(list(invalid))).delete(synchronize_session=False)
            db.commit()
    except Exception as exc:
        logger.warning("Le relais FCM a échoué sans interrompre GRIND : %s", exc)


async def envoyer_push_a_utilisateur(db, username, title, body, data=None):
    await _envoyer_aux_destinataires(db, [username], title, body, data)


async def envoyer_push_aux_utilisateurs(db, usernames, title, body, data=None):
    await _envoyer_aux_destinataires(db, usernames, title, body, data)


async def envoyer_push_a_tous(db, title, body, data=None, exclure=None):
    from models import GrindPushToken
    usernames = [x[0] for x in db.query(GrindPushToken.username).distinct().all()]
    await _envoyer_aux_destinataires(db, usernames, title, body, data, exclure=exclure)


async def envoyer_notifications_push(db, notifications):
    """Envoie les notifications persistantes déjà validées en base, en un seul lot FCM.

    Les messages de groupe peuvent toucher beaucoup de membres. Regrouper les
    destinataires évite d'attendre une requête réseau séquentielle par personne.
    L'échec FCM ne bloque jamais l'enregistrement ni la diffusion GRIND.
    """
    notifications = [n for n in (notifications or []) if getattr(n, "id", None)]
    if not notifications:
        return

    configuration = _configuration_fcm()
    if not configuration:
        return
    project_id, service_account_info = configuration

    bodies = {
        "mention": "t'a mentionné",
        "mention_classe": "t'a mentionné dans une discussion de classe",
        "message_classe": "a envoyé un message dans une discussion de classe",
        "like_publication": "a aimé ta publication",
        "commentaire": "a commenté ta publication",
        "reponse_commentaire": "a répondu à ton commentaire",
        "like_commentaire": "a aimé ton commentaire",
        "avis_doc": "a évalué un document de ta Library",
        "commentaire_doc": "a commenté un document de ta Library",
    }

    par_destinataire = {}
    for notification in notifications:
        acteur = db.get(CompteGrind, notification.acteur_username)
        acteur_nom = " ".join(
            x for x in [getattr(acteur, "prenom", None), getattr(acteur, "nom", None)] if x
        ).strip() if acteur else notification.acteur_username
        phrase = bodies.get(notification.type, "a interagi avec ton contenu")
        body = f"{acteur_nom} {phrase}"
        extrait = (notification.extrait or "").strip()
        if extrait and notification.type != "mention_classe":
            body = f"{body} · {extrait}"[:240]
        payload = {
            "title": "GRIND",
            "body": body[:240],
            "data": _message_data(notification),
        }
        par_destinataire.setdefault(notification.destinataire_username, []).append(payload)

    rows = db.query(GrindPushToken).filter(
        GrindPushToken.username.in_(list(par_destinataire))
    ).all()
    messages = [
        (row.token, payload)
        for row in rows
        for payload in par_destinataire.get(row.username, [])
    ]
    if not messages:
        return

    try:
        invalid = await asyncio.to_thread(
            _envoyer_batch_fcm, project_id, service_account_info, messages
        )
        if invalid:
            db.query(GrindPushToken).filter(
                GrindPushToken.token.in_(list(invalid))
            ).delete(synchronize_session=False)
            db.commit()
    except Exception as exc:
        logger.warning("Le relais FCM a échoué sans interrompre GRIND : %s", exc)


async def envoyer_notifications_push_par_ids(notification_ids):
    """Tâche de fond : recharge les notifications dans une session DB indépendante."""
    ids = [int(x) for x in (notification_ids or []) if x]
    if not ids:
        return
    from database import SessionLocal
    from models import NotificationGrind
    db = SessionLocal()
    try:
        notifications = db.query(NotificationGrind).filter(NotificationGrind.id.in_(ids)).all()
        await envoyer_notifications_push(db, notifications)
    except Exception as erreur:
        logger.warning("Notification push différée ignorée: %s", type(erreur).__name__)
    finally:
        db.close()
