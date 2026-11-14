import json
import re
import secrets
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import get_db
from auth_grind import verifier_session
from schemas import ProfilCreate, ClasseCreate, ClasseRejoindre, ClasseRenommer, ClasseTransfererProf
from moderation import badge_de, sanction_active, usernames_sanctionnes
from scolaire import (
    ErreurValidation, valider_age, valider_texte_scolaire, valider_difficultes_libres,
    valider_texte_libre, valider_classe, cle_doublon_classe, composer_nom_classe,
)
from models_proviseur import ProviseurCandidature, ProviseurAutorisation, Etablissement
from models import (Profil, Classe, ClasseMembre, CompteGrind, ClasseDiscussion,
                    ClasseDiscussionMembre, ClasseDiscussionMessage, ClasseDiscussionMention, NotificationGrind,
                    ClasseFeuille, ClasseFeuillePermission, ClasseTableau)
from discussion_service import assurer_discussion
from ws_manager import manager
from push_notifications import envoyer_push_a_utilisateur
from classe_salle_service import assurer_prof_et_sieges, prochain_siege

router = APIRouter()


def _identite(session_token: str, db: Session) -> str:
    username = verifier_session(session_token, db)
    if username is None:
        raise HTTPException(status_code=401, detail="Session GRIND invalide ou expirée — reconnecte-toi")
    return username


@router.post("/api/profil")
async def creer_ou_maj_profil(data: ProfilCreate, db: Session = Depends(get_db)):
    username = _identite(data.session_token, db)

    profil = db.get(Profil, username)
    if profil is None:
        profil = Profil(username=username)
        db.add(profil)

    # Validation serveur souple : les appellations de classes, filières et matières
    # varient selon les pays et les parcours général/professionnel.
    try:
        age = valider_age(data.age)
        niveau = valider_texte_scolaire(data.niveau, min_len=2, max_len=80, nom="La classe / le niveau")
        pays = (data.pays or "").strip()
        pays = pays.lower() if re.fullmatch(r"[A-Za-z]{2}", pays) else valider_texte_scolaire(pays, min_len=2, max_len=60, nom="Le pays")
        filiere = valider_texte_scolaire(data.filiere, min_len=2, max_len=100, nom="La filière", facultatif=True)
        etablissement = valider_texte_scolaire(data.etablissement, min_len=2, max_len=160, nom="L'établissement", facultatif=True)
        difficultes = valider_difficultes_libres(data.difficultes)
    except ErreurValidation as erreur:
        raise HTTPException(status_code=400, detail=str(erreur))

    profil.age = age
    profil.pays = pays
    profil.niveau = niveau
    champs_envoyes = getattr(data, "model_fields_set", None)
    if champs_envoyes is None:  # compatibilité Pydantic 1.x
        champs_envoyes = getattr(data, "__fields_set__", set())
    if "filiere" in champs_envoyes:
        profil.filiere = filiere
    if "etablissement" in champs_envoyes:
        profil.etablissement = etablissement
    profil.difficultes = json.dumps(difficultes, ensure_ascii=False)
    db.commit()

    return {"success": True, "message": "Profil enregistré"}



