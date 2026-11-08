import sqlite3
from pathlib import Path

import psycopg
from flask import Blueprint, current_app, jsonify

bp = Blueprint("health", __name__)


def _estado_bd():
    # Compatibilidad exclusiva con las pruebas antiguas.
    if current_app.testing and current_app.config.get("AGROASESOR_DB_PATH"):
        path = Path(current_app.config["AGROASESOR_DB_PATH"])
        if not path.is_file():
            return "no_disponible"
        try:
            con = sqlite3.connect(
                f"{path.resolve().as_uri()}?mode=ro",
                uri=True,
                timeout=3
            )
            con.execute("SELECT 1")
            con.close()
            return "conectada_solo_lectura"
        except sqlite3.Error:
            return "no_disponible"

    dsn = current_app.config.get("AGROASESOR_READONLY_DATABASE_URL")
    if not dsn:
        return "no_configurada"

    try:
        with psycopg.connect(dsn, connect_timeout=3) as con:
            estado = con.execute("SHOW transaction_read_only").fetchone()[0]
            if estado != "on":
                return "no_segura"
            con.execute("SELECT 1")
        return "conectada_solo_lectura"
    except psycopg.Error:
        return "no_disponible"


@bp.get("/health")
def health():
    estado = _estado_bd()
    codigo = 200 if estado == "conectada_solo_lectura" else 503
    return jsonify({
        "status": "ok" if codigo == 200 else "error",
        "servicio": "agroasesor-api",
        "version": "v1",
        "base_datos": estado
    }), codigo


@bp.get("/openapi.json")
def openapi_json():
    import yaml
    ruta = Path(current_app.root_path).parent / "openapi.yaml"
    return jsonify(yaml.safe_load(ruta.read_text(encoding="utf-8")))
