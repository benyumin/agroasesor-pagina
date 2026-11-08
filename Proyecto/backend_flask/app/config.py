import os


class Config:
    TESTING = False
    SECRET_KEY = os.getenv("SECRET_KEY")

    AGROASESOR_READONLY_DATABASE_URL = os.getenv(
        "AGROASESOR_READONLY_DATABASE_URL", ""
    )

    # Exclusivo para pruebas automatizadas. Nunca se utiliza en produccion.
    AGROASESOR_DB_PATH = os.getenv("AGROASESOR_DB_PATH", "")

    AGROASESOR_INTERNAL_SECRET = os.getenv("AGROASESOR_INTERNAL_SECRET", "")
    AGROASESOR_FIRMA_VIGENCIA_SEGUNDOS = int(
        os.getenv("AGROASESOR_FIRMA_VIGENCIA_SEGUNDOS", "60")
    )
    MAX_CONTENT_LENGTH = 64 * 1024
