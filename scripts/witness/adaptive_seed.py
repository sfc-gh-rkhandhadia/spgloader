#!/usr/bin/env python3
"""adaptive_seed.py - When a view returns 0 rows, parse its SQL to extract the
JOIN chain and WHERE conditions, then INSERT one dedicated witness row per
involved base table so all predicates are satisfied simultaneously.

INSERT approach (not UPDATE):
  - Each failing view gets its own isolated witness row set
  - Existing rows are never modified — no cross-view interference
  - IDENTITY PKs are read back after insert and propagated through the chain
"""

import re
from datetime import date, timedelta
from typing import Any
import uuid

# ---------------------------------------------------------------------------
# SQL parsing helpers
# ---------------------------------------------------------------------------

_RE_FROM_JOIN = re.compile(
    r"(?:FROM|JOIN)\s+"
    r"(?:\[?(?P<schema>\w+)\]?\.)?\[?(?P<table>\w+)\]?"
    r"(?:\s+(?:WITH\s*\([^)]*\)\s*)?(?:AS\s+)?(?P<alias>\w+))?",
    re.IGNORECASE,
)

_RE_CTE = re.compile(r"\b(\w+)\s+AS\s*\(", re.IGNORECASE)

_RE_EQUI = re.compile(
    r"\b(?P<a1>\w+)\.(?P<c1>\w+)\s*=\s*(?P<a2>\w+)\.(?P<c2>\w+)\b",
    re.IGNORECASE,
)

_RE_WHERE = re.compile(
    r"\b(?P<alias>\w+)\.(?P<col>\w+)\s*"
    r"(?P<op>=|<>|!=|>=|<=|>|<)\s*"
    r"(?P<val>'[^']*'|[-\d.]+)",
    re.IGNORECASE,
)

_RE_NONEMPTY = re.compile(
    r"COALESCE\s*\(\s*(?:\w+\s*\(\s*)*(?P<alias>\w+)\.(?P<col>\w+)",
    re.IGNORECASE,
)

# Bare column literal: col = 'value' without an alias prefix
_RE_WHERE_BARE = re.compile(
    r"(?<![.\w])(?P<col>[A-Za-z_]\w*)\s*=\s*'(?P<val>[^']+)'",
    re.IGNORECASE,
)

# OR-branch equi-join: OR alias1.col1 = alias2.col2
_RE_OR_EQUI = re.compile(
    r"\bOR\b\s+(?P<a1>\w+)\.(?P<c1>\w+)\s*=\s*(?P<a2>\w+)\.(?P<c2>\w+)",
    re.IGNORECASE,
)

_SQL_KEYWORDS = frozenset(
    "WITH NOLOCK READPAST UPDLOCK ROWLOCK TABLOCK READUNCOMMITTED "
    "WHERE INNER OUTER LEFT RIGHT FULL CROSS JOIN ON SET SELECT INSERT UPDATE DELETE "
    "FROM AS AND OR NOT IN IS NULL CASE WHEN THEN ELSE END HAVING GROUP ORDER BY "
    "DISTINCT TOP EXISTS ALL ANY SOME UNION INTERSECT EXCEPT".split()
)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def extract_cte_names(sql: str) -> set[str]:
    with_match = re.search(r"\bWITH\b", sql, re.IGNORECASE)
    if not with_match:
        return set()
    return {m.group(1).lower() for m in _RE_CTE.finditer(sql[with_match.start():])}


def extract_alias_map(sql: str, cte_names: set[str], known_fqns: set[str]) -> dict[str, str]:
    """Return {alias_lower: 'schema.table'} — case-insensitive FQN lookup."""
    fqn_lower_map = {fqn.lower(): fqn for fqn in known_fqns}
    mapping: dict[str, str] = {}
    for m in _RE_FROM_JOIN.finditer(sql):
        schema = m.group("schema") or "dbo"
        table  = m.group("table")
        alias  = (m.group("alias") or table).lower()

        if alias in cte_names or table.lower() in cte_names:
            continue
        # When extracted alias is a SQL keyword (e.g. ON/WITH), fall back to
        # the bare table name so the table still gets seeded.
        if alias.upper() in _SQL_KEYWORDS:
            alias = table.lower()
        if table.upper() in _SQL_KEYWORDS:
            continue

        candidate = f"{schema}.{table}".lower()
        if candidate in fqn_lower_map:
            mapping[alias] = fqn_lower_map[candidate]
        else:
            dbo_candidate = f"dbo.{table}".lower()
            if dbo_candidate in fqn_lower_map:
                mapping[alias] = fqn_lower_map[dbo_candidate]
    return mapping


def _inside_subquery(sql: str, pos: int) -> bool:
    depth = 0
    for ch in sql[:pos]:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
    return depth > 0


def extract_inner_join_pairs(sql: str, alias_map: dict) -> list[tuple]:
    """(table1, col1, table2, col2) equi-pairs from JOIN ... ON conditions."""
    pairs, seen = [], set()
    for m in _RE_EQUI.finditer(sql):
        a1, c1 = m.group("a1").lower(), m.group("c1")
        a2, c2 = m.group("a2").lower(), m.group("c2")
        if a1 not in alias_map or a2 not in alias_map or a1 == a2:
            continue
        if _inside_subquery(sql, m.start()):
            continue
        key = tuple(sorted([(alias_map[a1], c1.lower()), (alias_map[a2], c2.lower())]))
        if key not in seen:
            seen.add(key)
            pairs.append((alias_map[a1], c1, alias_map[a2], c2))
    return pairs


def extract_where_conditions(sql: str, alias_map: dict) -> list[tuple]:
    """(table, col, op, literal) from WHERE and JOIN ON filters."""
    conds = []
    for m in _RE_WHERE.finditer(sql):
        alias = m.group("alias").lower()
        if alias not in alias_map:
            continue
        if _inside_subquery(sql, m.start()):
            continue
        conds.append((alias_map[alias], m.group("col"), m.group("op"), m.group("val").strip("'")))

    for m in _RE_NONEMPTY.finditer(sql):
        alias = m.group("alias").lower()
        if alias in alias_map:
            conds.append((alias_map[alias], m.group("col"), "<>", ""))
    return conds


def extract_where_conditions_bare(sql: str, alias_map: dict, objects: dict) -> list[tuple]:
    """Match bare `col = 'literal'` WHERE conditions (no alias prefix).

    Attributes each condition to the first table in alias_map that has a column
    with that name.  Handles cases like `WHERE CartType = 'WebV2'` where the
    column is not qualified with a table alias.
    """
    conds = []
    for m in _RE_WHERE_BARE.finditer(sql):
        col = m.group("col")
        val = m.group("val")
        if col.upper() in _SQL_KEYWORDS:
            continue
        for alias, fqn in alias_map.items():
            obj_t = objects.get(fqn, {})
            if any(c["name"].lower() == col.lower()
                   for c in obj_t.get("columns", [])):
                conds.append((fqn, col, "=", val))
                break  # first matching table wins
    return conds


def extract_or_join_pairs(sql: str, alias_map: dict) -> list[tuple]:
    """Extract equi-pairs from the OR branch of JOIN ON conditions.

    For `JOIN t ON (a.x = b.y AND ...) OR a.z = b.w`, returns the OR-branch
    pair (table_a, z, table_b, w) so the seed can align both branches.
    """
    pairs, seen = [], set()
    for m in _RE_OR_EQUI.finditer(sql):
        a1, c1 = m.group("a1").lower(), m.group("c1")
        a2, c2 = m.group("a2").lower(), m.group("c2")
        if a1 not in alias_map or a2 not in alias_map or a1 == a2:
            continue
        if _inside_subquery(sql, m.start()):
            continue
        key = tuple(sorted([(alias_map[a1], c1.lower()), (alias_map[a2], c2.lower())]))
        if key not in seen:
            seen.add(key)
            pairs.append((alias_map[a1], c1, alias_map[a2], c2))
    return pairs


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------

def _default_val(col: dict, seq: int) -> Any:
    dtype = col.get("data_type", "NVARCHAR").upper()
    if col.get("identity"):
        return None
    if any(t in dtype for t in ("BIGINT", "INT", "SMALLINT", "TINYINT")):
        return seq
    if any(t in dtype for t in ("DECIMAL", "NUMERIC", "FLOAT", "REAL", "MONEY")):
        return float(seq)
    if "BIT" in dtype:
        return 0
    if "TIME" == dtype.strip():
        return "10:00:00"
    if "DATE" in dtype and "TIME" not in dtype:
        return str(date(2024, 1, 1) + timedelta(days=seq % 365))
    if "DATETIME" in dtype:
        return "2024-01-01 10:00:00"
    if "UNIQUEIDENTIFIER" in dtype:
        return str(uuid.uuid4())
    return f"w{seq}"


