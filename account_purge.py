"""Purge définitif et transactionnel d'un compte GRIND et de ses données."""
import json
from sqlalchemy import or_

from models import (
    CompteGrind, GrindSession, GrindDeviceAccount, Profil, Classe, ClasseMembre,
    ClasseFeuille, ClasseFeuillePermission, ClasseTableau, DemandeAide,
    AideParticipant, Post, PostLike, PostCommentaire, PostCommentaireLike,
    AvatarConfig, AvatarDeblocage, Signalement, Sanction, SupportConversation,
    SupportMessage, Examen, Blocage, VerificationEmail, NotificationGrind,
    LibraryPlaylist, LibraryDocument, LibraryDocumentVote, LibraryDocumentComment, GrindPushToken,
    ClasseDiscussion, ClasseDiscussionMembre, ClasseDiscussionMessage,
    ClasseDiscussionMention, CountryAccessBlock, GrindMediaAsset,
    GrindMediaAccessTicket,
)
from models_proviseur import ProviseurCandidature, ProviseurDocument, ProviseurAutorisation, Etablissement
from library_service import obtenir_playlist_globale


def purger_compte(db, username: str) -> list[str]:
    """Supprime toutes les données appartenant au compte. Appelant = transaction DB."""
    urls_media = []
    compte = db.get(CompteGrind, username)
    if compte is None:
        return urls_media

    # Publications et commentaires (y compris les réactions sur les commentaires).
    post_ids = [x[0] for x in db.query(Post.id).filter(Post.auteur_username == username).all()]
    commentaire_ids = [x[0] for x in db.query(PostCommentaire.id).filter(
        or_(PostCommentaire.username == username, PostCommentaire.post_id.in_(post_ids or [-1]))
    ).all()]
    posts = db.query(Post).filter(Post.id.in_(post_ids or [-1])).all()
    for post in posts:
        for url in (post.media_url, post.document_url):
            if url: urls_media.append(url)
        if post.medias_json:
            try:
                medias = json.loads(post.medias_json)
                urls_media.extend(item.get("url") for item in medias if isinstance(item, dict) and item.get("url"))
            except (TypeError, ValueError):
                pass
    commentaires = db.query(PostCommentaire).filter(PostCommentaire.id.in_(commentaire_ids or [-1])).all()
    for commentaire in commentaires:
        if commentaire.media_url: urls_media.append(commentaire.media_url)
        if commentaire.document_url: urls_media.append(commentaire.document_url)
    db.query(PostCommentaireLike).filter(or_(
        PostCommentaireLike.username == username,
        PostCommentaireLike.commentaire_id.in_(commentaire_ids or [-1]),
    )).delete(synchronize_session=False)
    db.query(PostLike).filter(or_(PostLike.username == username, PostLike.post_id.in_(post_ids or [-1]))).delete(synchronize_session=False)
    db.query(PostCommentaire).filter(PostCommentaire.id.in_(commentaire_ids or [-1])).delete(synchronize_session=False)
    db.query(Post).filter(Post.id.in_(post_ids or [-1])).delete(synchronize_session=False)

    # Classes créées par la personne : la classe et sa discussion sont supprimées.
    owned_class_ids = [x[0] for x in db.query(Classe.id).filter(Classe.createur_username == username).all()]
    discussion_ids = [x[0] for x in db.query(ClasseDiscussion.id).filter(
        ClasseDiscussion.classe_id.in_(owned_class_ids or [-1])
    ).all()]
    for discussion in db.query(ClasseDiscussion).filter(ClasseDiscussion.classe_id.in_(owned_class_ids or [-1])).all():
        if discussion.photo_url: urls_media.append(discussion.photo_url)
    discussion_message_ids = [x[0] for x in db.query(ClasseDiscussionMessage.id).filter(
        ClasseDiscussionMessage.discussion_id.in_(discussion_ids or [-1])
    ).all()]
    db.query(ClasseDiscussionMention).filter(or_(
        ClasseDiscussionMention.username == username,
        ClasseDiscussionMention.message_id.in_(discussion_message_ids or [-1]),
    )).delete(synchronize_session=False)
    group_messages = db.query(ClasseDiscussionMessage).filter(or_(
        ClasseDiscussionMessage.auteur_username == username,
        ClasseDiscussionMessage.discussion_id.in_(discussion_ids or [-1]),
    )).all()
    for message in group_messages:
        if message.piece_url: urls_media.append(message.piece_url)
    db.query(ClasseDiscussionMessage).filter(or_(
        ClasseDiscussionMessage.auteur_username == username,
        ClasseDiscussionMessage.discussion_id.in_(discussion_ids or [-1]),
    )).delete(synchronize_session=False)
    db.query(ClasseDiscussionMembre).filter(or_(
        ClasseDiscussionMembre.username == username,
        ClasseDiscussionMembre.discussion_id.in_(discussion_ids or [-1]),
    )).delete(synchronize_session=False)
    db.query(ClasseDiscussion).filter(ClasseDiscussion.id.in_(discussion_ids or [-1])).delete(synchronize_session=False)
    db.query(ClasseFeuillePermission).filter(or_(
        ClasseFeuillePermission.proprietaire_username == username,
        ClasseFeuillePermission.lecteur_username == username,
        ClasseFeuillePermission.classe_id.in_(owned_class_ids or [-1]),
    )).delete(synchronize_session=False)
    db.query(ClasseFeuille).filter(or_(
        ClasseFeuille.username == username,
        ClasseFeuille.classe_id.in_(owned_class_ids or [-1]),
    )).delete(synchronize_session=False)
    db.query(ClasseTableau).filter(ClasseTableau.classe_id.in_(owned_class_ids or [-1])).delete(synchronize_session=False)
    db.query(ClasseMembre).filter(or_(
        ClasseMembre.username == username,
        ClasseMembre.classe_id.in_(owned_class_ids or [-1]),
    )).delete(synchronize_session=False)
    db.query(Classe).filter(Classe.id.in_(owned_class_ids or [-1])).delete(synchronize_session=False)

    # Dans les classes d'autrui, on retire seulement le membre et ses feuilles.
    db.query(ClasseFeuillePermission).filter(or_(
        ClasseFeuillePermission.proprietaire_username == username,
        ClasseFeuillePermission.lecteur_username == username,
    )).delete(synchronize_session=False)
    db.query(ClasseFeuille).filter_by(username=username).delete(synchronize_session=False)
    db.query(ClasseMembre).filter_by(username=username).delete(synchronize_session=False)
    db.query(Classe).filter(Classe.prof_username == username).update({Classe.prof_username: None}, synchronize_session=False)

    # Discussions de classes conservées : transfert du rôle owner avant retrait du compte.
    owned_discussions = db.query(ClasseDiscussion).filter(ClasseDiscussion.owner_username == username).all()
    for discussion in owned_discussions:
        classe = db.get(Classe, discussion.classe_id)
        successeur = None
        if classe:
            candidats = [classe.prof_username, classe.createur_username]
            for candidat in candidats:
                if candidat and candidat != username and db.get(CompteGrind, candidat):
                    successeur = candidat
                    break
        if successeur:
            discussion.owner_username = successeur
            membre = db.query(ClasseDiscussionMembre).filter_by(discussion_id=discussion.id, username=successeur).first()
            if membre:
                membre.role = "owner"
            else:
                db.add(ClasseDiscussionMembre(discussion_id=discussion.id, username=successeur, role="owner", actif=1, banni=0))
        else:
            # Cas anormal : aucun autre propriétaire possible. La discussion est fermée proprement.
            db.query(ClasseDiscussionMention).filter(ClasseDiscussionMention.message_id.in_(
                [x[0] for x in db.query(ClasseDiscussionMessage.id).filter_by(discussion_id=discussion.id).all()] or [-1]
            )).delete(synchronize_session=False)
            db.query(ClasseDiscussionMessage).filter_by(discussion_id=discussion.id).delete(synchronize_session=False)
            db.query(ClasseDiscussionMembre).filter_by(discussion_id=discussion.id).delete(synchronize_session=False)
            db.delete(discussion)
    # Nettoyer les messages/mentions de la personne dans les discussions qui restent.
    authored_message_ids = [x[0] for x in db.query(ClasseDiscussionMessage.id).filter_by(auteur_username=username).all()]
    message_ids_supprimes = list(dict.fromkeys(discussion_message_ids + authored_message_ids))
    db.query(ClasseDiscussionMention).filter(or_(
        ClasseDiscussionMention.username == username,
        ClasseDiscussionMention.message_id.in_(authored_message_ids or [-1]),
    )).delete(synchronize_session=False)
    authored_messages = db.query(ClasseDiscussionMessage).filter_by(auteur_username=username).all()
    for message in authored_messages:
        if message.piece_url: urls_media.append(message.piece_url)
    db.query(ClasseDiscussionMessage).filter_by(auteur_username=username).delete(synchronize_session=False)
    db.query(ClasseDiscussionMembre).filter_by(username=username).delete(synchronize_session=False)

    # Groupes d'aide.
    owned_help_ids = [x[0] for x in db.query(DemandeAide.id).filter(DemandeAide.demandeur_username == username).all()]
    db.query(AideParticipant).filter(or_(AideParticipant.username == username, AideParticipant.demande_id.in_(owned_help_ids or [-1]))).delete(synchronize_session=False)
    db.query(DemandeAide).filter(DemandeAide.id.in_(owned_help_ids or [-1])).delete(synchronize_session=False)

    # Library : playsits créés par la personne et documents qu'elle a partagés.
    owned_playlist_ids = [x[0] for x in db.query(LibraryPlaylist.id).filter(
        LibraryPlaylist.createur_username == username,
        LibraryPlaylist.est_defaut != 1,
    ).all()]

    # Les documents sont la propriété de leur auteur, pas du créateur de la playlist.
    # Les documents d'autres membres sont conservés et déplacés vers l'espace public global
    # avant la suppression de la playlist qui les contenait.
    documents_d_auteurs = db.query(LibraryDocument).filter(
        LibraryDocument.auteur_username == username
    ).all()
    document_ids = [doc.id for doc in documents_d_auteurs]
    for doc in documents_d_auteurs:
        if doc.fichier_url:
            urls_media.append(doc.fichier_url)

    if owned_playlist_ids:
        playlist_globale = obtenir_playlist_globale(db)
        db.query(LibraryDocument).filter(
            LibraryDocument.playlist_id.in_(owned_playlist_ids),
            LibraryDocument.auteur_username != username,
        ).update({LibraryDocument.playlist_id: playlist_globale.id}, synchronize_session=False)

    db.query(LibraryDocumentVote).filter(or_(
        LibraryDocumentVote.username == username,
        LibraryDocumentVote.document_id.in_(document_ids or [-1]),
    )).delete(synchronize_session=False)
    db.query(LibraryDocumentComment).filter(or_(
        LibraryDocumentComment.username == username,
        LibraryDocumentComment.document_id.in_(document_ids or [-1]),
    )).delete(synchronize_session=False)
    db.query(LibraryDocument).filter(LibraryDocument.id.in_(document_ids or [-1])).delete(synchronize_session=False)
    db.query(LibraryPlaylist).filter(LibraryPlaylist.id.in_(owned_playlist_ids or [-1])).delete(synchronize_session=False)

    # Données proviseur privées et établissements créés.
    candidature = db.query(ProviseurCandidature).filter_by(username=username).first()
    if candidature:
        if candidature.logo_url: urls_media.append(candidature.logo_url)
        db.query(ProviseurDocument).filter_by(candidature_id=candidature.id).delete(synchronize_session=False)
        db.query(ProviseurAutorisation).filter_by(candidature_id=candidature.id).delete(synchronize_session=False)
        db.delete(candidature)
    etablissements_crees = db.query(Etablissement).filter_by(cree_par=username).all()
    for etablissement in etablissements_crees:
        autres_candidats = db.query(ProviseurCandidature.id).filter(
            ProviseurCandidature.etablissement_id == etablissement.id,
            ProviseurCandidature.username != username,
        ).first()
        autres_autorisations = db.query(ProviseurAutorisation.id).filter(
            ProviseurAutorisation.etablissement_id == etablissement.id,
        ).first()
        publications_etablissement = db.query(Post.id).filter(Post.etablissement_id == etablissement.id).first()
        if autres_candidats or autres_autorisations or publications_etablissement:
            etablissement.cree_par = "GRIND"
        else:
            db.delete(etablissement)
    db.query(ProviseurCandidature).filter(ProviseurCandidature.examine_par == username).update(
        {ProviseurCandidature.examine_par: None}, synchronize_session=False
    )
    db.query(Examen).filter(Examen.traite_par == username).update({Examen.traite_par: None}, synchronize_session=False)

    # Conversations support appartenant au compte + messages envoyés dans d'autres conversations.
    support_ids = [x[0] for x in db.query(SupportConversation.id).filter_by(username=username).all()]
    db.query(SupportMessage).filter(or_(
        SupportMessage.conversation_id.in_(support_ids or [-1]),
        SupportMessage.auteur_username == username,
    )).delete(synchronize_session=False)
    db.query(SupportConversation).filter(SupportConversation.id.in_(support_ids or [-1])).delete(synchronize_session=False)

    # Autres données liées au compte.
    db.query(GrindSession).filter_by(username=username).delete(synchronize_session=False)
    db.query(GrindDeviceAccount).filter_by(username=username).delete(synchronize_session=False)
    db.query(GrindPushToken).filter_by(username=username).delete(synchronize_session=False)
    db.query(Profil).filter_by(username=username).delete(synchronize_session=False)
    db.query(AvatarConfig).filter_by(username=username).delete(synchronize_session=False)
    db.query(AvatarDeblocage).filter_by(username=username).delete(synchronize_session=False)
    db.query(Signalement).filter(or_(Signalement.signaleur_username == username, Signalement.cible_username == username, Signalement.traite_par == username)).delete(synchronize_session=False)
    db.query(Sanction).filter(or_(Sanction.username == username, Sanction.par == username)).delete(synchronize_session=False)
    db.query(Examen).filter(or_(Examen.username == username, Examen.traite_par == username)).delete(synchronize_session=False)
    db.query(Blocage).filter(or_(Blocage.bloqueur_username == username, Blocage.bloque_username == username)).delete(synchronize_session=False)
    db.query(VerificationEmail).filter_by(username=username).delete(synchronize_session=False)
    db.query(NotificationGrind).filter(or_(
        NotificationGrind.destinataire_username == username,
        NotificationGrind.acteur_username == username,
        NotificationGrind.post_id.in_(post_ids or [-1]),
        NotificationGrind.commentaire_id.in_(commentaire_ids or [-1]),
        NotificationGrind.classe_id.in_(owned_class_ids or [-1]),
        ((NotificationGrind.resource_type == "library_document") & NotificationGrind.resource_id.in_(document_ids or [-1])),
        ((NotificationGrind.resource_type == "classe_discussion_message") & NotificationGrind.resource_id.in_(message_ids_supprimes or [-1])),
    )).delete(synchronize_session=False)
    db.query(CountryAccessBlock).filter(CountryAccessBlock.modifie_par == username).update({CountryAccessBlock.modifie_par: None}, synchronize_session=False)

    # Les tickets et le registre média référencent le compte par clé étrangère.
    # Ils doivent être supprimés avant CompteGrind pour que la purge définitive
    # reste transactionnelle et ne soit pas bloquée par les nouvelles tables 2.10.9.
    db.query(GrindMediaAccessTicket).filter_by(username=username).delete(synchronize_session=False)
    db.query(GrindMediaAsset).filter_by(username=username).delete(synchronize_session=False)

    db.delete(compte)
    db.flush()
    return urls_media
