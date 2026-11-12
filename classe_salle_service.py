"""Règles de persistance de la salle scolaire (10 membres maximum).

Le propriétaire de la classe reste createur_username. Le professeur actuel est
prof_username et peut changer sans transférer la propriété. Les élèves gardent
un pupitre déterministe en base, indépendant de l'ordre d'arrivée LiveKit.
"""

from models import ClasseMembre

NB_SIEGES_ELEVES = 9


def professeur_actuel(classe, membres=None):
    membres = membres if membres is not None else []
    candidat = getattr(classe, "prof_username", None)
    if candidat and any(m.username == candidat for m in membres):
        return candidat
    ancien_prof = next((m.username for m in membres if m.role == "prof"), None)
    return ancien_prof or classe.createur_username


def assurer_prof_et_sieges(db, classe):
    """Répare progressivement les classes historiques sans changer leur capacité."""
    membres = db.query(ClasseMembre).filter_by(classe_id=classe.id).order_by(ClasseMembre.id.asc()).all()
    prof = professeur_actuel(classe, membres)
    if getattr(classe, "prof_username", None) != prof:
        classe.prof_username = prof

    prof_membre = next((m for m in membres if m.username == prof), None)
    if prof_membre is None and membres:
        prof = classe.createur_username
        prof_membre = next((m for m in membres if m.username == prof), None)
        classe.prof_username = prof

    if prof_membre is not None:
        prof_membre.role = "prof"
        prof_membre.siege_index = None

    occupes = set()
    for membre in membres:
        if membre.username == prof:
            continue
        index = membre.siege_index
        if index is not None and 0 <= index < NB_SIEGES_ELEVES and index not in occupes:
            occupes.add(index)
        else:
            membre.siege_index = None

    for membre in membres:
        if membre.username == prof:
            continue
        if membre.siege_index is None:
            libre = next((i for i in range(NB_SIEGES_ELEVES) if i not in occupes), None)
            if libre is not None:
                membre.siege_index = libre
                occupes.add(libre)
        if membre.role == "prof":
            membre.role = "membre"

    return membres


def prochain_siege(db, classe_id: int) -> int | None:
    # La session GRIND utilise autoflush=False : pousser d'abord les sièges
    # attribués en mémoire par assurer_prof_et_sieges pour éviter un doublon
    # lorsqu'un ancien groupe est réparé puis qu'un élève le rejoint.
    db.flush()
    pris = {
        row[0]
        for row in db.query(ClasseMembre.siege_index).filter(
            ClasseMembre.classe_id == classe_id,
            ClasseMembre.siege_index.isnot(None),
        ).all()
    }
    return next((i for i in range(NB_SIEGES_ELEVES) if i not in pris), None)
