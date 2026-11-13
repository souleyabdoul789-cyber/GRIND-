"""
routes_compte.py — comptes GRIND : inscription, vérification d'e-mail, connexion, mot de passe oublié.

Principes (tous appliqués ICI, côté serveur) :
  - une adresse e-mail obligatoire et UNIQUE par compte ; aucune session n'existe tant qu'elle n'est pas vérifiée ;
  - code à 7 chiffres envoyé à l'adresse de l'utilisateur (voir verification.py et mailer.py) ;
  - aucune réponse ne permet de savoir si une adresse e-mail a un compte (flux fantômes, messages identiques) ;
  - freins contre la force brute : par adresse IP, par compte, par code (3 essais) ;
  - les adresses e-mail ne s'affichent JAMAIS en entier dans une confirmation (ex****l@gmail.com) ;
  - les e-mails partent en tâche de fond : la durée de réponse ne trahit rien et l'utilisateur n'attend pas l'envoi.
"""

import re
import secrets
import unicodedata
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

import mailer
import verification as vf
from auth_grind import creer_session, hacher_mot_de_passe, supprimer_session, verifier_mot_de_passe, verifier_session
from database import get_db
from limiteur import autoriser, noter_echec, oublier, trop_d_echecs
from models import CompteGrind, GrindSession, VerificationEmail, GrindDeviceAccount, Profil
from models_proviseur import ProviseurCandidature
from moderation import (
    ADMIN_USERNAME, CGU_VERSION, ROLE_ADMIN,
    cle_admin_valide, detail_sanction, nom_reserve, sanction_active,
)
from scolaire import ErreurValidation, catalogue
from schemas import (
    AssocierEmail, CompteGrindLogin, CompteGrindSignup, JetonVerification, MotDePasseOublie,
    ReinitialiserMotDePasse, SessionRequest, VerifierCode, GoogleAuthRequest,
)
from validation import (
    email_canonique, masquer_email, valider_email, valider_mot_de_passe, valider_pseudo,
)

router = APIRouter()

# Hash factice : on fait le même travail pour un pseudo inconnu et pour un mauvais mot de passe,
# afin que le temps de réponse ne révèle pas si le pseudo existe.
_HASH_FACTICE = hacher_mot_de_passe("mot-de-passe-factice-pour-egaliser-le-temps")

MESSAGE_FLUX_PERIME = "Cette vérification a expiré. Recommence depuis le début."


# ------------------------------------------------------------------ outils

def _erreur(statut: int, code: str, message: str, **extra):
    raise HTTPException(status_code=statut, detail={"code": code, "message": message, **extra})


def _ip(request: Request) -> str:
    transmis = request.headers.get("x-forwarded-for", "")
    if transmis:
        return transmis.split(",")[0].strip()
    return request.client.host if request.client else "inconnue"


def _verifier_mail_configure():
    if not mailer.configuration_ok():
        _erreur(503, "mail_indisponible", "L'envoi des e-mails n'est pas encore disponible. Réessaie un peu plus tard.")


async def _envoyer_code(email: str, code: str, objet: str):
    sujet, texte, html = mailer.gabarit_code(code, objet, vf.DUREE_CODE_MIN)
    await mailer.envoyer(email, sujet, texte, html)


async def _envoyer_gabarit(email: str, gabarit):
    sujet, texte, html = gabarit()
    await mailer.envoyer(email, sujet, texte, html)


def _payload_session(compte: CompteGrind, token: str, db: Session) -> dict:
    candidature = db.query(ProviseurCandidature).filter_by(username=compte.username).first()
    profil = db.get(Profil, compte.username)
    profil_complet = bool(
        profil is not None
        and profil.age is not None
        and bool((profil.pays or "").strip())
        and bool((profil.niveau or "").strip())
    )
    return {
        "success": True,
        "session_token": token,
        "username": compte.username,
        "role": compte.role,
        "nom_affiche": compte.nom_affiche,
        "type_compte": "proviseur" if candidature else "eleve",
        "proviseur_statut": candidature.statut if candidature else None,
        # L'état d'onboarding vient de la base : il ne dépend jamais du navigateur.
        "profil_complet": True if candidature else profil_complet,
    }


