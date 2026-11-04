"""
Análisis de los .js y .css de los formularios web (Work Tasks) con expresiones regulares.
"""

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

_ESPECIALES =re.compile(r'//|/\*|[\'"`/]')
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


# Velocidad: hay librerías de 4 MB (kendo.all.min.js, varias copias) y un patrón que empieza por
# "un nombre" se prueba en cada nombre del fichero. Por eso las definiciones se buscan desde lo raro
# (function, class, =>) y el nombre se lee hacia atrás, y los nombres son posesivos (*+: sin
# retroceder letra a letra cuando lo de después no encaja). Mismo resultado, unas 4 veces más rápido.
_ID = r'[A-Za-z_$][\w$]*+'
_ES_ID = re.compile(r'[A-Za-z_$][\w$]*\Z')
# function nombre( · class nombre: el \b de delante se comprueba aparte (_empieza_palabra).
_DEF_FUNCION = re.compile(rf'function\s*\*?\s*({_ID})\s*\(')
_DEF_CLASE = re.compile(rf'class\s+({_ID})')
# nombre = function · nombre: function (el nombre, hacia atrás desde el = o los :)
_DEF_ASIGNADA = re.compile(r'[:=]\s*(?:async\s+)?function\b')
# nombre = (a, b) => · nombre = a => (todo hacia atrás desde la flecha)
_FLECHA = re.compile(r'=>')
_LLAMADA = re.compile(rf'(?<![\w$])({_ID})\s*\(')
# mdm.utilities.MESDirectAccess('GET', "sp_...", parametros): el segundo argumento es la SP.
# Si es una variable, se busca qué cadenas se le asignan; si no ("sp_" + x...), no se sabe cuál.
# Se busca desde la palabra DirectAccess (detrás de un nombre como MES), no desde cada letra del fichero.
_DIRECTO = re.compile(rf'(?<=[\w$])DirectAccess\s*\(\s*[^,()]*,\s*(?:(?:"(\d+)"|(?:{_ID}\.)*({_ID}))\s*(?=[,)]))?', re.I)
# SFU.getLookupSchemaAndData("MDMJobsLookup", ...): el primer argumento es el lookup.
_LOOKUP = re.compile(rf'lookup[\w$]*\s*\(\s*"(\d+)"', re.I)
# ... o una variable: var lookupName = "ofi_lm_mes_attribute"; SFU.getLookupSchemaAndData(lookupName, ...).
# Solo getLookup*: hay otras funciones con "lookup" que no reciben un lookup (las librerías,
# SFU.getUserLookupExtendedInformation(usuario)...).
_LOOKUP_VARIABLE = re.compile(rf'(?<![\w$])getLookup[\w$]*\s*\(\s*(?:{_ID}\.)*({_ID})\s*[,)]', re.I)
# Lo que se asigna a una variable: x = "cadena" o x = condicion ? "a" : "b" (no x = "a" + y).
_ASIGNACION = re.compile(rf'(?<![\w$])({_ID})\s*=(?!=)\s*"(\d+)"\s*(?=[;,)}}\n]|$)')
_ASIGNACION_TERNARIA = re.compile(rf'(?<![\w$])({_ID})\s*=(?!=)[^;\n?=]*+\?\s*"(\d+)"\s*:\s*"(\d+)"\s*(?=[;,)}}\n]|$)')
_NOMBRE_LOOKUP = re.compile(r'^\w+lookup$', re.I)
# Acaban en "lookup" pero no son lookups de Work Tasks: las fórmulas HLOOKUP, VLOOKUP y XLOOKUP de la
# hoja de cálculo de Kendo (kendo.all.min.js las define: B("hlookup", function(...){...})).
_NO_LOOKUPS = {'hlookup', 'vlookup', 'xlookup'}
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


def _es_letra(c):
    return c.isalnum() or c in '_$'


def _empieza_palabra(codigo, i):
    """Lo que hace \\b delante de function / class: lo anterior no es una letra, cifra ni _."""
    return i == 0 or not (codigo[i - 1].isalnum() or codigo[i - 1] == '_')


