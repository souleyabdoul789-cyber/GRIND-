"""
scolaire.py — la SEULE source de vérité sur les données scolaires de GRIND.

Objectif : qu'aucune donnée absurde ou incohérente (« kskdkdkaks », une matière inventée,
« Philosophie » en 6e...) n'entre dans l'écosystème. Tout est validé ICI, côté serveur ; le
frontend lit ce catalogue (GET /api/scolaire/catalogue) pour proposer des choix, mais ne décide de rien.

- Niveaux et matières viennent de catalogues FERMÉS : on choisit, on ne tape pas.
- Les seuls textes libres (complément d'un nom de classe) passent par un contrôle de plausibilité.
- Tout est normalisé (accents, majuscules, espaces) pour éviter les doublons.
"""

import re
import unicodedata

# Âge d'accès à GRIND. À confirmer avec un juriste selon le droit applicable (consentement parental...).
AGE_MIN = 11
AGE_MAX = 60


class ErreurValidation(ValueError):
    """Erreur destinée à l'utilisateur : le message est affiché tel quel."""


# ------------------------------------------------------------------ utilitaires

def sans_accents(texte: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texte) if unicodedata.category(c) != "Mn")


def cle(texte: str) -> str:
    """Clé de comparaison : minuscules, sans accents, lettres/chiffres séparés par un seul espace."""
    return re.sub(r"[^a-z0-9]+", " ", sans_accents(texte).lower()).strip()


def compact(texte: str) -> str:
    return cle(texte).replace(" ", "")


# ------------------------------------------------------------------ niveaux

CYCLE_COLLEGE = "college"
CYCLE_LYCEE = "lycee"
CYCLE_SUPERIEUR = "superieur"

NIVEAUX = [
    {"id": "6e", "label": "6e", "cycle": CYCLE_COLLEGE},
    {"id": "5e", "label": "5e", "cycle": CYCLE_COLLEGE},
    {"id": "4e", "label": "4e", "cycle": CYCLE_COLLEGE},
    {"id": "3e", "label": "3e", "cycle": CYCLE_COLLEGE},
    {"id": "2nde", "label": "2nde", "cycle": CYCLE_LYCEE},
    {"id": "1re", "label": "1re", "cycle": CYCLE_LYCEE},
    {"id": "terminale", "label": "Terminale", "cycle": CYCLE_LYCEE},
    {"id": "licence1", "label": "Licence 1", "cycle": CYCLE_SUPERIEUR},
    {"id": "licence2", "label": "Licence 2", "cycle": CYCLE_SUPERIEUR},
    {"id": "licence3", "label": "Licence 3", "cycle": CYCLE_SUPERIEUR},
    {"id": "master1", "label": "Master 1", "cycle": CYCLE_SUPERIEUR},
    {"id": "master2", "label": "Master 2", "cycle": CYCLE_SUPERIEUR},
    {"id": "doctorat", "label": "Doctorat", "cycle": CYCLE_SUPERIEUR},
]
_NIVEAU_PAR_ID = {n["id"]: n for n in NIVEAUX}

# Séries du lycée (modèle francophone d'Afrique de l'Ouest et de France) : facultatives.
FILIERES = ["A", "B", "C", "D", "E", "F1", "F2", "F3", "F4", "G1", "G2", "G3", "S", "L", "ES"]
_FILIERES_PAR_CLE = {f.lower(): f for f in FILIERES}

_ALIAS_NIVEAUX = {
    "6e": "6e", "6eme": "6e", "6": "6e", "sixieme": "6e",
    "5e": "5e", "5eme": "5e", "5": "5e", "cinquieme": "5e",
    "4e": "4e", "4eme": "4e", "4": "4e", "quatrieme": "4e",
    "3e": "3e", "3eme": "3e", "3": "3e", "troisieme": "3e",
    "2nde": "2nde", "2de": "2nde", "2": "2nde", "seconde": "2nde", "2eme": "2nde",
    "1re": "1re", "1ere": "1re", "1": "1re", "premiere": "1re",
    "terminale": "terminale", "tle": "terminale", "term": "terminale", "t": "terminale",
    "licence1": "licence1", "l1": "licence1", "licence2": "licence2", "l2": "licence2",
    "licence3": "licence3", "l3": "licence3", "master1": "master1", "m1": "master1",
    "master2": "master2", "m2": "master2", "doctorat": "doctorat", "doctorant": "doctorat", "phd": "doctorat",
}


