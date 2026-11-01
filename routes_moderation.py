"""
routes_moderation.py — signalements, administration, équipe, certification, annonces.

Comme partout dans GRIND : l'identité vient de la session (auth_grind), jamais
d'un champ envoyé par le client. Les DROITS viennent du rôle stocké en base
(compte.role) : "admin", "associe" ou "controleur" — voir moderation.py. Le seul
moyen de devenir admin est la clé d'administration (routes_compte.py).

Toutes les routes reçoivent le session_token dans le corps de la requête
(POST), pas dans l'URL : un jeton n'a rien à faire dans un historique de
navigation ou un journal de serveur.
"""

from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from auth_grind import verifier_session
from moderation import (
    ADMIN_USERNAME,
    CGU_VERSION,
    NOM_ADMIN_PAR_DEFAUT,
    ROLE_ADMIN,
    ROLES_EQUIPE,
    SUSPENSION_COURTE_MAX_JOURS,
    PERMISSIONS,
    a_permission,
    role_de,
    sanction_active,
)
from schemas import (
    SessionRequest,
    SignalementCreate,
    AdminListeSignalements,
    AdminTraiter,
    AdminRechercheUtilisateurs,
    AdminSanctionner,
    AdminLever,
    AdminNom,
    AdminEquipeNommer,
    AdminCertifier,
    AdminAnnoncePublier,
    AdminAnnonceSupprimer,
)
from models import (
    CompteGrind,
    Post,
    PostLike,
    PostCommentaire,
    Signalement,
    Sanction,
    Annonce,
)
from ws_manager import manager

router = APIRouter()

MOTIFS = ("inapproprie", "harcelement", "spam", "danger", "autre")
TYPES_CIBLE = ("post", "commentaire", "profil")
MAX_SIGNALEMENTS_PAR_JOUR = 20


# ------------------------------------------------------------------ identités et droits

def _identite(session_token: str, db: Session) -> str:
    username = verifier_session(session_token, db)
    if username is None:
        raise HTTPException(status_code=401, detail="Session GRIND invalide ou expirée — reconnecte-toi")
    return username


def _acteur(session_token: str, db: Session, permission: str | None = None):
    """Identité + rôle d'un membre de l'équipe. 403 pour tout autre compte."""
    username = _identite(session_token, db)
    role = role_de(db, username)
    if role is None:
        raise HTTPException(status_code=403, detail="Réservé à l'équipe d'administration")
    if permission and not a_permission(role, permission):
        raise HTTPException(status_code=403, detail="Ton rôle ne permet pas cette action")
    return username, role


def _iso(dt):
    return dt.isoformat() if dt else None


def _extrait(texte: str | None, limite: int = 300) -> str | None:
    if texte is None:
        return None
    return texte if len(texte) <= limite else texte[:limite] + "…"


def _nom_affichable(compte: CompteGrind) -> str:
    if compte.role == ROLE_ADMIN:
        return compte.nom_affiche or NOM_ADMIN_PAR_DEFAUT
    return compte.nom_affiche or compte.username


# ------------------------------------------------------------------ statut du compte

@router.post("/api/moderation/moi")
async def mon_statut(data: SessionRequest, db: Session = Depends(get_db)):
    username = _identite(data.session_token, db)
    compte = db.get(CompteGrind, username)
    role = role_de(db, username)
    return {
        "username": username,
        "role": role,
        "est_equipe": role is not None,
        "nom_affiche": compte.nom_affiche if compte else None,
        "certifie": bool(compte.certifie) if compte else False,
        "permissions": sorted(PERMISSIONS.get(role, [])),
        "cgu_version": CGU_VERSION,
        # le compte admin n'est pas un élève : il n'a pas de CGU à accepter
        "cgu_a_accepter": role != ROLE_ADMIN and compte is not None and compte.cgu_version != CGU_VERSION,
    }


