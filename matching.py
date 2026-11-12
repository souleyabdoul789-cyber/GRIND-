"""
matching.py — scoring réel pour orienter une demande d'aide vers
les bons candidats.

Logique :
1. On exclut quiconque a déclaré CE sujet dans ses propres difficultés
   (il galère dessus lui aussi, pas un bon aidant).
2. On note les candidats restants :
   +3 si même pays (programme scolaire souvent proche)
   +2 si même niveau/filière exact
3. On notifie seulement les N meilleurs, pas tout le monde.

Limite connue : le matching par niveau est un match EXACT sur la
chaîne ("Terminale S" == "Terminale S"), pas une notion de proximité
("3ème" proche de "4ème"). Pour une vraie proximité il faudrait un
référentiel de niveaux scolaires structuré (liste ordonnée par pays) —
à construire une fois qu'on a des vraies données d'usage pour savoir
si ça vaut le coup.
"""

import json
from sqlalchemy.orm import Session
from models import Profil

NB_CANDIDATS_MAX = 10


def _parser_difficultes(brut: str | None) -> list[str]:
    if not brut:
        return []
    try:
        data = json.loads(brut)
        if isinstance(data, list):
            return [str(x).strip().lower() for x in data]
    except ValueError:
        pass
    # fallback : chaîne "maths, physique" séparée par virgules
    return [x.strip().lower() for x in brut.split(",") if x.strip()]


def trouver_meilleurs_candidats(db: Session, demandeur_username: str, sujet: str) -> list[str]:
    demandeur = db.get(Profil, demandeur_username)
    if demandeur is None:
        return []  # pas de profil = pas de contexte pour scorer, on ne notifie personne

    sujet_norm = sujet.strip().lower()
    candidats = db.query(Profil).filter(Profil.username != demandeur_username).all()

    scores: list[tuple[int, str]] = []
    for c in candidats:
        if sujet_norm in _parser_difficultes(c.difficultes):
            continue  # lui-même en difficulté sur ce sujet, on l'exclut

        score = 0
        if c.pays == demandeur.pays:
            score += 3
        if c.niveau == demandeur.niveau:
            score += 2

        if score > 0:  # on ne notifie pas des gens sans aucun point commun
            scores.append((score, c.username))

    scores.sort(key=lambda x: x[0], reverse=True)
    return [username for _, username in scores[:NB_CANDIDATS_MAX]]
