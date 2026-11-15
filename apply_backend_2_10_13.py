#!/usr/bin/env python3
"""Apply verified GRIND 2.10.13 feed-media cleanup to backend main.

Run from the backend repository root. Existing route files are hash-checked,
backed up, then minimally patched. A new shared service module is added.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

ROOT = Path.cwd()
EXPECTED = {
    "routes_posts.py": "91b69230f4cad6af44020f351d16e11661b2b7b5",
    "routes_moderation.py": "32365c17c85af5561a5475e38a34e4bb970e3333",
}
HELPER_SOURCE = Path(__file__).with_name("post_media_cleanup.py").read_bytes()


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode("ascii") + data).hexdigest()


def replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f"Patch annulé : bloc « {label} » trouvé {count} fois au lieu d'une.")
    return source.replace(old, new, 1)


def prepare_posts(source: str) -> str:
    source = replace_once(
        source,
        "from fastapi import APIRouter, Depends, HTTPException\n",
        "from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException\n",
        "import BackgroundTasks des suppressions",
    )
    source = replace_once(
        source,
        "from push_notifications import envoyer_notifications_push\n",
        "from push_notifications import envoyer_notifications_push\nfrom post_media_cleanup import supprimer_post_et_programmer_nettoyage\n",
        "import du nettoyage média partagé",
    )

    old_delete = '''@router.delete("/api/posts/{post_id}")
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
'''
    new_delete = '''@router.delete("/api/posts/{post_id}")
async def supprimer_post(
    post_id: int,
    session_token: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    username = _identite(session_token, db)

    post = db.get(Post, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="Post introuvable")
    _verifier_acces_post(username, post, db)
    if post.auteur_username != username:
        raise HTTPException(status_code=403, detail="Seul l'auteur peut supprimer ce post")

    nombre_medias = supprimer_post_et_programmer_nettoyage(db, post, background_tasks)
    db.commit()
    return {"success": True, "nettoyage_medias_programme": nombre_medias}
'''
    source = replace_once(source, old_delete, new_delete, "suppression du post par son auteur")

    old_moderation = '''@router.post("/api/proviseur/posts/{post_id}/moderation")
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
'''
    new_moderation = '''@router.post("/api/proviseur/posts/{post_id}/moderation")
async def moderation_locale_post(
    post_id: int,
    data: LikeToggle,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Retrait local d'un contenu : seulement dans les établissements confiés au proviseur."""
    username = verifier_session(data.session_token, db, autoriser_proviseur=True)
    if not username:
        raise HTTPException(status_code=401, detail="Session invalide.")
    post = db.get(Post, post_id)
    if post is None or post.etablissement_id is None:
        raise HTTPException(status_code=404, detail="Publication institutionnelle introuvable.")
    _autorisation_etablissement(username, post.etablissement_id, db, besoin_moderation=True)
    nombre_medias = supprimer_post_et_programmer_nettoyage(db, post, background_tasks)
    db.commit()
    return {"success": True, "nettoyage_medias_programme": nombre_medias}
