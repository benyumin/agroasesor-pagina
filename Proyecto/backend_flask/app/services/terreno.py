"""Lectura de datos del terreno (predios, potreros, cultivos, zonas, insumos, cálculos) desde la base de Django.

Solo consultas SELECT. La propiedad de los datos se decide SIEMPRE aquí (no en la vista): un agricultor solo ve
lo suyo; un recurso ajeno responde igual que uno inexistente (404) para no revelar su existencia.
"""

import json

from app.db import abrir_bd
from app.errors import ApiError
from app.services.unidades import superficie_dual


def _json(texto, defecto):
    if texto in (None, ""):
        return defecto
    if isinstance(texto, (dict, list)):
        return texto
    try:
        return json.loads(texto)
    except (TypeError, ValueError):
        return defecto


def _num(valor):
    return None if valor is None else float(valor)


def _fecha(valor):
    return None if valor is None else str(valor)[:10]


# ---- usuario -------------------------------------------------------------------------------------------
def perfil_usuario(usuario_id):
    con = abrir_bd()
    fila = con.execute(
        "SELECT u.id, u.username, u.email, u.is_staff, u.is_active, a.nombre, a.telefono, a.comuna, a.rut "
        "FROM auth_user u LEFT JOIN core_agricultor a ON a.user_id = u.id WHERE u.id = %s",
        (usuario_id,),
    ).fetchone()
    if fila is None or not fila["is_active"]:
        raise ApiError(404, "NO_ENCONTRADO", "Usuario no encontrado.")
    return {
        "id": fila["id"],
        "usuario": fila["username"],
        "correo": fila["email"],
        "es_admin": bool(fila["is_staff"]),
        "perfil_completo": fila["nombre"] is not None,
        "nombre": fila["nombre"] or "",
        "telefono": fila["telefono"] or "",
        "comuna": fila["comuna"] or "",
        "rut": fila["rut"] or "",
    }


# ---- catálogos -----------------------------------------------------------------------------------------
def listar_zonas():
    filas = abrir_bd().execute("SELECT id, nombre, descripcion FROM core_zona WHERE activo IS TRUE ORDER BY nombre")
    return [dict(f) for f in filas]


def _cultivo(f):
    return {
        "id": f["id"],
        "nombre": f["nombre"],
        "descripcion": f["descripcion"],
        "rendimiento_ton_ha": _num(f["rendimiento_ton_ha"]),
        "densidad_siembra": _num(f["densidad_siembra"]),
        "unidad_siembra": f["unidad_siembra"],
        # El dato solo se considera verificado si tiene fuente y fecha de verificación registradas.
        "densidad": {
            "fuente": f["densidad_fuente"],
            "url": f["densidad_url"],
            "verificada": _fecha(f["densidad_verificada"]),
            "estado": "verificado" if f["densidad_verificada"] and f["densidad_fuente"] else "sin_verificar",
        },
        "rendimiento": {
            "fuente": f["rendimiento_fuente"],
            "url": f["rendimiento_url"],
            "verificado": _fecha(f["rendimiento_verificado"]),
            "estado": "verificado" if f["rendimiento_verificado"] and f["rendimiento_fuente"] else "sin_verificar",
        },
    }


def listar_cultivos():
    filas = abrir_bd().execute("SELECT * FROM core_cultivo WHERE activo IS TRUE ORDER BY nombre")
    return [_cultivo(f) for f in filas]


def obtener_cultivo(cultivo_id):
    fila = abrir_bd().execute("SELECT * FROM core_cultivo WHERE id = %s AND activo IS TRUE", (cultivo_id,)).fetchone()
    if fila is None:
        raise ApiError(404, "NO_ENCONTRADO", "Cultivo no encontrado.")
    return _cultivo(fila)


_CAMPOS_INSUMO = (
    "i.id, i.nombre, i.descripcion, i.dosis_hectarea, i.unidad_dosis, i.vigente, i.estado_verificacion, "
    "i.fuente_nombre, i.fuente_url, i.fecha_verificacion, i.fabricante, i.presentacion_cantidad, "
    "i.presentacion_unidad, i.numero_autorizacion_sag, i.etiqueta_url, i.cultivo_id, t.nombre AS tipo"
)


def _insumo(f):
    return {
        "id": f["id"],
        "nombre": f["nombre"],
        "tipo": f["tipo"],
        "descripcion": f["descripcion"],
        "dosis_hectarea": _num(f["dosis_hectarea"]),
        "unidad_dosis": f["unidad_dosis"],
        "vigente": bool(f["vigente"]),
        "cultivo_id": f["cultivo_id"],
        "fabricante": f["fabricante"],
        "presentacion": {"cantidad": _num(f["presentacion_cantidad"]), "unidad": f["presentacion_unidad"]},
        "numero_autorizacion_sag": f["numero_autorizacion_sag"],
        "verificacion": {
            "estado": f["estado_verificacion"],
            "fuente": f["fuente_nombre"],
            "url": f["fuente_url"] or f["etiqueta_url"],
            "fecha": _fecha(f["fecha_verificacion"]),
        },
    }


