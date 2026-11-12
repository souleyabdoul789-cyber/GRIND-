"""
routes_posts.py — fil d'actu GRIND.

Deux catégories de post :
- "demonstration" : pas de deadline, pas d'expiration, juste like/commentaire
- "probleme"      : deadline obligatoire (date_limite). Tant qu'elle n'est
                     pas dépassée, statut="ouverte". L'auteur peut la
                     marquer "resolue" à tout moment. Si la deadline passe
                     sans résolution, le post passe "expiree" tout seul
                     (vérifié paresseusement à la lecture — pas besoin
                     d'un job planifié pour un prototype).

Comme partout ailleurs : l'identité vient de la session GRIND
(auth_grind.verifier_session), jamais d'un champ envoyé par le client.
"""

from datetime import datetime, timezone
import json
import re
import unicodedata
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import or_

from database import get_db
from auth_grind import verifier_session
from schemas import PostCreate, CommentaireCreate, LikeToggle, ResoudrePost, ExpirerPost
from models import Post, PostLike, PostCommentaire, PostCommentaireLike, Profil, CompteGrind, NotificationGrind
from models_proviseur import ProviseurCandidature, ProviseurAutorisation, Etablissement
from moderation import badge_de, usernames_sanctionnes, bloques_par

router = APIRouter()


def _identite(session_token: str, db: Session) -> str:
    username = verifier_session(session_token, db, autoriser_proviseur=True)
    if username is None:
        raise HTTPException(status_code=401, detail="Session GRIND invalide ou expirée — reconnecte-toi")
    candidature = db.query(ProviseurCandidature).filter_by(username=username).first()
    if candidature and candidature.statut != "approuve":
        raise HTTPException(status_code=403, detail="Le compte établissement est en attente de validation.")
    return username


def _creer_notifications_mentions(texte: str, acteur: str, post_id: int, db: Session, commentaire_id=None):
    """Résout les @identifiants connus, sans notifier l'auteur lui-même."""
    mentions = {m.lower() for m in re.findall(r"(?<![A-Za-z0-9_])@([A-Za-z_]{3,24})", texte or "")}
    if not mentions:
        return
    comptes = db.query(CompteGrind).all()
    correspondances = {c.username.lower(): c.username for c in comptes}
    post = db.get(Post, post_id)
    extrait = re.sub(r"\s+", " ", texte or "")[:240]
    for cle in mentions:
        destinataire = correspondances.get(cle)
        if not destinataire or destinataire == acteur:
            continue
        # Une publication institutionnelle ne déclenche des mentions que dans son établissement.
        if post and post.etablissement_id is not None:
            profil_dest = db.get(Profil, destinataire)
            etab_dest = db.get(Etablissement, post.etablissement_id)
            if not profil_dest or not etab_dest or _canon_etablissement(profil_dest.etablissement) != etab_dest.nom_canonique:
                continue
        # Évite les doublons pour le même contenu et la même mention.
        q = db.query(NotificationGrind).filter_by(destinataire_username=destinataire, acteur_username=acteur, type="mention", post_id=post_id)
        if commentaire_id is not None:
            q = q.filter_by(commentaire_id=commentaire_id)
        if q.first() is None:
            db.add(NotificationGrind(destinataire_username=destinataire, acteur_username=acteur, type="mention", post_id=post_id, commentaire_id=commentaire_id, extrait=extrait))


CATEGORIES_VALIDES = ("demonstration", "probleme")


def _canon_etablissement(value: str | None) -> str:
    """Même clé canonique que l'attribution administrative des établissements."""
    value = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def _appliquer_expiration_si_necessaire(post: Post, db: Session) -> Post:
    if post.categorie != "probleme" or post.statut != "ouverte":
        return post
    if post.date_limite and post.date_limite.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        post.statut = "expiree"
        post.raison_expiration = post.raison_expiration or "Délai dépassé sans résolution"
        db.commit()
    return post


def _medias_post(post: Post) -> list[dict]:
    try:
        items = json.loads(post.medias_json or "[]")
        if isinstance(items, list) and items:
            return items[:10]
    except (TypeError, ValueError):
        pass
    if post.media_url:
        return [{"url": post.media_url, "type": post.media_type or "image"}]
    return []


