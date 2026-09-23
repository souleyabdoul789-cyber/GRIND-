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
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from auth_grind import verifier_session
from schemas import PostCreate, CommentaireCreate, LikeToggle, ResoudrePost, ExpirerPost
from models import Post, PostLike, PostCommentaire

router = APIRouter()


def _identite(session_token: str, db: Session) -> str:
    username = verifier_session(session_token, db)
    if username is None:
        raise HTTPException(status_code=401, detail="Session GRIND invalide ou expirée — reconnecte-toi")
    return username


CATEGORIES_VALIDES = ("demonstration", "probleme")


def _appliquer_expiration_si_necessaire(post: Post, db: Session) -> Post:
    if post.categorie != "probleme" or post.statut != "ouverte":
        return post
    if post.date_limite and post.date_limite.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        post.statut = "expiree"
        post.raison_expiration = post.raison_expiration or "Délai dépassé sans résolution"
        db.commit()
    return post


def _serialiser(post: Post, db: Session) -> dict:
    nb_likes = db.query(PostLike).filter(PostLike.post_id == post.id).count()
    nb_commentaires = db.query(PostCommentaire).filter(PostCommentaire.post_id == post.id).count()
    return {
        "id": post.id,
        "auteur": post.auteur_username,
        "categorie": post.categorie,
        "contenu": post.contenu,
        "date_limite": post.date_limite.isoformat() if post.date_limite else None,
        "statut": post.statut,
        "raison_expiration": post.raison_expiration,
        "media_url": post.media_url,
        "media_type": post.media_type,
        "created_at": post.created_at.isoformat() if post.created_at else None,
        "likes": nb_likes,
        "commentaires": nb_commentaires,
    }


@router.post("/api/posts")
async def creer_post(data: PostCreate, db: Session = Depends(get_db)):
    username = _identite(data.session_token, db)

    if data.categorie not in CATEGORIES_VALIDES:
        raise HTTPException(status_code=400, detail=f"Catégorie invalide, attendu: {CATEGORIES_VALIDES}")

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
        auteur_username=username,
        categorie=data.categorie,
        contenu=data.contenu,
        date_limite=date_limite_parsed,
        statut="ouverte" if data.categorie == "probleme" else "publiee",
        media_url=data.media_url,
        media_type=data.media_type,
    )
    db.add(post)
    db.commit()
    db.refresh(post)

    return {"success": True, "post": _serialiser(post, db)}


@router.get("/api/posts")
async def lister_posts(categorie: str | None = None, db: Session = Depends(get_db)):
    query = db.query(Post)
    if categorie:
        if categorie not in CATEGORIES_VALIDES:
            raise HTTPException(status_code=400, detail=f"Catégorie invalide, attendu: {CATEGORIES_VALIDES}")
        query = query.filter(Post.categorie == categorie)

    posts = query.order_by(Post.created_at.desc()).all()
    posts = [_appliquer_expiration_si_necessaire(p, db) for p in posts]

    return {"posts": [_serialiser(p, db) for p in posts]}


@router.post("/api/posts/{post_id}/like")
async def toggler_like(post_id: int, data: LikeToggle, db: Session = Depends(get_db)):
    username = _identite(data.session_token, db)

    post = db.get(Post, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="Post introuvable")

    existant = db.query(PostLike).filter(
        PostLike.post_id == post_id, PostLike.username == username
    ).first()

    if existant:
        db.delete(existant)
        db.commit()
        return {"success": True, "like": False}

    db.add(PostLike(post_id=post_id, username=username))
    db.commit()
    return {"success": True, "like": True}


@router.post("/api/posts/{post_id}/commentaires")
async def commenter(post_id: int, data: CommentaireCreate, db: Session = Depends(get_db)):
    username = _identite(data.session_token, db)

    post = db.get(Post, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="Post introuvable")

    commentaire = PostCommentaire(post_id=post_id, username=username, contenu=data.contenu)
    db.add(commentaire)
    db.commit()
    db.refresh(commentaire)

    return {
        "success": True,
        "commentaire": {
            "id": commentaire.id,
            "username": commentaire.username,
            "contenu": commentaire.contenu,
            "created_at": commentaire.created_at.isoformat() if commentaire.created_at else None,
        },
    }


@router.get("/api/posts/{post_id}/commentaires")
async def lister_commentaires(post_id: int, db: Session = Depends(get_db)):
    post = db.get(Post, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="Post introuvable")

    commentaires = db.query(PostCommentaire).filter(
        PostCommentaire.post_id == post_id
    ).order_by(PostCommentaire.created_at.asc()).all()

    return {
        "commentaires": [
            {"id": c.id, "username": c.username, "contenu": c.contenu, "created_at": c.created_at.isoformat() if c.created_at else None}
            for c in commentaires
        ]
    }


@router.post("/api/posts/{post_id}/resoudre")
async def resoudre_post(post_id: int, data: ResoudrePost, db: Session = Depends(get_db)):
    username = _identite(data.session_token, db)

    post = db.get(Post, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="Post introuvable")
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
    if post.categorie != "probleme":
        raise HTTPException(status_code=400, detail="Seuls les posts 'probleme' peuvent expirer")
    if post.auteur_username != username:
        raise HTTPException(status_code=403, detail="Seul l'auteur du post peut le clore")

    post.statut = "expiree"
    post.raison_expiration = data.raison
    db.commit()

    return {"success": True, "post": _serialiser(post, db)}
