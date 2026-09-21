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
from sqlalchemy import create_engine
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