@router.post("/api/moderation/accepter-cgu")
async def accepter_cgu(data: SessionRequest, db: Session = Depends(get_db)):
    username = _identite(data.session_token, db)
    compte = db.get(CompteGrind, username)
    compte.cgu_version = CGU_VERSION
    compte.cgu_acceptees_le = datetime.now(timezone.utc)
    db.commit()
    return {"success": True}


# ------------------------------------------------------------------ signaler (élèves)

def _resoudre_cible(type_cible: str, cible_id: str, db: Session):
    """Renvoie (auteur du contenu, extrait) ou lève 404/400."""
    if type_cible == "post":
        try:
            post = db.get(Post, int(cible_id))
        except ValueError:
            raise HTTPException(status_code=400, detail="Identifiant invalide")
        if post is None:
            raise HTTPException(status_code=404, detail="Ce post n'existe plus")
        extrait = _extrait(post.contenu)
        if post.media_url:
            extrait = (extrait or "") + f"  [{post.media_type or 'média'} joint]"
        return post.auteur_username, extrait

    if type_cible == "commentaire":
        try:
            commentaire = db.get(PostCommentaire, int(cible_id))
        except ValueError:
            raise HTTPException(status_code=400, detail="Identifiant invalide")
        if commentaire is None:
            raise HTTPException(status_code=404, detail="Ce commentaire n'existe plus")
        return commentaire.username, _extrait(commentaire.contenu)

    compte = db.get(CompteGrind, cible_id)
    if compte is None:
        raise HTTPException(status_code=404, detail="Ce profil n'existe pas")
    return compte.username, None


@router.post("/api/moderation/signaler")
async def signaler(data: SignalementCreate, db: Session = Depends(get_db)):
    username = _identite(data.session_token, db)

    if data.type_cible not in TYPES_CIBLE:
        raise HTTPException(status_code=400, detail="Type de contenu invalide")
    if data.motif not in MOTIFS:
        raise HTTPException(status_code=400, detail="Motif invalide")
    details = (data.details or "").strip()
    if len(details) > 500:
        raise HTTPException(status_code=400, detail="Précisions trop longues (500 caractères maximum)")

    auteur, extrait = _resoudre_cible(data.type_cible, data.cible_id, db)
    if auteur == username:
        raise HTTPException(status_code=400, detail="Tu ne peux pas signaler ton propre contenu")

    # Même personne, même contenu, signalement encore ouvert : on ne double pas.
    deja = db.query(Signalement).filter(
        Signalement.signaleur_username == username,
        Signalement.type_cible == data.type_cible,
        Signalement.cible_id == data.cible_id,
        Signalement.statut == "ouvert",
    ).first()
    if deja:
        return {"success": True, "message": "Tu avais déjà signalé ce contenu — l'administration s'en occupe"}

    # Garde-fou contre les signalements en rafale (abus du bouton).
    hier = datetime.now(timezone.utc) - timedelta(hours=24)
    nb_recents = db.query(Signalement).filter(
        Signalement.signaleur_username == username,
        Signalement.created_at > hier,
    ).count()
    if nb_recents >= MAX_SIGNALEMENTS_PAR_JOUR:
        raise HTTPException(status_code=429, detail="Trop de signalements aujourd'hui — réessaie demain")

    db.add(Signalement(
        signaleur_username=username,
        type_cible=data.type_cible,
        cible_id=data.cible_id,
        cible_username=auteur,
        motif=data.motif,
        details=details or None,
        extrait=extrait,
    ))
    db.commit()
    return {"success": True, "message": "Merci — ton signalement a été transmis à l'administration"}


# ------------------------------------------------------------------ annonces (lecture pour tous)

def _serialiser_annonce(a: Annonce, db: Session) -> dict:
    auteur = db.get(CompteGrind, a.auteur_username)
    return {
        "id": a.id,
        "titre": a.titre,
        "contenu": a.contenu,
        "created_at": _iso(a.created_at),
        "auteur": _nom_affichable(auteur) if auteur else NOM_ADMIN_PAR_DEFAUT,
        "role": ROLE_ADMIN,
    }