def niveau_par_label(label: str):
    """Renvoie le dict du niveau pour un label canonique ('3e', 'Terminale D'...), ou None."""
    base = normaliser_niveau(label)
    if base is None:
        return None
    return _NIVEAU_PAR_ID[base["id"]] if isinstance(base, dict) else None


def normaliser_niveau(texte: str):
    """'terminale d', 'Tle D', '3ème', 'troisieme'... -> {'id', 'label', 'cycle', 'filiere'} ou None.
    Une série (filière) n'est acceptée que pour 2nde / 1re / Terminale."""
    if not texte:
        return None
    sc = compact(texte)
    meilleur = None
    for alias in sorted(_ALIAS_NIVEAUX, key=len, reverse=True):
        if sc.startswith(alias):
            meilleur = alias
            break
    if meilleur is None:
        return None
    niveau = _NIVEAU_PAR_ID[_ALIAS_NIVEAUX[meilleur]]
    reste = sc[len(meilleur):]
    filiere = None
    if reste:
        if niveau["cycle"] != CYCLE_LYCEE or reste not in _FILIERES_PAR_CLE:
            return None
        filiere = _FILIERES_PAR_CLE[reste]
    label = niveau["label"] + (f" {filiere}" if filiere else "")
    return {"id": niveau["id"], "label": label, "cycle": niveau["cycle"], "filiere": filiere}


def valider_niveau(texte: str) -> dict:
    n = normaliser_niveau(texte)
    if n is None:
        raise ErreurValidation("Choisis ton niveau dans la liste (6e, 5e... Terminale, Licence 1...).")
    return n


# ------------------------------------------------------------------ matières

# (label canonique, cycles, niveaux autorisés si restriction)
_MATIERES = [
    ("Mathématiques", {CYCLE_COLLEGE, CYCLE_LYCEE, CYCLE_SUPERIEUR}, None),
    ("Français", {CYCLE_COLLEGE, CYCLE_LYCEE}, None),
    ("Anglais", {CYCLE_COLLEGE, CYCLE_LYCEE, CYCLE_SUPERIEUR}, None),
    ("Histoire-Géographie", {CYCLE_COLLEGE, CYCLE_LYCEE}, None),
    ("Éducation civique et morale", {CYCLE_COLLEGE}, None),
    ("Sciences de la vie et de la Terre", {CYCLE_COLLEGE, CYCLE_LYCEE}, None),
    ("Physique-Chimie", {CYCLE_COLLEGE, CYCLE_LYCEE}, None),
    ("Informatique", {CYCLE_COLLEGE, CYCLE_LYCEE, CYCLE_SUPERIEUR}, None),
    ("Éducation physique et sportive", {CYCLE_COLLEGE, CYCLE_LYCEE}, None),
    ("Espagnol", {CYCLE_COLLEGE, CYCLE_LYCEE}, None),
    ("Allemand", {CYCLE_COLLEGE, CYCLE_LYCEE}, None),
    ("Arabe", {CYCLE_COLLEGE, CYCLE_LYCEE}, None),
    ("Philosophie", {CYCLE_LYCEE, CYCLE_SUPERIEUR}, {"1re", "terminale"}),  # pas avant la 1re
    ("Sciences économiques et sociales", {CYCLE_LYCEE}, None),
    ("Comptabilité", {CYCLE_LYCEE, CYCLE_SUPERIEUR}, None),
    ("Gestion", {CYCLE_LYCEE, CYCLE_SUPERIEUR}, None),
    ("Électricité", {CYCLE_LYCEE}, None),
    ("Mécanique", {CYCLE_LYCEE, CYCLE_SUPERIEUR}, None),
    ("Dessin technique", {CYCLE_LYCEE}, None),
    ("Physique", {CYCLE_SUPERIEUR}, None),
    ("Chimie", {CYCLE_SUPERIEUR}, None),
    ("Biologie", {CYCLE_SUPERIEUR}, None),
    ("Algorithmique", {CYCLE_SUPERIEUR}, None),
    ("Statistiques", {CYCLE_SUPERIEUR}, None),
    ("Économie", {CYCLE_SUPERIEUR}, None),
    ("Droit", {CYCLE_SUPERIEUR}, None),
    ("Sociologie", {CYCLE_SUPERIEUR}, None),
    ("Histoire", {CYCLE_SUPERIEUR}, None),
    ("Géographie", {CYCLE_SUPERIEUR}, None),
    ("Expression écrite et orale", {CYCLE_SUPERIEUR}, None),
]
_MATIERE_PAR_CLE = {cle(m[0]): m for m in _MATIERES}

