"""Registre les ressources Cloudinary approuvées par GRIND et leur propriétaire."""
from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse, unquote

from fastapi import HTTPException
from sqlalchemy.orm import Session

from media import CLOUDINARY_CLOUD_NAME
from models import GrindMediaAsset


def extraire_reference_cloudinary(url: str):
    parsed = urlparse(str(url or ""))
    if (parsed.scheme != "https" or parsed.hostname != "res.cloudinary.com"
            or parsed.port not in (None, 443) or not CLOUDINARY_CLOUD_NAME):
        return None
    segments = [unquote(part) for part in parsed.path.strip("/").split("/")]
    if len(segments) < 6 or segments[0] != CLOUDINARY_CLOUD_NAME:
        return None
    resource_type, delivery = segments[1], segments[2]
    if resource_type not in {"image", "video", "raw"} or delivery != "upload":
        return None
    version_index = next((i for i, part in enumerate(segments[3:], start=3)
                          if re.fullmatch(r"v[0-9]+", part)), None)
    if version_index is None or version_index >= len(segments) - 1:
        return None
    public_id = "/".join(segments[version_index + 1:])
    if not public_id or any(part in {".", ".."} or "\\" in part for part in public_id.split("/")):
        return None
    return resource_type, public_id, segments[version_index]


def _cle_asset(resource_type: str, public_id: str) -> str:
    return hashlib.sha256(f"{resource_type}\0{public_id}".encode("utf-8")).hexdigest()


def enregistrer_media_asset(db: Session, *, username: str, url: str, mime_type: str | None, size_bytes: int):
    reference = extraire_reference_cloudinary(url)
    if reference is None:
        raise HTTPException(status_code=502, detail="La référence du fichier Cloudinary n'est pas valide.")
    resource_type, public_id, version = reference
    cle = _cle_asset(resource_type, public_id)
    row = db.query(GrindMediaAsset).filter_by(asset_key=cle).first()
    if row is not None and row.username != username:
        raise HTTPException(status_code=409, detail="Cette ressource Cloudinary appartient déjà à un autre compte GRIND.")
    if row is None:
        row = GrindMediaAsset(
            asset_key=cle, username=username, resource_type=resource_type, public_id=public_id,
            version=version, mime_type=(mime_type or "")[:120] or None, size_bytes=max(0, int(size_bytes or 0)),
        )
        db.add(row)
    else:
        row.version = version
        row.mime_type = (mime_type or "")[:120] or row.mime_type
        row.size_bytes = max(0, int(size_bytes or row.size_bytes or 0))
    return row


def verifier_proprietaire_media(db: Session, *, username: str, url: str):
    reference = extraire_reference_cloudinary(url)
    if reference is None:
        raise HTTPException(status_code=400, detail="La référence Cloudinary n'est pas reconnue.")
    resource_type, public_id, version = reference
    row = db.query(GrindMediaAsset).filter_by(asset_key=_cle_asset(resource_type, public_id)).first()
    if row is None or row.username != username or row.version != version:
        raise HTTPException(status_code=403, detail="Tu ne peux pas joindre un fichier qui n'a pas été confirmé par ton compte GRIND.")
    return row


# ---------------------------------------------------------------------------
# Nettoyage des médias publics orphelins après suppression validée en base
# ---------------------------------------------------------------------------

import json
import logging
import threading

from sqlalchemy import event, or_, select
from sqlalchemy.orm import object_session

from database import SessionLocal
from models import Post, PostCommentaire, Annonce, LibraryDocument, ClasseDiscussion, ClasseDiscussionMessage

_logger = logging.getLogger(__name__)
_CLE_URLS_MEDIA_A_PURGER = "grind_media_urls_after_commit"


def _urls_media_public_post(post: Post) -> set[str]:
    """Récupère les images/vidéos et le document attachés à une publication."""
    urls: set[str] = set()
    if post.media_url:
        urls.add(str(post.media_url))
    if post.document_url:
        urls.add(str(post.document_url))
    try:
        elements = json.loads(post.medias_json or "[]")
    except (TypeError, ValueError):
        elements = []
    if isinstance(elements, list):
        for element in elements[:10]:
            if isinstance(element, dict) and element.get("type") in {"image", "video"}:
                url = str(element.get("url") or "").strip()
                if url:
                    urls.add(url)
    return urls


def _enregistrer_urls_a_purger(session, urls) -> None:
    if session is None:
        return
    candidates = session.info.setdefault(_CLE_URLS_MEDIA_A_PURGER, set())
    for url in urls:
        reference = extraire_reference_cloudinary(url)
        if reference is None:
            continue
        resource_type, public_id, _version = reference
        # Limites strictes de dossiers : jamais les médias privés de discussion,
        # les avatars ou les logos d'établissement.
        dossier_post = resource_type in {"image", "video"} and public_id.startswith("grind_posts/")
        dossier_document = resource_type == "raw" and public_id.startswith(("grind_documents/", "grind_library/"))
        if dossier_post or dossier_document:
            candidates.add(str(url))


