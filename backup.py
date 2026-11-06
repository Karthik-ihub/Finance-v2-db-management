"""Supabase PostgreSQL backup helper.

This script creates a custom-format PostgreSQL archive using the official
pg_dump and pg_restore utilities. The archive is the primary backup and is
intended to preserve schema objects, data, constraints, relationships,
indexes, functions, policies, and other restorable objects.

CSV exports remain available as an optional supplementary feature for table
inspection, but they are not a substitute for a proper database dump.

IMPORTANT SAFETY NOTES:
- Passwords MUST NOT be in DATABASE_URL; use PGPASSWORD or PGPASSFILE
- A read-only schema inventory is generated for preflight review
- The backup does NOT automatically exclude or include schemas based on assumptions
- An explicit reviewed schema scope is required before treating a migration backup as ready
"""

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qsl, urlsplit, urlunsplit

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

import pandas as pd
from sqlalchemy import create_engine, inspect, text

# ------------------------------------------------------------------
# CONFIGURATION
# ------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "")
SOURCE_DATABASE_URL = os.getenv("SOURCE_DATABASE_URL", DATABASE_URL)  # Alias for clarity
SCHEMA = os.getenv("SCHEMA", "public")
ONLY_TABLES_STR = os.getenv("ONLY_TABLES", "")
ONLY_TABLES: List[str] = [item.strip() for item in ONLY_TABLES_STR.split(",") if item.strip()] if ONLY_TABLES_STR else []
SKIP_TABLES_STR = os.getenv("SKIP_TABLES", "")
SKIP_TABLES: List[str] = [item.strip() for item in SKIP_TABLES_STR.split(",") if item.strip()] if SKIP_TABLES_STR else []
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
RETRY_DELAY_SECONDS = int(os.getenv("RETRY_DELAY_SECONDS", "2"))
EXPORT_CSV = os.getenv("EXPORT_CSV", "false").strip().lower() in {"1", "true", "yes", "on"}
LOG_FILE = "backup.log"
LOG_LEVEL = logging.INFO
BACKUP_DIR_PREFIX = os.getenv("BACKUP_DIR_PREFIX", "supabase_backup")
PG_DUMP_PATH_ENV = os.getenv("PG_DUMP_PATH", "")
PG_RESTORE_PATH_ENV = os.getenv("PG_RESTORE_PATH", "")
PGPASSWORD_ENV = os.getenv("PGPASSWORD", "")
PGPASSFILE_ENV = os.getenv("PGPASSFILE", "")
DUMP_SCHEMAS_ENV = os.getenv("DUMP_SCHEMAS", "")


class BackupError(RuntimeError):
    """Raised when a mandatory backup or verification step fails."""


# ------------------------------------------------------------------
# SUPABASE PLATFORM-MANAGED SCHEMAS
# ------------------------------------------------------------------
# These schemas are managed by Supabase and should be identified separately
# from application schemas in the schema inventory.
SUPABASE_MANAGED_SCHEMAS: Set[str] = {
    "auth",
    "storage",
    "realtime",
    "vault",
    "graphql",
    "graphql_public",
    "extensions",
    "supabase_functions",
    "pgbouncer",
    "pgsodium",
    "pg_net",
    "cron",
    "_realtime",
    "_analytics",
    "_timescaledb_catalog",
    "_timescaledb_internal",
    "timescaledb_experimental",
    "timescaledb_public",
}

# System schemas that should not be dumped or considered as application schemas
SYSTEM_SCHEMAS: Set[str] = {
    "information_schema",
    "pg_catalog",
    "pg_toast",
}


# ------------------------------------------------------------------
# LOGGING
# ------------------------------------------------------------------

def setup_logging(backup_dir: Path) -> logging.Logger:
    """Setup logging to both the console and a file in the backup directory."""
    logger = logging.getLogger("supabase_backup")
    logger.setLevel(LOG_LEVEL)
    logger.handlers.clear()

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(LOG_LEVEL)
    console_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(console_handler)

    log_file = backup_dir / LOG_FILE
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(LOG_LEVEL)
    file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(file_handler)
    return logger


# ------------------------------------------------------------------
# DATABASE URL / PASSWORD HANDLING
# ------------------------------------------------------------------

def sanitize_database_url(database_url: str) -> str:
    """Return a redacted database URL safe for logs."""
    if not database_url:
        return "<not set>"

    try:
        split_url = urlsplit(database_url)
        username = split_url.username or ""
        host = split_url.hostname or "host"
        port = f":{split_url.port}" if split_url.port else ""

        if split_url.password:
            netloc = f"{username}:***@{host}{port}"
            safe_url = urlunsplit((split_url.scheme, netloc, split_url.path, split_url.query, split_url.fragment))
            return safe_url

        if username:
            netloc = f"{username}@{host}{port}"
            safe_url = urlunsplit((split_url.scheme, netloc, split_url.path, split_url.query, split_url.fragment))
            return safe_url

        return f"{split_url.scheme}://{host}{port}{split_url.path}"
    except Exception:
        return "<redacted>"