def _serialiser(post: Post, db: Session) -> dict:
    nb_likes = db.query(PostLike).filter(PostLike.post_id == post.id).count()
    nb_commentaires = db.query(PostCommentaire).filter(PostCommentaire.post_id == post.id).count()
    resultat = {
        "id": post.id,
        "auteur": post.auteur_username,
        "categorie": post.categorie,
        "contenu": post.contenu,
        "date_limite": post.date_limite.isoformat() if post.date_limite else None,
        "statut": post.statut,
        "raison_expiration": post.raison_expiration,
        "media_url": post.media_url,
        "media_type": post.media_type,
        "media_items": _medias_post(post),
        "document_url": post.document_url,
        "document_name": post.document_name,
        "document_type": post.document_type,
        "etablissement_id": post.etablissement_id,
        "auteur_type": post.auteur_type,
        "etablissement_nom": None,
        "logo_etablissement": None,
        "created_at": post.created_at.isoformat() if post.created_at else None,
        "likes": nb_likes,
        "commentaires": nb_commentaires,
        "auteur_badge": badge_de(db, post.auteur_username),
    }
    if post.etablissement_id:
        etab = db.get(Etablissement, post.etablissement_id)
        prov = db.query(ProviseurCandidature).filter_by(username=post.auteur_username, statut="approuve").first()
        resultat["etablissement_nom"] = etab.nom if etab else None
        resultat["logo_etablissement"] = prov.logo_url if prov else None
    return resultat


def _autorisation_etablissement(username: str, etablissement_id: int | None, db: Session, besoin_moderation=False):
    candidature = db.query(ProviseurCandidature).filter_by(username=username, statut="approuve").first()
    if candidature is None:
        return None
    query = db.query(ProviseurAutorisation).filter_by(candidature_id=candidature.id, actif=1)
    if etablissement_id is not None:
        query = query.filter_by(etablissement_id=etablissement_id)
    autorisation = query.order_by(ProviseurAutorisation.id.asc()).first()
    if autorisation is None or (besoin_moderation and not autorisation.moderation_locale) or (not besoin_moderation and not autorisation.peut_publier):
        raise HTTPException(status_code=403, detail="Aucune autorisation active pour cet établissement.")
    return autorisation


def _verifier_acces_post(username: str, post: Post, db: Session):
    if not post.etablissement_id:
        return
    from moderation import role_de, ROLE_ADMIN
    if role_de(db, username) == ROLE_ADMIN:
        return
    candidature = db.query(ProviseurCandidature).filter_by(username=username, statut="approuve").first()
    if candidature:
        _autorisation_etablissement(username, post.etablissement_id, db, besoin_moderation=False)
        return
    profil = db.get(Profil, username)
    if profil is None or not profil.etablissement:
        raise HTTPException(status_code=404, detail="Publication institutionnelle introuvable.")
    canon = _canon_etablissement(profil.etablissement)
    etab = db.get(Etablissement, post.etablissement_id)
    if etab is None or canon != etab.nom_canonique:
        raise HTTPException(status_code=404, detail="Publication institutionnelle introuvable.")


