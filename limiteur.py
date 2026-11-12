"""
limiteur.py — freine les essais de connexion en rafale (par adresse IP).

Protège les mots de passe ET la clé d'administration contre la force brute :
au-delà de MAX_ECHECS échecs dans la FENETRE, la connexion est refusée un moment.
En mémoire (comme ws_manager) : suffisant pour une seule instance du serveur.
"""

import time
from collections import defaultdict, deque

FENETRE_SECONDES = 600
MAX_ECHECS = 10

_echecs: dict[str, deque] = defaultdict(deque)


def _purger(ip: str) -> deque:
    limite = time.time() - FENETRE_SECONDES
    file = _echecs[ip]
    while file and file[0] < limite:
        file.popleft()
    return file


def trop_d_echecs(ip: str) -> bool:
    return len(_purger(ip)) >= MAX_ECHECS


def noter_echec(ip: str) -> None:
    _purger(ip).append(time.time())


def oublier(ip: str) -> None:
    _echecs.pop(ip, None)


_compteurs: dict[str, deque] = defaultdict(deque)


def autoriser(cle: str, maximum: int, fenetre_secondes: int) -> bool:
    """Compte une action (inscription, demande de code...) et dit si elle est encore permise :
    au plus `maximum` fois par fenêtre glissante. Renvoie False — sans compter — quand la limite est atteinte."""
    maintenant = time.time()
    file = _compteurs[cle]
    while file and file[0] < maintenant - fenetre_secondes:
        file.popleft()
    if len(file) >= maximum:
        return False
    file.append(maintenant)
    return True
