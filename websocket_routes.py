"""Canaux WebSocket GRIND.

Le canal /ws/session/... sert aux salles d'aide et aux salles scolaires. Le
mode `classe=1` exige une adhésion réelle et applique les permissions de
feuille/tableau côté serveur avant toute diffusion.
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from database import SessionLocal
from auth_grind import verifier_session
from ws_manager import manager
from models import (
    Classe, ClasseMembre, ClasseFeuille, ClasseFeuillePermission, ClasseTableau,
    ClasseDiscussionMembre,
)

router = APIRouter()


@router.websocket("/ws/notifs")
async def ws_notifs(websocket: WebSocket, session_token: str = Query(...)):
    db = SessionLocal()
    try:
        username = verifier_session(session_token, db)
    finally:
        db.close()

    if username is None:
        await websocket.close(code=1008)
        return

    await manager.connect_notif(username, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect_notif(username)


@router.websocket("/ws/session/{demande_id}")
async def ws_session(
    websocket: WebSocket,
    demande_id: int,
    session_token: str = Query(...),
    classe: bool = Query(False),
):
    db = SessionLocal()
    try:
        username = verifier_session(session_token, db)
        if classe and username:
            membre = db.query(ClasseMembre).filter_by(classe_id=demande_id, username=username).first()
            classe_obj = db.get(Classe, demande_id)
            if membre is None or classe_obj is None:
                username = None
    finally:
        db.close()

    if username is None:
        await websocket.close(code=1008)
        return

    # Un namespace négatif isole les salles scolaires des demandes d'aide,
    # même si leurs identifiants numériques coïncident.
    canal_id = -demande_id if classe else demande_id
    await manager.connect_session(canal_id, username, websocket)
    try:
        while True:
            msg = await websocket.receive_json()
            type_ = msg.get("type")

            # L'identité émettrice est toujours celle de la session vérifiée.
            if type_ in ("webrtc-offer", "webrtc-answer", "webrtc-ice"):
                cible = msg.get("cible")
                if classe:
                    db = SessionLocal()
                    try:
                        sender_ok = db.query(ClasseMembre).filter_by(classe_id=demande_id, username=username).first() is not None
                        cible_ok = (not cible) or db.query(ClasseMembre).filter_by(classe_id=demande_id, username=cible).first() is not None
                    finally:
                        db.close()
                    if not sender_ok or not cible_ok:
                        await websocket.send_json({"type": "error", "message": "Participant hors de cette classe."})
                        continue
                await manager.relay(canal_id, username, msg, cible=cible)
                continue

            if type_ in ("ready", "etat-micro"):
                if classe:
                    db = SessionLocal()
                    try:
                        membre_actif = db.query(ClasseMembre).filter_by(classe_id=demande_id, username=username).first() is not None
                    finally:
                        db.close()
                    if not membre_actif:
                        await websocket.close(code=1008, reason="Accès à la classe révoqué")
                        return
                await manager.relay(canal_id, username, msg)
                continue

            if not classe:
                await websocket.send_json({"type": "error", "message": "Événement réservé aux salles de classe."})
                continue

            db = SessionLocal()
            try:
                membre = db.query(ClasseMembre).filter_by(classe_id=demande_id, username=username).first()
                classe_obj = db.get(Classe, demande_id)
                if membre is None or classe_obj is None:
                    await websocket.close(code=1008, reason="Accès à la classe révoqué")
                    return
                professeur = classe_obj.prof_username or classe_obj.createur_username

                if type_ == "sheet-update":
                    contenu = str(msg.get("contenu") or "")
                    if len(contenu) > 50000:
                        await websocket.send_json({"type": "error", "message": "La feuille dépasse 50 000 caractères."})
                        continue
                    feuille = db.query(ClasseFeuille).filter_by(classe_id=demande_id, username=username).first()
                    if feuille is None:
                        feuille = ClasseFeuille(classe_id=demande_id, username=username, contenu=contenu)
                        db.add(feuille)
                    else:
                        feuille.contenu = contenu
                    db.commit()
                    autorises = db.query(ClasseFeuillePermission.lecteur_username).filter_by(
                        classe_id=demande_id, proprietaire_username=username
                    ).all()
                    payload = {"type": "sheet-update", "sender": username, "contenu": contenu}
                    for (lecteur,) in autorises:
                        await manager.envoyer_session(canal_id, lecteur, payload)

                elif type_ == "demande-voir-feuille":
                    cible = str(msg.get("cible") or "")
                    cible_ok = db.query(ClasseMembre).filter_by(classe_id=demande_id, username=cible).first()
                    if not cible_ok or cible == username:
                        await websocket.send_json({"type": "error", "message": "Ce membre ne peut pas recevoir cette demande."})
                        continue
                    await manager.envoyer_session(canal_id, cible, {
                        "type": "demande-voir-feuille", "sender": username,
                    })

                elif type_ == "autorisation-voir-feuille":
                    cible = str(msg.get("cible") or "")
                    autorise = bool(msg.get("autorise"))
                    cible_ok = db.query(ClasseMembre).filter_by(classe_id=demande_id, username=cible).first()
                    if not cible_ok or cible == username:
                        await websocket.send_json({"type": "error", "message": "Membre cible invalide."})
                        continue
                    permission = db.query(ClasseFeuillePermission).filter_by(
                        classe_id=demande_id, proprietaire_username=username, lecteur_username=cible
                    ).first()
                    if autorise:
                        if permission is None:
                            db.add(ClasseFeuillePermission(
                                classe_id=demande_id, proprietaire_username=username, lecteur_username=cible
                            ))
                        feuille = db.query(ClasseFeuille).filter_by(classe_id=demande_id, username=username).first()
                        contenu = feuille.contenu if feuille else ""
                    else:
                        if permission is not None:
                            db.delete(permission)
                        contenu = ""
                    db.commit()
                    await manager.envoyer_session(canal_id, cible, {
                        "type": "autorisation-voir-feuille", "sender": username,
                        "autorise": autorise, "contenu": contenu,
                    })

                elif type_ == "sheet-revoke":
                    cible = str(msg.get("cible") or "")
                    permission = db.query(ClasseFeuillePermission).filter_by(
                        classe_id=demande_id, proprietaire_username=username, lecteur_username=cible
                    ).first()
                    if permission is not None:
                        db.delete(permission)
                        db.commit()
                    await manager.envoyer_session(canal_id, cible, {
                        "type": "feuille-acces-revoque", "sender": username,
                    })

                elif type_ == "blackboard-update":
                    if username != professeur:
                        await websocket.send_json({"type": "error", "message": "Seul le professeur actuel peut écrire au tableau."})
                        continue
                    contenu = str(msg.get("contenu") or "")
                    if len(contenu) > 3000:
                        await websocket.send_json({"type": "error", "message": "Le tableau est limité à 3 000 caractères."})
                        continue
                    tableau = db.get(ClasseTableau, demande_id)
                    if tableau is None:
                        tableau = ClasseTableau(classe_id=demande_id, contenu=contenu, updated_by=username)
                        db.add(tableau)
                    else:
                        tableau.contenu = contenu
                        tableau.updated_by = username
                    db.commit()
                    await manager.relay(canal_id, username, {
                        "type": "blackboard-update", "contenu": contenu,
                    })

                elif type_ == "demande-devenir-prof":
                    cible = str(msg.get("cible") or "")
                    if cible != professeur or username == professeur:
                        await websocket.send_json({"type": "error", "message": "Demande de transfert invalide."})
                        continue
                    await manager.envoyer_session(canal_id, professeur, {
                        "type": "demande-devenir-prof", "sender": username,
                    })

                elif type_ == "reponse-devenir-prof":
                    cible = str(msg.get("cible") or "")
                    if username != professeur or not cible:
                        await websocket.send_json({"type": "error", "message": "Seul le professeur actuel peut répondre."})
                        continue
                    cible_ok = db.query(ClasseMembre).filter_by(classe_id=demande_id, username=cible).first()
                    if not cible_ok:
                        await websocket.send_json({"type": "error", "message": "Demandeur hors de cette classe."})
                        continue
                    if not bool(msg.get("accepte")):
                        await manager.envoyer_session(canal_id, cible, {
                            "type": "reponse-devenir-prof", "sender": username, "accepte": False,
                        })
                    else:
                        await websocket.send_json({"type": "info", "message": "Valide le transfert avec l'API de la classe."})

                elif type_ == "prof-change":
                    if str(msg.get("nouveau_prof") or "") != professeur:
                        await websocket.send_json({"type": "error", "message": "Changement de professeur non validé."})
                        continue
                    await manager.relay(canal_id, username, {
                        "type": "prof-change", "nouveau_prof": professeur,
                    })
                else:
                    await websocket.send_json({"type": "error", "message": f"Type de message inconnu: {type_}"})
            finally:
                db.close()

    except WebSocketDisconnect:
        manager.disconnect_session(canal_id, username)
        await manager.notifier_depart(canal_id, username)


@router.websocket("/ws/discussion/{discussion_id}")
async def ws_discussion(websocket: WebSocket, discussion_id: int, session_token: str = Query(...)):
    """Canal temps réel indépendant de LiveKit pour la messagerie des classes."""
    db = SessionLocal()
    try:
        username = verifier_session(session_token, db)
        membre = db.query(ClasseDiscussionMembre).filter_by(
            discussion_id=discussion_id, username=username, actif=1
        ).first() if username else None
        restreint = bool(membre and membre.restreint)
        jusqua = membre.restriction_jusqua if membre else None
        if restreint and jusqua is not None:
            from datetime import datetime
            fin = jusqua.replace(tzinfo=None) if jusqua.tzinfo else jusqua
            if fin <= datetime.utcnow():
                membre.restreint = 0
                membre.restriction_jusqua = None
                db.commit()
                restreint = False
    finally:
        db.close()

    if not username or not membre:
        await websocket.close(code=1008)
        return

    await manager.connect_discussion(discussion_id, username, websocket)
    try:
        while True:
            message = await websocket.receive_json()
            type_ = message.get("type")
            if type_ == "typing":
                await manager.diffuser_discussion(discussion_id, {
                    "type": "typing", "username": username,
                    "typing": bool(message.get("typing")),
                }, exclure=username)
            elif type_ == "ping":
                await websocket.send_json({"type": "pong"})
            else:
                await websocket.send_json({"type": "error", "message": "Événement non pris en charge."})
    except WebSocketDisconnect:
        manager.disconnect_discussion(discussion_id, username, websocket)