@router.post("/api/profil/nom-utilisateur")
async def modifier_nom_utilisateur(data: dict, db: Session = Depends(get_db)):
    """Change l'identifiant public et propage le renommage aux références internes."""
    username_actuel = _identite(data.get("session_token", ""), db)
    nouveau = (data.get("nouveau_username") or "").strip()
    if not re.fullmatch(r"[A-Za-z_]{3,24}", nouveau):
        raise HTTPException(status_code=400, detail="Le nom doit contenir 3 à 24 lettres ou underscores, sans espace ni @.")
    if nouveau.lower() == username_actuel.lower():
        return {"success": True, "username": username_actuel, "changed": False}
    from sqlalchemy import func, update
    from database import Base
    if db.query(CompteGrind).filter(func.lower(CompteGrind.username) == nouveau.lower()).first():
        raise HTTPException(status_code=409, detail="Ce nom de profil est déjà utilisé.")
    try:
        # Mettre à jour les tables dépendantes avant la clé primaire du compte.
        for table in Base.metadata.sorted_tables:
            if table.name == CompteGrind.__tablename__:
                continue
            for colonne in table.columns:
                if colonne.name == "username" or colonne.name.endswith("_username") or colonne.name == "traite_par":
                    db.execute(update(table).where(colonne == username_actuel).values({colonne.name: nouveau}))
        db.execute(update(CompteGrind.__table__).where(CompteGrind.username == username_actuel).values(username=nouveau))
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="Le changement de nom n'a pas pu être appliqué. Aucune modification n'a été validée.")
    return {"success": True, "username": nouveau, "changed": True}


@router.get("/api/profil/moi")
async def lire_mon_profil(session_token: str, db: Session = Depends(get_db)):
    username = _identite(session_token, db)
    profil = db.get(Profil, username)
    compte = db.get(CompteGrind, username)
    profil_complet = bool(
        profil is not None
        and profil.age is not None
        and bool((profil.pays or "").strip())
        and bool((profil.niveau or "").strip())
    )

    return {
        "username": username,
        "prenom": compte.prenom if compte else None,
        "nom": compte.nom if compte else None,
        "profil_existe": profil is not None,
        "profil_complet": profil_complet,
        "age": profil.age if profil else None,
        "pays": profil.pays if profil else None,
        "niveau": profil.niveau if profil else None,
        "filiere": profil.filiere if profil else None,
        "etablissement": profil.etablissement if profil else None,
        "difficultes": profil.difficultes if profil else None,
    }


@router.get("/api/profil/de/{username}")
async def lire_profil_de(username: str, db: Session = Depends(get_db)):
    """Public — nécessaire pour qu'un pseudo cliqué dans le fil ouvre
    le profil de son auteur. Rien de sensible dedans (mêmes infos que
    chacun affiche déjà sur son propre profil)."""
    profil = db.get(Profil, username)
    candidature = db.query(ProviseurCandidature).filter_by(username=username, statut="approuve").first()
    etablissements_prov = []
    if candidature:
        etablissements_prov = [
            e.nom for a, e in db.query(ProviseurAutorisation, Etablissement).join(
                Etablissement, Etablissement.id == ProviseurAutorisation.etablissement_id
            ).filter(ProviseurAutorisation.candidature_id == candidature.id, ProviseurAutorisation.actif == 1).all()
        ]
    return {
        "username": username,
        "badge": badge_de(db, username),
        "proviseur": ({"badge": "Proviseur", "nom_etablissement": candidature.etablissement_declare,
                       "logo_url": candidature.logo_url, "etablissements": etablissements_prov} if candidature else None),
        "sanctionne": sanction_active(db, username) is not None,
        "profil_existe": profil is not None,
        # L'âge est une donnée personnelle : il n'est jamais exposé par la route publique.
        "pays": profil.pays if profil else None,
        "niveau": profil.niveau if profil else None,
        "filiere": profil.filiere if profil else None,
        "etablissement": profil.etablissement if profil else None,
        "difficultes": profil.difficultes if profil else None,
    }


