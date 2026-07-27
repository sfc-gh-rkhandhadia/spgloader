#!/usr/bin/env python3
"""
check_secrets.py — Pre-commit guardrail for spgloader.

Scans staged (or all specified) files for:
  - Hardcoded credentials (passwords, tokens, keys)
  - Server / host / endpoint information
  - Client / company names
  - Snowflake account locators and usernames
  - Database connection strings

Exits 1 (blocking the commit) if any violation is found.

Usage (called automatically by pre-commit):
  python check_secrets.py [file1 file2 ...]

Usage (manual full-repo scan):
  python check_secrets.py --all
"""
import argparse
import re
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Sensitive patterns to block
# ---------------------------------------------------------------------------

# Each entry: (rule_name, compiled_regex, description)
CREDENTIAL_PATTERNS = [
    # Passwords / secrets assigned to a real value (not a placeholder or env-var reference).
    # Excluded from detection:
    #   "<placeholder>"   — angle bracket placeholders in docs
    #   "${ENV_VAR}"      — shell / Docker Compose env-var substitution
    #   "{template_var}"  — Python f-string / template variables
    #   "your_..._here"   — common example placeholder text
    (
        "hardcoded_password",
        re.compile(
            r"""(?:password|passwd|pwd|secret|token|apikey|api_key)\s*[=:]\s*['"]"""
            r"""(?!\s*(?:<[^>]+>|\$\{[^}]+\}|\{[^}]+\}|your[_\-][^'"]+|changeme|placeholder|example|none|null|\s*)['""])"""
            r"""[^'"<>\$\{]{4,}['"]""",
            re.IGNORECASE,
        ),
        "Hardcoded password or secret — use an environment variable instead",
    ),
    # Connection strings with real credentials — skip if the URL contains {template} placeholders
    (
        "connection_string_with_password",
        re.compile(
            r"""(?:postgresql|postgres|mssql|mysql|oracle)://(?![^@]*\{)[^@\s]+:[^@\s]+@""",
            re.IGNORECASE,
        ),
        "Connection string with embedded credentials",
    ),
    # Snowflake PAT / JWT tokens (eyJ...)
    (
        "jwt_token",
        re.compile(r"""['"](eyJ[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+)['"]"""),
        "Embedded JWT / PAT token",
    ),
    # Long hex secrets (32+ hex chars not in a comment or hash)
    (
        "hex_secret",
        re.compile(r"""(?<![#\w])['"]\s*[0-9a-fA-F]{32,}\s*['"]"""),
        "Possible hex secret or hash literal",
    ),
    # AWS-style access keys
    (
        "aws_access_key",
        re.compile(r"""\b(AKIA|ASIA|AROA)[A-Z0-9]{16}\b"""),
        "AWS access key ID",
    ),
    # Snowflake account locator patterns (e.g. ab12345.us-east-1 or ORGNAME-ACCOUNTNAME)
    (
        "snowflake_account_locator",
        re.compile(
            r"""['"]\s*(?:[a-z]{2,10}\d{5,8}\.[a-z0-9\-]+\.snowflakecomputing\.com|[A-Z0-9_]+-[A-Z0-9_]+\.snowflakecomputing\.com)\s*['"]""",
            re.IGNORECASE,
        ),
        "Snowflake account URL or locator",
    ),
    # SPG hostnames (*.postgres.snowflake.app)
    (
        "spg_hostname",
        re.compile(r"""['"]\s*[a-z0-9]+\.[a-z0-9\-]+\.aws\.postgres\.snowflake\.app\s*['"]""", re.IGNORECASE),
        "Snowflake Postgres (SPG) endpoint hostname",
    ),
    # IP addresses in string literals (skip 0.0.0.0, 127.0.0.1, localhost)
    (
        "ip_address",
        re.compile(
            r"""['"]\s*(?!(?:0\.0\.0\.0|127\.0\.0\.1|localhost|0\.0\.0))\b(\d{1,3}\.){3}\d{1,3}\b\s*['"]"""
        ),
        "IP address in string literal (use a variable or env var instead)",
    ),
    # Snowflake account names in connection_name or account= fields
    (
        "snowflake_account_name",
        re.compile(
            r"""(?:account|connection_name)\s*[=:]\s*['"]\s*[A-Z0-9_]+-[A-Z0-9_]+(?:-[A-Z0-9_]+)?\s*['"]""",
            re.IGNORECASE,
        ),
        "Snowflake account name or connection name hardcoded",
    ),
    # Database names that look like client-specific identifiers
    # Heuristic: database names with >2 words or company-style names in --source-db or DATABASE= context
    (
        "client_database_name",
        re.compile(
            r"""--(?:source-db|database)\s+['""]?([a-z][a-z0-9_]{3,}(?:_[a-z0-9]{2,}){2,})['""]?""",
            re.IGNORECASE,
        ),
        "Possible client-specific database name in CLI argument (use a placeholder)",
    ),
]