_ALIAS_MATIERES = {
    "maths": "Mathématiques", "math": "Mathématiques", "mathematique": "Mathématiques",
    "fr": "Français", "ang": "Anglais", "english": "Anglais",
    "hg": "Histoire-Géographie", "histoire geo": "Histoire-Géographie", "hist geo": "Histoire-Géographie",
    "histoire geographie": "Histoire-Géographie",
    "ecm": "Éducation civique et morale", "education civique": "Éducation civique et morale",
    "instruction civique": "Éducation civique et morale", "civique": "Éducation civique et morale",
    "svt": "Sciences de la vie et de la Terre", "sciences naturelles": "Sciences de la vie et de la Terre",
    "sciences de la vie et de la terre": "Sciences de la vie et de la Terre",
    "pc": "Physique-Chimie", "physique chimie": "Physique-Chimie", "sciences physiques": "Physique-Chimie",
    "info": "Informatique", "eps": "Éducation physique et sportive", "sport": "Éducation physique et sportive",
    "education physique": "Éducation physique et sportive", "philo": "Philosophie",
    "ses": "Sciences économiques et sociales", "compta": "Comptabilité", "eco": "Économie",
    "electrotechnique": "Électricité", "dt": "Dessin technique", "algo": "Algorithmique",
    "stats": "Statistiques", "geo": "Géographie", "eeo": "Expression écrite et orale",
    "techniques d expression": "Expression écrite et orale",
}


def normaliser_matiere(texte: str):
    """'maths', 'Physique chimie', 'SVT'... -> label canonique, ou None si inconnue."""
    k = cle(texte or "")
    if not k:
        return None
    if k in _MATIERE_PAR_CLE:
        return _MATIERE_PAR_CLE[k][0]
    alias = _ALIAS_MATIERES.get(k)
    return alias


def matieres_pour(niveau: dict) -> list[str]:
    """Matières proposées pour un niveau (dict renvoyé par normaliser_niveau)."""
    resultat = []
    for label, cycles, restreint in _MATIERES:
        if niveau["cycle"] in cycles and (restreint is None or niveau["id"] in restreint):
            resultat.append(label)
    return resultat


def valider_matiere(texte: str, niveau: dict | None = None) -> str:
    label = normaliser_matiere(texte)
    if label is None:
        raise ErreurValidation(
            f"Matière non reconnue : « {_apercu(texte)} ». Choisis dans la liste ; "
            "s'il en manque une, écris-nous depuis Paramètres › Contactez-nous."
        )
    if niveau is not None and label not in matieres_pour(niveau):
        raise ErreurValidation(f"« {label} » ne correspond pas au niveau {niveau['label']}.")
    return label


