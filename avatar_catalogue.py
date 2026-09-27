"""
avatar_catalogue.py — la liste des objets disponibles par catégorie.

Volontairement réduit à UN personnage complet et fonctionnel pour
l'instant (validation du système), plus 2-3 objets verrouillés pour
tester l'affichage "à débloquer" côté frontend. Le vrai catalogue
s'enrichira une fois le système de monnaie G en place (chaque item
verrouillé a déjà un prix_g prêt pour ce jour-là).

Chaque item : id (utilisé partout comme référence), categorie,
nom affiché, gratuit (débloqué pour tous dès le départ), prix_g
(ignoré si gratuit=True, sinon montant futur).
"""

CATALOGUE = [
    # ---- Genre ----
    {"id": "genre_neutre", "categorie": "genre", "nom": "Neutre", "gratuit": True, "prix_g": 0},
    {"id": "genre_fille", "categorie": "genre", "nom": "Fille", "gratuit": True, "prix_g": 0},
    {"id": "genre_garcon", "categorie": "genre", "nom": "Garçon", "gratuit": True, "prix_g": 0},

    # ---- Corps ----
    {"id": "corps_base", "categorie": "corps", "nom": "Silhouette standard", "gratuit": True, "prix_g": 0},

    # ---- Visage ----
    {"id": "visage_base", "categorie": "visage", "nom": "Visage rond", "gratuit": True, "prix_g": 0},

    # ---- Cheveux ----
    {"id": "cheveux_base", "categorie": "cheveux", "nom": "Coupe courte", "gratuit": True, "prix_g": 0},
    {"id": "cheveux_long", "categorie": "cheveux", "nom": "Longs cheveux", "gratuit": False, "prix_g": 500},

    # ---- Vêtements ----
    {"id": "vetement_base", "categorie": "vetement", "nom": "Tenue GRIND", "gratuit": True, "prix_g": 0},
    {"id": "vetement_veste", "categorie": "vetement", "nom": "Veste stylée", "gratuit": False, "prix_g": 1000},

    # ---- Chapeaux (aucun par défaut = valeur null) ----
    {"id": "chapeau_casquette", "categorie": "chapeau", "nom": "Casquette", "gratuit": False, "prix_g": 800},

    # ---- Lunettes (aucune par défaut = valeur null) ----
    {"id": "lunettes_rondes", "categorie": "lunettes", "nom": "Lunettes rondes", "gratuit": False, "prix_g": 600},

    # ---- Couleur d'aura (celle qui s'allume quand on parle) ----
    {"id": "brand", "categorie": "couleur_aura", "nom": "Brun/Violet (GRIND)", "gratuit": True, "prix_g": 0},
    {"id": "or", "categorie": "couleur_aura", "nom": "Or légendaire", "gratuit": False, "prix_g": 10000},
]

CATEGORIES_OBLIGATOIRES = ("genre", "corps", "visage", "cheveux", "vetement", "couleur_aura")
CATEGORIES_OPTIONNELLES = ("chapeau", "lunettes")


def item_existe(item_id: str, categorie: str) -> bool:
    return any(i["id"] == item_id and i["categorie"] == categorie for i in CATALOGUE)


def item_est_gratuit(item_id: str) -> bool:
    item = next((i for i in CATALOGUE if i["id"] == item_id), None)
    return item["gratuit"] if item else False