@router.post("/api/posts")
async def creer_post(data: PostCreate, db: Session = Depends(get_db)):
    username = _identite(data.session_token, db)
    if data.categorie not in CATEGORIES_VALIDES:
        raise HTTPException(status_code=400, detail=f"Catégorie invalide, attendu: {CATEGORIES_VALIDES}")

    candidature = db.query(ProviseurCandidature).filter_by(username=username, statut="approuve").first()
    etablissement_id = None
    if candidature:
        if data.categorie != "demonstration":
            raise HTTPException(status_code=400, detail="Un compte établissement publie des annonces institutionnelles, pas des demandes d'aide élèves.")
        autorisation = _autorisation_etablissement(username, data.etablissement_id, db, besoin_moderation=False)
        etablissement_id = autorisation.etablissement_id
    elif data.etablissement_id is not None:
        raise HTTPException(status_code=403, detail="Seul un compte établissement autorisé peut publier dans un établissement.")

    contenu = (data.contenu or "").strip()
    if len(contenu) > 5000:
        raise HTTPException(status_code=400, detail="La légende ne peut pas dépasser 5 000 caractères.")
    media_url = (data.media_url or "").strip() or None
    media_type = data.media_type
    document_url = (data.document_url or "").strip() or None
    document_name = (data.document_name or "").strip() or None
    document_type = (data.document_type or "").strip() or None
    if media_type not in (None, "image", "video"):
        raise HTTPException(status_code=400, detail="Type de média non pris en charge.")
    if media_url and (not media_url.startswith("https://res.cloudinary.com/") or "/grind_posts/" not in media_url or media_type not in ("image", "video")):
        raise HTTPException(status_code=400, detail="Le média doit provenir du stockage GRIND et posséder un type valide.")
    if not media_url and media_type:
        raise HTTPException(status_code=400, detail="Un type de média est fourni sans fichier associé.")
    if document_url:
        if not document_url.startswith("https://res.cloudinary.com/") or document_type not in ("application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"):
            raise HTTPException(status_code=400, detail="Document PDF ou DOCX non valide : il doit provenir du stockage GRIND.")
        if not document_name or len(document_name) > 180:
            raise HTTPException(status_code=400, detail="Nom de document invalide.")
        extension = document_name.lower().rsplit(".", 1)[-1] if "." in document_name else ""
        extension_attendue = "pdf" if document_type == "application/pdf" else "docx"
        if extension != extension_attendue or "/grind_documents/" not in document_url:
            raise HTTPException(status_code=400, detail="Le document ne correspond pas au format ou à l'espace de stockage autorisé.")
    media_items = []
    for item in (data.media_items or []):
        if not isinstance(item, dict) or item.get("type") not in ("image", "video"):
            raise HTTPException(status_code=400, detail="Un élément de la galerie est invalide.")
        url = str(item.get("url") or "").strip()
        if not url.startswith("https://res.cloudinary.com/") or "/grind_posts/" not in url:
            raise HTTPException(status_code=400, detail="Chaque média doit provenir du stockage GRIND.")
        media_items.append({"url": url, "type": item["type"]})
    if len(media_items) > 10:
        raise HTTPException(status_code=400, detail="Une publication accepte au maximum 10 images ou médias.")
    if media_items and media_url:
        raise HTTPException(status_code=400, detail="Utilise la galerie ou le média unique, pas les deux.")
    if not contenu and not media_url and not media_items and not document_url:
        raise HTTPException(status_code=400, detail="Ajoute une légende ou un fichier à ta publication.")

    date_limite_parsed = None
    if data.categorie == "probleme":
        if not data.date_limite:
            raise HTTPException(status_code=400, detail="Un post 'probleme' nécessite une date_limite (ISO 8601)")
        try:
            date_limite_parsed = datetime.fromisoformat(data.date_limite)
        except ValueError:
            raise HTTPException(status_code=400, detail="date_limite invalide, format attendu: ISO 8601")
        if date_limite_parsed <= datetime.now(date_limite_parsed.tzinfo or timezone.utc):
            raise HTTPException(status_code=400, detail="date_limite doit être dans le futur")


    post = Post(
        auteur_username=username, categorie=data.categorie, contenu=contenu,
        date_limite=date_limite_parsed, statut="ouverte" if data.categorie == "probleme" else "publiee",
        media_url=media_url, media_type=media_type,
        medias_json=json.dumps(media_items, ensure_ascii=False) if media_items else None,
        document_url=document_url, document_name=document_name, document_type=document_type,
        etablissement_id=etablissement_id, auteur_type="proviseur" if candidature else "eleve",
    )
    db.add(post); db.flush()
    _creer_notifications_mentions(contenu, username, post.id, db)
    db.commit(); db.refresh(post)
    return {"success": True, "post": _serialiser(post, db)}


@router.get("/api/posts")
async def lister_posts(categorie: str | None = None, session_token: str | None = None, db: Session = Depends(get_db)):
    query = db.query(Post)
    if categorie:
        if categorie not in CATEGORIES_VALIDES:
            raise HTTPException(status_code=400, detail=f"Catégorie invalide, attendu: {CATEGORIES_VALIDES}")
        query = query.filter(Post.categorie == categorie)

    posts = query.order_by(Post.created_at.desc()).all()
    # Les publications institutionnelles sont cloisonnées au réseau de l'établissement.
    lecteur = verifier_session(session_token, db, autoriser_proviseur=True) if session_token else None
    if lecteur:
        from moderation import role_de, ROLE_ADMIN
        if role_de(db, lecteur) != ROLE_ADMIN:
            candidate = db.query(ProviseurCandidature).filter_by(username=lecteur, statut="approuve").first()
            if candidate:
                ids = [a.etablissement_id for a in db.query(ProviseurAutorisation).filter_by(candidature_id=candidate.id, actif=1).all()]
            else:
                profil = db.get(Profil, lecteur)
                key = _canon_etablissement(profil.etablissement if profil else None)
                ids = [e.id for e in db.query(Etablissement).all() if e.nom_canonique == key] if key else []
            posts = [p for p in posts if p.etablissement_id is None or p.etablissement_id in ids]
        else:
            pass
    else:
        posts = [p for p in posts if p.etablissement_id is None]
    # Contenus masqués : comptes sanctionnés (traces gardées) et comptes que le lecteur ne veut plus voir.
    masques = usernames_sanctionnes(db)
    if lecteur:
        masques |= bloques_par(db, lecteur)
    posts = [p for p in posts if p.auteur_username not in masques]
    posts = [_appliquer_expiration_si_necessaire(p, db) for p in posts]

    return {"posts": [_serialiser(p, db) for p in posts]}