@router.post("/api/classes")
async def creer_classe(data: ClasseCreate, db: Session = Depends(get_db)):
    username = _identite(data.session_token, db)

    try:
        # Nouveau client : le nom seul suffit. Les colonnes scolaires historiques
        # sont nullable dans le modèle Classe ; aucun changement SQL n'est requis.
        if data.nom is not None:
            # Si le nouveau client a envoyé le champ nom, il doit être valide :
            # un nom vide ne retombe jamais silencieusement sur l'ancien catalogue.
            nom_libre = valider_texte_scolaire(
                data.nom, min_len=2, max_len=128, nom="Le nom de la classe", facultatif=False
            )
            valide = {
                "nom": nom_libre,
                "niveau": None,
                "matiere": None,
                "complement": None,
            }
        else:
            # Compatibilité ascendante : les anciennes versions qui envoient
            # niveau/matière/complément continuent à créer leurs classes.
            valide = valider_classe(data.niveau or "", data.matiere or "", data.complement)
    except ErreurValidation as erreur:
        raise HTTPException(status_code=400, detail=str(erreur))

    existantes = db.query(Classe).filter(Classe.createur_username == username).all()
    if len(existantes) >= 10:
        raise HTTPException(status_code=400, detail="Tu as atteint la limite de 10 classes.")
    if any(cle_doublon_classe(c.nom) == cle_doublon_classe(valide["nom"]) for c in existantes):
        raise HTTPException(status_code=400, detail="Tu as déjà une classe avec ce nom.")

    code = secrets.token_urlsafe(8)
    classe = Classe(
        nom=valide["nom"], niveau=valide["niveau"], matiere=valide["matiere"], complement=valide["complement"],
        code_invitation=code, createur_username=username, prof_username=username,
    )
    # Une seule transaction : la classe ne peut pas rester sans propriétaire
    # ni discussion si une étape intermédiaire échoue. assurer_discussion()
    # utilise flush() et ne valide jamais la transaction elle-même.
    try:
        db.add(classe)
        db.flush()
        db.add(ClasseMembre(classe_id=classe.id, username=username, role="prof", siege_index=None))
        assurer_discussion(db, classe)
        db.commit()
        db.refresh(classe)
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="La classe n'a pas pu être créée. Réessaie sans fermer l'application.")

    return {
        "success": True,
        "classe_id": classe.id,
        "nom": classe.nom,
        "lien_invitation": f"/rejoindre/{code}",
    }


@router.post("/api/classes/rejoindre")
async def rejoindre_classe(data: ClasseRejoindre, db: Session = Depends(get_db)):
    username = _identite(data.session_token, db)

    classe = db.query(Classe).filter(Classe.code_invitation == data.code_invitation).first()
    if classe is None:
        raise HTTPException(status_code=404, detail="Lien d'invitation invalide")

    if sanction_active(db, classe.createur_username) is not None:
        raise HTTPException(status_code=403, detail="Cette classe est suspendue")

    deja_membre = db.query(ClasseMembre).filter(
        ClasseMembre.classe_id == classe.id,
        ClasseMembre.username == username,
    ).first()
    if deja_membre:
        assurer_prof_et_sieges(db, classe)
        db.commit()
        return {"success": True, "message": "Déjà membre", "classe_id": classe.id}

    nb_membres_actuels = db.query(ClasseMembre).filter(ClasseMembre.classe_id == classe.id).count()
    if nb_membres_actuels >= 10:
        raise HTTPException(status_code=400, detail="Cette classe a déjà atteint sa limite de 10 élèves")

    assurer_prof_et_sieges(db, classe)
    siege = prochain_siege(db, classe.id)
    if siege is None:
        raise HTTPException(status_code=400, detail="Aucun pupitre disponible dans cette classe")
    db.add(ClasseMembre(classe_id=classe.id, username=username, role="membre", siege_index=siege))
    discussion = assurer_discussion(db, classe)
    db.commit()

    await manager.diffuser_discussion(discussion.id, {"type": "member_joined", "username": username, "nom": username})
    await manager.notifier_nouveau_membre_classe(classe.id, classe.nom, classe.createur_username, username)
    await envoyer_push_a_utilisateur(
        db, classe.createur_username, "Nouveau membre dans ta classe",
        f"{username} a rejoint la classe « {classe.nom} ».",
        {"event_type": "nouveau_membre_classe", "classe_id": classe.id},
    )

    return {"success": True, "classe_id": classe.id, "nom": classe.nom}


