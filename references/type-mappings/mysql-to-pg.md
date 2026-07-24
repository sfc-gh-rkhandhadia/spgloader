# MySQL → Snowflake Postgres Type Mapping

Use this reference when performing LLM-based conversion of MySQL objects (views, procedures, functions, triggers).

## Data Types

| MySQL Type | PostgreSQL Type | Notes |
|------------|----------------|-------|
| `TINYINT` | `SMALLINT` | |
| `TINYINT(1)` | `BOOLEAN` | MySQL convention for boolean |
| `SMALLINT` | `SMALLINT` | |
| `MEDIUMINT` | `INTEGER` | No exact PG equivalent |
| `INT` / `INTEGER` | `INTEGER` | |
| `BIGINT` | `BIGINT` | |
| `BIT(1)` | `BOOLEAN` | |
| `BIT(n)` | `BIT(n)` | |
| `FLOAT` | `REAL` | |
| `DOUBLE` | `DOUBLE PRECISION` | |
| `DECIMAL(p,s)` | `NUMERIC(p,s)` | |
| `NUMERIC(p,s)` | `NUMERIC(p,s)` | |
| `CHAR(n)` | `CHAR(n)` | |
| `VARCHAR(n)` | `VARCHAR(n)` | |
| `TINYTEXT` | `TEXT` | |
| `TEXT` | `TEXT` | |
| `MEDIUMTEXT` | `TEXT` | |
| `LONGTEXT` | `TEXT` | |
| `BINARY(n)` | `BYTEA` | |
| `VARBINARY(n)` | `BYTEA` | |
| `TINYBLOB` | `BYTEA` | |
| `BLOB` | `BYTEA` | |
| `MEDIUMBLOB` | `BYTEA` | |
| `LONGBLOB` | `BYTEA` | |
| `DATE` | `DATE` | |
| `TIME` | `TIME` | |
| `DATETIME` | `TIMESTAMP` | No timezone in MySQL DATETIME |
| `TIMESTAMP` | `TIMESTAMPTZ` | MySQL TIMESTAMP is UTC-stored |
| `YEAR` | `INTEGER` | |
| `ENUM(...)` | `TEXT` | Add CHECK constraint: `CHECK (col IN ('val1','val2'))` |
| `SET(...)` | `TEXT[]` or `TEXT` | Use TEXT[] for multi-value, TEXT for simple |
| `JSON` | `JSONB` | |
| `GEOMETRY` | `TEXT` | PostGIS needed for spatial |

## Auto-Increment

| MySQL | PostgreSQL |
|-------|-----------|
| `INT AUTO_INCREMENT` | `SERIAL` or `INTEGER GENERATED ALWAYS AS IDENTITY` |
| `BIGINT AUTO_INCREMENT` | `BIGSERIAL` or `BIGINT GENERATED ALWAYS AS IDENTITY` |

## SQL Functions

| MySQL Function | PostgreSQL Equivalent |
|---------------|----------------------|
| `NOW()` | `NOW()` — same |
| `CURDATE()` | `CURRENT_DATE` |
| `CURTIME()` | `CURRENT_TIME` |
| `UNIX_TIMESTAMP()` | `EXTRACT(epoch FROM NOW())::BIGINT` |
| `FROM_UNIXTIME(n)` | `TO_TIMESTAMP(n)` |
| `DATE_FORMAT(d, fmt)` | `TO_CHAR(d, fmt)` — format codes differ |
| `STR_TO_DATE(s, fmt)` | `TO_DATE(s, fmt)` |
| `DATEDIFF(d1, d2)` | `(d1::date - d2::date)` |
| `DATE_ADD(d, INTERVAL n unit)` | `d + (n * INTERVAL '1 unit')` |
| `DATE_SUB(d, INTERVAL n unit)` | `d - (n * INTERVAL '1 unit')` |
| `IFNULL(a, b)` | `COALESCE(a, b)` |
| `IF(cond, a, b)` | `CASE WHEN cond THEN a ELSE b END` |
| `GREATEST(a, b)` | `GREATEST(a, b)` — same |
| `LEAST(a, b)` | `LEAST(a, b)` — same |
| `GROUP_CONCAT(col)` | `STRING_AGG(col, ',')` |
| `CONCAT(a, b)` | `CONCAT(a, b)` or `a || b` |
| `SUBSTR(s, pos, len)` | `SUBSTRING(s FROM pos FOR len)` |
| `INSTR(str, sub)` | `POSITION(sub IN str)` |
| `LENGTH(s)` | `LENGTH(s)` — same (byte length in MySQL for multibyte) |
| `CHAR_LENGTH(s)` | `LENGTH(s)` — character length |
| `LPAD(s, n, fill)` | `LPAD(s, n, fill)` — same |
| `RPAD(s, n, fill)` | `RPAD(s, n, fill)` — same |
| `TRUNCATE(n, d)` | `TRUNC(n, d)` |
| `RAND()` | `RANDOM()` |
| `ROUND(n, d)` | `ROUND(n, d)` — same |
| `FLOOR(n)` | `FLOOR(n)` — same |
| `CEIL(n)` | `CEIL(n)` — same |
| `POW(a, b)` | `POWER(a, b)` |
| `LOG(n)` | `LN(n)` (natural log); `LOG(n)` = base 10 in PG |
| `UUID()` | `gen_random_uuid()` |
| `LAST_INSERT_ID()` | `lastval()` or use `RETURNING id` |
| `ROW_COUNT()` | `GET DIAGNOSTICS n = ROW_COUNT` (PL/pgSQL) |
| `FOUND_ROWS()` | No equivalent — use `COUNT(*)` over same query |

