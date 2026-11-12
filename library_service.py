"""Règles communes de la Library : playsits libres, catégorie portée par le document."""
from models import LibraryPlaylist, LibraryDocument


PLAYLIST_GLOBALE_TITRE = "Documents partagés"


def obtenir_playlist_globale(db):
    playlist = db.query(LibraryPlaylist).filter_by(est_defaut=1, categorie="Général").order_by(LibraryPlaylist.id.asc()).first()
    if playlist is None:
        playlist = LibraryPlaylist(
            titre=PLAYLIST_GLOBALE_TITRE,
            description="Bibliothèque publique des documents partagés par la communauté GRIND.",
            categorie="Général", createur_username="GRIND", est_defaut=1, public=1,
        )
        db.add(playlist)
        db.flush()

    # Migration douce et idempotente : les anciens playsits système par matière
    # deviennent un seul espace global ; leur ancienne matière reste sur le document.
    anciens = db.query(LibraryPlaylist).filter(
        LibraryPlaylist.est_defaut == 1,
        LibraryPlaylist.id != playlist.id,
    ).all()
    for ancien in anciens:
        docs = db.query(LibraryDocument).filter_by(playlist_id=ancien.id).all()
        for doc in docs:
            if not doc.categorie_document:
                doc.categorie_document = (ancien.categorie or "").strip()[:120] or None
            doc.playlist_id = playlist.id
        db.delete(ancien)
    db.flush()
    return playlist