def _purger_inscriptions_non_verifiees(db: Session):
    """Une inscription jamais vérifiée disparaît au bout d'1 h : elle ne bloque ni le pseudo ni l'adresse."""
    limite = datetime.now(timezone.utc) - timedelta(hours=1)
    perimes = db.query(CompteGrind).filter(CompteGrind.email_verifie == 0, CompteGrind.created_at < limite).all()
    for compte in perimes:
        _supprimer_compte_non_verifie(db, compte)
    if perimes:
        db.commit()


def _supprimer_compte_non_verifie(db: Session, compte: CompteGrind):
    db.query(VerificationEmail).filter(VerificationEmail.username == compte.username).delete()
    db.query(GrindSession).filter(GrindSession.username == compte.username).delete()
    db.delete(compte)


def _flux_actif(db: Session, jeton: str) -> VerificationEmail:
    flux = vf.trouver_flux(db, VerificationEmail, jeton)
    if flux is None or not vf.flux_utilisable(flux, vf.maintenant()):
        _erreur(400, "flux_expire", MESSAGE_FLUX_PERIME)
    return flux


# ------------------------------------------------------------------ catalogue scolaire (public)

@router.get("/api/scolaire/catalogue")
async def catalogue_scolaire():
    return catalogue()


# ------------------------------------------------------------------ inscription

@router.post("/api/compte/signup")
async def signup(data: CompteGrindSignup, request: Request, taches: BackgroundTasks, db: Session = Depends(get_db)):
    ip = _ip(request)
    if not autoriser(f"signup:{ip}", 5, 3600):
        _erreur(429, "trop_de_demandes", "Trop d'inscriptions depuis cette connexion. Réessaie dans une heure.")
    _verifier_mail_configure()

    try:
        pseudo = valider_pseudo(data.username)
        email = valider_email(data.email)
        valider_mot_de_passe(data.password, pseudo)
    except ErreurValidation as erreur:
        _erreur(400, "validation", str(erreur))

    # Nom et prénom sont des champs civils distincts du nom de profil.
    # strip() retire uniquement les espaces de bord et conserve les espaces internes.
    nom = data.nom.strip() if isinstance(data.nom, str) else None
    prenom = data.prenom.strip() if isinstance(data.prenom, str) else None
    if (nom is None) != (prenom is None):
        _erreur(400, "identite_incomplete", "Renseigne le nom et le prénom dans deux champs séparés.")
    if nom is not None and (not nom or not prenom):
        _erreur(400, "identite_incomplete", "Le nom et le prénom ne peuvent pas être vides.")
    if nom is not None and (len(nom) > 80 or len(prenom) > 80):
        _erreur(400, "identite_trop_longue", "Le nom et le prénom sont limités à 80 caractères chacun.")
    if nom_reserve(pseudo):
        _erreur(400, "pseudo_reserve", "Ce nom d'utilisateur est réservé — choisis-en un autre.")
    if not data.cgu_acceptees:
        _erreur(400, "cgu", "Tu dois accepter les conditions d'utilisation pour créer un compte.")

    _purger_inscriptions_non_verifiees(db)

    if db.query(CompteGrind).filter(func.lower(CompteGrind.username) == pseudo.lower()).first() is not None:
        _erreur(400, "pseudo_pris", "Ce nom d'utilisateur est déjà pris.")

    canon = email_canonique(email)
    masque = masquer_email(email)

    # Adresse DÉJÀ liée à un compte vérifié : on ne dit rien au demandeur (même réponse que d'habitude),
    # et on prévient le propriétaire de l'adresse par e-mail.
    if db.query(CompteGrind).filter(CompteGrind.email_canonique == canon, CompteGrind.email_verifie == 1).first():
        _, jeton = vf.creer_flux(db, VerificationEmail, "inscription", email, None, ip, fantome=True)
        taches.add_task(_envoyer_gabarit, email, mailer.gabarit_deja_inscrit)
        return {"verification_requise": True, "email_masque": masque, "jeton_verification": jeton}

    # Une inscription non vérifiée qui tient la même adresse ne bloque personne : on la remplace.
    for ancien in db.query(CompteGrind).filter(CompteGrind.email_canonique == canon, CompteGrind.email_verifie == 0).all():
        _supprimer_compte_non_verifie(db, ancien)

    compte = CompteGrind(
        username=pseudo,
        password_hash=hacher_mot_de_passe(data.password),
        # Valeur sentinelle compatible avec les bases existantes (colonne historique NOT NULL).
        # Aucune vérification ni dépendance à G-SOCIETY n'est utilisée pour les nouvelles inscriptions.
        gsociety_username="__GRIND_NATIVE__",
        cgu_version=CGU_VERSION,
        cgu_acceptees_le=datetime.now(timezone.utc),
        email=email,
        email_canonique=canon,
        email_verifie=0,
        prenom=prenom,
        nom=nom,
        bienvenue_vue=0,
    )
    db.add(compte)
    db.commit()

    flux, jeton = vf.creer_flux(db, VerificationEmail, "inscription", email, pseudo, ip)
    _, code = vf.preparer_envoi(flux, vf.maintenant())
    db.commit()
    taches.add_task(_envoyer_code, email, code, "inscription")
    return {"verification_requise": True, "email_masque": masque, "jeton_verification": jeton}