def valider_difficultes(brut, niveau: dict | None = None) -> list[str]:
    """Accepte une liste ou un texte « a, b, c » (ancien format). Renvoie la liste canonique, sans doublon."""
    if brut is None:
        raise ErreurValidation("Choisis au moins une matière.")
    if isinstance(brut, str):
        texte = brut.strip()
        if texte.startswith("["):
            import json

            try:
                elements = json.loads(texte)
            except ValueError:
                raise ErreurValidation("Liste de matières illisible.")
        else:
            elements = [e for e in texte.split(",")]
    else:
        elements = list(brut)
    elements = [str(e).strip() for e in elements if str(e).strip()]
    if not elements:
        raise ErreurValidation("Choisis au moins une matière.")
    if len(elements) > 8:
        raise ErreurValidation("8 matières au maximum.")
    resultat = []
    for e in elements:
        label = valider_matiere(e, niveau)
        if label not in resultat:
            resultat.append(label)
    return resultat


def _apercu(texte: str, n: int = 30) -> str:
    texte = " ".join(str(texte).split())
    return texte if len(texte) <= n else texte[:n] + "…"


# ------------------------------------------------------------------ plausibilité des noms libres

_VOYELLES = set("aeiouy")
_CONSONNES = set("bcdfghjklmnpqrstvwxz")

# Sigles scolaires courants : ils n'ont pas de voyelles ou des suites inhabituelles, mais sont légitimes.
_SIGLES = {
    "svt", "eps", "pc", "mpc", "bepc", "bac", "cep", "cap", "bts", "dut", "ecm", "ses", "hg", "tice", "cpge",
    "ufr", "tle", "mp", "ts", "tl", "tsm", "tse", "tsexp", "cm", "cfa", "cnc", "dts", "bt", "lmd",
}

# Paires de consonnes qui existent en français, anglais, noms arabes / haoussa / zarma translittérés.
_PAIRES_OK = set("""
bl br bs bt ch cl cr ct cq cs dj dh dl dr ds dw fl fr fs ft gh gl gn gr gs hm hr kh kl kr ks lb lc ld lf lg
lk ll lm lp ls lt lv mb mm mn mp ms nc nd nf ng nj nk nn ns nt nv nx pl pn pp pr ps pt rb rc rd rf rg rh rk
rl rm rn rp rr rs rt rv rw rz sc sh sk sl sm sn sp sq st sw tc th tr ts tt tw tz vr wh wr wn ws xc xp xt xq
zz zh bb cc dd ff gg ml mr nr nz tl yb ye
""".split())


def _paires_inconnues(mot: str) -> int:
    n = 0
    for a, b in zip(mot, mot[1:]):
        if a in _CONSONNES and b in _CONSONNES and (a + b) not in _PAIRES_OK:
            n += 1
    return n


def _plus_longue_suite(mot: str, ensemble: set) -> int:
    meilleur = courant = 0
    for c in mot:
        courant = courant + 1 if c in ensemble else 0
        meilleur = max(meilleur, courant)
    return meilleur


_LETTRES_RARES = set("jkqwxz")
_RANGEES_CLAVIER = ("azertyuiop", "qsdfghjklm", "wxcvbn", "qwertyuiop", "asdfghjkl", "zxcvbnm")
_SUITES_CLAVIER = {r[i:i + 5] for r in _RANGEES_CLAVIER + tuple(x[::-1] for x in _RANGEES_CLAVIER) for i in range(len(r) - 4)}
_TRIPLES_VOYELLES_OK = ("eau", "ieu", "oui", "iou", "aie", "uie", "eux", "oeu", "ous", "ial", "iat", "ien", "ion", "uou")
_SEUIL_REJET = 1.5