@router.post("/api/annonces")
async def lister_annonces(data: SessionRequest, db: Session = Depends(get_db)):
    _identite(data.session_token, db)
    annonces = db.query(Annonce).order_by(Annonce.created_at.desc()).limit(20).all()
    return {"annonces": [_serialiser_annonce(a, db) for a in annonces]}


# ------------------------------------------------------------------ équipe : bureau, aperçu, nom

@router.post("/api/admin/apercu")
async def apercu(data: SessionRequest, db: Session = Depends(get_db)):
    _acteur(data.session_token, db)
    return {
        "nb_signalements_ouverts": db.query(Signalement).filter(Signalement.statut == "ouvert").count(),
        "nb_comptes": db.query(CompteGrind).filter(CompteGrind.username != ADMIN_USERNAME).count(),
        "nb_certifies": db.query(CompteGrind).filter(CompteGrind.certifie == 1).count(),
        "nb_equipe": db.query(CompteGrind).filter(CompteGrind.role.in_(ROLES_EQUIPE)).count(),
        "nb_annonces": db.query(Annonce).count(),
    }


@router.post("/api/admin/nom")
async def definir_nom(data: AdminNom, db: Session = Depends(get_db)):
    username, role = _acteur(data.session_token, db, "annonces")  # réservé à l'admin
    nom = data.nom_affiche.strip()
    if not (2 <= len(nom) <= 40):
        raise HTTPException(status_code=400, detail="Le nom doit faire entre 2 et 40 caractères")
    compte = db.get(CompteGrind, username)
    compte.nom_affiche = nom
    db.commit()
    return {"success": True, "nom_affiche": nom}


# ------------------------------------------------------------------ signalements

def _contenu_actuel(sig: Signalement, db: Session) -> dict:
    """Ce que l'administration voit : le contenu tel qu'il est maintenant
    (ou l'extrait gardé au moment du signalement s'il a été supprimé)."""
    info = {"existe": False, "texte": sig.extrait, "media_url": None, "media_type": None}
    try:
        if sig.type_cible == "post":
            post = db.get(Post, int(sig.cible_id))
            if post:
                info.update(existe=True, texte=post.contenu, media_url=post.media_url, media_type=post.media_type)
        elif sig.type_cible == "commentaire":
            commentaire = db.get(PostCommentaire, int(sig.cible_id))
            if commentaire:
                info.update(existe=True, texte=commentaire.contenu)
        else:
            info.update(existe=db.get(CompteGrind, sig.cible_id) is not None)
    except ValueError:
        pass
    return info


def _serialiser_signalement(sig: Signalement, db: Session) -> dict:
    nb_ouverts_cible = db.query(Signalement).filter(
        Signalement.type_cible == sig.type_cible,
        Signalement.cible_id == sig.cible_id,
        Signalement.statut == "ouvert",
    ).count()
    nb_total_auteur = db.query(Signalement).filter(Signalement.cible_username == sig.cible_username).count()
    sanction = sanction_active(db, sig.cible_username)
    role_auteur = role_de(db, sig.cible_username)
    return {
        "id": sig.id,
        "signaleur": sig.signaleur_username,
        "type_cible": sig.type_cible,
        "cible_id": sig.cible_id,
        "auteur": sig.cible_username,
        "motif": sig.motif,
        "details": sig.details,
        "contenu": _contenu_actuel(sig, db),
        "statut": sig.statut,
        "decision": sig.decision,
        "note_admin": sig.note_admin,
        "traite_par": sig.traite_par,
        "traite_le": _iso(sig.traite_le),
        "created_at": _iso(sig.created_at),
        "nb_signalements_ce_contenu": nb_ouverts_cible,
        "nb_signalements_auteur": nb_total_auteur,
        "auteur_sanctionne": sanction.type if sanction else None,
        "auteur_role": role_auteur,
    }