## Identifier Quoting

MySQL uses backticks: `` `table_name` `` → PostgreSQL uses double quotes: `"table_name"`.
If identifiers are lowercase and contain no reserved words, quotes can be omitted.

## Control Flow (MySQL → PL/pgSQL)

| MySQL | PL/pgSQL |
|-------|---------|
| `DECLARE var TYPE DEFAULT value;` | `DECLARE var TYPE := value;` |
| `SET var = value;` | `var := value;` |
| `IF condition THEN ... END IF;` | Same |
| `IF ... THEN ... ELSEIF ... THEN ... ELSE ... END IF;` | `IF ... THEN ... ELSIF ... THEN ... ELSE ... END IF;` |
| `WHILE condition DO ... END WHILE;` | `WHILE condition LOOP ... END LOOP;` |
| `REPEAT ... UNTIL condition END REPEAT;` | `LOOP ... EXIT WHEN condition; END LOOP;` |
| `LOOP ... END LOOP;` with `LEAVE label` | `LOOP ... EXIT; END LOOP;` |
| `ITERATE label` | `CONTINUE;` |
| `CALL proc()` | `CALL proc()` or `PERFORM proc()` |
| `SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'msg'` | `RAISE EXCEPTION 'msg';` |
| `RESIGNAL` | `RAISE;` |
| `DECLARE CONTINUE HANDLER FOR SQLEXCEPTION ...` | `EXCEPTION WHEN OTHERS THEN ...` |
| `SELECT col INTO var FROM ...` | Same — `SELECT col INTO var FROM ...` |

## Procedure Template

```sql
-- MySQL stored procedure
CREATE PROCEDURE get_orders(IN p_status VARCHAR(50))
BEGIN
    SELECT * FROM orders WHERE status = p_status;
END

-- PostgreSQL equivalent
CREATE OR REPLACE FUNCTION get_orders(p_status TEXT)
RETURNS TABLE(id INTEGER, status TEXT, total NUMERIC) AS $$
BEGIN
    RETURN QUERY
    SELECT o.id, o.status, o.total
    FROM orders o
    WHERE o.status = p_status;
END;
$$ LANGUAGE plpgsql;
```

## Trigger Template

```sql
-- MySQL trigger
CREATE TRIGGER orders_after_insert
AFTER INSERT ON orders
FOR EACH ROW
BEGIN
    INSERT INTO audit_log (action, order_id) VALUES ('INSERT', NEW.id);
END

-- PostgreSQL equivalent (function + trigger)
CREATE OR REPLACE FUNCTION orders_after_insert_fn()
RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO audit_log (action, order_id) VALUES ('INSERT', NEW.id);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER orders_after_insert
AFTER INSERT ON orders
FOR EACH ROW
EXECUTE FUNCTION orders_after_insert_fn();
```

## Notes

- MySQL `CHARACTER SET utf8mb4` — not needed in PostgreSQL (all text is Unicode)
- MySQL `ENGINE=InnoDB` / `ENGINE=MyISAM` — remove; not applicable to PostgreSQL
- MySQL `COLLATE utf8mb4_unicode_ci` — remove; PostgreSQL collation is set at DB level
- `AUTO_INCREMENT` table option (e.g., `AUTO_INCREMENT=1001`) — not needed; handled by sequence reset
- `ROW_FORMAT=COMPRESSED` — remove
- `ON UPDATE CURRENT_TIMESTAMP` column option — implement as a trigger in PostgreSQL
- Backtick-quoted reserved words: review and replace with `"double_quotes"` or rename if possible
