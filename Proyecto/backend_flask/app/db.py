import sqlite3
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from flask import current_app, g

from app.errors import ApiError


class SQLiteTestConnection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, params=()):
        return self.connection.execute(sql.replace("%s", "?"), params)

    def close(self):
        self.connection.close()


def abrir_bd():
    if "bd" in g:
        return g.bd

    # SQLite solo se permite dentro de las pruebas automatizadas.
    if current_app.testing and current_app.config.get("AGROASESOR_DB_PATH"):
        path = Path(current_app.config["AGROASESOR_DB_PATH"])
        try:
            con = sqlite3.connect(
                f"{path.resolve().as_uri()}?mode=ro",
                uri=True,
                timeout=5
            )
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA query_only = ON")
            g.bd = SQLiteTestConnection(con)
            return g.bd
        except sqlite3.Error:
            raise ApiError(503, "BASE_NO_DISPONIBLE", "No se pudo abrir la base de pruebas.")

    dsn = current_app.config.get("AGROASESOR_READONLY_DATABASE_URL")
    if not dsn:
        raise ApiError(503, "BASE_NO_CONECTADA", "La conexión PostgreSQL no está configurada.")

    try:
        con = psycopg.connect(
            dsn,
            row_factory=dict_row,
            connect_timeout=8
        )

        with con.cursor() as cursor:
            cursor.execute("SHOW transaction_read_only")
            if cursor.fetchone()["transaction_read_only"] != "on":
                con.close()
                raise ApiError(503, "BASE_NO_SEGURA", "La conexión no está en modo de solo lectura.")

        g.bd = con
        return con

    except psycopg.Error:
        raise ApiError(503, "BASE_NO_DISPONIBLE", "No se pudo conectar con PostgreSQL.")


def cerrar_bd(_error=None):
    con = g.pop("bd", None)
    if con is not None:
        con.close()