@router.post("/api/admin/signalements")
async def lister_signalements(data: AdminListeSignalements, db: Session = Depends(get_db)):
    _acteur(data.session_token, db, "signalements")
    if data.statut not in ("ouvert", "traite", "tous"):
        raise HTTPException(status_code=400, detail="Statut invalide")

    requete = db.query(Signalement)
    if data.statut != "tous":
        requete = requete.filter(Signalement.statut == data.statut)
    signalements = requete.order_by(Signalement.created_at.desc()).limit(100).all()

    nb_ouverts = db.query(Signalement).filter(Signalement.statut == "ouvert").count()
    return {
        "nb_ouverts": nb_ouverts,
        "signalements": [_serialiser_signalement(s, db) for s in signalements],
    }


# ------------------------------------------------------------------ sanctions

def _verifier_droit_de_sanction(role: str, type_sanction: str, duree_jours):
    """Qui peut infliger quoi : le contrôleur ne suspend que brièvement, le
    bannissement est réservé aux associés et à l'administrateur."""
    if type_sanction == "bannissement":
        if not a_permission(role, "bannissement"):
            raise HTTPException(status_code=403, detail="Le bannissement est réservé aux associés et à l'administrateur")
        return
    if a_permission(role, "suspension"):
        return
    if not a_permission(role, "suspension_courte"):
        raise HTTPException(status_code=403, detail="Ton rôle ne permet pas de suspendre")
    if duree_jours is None or duree_jours > SUSPENSION_COURTE_MAX_JOURS:
        raise HTTPException(
            status_code=403,
            detail=f"Un contrôleur peut suspendre {SUSPENSION_COURTE_MAX_JOURS} jours au maximum",
        )


def _verifier_cible(db: Session, role_acteur: str, username_cible: str):
    role_cible = role_de(db, username_cible)
    if role_cible == ROLE_ADMIN:
        raise HTTPException(status_code=400, detail="L'administrateur ne peut pas être sanctionné")
    if role_cible is not None and role_acteur != ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="Seul l'administrateur peut sanctionner un membre de l'équipe")


def _creer_sanction(db: Session, acteur: str, role: str, username: str, type_sanction: str, duree_jours, motif: str) -> Sanction:
    if db.get(CompteGrind, username) is None:
        raise HTTPException(status_code=404, detail="Compte introuvable")
    if type_sanction not in ("suspension", "bannissement"):
        raise HTTPException(status_code=400, detail="Type de sanction invalide")

    fin = None
    if type_sanction == "suspension":
        if duree_jours is None or not (1 <= duree_jours <= 365):
            raise HTTPException(status_code=400, detail="Durée de suspension : entre 1 et 365 jours")
        fin = datetime.now(timezone.utc) + timedelta(days=duree_jours)

    _verifier_droit_de_sanction(role, type_sanction, duree_jours)
    _verifier_cible(db, role, username)

    sanction = Sanction(username=username, type=type_sanction, motif=motif, jusqu_a=fin, par=acteur)
    db.add(sanction)
    return sanction


def _supprimer_contenu(sig: Signalement, db: Session) -> bool:
    if sig.type_cible == "post":
        try:
            post_id = int(sig.cible_id)
        except ValueError:
            return False
        post = db.get(Post, post_id)
        if post is None:
            return False
        db.query(PostLike).filter(PostLike.post_id == post_id).delete()
        db.query(PostCommentaire).filter(PostCommentaire.post_id == post_id).delete()
        db.delete(post)
        return True
    if sig.type_cible == "commentaire":
        try:
            commentaire = db.get(PostCommentaire, int(sig.cible_id))
        except ValueError:
            return False
        if commentaire is None:
            return False
        db.delete(commentaire)
        return True
    return False


