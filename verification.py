"""
verification.py — vérification d'adresse e-mail et réinitialisation de mot de passe.

Un « flux » (table verifications_email) suit UNE démarche : inscription ou réinitialisation.
Le client ne connaît que le jeton du flux (aléatoire, 256 bits) ; le serveur n'en garde que l'empreinte.

Règles (toutes appliquées ICI, côté serveur) :
  - code à 7 chiffres tiré avec `secrets` (générateur cryptographique), jamais écrit dans les journaux ;
  - le code n'est stocké que sous forme d'empreinte salée ; valable 10 minutes ; à usage unique ;
  - 3 essais par code : au 3e échec le code est ANNULÉ, il faut en demander un nouveau ;
  - un nouvel envoi au plus toutes les 60 s, 5 envois maximum par flux ; le flux lui-même expire après 30 min ;
  - FLUX FANTÔMES : pour une adresse inconnue (mot de passe oublié) ou déjà utilisée (inscription), on crée un
    faux flux qui se comporte exactement comme un vrai (compteurs, expiration, messages) mais ne peut jamais
    aboutir : personne ne peut deviner, en observant les réponses, si une adresse a un compte.

La logique de décision est dans des fonctions PURES (preparer_envoi, controler_code) qui ne touchent qu'à
l'objet « flux » : elles se testent sans base de données.
"""

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

DUREE_CODE_MIN = 10
DUREE_FLUX_MIN = 30
MAX_TENTATIVES = 3
MAX_ENVOIS = 5
ATTENTE_RENVOI_S = 60
FENETRE_REINIT_MIN = 15  # une fois le code validé, on a 15 minutes pour choisir le nouveau mot de passe


def maintenant() -> datetime:
    return datetime.now(timezone.utc)


def avec_fuseau(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def generer_code() -> str:
    return f"{secrets.randbelow(10 ** 7):07d}"


def _empreinte(code: str, sel: str) -> str:
    return hashlib.sha256((sel + code).encode()).hexdigest()


def empreinte_jeton(jeton: str) -> str:
    return hashlib.sha256((jeton or "").encode()).hexdigest()


# ------------------------------------------------------------------ logique pure

def preparer_envoi(flux, maintenant_: datetime):
    """Décide si un code peut être (ré)envoyé et prépare le flux.
    Renvoie ("ok", code | None) — code None pour un flux fantôme —, ("trop_tot", secondes) ou ("trop_d_envois", None)."""
    if (flux.nb_envois or 0) >= MAX_ENVOIS:
        return "trop_d_envois", None
    dernier = avec_fuseau(flux.dernier_envoi_le)
    if dernier is not None:
        ecoule = (maintenant_ - dernier).total_seconds()
        if ecoule < ATTENTE_RENVOI_S:
            return "trop_tot", int(ATTENTE_RENVOI_S - ecoule) + 1

    code = generer_code()
    flux.code_sel = secrets.token_hex(8)
    flux.code_hash = _empreinte(code, flux.code_sel)
    flux.expire_le = maintenant_ + timedelta(minutes=DUREE_CODE_MIN)
    flux.tentatives = 0
    flux.nb_envois = (flux.nb_envois or 0) + 1
    flux.dernier_envoi_le = maintenant_
    # Un flux fantôme garde un code que PERSONNE ne connaît : il ne pourra jamais être validé.
    return "ok", (None if flux.fantome else code)


def controler_code(flux, code: str, maintenant_: datetime):
    """Contrôle un code saisi. Renvoie un couple (résultat, info) :
       ("ok", None) | ("incorrect", essais_restants) | ("annule", None) | ("expire", None)
    Le flux est modifié (essais comptés, code annulé) : l'appelant DOIT enregistrer avant de répondre."""
    if not flux.code_hash:
        return "expire", None
    fin = avec_fuseau(flux.expire_le)
    if fin is None or maintenant_ > fin:
        flux.code_hash = None
        return "expire", None

    attendu = flux.code_hash
    donne = _empreinte(code, flux.code_sel or "")
    if not flux.fantome and hmac.compare_digest(donne, attendu):
        flux.code_hash = None  # à usage unique
        return "ok", None

    flux.tentatives = (flux.tentatives or 0) + 1
    if flux.tentatives >= MAX_TENTATIVES:
        flux.code_hash = None  # trop d'essais : le code est annulé
        return "annule", None
    return "incorrect", MAX_TENTATIVES - flux.tentatives


def flux_utilisable(flux, maintenant_: datetime) -> bool:
    return flux.utilise_le is None and avec_fuseau(flux.flux_expire_le) is not None and maintenant_ <= avec_fuseau(flux.flux_expire_le)


def reinit_autorisee(flux, maintenant_: datetime) -> bool:
    """Le code de réinitialisation a-t-il été validé il y a moins de 15 minutes ?"""
    valide = avec_fuseau(flux.code_valide_le)
    return (
        not flux.fantome
        and flux.objet in {"reinitialisation", "pin_reinitialisation"}
        and valide is not None
        and (maintenant_ - valide) <= timedelta(minutes=FENETRE_REINIT_MIN)
        and flux_utilisable(flux, maintenant_)
    )


# ------------------------------------------------------------------ accès base de données

def creer_flux(db, modele, objet: str, email: str, username: str | None, ip: str | None, fantome: bool = False):
    """Crée un flux et renvoie (flux, jeton_en_clair)."""
    jeton = secrets.token_urlsafe(32)
    maintenant_ = maintenant()
    flux = modele(
        objet=objet,
        username=username,
        email=email or "",
        jeton_hash=empreinte_jeton(jeton),
        tentatives=0,
        nb_envois=0,
        fantome=1 if fantome else 0,
        flux_expire_le=maintenant_ + timedelta(minutes=DUREE_FLUX_MIN),
        ip=(ip or "")[:64],
    )
    if fantome:
        # Un code existe (il se comporte comme un vrai) mais n'est connu de personne.
        flux.code_sel = secrets.token_hex(8)
        flux.code_hash = _empreinte(generer_code(), flux.code_sel)
        flux.expire_le = maintenant_ + timedelta(minutes=DUREE_CODE_MIN)
        flux.nb_envois = 1
        flux.dernier_envoi_le = maintenant_
    db.add(flux)
    db.commit()
    db.refresh(flux)
    return flux, jeton


def trouver_flux(db, modele, jeton: str):
    return db.query(modele).filter(modele.jeton_hash == empreinte_jeton(jeton)).first()