def strip_password_from_url(database_url: str) -> str:
    """
    Return the URL without any password, preserving connection query parameters.
    
    This is critical for safety: passwords must not be passed via command-line arguments.
    """
    if not database_url:
        return ""

    split_url = urlsplit(database_url)
    username = split_url.username or ""
    hostname = split_url.hostname or ""
    port = f":{split_url.port}" if split_url.port else ""
    
    # Build netloc without password
    if username:
        netloc = f"{username}@{hostname}{port}"
    else:
        netloc = f"{hostname}{port}"
    
    # Preserve query parameters, but remove any password parameter
    query_pairs = []
    for key, value in parse_qsl(split_url.query, keep_blank_values=True):
        if key.lower() == "password":
            continue  # Remove password from query string
        query_pairs.append((key, value))
    query = "&".join(f"{key}={value}" for key, value in query_pairs)
    
    return urlunsplit((split_url.scheme, netloc, split_url.path, query, split_url.fragment))


def ensure_safe_pg_environment(logger: logging.Logger, database_url: str) -> Tuple[str, Dict[str, str]]:
    """
    Return a password-free URL and a safe env dictionary for PostgreSQL CLI tools.
    
    Raises BackupError if password is found in URL without PGPASSWORD/PGPASSFILE.
    """
    if not database_url:
        raise BackupError(
            "DATABASE_URL is required. Use a password-free URI in .env and set PGPASSWORD or PGPASSFILE."
        )

    url_without_password = strip_password_from_url(database_url)
    resolved_env = os.environ.copy()

    parsed = urlsplit(database_url)
    
    # CRITICAL: Reject password-bearing DATABASE_URL values
    if parsed.password:
        if not (PGPASSWORD_ENV or PGPASSFILE_ENV):
            raise BackupError(
                "Password found in DATABASE_URL. Remove the password from DATABASE_URL and set PGPASSWORD or PGPASSFILE instead. "
                "This prevents credentials from being passed through command-line arguments to pg_dump. "
                "For security, the script rejects password-bearing DATABASE_URL values."
            )
        if PGPASSWORD_ENV:
            resolved_env["PGPASSWORD"] = PGPASSWORD_ENV
        if PGPASSFILE_ENV:
            resolved_env["PGPASSFILE"] = PGPASSFILE_ENV
        logger.info("Password-free URL will be passed to pg_dump; credentials from environment variables.")
    else:
        if PGPASSWORD_ENV:
            resolved_env["PGPASSWORD"] = PGPASSWORD_ENV
        if PGPASSFILE_ENV:
            resolved_env["PGPASSFILE"] = PGPASSFILE_ENV

    return url_without_password, resolved_env


# ------------------------------------------------------------------
# SCHEMA INVENTORY (PREFLIGHT REPORT)
# ------------------------------------------------------------------

def get_source_schemas(logger: logging.Logger, database_url: str) -> Dict[str, Dict[str, object]]:
    """
    Generate a read-only schema inventory from the source database.
    
    This provides a preflight report to identify application-owned schemas
    separately from Supabase-managed schemas.
    
    Returns a dict with schema information including:
    - is_application: True if not Supabase-managed or system
    - is_supabase: True if Supabase-managed
    - is_system: True if PostgreSQL system schema
    - table_count, view_count, function_count, etc.
    """
    engine = create_engine(database_url, pool_pre_ping=True, pool_recycle=3600)
    
    try:
        inspector = inspect(engine)
        all_schemas = inspector.get_schema_names()
        
        schema_inventory = {}
        
        for schema in all_schemas:
            if schema.startswith("pg_temp_"):
                continue  # Skip temporary schemas
            
            is_supabase = schema in SUPABASE_MANAGED_SCHEMAS
            is_system = schema in SYSTEM_SCHEMAS
            is_application = not (is_supabase or is_system)
            
            # Get object counts
            with engine.connect() as connection:
                # Table count
                tables_result = connection.execute(text("""
                    SELECT COUNT(*) 
                    FROM pg_catalog.pg_class c
                    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = :schema 
                    AND c.relkind = 'r'
                """), {"schema": schema})
                table_count = tables_result.scalar_one() or 0
                
                # View count
                views_result = connection.execute(text("""
                    SELECT COUNT(*) 
                    FROM pg_catalog.pg_class c
                    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = :schema 
                    AND c.relkind IN ('v', 'm')
                """), {"schema": schema})
                view_count = views_result.scalar_one() or 0
                
                # Function/procedure count
                funcs_result = connection.execute(text("""
                    SELECT COUNT(*) 
                    FROM pg_catalog.pg_proc p
                    JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
                    WHERE n.nspname = :schema
                """), {"schema": schema})
                function_count = funcs_result.scalar_one() or 0
                
                # Sequence count
                seqs_result = connection.execute(text("""
                    SELECT COUNT(*) 
                    FROM pg_catalog.pg_class c
                    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = :schema 
                    AND c.relkind = 'S'
                """), {"schema": schema})
                sequence_count = seqs_result.scalar_one() or 0
            
            schema_inventory[schema] = {
                "is_application": is_application,
                "is_supabase": is_supabase,
                "is_system": is_system,
                "table_count": table_count,
                "view_count": view_count,
                "function_count": function_count,
                "sequence_count": sequence_count,
                "total_objects": table_count + view_count + function_count + sequence_count,
            }
        
        # Log summary
        app_schemas = [s for s, info in schema_inventory.items() if info["is_application"]]
        supabase_schemas = [s for s, info in schema_inventory.items() if info["is_supabase"]]
        system_schemas = [s for s, info in schema_inventory.items() if info["is_system"]]
        
        logger.info("Schema inventory generated:")
        logger.info("  Application schemas: %s", ", ".join(sorted(app_schemas)) if app_schemas else "none")
        logger.info("  Supabase-managed schemas: %s", ", ".join(sorted(supabase_schemas)) if supabase_schemas else "none")
        logger.info("  System schemas: %s", ", ".join(sorted(system_schemas)) if system_schemas else "none")
        
        return schema_inventory
        
    finally:
        engine.dispose()


