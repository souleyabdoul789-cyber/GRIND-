"""Safe cleanup for media attached to deleted GRIND feed publications.

Only Cloudinary image/video assets in the dedicated grind_posts folder are
candidates. Documents and assets still referenced elsewhere are preserved.
"""
from __future__ import annotations

import json

from fastapi import BackgroundTasks
from sqlalchemy import or_
from sqlalchemy.orm import Session

from models import (
    Annonce,
    ClasseDiscussion,
    ClasseDiscussionMessage,
    LibraryDocument,
    Post,
    PostCommentaire,
    PostCommentaireLike,
    PostLike,
)
from models_proviseur import ProviseurCandidature


def _est_media_publication(url: str) -> bool:
    valeur = str(url or "").strip()
    return (
        valeur.startswith("https://res.cloudinary.com/")
        and "/grind_posts/" in valeur
        and ("/image/upload/" in valeur or "/video/upload/" in valeur)
    )


def urls_medias_publication(post: Post, commentaires: list[PostCommentaire]) -> list[str]:
    """Collecte images et vidéos de post/commentaires, sans prendre les documents."""
    urls: list[str] = []
    try:
        items = json.loads(post.medias_json or "[]")
    except (TypeError, ValueError):
        items = []
    if isinstance(items, list):
        for item in items[:10]:
            if isinstance(item, dict) and item.get("type") in {"image", "video"}:
                url = str(item.get("url") or "").strip()
                if _est_media_publication(url):
                    urls.append(url)
    # Champ historique conservé pour les anciennes publications et rétrocompatibilité.
    ancienne_url = str(post.media_url or "").strip()
    if _est_media_publication(ancienne_url):
        urls.append(ancienne_url)
    for commentaire in commentaires:
        url = str(commentaire.media_url or "").strip()
        if _est_media_publication(url):
            urls.append(url)
    return list(dict.fromkeys(urls))


def medias_publication_orphelins(db: Session, urls: list[str], post_id: int) -> list[str]:
    """Ne retourne que les URL qui ne sont plus référencées par aucune ressource."""
    a_purger: list[str] = []
    for url in dict.fromkeys(urls):
        encore_utilise = (
            db.query(Post.id).filter(
                Post.id != post_id,
                or_(Post.media_url == url, Post.document_url == url, Post.medias_json.like(f"%{url}%")),
            ).first()
            or db.query(PostCommentaire.id).filter(
                PostCommentaire.post_id != post_id,
                or_(PostCommentaire.media_url == url, PostCommentaire.document_url == url),
            ).first()
            or db.query(LibraryDocument.id).filter_by(fichier_url=url).first()
            or db.query(Annonce.id).filter(or_(Annonce.media_url == url, Annonce.document_url == url)).first()
            or db.query(ClasseDiscussionMessage.id).filter_by(piece_url=url).first()
            or db.query(ClasseDiscussion.id).filter_by(photo_url=url).first()
            or db.query(ProviseurCandidature.id).filter_by(logo_url=url).first()
        )
        if not encore_utilise:
            a_purger.append(url)
    return a_purger


def programmer_purge_cloudinary(background_tasks: BackgroundTasks, urls: list[str]) -> None:
    """Ajoute la purge Cloudinary après commit, sans retarder la réponse HTTP."""
    if not urls:
        return
    from media import supprimer_assets_cloudinary
    background_tasks.add_task(supprimer_assets_cloudinary, urls)


def media_annonce_eligible_a_purge(annonce: Annonce) -> str | None:
    """Seule l'image/vidéo de l'annonce peut être détruite, jamais son document."""
    if getattr(annonce, "media_type", None) not in {"image", "video"}:
        return None
    url = str(getattr(annonce, "media_url", None) or "").strip()
    return url if _est_media_publication(url) else None


def media_annonce_orphelin(db: Session, url: str, annonce_id: int) -> bool:
    """Vérifie que l'URL n'est référencée par aucune autre donnée conservée."""
    encore_utilise = (
        db.query(Post.id).filter(
            or_(Post.media_url == url, Post.document_url == url, Post.medias_json.like(f"%{url}%")),
        ).first()
        or db.query(PostCommentaire.id).filter(
            or_(PostCommentaire.media_url == url, PostCommentaire.document_url == url),
        ).first()
        or db.query(LibraryDocument.id).filter_by(fichier_url=url).first()
        or db.query(Annonce.id).filter(
            Annonce.id != annonce_id,
            or_(Annonce.media_url == url, Annonce.document_url == url),
        ).first()
        or db.query(ClasseDiscussionMessage.id).filter_by(piece_url=url).first()
        or db.query(ClasseDiscussion.id).filter_by(photo_url=url).first()
        or db.query(ProviseurCandidature.id).filter_by(logo_url=url).first()
    )
    return not bool(encore_utilise)


def supprimer_annonce_et_programmer_nettoyage(
    db: Session,
    annonce: Annonce,
    background_tasks: BackgroundTasks,
) -> int:
    """Supprime une annonce et détruit uniquement son média image/vidéo orphelin."""
    url = media_annonce_eligible_a_purge(annonce)
    annonce_id = annonce.id
    db.delete(annonce)
    # Flush les suppressions en attente avant de vérifier les références restantes.
    # Cela couvre aussi les suppressions par lots dans une seule transaction.
    db.flush()
    if not url or not media_annonce_orphelin(db, url, annonce_id):
        return 0
    programmer_purge_cloudinary(background_tasks, [url])
    return 1


def supprimer_post_et_programmer_nettoyage(
    db: Session,
    post: Post,
    background_tasks: BackgroundTasks,
) -> int:
    """Supprime un post en DB et programme la purge des médias devenus orphelins.

    L'appelant garde la responsabilité de faire `db.commit()` après cette fonction.
    """
    commentaires = db.query(PostCommentaire).filter(PostCommentaire.post_id == post.id).all()
    urls_media = urls_medias_publication(post, commentaires)
    ids_commentaires = [commentaire.id for commentaire in commentaires]
    db.query(PostLike).filter(PostLike.post_id == post.id).delete(synchronize_session=False)
    if ids_commentaires:
        db.query(PostCommentaireLike).filter(
            PostCommentaireLike.commentaire_id.in_(ids_commentaires)
        ).delete(synchronize_session=False)
    db.query(PostCommentaire).filter(PostCommentaire.post_id == post.id).delete(synchronize_session=False)
    db.delete(post)
    # Flush avant la recherche d'orphelins pour tenir compte des suppressions
    # précédentes d'un éventuel lot (suppression totale de compte, par exemple).
    db.flush()
    urls_orphelines = medias_publication_orphelins(db, urls_media, post.id)
    programmer_purge_cloudinary(background_tasks, urls_orphelines)
    return len(urls_orphelines)