def _media_encore_reference(db: Session, url: str) -> bool:
    """Protège une ressource si une autre publication, discussion ou Library la référence encore.

    Les URLs Cloudinary peuvent être des variantes transformées. On compare donc le
    type et le public_id, et non l'URL complète.
    """
    reference = extraire_reference_cloudinary(url)
    if reference is None:
        return True
    resource_type, public_id, _version = reference
    motif = f"%{public_id}%"

    checks = (
        db.query(Post.id).filter(or_(Post.media_url.like(motif), Post.document_url.like(motif), Post.medias_json.like(motif))).first(),
        db.query(PostCommentaire.id).filter(or_(PostCommentaire.media_url.like(motif), PostCommentaire.document_url.like(motif))).first(),
        db.query(Annonce.id).filter(or_(Annonce.media_url.like(motif), Annonce.document_url.like(motif))).first(),
        db.query(LibraryDocument.id).filter(LibraryDocument.fichier_url.like(motif)).first(),
        db.query(ClasseDiscussionMessage.id).filter(ClasseDiscussionMessage.piece_url.like(motif)).first(),
        db.query(ClasseDiscussion.id).filter(ClasseDiscussion.photo_url.like(motif)).first(),
    )
    if any(checks):
        return True

    # Le logo d'un établissement est stocké dans un modèle séparé.
    try:
        from models_proviseur import ProviseurCandidature
        if db.query(ProviseurCandidature.id).filter(ProviseurCandidature.logo_url.like(motif)).first():
            return True
    except Exception:
        # En cas d'incertitude sur une référence, on préserve le média.
        _logger.exception("Vérification d'une référence de logo impossible ; média conservé")
        return True

    return False


def _purger_medias_apres_commit(urls: set[str]) -> None:
    """Worker indépendant : le commit SQL ne dépend jamais de la disponibilité Cloudinary."""
    db = SessionLocal()
    try:
        orphelins = [url for url in urls if not _media_encore_reference(db, url)]
    except Exception:
        _logger.exception("Impossible de vérifier les références média ; aucune ressource n'est détruite")
        return
    finally:
        db.close()

    if not orphelins:
        return
    try:
        from media import supprimer_assets_cloudinary
        resultat = supprimer_assets_cloudinary(orphelins)
        _logger.info(
            "Nettoyage média après suppression : %s supprimé(s), %s restant(s)",
            resultat.get("supprimes", 0), resultat.get("non_supprimes", 0),
        )
    except Exception:
        _logger.exception("Le nettoyage Cloudinary a échoué ; les suppressions SQL restent validées")


def _post_avant_suppression(_mapper, connection, target: Post) -> None:
    # Les messages liés sont parfois supprimés par DELETE SQL en masse ; ils ne
    # déclenchent alors pas d'événement ORM. Le post garde ses médias principaux.
    session = object_session(target)
    _enregistrer_urls_a_purger(session, _urls_media_public_post(target))


def _commentaire_avant_suppression(_mapper, _connection, target: PostCommentaire) -> None:
    _enregistrer_urls_a_purger(object_session(target), [target.media_url, target.document_url])


def _annonce_avant_suppression(_mapper, _connection, target: Annonce) -> None:
    _enregistrer_urls_a_purger(object_session(target), [target.media_url, target.document_url])


def _document_library_avant_suppression(_mapper, _connection, target: LibraryDocument) -> None:
    _enregistrer_urls_a_purger(object_session(target), [target.fichier_url])


def _capturer_suppression_sql_masse(execute_state) -> None:
    """Capture les fichiers avant un DELETE ORM en masse (qui contourne mapper.before_delete)."""
    if not execute_state.is_delete:
        return
    statement = execute_state.statement
    table = getattr(getattr(statement, "table", None), "name", None)
    where = getattr(statement, "whereclause", None)
    if table not in {"posts", "post_commentaires", "annonces"} or where is None:
        return

    db = execute_state.session
    if table == "posts":
        rows = db.execute(select(Post.media_url, Post.document_url, Post.medias_json).where(where)).all()
        for media_url, document_url, medias_json in rows:
            urls = [media_url, document_url]
            try:
                elements = json.loads(medias_json or "[]")
            except (TypeError, ValueError):
                elements = []
            if isinstance(elements, list):
                urls.extend(
                    item.get("url") for item in elements[:10]
                    if isinstance(item, dict) and item.get("type") in {"image", "video"}
                )
            _enregistrer_urls_a_purger(db, urls)
    elif table == "post_commentaires":
        rows = db.execute(select(PostCommentaire.media_url, PostCommentaire.document_url).where(where)).all()
        _enregistrer_urls_a_purger(db, (url for row in rows for url in row))
    else:  # annonces
        rows = db.execute(select(Annonce.media_url, Annonce.document_url).where(where)).all()
        _enregistrer_urls_a_purger(db, (url for row in rows for url in row))


def _apres_commit_media(session) -> None:
    urls = session.info.pop(_CLE_URLS_MEDIA_A_PURGER, None)
    if not urls:
        return
    worker = threading.Thread(
        target=_purger_medias_apres_commit,
        args=(set(urls),),
        name="grind-cloudinary-cleanup",
        daemon=True,
    )
    worker.start()


def _apres_rollback_media(session) -> None:
    # Une transaction annulée ne doit jamais entraîner une suppression Cloudinary.
    session.info.pop(_CLE_URLS_MEDIA_A_PURGER, None)


# Enregistre les événements une fois lorsque main.py importe ce module.
event.listen(Post, "before_delete", _post_avant_suppression, propagate=True)
event.listen(PostCommentaire, "before_delete", _commentaire_avant_suppression, propagate=True)
event.listen(Annonce, "before_delete", _annonce_avant_suppression, propagate=True)
event.listen(LibraryDocument, "before_delete", _document_library_avant_suppression, propagate=True)
event.listen(Session, "do_orm_execute", _capturer_suppression_sql_masse)
event.listen(Session, "after_commit", _apres_commit_media)
event.listen(Session, "after_rollback", _apres_rollback_media)
