"""
Análisis de los .js y .css de los formularios web (Work Tasks) con expresiones regulares.
"""

import re

_ESPECIALES = re.compile(r'//|/\*|[\'"`/]')
_FIN_CADENA = {'"': re.compile(r'[\\"\n]'), "'": re.compile(r"[\\'\n]"), '`': re.compile(r'[\\`$]')}
_ESCAPES = {'n': '\n', 'r': '\r', 't': '\t', '\n': ''}
# Una "/" abre una expresión regular (y no es una división) si va detrás de uno de estos.
_ANTES_DE_REGEX = set('(,=:[!&|?{;+-*%<>~^')
_PALABRAS_ANTES_DE_REGEX = {'return', 'typeof', 'case', 'do', 'else', 'in', 'of', 'new', 'delete',
                            'void', 'throw', 'yield', 'await'}


def _fin_cadena(js, i, comilla):
    """Desde la comilla de apertura: (posición tras el cierre, texto de dentro)."""
    patron, n, j, partes = _FIN_CADENA[comilla], len(js), i + 1, []
    while True:
        m = patron.search(js, j)
        if not m:
            partes.append(js[j:])
            return n, ''.join(partes)
        k = m.start()
        partes.append(js[j:k])
        c = m.group()
        if c == '\\':
            siguiente = js[k + 1:k + 2]
            partes.append(_ESCAPES.get(siguiente, siguiente))
            j = k + 2
        elif c == '\n':          # cadena sin cerrar: termina en la línea
            return k, ''.join(partes)
        elif c == '$':           # ${...} de una plantilla: se queda como texto
            if not js.startswith('${', k):
                partes.append('$')
                j = k + 1
                continue
            fin = _fin_expresion(js, k + 2)
            partes.append(js[k:fin])
            j = fin
        else:
            return k + 1, ''.join(partes)


_LLAVES = re.compile(r'[{}\'"`]')


def _fin_expresion(js, j):
    """El cierre de un ${...}, saltando las llaves y cadenas de dentro."""
    profundidad, n = 1, len(js)
    while j < n:
        m = _LLAVES.search(js, j)
        if not m:
            return n
        c = m.group()
        if c in '\'"`':
            j = _fin_cadena(js, m.start(), c)[0]
            continue
        profundidad += 1 if c == '{' else -1
        j = m.end()
        if not profundidad:
            return j
    return n


def _abre_regex(js, i):
    k = i - 1
    while k >= 0 and js[k] in ' \t\r\n':
        k -= 1
    if k < 0 or js[k] in _ANTES_DE_REGEX:
        return True
    if js[k].isalnum() or js[k] in '_$':
        fin = k + 1
        while k >= 0 and (js[k].isalnum() or js[k] in '_$'):
            k -= 1
        return js[k + 1:fin] in _PALABRAS_ANTES_DE_REGEX
    return False


def _fin_regex(js, i):
    """El final de /.../flags, o None si en esa línea no se cierra (era una división)."""
    j, n, en_clase = i + 1, len(js), False
    while j < n:
        c = js[j]
        if c == '\\':
            j += 2
            continue
        if c == '\n':
            return None
        if en_clase:
            en_clase = c != ']'
        elif c == '[':
            en_clase = True
        elif c == '/':
            j += 1
            while j < n and js[j].isalpha():
                j += 1
            return j
        j += 1
    return None


def separar_js(js):
    """Devuelve (código sin comentarios y con cada cadena cambiada por "n", [cadenas])."""
    trozos, literales = [], []
    i = inicio = 0
    n = len(js)
    while True:
        m = _ESPECIALES.search(js, i)
        if not m:
            break
        i = m.start()
        marca = m.group()
        if marca in ('//', '/*'):
            if marca == '//':
                fin = js.find('\n', i)
                fin = n if fin == -1 else fin
            else:
                fin = js.find('*/', i + 2)
                fin = n if fin == -1 else fin + 2
            trozos.append(js[inicio:i])
            trozos.append(' ')
            i = inicio = fin
        elif marca == '/':
            fin = _fin_regex(js, i) if _abre_regex(js, i) else None
            if fin is None:
                i += 1
                continue
            trozos.append(js[inicio:i])
            trozos.append('/r/')
            i = inicio = fin
        else:
            fin, texto = _fin_cadena(js, i, marca)
            trozos.append(js[inicio:i])
            trozos.append(f'"{len(literales)}"')
            literales.append(texto)
            i = inicio = fin
    trozos.append(js[inicio:])
    return ''.join(trozos), literales


