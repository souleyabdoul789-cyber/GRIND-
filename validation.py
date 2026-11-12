"""
validation.py — validation côté serveur des données de compte.

Le frontend peut afficher des indices, mais JAMAIS décider : tout est revérifié ici.
Les messages sont destinés à l'utilisateur et ne révèlent rien de sensible.
"""

import re

from scolaire import ErreurValidation

# ------------------------------------------------------------------ pseudo

_PSEUDO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,22}[A-Za-z0-9]$")


def valider_pseudo(texte: str) -> str:
    pseudo = (texte or "").strip()
    if not (3 <= len(pseudo) <= 24):
        raise ErreurValidation("Le pseudo doit faire entre 3 et 24 caractères.")
    if not _PSEUDO.match(pseudo):
        raise ErreurValidation("Le pseudo ne peut contenir que des lettres, des chiffres et . _ - (sans commencer ni finir par un symbole).")
    if re.search(r"[._-]{2,}", pseudo):
        raise ErreurValidation("Le pseudo ne peut pas contenir deux symboles de suite.")
    if pseudo.isdigit():
        raise ErreurValidation("Le pseudo doit contenir des lettres.")
    return pseudo


# ------------------------------------------------------------------ mot de passe

_MOTS_DE_PASSE_FAIBLES = {
    "password", "passw0rd", "motdepasse", "motdepasse1", "azertyuiop", "azerty123", "qwertyuiop", "qwerty123",
    "12345678", "123456789", "1234567890", "00000000", "11111111", "abcdefgh", "abcd1234", "iloveyou", "grind123",
    "grindgrind", "gsociety", "gsociety123", "bonjour123", "soleil123", "admin1234", "azertyui", "motdepasse123",
}


def valider_mot_de_passe(mot_de_passe: str, pseudo: str = "") -> str:
    mdp = mot_de_passe or ""
    if len(mdp) < 8:
        raise ErreurValidation("Le mot de passe doit faire au moins 8 caractères.")
    if len(mdp.encode("utf-8")) > 72:
        raise ErreurValidation("Le mot de passe est trop long (72 octets maximum).")
    if not mdp.strip():
        raise ErreurValidation("Le mot de passe ne peut pas être vide.")
    bas = mdp.lower()
    if bas in _MOTS_DE_PASSE_FAIBLES:
        raise ErreurValidation("Ce mot de passe est trop courant. Choisis-en un autre.")
    if re.fullmatch(r"(.)\1+", mdp):
        raise ErreurValidation("Le mot de passe ne peut pas répéter le même caractère.")
    if pseudo and len(pseudo) >= 3 and pseudo.lower() in bas:
        raise ErreurValidation("Le mot de passe ne doit pas contenir ton pseudo.")
    classes = sum(
        bool(re.search(p, mdp)) for p in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^A-Za-z0-9]")
    )
    if classes < 2 and len(mdp) < 12:
        raise ErreurValidation("Ajoute des chiffres, des majuscules ou des symboles (ou fais un mot de passe d'au moins 12 caractères).")
    if mdp.isdigit():
        raise ErreurValidation("Le mot de passe ne peut pas être composé uniquement de chiffres.")
    return mdp


# ------------------------------------------------------------------ e-mail

_EMAIL = re.compile(
    r"^[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)

# Adresses jetables courantes : elles permettraient de créer des comptes sans personne derrière.
_DOMAINES_JETABLES = {
    "mailinator.com", "yopmail.com", "yopmail.fr", "guerrillamail.com", "10minutemail.com", "tempmail.com",
    "temp-mail.org", "trashmail.com", "sharklasers.com", "getnada.com", "maildrop.cc", "dispostable.com",
    "throwawaymail.com", "fakeinbox.com", "moakt.com", "mohmal.com", "tempmailo.com", "emailondeck.com",
}


def valider_email(texte: str) -> str:
    """Renvoie l'adresse normalisée (minuscules) ou lève une erreur lisible."""
    adresse = (texte or "").strip().lower()
    if not adresse or len(adresse) > 254:
        raise ErreurValidation("Indique une adresse e-mail valide.")
    local, _, domaine = adresse.partition("@")
    if not _EMAIL.match(adresse) or len(local) > 64 or ".." in local or local.startswith(".") or local.endswith("."):
        raise ErreurValidation("Indique une adresse e-mail valide.")
    if len(domaine.rsplit(".", 1)[-1]) < 2:
        raise ErreurValidation("Indique une adresse e-mail valide.")
    if domaine in _DOMAINES_JETABLES:
        raise ErreurValidation("Les adresses e-mail jetables ne sont pas acceptées.")
    return adresse


def email_canonique(adresse: str) -> str:
    """Forme servant à l'UNICITÉ : deux écritures de la même boîte Gmail (points, +étiquette) donnent la même clé."""
    adresse = adresse.strip().lower()
    local, _, domaine = adresse.partition("@")
    if domaine in ("gmail.com", "googlemail.com"):
        local = local.split("+", 1)[0].replace(".", "")
        domaine = "gmail.com"
    return f"{local}@{domaine}"


def masquer_email(adresse: str) -> str:
    """ex****l@gmail.com — jamais l'adresse complète dans un message de confirmation."""
    local, _, domaine = (adresse or "").partition("@")
    if not domaine:
        return "***"
    n = len(local)
    if n >= 5:
        visible = local[:2] + "****" + local[-1]
    elif n >= 3:
        visible = local[0] + "***" + local[-1]
    else:
        visible = (local[:1] or "*") + "***"
    return f"{visible}@{domaine}"