# ------------------------------------------------------------------ vérification du code

@router.post("/api/compte/verification/verifier")
async def verifier_le_code(data: VerifierCode, request: Request, db: Session = Depends(get_db)):
    ip = _ip(request)
    if trop_d_echecs(f"verif:{ip}"):
        _erreur(429, "trop_d_essais_ip", "Trop de tentatives depuis cette connexion. Réessaie dans quelques minutes.")

    flux = _flux_actif(db, data.jeton_verification)
    code = re.sub(r"\s+", "", data.code or "")
    if not re.fullmatch(r"\d{7}", code):
        _erreur(400, "code_format", "Le code contient exactement 7 chiffres.")

    resultat, info = vf.controler_code(flux, code, vf.maintenant())
    db.commit()  # on enregistre AVANT de répondre : essais comptés, code annulé

    if resultat == "incorrect":
        noter_echec(f"verif:{ip}")
        _erreur(400, "code_incorrect", f"Code incorrect. Il te reste {info} essai{'s' if info > 1 else ''}.", tentatives_restantes=info)
    if resultat == "annule":
        noter_echec(f"verif:{ip}")
        _erreur(400, "code_annule", "Trop d'essais : ce code est annulé. Demande-en un nouveau.")
    if resultat == "expire":
        _erreur(400, "code_expire", "Ce code n'est plus valable. Demande-en un nouveau.")

    now = vf.maintenant()
    if flux.objet == "inscription":
        compte = db.get(CompteGrind, flux.username) if flux.username else None
        if compte is None or flux.fantome:
            _erreur(400, "flux_expire", MESSAGE_FLUX_PERIME)
        compte.email_verifie = 1
        compte.email_verifie_le = now
        flux.utilise_le = now
        db.commit()
        oublier(f"verif:{ip}")
        return _payload_session(compte, creer_session(compte.username, db), db)

    # Réinitialisation : le code est validé, il reste à choisir le nouveau mot de passe.
    flux.code_valide_le = now
    db.commit()
    return {"etape": "nouveau_mot_de_passe"}


