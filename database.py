"""
database.py — connexion à la base GRIND.

En local (Termux) : SQLite par défaut, zéro config.
En prod (Render) : mets DATABASE_URL dans les variables d'environnement,
ex. mysql+pymysql://user:password@host:port/grind_db (base Aiven séparée
de celle de Pluton, même fournisseur, schéma indépendant).
"""

import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./grind.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