@router.get("/api/classes/mes-classes")
async def lister_mes_classes(session_token: str, db: Session = Depends(get_db)):
    username = _identite(session_token, db)
    memberships = db.query(ClasseMembre).filter(ClasseMembre.username == username).all()
    discussions_membres = db.query(ClasseDiscussionMembre).filter_by(username=username, actif=1).all()
    classe_ids = {m.classe_id for m in memberships}
    discussion_ids_utilisateur = {m.discussion_id for m in discussions_membres}

    discussions_user = db.query(ClasseDiscussion).filter(ClasseDiscussion.id.in_(discussion_ids_utilisateur)).all() if discussion_ids_utilisateur else []
    classe_ids.update(d.classe_id for d in discussions_user)
    if not classe_ids:
        return {"classes": []}

    classes = db.query(Classe).filter(Classe.id.in_(classe_ids)).all()
    classes_map = {c.id: c for c in classes}
    membres_classes = db.query(ClasseMembre).filter(ClasseMembre.classe_id.in_(classe_ids)).order_by(ClasseMembre.id.asc()).all()
    membres_par_classe = {}
    for membre in membres_classes:
        membres_par_classe.setdefault(membre.classe_id, []).append(membre)

    discussions = db.query(ClasseDiscussion).filter(ClasseDiscussion.classe_id.in_(classe_ids)).all()
    discussions_map = {d.classe_id: d for d in discussions}
    discussions_actifs = db.query(ClasseDiscussionMembre.discussion_id, func.count(ClasseDiscussionMembre.id)).filter(
        ClasseDiscussionMembre.discussion_id.in_([d.id for d in discussions]), ClasseDiscussionMembre.actif == 1
    ).group_by(ClasseDiscussionMembre.discussion_id).all() if discussions else []
    nb_discussion_par_id = {did: int(total) for did, total in discussions_actifs}
    membres_discussion_existants = db.query(ClasseDiscussionMembre).filter(
        ClasseDiscussionMembre.discussion_id.in_([d.id for d in discussions])
    ).all() if discussions else []
    pseudos_par_discussion = {}
    for dm in membres_discussion_existants:
        pseudos_par_discussion.setdefault(dm.discussion_id, set()).add(dm.username)
    nb_classes_par_id = {cid: len(membres_par_classe.get(cid, [])) for cid in classe_ids}
    membre_discussion_ids = {m.discussion_id for m in discussions_membres}
    membre_classe_ids = {m.classe_id for m in memberships}
    role_classe = {m.classe_id: m.role for m in memberships}
    role_discussion = {m.discussion_id: m.role for m in discussions_membres}

    resultat = []
    sanctions_actives = usernames_sanctionnes(db)
    # Conserve l'ordre historique : classes scolaires d'abord, puis groupes seuls.
    ordre_classes = list(dict.fromkeys([m.classe_id for m in memberships] + [d.classe_id for d in discussions_user]))
    for classe_id in ordre_classes:
        classe = classes_map.get(classe_id)
        if classe is None:
            continue
        membres_locaux = membres_par_classe.get(classe.id, [])
        # Répare les sièges en mémoire sans refaire une requête SQL par classe.
        assurer_prof_et_sieges(db, classe, membres_locaux)
        discussion = discussions_map.get(classe.id)
        if discussion is None:
            discussion = assurer_discussion(db, classe)
            discussions_map[classe.id] = discussion
            pseudos_par_discussion[discussion.id] = {m.username for m in membres_locaux}
            if discussion.owner_username:
                pseudos_par_discussion[discussion.id].add(discussion.owner_username)
            nb_discussion_par_id[discussion.id] = len(pseudos_par_discussion[discussion.id])
        else:
            pseudos_connus = pseudos_par_discussion.get(discussion.id, set())
            manque = discussion.owner_username not in pseudos_connus or any(m.username not in pseudos_connus for m in membres_locaux)
            if manque:
                discussion = assurer_discussion(db, classe)
                nouveaux = db.query(ClasseDiscussionMembre.username).filter_by(discussion_id=discussion.id, actif=1).all()
                pseudos_par_discussion[discussion.id] = {row[0] for row in nouveaux}
                nb_discussion_par_id[discussion.id] = len(pseudos_par_discussion[discussion.id])
        if username in pseudos_par_discussion.get(discussion.id, set()):
            membre_discussion_ids.add(discussion.id)
            if discussion.owner_username == username:
                role_discussion[discussion.id] = "owner"
        resultat.append({
            "classe_id": classe.id, "nom": classe.nom,
            "role": role_classe.get(classe.id, role_discussion.get(discussion.id, "membre")),
            "code_invitation": classe.code_invitation,
            "nb_membres": nb_classes_par_id.get(classe.id, 0),
            "prof_actuel": classe.prof_username or classe.createur_username,
            "owner_username": classe.createur_username,
            "est_owner": username == classe.createur_username,
            "suspendue": classe.createur_username in sanctions_actives,
            "niveau": classe.niveau, "matiere": classe.matiere, "complement": classe.complement,
            "est_membre_classe": classe.id in membre_classe_ids,
            "est_membre_discussion": discussion.id in membre_discussion_ids,
            "discussion_id": discussion.id,
            "discussion_membres": nb_discussion_par_id.get(discussion.id, 0),
            "discussion_invitation": discussion.code_invitation,
        })

    # La route reste compatible avec les anciennes classes nécessitant une
    # réparation paresseuse, mais une lecture sans changement ne force plus
    # une transaction d'écriture inutile.
    if db.new or db.dirty or db.deleted:
        db.commit()
    return {"classes": resultat}