@router.post("/api/compte/verification/renvoyer")
async def renvoyer_le_code(data: JetonVerification, request: Request, taches: BackgroundTasks, db: Session = Depends(get_db)):
    ip = _ip(request)
    if not autoriser(f"renvoi:{ip}", 20, 3600):
        _erreur(429, "trop_de_demandes", "Trop de demandes de code. Réessaie plus tard.")
    _verifier_mail_configure()

    flux = _flux_actif(db, data.jeton_verification)
    if not flux.email:
        _erreur(400, "email_manquant", "Indique d'abord ton adresse e-mail.")

    statut, info = vf.preparer_envoi(flux, vf.maintenant())
    db.commit()
    if statut == "trop_tot":
        _erreur(429, "trop_tot", f"Patiente {info} secondes avant de redemander un code.", attente_secondes=info)
    if statut == "trop_d_envois":
        _erreur(429, "trop_d_envois", "Tu as demandé trop de codes. Recommence la démarche plus tard.")
    if info is not None:  # info = code ; None pour un flux fantôme (rien n'est envoyé)
        taches.add_task(_envoyer_code, flux.email, info, "reinitialisation" if flux.objet == "reinitialisation" else "inscription")
    return {"success": True, "attente_secondes": vf.ATTENTE_RENVOI_S, "email_masque": masquer_email(flux.email)}


# ------------------------------------------------------------------ connexion

async def _exiger_verification(db: Session, compte: CompteGrind, ip: str, taches: BackgroundTasks):
    """Mot de passe correct mais e-mail pas vérifié : aucune session, on ouvre un flux de vérification."""
    _verifier_mail_configure()
    if not compte.email:
        # Ancien compte sans adresse : il doit d'abord en indiquer une.
        _, jeton = vf.creer_flux(db, VerificationEmail, "inscription", "", compte.username, ip)
        _erreur(403, "email_requis", "Ajoute et vérifie ton adresse e-mail pour continuer.", jeton_verification=jeton)
    flux, jeton = vf.creer_flux(db, VerificationEmail, "inscription", compte.email, compte.username, ip)
    _, code = vf.preparer_envoi(flux, vf.maintenant())
    db.commit()
    taches.add_task(_envoyer_code, compte.email, code, "inscription")
    _erreur(403, "email_non_verifie", "Vérifie ton adresse e-mail pour continuer.",
            email_masque=masquer_email(compte.email), jeton_verification=jeton)


def _compte_admin(db: Session) -> CompteGrind:
    """Le compte admin existe dès la première entrée avec la clé. Son mot de passe est aléatoire et jeté :
    la SEULE façon d'y entrer est la clé d'administration."""
    compte = db.get(CompteGrind, ADMIN_USERNAME)
    if compte is None:
        compte = CompteGrind(
            username=ADMIN_USERNAME,
            password_hash=hacher_mot_de_passe(secrets.token_urlsafe(48)),
            gsociety_username=ADMIN_USERNAME,
            role=ROLE_ADMIN,
            cgu_version=CGU_VERSION,
            cgu_acceptees_le=datetime.now(timezone.utc),
            email_verifie=1,
        )
        db.add(compte)
        db.commit()
    elif compte.role != ROLE_ADMIN:
        compte.role = ROLE_ADMIN
        db.commit()
    return compte