# Patterns in comments/strings that indicate real usernames / real person names
# These are softer checks — flagged as warnings, not hard blocks
PERSONAL_INFO_PATTERNS = [
    (
        "email_address",
        re.compile(r"""['"]\s*[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\s*['"]"""),
        "Email address literal",
    ),
    (
        "personal_username",
        re.compile(
            r"""['"]\s*(?:SNOWFLAKE_USER|my_username|example_user)\s*['"]""",
            re.IGNORECASE,
        ),
        "Personal Snowflake username or account name",
    ),
]

# ---------------------------------------------------------------------------
# File extension allowlist (only scan text files)
# ---------------------------------------------------------------------------
SCAN_EXTENSIONS = {
    ".py", ".yaml", ".yml", ".toml", ".md", ".txt", ".json",
    ".sh", ".bash", ".env", ".cfg", ".conf", ".ini", ".sql",
}

# Files / patterns to always skip
SKIP_PATHS = {
    "uv.lock", "poetry.lock", "package-lock.json",
    ".git", "__pycache__", ".venv", "node_modules",
}


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

def _should_skip(path: Path) -> bool:
    parts = set(path.parts)
    if parts & SKIP_PATHS:
        return True
    if path.suffix.lower() not in SCAN_EXTENSIONS and path.suffix != "":
        return True
    return False


def scan_file(path: Path) -> list[dict]:
    """Return list of violations found in *path*."""
    if _should_skip(path):
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    violations = []
    lines = text.splitlines()

    for lineno, line in enumerate(lines, 1):
        # Skip pure comment lines (starts with # or --)
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("--") or stripped.startswith("//"):
            continue

        for rule_name, pattern, description in CREDENTIAL_PATTERNS:
            m = pattern.search(line)
            if m:
                violations.append({
                    "file": str(path),
                    "line": lineno,
                    "rule": rule_name,
                    "description": description,
                    "snippet": line.strip()[:120],
                    "severity": "ERROR",
                })

        for rule_name, pattern, description in PERSONAL_INFO_PATTERNS:
            m = pattern.search(line)
            if m:
                violations.append({
                    "file": str(path),
                    "line": lineno,
                    "rule": rule_name,
                    "description": description,
                    "snippet": line.strip()[:120],
                    "severity": "ERROR",
                })

    return violations


def scan_files(paths: list[Path]) -> list[dict]:
    all_violations = []
    for p in paths:
        if p.is_file():
            all_violations.extend(scan_file(p))
        elif p.is_dir():
            for f in p.rglob("*"):
                if f.is_file():
                    all_violations.extend(scan_file(f))
    return all_violations


def print_report(violations: list[dict]) -> None:
    errors = [v for v in violations if v["severity"] == "ERROR"]
    warnings = [v for v in violations if v["severity"] == "WARNING"]

    if errors:
        print("\n\033[91m✖  CREDENTIAL / SENSITIVE DATA CHECK FAILED\033[0m")
        print("=" * 70)
        print("The following violations must be fixed before committing:\n")
        for v in errors:
            print(f"  \033[91m[{v['rule']}]\033[0m {v['file']}:{v['line']}")
            print(f"    {v['description']}")
            print(f"    → {v['snippet']}")
            print()
        print("=" * 70)
        print("\nFix: Replace literals with environment variables or config keys.")
        print("     e.g.  password = os.environ['MY_PASSWORD']")
        print("     e.g.  account  = config.get('snowflake_account')")
        print("\nTo temporarily skip (NOT recommended):")
        print("     git commit --no-verify")
        print()

    if warnings:
        print(f"\n\033[93m⚠  {len(warnings)} warning(s):\033[0m")
        for v in warnings:
            print(f"  [{v['rule']}] {v['file']}:{v['line']} — {v['description']}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scan files for hardcoded credentials and sensitive data"
    )
    parser.add_argument("files", nargs="*", help="Files to scan (from pre-commit)")
    parser.add_argument("--all", action="store_true",
                        help="Scan entire repository instead of staged files")
    args = parser.parse_args()

    if args.all:
        root = Path(__file__).parent.parent
        targets = [root]
    elif args.files:
        targets = [Path(f) for f in args.files]
    else:
        # Called with no arguments: scan everything
        root = Path(__file__).parent.parent
        targets = [root]

    violations = scan_files(targets)
    errors = [v for v in violations if v["severity"] == "ERROR"]

    print_report(violations)

    if errors:
        return 1  # Block the commit
    elif not violations:
        print("✔  No credentials or sensitive data found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
