"""
Construcción del grafo.
"""

from collections import defaultdict

from analisis import analizar_modulo, partir_nombre, referencias_en_texto

TIPO_OBJETO = {
    'U': 'TABLA', 'ET': 'TABLA', 'V': 'VISTA',
    'P': 'PROC', 'PC': 'PROC', 'X': 'PROC', 'RF': 'PROC',
    'FN': 'FUNCION', 'IF': 'FUNCION', 'TF': 'FUNCION', 'FS': 'FUNCION',
    'FT': 'FUNCION', 'AF': 'FUNCION',
    'TR': 'TRIGGER', 'TA': 'TRIGGER',
    'SN': 'SINONIMO', 'SO': 'SECUENCIA',
}
# Restricciones CHECK y DEFAULT: lo que usan (una función, casi siempre) se apunta a su tabla.
DE_SU_TABLA = {'C', 'D'}

TIPOS_ARISTA = ['lee', 'escribe', 'ejecuta', 'usa', 'dispara', 'sinonimo']
_FUERZA = {'lee': 0, 'escribe': 1}
_BASES_IGNORADAS = {'tempdb'}
_PSEUDOTABLAS = {'inserted', 'deleted'}


def _fecha(valor):
    if not valor:
        return None
    # Del .json de extraer.ps1 llega como texto; de pyodbc, como datetime.
    return valor[:10] if isinstance(valor, str) else valor.strftime('%Y-%m-%d')


class Grafo:
    def __init__(self):
        self.nodos = {}      # clave -> dict
        self.aristas = {}    # (origen, destino) -> [tipo, posible]

    def nodo(self, clave, **datos):
        if clave not in self.nodos:
            self.nodos[clave] = {'k': clave, **datos}
        return self.nodos[clave]

    def arista(self, origen, destino, tipo, posible=False):
        if origen == destino:
            return
        actual = self.aristas.get((origen, destino))
        if actual is None:
            self.aristas[(origen, destino)] = [tipo, posible]
            return
        # Lo confirmado por el catálogo gana a lo "posible"; escribir, a leer.
        if actual[1] and not posible:
            self.aristas[(origen, destino)] = [tipo, False]
        elif actual[1] == posible and _FUERZA.get(tipo, -1) > _FUERZA.get(actual[0], -1):
            actual[0] = tipo