@router.post("/api/compte/login")
async def login(data: CompteGrindLogin, request: Request, taches: BackgroundTasks, db: Session = Depends(get_db)):
    ip = _ip(request)
    cle_compte = f"compte:{(data.username or '').strip().lower()}"
    if trop_d_echecs(ip) or trop_d_echecs(cle_compte):
        _erreur(429, "trop_d_essais", "Trop de tentatives échouées — réessaie dans quelques minutes.")

    # Clé d'administration : elle tient lieu de mot de passe, quel que soit le pseudo saisi.
    if cle_admin_valide(data.password):
        compte = _compte_admin(db)
        oublier(ip)
        return _payload_session(compte, creer_session(compte.username, db), db)

    identifiant = (data.username or "").strip()
    if "@" in identifiant:
        try:
            canon = email_canonique(valider_email(identifiant))
            compte = db.query(CompteGrind).filter(
                (CompteGrind.email_canonique == canon)
                | (func.lower(CompteGrind.email) == identifiant.lower())
            ).first()
        except ErreurValidation:
            compte = None
    else:
        # Le login utilise le nom de profil GRIND (jamais le nom/prénom civil).
        # La comparaison sans casse évite un refus lié aux majuscules saisies.
        compte = db.query(CompteGrind).filter(
            func.lower(CompteGrind.username) == identifiant.lower()
        ).first()
    mot_de_passe_ok = verifier_mot_de_passe(data.password, compte.password_hash if compte else _HASH_FACTICE)
    if compte is None or compte.username == ADMIN_USERNAME or not mot_de_passe_ok:
        noter_echec(ip)
        noter_echec(cle_compte)
        _erreur(401, "identifiants", "Identifiants GRIND incorrects")

    sanction = sanction_active(db, compte.username)
    if sanction is not None:
        # Mot de passe correct, mais compte sanctionné : détail structuré pour l'écran de sanction.
        raise HTTPException(status_code=403, detail=detail_sanction(db, sanction, emettre_jeton=True))

    # Tant que l'envoi d'e-mails n'est pas configuré, personne n'est enfermé dehors : la vérification
    # devient obligatoire dès que le relais e-mail fonctionne (voir docs/CONFIGURATION-EMAIL.md).
    candidature_prov = db.query(ProviseurCandidature).filter_by(username=compte.username).first()
    # Les comptes proviseur utilisent un numéro de contact manuel, jamais un OTP téléphonique
    # ni une adresse e-mail obligatoire. Leur activation dépend de la décision admin.
    if candidature_prov is None and compte.email_verifie != 1 and mailer.configuration_ok():
        await _exiger_verification(db, compte, ip, taches)

    oublier(ip)
    oublier(cle_compte)
    return _payload_session(compte, creer_session(compte.username, db), db)


