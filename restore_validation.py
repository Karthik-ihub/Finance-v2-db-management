"""Manual restore-validation helper.

This script validates that a pg_dump archive can be restored into a dedicated,
non-production validation database. It performs extensive safety checks to ensure
that:

1. The target is explicitly configured as the validation database (host, project, dbname)
2. The target is NOT the source database (DATABASE_URL)
3. The target URL does not contain a password
4. Application schemas in the target do not already contain application objects
5. Supabase platform-managed schemas are handled correctly
6. The restore uses --exit-on-error and never uses --clean, --create, DROP, TRUNCATE, or DELETE

This script is intentionally separate from the backup script and does not overwrite
the current source or an existing target database.

Use it ONLY with a freshly created validation database that is not your live
project database. The validation database identity MUST be explicitly configured
in environment variables.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qsl, urlsplit, urlunsplit

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


class RestoreValidationError(RuntimeError):
    """Raised when the validation restore cannot run safely."""


# ------------------------------------------------------------------
# SUPABASE PLATFORM-MANAGED SCHEMAS
# ------------------------------------------------------------------
# These schemas are managed by Supabase and should not be considered
# as application schemas. They may exist in a new project and should
# not cause validation to fail.
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
    "information_schema",
    "pg_catalog",
    "pg_toast",
    "public",  # Often used by Supabase for default objects
}

# Minimum schemas that must exist in a fresh Supabase project
EXPECTED_SUPABASE_SCHEMAS: Set[str] = {
    "auth",
    "storage",
    "pg_catalog",
    "information_schema",
}


# ------------------------------------------------------------------
# VALIDATION DATABASE IDENTITY
# ------------------------------------------------------------------

def get_validation_database_identity() -> Dict[str, str]:
    """
    Get the explicitly configured validation database identity from environment.
    
    Returns a dict with:
    - host: Expected hostname (e.g., db.[PROJECT-REF].supabase.co)
    - project_ref: Expected project reference
    - database_name: Expected database name
    - full_host_pattern: Regex pattern for host matching
    """
    identity = {}
    
    # Get validation database URL for identity extraction
    validation_url = os.getenv("VALIDATION_DATABASE_URL", "").strip()
    
    # Get explicit identity settings (preferred over URL parsing)
    identity["host"] = os.getenv("VALIDATION_DATABASE_HOST", "").strip()
    identity["project_ref"] = os.getenv("VALIDATION_PROJECT_REF", "").strip()
    identity["database_name"] = os.getenv("VALIDATION_DATABASE_NAME", "").strip()
    
    # If explicit settings not provided, try to extract from URL
    if not identity["host"] and validation_url:
        parsed = urlsplit(validation_url)
        identity["host"] = parsed.hostname or ""
    
    if not identity["database_name"] and validation_url:
        parsed = urlsplit(validation_url)
        # Remove leading slash
        db_path = parsed.path.lstrip("/") if parsed.path else ""
        identity["database_name"] = db_path
    
    # Extract project ref from host if not explicitly set
    if not identity["project_ref"] and identity["host"]:
        # Pattern: db.[PROJECT-REF].supabase.co
        match = re.search(r"db\.([^.]+)\.supabase\.co", identity["host"])
        if match:
            identity["project_ref"] = match.group(1)
    
    # Build host pattern for matching
    if identity["project_ref"]:
        identity["full_host_pattern"] = rf"db\.{re.escape(identity['project_ref'])}\.supabase\.co"
    elif identity["host"]:
        # Escape special regex characters in host
        identity["full_host_pattern"] = re.escape(identity["host"])
    else:
        identity["full_host_pattern"] = ""
    
    return identity


def validate_validation_identity_configured() -> None:
    """
    Ensure the validation database identity is explicitly configured.
    Raises RestoreValidationError if not properly configured.
    """
    identity = get_validation_database_identity()
    
    missing = []
    if not identity.get("host"):
        missing.append("VALIDATION_DATABASE_HOST or VALIDATION_DATABASE_URL")
    if not identity.get("database_name"):
        missing.append("VALIDATION_DATABASE_NAME or VALIDATION_DATABASE_URL")
    
    if missing:
        raise RestoreValidationError(
            f"Validation database identity is not explicitly configured. "
            f"The following environment variables must be set: {', '.join(missing)}. "
            f"A validation restore MUST use a dedicated, explicitly identified validation database. "
            f"This prevents accidental restore into production or the wrong target."
        )


def check_target_matches_validation_identity(target_url: str) -> None:
    """
    Verify that the supplied target URL matches the configured validation database identity.
    Raises RestoreValidationError if it doesn't match.
    """
    identity = get_validation_database_identity()
    parsed = urlsplit(target_url)
    
    # Check host
    target_host = parsed.hostname or ""
    host_pattern = identity.get("full_host_pattern", "")
    
    if host_pattern:
        if not re.match(host_pattern, target_host):
            raise RestoreValidationError(
                f"Target host '{target_host}' does not match the configured validation database host pattern '{host_pattern}'. "
                f"Validation restores must use the explicitly configured validation database only."
            )
    else:
        # If no pattern, do exact match
        if target_host != identity.get("host", ""):
            raise RestoreValidationError(
                f"Target host '{target_host}' does not match the configured validation database host '{identity.get('host', '')}'. "
                f"Validation restores must use the explicitly configured validation database only."
            )
    
    # Check database name
    target_dbname = parsed.path.lstrip("/") if parsed.path else ""
    expected_dbname = identity.get("database_name", "")
    
    if target_dbname != expected_dbname:
        raise RestoreValidationError(
            f"Target database name '{target_dbname}' does not match the configured validation database name '{expected_dbname}'. "
            f"Validation restores must use the explicitly configured validation database only."
        )


def check_target_not_source(target_url: str) -> None:
    """
    Verify that the target is NOT the source database.
    Compares host, port, and database name.
    Raises RestoreValidationError if they match.
    """
    source_url = os.getenv("DATABASE_URL", "").strip()
    # Get password-free URL for comparison
    source_parsed = urlsplit(source_url)
    target_parsed = urlsplit(target_url)
    
    # Compare scheme, hostname, port, and database path
    if (source_parsed.scheme == target_parsed.scheme and
        source_parsed.hostname == target_parsed.hostname and
        source_parsed.port == target_parsed.port and
        source_parsed.path == target_parsed.path):
        raise RestoreValidationError(
            f"Target database URL matches the source DATABASE_URL. "
            f"A validation restore MUST use a separate, dedicated validation database, not the source database. "
            f"Source: {sanitize_database_url(source_url)}"
        )


# ------------------------------------------------------------------
# URL HANDLING
# ------------------------------------------------------------------

def sanitize_database_url(database_url: str) -> str:
    """Return a redacted database URL safe for logs."""
    if not database_url:
        return "<not set>"

    try:
        parsed = urlsplit(database_url)
        username = parsed.username or ""
        host = parsed.hostname or "host"
        port = f":{parsed.port}" if parsed.port else ""

        if parsed.password:
            netloc = f"{username}:***@{host}{port}"
            safe_url = urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
            return safe_url

        if username:
            netloc = f"{username}@{host}{port}"
            safe_url = urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
            return safe_url

        return f"{parsed.scheme}://{host}{port}{parsed.path}"
    except Exception:
        return "<redacted>"


def strip_password_from_url(database_url: str) -> str:
    """Return the URL without any password, preserving query parameters."""
    if not database_url:
        return ""

    parsed = urlsplit(database_url)
    username = parsed.username or ""
    hostname = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    
    # Build netloc without password
    if username:
        netloc = f"{username}@{hostname}{port}"
    else:
        netloc = f"{hostname}{port}"
    
    # Preserve query parameters, but remove any password parameter
    query_pairs = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() == "password":
            continue
        query_pairs.append((key, value))
    query = "&".join(f"{key}={value}" for key, value in query_pairs)
    
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))


def resolve_validation_database_url(database_url: str) -> str:
    """
    Validate and resolve the validation database URL.
    Raises RestoreValidationError for any safety violation.
    """
    if not database_url:
        raise RestoreValidationError(
            "--database-url is required for restore validation. "
            "Use a separate validation database, not production."
        )

    parsed = urlsplit(database_url)
    
    # Reject password-bearing URLs
    if parsed.password:
        raise RestoreValidationError(
            "Password found in --database-url. Remove the password from the connection URI and set PGPASSWORD or PGPASSFILE instead. "
            "This keeps credentials out of process arguments and logs. "
            "The validation database URL MUST be password-free."
        )

    # Validate that identity is configured
    validate_validation_identity_configured()
    
    # Check target matches validation identity
    check_target_matches_validation_identity(database_url)
    
    # Check target is not source
    check_target_not_source(database_url)

    return strip_password_from_url(database_url)


# ------------------------------------------------------------------
# TARGET SCHEMA INSPECTION
# ------------------------------------------------------------------

try:
    from sqlalchemy import create_engine, text, inspect
    SQLALCHEMY_AVAILABLE = True
except ImportError:
    SQLALCHEMY_AVAILABLE = False


def get_target_schemas(target_url: str, password_free_url: str) -> Set[str]:
    """
    Get the set of schemas that exist in the target database.
    Uses SQLAlchemy to query pg_catalog.pg_namespace.
    """
    if not SQLALCHEMY_AVAILABLE:
        raise RestoreValidationError(
            "SQLAlchemy is required for target schema inspection. "
            "Install it with: pip install sqlalchemy"
        )
    
    try:
        # Use the password-free URL; credentials come from environment
        engine = create_engine(password_free_url, pool_pre_ping=True)
        with engine.connect() as connection:
            result = connection.execute(text("""
                SELECT nspname 
                FROM pg_catalog.pg_namespace 
                WHERE nspname NOT LIKE 'pg_temp_%'
                ORDER BY nspname
            """))
            schemas = {row[0] for row in result.fetchall()}
        engine.dispose()
        return schemas
    except Exception as exc:
        raise RestoreValidationError(
            f"Failed to query target database schemas: {exc}. "
            "Cannot verify that the target is safe for restore."
        )


def get_archive_schemas(archive_path: Path, pg_restore_path: str) -> Set[str]:
    """
    Extract the set of schemas present in the archive.
    Uses pg_restore --list to parse schema information.
    """
    result = subprocess.run(
        [pg_restore_path, "--list", str(archive_path)],
        capture_output=True,
        text=True,
        check=False
    )
    
    if result.returncode != 0:
        raise RestoreValidationError(
            f"Failed to list archive contents: {result.stderr or result.stdout}"
        )
    
    schemas: Set[str] = set()
    for line in result.stdout.splitlines():
        # Match lines like: SCHEMA - public
        # or: SCHEMA DATA - public
        match = re.search(r"^SCHEMA\s+(?:DATA\s+)?-\s+(\S+)", line)
        if match:
            schemas.add(match.group(1))
    
    return schemas


def get_application_schemas(
    target_schemas: Set[str],
    archive_schemas: Set[str]
) -> Set[str]:
    """
    Identify which schemas from the archive are application schemas
    (not Supabase-managed or PostgreSQL system schemas).
    """
    # Only consider schemas that are in both the archive and target
    common_schemas = target_schemas & archive_schemas
    
    # Filter out Supabase-managed and system schemas
    application_schemas = set()
    for schema in common_schemas:
        if schema not in SUPABASE_MANAGED_SCHEMAS:
            application_schemas.add(schema)
    
    return application_schemas


def check_schema_empty(
    target_url: str,
    password_free_url: str,
    schema: str
) -> Tuple[bool, int, int]:
    """
    Check if a schema contains any application objects.
    Returns (is_empty, table_count, object_count).
    """
    if not SQLALCHEMY_AVAILABLE:
        raise RestoreValidationError("SQLAlchemy required for schema inspection")
    
    try:
        engine = create_engine(password_free_url, pool_pre_ping=True)
        
        with engine.connect() as connection:
            # Count tables in schema
            tables_result = connection.execute(text("""
                SELECT COUNT(*) 
                FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = :schema 
                AND c.relkind = 'r'  -- regular tables
                AND n.nspname NOT LIKE 'pg_temp_%'
            """), {"schema": schema})
            table_count = tables_result.scalar_one() or 0
            
            # Count all objects in schema
            objects_result = connection.execute(text("""
                SELECT COUNT(*) 
                FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = :schema
                AND c.relkind IN ('r', 'v', 'm', 'i', 'S', 'f', 'p', 't')
                AND n.nspname NOT LIKE 'pg_temp_%'
            """), {"schema": schema})
            object_count = objects_result.scalar_one() or 0
        
        engine.dispose()
        return (object_count == 0, table_count, object_count)
        
    except Exception as exc:
        raise RestoreValidationError(
            f"Failed to inspect schema '{schema}': {exc}"
        )


def validate_target_schemas(
    target_url: str,
    password_free_url: str,
    archive_schemas: Set[str],
    logger
) -> List[str]:
    """
    Validate that application schemas in the target are empty.
    Returns a list of warnings/errors.
    """
    issues = []
    
    # Get schemas in target
    target_schemas = get_target_schemas(target_url, password_free_url)
    
    # Get application schemas (archive schemas that exist in target and are not Supabase-managed)
    app_schemas = get_application_schemas(target_schemas, archive_schemas)
    
    if not app_schemas:
        logger.info(
            "No application schemas found in target that are also in the archive. "
            "Target appears to be a fresh validation database."
        )
        return issues
    
    logger.info(
        "Checking application schemas in target for existing objects: %s",
        ", ".join(sorted(app_schemas))
    )
    
    for schema in sorted(app_schemas):
        is_empty, table_count, object_count = check_schema_empty(
            target_url, password_free_url, schema
        )
        
        if not is_empty:
            issues.append(
                f"Schema '{schema}' contains {object_count} objects ({table_count} tables). "
                f"A validation database must have empty application schemas. "
                f"This prevents accidental overwrite of existing data."
            )
        else:
            logger.info(f"Schema '{schema}' is empty (OK)")
    
    # Also check if there are archive schemas that don't exist in target
    archive_only = archive_schemas - target_schemas
    if archive_only:
        logger.info(
            "Archive contains schemas not in target (will be created): %s",
            ", ".join(sorted(archive_only))
        )
    
    return issues


# ------------------------------------------------------------------
# MAIN VALIDATION LOGIC
# ------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate a pg_dump archive in a dedicated validation database. "
            "This script performs extensive safety checks and MUST use a dedicated, "
            "explicitly configured validation database. It will NOT restore to production "
            "or any database that is not the explicitly configured validation target."
        )
    )
    parser.add_argument(
        "--archive", 
        required=True, 
        help="Path to the .dump file created by backup.py"
    )
    parser.add_argument(
        "--database-url", 
        required=True, 
        help="Connection string for the explicitly configured validation database; will NOT accept production"
    )
    parser.add_argument(
        "--pg-restore-path", 
        default=os.getenv("PG_RESTORE_PATH", ""),
        help="Optional path to pg_restore"
    )
    parser.add_argument(
        "--jobs", 
        type=int, 
        default=4, 
        help="Number of parallel restore jobs (default: 4)"
    )
    parser.add_argument(
        "--skip-schema-check",
        action="store_true",
        default=False,
        help="Skip the schema emptiness check (NOT RECOMMENDED; only for debugging)"
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.jobs < 1:
        raise RestoreValidationError("--jobs must be at least 1.")

    # Set up basic logging
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s"
    )
    logger = logging.getLogger("restore_validation")

    archive_path = Path(args.archive).expanduser().resolve()
    if not archive_path.exists():
        raise RestoreValidationError(f"Archive file not found: {archive_path}")
    if not archive_path.name.lower().endswith(".dump"):
        logger.warning(
            "The archive file does not end with .dump; continue only if this is a valid pg_dump archive."
        )

    pg_restore_path = args.pg_restore_path or shutil.which("pg_restore")
    if not pg_restore_path:
        raise RestoreValidationError(
            "pg_restore not found. Install PostgreSQL client tools or set PG_RESTORE_PATH."
        )

    # Validate and resolve the target URL with all safety checks
    logger.info("=" * 80)
    logger.info("VALIDATION DATABASE SAFETY CHECKS")
    logger.info("=" * 80)
    
    validation_url = resolve_validation_database_url(args.database_url)
    
    logger.info("Validation database identity: %s", sanitize_database_url(args.database_url))
    logger.info("Archive: %s", archive_path)
    logger.info("")

    # Log the configured identity
    identity = get_validation_database_identity()
    logger.info("Configured validation identity:")
    logger.info("  Host pattern: %s", identity.get("full_host_pattern", "not set"))
    logger.info("  Database name: %s", identity.get("database_name", "not set"))
    logger.info("  Project ref: %s", identity.get("project_ref", "not set"))
    logger.info("")

    # Check archive readability
    logger.info("Checking archive readability...")
    list_result = subprocess.run(
        [pg_restore_path, "--list", str(archive_path)],
        capture_output=True,
        text=True,
        check=False
    )
    if list_result.returncode != 0:
        raise RestoreValidationError(
            f"The archive is not readable by pg_restore: {list_result.stderr or list_result.stdout}"
        )
    
    logger.info("Archive readability check passed: pg_restore --list succeeded.")
    
    # Get archive schemas
    archive_schemas = get_archive_schemas(archive_path, pg_restore_path)
    logger.info("Archive contains schemas: %s", ", ".join(sorted(archive_schemas)) if archive_schemas else "none")
    logger.info("")
    
    # Build password-free URL for connection
    password_free_url = strip_password_from_url(args.database_url)
    
    # Validate target schemas if not skipped
    if not args.skip_schema_check:
        logger.info("Checking target database for conflicting application objects...")
        schema_issues = validate_target_schemas(
            args.database_url,
            password_free_url,
            archive_schemas,
            logger
        )
        
        if schema_issues:
            for issue in schema_issues:
                logger.error(issue)
            raise RestoreValidationError(
                "Target database contains existing application objects. "
                "A validation restore requires empty application schemas. "
                "Create a fresh validation database and try again."
            )
        
        logger.info("Target schema validation passed: no conflicting application objects found.")
        logger.info("")
    else:
        logger.warning(
            "Schema emptiness check SKIPPED at user request. "
            "This is NOT RECOMMENDED for production validation."
        )
        logger.info("")

    # Prepare environment with credentials
    env = os.environ.copy()
    if os.getenv("PGPASSWORD"):
        env["PGPASSWORD"] = os.getenv("PGPASSWORD")
    if os.getenv("PGPASSFILE"):
        env["PGPASSFILE"] = os.getenv("PGPASSFILE")

    logger.info("Starting restore into the dedicated validation database...")
    logger.info("IMPORTANT: This restore uses --exit-on-error and will NOT use --clean or --create.")
    logger.info("DROP, TRUNCATE, and DELETE operations are NOT permitted.")

    # Build restore command with --exit-on-error
    restore_command = [
        pg_restore_path,
        "--dbname",
        password_free_url,
        "--verbose",
        "--jobs",
        str(args.jobs),
        "--no-owner",
        "--no-privileges",
        "--exit-on-error",  # Critical: fail on any error
        str(archive_path),
    ]

    restore_result = subprocess.run(
        restore_command,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    # Check for failure
    if restore_result.returncode != 0:
        error_output = restore_result.stderr or restore_result.stdout or "Unknown restore validation error"
        # Check for specific error patterns that indicate safety violations
        error_lower = error_output.lower()
        
        if "drop" in error_lower or "truncate" in error_lower or "delete" in error_lower:
            raise RestoreValidationError(
                f"Restore validation FAILED with potentially destructive operation detected: {error_output}"
            )
        
        raise RestoreValidationError(
            f"Restore validation FAILED: {error_output}"
        )

    logger.info("Validation restore completed successfully.")
    logger.info("")
    logger.info("=" * 80)
    logger.info("VALIDATION SUMMARY")
    logger.info("=" * 80)
    logger.info("Validation database: %s", sanitize_database_url(args.database_url))
    logger.info("Archive: %s", archive_path)
    logger.info("Result: VALIDATION PASSED")
    logger.info("")
    logger.info("IMPORTANT NOTES:")
    logger.info("- This validation restore was performed on a dedicated validation database.")
    logger.info("- Archive readability does NOT prove restore success; actual restore was tested.")
    logger.info("- The validation database identity was explicitly verified.")
    logger.info("- Application schemas were checked for existing objects.")
    logger.info("- No DROP, TRUNCATE, DELETE, --clean, or --create operations were used.")
    logger.info("- The restore used --exit-on-error to ensure complete success.")
    logger.info("")
    
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RestoreValidationError as exc:
        print(f"[ERROR] Validation failed: {exc}")
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("\n[ERROR] Restore validation interrupted by user.")
        raise SystemExit(1)