class _Constructor:
    def __init__(self, ex, incluir_codigo):
        self.ex = ex
        self.incluir_codigo = incluir_codigo
        self.g = Grafo()
        self.sistema = {n.lower() for n in ex.get('sistema', ())}
        self.bases = {b.lower(): b for b in ex['bases']}
        self.por_id = {}        # base -> {object_id: clave}
        self.por_nombre = {}    # base -> {nombre: [claves]}
        self.referente = {}     # base -> {object_id: clave}  (restricción -> su tabla)
        self.esquemas = {}      # base -> {esquemas con algún objeto}
        self.analisis = {}      # clave -> Analisis
        self.avisos = list(ex.get('avisos', ()))

    # ---- nodos ---------------------------------------------------------------

    def cargar_objetos(self):
        for base, d in self.ex['bases'].items():
            bl = base.lower()
            por_id, por_nombre, referente = {}, defaultdict(list), {}
            modulos = {r['object_id']: r['definition'] for r in d['modulos']}
            filas = {r['object_id']: r['filas'] for r in d.get('filas', ())}
            restricciones, triggers = {}, []
            for o in d['objetos']:
                t = o['type'].strip()
                if t in DE_SU_TABLA:
                    restricciones[o['object_id']] = o['parent_object_id']
                    continue
                tipo = TIPO_OBJETO.get(t)
                if not tipo:
                    continue
                clave = f"{bl}.{o['esquema'].lower()}.{o['name'].lower()}"
                nodo = self.g.nodo(clave, b=base, e=o['esquema'], n=o['name'], t=tipo,
                                   c=_fecha(o.get('create_date')), m=_fecha(o.get('modify_date')))
                oid = o['object_id']
                por_id[oid] = referente[oid] = clave
                por_nombre[o['name'].lower()].append(clave)
                if tipo == 'TABLA' and oid in filas:
                    nodo['f'] = int(filas[oid] or 0)
                if oid in modulos:
                    definicion = modulos[oid]
                    if definicion is None:
                        nodo['cifrado'] = 1
                    else:
                        nodo['l'] = definicion.count('\n') + 1
                        if self.incluir_codigo:
                            nodo['s'] = definicion
                        self.analisis[clave] = analizar_modulo(definicion)
                        if self.analisis[clave].dinamico:
                            nodo['d'] = 1
                if tipo == 'TRIGGER':
                    triggers.append((clave, o['parent_object_id']))
            for rid, padre in restricciones.items():
                if padre in por_id:
                    referente[rid] = por_id[padre]
            for clave, padre in triggers:
                if padre in por_id:
                    self.g.arista(clave, por_id[padre], 'dispara')
            self.por_id[bl], self.por_nombre[bl], self.referente[bl] = por_id, por_nombre, referente
            self.esquemas[bl] = {o['esquema'].lower() for o in d['objetos']} | {'dbo'}


    def resolver(self, base_actual, servidor, bd, esquema, nombre, referenced_id=None, crear=True):
        if not nombre or nombre[0] in '#@':
            return None
        nl = nombre.lower()
        esq = (esquema or '').lower()
        if esq in ('sys', 'information_schema'):
            return None
        if servidor:
            if not crear:
                return None
            clave = f"{servidor}.{bd or ''}.{esq or 'dbo'}.{nl}".lower()
            self.g.nodo(clave, b=f"{servidor}.{bd or '?'}", e=esquema or 'dbo', n=nombre, t='EXTERNO')
            return clave
        bl = (bd or base_actual).lower()
        if bl in _BASES_IGNORADAS:
            return None
        if bl in self.bases:
            if referenced_id is not None and bl == base_actual.lower():
                # Resuelto por SQL Server. 
                return self.por_id[bl].get(referenced_id)
            if esq:
                clave = f"{bl}.{esq}.{nl}"
                if clave in self.g.nodos:
                    return clave
            else:
                clave = f"{bl}.dbo.{nl}"
                if clave in self.g.nodos:
                    return clave
                candidatos = self.por_nombre[bl].get(nl)
                if candidatos:
                    return candidatos[0]
            if nl in self.sistema or not crear:
                return None
            if nl in _PSEUDOTABLAS and esq in ('', 'dbo'):
                return None
            if esq and esq not in self.esquemas[bl]:
                return None
            clave = f"{bl}.{esq or 'dbo'}.{nl}"
            self.g.nodo(clave, b=self.bases[bl], e=esquema or 'dbo', n=nombre, t='NO_EXISTE')
            return clave
        # Otra base que no se ha extraído.
        if not crear or (bl == 'master' and nl in self.sistema):
            return None
        clave = f"{bl}.{esq or 'dbo'}.{nl}"
        self.g.nodo(clave, b=bd, e=esquema or 'dbo', n=nombre, t='EXTERNO')
        return clave


    def tipo_arista(self, origen, destino, esquema_escrito, nombre_escrito, es_restriccion):
        td = self.g.nodos[destino]['t']
        to = self.g.nodos[origen]['t']
        if es_restriccion or td in ('FUNCION', 'SECUENCIA'):
            return 'usa'
        if td == 'PROC':
            return 'ejecuta'
        a = self.analisis.get(origen)
        if a is None or to in ('VISTA', 'FUNCION', 'TABLA'):
            return 'lee'
        nombre_real = self.g.nodos[destino]['n']
        if a.ejecuta_a(esquema_escrito, nombre_escrito) and td in ('EXTERNO', 'NO_EXISTE'):
            return 'ejecuta'
        if a.escribe_en(esquema_escrito, nombre_escrito) or a.escribe_en(esquema_escrito, nombre_real):
            return 'escribe'
        return 'lee'

    def cargar_dependencias(self):
        for base, d in self.ex['bases'].items():
            bl = base.lower()
            referente = self.referente[bl]
            for dep in d['dependencias']:
                rid = dep['referencing_id']
                origen = referente.get(rid)
                if not origen:
                    continue
                destino = self.resolver(base, dep['referenced_server_name'], dep['referenced_database_name'],
                                        dep['referenced_schema_name'], dep['referenced_entity_name'],
                                        dep['referenced_id'])
                if not destino or destino == origen:
                    continue
                tipo = self.tipo_arista(origen, destino, dep['referenced_schema_name'],
                                        dep['referenced_entity_name'], rid not in self.por_id[bl])
                self.g.arista(origen, destino, tipo)

    def cargar_sinonimos(self):
        for base, d in self.ex['bases'].items():
            for s in d.get('sinonimos', ()):
                origen = self.por_id[base.lower()].get(s['object_id'])
                if not origen:
                    continue
                srv, bd, esq, nombre = partir_nombre(s['base_object_name'])
                destino = self.resolver(base, srv, bd, esq, nombre)
                if destino:
                    self.g.arista(origen, destino, 'sinonimo')

    def _tipo_texto(self, accion, destino):
        td = self.g.nodos[destino]['t']
        if td in ('FUNCION', 'SECUENCIA'):
            return 'usa'
        if td == 'PROC':
            return 'ejecuta'
        return 'escribe' if accion == 'escribe' else 'lee'

    def cargar_sql_dinamico(self):
        """Objetos nombrados dentro de los literales de un módulo con SQL dinámico."""
        for origen, a in self.analisis.items():
            if not a.dinamico:
                continue
            base = self.g.nodos[origen]['b']
            for literal in a.literales:
                for (srv, bd, esq, nombre), accion in referencias_en_texto(literal).items():
                    destino = self.resolver(base, srv, bd, esq, nombre, crear=False)
                    if destino and destino != origen and (origen, destino) not in self.g.aristas:
                        self.g.arista(origen, destino, self._tipo_texto(accion, destino), posible=True)

    def cargar_jobs(self):
        jobs = defaultdict(list)
        for paso in self.ex.get('jobs', ()):
            jobs[(paso['job_id'], paso['job'])].append(paso)
        for (_, nombre), pasos in sorted(jobs.items(), key=lambda x: x[0][1].lower()):
            clave = f"job:{nombre.lower()}"
            aristas, toca_bases = [], False
            for p in pasos:
                base = p['database_name'] or 'master'
                toca_bases |= base.lower() in self.bases
                if (p['subsystem'] or '').upper() != 'TSQL' or not p['command']:
                    continue
                for (srv, bd, esq, n), accion in referencias_en_texto(p['command']).items():
                    destino = self.resolver(base, srv, bd, esq, n, crear=False)
                    if destino:
                        aristas.append((destino, accion))
            if not aristas and not toca_bases:
                continue
            nodo = self.g.nodo(clave, b='SQL Agent', e='job', n=nombre, t='JOB',
                               activo=1 if pasos[0]['enabled'] else 0, pasos=len(pasos))
            texto = []
            for p in pasos:
                texto.append(f"-- Paso {p['step_id']}: {p['step_name']}  "
                             f"({p['subsystem']}, base {p['database_name'] or '-'})")
                texto.append((p['command'] or '').rstrip())
                texto.append('')
            codigo = '\n'.join(texto)
            nodo['l'] = codigo.count('\n') + 1
            if self.incluir_codigo:
                nodo['s'] = codigo
            for destino, accion in aristas:
                self.g.arista(clave, destino, self._tipo_texto(accion, destino))

    def resultado(self):
        claves = sorted(self.g.nodos)
        indice = {k: i for i, k in enumerate(claves)}
        nodos = [self.g.nodos[k] for k in claves]
        tipo_idx = {t: i for i, t in enumerate(TIPOS_ARISTA)}
        aristas = sorted([indice[o], indice[d], tipo_idx[t], 1 if p else 0]
                         for (o, d), (t, p) in self.g.aristas.items())
        conteo = defaultdict(int)
        for n in nodos:
            conteo[n['t']] += 1
        meta = {
            'servidor': self.ex.get('servidor'),
            'bases': list(self.ex['bases']),
            'fecha': self.ex.get('fecha'),
            'nodos': len(nodos),
            'aristas': len(aristas),
            'posibles': sum(a[3] for a in aristas),
            'porTipo': dict(conteo),
            'conCodigo': self.incluir_codigo,
            'avisos': self.avisos,
        }
        return {'meta': meta, 'tiposArista': TIPOS_ARISTA, 'nodos': nodos, 'aristas': aristas}


def construir(extraccion, incluir_codigo=True):
    """
    extraccion = {
      'servidor': str, 'fecha': str, 'sistema': [nombres de objetos del sistema],
      'bases': {nombre: {'objetos', 'modulos', 'filas', 'dependencias', 'sinonimos'}},
      'jobs': [pasos], 'avisos': [str],
    }
    """
    c = _Constructor(extraccion, incluir_codigo)
    c.cargar_objetos()
    c.cargar_dependencias()
    c.cargar_sinonimos()
    c.cargar_sql_dinamico()
    c.cargar_jobs()
    return c.resultado()
