"""
Análisis del texto de T-SQL con expresiones regulares.
"""

import re

# Una parte de un nombre: [con corchetes], "con comillas" o identificador normal.
_PARTE = r'(?:\[(?:[^\]]|\]\])+\]|"[^"]+"|[A-Za-z_#@][\w@#$]*)'
# Hasta cuatro partes: servidor.base.esquema.objeto (admite base..objeto).
NOMBRE = rf'(?:(?:{_PARTE})?\.){{0,3}}{_PARTE}'

_TOP = r'(?:TOP\s*(?:\([^)]*\)|\d+)\s*(?:PERCENT\s+)?)?'

_ESCRITURAS = [re.compile(p, re.I) for p in (
    rf'\bINSERT\s+{_TOP}(?:INTO\s+)?({NOMBRE})',
    rf'\bUPDATE\s+{_TOP}({NOMBRE})',
    rf'\bDELETE\s+{_TOP}(?:FROM\s+)?({NOMBRE})',
    rf'\bMERGE\s+{_TOP}(?:INTO\s+)?({NOMBRE})',
    rf'\bINTO\s+({NOMBRE})',  # SELECT ... INTO, OUTPUT ... INTO
    rf'\b(?:TRUNCATE|ALTER|DROP)\s+TABLE\s+(?:IF\s+EXISTS\s+)?({NOMBRE})',
)]
_LECTURAS = re.compile(rf'\b(?:FROM|JOIN|APPLY)\s+({NOMBRE})', re.I)
_EXEC = re.compile(rf'\bEXEC(?:UTE)?\s+(?:@[\w@#$]+\s*=\s*)?({NOMBRE})', re.I)
_DINAMICO = re.compile(
    r'\bEXEC(?:UTE)?\s*\(|\bsp_executesql\b|\bEXEC(?:UTE)?\s+@[\w@#$]+\b(?!\s*=)', re.I)

_ESPECIALES = re.compile(r"--|/\*|'|\[|\"")


def separar_codigo(sql):
    """Devuelve (código sin comentarios y con los literales vaciados, [literales])."""
    trozos, literales = [], []
    i = inicio = 0
    n = len(sql)
    while True:
        m = _ESPECIALES.search(sql, i)
        if not m:
            break
        i = m.start()
        marca = m.group()
        if marca == '--':
            fin = sql.find('\n', i)
            fin = n if fin == -1 else fin
            trozos.append(sql[inicio:i])
            trozos.append(' ')
            i = inicio = fin
        elif marca == '/*':
            profundidad, j = 1, i + 2
            while j < n and profundidad:
                if sql.startswith('/*', j):
                    profundidad += 1
                    j += 2
                elif sql.startswith('*/', j):
                    profundidad -= 1
                    j += 2
                else:
                    j += 1
            trozos.append(sql[inicio:i])
            trozos.append(' ')
            i = inicio = j
        elif marca == "'":
            j, partes = i + 1, []
            while True:
                k = sql.find("'", j)
                if k == -1:
                    partes.append(sql[j:])
                    j = n
                    break
                partes.append(sql[j:k])
                if sql.startswith("''", k):
                    partes.append("'")
                    j = k + 2
                else:
                    j = k + 1
                    break
            literales.append(''.join(partes))
            trozos.append(sql[inicio:i])
            trozos.append("''")
            i = inicio = j
        elif marca == '[': # Identificador entre corchetes: se queda en el código tal cual.
            k = i + 1
            while True:
                k = sql.find(']', k)
                if k == -1:
                    k = n
                    break
                if sql.startswith(']]', k):
                    k += 2
                    continue
                break
            i = k + 1
        else:  # identificador entre comillas dobles
            k = sql.find('"', i + 1)
            i = n if k == -1 else k + 1
    trozos.append(sql[inicio:])
    return ''.join(trozos), literales


def partir_nombre(texto):
    """'[srv].bd..[mi tabla]' -> [servidor, base, esquema, nombre] (None si falta)."""
    partes, actual, i, n = [], [], 0, len(texto)
    while i < n:
        c = texto[i]
        if c == '[':
            j = i + 1
            while j < n:
                if texto[j] == ']':
                    if texto.startswith(']]', j):
                        actual.append(']')
                        j += 2
                        continue
                    break
                actual.append(texto[j])
                j += 1
            i = j + 1
        elif c == '"':
            j = texto.find('"', i + 1)
            j = n if j == -1 else j
            actual.append(texto[i + 1:j])
            i = j + 1
        elif c == '.':
            partes.append(''.join(actual))
            actual = []
            i += 1
        else:
            actual.append(c)
            i += 1
    partes.append(''.join(actual))
    partes = [p.strip() or None for p in partes][-4:]
    return [None] * (4 - len(partes)) + partes


