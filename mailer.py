"""
mailer.py — envoi des e-mails de G-SOCIETY (codes de vérification, réinitialisation, alertes).

POURQUOI DEUX MODES. Render (offre gratuite) BLOQUE les connexions sortantes SMTP (ports 25, 465, 587)
depuis septembre 2025 : l'envoi direct par Gmail SMTP échoue. On passe donc par une porte HTTPS :

  MAIL_PROVIDER=relais_google   (recommandé tant que GRIND est sur Render gratuit)
      Un petit script Google Apps Script, déployé dans le compte Gmail de G-SOCIETY, reçoit une
      requête HTTPS du serveur et envoie le message DEPUIS ce compte Gmail (voir docs/).
      MAIL_RELAIS_URL      adresse du script déployé
      MAIL_RELAIS_SECRET   secret partagé avec le script

  MAIL_PROVIDER=smtp            (pour un vrai serveur / VPS, où SMTP n'est pas bloqué)
      SMTP_HOST (ex. smtp.gmail.com), SMTP_PORT (587), SMTP_USER, SMTP_PASSWORD (mot de passe
      d'APPLICATION Gmail, jamais le mot de passe du compte), MAIL_FROM_EMAIL (par défaut SMTP_USER)

  MAIL_FROM_NAME    nom affiché de l'expéditeur — « G-SOCIETY » par défaut.

AUCUN secret dans le code, le dépôt ou le frontend : tout vient de variables d'environnement.
Le contenu d'un code n'est jamais écrit dans les journaux.
"""

import asyncio
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr
from html import escape

import httpx

journal = logging.getLogger("grind.mail")

NOM_EXPEDITEUR_PAR_DEFAUT = "G-SOCIETY"


def nom_expediteur() -> str:
    return os.getenv("MAIL_FROM_NAME", "").strip() or NOM_EXPEDITEUR_PAR_DEFAUT


def fournisseur() -> str | None:
    choix = os.getenv("MAIL_PROVIDER", "").strip().lower()
    if choix == "relais_google" and os.getenv("MAIL_RELAIS_URL") and os.getenv("MAIL_RELAIS_SECRET"):
        return "relais_google"
    if choix == "smtp" and os.getenv("SMTP_HOST") and os.getenv("SMTP_USER") and os.getenv("SMTP_PASSWORD"):
        return "smtp"
    return None


def configuration_ok() -> bool:
    return fournisseur() is not None


def diagnostic() -> str:
    """Phrase lisible pour l'administrateur (sans secret)."""
    f = fournisseur()
    if f == "relais_google":
        return "E-mails : relais Google actif."
    if f == "smtp":
        return "E-mails : SMTP actif (ne marche pas sur Render gratuit : ports bloqués)."
    return "E-mails : non configuré (voir docs/CONFIGURATION-EMAIL.md)."


# ------------------------------------------------------------------ envoi

async def _par_relais(destinataire: str, sujet: str, texte: str, html: str) -> bool:
    url = os.environ["MAIL_RELAIS_URL"]
    corps = {
        "secret": os.environ["MAIL_RELAIS_SECRET"],
        "to": destinataire,
        "subject": sujet,
        "text": texte,
        "html": html,
        "name": nom_expediteur(),
    }
    try:
        # Google Apps Script répond par une redirection : il faut la suivre.
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            reponse = await client.post(url, json=corps)
        donnees = reponse.json()
    except Exception as exc:
        journal.warning("Relais e-mail injoignable ou illisible (%s)", type(exc).__name__)
        return False
    if not donnees.get("ok"):
        journal.warning("Le relais e-mail a refusé l'envoi : %s", str(donnees.get("erreur", "?"))[:80])
        return False
    return True


def _envoyer_smtp(destinataire: str, sujet: str, texte: str, html: str) -> bool:
    hote = os.environ["SMTP_HOST"]
    port = int(os.getenv("SMTP_PORT", "587"))
    utilisateur = os.environ["SMTP_USER"]
    mot_de_passe = os.environ["SMTP_PASSWORD"]
    adresse = os.getenv("MAIL_FROM_EMAIL", "").strip() or utilisateur

    message = EmailMessage()
    message["From"] = formataddr((nom_expediteur(), adresse))
    message["To"] = destinataire
    message["Subject"] = sujet
    message.set_content(texte)
    message.add_alternative(html, subtype="html")
    contexte = ssl.create_default_context()
    try:
        if port == 465:
            with smtplib.SMTP_SSL(hote, port, timeout=25, context=contexte) as serveur:
                serveur.login(utilisateur, mot_de_passe)
                serveur.send_message(message)
        else:
            with smtplib.SMTP(hote, port, timeout=25) as serveur:
                serveur.starttls(context=contexte)
                serveur.login(utilisateur, mot_de_passe)
                serveur.send_message(message)
    except Exception as exc:
        journal.warning("Envoi SMTP échoué (%s)", type(exc).__name__)
        return False
    return True


async def envoyer(destinataire: str, sujet: str, texte: str, html: str) -> bool:
    """Envoie un e-mail. Renvoie False (sans lever d'exception) si l'envoi échoue : l'appelant décide quoi faire."""
    f = fournisseur()
    if f == "relais_google":
        return await _par_relais(destinataire, sujet, texte, html)
    if f == "smtp":
        return await asyncio.to_thread(_envoyer_smtp, destinataire, sujet, texte, html)
    journal.warning("Aucun fournisseur d'e-mail configuré : message non envoyé.")
    return False