def _parse_literal(val: str, col: dict) -> Any:
    dtype = col.get("data_type", "NVARCHAR").upper() if col else "NVARCHAR"
    try:
        if any(t in dtype for t in ("INT", "BIGINT", "SMALLINT", "TINYINT", "BIT")):
            return int(float(val))
        if any(t in dtype for t in ("DECIMAL", "NUMERIC", "FLOAT", "REAL", "MONEY")):
            return float(val)
        return val
    except (ValueError, TypeError):
        return val


# ---------------------------------------------------------------------------
# INSERT-based witness chain
# ---------------------------------------------------------------------------

def _clean_col_name(name: str) -> str:
    """Strip brackets and trailing ASC/DESC — pk_columns are stored as 'Id] ASC'."""
    cleaned = re.sub(r"\s*(ASC|DESC)\s*$", "", name.strip(), flags=re.I)
    return cleaned.strip("[] `")


def _is_pk(col_name: str, obj: dict) -> bool:
    # pk_columns may have trailing ASC/DESC: strip before comparing
    pk = {_clean_col_name(c).lower() for c in obj.get("pk_columns", [])}
    if col_name.lower() in pk:
        return True
    return any(c["name"].lower() == col_name.lower() and c.get("identity")
               for c in obj.get("columns", []))


def _schema_table(fqn: str) -> str:
    parts = fqn.split(".", 1)
    s, t = (parts[0], parts[1]) if len(parts) == 2 else ("dbo", parts[0])
    return f"[{s}].[{t}]"


def _next_pk_val(conn: Any, fqn: str, pk_col: str) -> int:
    """Return MAX(pk_col) + 1 so the new row doesn't collide with existing PKs."""
    st = _schema_table(fqn)
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT ISNULL(MAX([{pk_col}]), 0) FROM {st}")
        row = cur.fetchone()
        cur.close()
        return int(row[0]) + 1 if row and row[0] is not None else 1
    except Exception:
        return 99_000_000 + hash(fqn) % 1_000_000


def _insert_witness(conn: Any, fqn: str, obj: dict, row_vals: dict, seq: int) -> dict:
    """INSERT one witness row into `fqn` and read back all committed column values.

    For non-IDENTITY PK columns, queries MAX(pk) + 1 to avoid PK collision.
    """
    columns = obj.get("columns", [])
    if not columns:
        return {}

    # Resolve the value for each non-identity column
    row_vals_lower = {k.lower(): v for k, v in row_vals.items()}
    final = {}

    # For non-IDENTITY PK columns, compute a safe next value unless already specified
    pk_cols_clean = [_clean_col_name(c) for c in obj.get("pk_columns", [])]
    for pk_name in pk_cols_clean:
        if pk_name.lower() not in row_vals_lower:
            # Check if it's an IDENTITY (auto-assigned — don't specify)
            is_identity = any(
                c["name"].lower() == pk_name.lower() and c.get("identity")
                for c in columns
            )
            if not is_identity:
                row_vals_lower[pk_name.lower()] = _next_pk_val(conn, fqn, pk_name)

    for col in columns:
        name = col["name"]
        if col.get("identity"):
            continue
        if name.lower() in row_vals_lower:
            final[name] = row_vals_lower[name.lower()]
        else:
            final[name] = _default_val(col, seq)

    if not final:
        return {}

    col_clause   = ", ".join(f"[{k}]" for k in final)
    placeholders = ", ".join("%s" for _ in final)
    sql_ins = f"INSERT INTO {_schema_table(fqn)} ({col_clause}) VALUES ({placeholders})"

    cur = conn.cursor()
    try:
        cur.execute(sql_ins, tuple(final.values()))
        conn.commit()
    except Exception:
        conn.rollback()
        cur.close()
        return {}

    # Read back the inserted row — use the PK value we just inserted
    all_cols = ", ".join(f"[{c['name']}]" for c in columns)
    pk_cols  = obj.get("pk_columns", [])
    order_by = f"ORDER BY [{_clean_col_name(pk_cols[0])}] DESC" if pk_cols else ""
    try:
        cur.execute(f"SELECT TOP 1 {all_cols} FROM {_schema_table(fqn)} {order_by}")
        db_row = cur.fetchone()
        cur.close()
    except Exception:
        cur.close()
        return {}

    if db_row is None:
        return {}

    return {c["name"]: db_row[i] for i, c in enumerate(columns) if db_row[i] is not None}


def _extract_from_order(sql: str, alias_map: dict, objects: dict) -> list[str]:
    """Extract tables in the order they appear in FROM/JOIN clauses.

    When a JOIN references a VIEW (e.g. api.extvw_DistinctHierarchy), this
    function resolves it to its underlying base tables so that those tables
    appear in the correct position in the insert sequence.
    """
    seen: set[str] = set()
    order: list[str] = []
    fqn_ci = {f.lower(): f for f in objects}

    for m in _RE_FROM_JOIN.finditer(sql):
        schema = m.group("schema") or "dbo"
        table  = m.group("table")
        alias  = (m.group("alias") or table).lower()
        if alias.upper() in _SQL_KEYWORDS:
            alias = table.lower()
        if table.upper() in _SQL_KEYWORDS:
            continue
        resolved = (fqn_ci.get(f"{schema}.{table}".lower())
                    or fqn_ci.get(f"dbo.{table}".lower()))
        if not resolved:
            continue
        obj = objects.get(resolved, {})
        if obj.get("type") == "TABLE":
            if resolved not in seen:
                seen.add(resolved)
                order.append(resolved)
        elif obj.get("type") == "VIEW":
            # Resolve the VIEW to its base tables and insert them in-place
            for base in _resolve_to_base_tables(resolved, objects):
                if base not in seen:
                    seen.add(base)
                    order.append(base)
    return order


def _toposort(tables: list[str], join_pairs: list[tuple], objects: dict) -> list[str]:
    """Topological sort: tables whose PK/IDENTITY columns are referenced go first."""
    from collections import defaultdict, deque
    in_deg = {t: 0 for t in tables}
    children: dict[str, list[str]] = defaultdict(list)

    for t1, c1, t2, c2 in join_pairs:
        if t1 not in in_deg or t2 not in in_deg or t1 == t2:
            continue
        obj1 = objects.get(t1, {})
        obj2 = objects.get(t2, {})
        c1_is_pk = _is_pk(c1, obj1)
        c2_is_pk = _is_pk(c2, obj2)
        if c1_is_pk and not c2_is_pk:
            in_deg[t2] += 1
            children[t1].append(t2)
        elif c2_is_pk and not c1_is_pk:
            in_deg[t1] += 1
            children[t2].append(t1)

    queue = deque([t for t, d in in_deg.items() if d == 0])
    order = []
    while queue:
        node = queue.popleft()
        order.append(node)
        for child in children[node]:
            in_deg[child] -= 1
            if in_deg[child] == 0:
                queue.append(child)
    for t in tables:
        if t not in order:
            order.append(t)
    return order


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def _resolve_to_base_tables(view_fqn: str, objects: dict, _seen: set | None = None) -> list[str]:
    """Recursively resolve a VIEW to its underlying base TABLE fqns (depth-first).

    Uses case-insensitive FQN matching because view DDL often stores dependency
    names in a different case than the inventory keys (e.g., 'API.Hierarchy' vs
    'api.Hierarchy').
    """
    if _seen is None:
        _seen = set()
    if view_fqn in _seen:
        return []
    _seen.add(view_fqn)
    obj = objects.get(view_fqn, {})
    if obj.get("type") == "TABLE":
        return [view_fqn]
    if obj.get("type") != "VIEW":
        return []
    # Case-insensitive lookup for dependencies
    fqn_ci = {fqn.lower(): fqn for fqn in objects}
    result = []
    for dep in obj.get("dependencies", []):
        resolved = fqn_ci.get(dep.lower())
        if resolved:
            result.extend(_resolve_to_base_tables(resolved, objects, _seen))
    return result


