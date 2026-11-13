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
import hashlib
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
from models import CompteGrind, GrindSession, VerificationEmail, InscriptionEnAttente, GrindDeviceAccount, Profil
from models_proviseur import ProviseurCandidature
from moderation import (
    ADMIN_USERNAME, CGU_VERSION, ROLE_ADMIN,
    cle_admin_valide, detail_sanction, nom_reserve, sanction_active,
)
from scolaire import ErreurValidation, catalogue
from schemas import (
    AssocierEmail, CompteGrindLogin, CompteGrindSignup, JetonVerification, MotDePasseOublie,
    ReinitialiserMotDePasse, SessionRequest, VerifierCode, GoogleAuthRequest,
    InscriptionPinRequest, NomProfilInscriptionRequest,
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
    db.query(InscriptionEnAttente).filter(InscriptionEnAttente.created_at < limite).delete(synchronize_session=False)
    db.commit()


def _supprimer_compte_non_verifie(db: Session, compte: CompteGrind):
    db.query(VerificationEmail).filter(VerificationEmail.username == compte.username).delete()
    db.query(GrindSession).filter(GrindSession.username == compte.username).delete()
    db.delete(compte)


def _flux_actif(db: Session, jeton: str):
    flux = vf.trouver_flux(db, InscriptionEnAttente, jeton)
    if flux is None:
        flux = vf.trouver_flux(db, VerificationEmail, jeton)
    if flux is None or not vf.flux_utilisable(flux, vf.maintenant()):
        _erreur(400, "flux_expire", MESSAGE_FLUX_PERIME)
    return flux


def _inscription_active(db: Session, jeton: str, pin_requis: bool = False) -> InscriptionEnAttente:
    # Verrouille le flux pendant les étapes sensibles : évite qu'un même jeton
    # finalise deux comptes simultanément sur les bases MySQL de production.
    flux = db.query(InscriptionEnAttente).filter(
        InscriptionEnAttente.jeton_hash == vf.empreinte_jeton(jeton)
    ).with_for_update().first()
    if flux is None or not vf.flux_utilisable(flux, vf.maintenant()) or flux.fantome:
        _erreur(400, "inscription_expiree", MESSAGE_FLUX_PERIME)
    if not flux.email_verifie_le:
        _erreur(403, "email_non_verifie", "Vérifie d'abord ton adresse e-mail.")
    if pin_requis and not flux.pin_hash:
        _erreur(403, "pin_requis", "Configure d'abord ton PIN de sécurité à 4 chiffres.")
    return flux


def _nom_profil_occupe(db: Session, pseudo: str) -> bool:
    return db.query(CompteGrind).filter(func.lower(CompteGrind.username) == pseudo.lower()).first() is not None


def _creer_inscription_en_attente(
    db: Session, *, email: str, canon: str, password_hash: str,
    nom: str | None, prenom: str | None, ip: str,
):
    """Crée atomiquement le brouillon d'inscription et son code e-mail."""
    maintenant = vf.maintenant()
    jeton = secrets.token_urlsafe(32)
    flux = InscriptionEnAttente(
        objet="inscription", username=None, email=email, email_canonique=canon,
        password_hash=password_hash, nom=nom, prenom=prenom,
        cgu_version=CGU_VERSION, cgu_acceptees_le=maintenant,
        jeton_hash=vf.empreinte_jeton(jeton), tentatives=0, nb_envois=0,
        flux_expire_le=maintenant + timedelta(minutes=vf.DUREE_FLUX_MIN),
        fantome=0, ip=ip[:64],
    )
    _, code = vf.preparer_envoi(flux, maintenant)
    db.add(flux)
    db.commit()
    db.refresh(flux)
    return flux, jeton, code


def _suggestions_nom_profil(base: str, db: Session, limite: int = 5) -> list[str]:
    """Propose des variantes lisibles sans contourner les règles de validation."""
    texte = unicodedata.normalize("NFKD", base or "").encode("ascii", "ignore").decode("ascii").lower()
    texte = re.sub(r"[^A-Za-z0-9._-]+", "_", texte)
    texte = re.sub(r"[._-]{2,}", "_", texte).strip("._-")
    if not texte or not texte[0].isalnum():
        texte = "Membre" + texte
    if texte.isdigit():
        texte = "Membre" + texte
    texte = texte[:20].rstrip("._-") or "Membre"
    candidats = []
    for suffixe in [*(str(i) for i in range(1, 10)), *(f"_{c}" for c in "abcdefghijklmnopqrstuvwxyz"), *(f"{i:02d}" for i in range(10, 100))]:
        candidat = (texte[:24 - len(suffixe)].rstrip("._-") + suffixe)
        try:
            valider_pseudo(candidat)
        except ErreurValidation:
            continue
        if nom_reserve(candidat) or _nom_profil_occupe(db, candidat):
            continue
        if candidat.lower() not in {x.lower() for x in candidats}:
            candidats.append(candidat)
        if len(candidats) >= limite:
            break
    return candidats


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

    # Compatibilité ascendante : les anciennes versions peuvent encore envoyer un
    # username dans le premier formulaire. Le nouveau parcours n'en envoie plus.
    pseudo_ancien = (data.username or "").strip()
    if pseudo_ancien:
        try:
            pseudo = valider_pseudo(pseudo_ancien)
            email = valider_email(data.email)
            valider_mot_de_passe(data.password, pseudo)
        except ErreurValidation as erreur:
            _erreur(400, "validation", str(erreur))
        nom = data.nom.strip() if isinstance(data.nom, str) else None
        prenom = data.prenom.strip() if isinstance(data.prenom, str) else None
        if (nom is None) != (prenom is None) or (nom is not None and (not nom or not prenom)):
            _erreur(400, "identite_incomplete", "Renseigne le nom et le prénom dans deux champs séparés.")
        if nom is not None and (len(nom) > 80 or len(prenom) > 80):
            _erreur(400, "identite_trop_longue", "Le nom et le prénom sont limités à 80 caractères chacun.")
        if nom_reserve(pseudo):
            _erreur(400, "pseudo_reserve", "Ce nom d'utilisateur est réservé — choisis-en un autre.")
        if not data.cgu_acceptees:
            _erreur(400, "cgu", "Tu dois accepter les conditions d'utilisation pour créer un compte.")
        _purger_inscriptions_non_verifiees(db)
        if _nom_profil_occupe(db, pseudo):
            _erreur(400, "pseudo_pris", "Ce nom d'utilisateur est déjà pris.")
        canon = email_canonique(email)
        if db.query(CompteGrind).filter(CompteGrind.email_canonique == canon, CompteGrind.email_verifie == 1).first():
            _, jeton = vf.creer_flux(db, VerificationEmail, "inscription", email, None, ip, fantome=True)
            taches.add_task(_envoyer_gabarit, email, mailer.gabarit_deja_inscrit)
            return {"verification_requise": True, "email_masque": masquer_email(email), "jeton_verification": jeton}
        for ancien in db.query(CompteGrind).filter(CompteGrind.email_canonique == canon, CompteGrind.email_verifie == 0).all():
            _supprimer_compte_non_verifie(db, ancien)
        compte = CompteGrind(
            username=pseudo, password_hash=hacher_mot_de_passe(data.password),
            gsociety_username="__GRIND_NATIVE__", cgu_version=CGU_VERSION,
            cgu_acceptees_le=datetime.now(timezone.utc), email=email,
            email_canonique=canon, email_verifie=0, prenom=prenom, nom=nom, bienvenue_vue=0,
        )
        db.add(compte)
        db.commit()
        flux, jeton = vf.creer_flux(db, VerificationEmail, "inscription", email, pseudo, ip)
        _, code = vf.preparer_envoi(flux, vf.maintenant())
        db.commit()
        taches.add_task(_envoyer_code, email, code, "inscription")
        return {"verification_requise": True, "email_masque": masquer_email(email), "jeton_verification": jeton}

    # Nouveau parcours : aucune ligne comptes_grind et aucun username public n'existent
    # avant que l'adresse soit vérifiée, que le PIN soit configuré et que le pseudo soit choisi.
    try:
        email = valider_email(data.email)
        valider_mot_de_passe(data.password)
    except ErreurValidation as erreur:
        _erreur(400, "validation", str(erreur))
    nom = data.nom.strip() if isinstance(data.nom, str) else ""
    prenom = data.prenom.strip() if isinstance(data.prenom, str) else ""
    if not nom or not prenom:
        _erreur(400, "identite_incomplete", "Renseigne ton nom et ton prénom dans deux champs séparés.")
    if len(nom) > 80 or len(prenom) > 80:
        _erreur(400, "identite_trop_longue", "Le nom et le prénom sont limités à 80 caractères chacun.")
    if not data.cgu_acceptees:
        _erreur(400, "cgu", "Tu dois accepter les conditions d'utilisation pour créer un compte.")

    _purger_inscriptions_non_verifiees(db)
    canon = email_canonique(email)
    if db.query(CompteGrind).filter(CompteGrind.email_canonique == canon, CompteGrind.email_verifie == 1).first():
        _, jeton = vf.creer_flux(db, InscriptionEnAttente, "inscription", email, None, ip, fantome=True)
        taches.add_task(_envoyer_gabarit, email, mailer.gabarit_deja_inscrit)
        return {"verification_requise": True, "email_masque": masquer_email(email), "jeton_verification": jeton}

    for ancien in db.query(CompteGrind).filter(CompteGrind.email_canonique == canon, CompteGrind.email_verifie == 0).all():
        _supprimer_compte_non_verifie(db, ancien)
    db.query(InscriptionEnAttente).filter(InscriptionEnAttente.email_canonique == canon).delete(synchronize_session=False)
    db.commit()

    flux, jeton, code = _creer_inscription_en_attente(
        db, email=email, canon=canon, password_hash=hacher_mot_de_passe(data.password),
        nom=nom, prenom=prenom, ip=ip,
    )
    taches.add_task(_envoyer_code, email, code, "inscription")
    return {"verification_requise": True, "email_masque": masquer_email(email), "jeton_verification": jeton, "inscription_en_attente": True}


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
    if isinstance(flux, InscriptionEnAttente):
        if flux.fantome or not flux.password_hash:
            _erreur(400, "flux_expire", MESSAGE_FLUX_PERIME)
        flux.email_verifie_le = now
        flux.code_valide_le = now
        db.commit()
        oublier(f"verif:{ip}")
        return {
            "inscription_en_attente": True,
            "etape": "pin_securite",
            "jeton_inscription": data.jeton_verification,
            "email_masque": masquer_email(flux.email),
        }

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
    if isinstance(flux, InscriptionEnAttente) and flux.email_verifie_le:
        _erreur(409, "email_deja_verifie", "Cette adresse est déjà vérifiée. Continue avec la configuration de ton compte.")
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


# ------------------------------------------------------------------ fin de l'inscription : PIN puis nom de profil

@router.post("/api/compte/inscription/pin")
async def configurer_pin_inscription(data: InscriptionPinRequest, db: Session = Depends(get_db)):
    flux = _inscription_active(db, data.jeton_inscription)
    if not autoriser(f"pin-inscription:{flux.id}", 5, 900):
        _erreur(429, "trop_de_tentatives_pin", "Trop de tentatives de configuration du PIN. Réessaie plus tard.")
    pin = re.sub(r"\s+", "", data.pin or "")
    if not re.fullmatch(r"\d{4}", pin):
        _erreur(400, "pin_format", "Le PIN de sécurité doit contenir exactement 4 chiffres.")
    if flux.pin_hash:
        return {"success": True, "deja_configure": True}
    flux.pin_hash = hacher_mot_de_passe(pin)
    db.commit()
    return {"success": True, "etape": "nom_profil"}


@router.post("/api/compte/inscription/nom-profil/disponibilite")
async def verifier_disponibilite_nom_profil(data: NomProfilInscriptionRequest, db: Session = Depends(get_db)):
    flux = _inscription_active(db, data.jeton_inscription, pin_requis=True)
    if not autoriser(f"nom-profil-check:{flux.id}", 45, 60):
        _erreur(429, "trop_de_verifications", "Tu effectues trop de vérifications. Patiente un instant.")
    brut = (data.username or "").strip()
    try:
        pseudo = valider_pseudo(brut).lower()
    except ErreurValidation as erreur:
        return {"valide": False, "disponible": False, "message": str(erreur), "suggestions": []}
    if nom_reserve(pseudo):
        return {
            "valide": False, "disponible": False,
            "message": "Ce nom de profil est réservé. Essaie une autre combinaison.",
            "suggestions": _suggestions_nom_profil(pseudo, db),
        }
    if _nom_profil_occupe(db, pseudo):
        return {
            "valide": True, "disponible": False,
            "message": "Ce nom de profil est déjà pris. Voici quelques variantes disponibles.",
            "suggestions": _suggestions_nom_profil(pseudo, db),
        }
    return {"valide": True, "disponible": True, "message": "Ce nom de profil est disponible.", "suggestions": []}


@router.post("/api/compte/inscription/nom-profil/finaliser")
async def finaliser_nom_profil_inscription(data: NomProfilInscriptionRequest, db: Session = Depends(get_db)):
    flux = _inscription_active(db, data.jeton_inscription, pin_requis=True)
    if not autoriser(f"nom-profil-final:{flux.id}", 10, 600):
        _erreur(429, "trop_de_validations", "Trop de tentatives. Attends quelques minutes avant de réessayer.")
    try:
        pseudo = valider_pseudo(data.username).lower()
    except ErreurValidation as erreur:
        _erreur(400, "pseudo_invalide", str(erreur))
    if nom_reserve(pseudo):
        _erreur(400, "pseudo_reserve", "Ce nom de profil est réservé. Choisis-en un autre.", suggestions=_suggestions_nom_profil(pseudo, db))
    if _nom_profil_occupe(db, pseudo):
        _erreur(409, "pseudo_pris", "Ce nom de profil vient d'être pris. Choisis une suggestion.", suggestions=_suggestions_nom_profil(pseudo, db))
    if db.query(CompteGrind).filter(CompteGrind.email_canonique == flux.email_canonique, CompteGrind.email_verifie == 1).first():
        _erreur(409, "email_deja_utilise", "Cette adresse e-mail est déjà associée à un compte GRIND.")

    maintenant = datetime.now(timezone.utc)
    compte = CompteGrind(
        username=pseudo,
        password_hash=flux.password_hash,
        gsociety_username="__GRIND_NATIVE__",
        cgu_version=flux.cgu_version or CGU_VERSION,
        cgu_acceptees_le=flux.cgu_acceptees_le or maintenant,
        email=flux.email,
        email_canonique=flux.email_canonique,
        email_verifie=1,
        email_verifie_le=flux.email_verifie_le or maintenant,
        prenom=flux.prenom,
        nom=flux.nom,
        google_sub=flux.google_sub,
        pin_hash=flux.pin_hash,
        pin_echecs=0,
        pin_bloque_jusqua=None,
        bienvenue_vue=0,
    )
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    try:
        db.add(compte)
        db.flush()  # la clé publique est réservée avant la création de la session
        db.add(GrindSession(
            token_hash=token_hash,
            username=pseudo,
            expires_at=maintenant + timedelta(days=30),
            pin_verifie_le=maintenant,
        ))
        flux.utilise_le = maintenant
        db.commit()
    except Exception:
        db.rollback()
        if _nom_profil_occupe(db, pseudo):
            _erreur(409, "pseudo_pris", "Ce nom de profil vient d'être pris. Choisis une suggestion.", suggestions=_suggestions_nom_profil(pseudo, db))
        raise
    return _payload_session(compte, token, db)


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
    if identifiant.startswith("@"):  # autorise la saisie du tag complet @profil
        pseudo_login = identifiant[1:].strip()
        compte = db.query(CompteGrind).filter(
            func.lower(CompteGrind.username) == pseudo_login.lower()
        ).first()
    elif "@" in identifiant:
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
                # Premier passage : Google a confirmé l'e-mail. On demande le mot de
                # passe GRIND et l'acceptation des CGU, mais PAS encore le nom public.
                if not data.password or not data.cgu_acceptees:
                    return {
                        "username_requis": True,  # nom historique de réponse conservé pour les clients précédents
                        "configuration_requise": True,
                        "prenom": prenom_google,
                        "nom": nom_google,
                    }
                try:
                    valider_mot_de_passe(data.password)
                except ErreurValidation as erreur:
                    _erreur(400, "validation", str(erreur))
                db.query(InscriptionEnAttente).filter(
                    (InscriptionEnAttente.email_canonique == canon) | (InscriptionEnAttente.google_sub == sub)
                ).delete(synchronize_session=False)
                maintenant = datetime.now(timezone.utc)
                jeton = secrets.token_urlsafe(32)
                flux = InscriptionEnAttente(
                    objet="inscription", username=None, email=email, email_canonique=canon,
                    password_hash=hacher_mot_de_passe(data.password),
                    prenom=prenom_google or None, nom=nom_google or None, google_sub=sub,
                    cgu_version=CGU_VERSION, cgu_acceptees_le=maintenant,
                    email_verifie_le=maintenant, pin_hash=None,
                    jeton_hash=vf.empreinte_jeton(jeton), tentatives=0, nb_envois=0,
                    flux_expire_le=maintenant + timedelta(minutes=vf.DUREE_FLUX_MIN),
                    code_valide_le=maintenant, fantome=0,
                )
                db.add(flux)
                db.commit()
                return {
                    "inscription_en_attente": True,
                    "etape": "pin_securite",
                    "jeton_inscription": jeton,
                    "email_masque": masquer_email(email),
                }

            # Compatibilité ascendante pour les anciennes versions clientes : elles
            # peuvent encore envoyer un username dès l'inscription Google.
            try:
                pseudo = valider_pseudo(username)
            except ErreurValidation as erreur:
                _erreur(400, "validation", str(erreur))
            if nom_reserve(pseudo):
                _erreur(400, "pseudo_reserve", "Ce nom d'utilisateur est réservé.")
            if not data.cgu_acceptees:
                _erreur(400, "cgu", "Tu dois accepter les conditions d'utilisation avant de créer un compte.")
            try:
                valider_mot_de_passe(data.password or "", pseudo)
            except ErreurValidation as erreur:
                _erreur(400, "validation", str(erreur))
            if _nom_profil_occupe(db, pseudo):
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