def generate_schema_inventory_report(
    schema_inventory: Dict[str, Dict[str, object]],
    dump_schemas: List[str]
) -> str:
    """Generate a human-readable schema inventory report."""
    lines = [
        "",
        "=" * 80,
        "SCHEMA INVENTORY - PREFLIGHT REPORT",
        "=" * 80,
        "",
        "This report identifies application-owned schemas separately from",
        "Supabase-managed and system schemas. Use this to verify the",
        "schema scope before treating a backup as ready for migration.",
        "",
        "IMPORTANT: The backup does NOT automatically exclude or include schemas",
        "based on assumptions. You MUST review and explicitly approve the",
        "schema scope for migration readiness.",
        "",
        "-" * 80,
        "SCHEMA CLASSIFICATION",
        "-" * 80,
        "",
    ]
    
    # Group schemas by type
    app_schemas = {s: info for s, info in schema_inventory.items() if info["is_application"]}
    supabase_schemas = {s: info for s, info in schema_inventory.items() if info["is_supabase"]}
    system_schemas = {s: info for s, info in schema_inventory.items() if info["is_system"]}
    
    # Application schemas
    lines.append("APPLICATION SCHEMAS (your code/data):")
    lines.append("-" * 40)
    if app_schemas:
        for schema, info in sorted(app_schemas.items()):
            lines.append(
                f"  {schema}: {info['table_count']} tables, {info['view_count']} views, "
                f"{info['function_count']} functions, {info['sequence_count']} sequences "
                f"= {info['total_objects']} total objects"
            )
    else:
        lines.append("  (none)")
    lines.append("")
    
    # Supabase schemas
    lines.append("SUPABASE-MANAGED SCHEMAS (platform features):")
    lines.append("-" * 40)
    if supabase_schemas:
        for schema, info in sorted(supabase_schemas.items()):
            lines.append(
                f"  {schema}: {info['table_count']} tables, {info['view_count']} views, "
                f"{info['function_count']} functions, {info['sequence_count']} sequences "
                f"= {info['total_objects']} total objects"
            )
    else:
        lines.append("  (none)")
    lines.append("")
    
    # System schemas
    lines.append("SYSTEM SCHEMAS (PostgreSQL internals):")
    lines.append("-" * 40)
    if system_schemas:
        for schema, info in sorted(system_schemas.items()):
            lines.append(
                f"  {schema}: {info['table_count']} tables, {info['view_count']} views, "
                f"{info['function_count']} functions, {info['sequence_count']} sequences "
                f"= {info['total_objects']} total objects"
            )
    else:
        lines.append("  (none)")
    lines.append("")
    
    # Dump scope
    lines.append("-" * 80)
    lines.append("DUMP SCOPE")
    lines.append("-" * 80)
    if not dump_schemas:
        lines.append("  DUMP_SCHEMAS is empty: ALL accessible schemas will be dumped.")
        lines.append("  This creates a complete backup.")
    else:
        lines.append(f"  DUMP_SCHEMAS = {', '.join(dump_schemas)}")
        lines.append("  WARNING: This creates a PARTIAL backup of selected schemas only.")
        lines.append("  This is NOT a complete migration backup.")
    lines.append("")
    
    # Recommendations
    lines.append("-" * 80)
    lines.append("RECOMMENDATIONS")
    lines.append("-" * 80)
    
    if app_schemas:
        lines.append("  [OK] Application schemas detected and will be backed up.")
    else:
        lines.append("  [WARNING] No application schemas detected. Verify this is expected.")
    
    if dump_schemas:
        # Check if all application schemas are included
        app_schema_names = set(app_schemas.keys())
        dump_schema_names = set(dump_schemas)
        missing = app_schema_names - dump_schema_names
        if missing:
            lines.append(
                f"  [CRITICAL] Application schemas NOT in dump scope: {', '.join(sorted(missing))}"
            )
            lines.append("  The backup will be INCOMPLETE for migration purposes.")
        else:
            lines.append("  [OK] All application schemas are included in dump scope.")
    
    lines.append("")
    lines.append("=" * 80)
    lines.append("END OF SCHEMA INVENTORY REPORT")
    lines.append("=" * 80)
    
    return "\n".join(lines)