def mot_plausible(mot: str) -> bool:
    """Un mot (lettres minuscules sans accents) ressemble-t-il à un vrai mot ?

    Heuristique par score — suites de consonnes impossibles, manque de voyelles, lettres rares en
    nombre, répétitions, motifs de type « kdkd » — réglée sur ~6 000 vrais mots (≈ 2,6 % de faux rejets,
    uniquement des mots composés rares) et sur des écrasements de clavier (≈ 86 % refusés). Elle ne
    peut pas attraper un mot absurde mais prononçable ; c'est pourquoi les niveaux et les matières
    viennent de catalogues fermés, et que seul le COMPLÉMENT du nom d'une classe est en texte libre."""
    n = len(mot)
    if n <= 2 or mot in _SIGLES:
        return True
    nb_voyelles = sum(c in _VOYELLES for c in mot)
    if n == 3:
        return nb_voyelles >= 1
    if nb_voyelles == 0:
        return False

    # Les digrammes (ch, sch, th, ph, qu...) comptent comme UN son, pas comme une suite de consonnes.
    simple = re.sub(r"sch|tch|ch|sh|th|ph|gh|kh|ck|qu", "S", mot)
    voyelles_pleines = set("aeiou")
    voyelles_effectives = sum(c in voyelles_pleines for c in simple) + 0.5 * simple.count("y")
    ratio = voyelles_effectives / len(simple)

    score = 0.0
    if ratio < 0.25:
        score += 1
    if ratio > 0.8:
        score += 1
    suite_consonnes = _plus_longue_suite(simple, _CONSONNES | {"S"})
    score += 2 if suite_consonnes >= 5 else (1 if suite_consonnes == 4 else 0)
    suite_voyelles = _plus_longue_suite(simple, voyelles_pleines | {"y"})
    score += 2 if suite_voyelles >= 5 else (1 if suite_voyelles == 4 else 0)
    if suite_voyelles == 3 and not any(t in simple for t in _TRIPLES_VOYELLES_OK):
        score += 0.7
    if re.search(r"(.)\1\1", mot):
        score += 2
    if re.search(r"(..)\1", mot) and not re.search(r"(an|on|in|en|ou|ai|er|ar|or)\1", mot):
        score += 1
    score += min(_paires_inconnues(mot), 3) * 0.7
    rares = sum(c in _LETTRES_RARES for c in mot)
    score += 2 if rares >= 3 else (1 if rares == 2 else 0)
    if any(mot[i:i + 5] in _SUITES_CLAVIER for i in range(max(0, n - 4))):
        score += 2  # cinq touches voisines du clavier de suite : écrasement de clavier
    q_sans_u = len(re.findall(r"q(?!u)", mot[:-1])) if "q" in mot else 0
    score += 0.5 * min(q_sans_u, 2)
    return score < _SEUIL_REJET


_CARACTERES_OK = re.compile(r"^[A-Za-zÀ-ÖØ-öø-ÿ0-9][A-Za-zÀ-ÖØ-öø-ÿ0-9 .'’()\-/&]*$")


def valider_texte_libre(texte: str, *, min_len: int = 2, max_len: int = 30, nom: str = "Ce nom") -> str:
    """Contrôle un court texte libre (complément de nom de classe). Renvoie le texte normalisé."""
    propre = " ".join((texte or "").split()).replace("’", "'")
    if len(propre) < min_len:
        raise ErreurValidation(f"{nom} est trop court ({min_len} caractères minimum).")
    if len(propre) > max_len:
        raise ErreurValidation(f"{nom} est trop long ({max_len} caractères maximum).")
    if not _CARACTERES_OK.match(propre):
        raise ErreurValidation(f"{nom} contient des caractères non autorisés (lettres, chiffres, espaces et - ' . ( ) / & seulement).")
    mots_lettres = [m for m in re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]+", propre)]
    if not mots_lettres:
        raise ErreurValidation(f"{nom} doit contenir des lettres.")
    for mot in mots_lettres:
        if not mot_plausible(sans_accents(mot).lower()):
            raise ErreurValidation(
                f"« {mot} » ne ressemble pas à un vrai mot. Écris un nom lisible (ex. « Groupe A », « Révisions BEPC »)."
            )
    return propre[0].upper() + propre[1:]


# ------------------------------------------------------------------ classes

def composer_nom_classe(matiere: str, niveau_label: str, complement: str | None) -> str:
    base = f"{matiere} · {niveau_label}"
    return f"{base} — {complement}" if complement else base