@router.post("/api/posts/{post_id}/like")
async def toggler_like(post_id: int, data: LikeToggle, db: Session = Depends(get_db)):
    username = _identite(data.session_token, db)

    post = db.get(Post, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="Post introuvable")
    _verifier_acces_post(username, post, db)

    existant = db.query(PostLike).filter(
        PostLike.post_id == post_id, PostLike.username == username
    ).first()

    if existant:
        db.delete(existant)
        db.commit()
        return {"success": True, "like": False}

    db.add(PostLike(post_id=post_id, username=username))
    if post.auteur_username != username:
        db.add(NotificationGrind(destinataire_username=post.auteur_username, acteur_username=username, type="like_publication", post_id=post_id, extrait="a aimé ta publication"))
    db.commit()
    return {"success": True, "like": True}


@router.post("/api/posts/{post_id}/commentaires")
async def commenter(post_id: int, data: CommentaireCreate, db: Session = Depends(get_db)):
    username = _identite(data.session_token, db)

    post = db.get(Post, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="Post introuvable")
    _verifier_acces_post(username, post, db)

    contenu = (data.contenu or "").strip()
    media_url = (data.media_url or "").strip() or None
    document_url = (data.document_url or "").strip() or None
    if len(contenu) > 2000:
        raise HTTPException(status_code=400, detail="Le commentaire est limité à 2 000 caractères.")
    if media_url and (not media_url.startswith("https://res.cloudinary.com/") or "/grind_posts/" not in media_url or data.media_type != "image"):
        raise HTTPException(status_code=400, detail="La pièce jointe image doit provenir du stockage GRIND.")
    if data.media_type not in (None, "image"):
        raise HTTPException(status_code=400, detail="Les commentaires acceptent les images, pas les vidéos.")
    if document_url and (not document_url.startswith("https://res.cloudinary.com/") or data.document_type not in ("application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")):
        raise HTTPException(status_code=400, detail="Le document joint doit être un PDF ou DOCX du stockage GRIND.")
    if not (contenu or media_url or document_url):
        raise HTTPException(status_code=400, detail="Ajoute un texte ou une pièce jointe.")
    parent = db.get(PostCommentaire, data.parent_id) if data.parent_id else None
    if data.parent_id and (parent is None or parent.post_id != post_id):
        raise HTTPException(status_code=404, detail="Commentaire parent introuvable.")
    commentaire = PostCommentaire(post_id=post_id, username=username, contenu=contenu, parent_id=data.parent_id,
        media_url=media_url, media_type=data.media_type, document_url=document_url,
        document_name=(data.document_name or "")[:180] or None, document_type=data.document_type)
    db.add(commentaire)
    db.flush()
    _creer_notifications_mentions(contenu, username, post_id, db, commentaire.id)
    cible = parent.username if parent else post.auteur_username
    if cible != username:
        db.add(NotificationGrind(destinataire_username=cible, acteur_username=username,
            type="reponse_commentaire" if parent else "commentaire", post_id=post_id,
            commentaire_id=commentaire.id, extrait=("a répondu à ton commentaire" if parent else "a commenté ta publication")))
    db.commit()
    db.refresh(commentaire)
    compte = db.get(CompteGrind, username)
    return {"success": True, "commentaire": {
        "id": commentaire.id, "username": username,
        "nom": " ".join(x for x in [compte.prenom, compte.nom] if x).strip() if compte else username,
        "badge": badge_de(db, username), "contenu": commentaire.contenu,
        "parent_id": commentaire.parent_id, "media_url": commentaire.media_url,
        "media_type": commentaire.media_type, "document_url": commentaire.document_url,
        "document_name": commentaire.document_name, "document_type": commentaire.document_type,
        "likes": 0, "created_at": commentaire.created_at.isoformat() if commentaire.created_at else None,
    }}