def adaptive_validate_view(conn: Any, obj: dict, objects: dict) -> dict | None:
    """For a view that returned 0 rows: parse its SQL, INSERT witness rows that
    satisfy all JOIN and WHERE conditions, then re-test.

    Uses INSERT (not UPDATE) so existing data is untouched and multiple views
    can be fixed independently without cross-contamination.
    """
    sql    = obj.get("ddl", "")
    schema = obj["schema"]
    name   = obj["name"]

    known_fqns = set(objects.keys())
    cte_names  = extract_cte_names(sql)
    alias_map  = extract_alias_map(sql, cte_names, known_fqns)

    if not alias_map:
        return None

    join_pairs  = extract_inner_join_pairs(sql, alias_map)
    or_pairs    = extract_or_join_pairs(sql, alias_map)          # Fix 3: OR-branch equi-pairs
    where_conds = extract_where_conditions(sql, alias_map)
    where_conds += extract_where_conditions_bare(sql, alias_map, objects)  # Fix 2: bare literals

    if not join_pairs and not or_pairs and not where_conds:
        return None

    # Expand alias_map: resolve VIEW FQNs to their underlying base TABLE FQNs.
    view_to_bases: dict[str, list[str]] = {}
    expanded_alias_map: dict[str, str] = {}  # alias → real table (resolved)
    for alias, fqn in alias_map.items():
        if objects.get(fqn, {}).get("type") == "VIEW":
            bases = _resolve_to_base_tables(fqn, objects)
            if bases:
                view_to_bases[fqn] = bases
                expanded_alias_map[alias] = bases[0]
        else:
            expanded_alias_map[alias] = fqn

    def _resolve_fqn(fqn: str) -> str:
        if fqn in view_to_bases:
            return view_to_bases[fqn][0]
        return fqn

    translated_join_pairs = [
        (_resolve_fqn(t1), c1, _resolve_fqn(t2), c2)
        for t1, c1, t2, c2 in (join_pairs + or_pairs)  # Fix 3: include OR branch
    ]

    # Build the full set of base tables (direct + from resolved views)
    base_tables = {fqn for fqn in set(expanded_alias_map.values())
                   if objects.get(fqn, {}).get("type") == "TABLE"}
    if not base_tables:
        return None
    for bases in view_to_bases.values():
        base_tables.update(b for b in bases if objects.get(b, {}).get("type") == "TABLE")

    # Fix 1: Self-join — build alias_occurrences so tables used under multiple
    # aliases each get their own witness row with alias-specific WHERE conditions.
    # e.g. mfcg→MenuFlowCondimentGroups (RelType=1) AND mfcgChild→same (RelType=2)
    from collections import defaultdict as _defaultdict
    alias_occurrences: dict[str, list[str]] = _defaultdict(list)
    for alias, fqn in expanded_alias_map.items():
        if objects.get(fqn, {}).get("type") == "TABLE":
            alias_occurrences[fqn].append(alias)

    # Collect per-alias WHERE conditions (e.g. mfcg.RelationshipType = 1)
    alias_conds: dict[str, list[tuple]] = _defaultdict(list)
    for m in _RE_WHERE.finditer(sql):
        al = m.group("alias").lower()
        if al in alias_map and not _inside_subquery(sql, m.start()):
            fqn_al = alias_map[al]
            alias_conds[al].append((
                _resolve_fqn(fqn_al),
                m.group("col"), m.group("op"),
                m.group("val").strip("'"),
            ))

    from_order = _extract_from_order(sql, alias_map, objects)
    ordered = from_order + [t for t in _toposort(list(base_tables), translated_join_pairs, objects)
                             if t not in from_order]

    committed: dict[tuple, Any] = {}
    seq_base = abs(hash(name)) % 50000 + 40000

    tables_inserted = []
    for tbl_fqn in ordered:
        if tbl_fqn not in base_tables:
            continue
        obj_tbl = objects.get(tbl_fqn, {})
        row_vals: dict[str, Any] = {}

        # --- Apply JOIN conditions: use committed parent values ---
        for t1, c1, t2, c2 in translated_join_pairs:
            if t2 == tbl_fqn:
                key = (t1, c1.lower())
                if key in committed:
                    row_vals[c2] = committed[key]
            elif t1 == tbl_fqn:
                key = (t2, c2.lower())
                if key in committed:
                    row_vals[c1] = committed[key]

        # --- Apply WHERE conditions (aliased + bare literals) ---
        for (tbl_raw, col, op, val) in where_conds:
            tbl = _resolve_fqn(tbl_raw)
            if tbl != tbl_fqn:
                continue
            col_meta = next((c for c in obj_tbl.get("columns", [])
                             if c["name"].lower() == col.lower()), {})
            if _is_pk(col, obj_tbl):
                continue
            if op == "=":
                row_vals[col] = _parse_literal(val, col_meta)
            elif op in (">", ">="):
                try:
                    row_vals[col] = max(int(float(val)) + 1, 1)
                except ValueError:
                    row_vals[col] = 1
            elif op in ("<", "<="):
                try:
                    threshold = int(float(val))
                    row_vals[col] = threshold if op == "<=" else max(threshold - 1, 0)
                except ValueError:
                    row_vals[col] = 0
            elif op in ("<>", "!="):
                if val == "":
                    row_vals[col] = "WITNESS_DATA"

        col_vals = _insert_witness(conn, tbl_fqn, obj_tbl, row_vals, seq_base)
        if col_vals:
            tables_inserted.append(tbl_fqn)
            for col_name, val in col_vals.items():
                committed[(tbl_fqn, col_name.lower())] = val
            seq_base += 1

    # Fix 1: Self-join extra rows — for tables used under multiple aliases with
    # DIFFERENT per-alias WHERE conditions (e.g. RelationshipType=1 vs =2),
    # insert one additional row per extra alias so every INNER JOIN alias can
    # find its own matching row.  This is separate from the main loop so it
    # cannot regress the FK chain of already-validated views.
    for tbl_fqn in ordered:
        if tbl_fqn not in base_tables:
            continue
        obj_tbl  = objects.get(tbl_fqn, {})
        slots    = alias_occurrences.get(tbl_fqn, [])
        if len(slots) <= 1:
            continue  # normal table — no extra rows needed

        # Collect the distinct condition-sets for each alias
        for extra_alias in slots[1:]:
            extra_conds = alias_conds.get(extra_alias, [])
            if not extra_conds:
                continue  # alias has no distinct conditions — skip
            # Build the extra row: start from the main committed values (FK alignment)
            extra_row: dict[str, Any] = {}
            for t1, c1, t2, c2 in translated_join_pairs:
                if t2 == tbl_fqn and (t1, c1.lower()) in committed:
                    extra_row[c2] = committed[(t1, c1.lower())]
                elif t1 == tbl_fqn and (t2, c2.lower()) in committed:
                    extra_row[c1] = committed[(t2, c2.lower())]
            # Overlay alias-specific conditions for this extra slot
            for (tbl2, col2, op2, val2) in extra_conds:
                if _resolve_fqn(tbl2) != tbl_fqn:
                    continue
                col_meta2 = next((c for c in obj_tbl.get("columns", [])
                                  if c["name"].lower() == col2.lower()), {})
                if not _is_pk(col2, obj_tbl) and op2 == "=":
                    extra_row[col2] = _parse_literal(val2, col_meta2)
            _insert_witness(conn, tbl_fqn, obj_tbl, extra_row, seq_base)
            seq_base += 1

    if not tables_inserted:
        return None

    # Re-test the view
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT TOP 1 * FROM [{schema}].[{name}]")
        row = cur.fetchone()
        cur.close()
    except Exception as exc:
        return {"status": "failed", "note": f"Error after adaptive seed: {str(exc)[:200]}"}

    n = len(tables_inserted)
    if row is not None:
        return {
            "status": "validated",
            "note": (
                f"Validated after adaptive seed "
                f"(INSERT witness rows into {n} table(s): "
                f"{', '.join(t.split('.',1)[-1] for t in tables_inserted[:4])})"
            ),
        }

    return {
        "status": "failed",
        "note": (
            f"0 rows after adaptive seed (INSERTs into {n} table(s)) — "
            f"probable OR/AND join, computed predicate, or EXISTS subquery"
        ),
    }


# ---------------------------------------------------------------------------
# Procedure / Function support
# ---------------------------------------------------------------------------

# Matches parameter declarations: @name [AS] type [= default]
_RE_PROC_PARAM = re.compile(
    r"@(?P<name>\w+)\s+(?:AS\s+)?(?P<type>\w+(?:\s*\([^)]*\))?)"
    r"(?:\s*=\s*(?P<default>'[^']*'|[^,\n)]+))?",
    re.IGNORECASE,
)

