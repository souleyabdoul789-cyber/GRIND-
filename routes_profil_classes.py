import json
import re
import secrets
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from auth_grind import verifier_session
from schemas import ProfilCreate, ClasseCreate, ClasseRejoindre, ClasseRenommer, ClasseTransfererProf
from moderation import badge_de, sanction_active
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
    db.add(classe)
    db.commit()
    db.refresh(classe)

    db.add(ClasseMembre(classe_id=classe.id, username=username, role="prof", siege_index=None))
    assurer_discussion(db, classe)
    db.commit()

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

    resultat = []
    deja = set()
    memberships = db.query(ClasseMembre).filter(ClasseMembre.username == username).all()
    for m in memberships:
        classe = db.get(Classe, m.classe_id)
        if classe is None:
            continue
        assurer_prof_et_sieges(db, classe)
        discussion = assurer_discussion(db, classe)
        nb_membres = db.query(ClasseMembre).filter(ClasseMembre.classe_id == classe.id).count()
        nb_discussion = db.query(ClasseDiscussionMembre).filter_by(discussion_id=discussion.id, actif=1).count()
        adhesion_discussion = db.query(ClasseDiscussionMembre).filter_by(
            discussion_id=discussion.id, username=username, actif=1
        ).first()
        resultat.append({
            "classe_id": classe.id,
            "nom": classe.nom,
            "role": m.role,
            "code_invitation": classe.code_invitation,
            "nb_membres": nb_membres,
            "prof_actuel": classe.prof_username or classe.createur_username,
            "owner_username": classe.createur_username,
            "est_owner": username == classe.createur_username,
            "suspendue": sanction_active(db, classe.createur_username) is not None,
            "niveau": classe.niveau,
            "matiere": classe.matiere,
            "complement": classe.complement,
            "est_membre_classe": True,
            "est_membre_discussion": adhesion_discussion is not None,
            "discussion_id": discussion.id,
            "discussion_membres": nb_discussion,
            "discussion_invitation": discussion.code_invitation,
        })
        deja.add(classe.id)

    # Un membre du groupe peut participer à la discussion sans occuper l'un des
    # 10 sièges de la classe scolaire. La carte reste visible avec une action
    # distincte « rejoindre la vraie classe ».
    discussions = db.query(ClasseDiscussionMembre).filter_by(username=username, actif=1).all()
    for dm in discussions:
        discussion = db.get(ClasseDiscussion, dm.discussion_id)
        if discussion is None or discussion.classe_id in deja:
            continue
        classe = db.get(Classe, discussion.classe_id)
        if classe is None:
            continue
        assurer_prof_et_sieges(db, classe)
        nb_membres = db.query(ClasseMembre).filter(ClasseMembre.classe_id == classe.id).count()
        nb_discussion = db.query(ClasseDiscussionMembre).filter_by(discussion_id=discussion.id, actif=1).count()
        resultat.append({
            "classe_id": classe.id,
            "nom": classe.nom,
            "role": dm.role,
            "code_invitation": classe.code_invitation,
            "nb_membres": nb_membres,
            "prof_actuel": classe.prof_username or classe.createur_username,
            "owner_username": classe.createur_username,
            "est_owner": username == classe.createur_username,
            "suspendue": sanction_active(db, classe.createur_username) is not None,
            "niveau": classe.niveau,
            "matiere": classe.matiere,
            "complement": classe.complement,
            "est_membre_classe": False,
            "est_membre_discussion": True,
            "discussion_id": discussion.id,
            "discussion_membres": nb_discussion,
            "discussion_invitation": discussion.code_invitation,
        })
        deja.add(classe.id)

    db.commit()  # persiste la création lazy des groupes pour les classes historiques
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
            nouveau_nom = valider_texte_libre(data.nom, min_len=3, max_len=60, nom="Le nom de la classe")
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
