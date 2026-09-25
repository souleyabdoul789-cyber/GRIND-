"""
ws_manager.py — connexions WebSocket en mémoire.

Deux usages distincts :
1. Notifications globales (ex: "nouvelle demande d'aide sur ton sujet")
2. Canal par session d'aide/classe : signalisation WebRTC (offer/answer/ice)
   + synchronisation de la feuille + permissions de visibilité.

Limite connue : en mémoire, donc si le process Render redémarre (mise en
veille du plan gratuit), les connexions actives tombent — les clients
devront se reconnecter. Acceptable pour un prototype, à revoir avec du
Redis pub/sub si un jour plusieurs instances du serveur tournent en //.
"""

from fastapi import WebSocket


class ConnectionManager:
    def __init__(self):
        self.notif_connections: dict[str, WebSocket] = {}
        self.session_connections: dict[int, dict[str, WebSocket]] = {}

    # ---------- Notifications globales ----------
    async def connect_notif(self, username: str, ws: WebSocket):
        await ws.accept()
        self.notif_connections[username] = ws

    def disconnect_notif(self, username: str):
        self.notif_connections.pop(username, None)

    async def notifier_nouvelle_demande(self, demande_id: int, sujet: str, demandeur: str, candidats: list[str]):
        payload = {
            "type": "nouvelle_demande",
            "demande_id": demande_id,
            "sujet": sujet,
            "demandeur": demandeur,
        }
        for username in candidats:
            ws = self.notif_connections.get(username)
            if ws:
                await ws.send_json(payload)

    async def notifier_participant_rejoint(self, demande_id: int, username: str, role: str):
        payload = {"type": "participant_rejoint", "demande_id": demande_id, "username": username, "role": role}
        ws = self.notif_connections.get(username)
        if ws:
            await ws.send_json(payload)

    async def notifier_nouveau_membre_classe(self, classe_id: int, nom_classe: str, createur: str, nouveau_membre: str):
        payload = {
            "type": "nouveau_membre_classe",
            "classe_id": classe_id,
            "nom_classe": nom_classe,
            "nouveau_membre": nouveau_membre,
        }
        ws = self.notif_connections.get(createur)
        if ws:
            await ws.send_json(payload)

    # ---------- Canal de session (aide en cours / classe) ----------
    async def connect_session(self, demande_id: int, username: str, ws: WebSocket):
        await ws.accept()
        self.session_connections.setdefault(demande_id, {})[username] = ws

    def disconnect_session(self, demande_id: int, username: str):
        if demande_id in self.session_connections:
            self.session_connections[demande_id].pop(username, None)
            if not self.session_connections[demande_id]:
                del self.session_connections[demande_id]

    async def relay(self, demande_id: int, sender: str, message: dict, cible: str | None = None):
        """Relaie un message aux autres participants de la session.
        Si `cible` est précisé, envoie seulement à ce participant
        (ex: réponse WebRTC destinée à une personne précise)."""
        participants = self.session_connections.get(demande_id, {})
        message = {**message, "sender": sender}

        if cible:
            ws = participants.get(cible)
            if ws:
                await ws.send_json(message)
            return

        for username, ws in participants.items():
            if username != sender:
                await ws.send_json(message)

    async def notifier_depart(self, demande_id: int, username_parti: str):
        """Prévient les participants restants qu'un membre vient de quitter
        la salle — sans ça, l'UI de l'autre le montrerait encore présent."""
        participants = self.session_connections.get(demande_id, {})
        for username, ws in participants.items():
            if username != username_parti:
                await ws.send_json({"type": "peer-parti", "sender": username_parti})


manager = ConnectionManager()