def _clave(partes):
    """(esquema, nombre) en minúsculas, o None si no es un objeto (variable, temporal)."""
    nombre = partes[3]
    if not nombre or nombre[0] in '@#':
        return None
    return ((partes[2] or '').lower() or None, nombre.lower())


_ALIAS = re.compile(rf'(?:\bFROM|\bJOIN|,)\s+({NOMBRE})\s+(?:AS\s+)?([A-Za-z_][\w@#$]*)\b(?!\s*\.)', re.I)


class _Alias:
    """
    UPDATE t SET ... FROM dbo.Tabla t: el objetivo real es dbo.Tabla.
    """

    def __init__(self, codigo):
        self.codigo = codigo
        self.mapa = None

    def resolver(self, alias, desde):
        if self.mapa is None:
            self.mapa = {}
            for m in _ALIAS.finditer(self.codigo):
                self.mapa.setdefault(m.group(2).lower(), []).append((m.start(), m.group(1)))
        usos = self.mapa.get(alias.lower())
        if not usos:
            return None
        # El primero que aparece después de la escritura; si no, el primero de todos.
        texto = next((t for pos, t in usos if pos >= desde), usos[0][1])
        return partir_nombre(texto)


class Analisis:
    __slots__ = ('escribe', 'ejecuta', 'dinamico', 'literales')

    def __init__(self):
        self.escribe = set()   # {(esquema|None, nombre)}
        self.ejecuta = set()
        self.dinamico = False
        self.literales = []

    def escribe_en(self, esquema, nombre):
        return _contiene(self.escribe, esquema, nombre)

    def ejecuta_a(self, esquema, nombre):
        return _contiene(self.ejecuta, esquema, nombre)


def _contiene(conjunto, esquema, nombre):
    nombre = nombre.lower()
    if (None, nombre) in conjunto:
        return True
    if esquema:
        return (esquema.lower(), nombre) in conjunto
    return any(n == nombre for _, n in conjunto)


def analizar_modulo(sql):
    """Qué escribe, qué ejecuta y si usa SQL dinámico un procedimiento, trigger..."""
    a = Analisis()
    if not sql:
        return a
    codigo, a.literales = separar_codigo(sql)
    alias = _Alias(codigo)
    for patron in _ESCRITURAS:
        for m in patron.finditer(codigo):
            partes = partir_nombre(m.group(1))
            clave = _clave(partes)
            if not clave:
                continue
            a.escribe.add(clave)
            if clave[0] is None:
                real = alias.resolver(partes[3], m.end())
                if real and _clave(real):
                    a.escribe.add(_clave(real))
    for m in _EXEC.finditer(codigo):
        clave = _clave(partir_nombre(m.group(1)))
        if clave:
            a.ejecuta.add(clave)
    a.dinamico = bool(_DINAMICO.search(codigo))
    return a


_PRIORIDAD = {'lee': 0, 'escribe': 1, 'ejecuta': 2}


def referencias_en_texto(sql, profundidad=0):
    codigo, literales = separar_codigo(sql)
    res = {}

    def poner(partes, accion):
        if not _clave(partes):
            return
        clave = tuple(partes)
        if _PRIORIDAD[accion] > _PRIORIDAD.get(res.get(clave), -1):
            res[clave] = accion

    alias = _Alias(codigo)
    for m in _LECTURAS.finditer(codigo):
        poner(partir_nombre(m.group(1)), 'lee')
    for patron in _ESCRITURAS:
        for m in patron.finditer(codigo):
            partes = partir_nombre(m.group(1))
            poner(partes, 'escribe')
            if _clave(partes) and not partes[2]:
                real = alias.resolver(partes[3], m.end())
                if real:
                    poner(real, 'escribe')
    for m in _EXEC.finditer(codigo):
        poner(partir_nombre(m.group(1)), 'ejecuta')
    # Un job que hace EXEC('...') o sp_executesql: mirar también dentro, un nivel.
    if profundidad == 0 and _DINAMICO.search(codigo):
        for lit in literales:
            for clave, accion in referencias_en_texto(lit, 1).items():
                poner(list(clave), accion)
    return res