@router.post("/api/admin/signalements/{signalement_id}/traiter")
async def traiter_signalement(signalement_id: int, data: AdminTraiter, db: Session = Depends(get_db)):
    acteur, role = _acteur(data.session_token, db, "signalements")

    sig = db.get(Signalement, signalement_id)
    if sig is None:
        raise HTTPException(status_code=404, detail="Signalement introuvable")
    if sig.statut != "ouvert":
        raise HTTPException(status_code=400, detail="Ce signalement est déjà traité")
    if data.action not in ("ignorer", "supprimer", "suspendre", "bannir"):
        raise HTTPException(status_code=400, detail="Action invalide")
    note = (data.note or "").strip() or None

    if data.action == "supprimer" and sig.type_cible == "profil":
        raise HTTPException(
            status_code=400,
            detail="Un profil ne se supprime pas : suspends ou bannis le compte",
        )

    decision = data.action
    if data.action in ("suspendre", "bannir"):
        motif = f"Signalement n°{sig.id} ({sig.motif})" + (f" — {note}" if note else "")
        _creer_sanction(
            db, acteur, role, sig.cible_username,
            "suspension" if data.action == "suspendre" else "bannissement",
            data.duree_jours, motif,
        )

    supprime = False
    if data.action == "supprimer" or (data.action in ("suspendre", "bannir") and data.supprimer_contenu):
        supprime = _supprimer_contenu(sig, db)
        if supprime and data.action != "supprimer":
            decision += "+suppression"

    # Tous les signalements ouverts sur le MÊME contenu sont réglés d'un coup.
    maintenant = datetime.now(timezone.utc)
    lies = db.query(Signalement).filter(
        Signalement.type_cible == sig.type_cible,
        Signalement.cible_id == sig.cible_id,
        Signalement.statut == "ouvert",
    ).all()
    for s in lies:
        s.statut = "traite"
        s.decision = decision
        s.note_admin = note
        s.traite_par = acteur
        s.traite_le = maintenant
    db.commit()

    return {"success": True, "traites": len(lies), "contenu_supprime": supprime}


# ------------------------------------------------------------------ registre des comptes

@router.post("/api/admin/utilisateurs")
async def chercher_utilisateurs(data: AdminRechercheUtilisateurs, db: Session = Depends(get_db)):
    _acteur(data.session_token, db, "utilisateurs")

    requete = db.query(CompteGrind).filter(CompteGrind.username != ADMIN_USERNAME)
    q = (data.q or "").strip()
    if q:
        requete = requete.filter(CompteGrind.username.like(f"%{q}%"))
    comptes = requete.order_by(CompteGrind.created_at.desc()).limit(50).all()

    resultat = []
    for c in comptes:
        sanction = sanction_active(db, c.username)
        resultat.append({
            "username": c.username,
            "created_at": _iso(c.created_at),
            "role": c.role if c.role in ROLES_EQUIPE else None,
            "certifie": bool(c.certifie),
            "nb_signalements": db.query(Signalement).filter(Signalement.cible_username == c.username).count(),
            "sanction": {
                "type": sanction.type,
                "motif": sanction.motif,
                "jusqu_a": _iso(sanction.jusqu_a),
            } if sanction else None,
        })
    return {"utilisateurs": resultat}


@router.post("/api/admin/sanctionner")
async def sanctionner(data: AdminSanctionner, db: Session = Depends(get_db)):
    acteur, role = _acteur(data.session_token, db, "utilisateurs")
    motif = data.motif.strip()
    if not motif:
        raise HTTPException(status_code=400, detail="Indique un motif")
    _creer_sanction(db, acteur, role, data.username, data.type, data.duree_jours, motif)
    db.commit()
    return {"success": True}


@router.post("/api/admin/lever-sanction")
async def lever_sanction(data: AdminLever, db: Session = Depends(get_db)):
    _acteur(data.session_token, db, "lever_sanction")
    maintenant = datetime.now(timezone.utc)
    actives = db.query(Sanction).filter(
        Sanction.username == data.username,
        Sanction.levee_le.is_(None),
    ).all()
    for s in actives:
        s.levee_le = maintenant
    db.commit()
    return {"success": True, "levees": len(actives)}


