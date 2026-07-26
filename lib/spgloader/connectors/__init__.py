"""Connector factory and public interface for spgloader connectors."""
from .base import Connector, make_object, parse_ddl_file, _extract_deps_from_sql
from .mssql import MSSQLConnector
from .mysql import MySQLConnector
from .oracle import OracleConnector


DEFAULT_PORTS = {"mssql": 1433, "mysql": 3306, "mariadb": 3306, "oracle": 1521}


def get_connector(
    source_type: str,
    host: str = "localhost",
    port: int | None = None,
    database: str = "",
    user: str = "",
    password: str = "",
) -> Connector:
    """Return the correct Connector subclass for the given source type."""
    resolved_port = port or DEFAULT_PORTS.get(source_type, 5432)
    match source_type:
        case "mssql":
            return MSSQLConnector(host, resolved_port, database, user, password)
        case "mysql" | "mariadb":
            # MariaDB is protocol-compatible with MySQL connector
            return MySQLConnector(host, resolved_port, database, user, password)
        case "oracle":
            return OracleConnector(host, resolved_port, database, user, password)
        case _:
            raise ValueError(
                f"Unsupported source_type: {source_type!r}. "
                f"Choose: mssql, mysql, mariadb, oracle"
            )


__all__ = [
    "Connector", "get_connector", "make_object", "parse_ddl_file",
    "DEFAULT_PORTS", "MSSQLConnector", "MySQLConnector", "OracleConnector",
]
