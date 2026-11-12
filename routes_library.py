"""Bibliothèque publique GRIND : playlists scolaires, documents, votes et avis."""
from pathlib import Path
import zipfile

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from urllib.parse import urlparse
import httpx
from sqlalchemy.orm import Session
from sqlalchemy import or_

from database import get_db
from auth_grind import verifier_session
from models import (LibraryPlaylist, LibraryDocument, LibraryDocumentVote,
                    LibraryDocumentComment, NotificationGrind, CompteGrind)
from library_service import obtenir_playlist_globale
from media import _envoyer_cloudinary
from push_notifications import envoyer_notifications_push

router = APIRouter()

from library_catalog import CATEGORIES

TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".csv": "text/csv",
}


def _user(token: str, db: Session) -> str:
    username = verifier_session(token, db, autoriser_proviseur=True)
    if not username:
        raise HTTPException(status_code=401, detail="Session GRIND invalide ou expirée")
    return username


def _identite(db: Session, username: str):
    compte = db.get(CompteGrind, username)
    if not compte:
        return {"username": username, "nom": username, "avatar": None}
    return {"username": username, "nom": " ".join(x for x in [compte.prenom, compte.nom] if x).strip() or username, "avatar": None}


def _playlist_par_defaut(db: Session, categorie: str | None = None, username: str | None = None):
    return obtenir_playlist_globale(db)


def _doc_payload(db: Session, doc: LibraryDocument, lecteur: str):
    votes = db.query(LibraryDocumentVote).filter_by(document_id=doc.id).all()
    comments = db.query(LibraryDocumentComment).filter_by(document_id=doc.id).count()
    my_vote = next((v.valeur for v in votes if v.username == lecteur), 0)
    playlist = db.get(LibraryPlaylist, doc.playlist_id)
    auteur = _identite(db, doc.auteur_username)
    return {
        "id": doc.id, "playlist_id": doc.playlist_id, "playlist": playlist.titre if playlist else "Library",
        "categorie": doc.categorie_document, "auteur": auteur,
        "titre": doc.titre, "description": doc.description, "fichier_url": doc.fichier_url,
        "fichier_nom": doc.fichier_nom, "fichier_type": doc.fichier_type,
        "taille_octets": doc.taille_octets,
        "date": doc.created_at.date().isoformat() if doc.created_at else None,
        "votes_positifs": sum(1 for v in votes if v.valeur == 1),
        "votes_negatifs": sum(1 for v in votes if v.valeur == -1),
        "mon_vote": my_vote, "commentaires": comments,
    }


@router.get("/api/library/categories")
async def categories_library():
    return {"categories": CATEGORIES}


@router.get("/api/library/playlists")
async def lister_playlists(session_token: str, categorie: str | None = None, q: str | None = None,
                           db: Session = Depends(get_db), offset: int = 0, limit: int = 100):
    lecteur = _user(session_token, db)
    obtenir_playlist_globale(db)
    db.commit()
    query = db.query(LibraryPlaylist).filter(LibraryPlaylist.public == 1)
    if q:
        q = q.strip()[:100]
        query = query.filter(or_(LibraryPlaylist.titre.ilike(f"%{q}%"), LibraryPlaylist.description.ilike(f"%{q}%")))
    total = query.count()
    decalage = max(0, int(offset))
    limite = min(100, max(1, int(limit)))
    playlists = query.order_by(LibraryPlaylist.est_defaut.desc(), LibraryPlaylist.created_at.desc()).offset(decalage).limit(limite).all()
    result = []
    for p in playlists:
        result.append({"id":p.id,"titre":p.titre,"description":p.description,"categorie":p.categorie,"createur":_identite(db,p.createur_username),"est_defaut":bool(p.est_defaut),"documents":db.query(LibraryDocument).filter_by(playlist_id=p.id).count()})
    return {"playlists": result, "total": total, "offset": decalage, "limit": limite, "has_more": decalage + len(result) < total}