def _saltar_espacios(codigo, j):
    while j and codigo[j - 1].isspace():
        j -= 1
    return j


def _nombre_antes(codigo, j):
    """(inicio, nombre) del nombre entero que acaba en j, o (j, None) si ahí no acaba un nombre."""
    i = j
    while i and _es_letra(codigo[i - 1]):
        i -= 1
    return (i, codigo[i:j]) if i < j and _ES_ID.match(codigo[i:j]) else (j, None)


def _definida_con_flecha(codigo, flecha):
    """nombre = (params) => · nombre: async a => …: el nombre, leyendo hacia atrás desde la flecha."""
    j = _saltar_espacios(codigo, flecha)
    if j and codigo[j - 1] == ')':
        abre = codigo.rfind('(', 0, j - 1)
        if abre < 0 or ')' in codigo[abre + 1:j - 1]:
            return None
        inicio = abre
    else:
        inicio, parametro = _nombre_antes(codigo, j)
        if parametro is None:
            return None
    j = _saltar_espacios(codigo, inicio)
    if j and codigo[j - 1] not in ':=' and j < inicio:      # ¿"async " delante de los parámetros?
        i, palabra = _nombre_antes(codigo, j)
        if palabra != 'async':
            return None
        j = _saltar_espacios(codigo, i)
    if not j or codigo[j - 1] not in ':=':
        return None
    return _nombre_antes(codigo, _saltar_espacios(codigo, j - 1))[1]


def _definiciones(codigo):
    nombres = set()
    for patron in (_DEF_FUNCION, _DEF_CLASE):
        nombres.update(m.group(1) for m in patron.finditer(codigo) if _empieza_palabra(codigo, m.start()))
    for m in _DEF_ASIGNADA.finditer(codigo):
        nombre = _nombre_antes(codigo, _saltar_espacios(codigo, m.start()))[1]
        if nombre:
            nombres.add(nombre)
    for m in _FLECHA.finditer(codigo):
        nombre = _definida_con_flecha(codigo, m.start())
        if nombre:
            nombres.add(nombre)
    return nombres


def analizar_js(js):
    a = AnalisisWeb()
    if not js:
        return a
    codigo, a.literales = separar_js(js)
    a.definiciones = _definiciones(codigo)
    a.llamadas = {m.group(1) for m in _LLAMADA.finditer(codigo)}
    # Lo demás solo hace falta si el fichero nombra DirectAccess o lookups: en las librerías no.
    bajo = codigo.lower()
    asignadas = None

    def textos(variable):
        # Las cadenas que recibe cada variable en el fichero (sin mirar ámbitos: aproximado).
        # Se calcula la primera vez que hace falta: en la mayoría de ficheros, nunca.
        nonlocal asignadas
        if asignadas is None:
            asignadas = {}
            for m in _ASIGNACION.finditer(codigo):
                asignadas.setdefault(m.group(1), set()).add(int(m.group(2)))
            for m in _ASIGNACION_TERNARIA.finditer(codigo):
                asignadas.setdefault(m.group(1), set()).update((int(m.group(2)), int(m.group(3))))
        return {a.literales[i].strip() for i in asignadas.get(variable, ())}

    if 'directaccess' in bajo:
        for m in _DIRECTO.finditer(codigo):
            if m.group(1) is not None:
                a.directos.add(a.literales[int(m.group(1))].strip())
            elif m.group(2) and textos(m.group(2)):
                a.directos.update(textos(m.group(2)))
            else:
                a.dinamico = True
    if 'lookup' in bajo:
        for m in _LOOKUP.finditer(codigo):
            a.lookups.add(a.literales[int(m.group(1))].strip())
        if 'getlookup' in bajo:
            for m in _LOOKUP_VARIABLE.finditer(codigo):
                if textos(m.group(1)):
                    a.lookups.update(textos(m.group(1)))
                else:
                    a.dinamico = True
    for texto in a.literales:
        _clasificar(a, texto)
    a.lookups.discard('')
    a.lookups = {x for x in a.lookups if x.lower() not in _NO_LOOKUPS}
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
    return bool(_NOMBRE_LOOKUP.match(texto)) and texto.lower() not in _NO_LOOKUPS