@router.patch("/api/classes/{classe_id}")
async def renommer_classe(classe_id: int, data: ClasseRenommer, db: Session = Depends(get_db)):
    username = _identite(data.session_token, db)

    classe = db.get(Classe, classe_id)
    if classe is None:
        raise HTTPException(status_code=404, detail="Classe introuvable")
    if classe.createur_username != username:
        raise HTTPException(status_code=403, detail="Seul le créateur peut renommer cette classe")

    try:
        if classe.niveau and classe.matiere:
            # Classe structurée : on ne change que le complément ; niveau et matière restent cohérents.
            complement = valider_texte_libre(data.nom, min_len=2, max_len=30, nom="Le complément du nom") if (data.nom or "").strip() else None
            nouveau_nom = composer_nom_classe(classe.matiere, classe.niveau, complement)
        else:
            complement = None
            nouveau_nom = valider_texte_scolaire(
                data.nom, min_len=2, max_len=128, nom="Le nom de la classe", facultatif=False
            )
    except ErreurValidation as erreur:
        raise HTTPException(status_code=400, detail=str(erreur))

    autres = db.query(Classe).filter(Classe.createur_username == username, Classe.id != classe.id).all()
    if any(cle_doublon_classe(c.nom) == cle_doublon_classe(nouveau_nom) for c in autres):
        raise HTTPException(status_code=400, detail="Tu as déjà une classe avec ce nom.")

    classe.nom = nouveau_nom
    if classe.niveau and classe.matiere:
        classe.complement = complement
    discussion = db.query(ClasseDiscussion).filter_by(classe_id=classe.id).first()
    if discussion:
        discussion.nom = nouveau_nom
    db.commit()
    if discussion:
        await manager.diffuser_discussion(discussion.id, {"type": "group_name_update", "nom": nouveau_nom})

    return {"success": True, "nom": classe.nom}