# ------------------------------------------------------------------ équipe (admin uniquement)

@router.post("/api/admin/equipe")
async def lister_equipe(data: SessionRequest, db: Session = Depends(get_db)):
    _acteur(data.session_token, db, "equipe")
    membres = db.query(CompteGrind).filter(CompteGrind.role.in_(ROLES_EQUIPE)).order_by(CompteGrind.username).all()
    return {
        "equipe": [{"username": m.username, "role": m.role, "certifie": bool(m.certifie)} for m in membres]
    }


@router.post("/api/admin/equipe/nommer")
async def nommer_membre(data: AdminEquipeNommer, db: Session = Depends(get_db)):
    _acteur(data.session_token, db, "equipe")
    if data.role not in ROLES_EQUIPE:
        raise HTTPException(status_code=400, detail="Rôle invalide : associé ou contrôleur uniquement")
    compte = db.get(CompteGrind, data.username.strip())
    if compte is None or compte.username == ADMIN_USERNAME:
        raise HTTPException(status_code=404, detail="Compte GRIND introuvable")
    if sanction_active(db, compte.username) is not None:
        raise HTTPException(status_code=400, detail="Ce compte est sanctionné : impossible de le nommer")
    compte.role = data.role
    db.commit()
    return {"success": True}


@router.post("/api/admin/equipe/retirer")
async def retirer_membre(data: AdminLever, db: Session = Depends(get_db)):
    _acteur(data.session_token, db, "equipe")
    compte = db.get(CompteGrind, data.username.strip())
    if compte is None or compte.role not in ROLES_EQUIPE:
        raise HTTPException(status_code=404, detail="Ce compte n'est pas dans l'équipe")
    compte.role = None
    db.commit()
    return {"success": True}


# ------------------------------------------------------------------ certification

@router.post("/api/admin/certifier")
async def certifier(data: AdminCertifier, db: Session = Depends(get_db)):
    _acteur(data.session_token, db, "certification")
    compte = db.get(CompteGrind, data.username.strip())
    if compte is None or compte.username == ADMIN_USERNAME:
        raise HTTPException(status_code=404, detail="Compte GRIND introuvable")
    compte.certifie = 1 if data.certifie else 0
    db.commit()
    return {"success": True, "certifie": bool(data.certifie)}


# ------------------------------------------------------------------ annonces (admin uniquement)

@router.post("/api/admin/annonces/publier")
async def publier_annonce(data: AdminAnnoncePublier, db: Session = Depends(get_db)):
    username, _ = _acteur(data.session_token, db, "annonces")
    titre = data.titre.strip()
    contenu = data.contenu.strip()
    if not (3 <= len(titre) <= 120):
        raise HTTPException(status_code=400, detail="Le titre doit faire entre 3 et 120 caractères")
    if not (3 <= len(contenu) <= 3000):
        raise HTTPException(status_code=400, detail="Le texte doit faire entre 3 et 3000 caractères")

    annonce = Annonce(auteur_username=username, titre=titre, contenu=contenu)
    db.add(annonce)
    db.commit()
    db.refresh(annonce)

    payload = _serialiser_annonce(annonce, db)
    await manager.notifier_annonce(payload, exclure=username)  # notification à tous les connectés
    return {"success": True, "annonce": payload}


@router.post("/api/admin/annonces/supprimer")
async def supprimer_annonce(data: AdminAnnonceSupprimer, db: Session = Depends(get_db)):
    _acteur(data.session_token, db, "annonces")
    annonce = db.get(Annonce, data.annonce_id)
    if annonce is None:
        raise HTTPException(status_code=404, detail="Annonce introuvable")
    db.delete(annonce)
    db.commit()
    return {"success": True}