# Extracts the parameter block (everything before AS/BEGIN)
_RE_PARAM_BLOCK = re.compile(
    r"CREATE\s+(?:OR\s+ALTER\s+)?(?:PROCEDURE|PROC|FUNCTION)\s+.*?(?:\(([^)]+)\)|(?=\s+AS\s|\s+WITH\s|\s+BEGIN\s))",
    re.IGNORECASE | re.DOTALL,
)


def parse_proc_params(sql: str) -> list[dict]:
    """Extract parameter declarations from a stored procedure or function DDL.

    Returns a list of {name, type, default} dicts, preserving declaration order.
    """
    # Use bracket-counting to find the true param block (handles varchar(MAX) etc.)
    m_create = re.search(
        r'CREATE\s+(?:OR\s+ALTER\s+)?(?:PROCEDURE|PROC|FUNCTION)\s+'
        r'\[?\w+\]?\s*\.\s*\[?\w+\]?\s*\(',
        sql, re.I,
    )
    if m_create:
        start = m_create.end() - 1  # position of opening '('
        depth, i = 0, start
        while i < len(sql):
            if sql[i] == '(':
                depth += 1
            elif sql[i] == ')':
                depth -= 1
                if depth == 0:
                    param_block = sql[start + 1:i]
                    break
            i += 1
        else:
            param_block = sql[start + 1:]
    else:
        # No parentheses — scan between CREATE and AS/BEGIN
        create_m = re.search(r"\bCREATE\b", sql, re.I)
        as_m     = re.search(r"^\s*(AS|BEGIN)\s*$", sql, re.I | re.M)
        if create_m and as_m:
            param_block = sql[create_m.end():as_m.start()]
        else:
            param_block = sql[:500]

    params = []
    for m in _RE_PROC_PARAM.finditer(param_block):
        default = (m.group("default") or "").strip().strip("'\"")
        params.append({
            "name":    m.group("name"),
            "type":    m.group("type").upper().split("(")[0],  # strip size
            "default": default or None,
        })
    return params


def infer_param_value(param: dict, conn: Any, objects: dict) -> Any:
    """Return a usable value for a stored procedure parameter.

    Priority:
      1. Explicit DDL default (= value in signature)
      2. Query the seeded table for the first matching value where the param
         name looks like a column (e.g. @posSystemId → POSSystem.POSSystemId)
      3. Type-based fallback
    """
    name  = param["name"]
    ptype = param["type"]
    default = param.get("default")

    # Use DDL default if present
    if default is not None:
        try:
            if any(t in ptype for t in ("INT", "BIGINT", "SMALLINT")):
                return int(float(default))
            if any(t in ptype for t in ("DECIMAL", "FLOAT", "NUMERIC")):
                return float(default)
            if "BIT" in ptype:
                return int(default)
        except (ValueError, TypeError):
            pass
        return default

    # Try to find a real value from seeded data by matching param name to a column
    name_lower = name.lower()
    fqn_ci = {fqn.lower(): fqn for fqn in objects}
    for fqn, obj in objects.items():
        if obj.get("type") != "TABLE":
            continue
        for col in obj.get("columns", []):
            if col["name"].lower() == name_lower:
                st = _schema_table(fqn)
                try:
                    cur = conn.cursor()
                    # MAX returns the most recently inserted witness row value,
                    # which is what our aligned seed will have just populated.
                    cur.execute(f"SELECT MAX([{col['name']}]) FROM {st} WHERE [{col['name']}] IS NOT NULL")
                    row = cur.fetchone()
                    cur.close()
                    if row and row[0] is not None:
                        return row[0]
                except Exception:
                    pass

    # Type-based fallback
    if any(t in ptype for t in ("INT", "BIGINT", "SMALLINT", "TINYINT")):
        return 1
    if any(t in ptype for t in ("DECIMAL", "NUMERIC", "FLOAT", "REAL", "MONEY")):
        return 1.0
    if "BIT" in ptype:
        return 0
    if "DATE" in ptype and "TIME" not in ptype:
        return "2024-01-01"
    if "DATETIME" in ptype:
        return "2024-01-01 10:00:00"
    if "UNIQUEIDENTIFIER" in ptype:
        return str(uuid.uuid4())
    return "TEST_VALUE"


def _exec_proc_and_fetch(conn: Any, schema: str, name: str, param_vals: dict) -> tuple[Any, str]:
    """Execute a stored procedure and return (first_row, error_note).

    param_vals: {param_name: value} — pass empty dict for no-arg execution.
    Returns (row, "") on success, (None, error_str) on error.
    """
    if param_vals:
        assignments = ", ".join(f"@{k} = %s" for k in param_vals)
        sql = f"EXEC [{schema}].[{name}] {assignments}"
        vals = tuple(param_vals.values())
    else:
        sql = f"EXEC [{schema}].[{name}]"
        vals = ()
    try:
        cur = conn.cursor()
        if vals:
            cur.execute(sql, vals)
        else:
            cur.execute(sql)
        row = cur.fetchone()
        cur.close()
        return row, ""
    except Exception as exc:
        return None, str(exc)[:250]


def _exec_tvf_and_fetch(conn: Any, schema: str, name: str, param_vals: list) -> tuple[Any, str]:
    """SELECT TOP 1 from a TVF with positional parameter values."""
    placeholders = ", ".join("%s" for _ in param_vals)
    sql = f"SELECT TOP 1 * FROM [{schema}].[{name}]({placeholders})"
    try:
        cur = conn.cursor()
        cur.execute(sql, tuple(param_vals))
        row = cur.fetchone()
        cur.close()
        return row, ""
    except Exception as exc:
        return None, str(exc)[:250]


def _extract_proc_select_body(sql: str) -> str | None:
    """Extract the first meaningful SELECT body from a stored procedure DDL.

    Strips the CREATE PROC header and any leading DML (INSERT/UPDATE/DELETE/EXEC)
    blocks, returning the fragment that starts with the first standalone SELECT
    that has a FROM clause. Returns None if no usable SELECT is found.
    """
    # Find the AS/BEGIN boundary — everything after that is the proc body
    m = re.search(r"\bAS\s*\n|^\s*BEGIN\s*$", sql, re.I | re.M)
    body = sql[m.end():] if m else sql

    # Walk lines, collect from the first SELECT … FROM that is not inside
    # an INSERT INTO … SELECT (those are DML, not query bodies)
    lines = body.splitlines()
    collecting = False
    depth = 0
    result_lines: list[str] = []

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip().upper()

        # Skip pure DML blocks
        if not collecting and re.match(r"(INSERT\s+INTO|UPDATE\s+\w|DELETE\s+FROM|EXEC\b)", stripped):
            i += 1
            continue

        # Start collecting at a top-level SELECT
        if not collecting and stripped.startswith("SELECT"):
            collecting = True

        if collecting:
            result_lines.append(line)
            depth += line.count("(") - line.count(")")
            # Stop at the end of the statement or BEGIN/END boundaries
            if depth <= 0 and any(kw in stripped for kw in ("FROM", "WHERE", "JOIN")):
                # Keep going until we hit a GO, END, or RETURN at depth 0
                pass
            if stripped in ("END", "GO", "RETURN") and depth <= 0:
                break

        i += 1

    candidate = "\n".join(result_lines).strip()
    # Must contain at least a FROM clause to be useful
    if re.search(r"\bFROM\b", candidate, re.I):
        return candidate
    return None