@router.get("/api/posts/{post_id}/commentaires")
async def lister_commentaires(post_id: int, session_token: str | None = None, db: Session = Depends(get_db)):
    post = db.get(Post, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="Post introuvable")
    if not session_token:
        raise HTTPException(status_code=401, detail="Connexion requise pour lire les commentaires.")
    lecteur = verifier_session(session_token, db, autoriser_proviseur=True)
    if not lecteur:
        raise HTTPException(status_code=401, detail="Session GRIND invalide ou expirée")
    _verifier_acces_post(lecteur, post, db)

    commentaires = db.query(PostCommentaire).filter(
        PostCommentaire.post_id == post_id
    ).order_by(PostCommentaire.created_at.asc()).all()

    masques = usernames_sanctionnes(db)
    if lecteur:
        masques |= bloques_par(db, lecteur)
    commentaires = [c for c in commentaires if c.username not in masques]

    return {
        "commentaires": [
            {"id": c.id, "username": c.username,
             "nom": (" ".join(x for x in [db.get(CompteGrind, c.username).prenom, db.get(CompteGrind, c.username).nom] if x).strip() if db.get(CompteGrind, c.username) else c.username),
             "badge": badge_de(db, c.username), "contenu": c.contenu, "parent_id": c.parent_id,
             "media_url": c.media_url, "media_type": c.media_type, "document_url": c.document_url,
             "document_name": c.document_name, "document_type": c.document_type,
             "likes": db.query(PostCommentaireLike).filter_by(commentaire_id=c.id).count(),
             "aime": db.query(PostCommentaireLike).filter_by(commentaire_id=c.id, username=lecteur).first() is not None,
             "created_at": c.created_at.isoformat() if c.created_at else None}
            for c in commentaires
        ]
    }


@router.post("/api/commentaires/{commentaire_id}/like")
async def liker_commentaire(commentaire_id: int, data: LikeToggle, db: Session = Depends(get_db)):
    username = _identite(data.session_token, db)
    commentaire = db.get(PostCommentaire, commentaire_id)
    if commentaire is None:
        raise HTTPException(status_code=404, detail="Commentaire introuvable.")
    post = db.get(Post, commentaire.post_id)
    _verifier_acces_post(username, post, db)
    vote = db.query(PostCommentaireLike).filter_by(commentaire_id=commentaire_id, username=username).first()
    if vote:
        db.delete(vote); liked = False
    else:
        db.add(PostCommentaireLike(commentaire_id=commentaire_id, username=username)); liked = True
        if commentaire.username != username:
            db.add(NotificationGrind(destinataire_username=commentaire.username, acteur_username=username, type="like_commentaire", post_id=post.id, commentaire_id=commentaire_id, extrait="a aimé ton commentaire"))
    db.commit()
    return {"success": True, "like": liked, "likes": db.query(PostCommentaireLike).filter_by(commentaire_id=commentaire_id).count()}


@router.post("/api/posts/{post_id}/modifier")
async def modifier_post(post_id: int, data: dict, db: Session = Depends(get_db)):
    username = _identite(data.get("session_token", ""), db)
    post = db.get(Post, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="Publication introuvable.")
    if post.auteur_username != username:
        raise HTTPException(status_code=403, detail="Tu peux uniquement modifier ta propre légende.")
    contenu = (data.get("contenu") or "").strip()
    if len(contenu) > 5000:
        raise HTTPException(status_code=400, detail="La légende est limitée à 5 000 caractères.")
    post.contenu = contenu
    db.commit()
    return {"success": True, "contenu": contenu}


@router.post("/api/posts/{post_id}/resoudre")
async def resoudre_post(post_id: int, data: ResoudrePost, db: Session = Depends(get_db)):
    username = _identite(data.session_token, db)

    post = db.get(Post, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="Post introuvable")
    _verifier_acces_post(username, post, db)
    if post.categorie != "probleme":
        raise HTTPException(status_code=400, detail="Seuls les posts 'probleme' peuvent être résolus")
    if post.auteur_username != username:
        raise HTTPException(status_code=403, detail="Seul l'auteur du post peut le marquer résolu")

    _appliquer_expiration_si_necessaire(post, db)
    if post.statut == "expiree":
        raise HTTPException(status_code=400, detail="Ce problème a déjà expiré, il ne peut plus être marqué résolu")

    post.statut = "resolue"
    db.commit()

    return {"success": True, "post": _serialiser(post, db)}