# ------------------------------------------------------------------
# PG DUMP / PG RESTORE COMMANDS
# ------------------------------------------------------------------

def build_pg_command(
    pg_dump_path: str, 
    archive_path: Path, 
    database_url: str, 
    dump_schemas: List[str]
) -> List[str]:
    """Build a safe pg_dump command without destructive options."""
    command = [
        pg_dump_path,
        "--format=c",
        "--verbose",
        "--file",
        str(archive_path),
        "--dbname",
        database_url,
        "--no-owner",
        "--no-privileges",
    ]
    for schema_name in dump_schemas:
        command.extend(["--schema", schema_name])
    return command


def command_for_logging(command: List[str]) -> List[str]:
    """Prepare a redacted command for logs to avoid exposing credentials."""
    redacted = []
    for index, item in enumerate(command):
        if index > 0 and command[index - 1] in {"--dbname", "-d"}:
            redacted.append(sanitize_database_url(item))
        else:
            redacted.append(item)
    return redacted


def get_pg_version(pg_dump_path: str) -> str:
    """Return the PostgreSQL client version for reporting."""
    result = subprocess.run([pg_dump_path, "--version"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        error_text = (result.stderr or result.stdout or "unknown").strip()
        raise BackupError(f"pg_dump --version failed: {error_text}")
    output = (result.stdout or result.stderr or "").strip()
    version = output.splitlines()[0] if output else "unknown"
    return version


def parse_postgres_version(value: str) -> Optional[Tuple[int, int]]:
    """Parse values such as '17.11' or '18.6'."""
    match = re.search(r"(\d+)(?:\.(\d+))?", value)
    if not match:
        return None
    major = int(match.group(1))
    minor = int(match.group(2) or 0)
    return major, minor


def get_pg_server_version(engine) -> str:
    """Ask PostgreSQL for the server version string without exposing credentials in the logs."""
    with engine.connect() as connection:
        result = connection.exec_driver_sql("SHOW server_version")
        return str(result.scalar_one())


def check_postgres_version_compatibility(
    logger: logging.Logger, 
    server_version: str, 
    client_version: str
) -> None:
    """Warn if client/server versions are mismatched and document compatibility expectations."""
    server_parse = parse_postgres_version(server_version)
    client_parse = parse_postgres_version(client_version)
    if server_parse is None or client_parse is None:
        logger.warning(
            "Could not parse PostgreSQL version values for compatibility check: server=%s client=%s",
            server_version,
            client_version
        )
        return

    server_major, _ = server_parse
    client_major, _ = client_parse
    logger.info("Source server version: %s", server_version)
    logger.info("Client tool version: %s", client_version)

    if client_major < server_major:
        raise BackupError(
            f"pg_dump client version {client_version} is older than the source PostgreSQL server {server_version}. "
            "Upgrade the PostgreSQL client tools before running a full backup."
        )

    if server_major == 17 and client_major == 18:
        logger.info(
            "Version check: PostgreSQL 18 client tools are expected to be compatible with PostgreSQL 17 source servers for pg_dump/pg_restore operations."
        )
    elif client_major > server_major:
        logger.warning(
            "The PostgreSQL client version is newer than the source server. "
            "This is usually acceptable for pg_dump/pg_restore, "
            "but the dump should still be tested in a validation database before production use."
        )


def ensure_pg_tools(logger: logging.Logger) -> Dict[str, str]:
    """Ensure pg_dump and pg_restore are available on the system PATH."""
    pg_dump = PG_DUMP_PATH_ENV or shutil.which("pg_dump")
    pg_restore = PG_RESTORE_PATH_ENV or shutil.which("pg_restore")
    if not pg_dump:
        raise BackupError("pg_dump not found. Install PostgreSQL client tools or set PG_DUMP_PATH in .env")
    if not pg_restore:
        raise BackupError("pg_restore not found. Install PostgreSQL client tools or set PG_RESTORE_PATH in .env")
    logger.info("Using pg_dump: %s", pg_dump)
    logger.info("Using pg_restore: %s", pg_restore)
    return {"pg_dump": pg_dump, "pg_restore": pg_restore}


def validate_dump_schema_setting(logger: logging.Logger) -> List[str]:
    """
    Validate the schema filter setting.
    
    A full backup should default to all accessible schemas.
    If DUMP_SCHEMAS is set, warn that this creates a partial backup
    and require explicit reviewed schema scope for migration.
    """
    if not DUMP_SCHEMAS_ENV.strip():
        logger.info(
            "DUMP_SCHEMAS is empty: creating a complete backup of all accessible schemas. "
            "This is the recommended setting for migration backups."
        )
        return []

    dump_schemas = [item.strip() for item in DUMP_SCHEMAS_ENV.split(",") if item.strip()]
    logger.warning(
        "DUMP_SCHEMAS is set to a subset: %s. "
        "This creates a PARTIAL dump and is NOT a complete backup of every accessible schema. "
        "An explicit reviewed schema scope is required before treating this backup as ready for migration. "
        "For a complete migration backup, leave DUMP_SCHEMAS empty.",
        ", ".join(dump_schemas),
    )
    return dump_schemas


def create_pg_dump_archive(
    logger: logging.Logger,
    database_url: str,
    archive_path: Path,
    dump_schemas: List[str],
    pg_dump_path: str,
    pg_env: Dict[str, str],
) -> None:
    """Run pg_dump to an intermediate archive file and only rename it on success."""
    temp_archive_path = archive_path.with_name(f"{archive_path.name}.partial")
    if temp_archive_path.exists():
        temp_archive_path.unlink()
    if archive_path.exists():
        raise BackupError(
            f"Destination archive already exists: {archive_path}. "
            "Choose a new timestamped backup directory to avoid overwriting any existing backup file."
        )

    command = build_pg_command(pg_dump_path, temp_archive_path, database_url, dump_schemas)
    transient_errors = (
        "connection refused",
        "could not connect",
        "timeout",
        "timed out",
        "server closed the connection",
        "connection reset",
        "too many connections",
        "temporary failure",
        "network unreachable",
    )

    for attempt in range(1, MAX_RETRIES + 2):
        logger.info("Running pg_dump (attempt %s/%s)", attempt, MAX_RETRIES + 1)
        logger.debug("Command: %s", command_for_logging(command))
        result = subprocess.run(command, capture_output=True, text=True, env=pg_env, check=False)
        if result.returncode == 0:
            if not temp_archive_path.exists() or temp_archive_path.stat().st_size <= 0:
                raise BackupError(f"Temporary archive was not created correctly: {temp_archive_path}")
            temp_archive_path.replace(archive_path)
            logger.info("pg_dump completed successfully and the archive was finalized.")
            return

        combined_output = (result.stdout or "") + "\n" + (result.stderr or "")
        lowered_output = combined_output.lower()
        is_transient = any(marker in lowered_output for marker in transient_errors)

        if is_transient and attempt <= MAX_RETRIES:
            wait_time = RETRY_DELAY_SECONDS * attempt
            logger.warning("Transient PostgreSQL connection issue detected. Retrying in %s seconds.", wait_time)
            time.sleep(wait_time)
            continue

        # Cleanup any partial archive so it is never mistaken for a valid backup.
        if temp_archive_path.exists():
            temp_archive_path.unlink()
        error_message = (result.stderr or result.stdout or "Unknown pg_dump failure").strip()
        raise BackupError(f"pg_dump failed: {error_message}")

    if temp_archive_path.exists():
        temp_archive_path.unlink()
    raise BackupError("pg_dump failed after all retry attempts")


def verify_archive(
    logger: logging.Logger, 
    archive_path: Path, 
    pg_restore_path: str, 
    manifest_path: Path
) -> Dict[str, object]:
    """Verify the archive is readable using pg_restore --list without restoring data."""
    if not archive_path.exists() or archive_path.stat().st_size <= 0:
        raise BackupError(f"Backup archive is missing or empty: {archive_path}")

    logger.info(
        "Verifying archive readability with pg_restore --list (read-only archive inspection only)"
    )
    result = subprocess.run(
        [pg_restore_path, "--list", str(archive_path)], 
        capture_output=True, 
        text=True, 
        check=False
    )
    if result.returncode != 0:
        error_message = (result.stderr or result.stdout or "Archive verification failed").strip()
        raise BackupError(f"Archive verification failed: {error_message}")

    manifest_path.write_text(result.stdout or "", encoding="utf-8")
    if manifest_path.stat().st_size <= 0:
        raise BackupError(f"Archive manifest is empty: {manifest_path}")

    logger.info("Archive verification passed: pg_restore --list succeeded.")
    return {
        "verification_status": "passed",
        "manifest_path": str(manifest_path),
        "archive_size_bytes": archive_path.stat().st_size,
    }


def compute_sha256(file_path: Path) -> str:
    """Return the SHA256 hash for a file."""
    sha256 = hashlib.sha256()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def write_sha256_file(file_path: Path) -> Tuple[Path, str]:
    """Create a SHA256 checksum file and return the checksum value."""
    checksum_value = compute_sha256(file_path)
    checksum_path = Path(f"{file_path}.sha256")
    checksum_path.write_text(f"{checksum_value}  {file_path.name}\n", encoding="utf-8")
    return checksum_path, checksum_value


def export_csv_backups(
    logger: logging.Logger, 
    engine, 
    backup_dir: Path, 
    schema: str
) -> List[Dict[str, object]]:
    """Optional supplementary backup: export table data to CSV files for inspection."""
    inspector = inspect(engine)
    tables = inspector.get_table_names(schema=schema)
    filtered_tables = [table for table in tables if table not in SKIP_TABLES]
    if ONLY_TABLES:
        filtered_tables = [table for table in filtered_tables if table in ONLY_TABLES]
    filtered_tables = sorted(filtered_tables)

    if not filtered_tables:
        logger.warning("No CSV tables selected for supplementary export.")
        return []

    logger.info("Starting supplementary CSV export for %s table(s).", len(filtered_tables))
    results: List[Dict[str, object]] = []
    for index, table in enumerate(filtered_tables, 1):
        logger.info("[%s/%s] Exporting CSV for %s", index, len(filtered_tables), table)
        try:
            df = pd.read_sql_table(table, con=engine, schema=schema)
            csv_path = backup_dir / f"{table}.csv"
            df.to_csv(csv_path, index=False, encoding="utf-8")
            results.append(
                {
                    "status": "success",
                    "table": table,
                    "row_count": len(df),
                    "file_path": str(csv_path),
                    "file_size": csv_path.stat().st_size,
                    "error": None,
                }
            )
            logger.info("[OK] %s rows=%s file=%s", table, len(df), csv_path)
        except Exception as exc:
            logger.exception("CSV export failed for %s", table)
            results.append(
                {
                    "status": "failed",
                    "table": table,
                    "row_count": 0,
                    "file_path": None,
                    "file_size": 0,
                    "error": str(exc),
                }
            )
    return results


def generate_backup_report(
    backup_dir: Path,
    start_time: datetime,
    pg_version: str,
    archive_path: Path,
    manifest_path: Path,
    checksum_path: Path,
    checksum_value: str,
    verification_status: str,
    schema_inventory: Optional[Dict[str, Dict[str, object]]] = None,
    csv_results: Optional[List[Dict[str, object]]] = None,
    server_version: Optional[str] = None,
    dump_schemas: Optional[List[str]] = None,
) -> str:
    """Create a human-readable backup report that separates archive verification from restore testing."""
    end_time = datetime.now()
    duration = end_time - start_time
    archive_size = archive_path.stat().st_size if archive_path.exists() else 0
    csv_success_count = sum(1 for result in csv_results or [] if result.get("status") == "success")
    csv_failed_count = sum(1 for result in csv_results or [] if result.get("status") == "failed")

    # Count application schemas
    app_schema_count = 0
    if schema_inventory:
        app_schema_count = sum(1 for s, info in schema_inventory.items() if info.get("is_application"))

    report = """\
================================================================================
                       SUPABASE POSTGRESQL BACKUP REPORT
================================================================================
Backup Directory:           {backup_dir}
Created:                    {created_at}
Duration:                   {duration}
Source PostgreSQL Version:  {server_version}
PostgreSQL Client Version:  {pg_version}
Archive File:               {archive_name}
Archive Size:               {archive_size_bytes:,} bytes
Archive Checksum:           {checksum_value}
Checksum File:              {checksum_name}
Archive Readability:        {verification_status}
Manifest File:              {manifest_name}
Restore Test Status:        NOT RUN (manual validation only; archive is not auto-restored)
Schema Inventory:           {app_schema_count} application schema(s) detected

IMPORTANT NOTES
- The full PostgreSQL archive is the primary backup and is created with pg_dump --format=c.
- This archive includes accessible database objects and data, including constraints, indexes,
  sequences, enums, triggers, views, functions, policies, and other restorable schema objects.
- `--no-owner` and `--no-privileges` are intentionally used for portability; destination owners,
  permissions, and project configuration must be configured separately.
- A successful restore test is intentionally not run automatically. Restore validation happens in a
  dedicated validation database only.
- CSV export remains optional and supplemental; it is not the primary backup.
- The backup does NOT automatically exclude or include schemas based on assumptions.
- An explicit reviewed schema scope is REQUIRED before treating a migration backup as ready for migration.

ARCHIVE READABILITY WARNING
- pg_restore --list was used to verify the archive is readable.
- Archive readability does NOT prove restore success.
- A separate validation restore test in a dedicated validation database is REQUIRED.

CSV EXPORT SUMMARY
- Successful CSV exports: {csv_success_count}
- Failed CSV exports:     {csv_failed_count}

{schema_inventory_note}

================================================================================
""".format(
        backup_dir=backup_dir,
        created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        duration=str(duration),
        server_version=server_version or "unknown",
        pg_version=pg_version,
        archive_name=archive_path.name,
        archive_size_bytes=archive_size,
        checksum_value=checksum_value,
        checksum_name=checksum_path.name,
        verification_status=verification_status,
        manifest_name=manifest_path.name,
        csv_success_count=csv_success_count,
        csv_failed_count=csv_failed_count,
        app_schema_count=app_schema_count,
        schema_inventory_note=(
            "\nSCHEMA SCOPE:\n"
            f"- DUMP_SCHEMAS: {'ALL schemas (complete backup)' if not dump_schemas else 'Partial: ' + ', '.join(dump_schemas)}\n"
            "- Review the SCHEMA_INVENTORY.txt file for detailed schema classification.\n"
            + ("- WARNING: Partial backup - not all schemas are included.\n" if dump_schemas else "")
        ) if dump_schemas is not None else ""
    )

    if csv_results:
        report += "\nCSV EXPORT DETAILS\n"
        for result in sorted(csv_results, key=lambda item: str(item.get("table"))):
            status = result.get("status", "unknown")
            table = result.get("table", "unknown")
            if status == "success":
                report += f"[OK] {table}: {result.get('row_count', 0):,} rows\n"
            else:
                report += f"[FAILED] {table}: {result.get('error', 'Unknown error')}\n"

    report += "\n================================================================================\n"
    return report


def write_metadata_file(
    backup_dir: Path,
    archive_path: Path,
    pg_version: str,
    verification_status: str,
    checksum_value: str,
    server_version: str,
    dump_schemas: List[str],
    schema_inventory: Optional[Dict[str, Dict[str, object]]] = None,
) -> Path:
    """Write metadata that records the archive checksum and backup validation state."""
    # Build schema summary
    schema_summary = {}
    if schema_inventory:
        for schema, info in schema_inventory.items():
            schema_summary[schema] = {
                "is_application": info.get("is_application", False),
                "is_supabase": info.get("is_supabase", False),
                "is_system": info.get("is_system", False),
                "object_count": info.get("total_objects", 0),
            }
    
    metadata = {
        "backup_directory": str(backup_dir),
        "archive_file": archive_path.name,
        "archive_size_bytes": archive_path.stat().st_size if archive_path.exists() else 0,
        "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "postgresql_source_version": server_version,
        "postgresql_client_version": pg_version,
        "archive_verification_status": verification_status,
        "restore_validation_status": "NOT_RUN",
        "checksum_sha256": checksum_value,
        "dump_schemas": dump_schemas or [],
        "schema_inventory": schema_summary,
        "notes": [
            "Primary backup is a pg_dump custom-format archive.",
            "CSV exports are supplementary and optional only.",
            "Archive readability was checked with pg_restore --list.",
            "A successful restore test is a separate manual validation step and is not executed automatically.",
            "Destination ownership, permissions, and project configuration need separate setup after restore.",
            "The backup does NOT automatically exclude or include schemas based on assumptions.",
            "An explicit reviewed schema scope is REQUIRED before treating a migration backup as ready for migration.",
            "Archive readability does NOT prove restore success.",
        ],
    }
    metadata_path = backup_dir / "BACKUP_METADATA.json"
    metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return metadata_path


# ------------------------------------------------------------------
# MAIN BACKUP
# ------------------------------------------------------------------

def backup_supabase() -> int:
    """Create a complete dump archive and keep optional CSV export as supplementary only."""
    if not DATABASE_URL and not SOURCE_DATABASE_URL:
        raise BackupError(
            "DATABASE_URL or SOURCE_DATABASE_URL is not configured. "
            "Add a password-free URI to .env and set PGPASSWORD or PGPASSFILE."
        )
    
    database_url = SOURCE_DATABASE_URL or DATABASE_URL

    backup_dir = Path(f"{BACKUP_DIR_PREFIX}_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    suffix = 1
    while backup_dir.exists():
        backup_dir = Path(f"{BACKUP_DIR_PREFIX}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{suffix}")
        suffix += 1
    backup_dir.mkdir(parents=True, exist_ok=True)

    logger = setup_logging(backup_dir)
    start_time = datetime.now()

    logger.info("=" * 80)
    logger.info("SUPABASE POSTGRESQL BACKUP STARTED")
    logger.info("=" * 80)
    logger.info("Backup directory: %s", backup_dir.resolve())
    logger.info("Source database URL: %s", sanitize_database_url(database_url))
    logger.info("CSV export enabled: %s", EXPORT_CSV)
    logger.info("Schema setting: %s (used for CSV export only)", SCHEMA)
    if ONLY_TABLES:
        logger.info("CSV-only table filter: %s", ", ".join(ONLY_TABLES))
    if SKIP_TABLES:
        logger.info("CSV-only skip list: %s", ", ".join(SKIP_TABLES))

    # Validate and get dump schemas
    dump_schemas = validate_dump_schema_setting(logger)
    
    # Ensure safe PostgreSQL environment (password-free URL)
    password_free_database_url, pg_env = ensure_safe_pg_environment(logger, database_url)

    pg_tools = ensure_pg_tools(logger)
    pg_dump_path = pg_tools["pg_dump"]
    pg_restore_path = pg_tools["pg_restore"]
    pg_version = get_pg_version(pg_dump_path)
    archive_path = backup_dir / f"{backup_dir.name}.dump"
    manifest_path = backup_dir / "BACKUP_MANIFEST.txt"
    logger.info("Archive target: %s", archive_path)

    # Connect to get server version and schema inventory
    engine = create_engine(password_free_database_url, pool_pre_ping=True, pool_recycle=3600)
    try:
        server_version = get_pg_server_version(engine)
        check_postgres_version_compatibility(logger, server_version, pg_version)
        
        # Generate schema inventory for preflight report
        logger.info("Generating schema inventory for preflight report...")
        schema_inventory = get_source_schemas(logger, password_free_database_url)
        
        # Write schema inventory report
        schema_inventory_report = generate_schema_inventory_report(schema_inventory, dump_schemas)
        schema_inventory_path = backup_dir / "SCHEMA_INVENTORY.txt"
        schema_inventory_path.write_text(schema_inventory_report, encoding="utf-8")
        logger.info("Schema inventory written to %s", schema_inventory_path)
        
    finally:
        engine.dispose()

    try:
        # Create the archive
        create_pg_dump_archive(
            logger, 
            password_free_database_url, 
            archive_path, 
            dump_schemas, 
            pg_dump_path, 
            pg_env
        )
        
        if not archive_path.exists() or archive_path.stat().st_size <= 0:
            raise BackupError(f"Backup archive was not created or is empty: {archive_path}")

        # Verify archive
        archive_verification = verify_archive(logger, archive_path, pg_restore_path, manifest_path)
        
        # Create SHA256 checksum
        checksum_path, checksum_value = write_sha256_file(archive_path)
        
        # Write metadata
        metadata_path = write_metadata_file(
            backup_dir,
            archive_path,
            pg_version,
            archive_verification["verification_status"],
            checksum_value,
            server_version,
            dump_schemas,
            schema_inventory,
        )

        # Optional CSV export
        csv_results: List[Dict[str, object]] = []
        if EXPORT_CSV:
            engine = create_engine(password_free_database_url, pool_pre_ping=True, pool_recycle=3600)
            try:
                csv_results = export_csv_backups(logger, engine, backup_dir, SCHEMA)
            finally:
                engine.dispose()
        else:
            logger.info("CSV export skipped. This is intentional: the pg_dump archive is the primary backup.")

        # Generate backup report
        report = generate_backup_report(
            backup_dir,
            start_time,
            pg_version,
            archive_path,
            manifest_path,
            checksum_path,
            checksum_value,
            str(archive_verification["verification_status"]),
            schema_inventory,
            csv_results,
            server_version,
            dump_schemas,
        )
        report_path = backup_dir / "BACKUP_REPORT.txt"
        report_path.write_text(report, encoding="utf-8")

        logger.info("\n%s", report)
        logger.info("Metadata file: %s", metadata_path)
        logger.info("Schema inventory: %s", schema_inventory_path)
        logger.info("Archive verification succeeded and backup completed without auto-restore.")
        logger.info("")
        logger.info("REMINDER: Review SCHEMA_INVENTORY.txt and verify the schema scope.")
        logger.info("An explicit reviewed schema scope is REQUIRED before migration.")
        return 0
        
    except Exception as exc:
        logger.error("Backup failed: %s", exc)
        failure_report = backup_dir / "BACKUP_REPORT_FAILURE.txt"
        failure_report.write_text(
            f"Backup failed: {exc}\nTimestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n",
            encoding="utf-8",
        )
        raise BackupError(str(exc)) from exc


if __name__ == "__main__":
    try:
        raise SystemExit(backup_supabase())
    except KeyboardInterrupt:
        print("\n[ERROR] Backup interrupted by user.")
        raise SystemExit(1)
    except BackupError as exc:
        print(f"\n[ERROR] Backup failed: {exc}")
        raise SystemExit(1)
    except Exception as exc:
        print(f"\n[ERROR] Unexpected backup failure: {exc}")
        raise SystemExit(1)