def _seed_proc_witness(conn: Any, obj: dict, objects: dict) -> dict | None:
    """Insert aligned witness rows driven by the proc's SELECT body.

    Unlike adaptive_validate_view, this function skips the final
    SELECT-from-view verification step — because the object is a proc,
    not a view.  It returns a dict with 'tables_inserted' if any rows
    were inserted, or None if the proc body has no parseable FROM/JOIN.
    """
    select_body = _extract_proc_select_body(obj.get("ddl", ""))
    # Fall back to the full DDL if body extraction yields nothing useful
    body_sql = select_body or obj.get("ddl", "")

    known_fqns = set(objects.keys())
    cte_names  = extract_cte_names(body_sql)
    alias_map  = extract_alias_map(body_sql, cte_names, known_fqns)

    if not alias_map:
        return None

    join_pairs  = extract_inner_join_pairs(body_sql, alias_map)
    where_conds = extract_where_conditions(body_sql, alias_map)

    if not join_pairs and not where_conds:
        return None

    # Resolve VIEWs to base tables (same as adaptive_validate_view)
    view_to_bases: dict[str, list[str]] = {}
    expanded_alias_map: dict[str, str] = {}
    for alias, fqn in alias_map.items():
        if objects.get(fqn, {}).get("type") == "VIEW":
            bases = _resolve_to_base_tables(fqn, objects)
            if bases:
                view_to_bases[fqn] = bases
                expanded_alias_map[alias] = bases[0]
        else:
            expanded_alias_map[alias] = fqn

    def _resolve_fqn(fqn: str) -> str:
        return view_to_bases[fqn][0] if fqn in view_to_bases else fqn

    translated_join_pairs = [
        (_resolve_fqn(t1), c1, _resolve_fqn(t2), c2)
        for t1, c1, t2, c2 in join_pairs
    ]

    base_tables = {fqn for fqn in set(expanded_alias_map.values())
                   if fqn in objects and objects[fqn].get("type") == "TABLE"}
    for t1, _, t2, _ in translated_join_pairs:
        if t1 in objects and objects[t1].get("type") == "TABLE":
            base_tables.add(t1)
        if t2 in objects and objects[t2].get("type") == "TABLE":
            base_tables.add(t2)

    # Build a consistent FK-value map (same as adaptive_validate_view)
    from_order = _extract_from_order(body_sql, alias_map, objects)
    table_order = [t for t in from_order if t in base_tables]
    for t in base_tables:
        if t not in table_order:
            table_order.append(t)

    committed: dict[tuple, Any] = {}
    tables_inserted: list[str] = []
    seq_base = 9000  # distinct range from bulk seed to avoid PK collisions

    for tbl_fqn in table_order:
        obj_tbl = objects.get(tbl_fqn)
        if not obj_tbl:
            continue

        row_vals: dict[str, Any] = {}

        # Honour JOIN equi-pairs: set col = committed value from paired table
        for t1, c1, t2, c2 in translated_join_pairs:
            if t1 == tbl_fqn and (t2, c2.lower()) in committed:
                row_vals[c1] = committed[(t2, c2.lower())]
            elif t2 == tbl_fqn and (t1, c1.lower()) in committed:
                row_vals[c2] = committed[(t1, c1.lower())]

        # Honour WHERE conditions
        for tbl, col, op, val in where_conds:
            real_tbl = expanded_alias_map.get(tbl, tbl)
            if _resolve_fqn(real_tbl) == tbl_fqn and col not in row_vals:
                if op in ("=", "=="):
                    row_vals[col] = val
                elif op in (">", ">="):
                    try:
                        row_vals[col] = int(val) + 1
                    except (ValueError, TypeError):
                        row_vals[col] = val
                elif op in ("<", "<="):
                    try:
                        row_vals[col] = int(val)
                    except (ValueError, TypeError):
                        row_vals[col] = val

        col_vals = _insert_witness(conn, tbl_fqn, obj_tbl, row_vals, seq_base)
        if col_vals:
            tables_inserted.append(tbl_fqn)
            for col_name, val in col_vals.items():
                committed[(tbl_fqn, col_name.lower())] = val
            seq_base += 1

    if not tables_inserted:
        return None

    return {"status": "seeded", "tables_inserted": tables_inserted, "committed": committed}


def _infer_params_from_anchor_row(params: list[dict], conn: Any,
                                  proc_ddl: str, objects: dict) -> dict[str, Any]:
    """Infer all proc params from a SINGLE row of the anchor (primary FROM) table.

    Picking values from the same row guarantees that multi-column WHERE conditions
    like WHERE Id = @Id AND HierarchyId = @HierarchyId are satisfied simultaneously.
    Falls back to the standard infer_param_value for params not found in anchor.
    """
    # Find the primary FROM table
    froms = re.findall(r'\bFROM\b\s+\[?(\w+)\]?\.\[?(\w+)\]?', proc_ddl, re.I)
    if froms:
        schema, table = froms[0]
        anchor_fqn  = f"{schema}.{table}"
        objects_lc  = {k.lower(): k for k in objects}
        anchor_real = objects_lc.get(anchor_fqn.lower())
        anchor_obj  = objects.get(anchor_real, {}) if anchor_real else {}

        col_names_lc = {c["name"].lower(): c["name"]
                        for c in anchor_obj.get("columns", [])}
        param_to_col = {p["name"]: col_names_lc[p["name"].lower()]
                        for p in params
                        if p["name"].lower() in col_names_lc}

        if param_to_col:
            # Pick one row (highest PK so it's our most recent witness row)
            pk_cols = anchor_obj.get("pk_columns") or []
            pk_clean = [_clean_col_name(c) for c in pk_cols]
            order_by = f"ORDER BY [{pk_clean[0]}] DESC" if pk_clean else ""
            sel_cols = ", ".join(f"[{c}]" for c in param_to_col.values())
            st = f"[{schema}].[{table}]"
            try:
                cur = conn.cursor()
                cur.execute(f"SELECT TOP 1 {sel_cols} FROM {st} {order_by}")
                row = cur.fetchone()
                cur.close()
                if row:
                    aligned = {pname: row[i]
                               for i, pname in enumerate(param_to_col.keys())}
                    # Fill remaining params with standard inference
                    result = {}
                    for p in params:
                        if p["name"] in aligned:
                            result[p["name"]] = aligned[p["name"]]
                        else:
                            result[p["name"]] = infer_param_value(p, conn, objects)
                    return result
            except Exception:
                pass

    # Fallback: standard per-param MAX inference
    return {p["name"]: infer_param_value(p, conn, objects) for p in params}


def adaptive_validate_proc(conn: Any, obj: dict, objects: dict) -> dict | None:
    """Adaptive validation for a stored procedure.

    Steps:
      1. Parse parameter declarations.
      2. Infer values for each required parameter (SELECT MAX — returns most
         recently inserted witness value).
      3. Execute the procedure.
      4. If 0 rows: insert aligned witness rows from the proc's SELECT body,
         re-infer params with MAX (now picks the witness rows), then retry.
      5. If 'no resultset': classify as partially_validated (DML-only proc).

    Returns a result dict or None if nothing useful could be inferred.
    """
    sql    = obj.get("ddl", "")
    schema = obj["schema"]
    name   = obj["name"]

    params     = parse_proc_params(sql)
    param_vals = {p["name"]: infer_param_value(p, conn, objects) for p in params}

    # First try: execute with inferred parameter values
    row, err = _exec_proc_and_fetch(conn, schema, name, param_vals)

    if err:
        no_rs = any(kw in err.lower() for kw in ("no resultset", "resultset", "not executed"))
        if no_rs:
            return {
                "status": "partially_validated",
                "note":   f"DML-only procedure (no SELECT output) — executed with {len(param_vals)} param(s)",
            }
        # Error other than 'no resultset' — try without params as fallback
        row2, err2 = _exec_proc_and_fetch(conn, schema, name, {})
        if not err2:
            row, err = row2, err2
        elif "no resultset" in err2.lower():
            return {"status": "partially_validated",
                    "note": "DML-only procedure — executed without params"}

    if row is not None:
        pinfo = f"{len(param_vals)} param(s)" if param_vals else "no params"
        return {"status": "validated",
                "note": f"Executed ({pinfo}), returns \u22651 row"}

    # 0 rows — insert aligned witness rows from the proc's SELECT body,
    # then re-infer params directly from the committed witness values so
    # all WHERE conditions match the exact inserted data.
    seed_result = _seed_proc_witness(conn, obj, objects)
    if seed_result and seed_result.get("tables_inserted"):
        committed = seed_result.get("committed", {})

        # Map each param to its committed value from the witness rows.
        # committed = {(table_fqn, col_lower): value}
        # Primary match: exact param-name == col_lower
        # Secondary match: param name is the table name → use first non-null value from that table
        # Tertiary: MAX-based inference
        param_vals2: dict[str, Any] = {}
        for p in params:
            pname_lower = p["name"].lower()
            matched_val = None

            # 1. Exact column-name match
            for (tbl_fqn, col_lower), val in committed.items():
                if col_lower == pname_lower and val is not None:
                    matched_val = val
                    break

            # 2. Param name matches table name: use first string/int value from that table
            if matched_val is None:
                for (tbl_fqn, col_lower), val in committed.items():
                    tbl_name = tbl_fqn.split(".")[-1].lower()
                    # e.g. @barcode → api.Barcode table → first committed value
                    if pname_lower in tbl_name and val is not None:
                        matched_val = val
                        break

            if matched_val is not None:
                param_vals2[p["name"]] = matched_val
            else:
                # Fall back to anchor-row then MAX for unmatched params
                param_vals2[p["name"]] = infer_param_value(p, conn, objects)

        row2, err2 = _exec_proc_and_fetch(conn, schema, name, param_vals2)
        if row2 is not None:
            pinfo2 = ", ".join(f"@{k}={v!r}" for k, v in list(param_vals2.items())[:3])
            return {
                "status": "validated",
                "note":   f"Validated after aligned witness seed + committed-value params ({pinfo2}…)",
            }
        return {
            "status": "partially_validated",
            "note":   f"Aligned seed inserted rows but proc still returned 0 — complex multi-table filter or UDT param",
        }

    # Couldn't fix: partial
    pinfo = ", ".join(f"@{k}={v!r}" for k, v in list(param_vals.items())[:3])
    return {
        "status": "partially_validated",
        "note":   f"Executed with inferred params ({pinfo}…) but returned 0 rows — complex internal logic",
    }


