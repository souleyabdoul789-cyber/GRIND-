"""
websocket_routes.py — canaux temps réel de GRIND.

/ws/notifs?api_key=...              : notifications globales
/ws/session/{demande_id}?api_key=... : signalisation WebRTC + feuille + permissions

L'identité (username) n'est JAMAIS prise dans l'URL — elle vient
toujours de la vérification de la clé auprès de Pluton, pour éviter
qu'un client se fasse passer pour quelqu'un d'autre en changeant
l'URL.

Types de message côté client -> serveur, sur /ws/session/{demande_id} :
- "webrtc-offer" / "webrtc-answer" / "webrtc-ice"  → relayés tels quels, avec `cible` = username destinataire
- "sheet-update"                                    → contenu de la feuille de l'émetteur, relayé à tous
- "demande-voir-feuille"                            → { cible: <username dont on veut voir la feuille> }, relayé au "prof"
- "autorisation-voir-feuille"                       → envoyé par le "prof", { cible, autorise: true/false }, relayé au demandeur original
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from pluton_client import verifier_cle
from ws_manager import manager

router = APIRouter()


@router.websocket("/ws/notifs")
async def ws_notifs(websocket: WebSocket, api_key: str = Query(...)):
    valide, username = await verifier_cle(api_key)
    if not valide:
        await websocket.close(code=1008)
        return

    await manager.connect_notif(username, websocket)
    try:
        while True:
            await websocket.receive_text()  # canal descendant uniquement pour l'instant
    except WebSocketDisconnect:
        manager.disconnect_notif(username)


@router.websocket("/ws/session/{demande_id}")
async def ws_session(websocket: WebSocket, demande_id: int, api_key: str = Query(...)):
    valide, username = await verifier_cle(api_key)
    if not valide:
        await websocket.close(code=1008)
        return

    await manager.connect_session(demande_id, username, websocket)
    try:
        while True:
            msg = await websocket.receive_json()
            type_ = msg.get("type")

            if type_ in ("webrtc-offer", "webrtc-answer", "webrtc-ice"):
                await manager.relay(demande_id, username, msg, cible=msg.get("cible"))

            elif type_ == "sheet-update":
                await manager.relay(demande_id, username, msg)

            elif type_ == "demande-voir-feuille":
                await manager.relay(demande_id, username, msg, cible=msg.get("cible"))

            elif type_ == "autorisation-voir-feuille":
                await manager.relay(demande_id, username, msg, cible=msg.get("cible"))

            else:
                await websocket.send_json({"type": "error", "message": f"Type de message inconnu: {type_}"})

    except WebSocketDisconnect:
        manager.disconnect_session(demande_id, username)
