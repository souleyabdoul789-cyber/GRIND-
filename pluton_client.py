"""
pluton_client.py — le seul endroit du projet qui parle à Pluton.

Toute route GRIND qui a besoin de vérifier qu'un utilisateur est
légitime passe par ici. Pluton reste la seule source de vérité pour
l'identité — GRIND ne fait JAMAIS confiance à un username fourni par
son propre client, seulement à celui renvoyé par Pluton.
"""

import os
import httpx

# ⚠️ glitch-wbfo.onrender.com = le vrai serveur Pluton (FastAPI/API).
# g-society.onrender.com = le site statique G-SOCIETY, pas la même chose.
PLUTON_BASE_URL = os.getenv("PLUTON_BASE_URL", "https://glitch-wbfo.onrender.com")
CATEGORIE_GRIND = "grind"


async def verifier_cle(api_key: str) -> tuple[bool, str | None]:
    """Renvoie (valide, username). username est None si la clé est
    invalide — ne jamais utiliser un username venu d'ailleurs."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{PLUTON_BASE_URL}/api/verify-key",
                json={"api_key": api_key, "categorie": CATEGORIE_GRIND},
            )
            data = resp.json()
            return data.get("valid", False), data.get("username")
    except (httpx.RequestError, ValueError):
        return False, None