@router.post("/api/posts/{post_id}/expirer")
async def expirer_post_manuellement(post_id: int, data: ExpirerPost, db: Session = Depends(get_db)):
    username = _identite(data.session_token, db)

    post = db.get(Post, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="Post introuvable")
    _verifier_acces_post(username, post, db)
    if post.categorie != "probleme":
        raise HTTPException(status_code=400, detail="Seuls les posts 'probleme' peuvent expirer")
    if post.auteur_username != username:
        raise HTTPException(status_code=403, detail="Seul l'auteur du post peut le clore")

    post.statut = "expiree"
    post.raison_expiration = data.raison
    db.commit()

    return {"success": True, "post": _serialiser(post, db)}


@router.delete("/api/posts/{post_id}")
async def supprimer_post(post_id: int, session_token: str, db: Session = Depends(get_db)):
    username = _identite(session_token, db)

    post = db.get(Post, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="Post introuvable")
    _verifier_acces_post(username, post, db)
    if post.auteur_username != username:
        raise HTTPException(status_code=403, detail="Seul l'auteur peut supprimer ce post")

    db.query(PostLike).filter(PostLike.post_id == post_id).delete()
    db.query(PostCommentaire).filter(PostCommentaire.post_id == post_id).delete()
    db.delete(post)
    db.commit()

    return {"success": True}


@router.post("/api/proviseur/posts/{post_id}/moderation")
async def moderation_locale_post(post_id: int, data: LikeToggle, db: Session = Depends(get_db)):
    """Retrait local d'un contenu : seulement dans les établissements confiés au proviseur."""
    username = verifier_session(data.session_token, db, autoriser_proviseur=True)
    if not username:
        raise HTTPException(status_code=401, detail="Session invalide.")
    post = db.get(Post, post_id)
    if post is None or post.etablissement_id is None:
        raise HTTPException(status_code=404, detail="Publication institutionnelle introuvable.")
    _autorisation_etablissement(username, post.etablissement_id, db, besoin_moderation=True)
    db.query(PostLike).filter(PostLike.post_id == post_id).delete()
    db.query(PostCommentaire).filter(PostCommentaire.post_id == post_id).delete()
    db.delete(post); db.commit()
    return {"success": True}


@router.get("/api/utilisateurs/suggestions")
async def suggestions_utilisateurs(session_token: str, q: str = "", db: Session = Depends(get_db)):
    lecteur = verifier_session(session_token, db, autoriser_proviseur=True)
    if not lecteur:
        raise HTTPException(status_code=401, detail="Session GRIND invalide.")
    terme = (q or "").strip().lstrip("@").lower()[:24]
    query = db.query(CompteGrind).filter(CompteGrind.role.is_(None))
    if terme:
        query = query.filter(CompteGrind.username.ilike(f"{terme}%"))
    rows = query.order_by(CompteGrind.username.asc()).limit(12).all()
    return {"utilisateurs": [{"username": c.username, "nom": " ".join(x for x in [c.prenom, c.nom] if x).strip() or c.username, "avatar": None} for c in rows]}


@router.get("/api/notifications")
async def lister_notifications(session_token: str, db: Session = Depends(get_db)):
    username = _identite(session_token, db)
    lignes = db.query(NotificationGrind).filter_by(destinataire_username=username).order_by(NotificationGrind.created_at.desc()).limit(50).all()
    notifications = []
    for n in lignes:
        acteur = db.get(CompteGrind, n.acteur_username)
        notifications.append({"id": n.id, "type": n.type, "acteur": n.acteur_username,
            "acteur_nom": " ".join(x for x in [acteur.prenom, acteur.nom] if x).strip() if acteur else n.acteur_username,
            "post_id": n.post_id, "commentaire_id": n.commentaire_id,
            "resource_type": n.resource_type, "resource_id": n.resource_id,
            "extrait": n.extrait, "lu": bool(n.lu), "created_at": n.created_at.isoformat() if n.created_at else None})
    return {"notifications": notifications}


@router.post("/api/notifications/{notification_id}/lue")
async def marquer_notification_lue(notification_id: int, data: dict, db: Session = Depends(get_db)):
    username = _identite(data.get("session_token", ""), db)
    notif = db.query(NotificationGrind).filter_by(id=notification_id, destinataire_username=username).first()
    if notif is None:
        raise HTTPException(status_code=404, detail="Notification introuvable")
    notif.lu = 1
    db.commit()
    return {"success": True}
