#!/usr/bin/env python3
"""
generer_cle_admin.py — crée la clé d'administration de GRIND.

À lancer UNE FOIS, sur TON téléphone ou ton ordinateur (pas sur le serveur) :

    python3 generer_cle_admin.py

Le script affiche deux choses :
  1. LA CLÉ        -> c'est ton « mot de passe » d'administrateur. Garde-la pour
                      toi seul (gestionnaire de mots de passe, papier). Ne la
                      colle jamais dans un chat, sur GitHub ou dans un fichier du dépôt.
  2. L'EMPREINTE   -> à coller sur Render, dans les variables d'environnement
                      du serveur GRIND, sous le nom GRIND_ADMIN_KEY_HASH.

Le serveur ne stocke que l'empreinte : même quelqu'un qui verrait la base de
données ou les variables Render ne pourrait pas en déduire la clé.

Pour l'utiliser : sur la page de connexion de GRIND, écris n'importe quel nom
d'utilisateur et colle la clé à la place du mot de passe.

Pour la changer un jour (clé perdue ou compromise) : relance ce script et
remplace l'empreinte sur Render. L'ancienne clé cesse aussitôt de fonctionner.
"""

import hashlib
import secrets

cle = "grind-admin-" + secrets.token_urlsafe(48)
empreinte = hashlib.sha256(cle.encode()).hexdigest()

print()
print("=" * 64)
print("1) TA CLÉ D'ADMINISTRATION (à garder secrète, ne la partage JAMAIS) :")
print()
print(cle)
print()
print("2) À METTRE SUR RENDER (variable d'environnement du serveur) :")
print()
print(f"GRIND_ADMIN_KEY_HASH={empreinte}")
print("=" * 64)
print()