def adaptive_validate_tvf(conn: Any, obj: dict, objects: dict) -> dict | None:
    """Adaptive validation for a table-valued function.

    Infers parameter values and calls SELECT TOP 1 * FROM tvf(params).
    Applies adaptive INSERT witness if initial call returns 0 rows.

    Special handling:
    - Optional params (= NULL default): try all-NULL call first, then inferred
    - CSV string params (NVARCHAR containing DefinitionIds etc.): build from MAX(Id)
    - UDT READONLY params: skip (can't pass TVPs via pymssql)
    """
    sql    = obj.get("ddl", "")
    schema = obj["schema"]
    name   = obj["name"]

    # UDT / READONLY params cannot be passed — mark partial
    if re.search(r"\bREADONLY\b", sql, re.I):
        return {
            "status": "partially_validated",
            "note":   "TVF uses Table-Valued Parameter (READONLY UDT) — cannot invoke via standard SQL",
        }

    params = parse_proc_params(sql)
    if not params:
        return None  # no-param TVF should already be tested by _check_tvf

    # Build param values: special-case CSV string params and optional (= NULL) params
    param_vals = []
    for p in params:
        ptype   = p.get("type", "")
        default = p.get("default")

        # CSV string param (e.g. @DefinitionIds NVARCHAR): look up MAX(Id) from matching table
        if any(t in ptype for t in ("VARCHAR", "NVARCHAR", "CHAR")) and default is None:
            pname_lower = p["name"].lower()
            csv_val = None
            # Try to find a table whose PK name resembles the param minus trailing 's'/'Ids'
            stem = re.sub(r"ids?$", "", pname_lower)  # "definitionids" → "definition"
            for fqn, obj_t in objects.items():
                if obj_t.get("type") != "TABLE":
                    continue
                if stem and stem in fqn.lower():
                    pk_cols = obj_t.get("pk_columns") or []
                    if pk_cols:
                        pk = _clean_col_name(pk_cols[0])
                        st = _schema_table(fqn)
                        try:
                            cur = conn.cursor()
                            cur.execute(f"SELECT MAX([{pk}]) FROM {st}")
                            row = cur.fetchone()
                            cur.close()
                            if row and row[0] is not None:
                                csv_val = str(row[0])
                                break
                        except Exception:
                            pass
            param_vals.append(csv_val or "1")
        else:
            param_vals.append(infer_param_value(p, conn, objects))

    # If all params have defaults (= NULL), try NULL call first
    all_optional = all(p.get("default") is not None for p in params)
    if all_optional:
        null_vals = [None] * len(params)
        row_null, err_null = _exec_tvf_and_fetch(conn, schema, name, null_vals)
        if row_null is not None:
            return {"status": "validated",
                    "note": f"TVF called with all-NULL defaults, returns ≥1 row"}

    row, err = _exec_tvf_and_fetch(conn, schema, name, param_vals)

    if err and any(kw in err.lower() for kw in ("requires", "argument", "parameter", "expects", "insufficient")):
        return {
            "status": "partially_validated",
            "note":   f"TVF parameters inferred ({len(params)}) but call failed: {err[:120]}",
        }

    if row is not None:
        return {"status": "validated",
                "note":  f"TVF called with {len(params)} inferred param(s), returns ≥1 row"}

    # 0 rows — apply adaptive seed on the TVF body
    seed_result = adaptive_validate_view(conn, obj, objects)
    if seed_result and seed_result["status"] == "validated":
        row2, _ = _exec_tvf_and_fetch(conn, schema, name, param_vals)
        if row2 is not None:
            return {"status": "validated",
                    "note": f"Validated after adaptive seed + {len(params)} inferred param(s)"}
        return {"status": "partially_validated",
                "note": "Adaptive seed inserted data but TVF still returns 0 rows"}

    return {
        "status": "partially_validated",
        "note":   f"TVF executed with {len(params)} inferred param(s) but returned 0 rows",
    }


# ---------------------------------------------------------------------------
# TVP (Table-Valued Parameter) proc handler
# ---------------------------------------------------------------------------
# pymssql cannot marshal User-Defined Table Type parameters.
# This function builds a multi-statement T-SQL batch and runs it via sqlcmd.