'''
    source = replace_once(source, old_moderation, new_moderation, "suppression d'une publication institutionnelle")
    return source


def prepare_moderation(source: str) -> str:
    source = replace_once(
        source,
        "from database import get_db\n",
        "from database import get_db\nfrom post_media_cleanup import supprimer_post_et_programmer_nettoyage, supprimer_annonce_et_programmer_nettoyage\n",
        "import du nettoyage média partagé",
    )
    source = replace_once(
        source,
        "def _supprimer_contenu(sig: Signalement, db: Session) -> bool:",
        "def _supprimer_contenu(sig: Signalement, db: Session, background_tasks: BackgroundTasks) -> bool:",
        "signature de suppression depuis signalement",
    )
    old_signalement = '''        db.query(PostLike).filter(PostLike.post_id == post_id).delete()
        db.query(PostCommentaire).filter(PostCommentaire.post_id == post_id).delete()
        db.delete(post)
        return True'''
    new_signalement = '''        supprimer_post_et_programmer_nettoyage(db, post, background_tasks)
        return True'''
    source = replace_once(source, old_signalement, new_signalement, "purge média par signalement")
    source = replace_once(
        source,
        "async def traiter_signalement(signalement_id: int, data: AdminTraiter, db: Session = Depends(get_db)):",
        "async def traiter_signalement(signalement_id: int, data: AdminTraiter, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):",
        "tâches de fond du traitement des signalements",
    )
    source = replace_once(
        source,
        "supprime = _supprimer_contenu(sig, db)",
        "supprime = _supprimer_contenu(sig, db, background_tasks)",
        "passage de BackgroundTasks au signalement",
    )

    source = replace_once(
        source,
        "def _supprimer_post(db: Session, post_id: int) -> bool:",
        "def _supprimer_post(db: Session, post_id: int, background_tasks: BackgroundTasks) -> bool:",
        "signature de purge administrative",
    )
    old_bulk = '''    db.query(PostLike).filter(PostLike.post_id == post_id).delete()
    db.query(PostCommentaire).filter(PostCommentaire.post_id == post_id).delete()
    db.delete(post)
    return True'''
    new_bulk = '''    supprimer_post_et_programmer_nettoyage(db, post, background_tasks)
    return True'''
    source = replace_once(source, old_bulk, new_bulk, "purge média lors de suppression administrative")
    source = replace_once(
        source,
        "async def supprimer_contenu_admin(data: AdminSupprimerContenu, db: Session = Depends(get_db)):",
        "async def supprimer_contenu_admin(data: AdminSupprimerContenu, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):",
        "tâches de fond pour suppression de contenu admin",
    )
    source = replace_once(
        source,
        "ok = _supprimer_post(db, identifiant)",
        "ok = _supprimer_post(db, identifiant, background_tasks)",
        "passage des tâches à la suppression admin",
    )
    source = replace_once(
        source,
        "async def tout_supprimer(data: AdminToutSupprimer, db: Session = Depends(get_db)):",
        "async def tout_supprimer(data: AdminToutSupprimer, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):",
        "tâches de fond pour purge totale de compte",
    )
    source = replace_once(
        source,
        "_supprimer_post(db, post_id)",
        "_supprimer_post(db, post_id, background_tasks)",
        "passage des tâches pour chaque post supprimé",
    )

    source = replace_once(
        source,
        "async def supprimer_annonce(data: AdminAnnonceSupprimer, db: Session = Depends(get_db)):",
        "async def supprimer_annonce(data: AdminAnnonceSupprimer, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):",
        "tâche de fond pour suppression d'annonce",
    )
    source = replace_once(
        source,
        "    db.delete(annonce)\n    db.commit()\n    return {\"success\": True}",
        "    nettoyage_programme = supprimer_annonce_et_programmer_nettoyage(db, annonce, background_tasks)\n    db.commit()\n    return {\"success\": True, \"nettoyage_medias_programme\": nettoyage_programme}",
        "purge de l'image/vidéo d'une annonce supprimée",
    )
    return source


def main() -> None:
    if not (ROOT / ".git").exists():
        raise SystemExit("Lance ce script depuis la racine du dépôt backend GRIND- (dossier .git requis).")
    original: dict[str, bytes] = {}
    for relative, expected in EXPECTED.items():
        path = ROOT / relative
        if not path.is_file():
            raise SystemExit(f"Fichier requis introuvable : {relative}. Aucun fichier n'a été modifié.")
        raw = path.read_bytes()
        actual = blob_sha(raw)
        if actual != expected:
            raise SystemExit(
                f"Base backend inattendue pour {relative}. Aucun fichier n'a été modifié.\n"
                f"SHA attendu : {expected}\nSHA trouvé : {actual}"
            )
        original[relative] = raw

    helper_path = ROOT / "post_media_cleanup.py"
    if helper_path.exists():
        raise SystemExit("post_media_cleanup.py existe déjà. Aucun fichier n'a été modifié ; vérifie son contenu avant de continuer.")

    updated = {
        "routes_posts.py": prepare_posts(original["routes_posts.py"].decode("utf-8")).encode("utf-8"),
        "routes_moderation.py": prepare_moderation(original["routes_moderation.py"].decode("utf-8")).encode("utf-8"),
    }
    backups = {name: (ROOT / name).with_name(name + ".2.10.12.bak") for name in updated}
    for name, backup in backups.items():
        if backup.exists():
            raise SystemExit(f"Sauvegarde existante : {backup}. Aucun fichier n'a été remplacé.")

    for name, raw in original.items():
        backups[name].write_bytes(raw)
    for name, content in updated.items():
        path = ROOT / name
        temp = path.with_name(path.name + ".grind-2.10.13.tmp")
        temp.write_bytes(content)
        os.replace(temp, path)
    helper_path.write_bytes(HELPER_SOURCE)

    print("Correctifs backend GRIND 2.10.13 appliqués à routes_posts.py, routes_moderation.py et post_media_cleanup.py.")
    print("Sauvegardes créées : routes_posts.py.2.10.12.bak et routes_moderation.py.2.10.12.bak")
    print("Purge Cloudinary programmée en tâche de fond pour les médias de publication réellement orphelins.")


if __name__ == "__main__":
    main()
