"""
websocket_routes.py — canaux temps réel de GRIND.

/ws/notifs?session_token=...              : notifications globales
/ws/session/{demande_id}?session_token=... : signalisation WebRTC + feuille + permissions

L'identité vient toujours de la session GRIND (auth_grind.verifier_session),
jamais d'un champ pris dans l'URL par un client.

Types de message côté client -> serveur, sur /ws/session/{demande_id} :
- "webrtc-offer" / "webrtc-answer" / "webrtc-ice"  → relayés tels quels, avec `cible` = username destinataire
- "sheet-update"                                    → contenu de la feuille de l'émetteur, relayé à tous
- "demande-voir-feuille"                            → { cible: <username dont on veut voir la feuille> }, relayé au "prof"
- "autorisation-voir-feuille"                       → envoyé par le "prof", { cible, autorise: true/false }, relayé au demandeur original
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from database import SessionLocal
from auth_grind import verifier_session
from ws_manager import manager

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
async def ws_session(websocket: WebSocket, demande_id: int, session_token: str = Query(...)):
    db = SessionLocal()
    try:
        username = verifier_session(session_token, db)
    finally:
        db.close()

    if username is None:
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