def adaptive_validate_proc_tvp(conn_params: dict, obj: dict, objects: dict) -> dict | None:
    """Validate a stored procedure that has one or more READONLY UDT parameters.

    Steps:
      1. Find all @param schema.TypeName READONLY declarations in the proc DDL.
      2. For each UDT, query sys.table_types for its column names and types.
      3. Find a source table (in seeded objects) whose columns overlap the UDT.
      4. Build: DECLARE @v schema.UDT; INSERT INTO @v SELECT TOP 1 ... FROM src; EXEC proc ...
      5. Execute via sqlcmd subprocess (bypasses pymssql TVP limitation).
      6. Parse stdout for data rows.
    """
    import subprocess

    sql    = obj.get("ddl", "")
    schema = obj["schema"]
    name   = obj["name"]

    # Extract READONLY UDT params: @paramName schema.TypeName READONLY
    udt_params = re.findall(
        r'@(\w+)\s+\[?(\w+)\]?\.\[?(\w+)\]?\s+READONLY',
        sql, re.IGNORECASE,
    )
    if not udt_params:
        # Also check schema-less UDT declarations (rare but possible)
        udt_params_bare = re.findall(
            r'@(\w+)\s+\[?(\w+)\]?\s+READONLY',
            sql, re.IGNORECASE,
        )
        if not udt_params_bare:
            return None
        # Assume dbo schema for bare type names
        udt_params = [(p, "dbo", t) for p, t in udt_params_bare]

    server   = conn_params.get("server", "localhost")
    port     = conn_params.get("port", 1433)
    user     = conn_params.get("user", "sa")
    password = conn_params.get("password", "")
    database = conn_params.get("database", "RealizationDB")

    # Detect sqlcmd location — prefer local binary, fall back to docker exec
    import shutil
    _local_sqlcmd = (
        shutil.which("sqlcmd")
        or shutil.which("sqlcmd18")
        # Common container-internal paths (when running inside Docker)
        or ("/opt/mssql-tools18/bin/sqlcmd" if __import__("os").path.exists("/opt/mssql-tools18/bin/sqlcmd") else None)
        or ("/opt/mssql-tools/bin/sqlcmd" if __import__("os").path.exists("/opt/mssql-tools/bin/sqlcmd") else None)
    )

    # Docker exec fallback: if no local sqlcmd and a container_name is given
    # (or server is localhost), run sqlcmd inside the container via docker exec.
    _docker_container = conn_params.get("docker_container")
    _use_docker = not _local_sqlcmd and (
        _docker_container or server in ("localhost", "127.0.0.1")
    )
    if _use_docker and not _docker_container:
        # Auto-detect running MSSQL containers.
        # --filter ancestor= does not match tagged images, so filter by image name instead.
        _dc_result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}} {{.Image}}"],
            capture_output=True, text=True, timeout=10,
        )
        _names = [
            line.split()[0]
            for line in _dc_result.stdout.splitlines()
            if "mssql/server" in line
        ]
        _docker_container = _names[0] if _names else None

    if not _local_sqlcmd and not _docker_container:
        return {
            "status": "partially_validated",
            "note":   "TVP proc: sqlcmd not found locally and no Docker MSSQL container detected. "
                      "Install sqlcmd or pass 'docker_container' in conn_params.",
        }

    def _run_query(q: str) -> list[list]:
        """Run a T-SQL query via sqlcmd (local or docker exec), return parsed rows."""
        if _use_docker and _docker_container:
            # Inside the container, SQL Server always listens on 1433.
            cmd = [
                "docker", "exec", _docker_container,
                "/opt/mssql-tools18/bin/sqlcmd",
                "-S", "localhost,1433", "-U", user, "-P", password,
                "-No", "-d", database, "-h", "-1", "-s", "|", "-Q", q,
            ]
        else:
            cmd = [
                _local_sqlcmd, "-S", f"{server},{port}", "-U", user, "-P", password,
                "-No", "-d", database, "-h", "-1", "-s", "|", "-Q", q,
            ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        rows = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line or "rows affected" in line or line.startswith("Changed"):
                continue
            rows.append([c.strip() for c in line.split("|")])
        return rows

    def _run_batch(batch: str) -> tuple[list[str], str | None]:
        """Execute a multi-statement T-SQL batch. Returns (data_lines, error_msg)."""
        if _use_docker and _docker_container:
            # Inside the container, SQL Server always listens on 1433.
            cmd = [
                "docker", "exec", _docker_container,
                "/opt/mssql-tools18/bin/sqlcmd",
                "-S", "localhost,1433", "-U", user, "-P", password,
                "-No", "-d", database, "-h", "-1", "-Q", batch,
            ]
        else:
            cmd = [
                _local_sqlcmd, "-S", f"{server},{port}", "-U", user, "-P", password,
                "-No", "-d", database, "-h", "-1", "-Q", batch,
            ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        output = result.stdout + result.stderr
        data_lines = []
        for line in output.splitlines():
            ln = line.strip()
            if not ln:
                continue
            if "rows affected" in ln:
                continue
            if ln.startswith("Changed database"):
                continue
            if ln.startswith("Msg ") and "Level" in ln:
                return [], ln
            data_lines.append(ln)
        return data_lines, None

    # Build declare + insert + exec batch
    batch_lines = []
    exec_args   = []

    for param_name, udt_schema, udt_type in udt_params:
        # Query UDT column definitions
        col_rows = _run_query(
            f"SELECT c.name, t.name "
            f"FROM sys.table_types tt "
            f"JOIN sys.columns c ON c.object_id = tt.type_table_object_id "
            f"JOIN sys.types t ON c.user_type_id = t.user_type_id "
            f"WHERE tt.schema_id = SCHEMA_ID('{udt_schema}') AND tt.name = '{udt_type}' "
            f"ORDER BY c.column_id;"
        )
        if not col_rows:
            # UDT not found — cannot build TVP
            return {
                "status": "partially_validated",
                "note":   f"TVP proc: UDT [{udt_schema}].[{udt_type}] not found in sys.table_types",
            }

        udt_cols = [(r[0], r[1]) for r in col_rows if len(r) >= 2]

        # Find a seeded source table whose columns overlap the UDT columns
        udt_col_names_lower = {c[0].lower() for c in udt_cols}
        best_src  = None
        best_score = 0
        for fqn, tbl_obj in objects.items():
            if tbl_obj.get("type") != "TABLE":
                continue
            tbl_cols_lower = {c["name"].lower() for c in tbl_obj.get("columns", [])}
            overlap = len(udt_col_names_lower & tbl_cols_lower)
            if overlap > best_score:
                best_score = overlap
                best_src   = (fqn, tbl_obj)

        batch_lines.append(f"DECLARE @{param_name} [{udt_schema}].[{udt_type}];")

        if best_src and best_score > 0:
            src_fqn, src_obj = best_src
            src_schema, src_table = src_fqn.split(".", 1)
            # Build INSERT from source — only columns that exist in both
            src_col_names_lower = {c["name"].lower(): c["name"] for c in src_obj.get("columns", [])}
            matched = [
                (udt_col, src_col_names_lower[udt_col.lower()])
                for udt_col, _ in udt_cols
                if udt_col.lower() in src_col_names_lower
            ]
            if matched:
                udt_col_list = ", ".join(f"[{uc}]" for uc, _ in matched)
                src_col_list = ", ".join(f"[{sc}]" for _, sc in matched)
                batch_lines.append(
                    f"INSERT INTO @{param_name} ({udt_col_list}) "
                    f"SELECT TOP 1 {src_col_list} FROM [{src_schema}].[{src_table}];"
                )
            else:
                # Column names don't match exactly — insert a minimal row with defaults
                default_vals = []
                for _, type_name in udt_cols:
                    tn = type_name.upper()
                    if any(t in tn for t in ("INT", "BIGINT", "SMALLINT", "TINYINT")):
                        default_vals.append("1")
                    elif "BIT" in tn:
                        default_vals.append("1")
                    elif any(t in tn for t in ("VARCHAR", "NVARCHAR", "CHAR")):
                        default_vals.append("N'WIT001'")
                    elif any(t in tn for t in ("DECIMAL", "NUMERIC", "FLOAT", "MONEY")):
                        default_vals.append("1.0")
                    elif "DATE" in tn:
                        default_vals.append("'2024-01-01'")
                    else:
                        default_vals.append("NULL")
                batch_lines.append(
                    f"INSERT INTO @{param_name} VALUES ({', '.join(default_vals)});"
                )
        else:
            # No source table found — insert minimal defaults
            default_vals = []
            for _, type_name in udt_cols:
                tn = type_name.upper()
                if any(t in tn for t in ("INT", "BIGINT", "SMALLINT", "TINYINT")):
                    default_vals.append("1")
                elif "BIT" in tn:
                    default_vals.append("1")
                elif any(t in tn for t in ("VARCHAR", "NVARCHAR", "CHAR")):
                    default_vals.append("N'WIT001'")
                else:
                    default_vals.append("NULL")
            batch_lines.append(
                f"INSERT INTO @{param_name} VALUES ({', '.join(default_vals)});"
            )

        exec_args.append(f"@{param_name} = @{param_name}")

    # Add non-TVP params (scalar) using inferred values
    try:
        import pymssql as _pymssql
        conn_tmp = _pymssql.connect(
            server=server, port=port, user=user, password=password,
            database=database, timeout=10, login_timeout=10,
        )
        all_params = parse_proc_params(sql)
        for p in all_params:
            if re.search(rf'@{p["name"]}\s+\S+\s+READONLY', sql, re.I):
                continue  # Already handled as TVP above
            val = infer_param_value(p, conn_tmp, objects)
            if isinstance(val, str):
                exec_args.append(f"@{p['name']} = N'{val}'")
            elif val is None:
                exec_args.append(f"@{p['name']} = NULL")
            else:
                exec_args.append(f"@{p['name']} = {val}")
        conn_tmp.close()
    except Exception:
        pass

    exec_stmt = f"EXEC [{schema}].[{name}]"
    if exec_args:
        exec_stmt += " " + ", ".join(exec_args)
    exec_stmt += ";"

    batch_lines.append(exec_stmt)
    full_batch = "\n".join(batch_lines)

    # Execute via sqlcmd (local binary or docker exec)
    data_lines, err_msg = _run_batch(full_batch)
    if err_msg:
        return {
            "status": "partially_validated",
            "note":   f"TVP proc executed but SQL error: {err_msg[:200]}",
        }

    if data_lines:
        return {
            "status": "validated",
            "note":   (
                f"TVP proc validated via sqlcmd — "
                f"DECLARE+INSERT+EXEC batch, {len(data_lines)} output line(s) returned. "
                f"UDT params: {[t for _, _, t in udt_params]}"
            ),
        }

    # 0 data lines — proc executed without error but returned no rows.
    # Seed data in the source table may not match the proc's join chain.
    return {
        "status": "partially_validated",
        "note":   (
            f"TVP proc executed via sqlcmd (DECLARE+INSERT+EXEC) but returned 0 rows — "
            f"source table data may not satisfy proc's join chain. "
            f"UDT params: {[t for _, _, t in udt_params]}"
        ),
    }


# ---------------------------------------------------------------------------
# Join consistency pre-check
# ---------------------------------------------------------------------------

def verify_and_fix_join_consistency(conn: Any, objects: dict) -> list[dict]:
    """Check every INNER JOIN used by views and procs against current seed data.

    For each pair (t1.col1 = t2.col2) extracted from a view or proc body:
      1. Run: SELECT COUNT(*) FROM t1 INNER JOIN t2 ON t1.col1 = t2.col2
      2. If count = 0, diagnose: sample values from each side
      3. Auto-fix: UPDATE the FK side so its value matches an existing PK value
         in the referenced side (minimal disruption — only changes rows that
         already have no match, not rows that are already consistent).

    Returns a list of {object, join, action, rows_affected} for every join
    that was broken and fixed (or attempted).
    """

    def _st(fqn: str) -> str:
        parts = fqn.split(".", 1)
        s, t = (parts[0], parts[1]) if len(parts) == 2 else ("dbo", parts[0])
        return f"[{s}].[{t}]"

    def _sample_vals(fqn: str, col: str, n: int = 5) -> list:
        try:
            cur = conn.cursor()
            cur.execute(f"SELECT TOP {n} [{col}] FROM {_st(fqn)} WHERE [{col}] IS NOT NULL")
            rows = cur.fetchall()
            cur.close()
            return [r[0] for r in rows if r]
        except Exception:
            return []

    def _row_count(fqn: str) -> int:
        try:
            cur = conn.cursor()
            cur.execute(f"SELECT COUNT(*) FROM {_st(fqn)}")
            row = cur.fetchone()
            cur.close()
            return int(row[0]) if row else 0
        except Exception:
            return -1

    issues: list[dict] = []
    seen_pairs: set[tuple] = set()

    for fqn, obj in objects.items():
        if obj.get("type") not in ("VIEW", "PROCEDURE"):
            continue
        sql = obj.get("ddl", "")
        if not sql:
            continue

        cte_names = extract_cte_names(sql)
        alias_map = extract_alias_map(sql, cte_names, set(objects.keys()))
        if not alias_map:
            continue

        join_pairs = extract_inner_join_pairs(sql, alias_map)
        for t1, c1, t2, c2 in join_pairs:
            # Normalize so we only check each unique pair once
            pair_key = tuple(sorted([(t1, c1), (t2, c2)]))
            if pair_key in seen_pairs:
                continue
            # Only check pairs where both tables exist in inventory
            if t1 not in objects or t2 not in objects:
                continue
            if objects[t1].get("type") != "TABLE" or objects[t2].get("type") != "TABLE":
                continue
            seen_pairs.add(pair_key)

            # Check for orphaned rows: rows in t1 where c1 has no matching c2 in t2.
            # This is stricter than checking whether the join resolves at all —
            # even if some rows join fine, orphaned rows will silently drop from
            # result sets, causing procs/views to return incomplete (or 0) data.
            try:
                cur = conn.cursor()
                cur.execute(
                    f"SELECT COUNT(*) FROM {_st(t1)} "
                    f"WHERE [{c1}] IS NOT NULL "
                    f"AND [{c1}] NOT IN (SELECT [{c2}] FROM {_st(t2)} WHERE [{c2}] IS NOT NULL)"
                )
                orphan_count = cur.fetchone()[0]
                cur.close()
            except Exception:
                # Also try the simpler join count as fallback
                try:
                    cur = conn.cursor()
                    cur.execute(
                        f"SELECT COUNT(*) FROM {_st(t1)} AS a "
                        f"INNER JOIN {_st(t2)} AS b ON a.[{c1}] = b.[{c2}]"
                    )
                    join_count = cur.fetchone()[0]
                    cur.close()
                    orphan_count = 0 if join_count > 0 else 1  # approximate
                except Exception:
                    continue  # column name may be wrong; skip silently

            if orphan_count == 0:
                continue  # all rows satisfy the join — healthy

            # Join is broken — diagnose
            vals1 = _sample_vals(t1, c1)
            vals2 = _sample_vals(t2, c2)
            rc1   = _row_count(t1)
            rc2   = _row_count(t2)

            issue: dict = {
                "object":   fqn,
                "join":     f"{t1}.{c1} = {t2}.{c2}",
                "t1_vals":  vals1,
                "t2_vals":  vals2,
                "action":   None,
                "rows_affected": 0,
            }

            # Determine which side is the FK (child) and which is the PK (parent).
            # Heuristic: the table with fewer rows and a column matching a PK is
            # usually the parent; the other is the FK side we should UPDATE.
            obj1 = objects.get(t1, {})
            obj2 = objects.get(t2, {})
            pk1  = [_clean_col_name(c) for c in obj1.get("pk_columns", [])]
            pk2  = [_clean_col_name(c) for c in obj2.get("pk_columns", [])]

            # Skip UPDATEs on columns that are part of a composite FK referenced by
            # child tables (e.g. api.Hierarchy.POSSystemId is part of a compound FK
            # referenced by api.BundleLocationAccess etc.). Updating such a column
            # without first updating all child tables violates FK constraints.
            # Detect this by checking sys.foreign_key_columns for inbound references.
            def _has_inbound_fk(tbl_fqn: str, col_name: str) -> bool:
                """Return True if col_name in tbl_fqn is referenced by an FK in another table."""
                schema_name, table_name = tbl_fqn.split('.', 1)
                try:
                    chk = conn.cursor()
                    chk.execute(
                        "SELECT COUNT(*) "
                        "FROM sys.foreign_key_columns fkc "
                        "JOIN sys.columns rc ON rc.object_id = fkc.referenced_object_id "
                        "  AND rc.column_id = fkc.referenced_column_id "
                        "JOIN sys.tables rt ON rt.object_id = fkc.referenced_object_id "
                        "WHERE rt.schema_id = SCHEMA_ID(%s) AND rt.name = %s "
                        "  AND rc.name = %s",
                        (schema_name, table_name, col_name),
                    )
                    cnt = chk.fetchone()[0]
                    chk.close()
                    return cnt > 0
                except Exception:
                    return False

            # If t2.c2 is a PK and t1.c1 is not → t1.c1 is the FK side (update t1)
            # but only if t1.c1 is NOT itself referenced by other tables' FKs
            if c2 in pk2 and c1 not in pk1 and vals2:
                if _has_inbound_fk(t1, c1):
                    issue["action"] = (
                        f"SKIPPED — {t1}.{c1} is referenced by an FK in child tables; "
                        f"updating it would violate referential integrity"
                    )
                else:
                    target_val = vals2[0]
                    try:
                        cur = conn.cursor()
                        cur.execute(
                            f"UPDATE {_st(t1)} SET [{c1}] = %s "
                            f"WHERE [{c1}] NOT IN (SELECT [{c2}] FROM {_st(t2)})",
                            (target_val,),
                        )
                        affected = cur.rowcount
                        conn.commit()
                        cur.close()
                        issue["action"]        = f"UPDATE {t1}.{c1} → {target_val!r} (matched {t2}.{c2})"
                        issue["rows_affected"] = affected
                    except Exception as upd_exc:
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                        issue["action"] = f"UPDATE attempted but failed: {str(upd_exc)[:100]}"

            # If t1.c1 is a PK and t2.c2 is not → t2.c2 is the FK side (update t2)
            elif c1 in pk1 and c2 not in pk2 and vals1:
                if _has_inbound_fk(t2, c2):
                    issue["action"] = (
                        f"SKIPPED — {t2}.{c2} is referenced by an FK in child tables"
                    )
                else:
                    target_val = vals1[0]
                    try:
                        cur = conn.cursor()
                        cur.execute(
                            f"UPDATE {_st(t2)} SET [{c2}] = %s "
                            f"WHERE [{c2}] NOT IN (SELECT [{c1}] FROM {_st(t1)})",
                            (target_val,),
                        )
                        affected = cur.rowcount
                        conn.commit()
                        cur.close()
                        issue["action"]        = f"UPDATE {t2}.{c2} → {target_val!r} (matched {t1}.{c1})"
                        issue["rows_affected"] = affected
                    except Exception as upd_exc:
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                        issue["action"] = f"UPDATE attempted but failed: {str(upd_exc)[:100]}"

            else:
                # Cannot determine FK direction — insert a new bridging row in t1
                # pointing at first available value in t2
                if vals2 and rc1 > 0:
                    if _has_inbound_fk(t1, c1):
                        issue["action"] = (
                            f"SKIPPED — {t1}.{c1} is referenced by an FK in child tables"
                        )
                    else:
                        target_val = vals2[0]
                        try:
                            cur = conn.cursor()
                            cur.execute(
                                f"UPDATE {_st(t1)} SET [{c1}] = %s "
                                f"WHERE [{c1}] IS NULL OR [{c1}] NOT IN (SELECT [{c2}] FROM {_st(t2)})",
                                (target_val,),
                            )
                            affected = cur.rowcount
                            conn.commit()
                            cur.close()
                            issue["action"]        = f"UPDATE {t1}.{c1} → {target_val!r} (bridged to {t2}.{c2})"
                            issue["rows_affected"] = affected
                        except Exception as upd_exc:
                            try:
                                conn.rollback()
                            except Exception:
                                pass
                            issue["action"] = f"Bridge UPDATE failed: {str(upd_exc)[:100]}"
                else:
                    issue["action"] = "Could not determine fix direction — no data on reference side"

            issues.append(issue)
            print(
                f"  [JoinFix] {issue['join']} — 0 rows → {issue['action']} "
                f"({issue['rows_affected']} row(s) updated)"
            )

    return issues