def listar_insumos(tipo=None, cultivo_id=None):
    sql = f"SELECT {_CAMPOS_INSUMO} FROM core_insumo i JOIN core_tipoinsumo t ON t.id = i.tipo_id WHERE i.vigente IS TRUE"
    args = []
    if tipo:
        sql += " AND t.nombre = %s"
        args.append(tipo)
    if cultivo_id:
        sql += " AND i.cultivo_id = %s"
        args.append(cultivo_id)
    return [_insumo(f) for f in abrir_bd().execute(sql + " ORDER BY i.nombre", args)]


def obtener_insumo(insumo_id):
    fila = (
        abrir_bd()
        .execute(
            f"SELECT {_CAMPOS_INSUMO} FROM core_insumo i JOIN core_tipoinsumo t ON t.id = i.tipo_id "
            "WHERE i.id = %s AND i.vigente IS TRUE",
            (insumo_id,),
        )
        .fetchone()
    )
    if fila is None:
        raise ApiError(404, "NO_ENCONTRADO", "Insumo no encontrado.")
    return _insumo(fila)


# ---- predios y potreros (siempre acotados al dueño) -----------------------------------------------------
def _predio(f, n_potreros):
    return {
        "id": f["id"],
        "nombre": f["nombre"],
        "ubicacion": f["ubicacion"],
        "zona_id": f["zona_id"],
        "superficie": superficie_dual(f["superficie_ha"]),
        "latitud": f["latitud"],
        "longitud": f["longitud"],
        "geojson": _json(f["geojson"], None),
        "potreros": n_potreros,
    }


def listar_predios(usuario):
    filas = (
        abrir_bd()
        .execute(
            "SELECT p.*, (SELECT COUNT(*) FROM core_potrero q WHERE q.predio_id = p.id) AS n "
            "FROM core_predio p WHERE p.propietario_id = %s ORDER BY p.nombre",
            (usuario.id,),
        )
        .fetchall()
    )
    return [_predio(f, f["n"]) for f in filas]


def _predio_propio(predio_id, usuario):
    fila = (
        abrir_bd()
        .execute("SELECT * FROM core_predio WHERE id = %s AND propietario_id = %s", (predio_id, usuario.id))
        .fetchone()
    )
    if fila is None:
        raise ApiError(404, "NO_ENCONTRADO", "Predio no encontrado.")
    return fila


def obtener_predio(predio_id, usuario):
    fila = _predio_propio(predio_id, usuario)
    n = abrir_bd().execute("SELECT COUNT(*) AS total FROM core_potrero WHERE predio_id = %s", (predio_id,)).fetchone()["total"]
    return _predio(fila, n)


def _potrero(f):
    perimetro = _json(f["perimetro"], [])
    return {
        "id": f["id"],
        "predio_id": f["predio_id"],
        "nombre": f["nombre"],
        "cultivo_id": f["cultivo_id"],
        "cultivo": f["cultivo_nombre"],
        "variedad": f["variedad"],
        "fecha_siembra": _fecha(f["fecha_siembra"]),
        "fecha_cosecha": _fecha(f["fecha_cosecha"]),
        "superficie": superficie_dual(f["superficie_ha"]),
        "latitud": f["latitud"],
        "longitud": f["longitud"],
        "perimetro": perimetro,
        "tiene_perimetro": bool(perimetro),
        "geojson": _json(f["geojson"], None),
    }


_SQL_POTRERO = (
    "SELECT q.*, c.nombre AS cultivo_nombre FROM core_potrero q "
    "JOIN core_predio p ON p.id = q.predio_id LEFT JOIN core_cultivo c ON c.id = q.cultivo_id "
)


def listar_potreros(predio_id, usuario):
    _predio_propio(predio_id, usuario)
    filas = abrir_bd().execute(_SQL_POTRERO + "WHERE q.predio_id = %s ORDER BY q.nombre", (predio_id,))
    return [_potrero(f) for f in filas]


def obtener_potrero(potrero_id, usuario):
    fila = (
        abrir_bd()
        .execute(_SQL_POTRERO + "WHERE q.id = %s AND p.propietario_id = %s", (potrero_id, usuario.id))
        .fetchone()
    )
    if fila is None:
        raise ApiError(404, "NO_ENCONTRADO", "Potrero no encontrado.")
    return _potrero(fila)


def historial_calculos(usuario, potrero_id=None, limite=50):
    sql = (
        "SELECT c.id, c.operacion, c.entrada, c.resultado, c.creado, c.potrero_id, q.nombre AS potrero "
        "FROM core_calculoagricola c JOIN core_potrero q ON q.id = c.potrero_id "
        "WHERE c.usuario_id = %s"
    )
    args = [usuario.id]
    if potrero_id:
        sql += " AND c.potrero_id = %s"
        args.append(potrero_id)
    sql += " ORDER BY c.creado DESC, c.id DESC LIMIT %s"
    args.append(limite)
    return [
        {
            "id": f["id"],
            "operacion": f["operacion"],
            "potrero_id": f["potrero_id"],
            "potrero": f["potrero"],
            "creado": str(f["creado"]),
            "entrada": _json(f["entrada"], {}),
            "resultado": _json(f["resultado"], {}),
        }
        for f in abrir_bd().execute(sql, args)
    ]
