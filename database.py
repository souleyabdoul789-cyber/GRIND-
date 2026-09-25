"""
database.py — connexion à la base GRIND.

En local : SQLite par défaut, zéro config.
En prod (Render) : DATABASE_URL pointe vers Aiven MySQL.

IMPORTANT : ne mets PAS "?ssl-mode=REQUIRED" dans l'URL elle-même —
pymysql ne comprend pas ce mot-clé avec un tiret (erreur "unexpected
keyword argument 'ssl-mode'"). Le SSL est activé ici, dans le code,
via connect_args à la place.
"""

import os
from urllib.parse import urlparse, urlunparse
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base

RAW_DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./grind.db")

# On retire un éventuel "?ssl-mode=..." de l'URL s'il traîne encore
# dans la variable d'environnement — on gère le SSL nous-mêmes ci-dessous.
_parsed = urlparse(RAW_DATABASE_URL)
DATABASE_URL = urlunparse(_parsed._replace(query=""))

if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}
elif DATABASE_URL.startswith("mysql"):
    # Active le TLS côté pymysql sans exiger de certificat CA local —
    # suffisant pour le chiffrement en transit exigé par Aiven.
    connect_args = {"ssl": {}}
else:
    connect_args = {}

engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def migrer_colonnes_manquantes(base_declarative):
    """create_all() ne crée que les tables absentes, jamais les colonnes
    manquantes sur une table déjà existante. Cette fonction compare le
    modèle Python à la vraie base et ajoute (ALTER TABLE) ce qui manque —
    une "migration" légère, pas un vrai Alembic, mais suffisante pour ce
    projet tant qu'on n'a pas des changements de colonnes plus complexes
    (renommage, changement de type...)."""
    inspecteur = inspect(engine)
    with engine.begin() as connexion:
        for nom_table, table in base_declarative.metadata.tables.items():
            if nom_table not in inspecteur.get_table_names():
                continue  # create_all() s'en charge déjà
            colonnes_existantes = {c["name"] for c in inspecteur.get_columns(nom_table)}
            for colonne in table.columns:
                if colonne.name in colonnes_existantes:
                    continue
                type_sql = colonne.type.compile(dialect=engine.dialect)
                nullable = "NULL" if colonne.nullable else "NOT NULL"
                try:
                    connexion.execute(text(
                        f"ALTER TABLE {nom_table} ADD COLUMN {colonne.name} {type_sql} {nullable}"
                    ))
                    print(f"Migration : colonne '{colonne.name}' ajoutée à '{nom_table}'")
                except Exception as e:
                    print(f"Migration échouée pour {nom_table}.{colonne.name} : {e}")