def _nom_profil_google_suggere(prenom: str, nom: str, db: Session) -> str:
    """Construit un nom public lisible, unique et compatible avec les règles GRIND."""
    brut = f"{prenom}_{nom}".strip("_") or "Membre_GRIND"
    ascii_brut = unicodedata.normalize("NFKD", brut).encode("ascii", "ignore").decode("ascii")
    base = re.sub(r"[^A-Za-z]+", "_", ascii_brut).strip("_")
    if len(base) < 3:
        base = (base + "_GRIND")[:24].strip("_")
    base = base[:24].rstrip("_")
    if not base or not base[0].isalpha():
        base = "Membre_" + base
    base = base[:24].rstrip("_")

    # Les suffixes sont alphabétiques pour conserver les règles de nom de profil.
    candidats = [base]
    for suffix in "abcdefghijklmnopqrstuvwxyz":
        candidats.append(base[: 24 - len(suffix)] + suffix)
    for candidat in candidats:
        try:
            valider_pseudo(candidat)
        except ErreurValidation:
            continue
        if not db.query(CompteGrind).filter(func.lower(CompteGrind.username) == candidat.lower()).first():
            if not nom_reserve(candidat):
                return candidat
    # Repli déterministe en cas de collisions nombreuses.
    for suffix in range(100):
        candidat = (base[:20].rstrip("_") + "_" + chr(97 + (suffix // 26) % 26) + chr(97 + suffix % 26))[:24]
        try:
            valider_pseudo(candidat)
        except ErreurValidation:
            continue
        if not db.query(CompteGrind).filter(func.lower(CompteGrind.username) == candidat.lower()).first() and not nom_reserve(candidat):
            return candidat
    raise HTTPException(status_code=503, detail="Impossible de proposer un nom de profil disponible. Réessaie.")


@router.post("/api/compte/google")
async def connexion_google(data: GoogleAuthRequest, db: Session = Depends(get_db)):
    """Connexion/inscription Google : le jeton ID est vérifié côté serveur."""
    import os
    from datetime import datetime, timezone
    client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    if not client_id:
        _erreur(503, "google_indisponible", "La connexion Google n'est pas encore configurée côté serveur.")
    try:
        from google.oauth2 import id_token
        from google.auth.transport import requests as google_requests
    except ImportError:
        _erreur(503, "google_dependance_absente", "La vérification Google est indisponible côté serveur. Installe les dépendances déclarées dans requirements.txt.")
    try:
        claims = id_token.verify_oauth2_token(data.credential, google_requests.Request(), audience=client_id)
    except Exception:
        _erreur(401, "google_jeton_invalide", "La validation de ton compte Google a échoué. Recommence.")
    issuer = claims.get("iss")
    if issuer not in {"accounts.google.com", "https://accounts.google.com"} or claims.get("email_verified") is not True:
        _erreur(401, "google_identite_non_verifiee", "Google n'a pas confirmé cette adresse e-mail.")
    email = claims.get("email", "").strip()
    sub = claims.get("sub", "").strip()
    prenom_google = (claims.get("given_name") or "").strip()[:80]
    nom_google = (claims.get("family_name") or "").strip()[:80]
    if not prenom_google and not nom_google:
        # Certains jetons ne contiennent que le champ name ; il est conservé sans
        # tenter de deviner les frontières du prénom et du nom.
        nom_complet_google = (claims.get("name") or "").strip()[:160]
        if nom_complet_google:
            prenom_google = nom_complet_google[:80]
    try:
        email = valider_email(email)
        canon = email_canonique(email)
    except ErreurValidation:
        _erreur(400, "google_email_invalide", "Google n'a pas fourni une adresse e-mail utilisable.")
    if not sub:
        _erreur(401, "google_identite_invalide", "Identité Google incomplète.")

    compte = db.query(CompteGrind).filter(CompteGrind.google_sub == sub).first()
    if compte is None:
        compte = db.query(CompteGrind).filter(
            (CompteGrind.email_canonique == canon) | (func.lower(CompteGrind.email) == email.lower())
        ).first()
        if compte is not None:
            candidature = db.query(ProviseurCandidature).filter_by(username=compte.username).first()
            if candidature:
                _erreur(403, "google_compte_direction", "Ce compte de direction utilise sa connexion par nom de profil et mot de passe.")
            if compte.google_sub and compte.google_sub != sub:
                _erreur(409, "google_deja_lie", "Cette adresse est déjà liée à une autre identité Google.")
            compte.google_sub = sub
            compte.email = email
            compte.email_canonique = canon
            compte.email_verifie = 1
            compte.email_verifie_le = datetime.now(timezone.utc)
            if prenom_google and not compte.prenom:
                compte.prenom = prenom_google
            if nom_google and not compte.nom:
                compte.nom = nom_google
        else:
            username = (data.username or "").strip()
            if not username:
                suggere = _nom_profil_google_suggere(prenom_google, nom_google, db)
                return {
                    "username_requis": True,
                    "suggested_username": suggere,
                    "prenom": prenom_google,
                    "nom": nom_google,
                }
            try:
                pseudo = valider_pseudo(username)
            except ErreurValidation as erreur:
                _erreur(400, "validation", str(erreur))
            if nom_reserve(pseudo):
                _erreur(400, "pseudo_reserve", "Ce nom d'utilisateur est réservé.")
            if not data.cgu_acceptees:
                _erreur(400, "cgu", "Tu dois accepter les conditions d'utilisation avant de créer ton compte.")
            try:
                valider_mot_de_passe(data.password or "", pseudo)
            except ErreurValidation as erreur:
                _erreur(400, "validation", str(erreur))
            if db.query(CompteGrind).filter(func.lower(CompteGrind.username) == pseudo.lower()).first():
                _erreur(409, "pseudo_pris", "Ce nom de profil est déjà utilisé.")
            compte = CompteGrind(
                username=pseudo,
                password_hash=hacher_mot_de_passe(data.password),
                gsociety_username="__GRIND_NATIVE__",
                cgu_version=CGU_VERSION,
                cgu_acceptees_le=datetime.now(timezone.utc),
                email=email,
                email_canonique=canon,
                email_verifie=1,
                email_verifie_le=datetime.now(timezone.utc),
                prenom=prenom_google or None,
                nom=nom_google or None,
                google_sub=sub,
                bienvenue_vue=0,
            )
            db.add(compte)
    elif compte.email_canonique != canon:
        _erreur(409, "google_email_modifie", "L'adresse du compte Google ne correspond plus à celle liée au compte GRIND.")
    else:
        if prenom_google and not compte.prenom:
            compte.prenom = prenom_google
        if nom_google and not compte.nom:
            compte.nom = nom_google

    db.commit()
    if compte.username == ADMIN_USERNAME:
        _erreur(403, "google_admin_interdit", "L'administration utilise son mécanisme de connexion dédié.")
    sanction = sanction_active(db, compte.username)
    if sanction is not None:
        raise HTTPException(status_code=403, detail=detail_sanction(db, sanction, emettre_jeton=True))
    return _payload_session(compte, creer_session(compte.username, db), db)


@router.post("/api/compte/deconnexion")
async def deconnexion(data: SessionRequest, db: Session = Depends(get_db)):
    import hashlib
    token_hash = hashlib.sha256(data.session_token.encode()).hexdigest()
    db.query(GrindDeviceAccount).filter(GrindDeviceAccount.token_hash == token_hash).delete()
    supprimer_session(data.session_token, db)
    return {"success": True}


@router.post("/api/compte/identite")
async def modifier_identite_compte(data: dict, db: Session = Depends(get_db)):
    """Permet au titulaire de modifier son prénom et son nom civil."""
    token = str(data.get("session_token") or "")
    username = verifier_session(token, db, autoriser_proviseur=True)
    if username is None:
        _erreur(401, "session_invalide", "Session GRIND invalide ou expirée — reconnecte-toi.")

    compte = db.get(CompteGrind, username)
    if compte is None:
        _erreur(404, "compte_introuvable", "Ce compte GRIND n'existe plus.")

    def normaliser_nom(value, libelle):
        valeur = unicodedata.normalize("NFC", str(value or ""))
        valeur = " ".join(valeur.split())
        if not valeur or len(valeur) > 80:
            _erreur(400, "identite_invalide", f"{libelle} doit contenir entre 1 et 80 caractères.")
        contient_lettre = False
        for caractere in valeur:
            categorie = unicodedata.category(caractere)
            if categorie.startswith("L"):
                contient_lettre = True
            elif categorie.startswith("M") or caractere in " -'’.·":
                continue
            else:
                _erreur(400, "identite_invalide", f"{libelle} contient un caractère non autorisé.")
        if not contient_lettre:
            _erreur(400, "identite_invalide", f"{libelle} doit contenir au moins une lettre.")
        return valeur

    prenom = normaliser_nom(data.get("prenom"), "Le prénom")
    nom = normaliser_nom(data.get("nom"), "Le nom")
    compte.prenom = prenom
    compte.nom = nom
    db.commit()
    return {"success": True, "prenom": prenom, "nom": nom}


# ------------------------------------------------------------------ ancien compte : ajouter son e-mail

@router.post("/api/compte/email/associer")
async def associer_email(data: AssocierEmail, request: Request, taches: BackgroundTasks, db: Session = Depends(get_db)):
    ip = _ip(request)
    if not autoriser(f"associer:{ip}", 10, 3600):
        _erreur(429, "trop_de_demandes", "Trop de demandes. Réessaie plus tard.")
    _verifier_mail_configure()

    flux = _flux_actif(db, data.jeton_verification)
    compte = db.get(CompteGrind, flux.username) if flux.username else None
    if compte is None or flux.objet != "inscription" or flux.fantome or flux.email:
        _erreur(400, "flux_expire", MESSAGE_FLUX_PERIME)

    try:
        email = valider_email(data.email)
    except ErreurValidation as erreur:
        _erreur(400, "validation", str(erreur))
    canon = email_canonique(email)
    autre = db.query(CompteGrind).filter(
        CompteGrind.email_canonique == canon, CompteGrind.email_verifie == 1, CompteGrind.username != compte.username
    ).first()
    if autre is not None:
        # La personne s'est authentifiée par son mot de passe : on peut être explicite.
        _erreur(400, "email_pris", "Cette adresse e-mail est déjà utilisée par un autre compte.")

    compte.email = email
    compte.email_canonique = canon
    flux.email = email
    _, code = vf.preparer_envoi(flux, vf.maintenant())
    db.commit()
    taches.add_task(_envoyer_code, email, code, "email")
    return {"verification_requise": True, "email_masque": masquer_email(email), "jeton_verification": data.jeton_verification}


# ------------------------------------------------------------------ mot de passe oublié

@router.post("/api/compte/mot-de-passe-oublie")
async def mot_de_passe_oublie(data: MotDePasseOublie, request: Request, taches: BackgroundTasks, db: Session = Depends(get_db)):
    ip = _ip(request)
    if not autoriser(f"oubli:{ip}", 5, 3600):
        _erreur(429, "trop_de_demandes", "Trop de demandes depuis cette connexion. Réessaie dans une heure.")
    _verifier_mail_configure()

    try:
        email = valider_email(data.email)
    except ErreurValidation:
        _erreur(400, "validation", "Indique une adresse e-mail valide.")
    canon = email_canonique(email)
    masque = masquer_email(email)

    compte = db.query(CompteGrind).filter(CompteGrind.email_canonique == canon, CompteGrind.email_verifie == 1).first()
    # Au plus 3 codes par heure pour une même adresse (empêche d'inonder une boîte mail).
    permis = autoriser(f"oubli-mail:{canon}", 3, 3600)

    if compte is not None and permis:
        flux, jeton = vf.creer_flux(db, VerificationEmail, "reinitialisation", compte.email, compte.username, ip)
        _, code = vf.preparer_envoi(flux, vf.maintenant())
        db.commit()
        taches.add_task(_envoyer_code, compte.email, code, "reinitialisation")
    else:
        # Adresse inconnue (ou limite atteinte) : faux flux, réponse strictement identique.
        _, jeton = vf.creer_flux(db, VerificationEmail, "reinitialisation", email, None, ip, fantome=True)
    return {"verification_requise": True, "email_masque": masque, "jeton_verification": jeton}


@router.post("/api/compte/mot-de-passe/reinitialiser")
async def reinitialiser_mot_de_passe(data: ReinitialiserMotDePasse, request: Request, taches: BackgroundTasks, db: Session = Depends(get_db)):
    ip = _ip(request)
    if trop_d_echecs(f"verif:{ip}"):
        _erreur(429, "trop_d_essais_ip", "Trop de tentatives depuis cette connexion. Réessaie dans quelques minutes.")

    flux = _flux_actif(db, data.jeton_verification)
    if not vf.reinit_autorisee(flux, vf.maintenant()):
        _erreur(400, "code_non_valide", "Valide d'abord le code reçu par e-mail.")
    compte = db.get(CompteGrind, flux.username) if flux.username else None
    if compte is None:
        _erreur(400, "flux_expire", MESSAGE_FLUX_PERIME)

    try:
        valider_mot_de_passe(data.mot_de_passe, compte.username)
    except ErreurValidation as erreur:
        _erreur(400, "validation", str(erreur))

    compte.password_hash = hacher_mot_de_passe(data.mot_de_passe)
    # Toutes les sessions existantes sont coupées : un voleur éventuel est déconnecté.
    db.query(GrindSession).filter(GrindSession.username == compte.username).delete()
    flux.utilise_le = vf.maintenant()
    db.commit()
    oublier(f"compte:{compte.username.lower()}")
    taches.add_task(_envoyer_gabarit, compte.email, mailer.gabarit_mot_de_passe_modifie)
    return {"success": True}
