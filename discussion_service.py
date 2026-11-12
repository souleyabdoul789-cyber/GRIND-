"""Helpers for the private class discussion groups."""
import secrets
from sqlalchemy.orm import Session
from models import (
    Classe, ClasseMembre, ClasseDiscussion, ClasseDiscussionMembre,
)

MAX_MEMBRES_DISCUSSION = 200


def assurer_discussion(db: Session, classe: Classe) -> ClasseDiscussion:
    """Create the group's one-to-one record for legacy classes and sync real members.

    Class membership remains capped independently at 10. Group-only invitees may
    bring the discussion to 200 without taking a seat in the real class.
    """
    discussion = db.query(ClasseDiscussion).filter_by(classe_id=classe.id).first()
    if discussion is None:
        discussion = ClasseDiscussion(
            classe_id=classe.id,
            owner_username=classe.createur_username,
            nom=classe.nom,
            code_invitation=secrets.token_urlsafe(18),
        )
        db.add(discussion)
        db.flush()

    # Class creator is the discussion owner. Preserve ownership if the teacher
    # role is later transferred: Telegram-style group ownership is separate.
    owner = db.query(ClasseDiscussionMembre).filter_by(
        discussion_id=discussion.id, username=discussion.owner_username
    ).first()
    if owner is None:
        count = db.query(ClasseDiscussionMembre).filter_by(discussion_id=discussion.id, actif=1).count()
        if count < MAX_MEMBRES_DISCUSSION:
            db.add(ClasseDiscussionMembre(
                discussion_id=discussion.id,
                username=discussion.owner_username,
                role="owner",
                actif=1,
                banni=0,
            ))
            # Les sessions GRIND utilisent autoflush=False : il faut persister
            # le propriétaire avant de synchroniser les élèves de la classe.
            db.flush()
    else:
        owner.role = "owner"

    count = db.query(ClasseDiscussionMembre).filter_by(discussion_id=discussion.id, actif=1).count()
    for real_member in db.query(ClasseMembre).filter_by(classe_id=classe.id).all():
        exists = db.query(ClasseDiscussionMembre.id).filter_by(
            discussion_id=discussion.id, username=real_member.username
        ).first()
        # Une ligne inactive est volontaire : ne pas réinscrire automatiquement
        # un membre qui a quitté ou a été exclu du groupe.
        if not exists and count < MAX_MEMBRES_DISCUSSION:
            role = "owner" if real_member.username == discussion.owner_username else "membre"
            db.add(ClasseDiscussionMembre(
                discussion_id=discussion.id,
                username=real_member.username,
                role=role,
            ))
            count += 1
    discussion.nom = classe.nom
    db.flush()
    return discussion