_ID = r'[A-Za-z_$][\w$]*'
_DEFINICIONES = [re.compile(p) for p in (
    rf'\bfunction\s*\*?\s*({_ID})\s*\(',
    rf'\bclass\s+({_ID})',
    rf'({_ID})\s*[:=]\s*(?:async\s+)?function\b',
    rf'({_ID})\s*[:=]\s*(?:async\s+)?(?:\([^()]*\)|{_ID})\s*=>',
)]
_LLAMADA = re.compile(rf'(?<![\w$])({_ID})\s*\(')
# mdm.utilities.MESDirectAccess('GET', "sp_...", parametros): el segundo argumento es la SP.
# Si es una variable, se busca qué cadenas se le asignan; si no ("sp_" + x...), no se sabe cuál.
_DIRECTO = re.compile(rf'{_ID}DirectAccess\s*\(\s*[^,()]*,\s*(?:(?:"(\d+)"|(?:{_ID}\.)*({_ID}))\s*(?=[,)]))?', re.I)
# SFU.getLookupSchemaAndData("MDMJobsLookup", ...): el primer argumento es el lookup.
_LOOKUP = re.compile(rf'{_ID}?lookup[\w$]*\s*\(\s*"(\d+)"', re.I)
# ... o una variable: var lookupName = "ofi_lm_mes_attribute"; SFU.getLookupSchemaAndData(lookupName, ...).
# Solo getLookup*: hay otras funciones con "lookup" que no reciben un lookup (las librerías,
# SFU.getUserLookupExtendedInformation(usuario)...).
_LOOKUP_VARIABLE = re.compile(rf'(?<![\w$])getLookup[\w$]*\s*\(\s*(?:{_ID}\.)*({_ID})\s*[,)]', re.I)
# Lo que se asigna a una variable: x = "cadena" o x = condicion ? "a" : "b" (no x = "a" + y).
_ASIGNACION = re.compile(rf'(?<![\w$])({_ID})\s*=(?!=)\s*"(\d+)"\s*(?=[;,)}}\n]|$)')
_ASIGNACION_TERNARIA = re.compile(rf'(?<![\w$])({_ID})\s*=(?!=)[^;\n?=]*\?\s*"(\d+)"\s*:\s*"(\d+)"\s*(?=[;,)}}\n]|$)')
_NOMBRE_LOOKUP = re.compile(r'^\w+lookup$', re.I)
_FICHERO = re.compile(r'([\w.-]+\.(?:js|css))(?:[?#].*)?$', re.I)
_SQL = re.compile(r'^\s*(?:SELECT|INSERT|UPDATE|DELETE|MERGE|EXEC|EXECUTE|WITH|select|insert|update|delete|exec)\s')

# Nombres de función tan corrientes (o del propio JavaScript) que no dicen de qué fichero vienen.
COMUNES = set('''
init get set push pop call apply bind then each map filter find forEach reduce some every toString
valueOf trim split join replace indexOf slice splice concat keys values test exec match log error warn
on off one trigger ready show hide close open load save render update refresh destroy create remove add
clear reset start stop extend resolve reject done fail always success value text html val attr css data
change click submit focus blur format parse stringify equals has includes sort reverse toFixed charAt
substring substr toLowerCase toUpperCase getTime now assign define require setTimeout setInterval
clearTimeout clearInterval alert confirm Number String Boolean Array Object Date Math JSON parseInt
parseFloat isNaN Promise Error RegExp fn handler callback next run execute invoke process main
'''.split())


class AnalisisWeb:
    __slots__ = ('literales', 'definiciones', 'llamadas', 'directos', 'lookups', 'ficheros', 'sql', 'dinamico')

    def __init__(self):
        self.literales = []
        self.definiciones = set()
        self.llamadas = set()
        self.directos = set()    # SPs nombradas en una llamada *DirectAccess
        self.lookups = set()
        self.ficheros = set()    # otros .js / .css que nombra
        self.sql = []            # cadenas que son T-SQL
        self.dinamico = False    # una SP o un lookup en una variable que no se sabe qué vale


def analizar_js(js):
    a = AnalisisWeb()
    if not js:
        return a
    codigo, a.literales = separar_js(js)
    for patron in _DEFINICIONES:
        a.definiciones.update(m.group(1) for m in patron.finditer(codigo))
    a.llamadas = {m.group(1) for m in _LLAMADA.finditer(codigo)}
    # Las cadenas que recibe cada variable en el fichero (sin mirar ámbitos: aproximado).
    asignadas = {}
    for m in _ASIGNACION.finditer(codigo):
        asignadas.setdefault(m.group(1), set()).add(int(m.group(2)))
    for m in _ASIGNACION_TERNARIA.finditer(codigo):
        asignadas.setdefault(m.group(1), set()).update((int(m.group(2)), int(m.group(3))))

    def textos(variable):
        return {a.literales[i].strip() for i in asignadas.get(variable, ())}

    for m in _DIRECTO.finditer(codigo):
        if m.group(1) is not None:
            a.directos.add(a.literales[int(m.group(1))].strip())
        elif m.group(2) and textos(m.group(2)):
            a.directos.update(textos(m.group(2)))
        else:
            a.dinamico = True
    for m in _LOOKUP.finditer(codigo):
        a.lookups.add(a.literales[int(m.group(1))].strip())
    for m in _LOOKUP_VARIABLE.finditer(codigo):
        if textos(m.group(1)):
            a.lookups.update(textos(m.group(1)))
        else:
            a.dinamico = True
    for texto in a.literales:
        _clasificar(a, texto)
    a.lookups.discard('')
    return a


def _clasificar(a, texto):
    """Una cadena suelta: ¿T-SQL, o el nombre de un fichero?"""
    if _SQL.match(texto):
        a.sql.append(texto)
    else:
        m = _FICHERO.search(texto)
        if m:
            a.ficheros.add(m.group(1).lower())


def es_nombre_lookup(texto):
    """Una cadena que es el nombre de un lookup: acaba en "Lookup" ("MDMJobsLookup")."""
    return bool(_NOMBRE_LOOKUP.match(texto))


_COMENTARIO_CSS = re.compile(r'/\*.*?\*/', re.S)
_IMPORT_CSS = re.compile(r'''@import\s+(?:url\(\s*)?["']?([^"')\s;]+)''', re.I)


def analizar_css(css):
    a = AnalisisWeb()
    for m in _IMPORT_CSS.finditer(_COMENTARIO_CSS.sub(' ', css or '')):
        f = _FICHERO.search(m.group(1))
        if f:
            a.ficheros.add(f.group(1).lower())
    return a

