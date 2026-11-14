"""Canaux WebSocket GRIND.

Le canal /ws/session/... sert aux salles d'aide et aux salles scolaires. Le
mode `classe=1` exige une adhésion réelle et applique les permissions de
feuille/tableau côté serveur avant toute diffusion.
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from datetime import datetime, timedelta
from database import SessionLocal
from auth_grind import verifier_session
from ws_manager import manager
from classe_salle_etendue import (
    ClasseFeuilleVersion, ClasseTableauVersion, ClasseTableauDelegation,
    duree_tableau_securisee, delegation_tableau_active, utc_naive,
    eleve_connecte_dans_salle, raison_fin_delegation,
)
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
                        precedent = feuille.contenu or ""
                        if precedent.strip() and precedent != contenu:
                            derniere_version = db.query(ClasseFeuilleVersion).filter_by(
                                classe_id=demande_id, username=username
                            ).order_by(ClasseFeuilleVersion.id.desc()).first()
                            instant_precedent = utc_naive(derniere_version.created_at) if derniere_version and derniere_version.created_at else None
                            if (derniere_version is None or
                                    (derniere_version.contenu != precedent and
                                     (not contenu.strip() or instant_precedent is None or utc_naive() - instant_precedent >= timedelta(seconds=30)))):
                                db.add(ClasseFeuilleVersion(classe_id=demande_id, username=username, contenu=precedent))
                                db.flush()
                                for ancienne in db.query(ClasseFeuilleVersion).filter_by(
                                    classe_id=demande_id, username=username
                                ).order_by(ClasseFeuilleVersion.id.desc()).offset(100).all():
                                    db.delete(ancienne)
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

                elif type_ == "tableau-inviter":
                    # Verrouille la classe pour sérialiser deux invitations simultanées
                    # (par exemple deux onglets ouverts par le professeur).
                    classe_verrouillee = db.query(Classe).filter_by(id=demande_id).with_for_update().first()
                    if classe_verrouillee is None:
                        await websocket.send_json({"type": "error", "message": "Classe introuvable."})
                        continue
                    professeur = classe_verrouillee.prof_username or classe_verrouillee.createur_username
                    if username != professeur:
                        await websocket.send_json({"type": "error", "message": "Seul le professeur actuel peut inviter un élève au tableau."})
                        continue
                    cible = str(msg.get("cible") or "")
                    cible_ok = db.query(ClasseMembre).filter_by(classe_id=demande_id, username=cible).first()
                    socket_cible = eleve_connecte_dans_salle(manager.session_connections, canal_id, cible)
                    if not cible or cible == professeur or cible_ok is None or not socket_cible:
                        await websocket.send_json({"type": "error", "message": "Cet élève n'est pas présent dans la salle."})
                        continue
                    maintenant = utc_naive()
                    delegation = db.get(ClasseTableauDelegation, demande_id)
                    if delegation is not None:
                        expiration = delegation.invitation_expire_at if delegation.statut == "invite" else delegation.expire_at
                        expiration = utc_naive(expiration) if expiration else None
                        # Un transfert permanent de professeur rend automatiquement
                        # caduque toute ancienne délégation, même non expirée.
                        if delegation.prof_username == professeur and expiration is not None and expiration > maintenant:
                            await websocket.send_json({"type": "error", "message": "Une invitation ou une prise de main est déjà en cours."})
                            continue
                        db.delete(delegation)
                        db.flush()
                    duree = duree_tableau_securisee(msg.get("duree_secondes", 180))
                    delegation = ClasseTableauDelegation(
                        classe_id=demande_id,
                        eleve_username=cible,
                        prof_username=professeur,
                        statut="invite",
                        duree_secondes=duree,
                        invitation_expire_at=maintenant + timedelta(seconds=60),
                        expire_at=None,
                    )
                    db.add(delegation)
                    db.commit()
                    await manager.envoyer_session(canal_id, cible, {
                        "type": "tableau-invitation", "sender": username,
                        "duree_secondes": duree,
                        "invitation_expire_at": delegation.invitation_expire_at.isoformat(),
                    })
                    pending_payload = {
                        "eleve_username": cible, "statut": "invite", "duree_secondes": duree,
                        "invitation_expire_at": delegation.invitation_expire_at.isoformat(),
                    }
                    await manager.relay(canal_id, username, {
                        "type": "tableau-delegation", "delegation": pending_payload,
                    })
                    await websocket.send_json({"type": "tableau-delegation", "delegation": pending_payload})

                elif type_ == "tableau-invitation-reponse":
                    delegation = db.query(ClasseTableauDelegation).filter_by(
                        classe_id=demande_id
                    ).with_for_update().first()
                    expiration = utc_naive(delegation.invitation_expire_at) if delegation and delegation.invitation_expire_at else None
                    professeur_connecte = eleve_connecte_dans_salle(
                        manager.session_connections, canal_id, professeur
                    )
                    if (delegation is None or delegation.eleve_username != username
                            or delegation.statut != "invite" or delegation.prof_username != professeur
                            or expiration is None or expiration <= utc_naive() or not professeur_connecte):
                        await websocket.send_json({"type": "error", "message": "Cette invitation au tableau a expiré ou n'est plus valable."})
                        continue
                    if not bool(msg.get("accepte")):
                        db.delete(delegation)
                        db.commit()
                        await manager.relay(canal_id, username, {"type": "tableau-delegation-terminee", "raison": "refusee", "eleve_username": username})
                        await manager.envoyer_session(canal_id, professeur, {"type": "tableau-invitation-terminee", "eleve_username": username, "accepte": False})
                        continue
                    delegation.statut = "actif"
                    delegation.expire_at = utc_naive() + timedelta(seconds=duree_tableau_securisee(delegation.duree_secondes))
                    delegation.invitation_expire_at = None
                    db.commit()
                    payload = {
                        "eleve_username": username,
                        "prof_username": professeur,
                        "statut": "actif",
                        "duree_secondes": int(delegation.duree_secondes or 180),
                        "expire_at": delegation.expire_at.isoformat() if delegation.expire_at else None,
                    }
                    await manager.relay(canal_id, username, {"type": "tableau-delegation-active", "delegation": payload})
                    await websocket.send_json({"type": "tableau-delegation-active", "delegation": payload})

                elif type_ == "tableau-reprendre":
                    if username != professeur:
                        await websocket.send_json({"type": "error", "message": "Seul le professeur actuel peut reprendre le tableau."})
                        continue
                    delegation = db.query(ClasseTableauDelegation).filter_by(
                        classe_id=demande_id
                    ).with_for_update().first()
                    ancien_eleve = delegation.eleve_username if delegation else None
                    if delegation is not None:
                        db.delete(delegation)
                        db.commit()
                    termine_payload = {
                        "type": "tableau-delegation-terminee", "raison": "reprise_professeur",
                        "eleve_username": ancien_eleve,
                    }
                    await manager.relay(canal_id, username, termine_payload)
                    await websocket.send_json(termine_payload)

                elif type_ == "blackboard-update":
                    delegation = db.query(ClasseTableauDelegation).filter_by(
                        classe_id=demande_id
                    ).with_for_update().first()
                    professeur_connecte = eleve_connecte_dans_salle(
                        manager.session_connections, canal_id, professeur
                    )
                    autorise_eleve = (
                        delegation_tableau_active(delegation, username, professeur)
                        and professeur_connecte
                    )
                    if username != professeur and not autorise_eleve:
                        if delegation is not None and delegation.statut == "actif":
                            expiration = utc_naive(delegation.expire_at) if delegation.expire_at else None
                            if expiration is None or expiration <= utc_naive() or delegation.prof_username != professeur:
                                db.delete(delegation)
                                db.commit()
                                fin_payload = {
                                    "type": "tableau-delegation-terminee", "raison": "expiree",
                                    "eleve_username": delegation.eleve_username,
                                }
                                await manager.relay(canal_id, username, fin_payload)
                                await manager.envoyer_session(canal_id, professeur, fin_payload)
                        await websocket.send_json({"type": "error", "message": "Tu n'as pas la main au tableau. Seul le professeur ou l'élève invité peut écrire."})
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
                        precedent = tableau.contenu or ""
                        if precedent.strip() and precedent != contenu:
                            derniere_version = db.query(ClasseTableauVersion).filter_by(
                                classe_id=demande_id
                            ).order_by(ClasseTableauVersion.id.desc()).first()
                            instant_precedent = utc_naive(derniere_version.created_at) if derniere_version and derniere_version.created_at else None
                            if (derniere_version is None or
                                    (derniere_version.contenu != precedent and
                                     (not contenu.strip() or instant_precedent is None or utc_naive() - instant_precedent >= timedelta(seconds=30)))):
                                db.add(ClasseTableauVersion(
                                    classe_id=demande_id,
                                    auteur_username=tableau.updated_by or professeur,
                                    contenu=precedent,
                                ))
                                db.flush()
                                for ancienne in db.query(ClasseTableauVersion).filter_by(
                                    classe_id=demande_id
                                ).order_by(ClasseTableauVersion.id.desc()).offset(150).all():
                                    db.delete(ancienne)
                        tableau.contenu = contenu
                        tableau.updated_by = username
                    delegation_terminee = None
                    if username == professeur and delegation is not None:
                        delegation_terminee = delegation.eleve_username
                        db.delete(delegation)
                    db.commit()
                    if delegation_terminee:
                        await manager.relay(canal_id, username, {
                            "type": "tableau-delegation-terminee",
                            "raison": "reprise_professeur",
                            "eleve_username": delegation_terminee,
                        })
                    await manager.relay(canal_id, username, {
                        "type": "blackboard-update", "contenu": contenu,
                        "auteur": username,
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
        # Un départ capture les derniers contenus confirmés côté serveur, même
        # si le délai de pause côté navigateur n'a pas encore expiré.
        # La déconnexion du professeur révoque également la main temporaire.
        if classe:
            db_fin = SessionLocal()
            delegation_fin_payload = None
            try:
                classe_fin = db_fin.get(Classe, demande_id)
                if classe_fin is not None:
                    feuille_fin = db_fin.query(ClasseFeuille).filter_by(
                        classe_id=demande_id, username=username
                    ).first()
                    if feuille_fin is not None and (feuille_fin.contenu or "").strip():
                        derniere_feuille = db_fin.query(ClasseFeuilleVersion).filter_by(
                            classe_id=demande_id, username=username
                        ).order_by(ClasseFeuilleVersion.id.desc()).first()
                        if derniere_feuille is None or derniere_feuille.contenu != feuille_fin.contenu:
                            db_fin.add(ClasseFeuilleVersion(
                                classe_id=demande_id, username=username, contenu=feuille_fin.contenu
                            ))
                            db_fin.flush()
                            for ancienne in db_fin.query(ClasseFeuilleVersion).filter_by(
                                classe_id=demande_id, username=username
                            ).order_by(ClasseFeuilleVersion.id.desc()).offset(100).all():
                                db_fin.delete(ancienne)

                    tableau_fin = db_fin.get(ClasseTableau, demande_id)
                    if tableau_fin is not None and (tableau_fin.contenu or "").strip():
                        derniere_tableau = db_fin.query(ClasseTableauVersion).filter_by(
                            classe_id=demande_id
                        ).order_by(ClasseTableauVersion.id.desc()).first()
                        if derniere_tableau is None or derniere_tableau.contenu != tableau_fin.contenu:
                            db_fin.add(ClasseTableauVersion(
                                classe_id=demande_id,
                                auteur_username=tableau_fin.updated_by or (classe_fin.prof_username or classe_fin.createur_username),
                                contenu=tableau_fin.contenu,
                            ))
                            db_fin.flush()
                            for ancienne in db_fin.query(ClasseTableauVersion).filter_by(
                                classe_id=demande_id
                            ).order_by(ClasseTableauVersion.id.desc()).offset(150).all():
                                db_fin.delete(ancienne)

                    prof_fin = classe_fin.prof_username or classe_fin.createur_username
                    delegation_fin = db_fin.query(ClasseTableauDelegation).filter_by(
                        classe_id=demande_id
                    ).with_for_update().first()
                    raison_fin = raison_fin_delegation(delegation_fin, username, prof_fin)
                    if raison_fin:
                        delegation_fin_payload = {
                            "type": "tableau-delegation-terminee",
                            "raison": raison_fin,
                            "eleve_username": delegation_fin.eleve_username,
                        }
                        db_fin.delete(delegation_fin)
                    db_fin.commit()
            except Exception:
                db_fin.rollback()
            finally:
                db_fin.close()
            if delegation_fin_payload:
                await manager.relay(canal_id, username, delegation_fin_payload)
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
            if fin <= utc_naive():
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