_COMENTARIO_CSS = re.compile(r'/\*.*?\*/', re.S)
_IMPORT_CSS = re.compile(r'''@import\s+(?:url\(\s*)?["']?([^"')\s;]+)''', re.I)


def analizar_css(css):
    a = AnalisisWeb()
    for m in _IMPORT_CSS.finditer(_COMENTARIO_CSS.sub(' ', css or '')):
        f = _FICHERO.search(m.group(1))
        if f:
            a.ficheros.add(f.group(1).lower())
    return a


# ---------------------------------------------------------------------------
# Caché en disco, por el contenido de cada fichero. Las librerías (kendo, knockout...) son iguales
# en todos los servidores y en todas las extracciones: se analizan una vez y ya está.
# Está en %LOCALAPPDATA%\SQL-Lens (lleva texto de los .js: se queda en el perfil del usuario).
# ---------------------------------------------------------------------------

# Súbela si cambia lo que detectan analizar_js / analizar_css: la caché vieja deja de usarse.
VERSION_ANALISIS = 2
_CACHE = Path(os.environ.get('LOCALAPPDATA') or tempfile.gettempdir()) / 'SQL-Lens' / 'cache-web.sqlite'
_CACHE_LOCK = threading.Lock()
_CADUCIDAD = 90 * 86400    # lo que no se usa en 90 días se borra
_CONJUNTOS = ('definiciones', 'llamadas', 'directos', 'lookups', 'ficheros')


def _a_texto(a):
    return json.dumps({k: sorted(getattr(a, k)) if k in _CONJUNTOS else getattr(a, k) for k in AnalisisWeb.__slots__},
                      ensure_ascii=False, separators=(',', ':'))


def _de_texto(texto):
    datos, a = json.loads(texto), AnalisisWeb()
    for k in AnalisisWeb.__slots__:
        setattr(a, k, set(datos[k]) if k in _CONJUNTOS else datos[k])
    return a


def analizar_ficheros(ficheros):
    """[(código, es_css)] → [AnalisisWeb], en el mismo orden. Sin caché si no se puede (y no pasa nada)."""
    claves = [hashlib.sha1(f'{VERSION_ANALISIS}|{int(css)}|'.encode() + (codigo or '').encode('utf-8', 'surrogatepass'))
              .hexdigest() for codigo, css in ficheros]
    with _CACHE_LOCK:
        guardados, con = {}, None
        try:
            _CACHE.parent.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(_CACHE, timeout=30)
            con.execute('CREATE TABLE IF NOT EXISTS analisis (clave TEXT PRIMARY KEY, datos TEXT, usado REAL)')
            pedidas = list(set(claves))
            for i in range(0, len(pedidas), 500):
                trozo = pedidas[i:i + 500]
                guardados.update(con.execute(f'SELECT clave, datos FROM analisis WHERE clave IN ({",".join("?" * len(trozo))})',
                                             trozo).fetchall())
        except (sqlite3.Error, OSError):
            con = None

        resultado, nuevos = [], {}
        for (codigo, css), clave in zip(ficheros, claves):
            if clave in guardados:
                try:
                    resultado.append(_de_texto(guardados[clave]))
                    continue
                except (ValueError, KeyError, TypeError):
                    pass    # entrada estropeada: se vuelve a analizar
            a = analizar_css(codigo) if css else analizar_js(codigo)
            nuevos[clave] = _a_texto(a)
            resultado.append(a)

        if con is not None:
            try:
                ahora = time.time()
                with con:
                    con.executemany('INSERT OR REPLACE INTO analisis VALUES (?, ?, ?)',
                                    [(c, d, ahora) for c, d in nuevos.items()])
                    con.executemany('UPDATE analisis SET usado = ? WHERE clave = ?',
                                    [(ahora, c) for c in guardados])
                    con.execute('DELETE FROM analisis WHERE usado < ?', (ahora - _CADUCIDAD,))
            except sqlite3.Error:
                pass
            finally:
                con.close()
    return resultado