# ------------------------------------------------------------------ gabarits G-SOCIETY

_MARRON, _VIOLET, _FOND = "#d8bfa6", "#c9b6e4", "#0a0a0f"


def _cadre(titre: str, corps_html: str, pied: str) -> str:
    return f"""<!doctype html>
<html lang="fr"><body style="margin:0;padding:0;background:{_FOND};font-family:Arial,Helvetica,sans-serif;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{_FOND};padding:24px 12px;">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:480px;background:#14141d;border:1px solid #2a2a38;border-radius:16px;">
<tr><td style="padding:22px 24px 8px;">
<div style="font-size:20px;font-weight:bold;letter-spacing:1px;color:{_MARRON};">G-SOCIETY</div>
<div style="font-size:12px;color:#9a9aa5;">Message officiel envoyé par G-SOCIETY pour GRIND</div>
</td></tr>
<tr><td style="padding:12px 24px 4px;color:#e0e0e0;font-size:15px;line-height:1.55;">
<div style="font-size:17px;font-weight:bold;color:#ffffff;margin-bottom:8px;">{escape(titre)}</div>
{corps_html}
</td></tr>
<tr><td style="padding:14px 24px 22px;color:#9a9aa5;font-size:12px;line-height:1.5;border-top:1px solid #2a2a38;">{pied}</td></tr>
</table></td></tr></table></body></html>"""


_PIED_SECURITE = (
    "Ne communique jamais ce code, même à quelqu'un qui se dit de GRIND ou de G-SOCIETY : "
    "nous ne te le demanderons jamais. Si tu n'es pas à l'origine de cette demande, ignore ce message."
)


def gabarit_code(code: str, objet: str, minutes: int = 10) -> tuple[str, str, str]:
    """(sujet, texte, html) d'un message contenant un code à 7 chiffres."""
    if objet == "reinitialisation":
        titre = "Réinitialisation de ton mot de passe"
        intro = "Tu as demandé à réinitialiser le mot de passe de ton compte GRIND. Voici ton code de vérification :"
    elif objet == "email":
        titre = "Vérification de ton adresse e-mail"
        intro = "Pour finaliser ton compte GRIND, voici ton code de vérification :"
    else:
        titre = "Vérification de ton inscription"
        intro = "Bienvenue sur GRIND ! Pour activer ton compte, voici ton code de vérification :"
    code_lisible = f"{code[:3]} {code[3:]}"
    sujet = f"G-SOCIETY · {titre}"
    texte = (
        f"G-SOCIETY — message officiel pour GRIND\n\n{titre}\n\n{intro}\n\n    {code_lisible}\n\n"
        f"Ce code est valable {minutes} minutes et ne fonctionne qu'une fois.\n\n{_PIED_SECURITE}\n"
    )
    html = _cadre(
        titre,
        f"""<p style="margin:0 0 14px;">{escape(intro)}</p>
<div style="margin:6px 0 14px;padding:16px;text-align:center;border-radius:12px;background:{_FOND};border:1px solid #2a2a38;">
<span style="font-size:32px;font-weight:bold;letter-spacing:8px;color:{_VIOLET};">{escape(code_lisible)}</span></div>
<p style="margin:0;color:#9a9aa5;font-size:13px;">Ce code est valable {minutes} minutes et ne fonctionne qu'une fois.</p>""",
        _PIED_SECURITE,
    )
    return sujet, texte, html


def gabarit_deja_inscrit() -> tuple[str, str, str]:
    """Envoyé à une adresse DÉJÀ liée à un compte : l'inscription n'a rien révélé à celui qui l'a tentée."""
    titre = "Quelqu'un a essayé de créer un compte avec ton adresse"
    texte = (
        f"G-SOCIETY — message officiel pour GRIND\n\n{titre}\n\n"
        "Cette adresse e-mail est déjà liée à un compte GRIND. Si c'est toi, connecte-toi, ou utilise « Mot de passe oublié » "
        "si tu as perdu ton mot de passe. Si ce n'est pas toi, tu peux ignorer ce message : rien n'a été modifié.\n"
    )
    html = _cadre(
        titre,
        "<p style=\"margin:0 0 10px;\">Cette adresse e-mail est déjà liée à un compte GRIND.</p>"
        "<p style=\"margin:0;\">Si c'est toi, connecte-toi, ou utilise « Mot de passe oublié » si tu as perdu ton mot de passe. "
        "Si ce n'est pas toi, ignore ce message : rien n'a été modifié.</p>",
        "Message automatique de G-SOCIETY.",
    )
    return f"G-SOCIETY · {titre}", texte, html


def gabarit_mot_de_passe_modifie() -> tuple[str, str, str]:
    titre = "Ton mot de passe a été modifié"
    texte = (
        f"G-SOCIETY — message officiel pour GRIND\n\n{titre}\n\n"
        "Le mot de passe de ton compte GRIND vient d'être changé et tu as été déconnecté de tes appareils. "
        "Si ce n'est pas toi, réinitialise-le tout de suite avec « Mot de passe oublié » et contacte-nous.\n"
    )
    html = _cadre(
        titre,
        "<p style=\"margin:0;\">Le mot de passe de ton compte GRIND vient d'être changé et tu as été déconnecté de tes appareils. "
        "Si ce n'est pas toi, réinitialise-le tout de suite avec « Mot de passe oublié » et contacte-nous.</p>",
        "Message automatique de G-SOCIETY.",
    )
    return f"G-SOCIETY · {titre}", texte, html