def valider_classe(niveau_texte: str, matiere_texte: str, complement: str | None) -> dict:
    """Valide les trois champs, vérifie la cohérence niveau/matière et renvoie tout normalisé."""
    niveau = valider_niveau(niveau_texte)
    matiere = valider_matiere(matiere_texte, niveau)
    complement_propre = None
    if complement and complement.strip():
        complement_propre = valider_texte_libre(complement, min_len=2, max_len=30, nom="Le complément du nom")
    return {
        "niveau": niveau["label"],
        "matiere": matiere,
        "complement": complement_propre,
        "nom": composer_nom_classe(matiere, niveau["label"], complement_propre),
    }


def cle_doublon_classe(nom: str) -> str:
    """Deux noms qui donnent la même clé sont des doublons (casse, accents, espaces, ponctuation ignorés)."""
    return cle(nom)


# ------------------------------------------------------------------ profil

def valider_age(age) -> int:
    try:
        valeur = int(age)
    except (TypeError, ValueError):
        raise ErreurValidation("Indique ton âge en chiffres.")
    if valeur < AGE_MIN:
        raise ErreurValidation(f"GRIND est réservé aux élèves de {AGE_MIN} ans et plus.")
    if valeur > AGE_MAX:
        raise ErreurValidation(f"Indique un âge valide (entre {AGE_MIN} et {AGE_MAX} ans).")
    return valeur


def catalogue() -> dict:
    """Ce que le frontend affiche : niveaux, séries, matières par niveau."""
    matieres_par_niveau = {}
    for n in NIVEAUX:
        matieres_par_niveau[n["id"]] = matieres_pour(n)
    return {
        "age_min": AGE_MIN,
        "age_max": AGE_MAX,
        "niveaux": NIVEAUX,
        "filieres": FILIERES,
        "matieres_par_niveau": matieres_par_niveau,
    }


# ------------------------------------------------------------------ profil international (classes et matières libres)

def valider_texte_scolaire(texte: str | None, *, min_len: int, max_len: int, nom: str, facultatif: bool = False) -> str | None:
    """Valide un libellé scolaire libre sans imposer le catalogue d'un pays.

    Les systèmes scolaires, séries, diplômes et parcours professionnels varient fortement.
    On contrôle la longueur et les caractères de contrôle, sans heuristique linguistique
    qui pourrait rejeter une appellation locale ou technique légitime.
    """
    propre = " ".join((texte or "").split())
    if not propre:
        if facultatif:
            return None
        raise ErreurValidation(f"{nom} est obligatoire.")
    if len(propre) < min_len:
        raise ErreurValidation(f"{nom} doit contenir au moins {min_len} caractères.")
    if len(propre) > max_len:
        raise ErreurValidation(f"{nom} ne doit pas dépasser {max_len} caractères.")
    if not any(c.isalpha() or c.isdigit() for c in propre):
        raise ErreurValidation(f"{nom} doit contenir des lettres ou des chiffres.")
    if any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in propre):
        raise ErreurValidation(f"{nom} contient un caractère non autorisé.")
    return propre


def valider_difficultes_libres(brut) -> list[str]:
    """Accepte une liste de matières librement saisies, sans catalogue fermé."""
    if brut is None:
        raise ErreurValidation("Indique au moins une matière à travailler.")
    if isinstance(brut, str):
        texte = brut.strip()
        if texte.startswith("["):
            import json
            try:
                elements = json.loads(texte)
            except ValueError:
                raise ErreurValidation("La liste des matières est illisible.")
        else:
            elements = re.split(r"[,;\n]", texte)
    else:
        try:
            elements = list(brut)
        except TypeError:
            raise ErreurValidation("Indique une liste de matières valide.")
    resultat = []
    cles = set()
    for element in elements:
        if element is None:
            continue
        valeur = valider_texte_scolaire(str(element), min_len=2, max_len=80, nom="Une matière")
        cle_matiere = sans_accents(valeur).casefold()
        if cle_matiere not in cles:
            cles.add(cle_matiere)
            resultat.append(valeur)
    if not resultat:
        raise ErreurValidation("Indique au moins une matière à travailler.")
    if len(resultat) > 8:
        raise ErreurValidation("8 matières au maximum.")
    return resultat
