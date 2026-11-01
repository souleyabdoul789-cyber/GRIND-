"""
moderation.py — rôles, permissions et clé d'administration de GRIND.

QUI EST QUI
- admin       : UN seul compte. On y entre avec la clé d'administration à la
                place du mot de passe (voir routes_compte.py). Il nomme l'équipe,
                publie les annonces, a tous les droits.
- associe     : équipe de confiance — modération complète + certification.
- controleur  : équipe de modération — traite les signalements, suspensions courtes.
- (aucun)     : élève ordinaire.

LA CLÉ D'ADMINISTRATION
Elle n'est JAMAIS dans le code ni dans la base. Le serveur ne connaît que son
empreinte SHA-256, dans la variable d'environnement GRIND_ADMIN_KEY_HASH
(Render). La clé elle-même n'existe que chez son propriétaire. Elle est
volontairement très longue et aléatoire (voir generer_cle_admin.py) : avec
autant d'entropie, une empreinte SHA-256 ne peut pas être devinée par force brute.
Pour la changer : générer une nouvelle clé, remplacer l'empreinte sur Render.
"""

import hashlib
import hmac
import os
from datetime import datetime, timezone

from models import Sanction, CompteGrind

# À changer quand le texte des CGU change : tous les comptes devront
# alors les accepter de nouveau à leur prochaine connexion.
CGU_VERSION = "2026-10-31"

ROLE_ADMIN = "admin"
ROLE_ASSOCIE = "associe"
ROLE_CONTROLEUR = "controleur"
ROLES_EQUIPE = (ROLE_ASSOCIE, ROLE_CONTROLEUR)
TOUS_LES_ROLES = (ROLE_ADMIN,) + ROLES_EQUIPE

SUSPENSION_COURTE_MAX_JOURS = 7

_BASE_CONTROLEUR = {"signalements", "utilisateurs", "suspension_courte"}
_BASE_ASSOCIE = _BASE_CONTROLEUR | {"suspension", "bannissement", "lever_sanction", "certification"}
PERMISSIONS = {
    ROLE_CONTROLEUR: _BASE_CONTROLEUR,
    ROLE_ASSOCIE: _BASE_ASSOCIE,
    ROLE_ADMIN: _BASE_ASSOCIE | {"equipe", "annonces"},
}

# Le compte admin a un pseudo fixe, réservé : personne ne peut s'inscrire avec.
ADMIN_USERNAME = "grind-admin"
NOMS_RESERVES = {
    ADMIN_USERNAME, "grind admin", "grindadmin", "admin", "administrateur",
    "administration", "moderateur", "modérateur", "controleur", "contrôleur", "associe", "associé",
}
NOM_ADMIN_PAR_DEFAUT = "GRIND Admin"


def nom_reserve(username: str) -> bool:
    return username.strip().lower() in NOMS_RESERVES


# ------------------------------------------------------------------ clé admin

def cle_admin_valide(cle: str) -> bool:
    attendu = os.getenv("GRIND_ADMIN_KEY_HASH", "").strip().lower()
    if len(attendu) != 64 or not cle:
        return False
    empreinte = hashlib.sha256(cle.encode()).hexdigest()
    return hmac.compare_digest(empreinte, attendu)  # comparaison à temps constant


# ------------------------------------------------------------------ rôles

def role_de(db, username: str):
    compte = db.get(CompteGrind, username)
    return compte.role if compte and compte.role in TOUS_LES_ROLES else None


def a_permission(role, permission: str) -> bool:
    return role in PERMISSIONS and permission in PERMISSIONS[role]


def badge_de(db, username: str) -> dict:
    """Ce qu'on affiche à côté d'un pseudo : rôle d'équipe et/ou certification."""
    compte = db.get(CompteGrind, username)
    if compte is None:
        return {"role": None, "certifie": False}
    role = compte.role if compte.role in TOUS_LES_ROLES else None
    return {"role": role, "certifie": bool(compte.certifie)}


# ------------------------------------------------------------------ sanctions

def _avec_fuseau(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def sanction_active(db, username: str):
    """Renvoie la sanction qui s'applique en ce moment à ce compte, sinon None."""
    maintenant = datetime.now(timezone.utc)
    sanctions = (
        db.query(Sanction)
        .filter(Sanction.username == username, Sanction.levee_le.is_(None))
        .order_by(Sanction.created_at.desc())
        .all()
    )
    for s in sanctions:
        if s.type == "bannissement":
            return s
        fin = _avec_fuseau(s.jusqu_a)
        if fin is None or fin > maintenant:
            return s
    return None


def message_sanction(sanction) -> str:
    if sanction.type == "bannissement":
        return f"Ce compte a été banni de GRIND. Motif : {sanction.motif}"
    fin = _avec_fuseau(sanction.jusqu_a)
    date = fin.strftime("%d/%m/%Y à %H:%M UTC") if fin else "une date indéterminée"
    return f"Ce compte est suspendu jusqu'au {date}. Motif : {sanction.motif}"
