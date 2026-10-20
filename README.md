# SQL-Lens

Shows what depends on each table, view, procedure or function of a SQL Server
database: **"if I change this, what breaks?"**. The result is a single `.html`
file you open with a double click.

```
extractions/   the .json files copied from the servers
maps/          the generated maps
comparisons/   the generated comparisons
```


## The app

Double-click **`SQL-Lens.exe`**. Nothing to install, the extraction runs `extraer.ps1` with PowerShell, so it also works on the VMs.

- **EXTRACT**: server and database → saves the `.json` in `extractions/`.
- **MAP**: pick an extraction → the map opens in the window.
- **COMPARE**: pick two extractions → the comparison opens in the window.

*Save as HTML* writes the map or comparison to `maps/` or `comparisons/` to
share it. 


## Build the .exe

`SQL-Lens.exe` is not in the repository: build it once, and again after any
change to the code. 

```bash
pip install pyinstaller pillow
```

```bash
.\build_exe.bat
```

It writes `SQL-Lens.exe` with the icon (`icon.ico`), the screens and
`extraer.ps1` inside. Copy just the `.exe` to any machine. It creates its `extractions/` folder next to itself.


## From the command line

The map is a snapshot. **Whenever the database changes** (new procedure, new
table, altered code...), repeat these three steps.


## 1. On the VM (extract): 

In the folder where `extraer.ps1` is:

```bash
powershell -ExecutionPolicy Bypass -File .\extraer.ps1 -Server localhost -Databases MESDB
```

It writes `extraction_<server>_MESDB_<date>.json`. It never touches table data or changes anything.


## 2. Copy the `.json` to the `extractions` folder


## 3. On your machine (map):

```bash
python map.py --from extraction_<server>_MESDB_<date>.json
```

With the extraction it writes `maps/map_<server>_MESDB_<date>.html`. Open it and search for any object.


## 4. Compare two databases

Extract each one (step 1, on each server) and run:

```bash
python comparar.py extraction_DEV_MESDB_<date>.json extraction_PRO_MESDB_<date>.json
```

It writes `comparisons/compare_<A>_vs_<B>_<date>.html`: how many objects of each type each one has, which exist only in A or only in B, and which differ. Tables are compared by their columns (whitespace differences are ignored).
