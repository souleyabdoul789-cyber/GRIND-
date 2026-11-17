"""Suppression cohérente d'une publication et de son document Library lié.

Le module ne contacte pas Cloudinary directement : media_assets collecte les
références ORM avant suppression, puis ne détruit un asset qu'après commit et
après vérification qu'aucune ressource GRIND ne le référence encore.
"""
from __future__ import annotations

from sqlalchemy import or_
from sqlalchemy.orm import Session

from models import (
    Annonce,
    ClasseDiscussionMessage,
    LibraryDocument,
    LibraryDocumentComment,
    LibraryDocumentVote,
    Post,
    PostCommentaire,
    PostCommentaireLike,
    PostLike,
)


def _document_encore_utilise(db: Session, url: str, post_id: int) -> bool:
    """Protège un document encore référencé, même via une URL Cloudinary transformée."""
    try:
        from media_assets import extraire_reference_cloudinary
        reference = extraire_reference_cloudinary(url)
    except Exception:
        reference = None

    if reference:
        _resource_type, public_id, _version = reference
        motif = f"%{public_id}%"
        post_ref = or_(Post.document_url.like(motif), Post.media_url.like(motif), Post.medias_json.like(motif))
        commentaire_ref = or_(PostCommentaire.document_url.like(motif), PostCommentaire.media_url.like(motif))
        annonce_ref = or_(Annonce.document_url.like(motif), Annonce.media_url.like(motif))
        discussion_ref = ClasseDiscussionMessage.piece_url.like(motif)
    else:
        post_ref = or_(Post.document_url == url, Post.media_url == url, Post.medias_json.like(f"%{url}%"))
        commentaire_ref = or_(PostCommentaire.document_url == url, PostCommentaire.media_url == url)
        annonce_ref = or_(Annonce.document_url == url, Annonce.media_url == url)
        discussion_ref = ClasseDiscussionMessage.piece_url == url

    return bool(
        db.query(Post.id).filter(Post.id != post_id, post_ref).first()
        or db.query(PostCommentaire.id).filter(commentaire_ref).first()
        or db.query(Annonce.id).filter(annonce_ref).first()
        or db.query(ClasseDiscussionMessage.id).filter(discussion_ref).first()
    )


def supprimer_post_et_documents_associes(db: Session, post: Post) -> int:
    """Supprime un post et son entrée Library associée, sans supprimer les fichiers partagés.

    La suppression SQL reste dans la transaction de l'appelant. Les fichiers
    Cloudinary seront nettoyés uniquement après le commit, via media_assets.
    """
    post_id = int(post.id)
    auteur = str(post.auteur_username or "")
    document_url = str(post.document_url or "").strip()

    commentaires = db.query(PostCommentaire).filter(PostCommentaire.post_id == post_id).all()
    ids_commentaires = [int(item.id) for item in commentaires]

    db.query(PostLike).filter(PostLike.post_id == post_id).delete(synchronize_session=False)
    if ids_commentaires:
        db.query(PostCommentaireLike).filter(
            PostCommentaireLike.commentaire_id.in_(ids_commentaires)
        ).delete(synchronize_session=False)
    # ORM delete déclenche le collecteur média pour les pièces jointes des commentaires.
    for commentaire in commentaires:
        db.delete(commentaire)

    db.delete(post)
    db.flush()

    documents_supprimes = 0
    if document_url:
        # Les entrées automatiques créées à la publication ont le même auteur
        # et réutilisent exactement Post.document_url ; aucun fichier n'est renvoyé.
        documents = db.query(LibraryDocument).filter_by(
            fichier_url=document_url, auteur_username=auteur
        ).all()
        if documents and not _document_encore_utilise(db, document_url, post_id):
            ids_documents = [int(doc.id) for doc in documents]
            db.query(LibraryDocumentVote).filter(
                LibraryDocumentVote.document_id.in_(ids_documents)
            ).delete(synchronize_session=False)
            db.query(LibraryDocumentComment).filter(
                LibraryDocumentComment.document_id.in_(ids_documents)
            ).delete(synchronize_session=False)
            for document in documents:
                db.delete(document)
            db.flush()
            documents_supprimes = len(documents)

    return documents_supprimes
