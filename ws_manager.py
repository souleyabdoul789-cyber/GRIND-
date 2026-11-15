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

import asyncio

from fastapi import WebSocket


class ConnectionManager:
    def __init__(self):
        self.notif_connections: dict[str, WebSocket] = {}
        self.session_connections: dict[int, dict[str, WebSocket]] = {}
        self.discussion_connections: dict[int, dict[str, set[WebSocket]]] = {}
        self._discussion_send_locks: dict[int, asyncio.Lock] = {}
        self._discussion_send_users: dict[int, int] = {}
        self._discussion_queues: dict[int, asyncio.Queue] = {}
        self._discussion_workers: dict[int, asyncio.Task] = {}
        self._taches_diffusion_discussion: set[asyncio.Task] = set()

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

    async def notifier_participant_rejoint(self, demande_id: int, username: str, role: str, destinataire: str):
        payload = {"type": "participant_rejoint", "demande_id": demande_id, "username": username, "role": role}
        ws = self.notif_connections.get(destinataire)
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

    async def notifier_utilisateur(self, username: str, payload: dict):
        """Envoie une notification à un utilisateur précis, s'il est connecté."""
        ws = self.notif_connections.get(username)
        if ws is None:
            return
        try:
            await ws.send_json(payload)
        except Exception:
            self.notif_connections.pop(username, None)

    async def notifier_annonce(self, payload: dict, exclure: str | None = None):
        """Diffuse une annonce officielle à tous les utilisateurs connectés."""
        for username, ws in list(self.notif_connections.items()):
            if username == exclure:
                continue
            try:
                await ws.send_json({**payload, "type": "annonce"})
            except Exception:
                self.notif_connections.pop(username, None)

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

    async def envoyer_session(self, demande_id: int, cible: str, payload: dict):
        """Envoie un événement privé à un membre connecté d'une salle."""
        ws = self.session_connections.get(demande_id, {}).get(cible)
        if ws is None:
            return False
        try:
            await ws.send_json(payload)
            return True
        except Exception:
            self.disconnect_session(demande_id, cible)
            return False

    async def notifier_depart(self, demande_id: int, username_parti: str):
        """Prévient les participants restants qu'un membre vient de quitter
        la salle — sans ça, l'UI de l'autre le montrerait encore présent."""
        participants = self.session_connections.get(demande_id, {})
        for username, ws in participants.items():
            if username != username_parti:
                await ws.send_json({"type": "peer-parti", "sender": username_parti})

    # ---------- Discussions privées liées aux classes ----------
    async def connect_discussion(self, discussion_id: int, username: str, ws: WebSocket):
        await ws.accept()
        self.discussion_connections.setdefault(discussion_id, {}).setdefault(username, set()).add(ws)

    def disconnect_discussion(self, discussion_id: int, username: str, ws: WebSocket):
        groupes = self.discussion_connections.get(discussion_id, {})
        connexions = groupes.get(username)
        if connexions is not None:
            connexions.discard(ws)
            if not connexions:
                groupes.pop(username, None)
        if not groupes:
            self.discussion_connections.pop(discussion_id, None)
        cle_socket = id(ws)
        if self._discussion_send_users.get(cle_socket, 0) == 0:
            self._discussion_send_locks.pop(cle_socket, None)
            self._discussion_send_users.pop(cle_socket, None)

    def planifier_diffusion_discussion(self, discussion_id: int, payload: dict, exclure: str | None = None):
        """Met en file la diffusion après commit, sans ralentir l'accusé HTTP.

        Un seul worker par discussion vide la file dans l'ordre d'arrivée. Les
        connexions destinataires sont ensuite notifiées en parallèle. Les tâches
        sont suivies et les exceptions consommées ; REST reste le filet de sécurité.
        """
        queue = self._discussion_queues.setdefault(discussion_id, asyncio.Queue())
        queue.put_nowait((payload, exclure))
        worker = self._discussion_workers.get(discussion_id)
        if worker is None or worker.done():
            worker = asyncio.create_task(self._vider_file_diffusion_discussion(discussion_id, queue))
            self._discussion_workers[discussion_id] = worker
            self._taches_diffusion_discussion.add(worker)

            def terminer(tache: asyncio.Task):
                self._taches_diffusion_discussion.discard(tache)
                try:
                    tache.result()
                except asyncio.CancelledError:
                    pass
                except Exception:
                    # Le transport WebSocket ne doit jamais annuler le commit SQL.
                    pass
                if self._discussion_workers.get(discussion_id) is tache:
                    self._discussion_workers.pop(discussion_id, None)
                    if queue.empty():
                        self._discussion_queues.pop(discussion_id, None)
                    else:
                        # Un événement a pu arriver entre la dernière lecture et
                        # le callback de fin : ne jamais laisser la file endormie.
                        self.planifier_reprise_file_diffusion(discussion_id, queue)

            worker.add_done_callback(terminer)
        return worker

    async def _vider_file_diffusion_discussion(self, discussion_id: int, queue: asyncio.Queue):
        while True:
            try:
                payload, exclure = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                await self.diffuser_discussion(discussion_id, payload, exclure)
            except Exception:
                # Le prochain message ne doit pas rester bloqué par un événement WS défaillant.
                pass
            finally:
                queue.task_done()

    def planifier_reprise_file_diffusion(self, discussion_id: int, queue: asyncio.Queue):
        worker = asyncio.create_task(self._vider_file_diffusion_discussion(discussion_id, queue))
        self._discussion_workers[discussion_id] = worker
        self._taches_diffusion_discussion.add(worker)

        def terminer(tache: asyncio.Task):
            self._taches_diffusion_discussion.discard(tache)
            try:
                tache.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            if self._discussion_workers.get(discussion_id) is tache:
                self._discussion_workers.pop(discussion_id, None)
                if queue.empty():
                    self._discussion_queues.pop(discussion_id, None)
                else:
                    self.planifier_reprise_file_diffusion(discussion_id, queue)

        worker.add_done_callback(terminer)

    async def diffuser_discussion(self, discussion_id: int, payload: dict, exclure: str | None = None):
        """Diffuse en parallèle vers les connexions actives et retire les sockets mortes."""
        groupes = self.discussion_connections.get(discussion_id, {})
        cibles = [
            (username, ws)
            for username, connexions in list(groupes.items())
            if username != exclure
            for ws in list(connexions)
        ]
        if not cibles:
            return

        async def envoyer(username, ws):
            cle_socket = id(ws)
            verrou = self._discussion_send_locks.setdefault(cle_socket, asyncio.Lock())
            self._discussion_send_users[cle_socket] = self._discussion_send_users.get(cle_socket, 0) + 1
            try:
                # Un seul écrivain par socket : évite les trames concurrentes
                # et conserve l'ordre des messages créés dans le même process.
                async with verrou:
                    await asyncio.wait_for(ws.send_json(payload), timeout=5.0)
                return None
            except Exception:
                return username, ws
            finally:
                actifs = max(0, self._discussion_send_users.get(cle_socket, 1) - 1)
                if actifs:
                    self._discussion_send_users[cle_socket] = actifs
                else:
                    self._discussion_send_users.pop(cle_socket, None)
                    groupes_actifs = self.discussion_connections.get(discussion_id, {})
                    encore_connectee = ws in groupes_actifs.get(username, set())
                    if not encore_connectee:
                        self._discussion_send_locks.pop(cle_socket, None)

        resultats = await asyncio.gather(*(envoyer(username, ws) for username, ws in cibles))
        for resultat in resultats:
            if resultat is not None:
                username, ws = resultat
                self.disconnect_discussion(discussion_id, username, ws)

    async def retirer_connexions_discussion(self, discussion_id: int, username: str):
        """Révoque immédiatement toutes les sockets d'un membre retiré du groupe."""
        groupes = self.discussion_connections.get(discussion_id, {})
        connexions = list(groupes.get(username, set()))
        for ws in connexions:
            try:
                await ws.close(code=1008, reason="Accès à la discussion révoqué")
            except Exception:
                pass
            self.disconnect_discussion(discussion_id, username, ws)

    async def fermer_discussion(self, discussion_id: int):
        """Ferme les connexions d'un groupe supprimé."""
        groupes = self.discussion_connections.get(discussion_id, {})
        for username in list(groupes):
            await self.retirer_connexions_discussion(discussion_id, username)


manager = ConnectionManager()