@router.patch("/api/classes/{classe_id}/transferer-prof")
async def transferer_prof(classe_id: int, data: ClasseTransfererProf, db: Session = Depends(get_db)):
    """Exécuté par le prof ACTUEL une fois qu'il a accepté une demande
    (l'échange de la demande elle-même se fait en direct, côté websocket —
    ceci ne fait que persister la décision en base)."""
    username = _identite(data.session_token, db)

    classe = db.get(Classe, classe_id)
    if classe is None:
        raise HTTPException(status_code=404, detail="Classe introuvable")
    assurer_prof_et_sieges(db, classe)
    professeur = classe.prof_username or classe.createur_username
    if professeur != username:
        raise HTTPException(status_code=403, detail="Seul le professeur actuel peut céder son rôle")

    nouveau = db.query(ClasseMembre).filter(
        ClasseMembre.classe_id == classe_id, ClasseMembre.username == data.nouveau_prof
    ).first()
    if nouveau is None:
        raise HTTPException(status_code=404, detail="Cette personne n'est pas membre de la classe")
    if nouveau.username == professeur:
        return {"success": True, "nouveau_prof": professeur, "owner_username": classe.createur_username}

    ancien = db.query(ClasseMembre).filter(
        ClasseMembre.classe_id == classe_id, ClasseMembre.username == professeur
    ).first()
    # Le nouveau professeur libère son ancien pupitre ; l'ancien professeur
    # prend exactement ce pupitre, même si la classe était pleine.
    ancien_siege = nouveau.siege_index if nouveau.siege_index is not None else prochain_siege(db, classe_id)
    if ancien:
        ancien.role = "membre"
        ancien.siege_index = ancien_siege
    nouveau.role = "prof"
    nouveau.siege_index = None
    # Le propriétaire de la classe ne change jamais lors d'un transfert pédagogique.
    classe.prof_username = data.nouveau_prof
    db.commit()
    transfert = {
        "nouveau_prof": data.nouveau_prof,
        "ancien_prof": username,
        "ancien_prof_siege": ancien_siege,
        "nouveau_prof_siege": None,
        "owner_username": classe.createur_username,
    }
    await manager.envoyer_session(-classe_id, data.nouveau_prof, {
        "type": "reponse-devenir-prof", "sender": username, "accepte": True, **transfert,
    })
    await manager.relay(-classe_id, username, {"type": "prof-change", **transfert})

    return {"success": True, **transfert}


@router.delete("/api/classes/{classe_id}")
async def supprimer_classe(classe_id: int, session_token: str, db: Session = Depends(get_db)):
    username = _identite(session_token, db)

    classe = db.get(Classe, classe_id)
    if classe is None:
        raise HTTPException(status_code=404, detail="Classe introuvable")
    if classe.createur_username != username:
        raise HTTPException(status_code=403, detail="Seul le créateur peut supprimer cette classe")

    discussion = db.query(ClasseDiscussion).filter_by(classe_id=classe_id).first()
    if discussion:
        await manager.fermer_discussion(discussion.id)
        db.query(NotificationGrind).filter_by(classe_id=classe_id, resource_type="classe_discussion_message").delete(synchronize_session=False)
        message_ids = [row[0] for row in db.query(ClasseDiscussionMessage.id).filter_by(discussion_id=discussion.id).all()]
        if message_ids:
            db.query(ClasseDiscussionMention).filter(ClasseDiscussionMention.message_id.in_(message_ids)).delete(synchronize_session=False)
        db.query(ClasseDiscussionMessage).filter_by(discussion_id=discussion.id).delete(synchronize_session=False)
        db.query(ClasseDiscussionMembre).filter_by(discussion_id=discussion.id).delete(synchronize_session=False)
        db.delete(discussion)
    db.query(ClasseFeuillePermission).filter_by(classe_id=classe_id).delete(synchronize_session=False)
    db.query(ClasseFeuille).filter_by(classe_id=classe_id).delete(synchronize_session=False)
    db.query(ClasseTableau).filter_by(classe_id=classe_id).delete(synchronize_session=False)
    db.query(ClasseMembre).filter(ClasseMembre.classe_id == classe_id).delete()
    db.delete(classe)
    db.commit()

    return {"success": True}
