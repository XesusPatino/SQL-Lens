# SQL Map

Shows what depends on each table, view, procedure or function of a SQL Server
database: **"if I change this, what breaks?"**. The result is a single `.html`
file you open with a double click.

```
extractions/   the .json files copied from the servers
maps/          the generated maps
comparisons/   the generated comparisons
```

The map is a snapshot. **Whenever the database changes** (new procedure, new
table, altered code...), repeat these three steps.

## 1. On the VM: extract

In the folder where `extraer.ps1` is:

```bash
powershell -ExecutionPolicy Bypass -File .\extraer.ps1 -Server localhost -Databases MESDB
```

It writes `extraction_<server>_MESDB_<date>.json`. It only reads the catalog:
it never touches table data or changes anything.

## 2. Copy the `.json` to the `extractions` folder

## 3. On your machine: build the map

```bash
python extraer.py --from extraction_<server>_MESDB_<date>.json
```

Just the file name: it is looked for in `extractions/`. It writes
`maps/map_<server>_MESDB_<date>.html`. Open it and search for any object.
Old `.json` and `.html` files can be deleted.

If you only changed `visor.html`, `grafo.py` or `analisis.py`, skip steps 1–2
and rerun step 3 with the `.json` you already have.

## 4. Compare two databases

Extract each one (step 1, on each server) and run:

```bash
python comparar.py extraction_DEV_MESDB_<date>.json extraction_PRO_MESDB_<date>.json
```

It writes `comparisons/compare_<A>_vs_<B>_<date>.html`: how many objects of each type each
one has, which exist only in A or only in B, and which differ (click one to see
the changed lines). Tables are compared by their columns; whitespace
differences are ignored.