@router.post("/api/library/playlists")
async def creer_playlist(data: dict, db: Session = Depends(get_db)):
    username = _user(data.get("session_token", ""), db)
    titre = (data.get("titre") or "").strip()
    description = (data.get("description") or "").strip()
    # Un playsit est une collection libre : seul un nom vide ou trop long est refusé.
    # Aucun catalogue de matières, motif ou longueur minimale artificielle n'est imposé.
    import unicodedata
    titre = unicodedata.normalize("NFC", titre)
    if not titre or len(titre) > 120:
        raise HTTPException(status_code=400, detail="Saisis un nom entre 1 et 120 caractères.")
    p = LibraryPlaylist(titre=titre, description=description[:1200] or None, categorie="Général", createur_username=username, public=1)
    db.add(p); db.commit(); db.refresh(p)
    return {"success":True,"playlist":{"id":p.id,"titre":p.titre,"categorie":p.categorie,"description":p.description}}


@router.post("/api/library/documents")
async def publier_document_library(
    session_token: str = Form(...), categorie: str = Form(""), titre: str = Form(...),
    description: str = Form(""), playlist_id: int | None = Form(None), file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    username = _user(session_token, db)
    obtenir_playlist_globale(db)
    extension = Path(file.filename or "").suffix.lower()
    mime = TYPES.get(extension)
    if not mime:
        raise HTTPException(status_code=415, detail="Format de document non pris en charge.")
    categorie_document = (categorie or "").strip()[:120] or None
    filename = Path(file.filename or "document").name[:180]
    file.file.seek(0, 2); size = file.file.tell(); file.file.seek(0)
    if size <= 0 or size > 30 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Le document doit peser au maximum 30 Mo.")
    head = file.file.read(16); file.file.seek(0)
    if extension == ".pdf" and not head.startswith(b"%PDF-"):
        raise HTTPException(status_code=415, detail="Le fichier PDF est invalide.")
    if extension in (".xlsx", ".docx"):
        try:
            with zipfile.ZipFile(file.file) as z:
                names = set(z.namelist())
                expected = "xl/workbook.xml" if extension == ".xlsx" else "word/document.xml"
                if expected not in names or "[Content_Types].xml" not in names:
                    raise HTTPException(status_code=415, detail="Le document bureautique est invalide.")
        except zipfile.BadZipFile:
            raise HTTPException(status_code=415, detail="Le document bureautique est endommagé.")
        finally:
            file.file.seek(0)
    if extension == ".xls" and not head.startswith(b"\xd0\xcf\x11\xe0"):
        raise HTTPException(status_code=415, detail="Le fichier Excel ancien est invalide.")
    if extension == ".csv":
        try:
            sample = file.file.read(4096); sample.decode("utf-8-sig"); file.file.seek(0)
        except UnicodeDecodeError:
            raise HTTPException(status_code=415, detail="Le CSV doit être encodé en UTF-8.")
    p = db.get(LibraryPlaylist, playlist_id) if playlist_id else None
    if p and not p.public:
        raise HTTPException(status_code=403, detail="Cette playlist n’est pas publique.")
    if not p:
        p = _playlist_par_defaut(db, categorie_document, username)
    import media as media_service
    params = {"folder":"grind_library", "allowed_formats":"pdf,docx,xlsx,xls,csv"}
    result = await media_service._envoyer_cloudinary(file, filename, mime, "raw", params)
    url = result.get("secure_url")
    if not url or not url.startswith("https://res.cloudinary.com/"):
        raise HTTPException(status_code=502, detail="Le stockage n’a pas retourné un fichier exploitable.")
    doc = LibraryDocument(playlist_id=p.id, auteur_username=username, titre=titre.strip()[:180] or filename, description=description.strip()[:2000] or None, fichier_url=url, fichier_nom=filename, fichier_type=mime, taille_octets=size, categorie_document=categorie_document)
    db.add(doc); db.commit(); db.refresh(doc)
    return {"success":True,"document":_doc_payload(db,doc,username)}


@router.get("/api/library/playlists/{playlist_id}/documents")
async def lister_documents_library(playlist_id: int, session_token: str, q: str | None = None,
                                   db: Session = Depends(get_db), offset: int = 0, limit: int = 100):
    username = _user(session_token, db)
    p = db.get(LibraryPlaylist, playlist_id)
    if not p or not p.public:
        raise HTTPException(status_code=404, detail="Playlist introuvable.")
    query = db.query(LibraryDocument).filter_by(playlist_id=playlist_id)
    if q:
        mot = q.strip()[:100]
        query = query.filter(or_(LibraryDocument.titre.ilike(f"%{mot}%"), LibraryDocument.description.ilike(f"%{mot}%"), LibraryDocument.fichier_nom.ilike(f"%{mot}%")))
    total = query.count()
    decalage = max(0, int(offset))
    limite = min(100, max(1, int(limit)))
    docs = query.order_by(LibraryDocument.created_at.desc(), LibraryDocument.id.desc()).offset(decalage).limit(limite).all()
    return {
        "playlist":{"id":p.id,"titre":p.titre,"categorie":p.categorie,"description":p.description},
        "documents":[_doc_payload(db,d,username) for d in docs],
        "total": total, "offset": decalage, "limit": limite,
        "has_more": decalage + len(docs) < total,
    }


@router.get("/api/library/documents")
async def rechercher_documents_library(
    session_token: str, q: str = "", categorie: str | None = None,
    db: Session = Depends(get_db), offset: int = 0, limit: int = 100,
):
    """Recherche globale dans les documents et noms de playlists publics, sans filtre de catégorie."""
    username = _user(session_token, db)
    mot = (q or "").strip()[:100]
    query = db.query(LibraryDocument).join(
        LibraryPlaylist, LibraryPlaylist.id == LibraryDocument.playlist_id
    ).filter(LibraryPlaylist.public == 1)
    if mot:
        query = query.filter(or_(
            LibraryDocument.titre.ilike(f"%{mot}%"),
            LibraryDocument.description.ilike(f"%{mot}%"),
            LibraryDocument.fichier_nom.ilike(f"%{mot}%"),
            LibraryPlaylist.titre.ilike(f"%{mot}%"),
            LibraryPlaylist.description.ilike(f"%{mot}%"),
        ))
    total = query.count()
    decalage = max(0, int(offset))
    limite = min(100, max(1, int(limit)))
    docs = query.order_by(LibraryDocument.created_at.desc(), LibraryDocument.id.desc()).offset(decalage).limit(limite).all()
    return {
        "documents": [_doc_payload(db, doc, username) for doc in docs],
        "total": total, "offset": decalage, "limit": limite,
        "has_more": decalage + len(docs) < total,
    }


@router.post("/api/library/documents/{document_id}/vote")
async def voter_document(document_id: int, data: dict, db: Session = Depends(get_db)):
    username = _user(data.get("session_token", ""), db)
    valeur = data.get("valeur")
    if valeur not in (-1, 1): raise HTTPException(status_code=400, detail="Vote invalide.")
    doc = db.get(LibraryDocument, document_id)
    if not doc: raise HTTPException(status_code=404, detail="Document introuvable.")
    vote = db.query(LibraryDocumentVote).filter_by(document_id=document_id, username=username).first()
    vote_actif = not (vote and vote.valeur == valeur)
    if vote and vote.valeur == valeur:
        db.delete(vote)
    elif vote:
        vote.valeur = valeur
    else:
        db.add(LibraryDocumentVote(document_id=document_id, username=username, valeur=valeur))
    if vote_actif and doc.auteur_username != username:
        db.add(NotificationGrind(destinataire_username=doc.auteur_username, acteur_username=username, type="avis_doc", resource_type="library_document", resource_id=doc.id, extrait=f"a évalué « {doc.titre[:160]} »"))
    notifications_push = [item for item in db.new if isinstance(item, NotificationGrind)]
    db.commit()
    await envoyer_notifications_push(db, notifications_push)
    return {"success":True}


@router.post("/api/library/documents/{document_id}/commentaires")
async def commenter_document(document_id: int, data: dict, db: Session = Depends(get_db)):
    username = _user(data.get("session_token", ""), db)
    contenu = (data.get("contenu") or "").strip()
    if not contenu or len(contenu) > 2000: raise HTTPException(status_code=400, detail="Le commentaire doit contenir entre 1 et 2 000 caractères.")
    doc = db.get(LibraryDocument, document_id)
    if not doc: raise HTTPException(status_code=404, detail="Document introuvable.")
    comment = LibraryDocumentComment(document_id=document_id, username=username, contenu=contenu)
    db.add(comment)
    if doc.auteur_username != username:
        db.add(NotificationGrind(destinataire_username=doc.auteur_username, acteur_username=username, type="commentaire_doc", resource_type="library_document", resource_id=doc.id, extrait=f"a commenté « {doc.titre[:150]} »"))
    notifications_push = [item for item in db.new if isinstance(item, NotificationGrind)]
    db.commit()
    await envoyer_notifications_push(db, notifications_push)
    db.refresh(comment)
    return {"success":True,"commentaire":{"id":comment.id,"username":username,"nom":_identite(db,username)["nom"],"contenu":contenu,"date":comment.created_at.isoformat() if comment.created_at else None}}


@router.get("/api/library/documents/{document_id}/commentaires")
async def lister_commentaires_document(document_id: int, session_token: str, db: Session = Depends(get_db)):
    username = _user(session_token, db)
    if not db.get(LibraryDocument, document_id): raise HTTPException(status_code=404, detail="Document introuvable.")
    rows = db.query(LibraryDocumentComment).filter_by(document_id=document_id).order_by(LibraryDocumentComment.created_at.asc()).limit(300).all()
    return {"commentaires":[{"id":c.id,"username":c.username,"nom":_identite(db,c.username)["nom"],"contenu":c.contenu,"date":c.created_at.isoformat() if c.created_at else None} for c in rows]}



@router.get("/api/library/documents/{document_id}")
async def obtenir_document_library(document_id: int, session_token: str, db: Session = Depends(get_db)):
    username = _user(session_token, db)
    doc = db.get(LibraryDocument, document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document introuvable.")
    playlist = db.get(LibraryPlaylist, doc.playlist_id)
    if not playlist or not playlist.public:
        raise HTTPException(status_code=404, detail="Document introuvable.")
    return {"document": _doc_payload(db, doc, username), "playlist": {"id": playlist.id, "titre": playlist.titre, "categorie": playlist.categorie, "description": playlist.description}}

@router.get("/api/library/documents/{document_id}/preview")
async def previsualiser_document_library(document_id: int, session_token: str, db: Session = Depends(get_db)):
    """Proxy authentifié du fichier public pour une prévisualisation sans dépendre du CORS Cloudinary."""
    _user(session_token, db)
    doc = db.get(LibraryDocument, document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document introuvable.")
    playlist = db.get(LibraryPlaylist, doc.playlist_id)
    if not playlist or not playlist.public:
        raise HTTPException(status_code=404, detail="Document introuvable.")
    url = urlparse(doc.fichier_url or "")
    if url.scheme != "https" or url.hostname != "res.cloudinary.com":
        raise HTTPException(status_code=502, detail="La source du document n'est pas reconnue.")
    contenu = bytearray()
    limite_octets = 30 * 1024 * 1024
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=15.0), follow_redirects=False) as client:
            async with client.stream("GET", doc.fichier_url) as upstream:
                if upstream.status_code != 200:
                    raise HTTPException(status_code=502, detail="Le document n'est pas disponible pour l'aperçu.")
                longueur = upstream.headers.get("content-length")
                if longueur and longueur.isdigit() and int(longueur) > limite_octets:
                    raise HTTPException(status_code=413, detail="Ce document dépasse la taille autorisée pour l'aperçu.")
                async for morceau in upstream.aiter_bytes():
                    contenu.extend(morceau)
                    if len(contenu) > limite_octets:
                        raise HTTPException(status_code=413, detail="Ce document dépasse la taille autorisée pour l'aperçu.")
    except HTTPException:
        raise
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="Impossible de récupérer le document pour l'aperçu.")
    return Response(
        content=bytes(contenu),
        media_type=doc.fichier_type,
        headers={"Cache-Control": "private, max-age=120", "X-Content-Type-Options": "nosniff"},
    )

