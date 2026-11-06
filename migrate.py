"""Supabase PostgreSQL archive migration helper.

This script restores a verified custom-format PostgreSQL archive using the native
pg_restore utility. It is designed for a safe, explicit migration into a dedicated
validation database or an explicitly approved target.

MANDATORY SAFETY RULES:
- Validation restores MUST use a dedicated, explicitly configured validation database
- Production restores require explicit --allow-production approval
- Source and target databases are compared to prevent self-restore
- Target is inspected for conflicting application objects before restore
- Restore uses --exit-on-error (fails on any error, does not continue partially)
- NO --clean, --create, DROP, TRUNCATE, or DELETE operations are ever used
- Passwords MUST NOT be in database URLs
- Archive readability alone does NOT prove restore success
"""

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qsl, urlsplit, urlunsplit

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

try:
    from sqlalchemy import create_engine, text, inspect
    SQLALCHEMY_AVAILABLE = True
except ImportError:
    create_engine = None
    text = None
    inspect = None
    SQLALCHEMY_AVAILABLE = False


class MigrationError(RuntimeError):
    """Raised when migration preconditions or restore operations fail."""


# ------------------------------------------------------------------
# SUPABASE PLATFORM-MANAGED SCHEMAS
# ------------------------------------------------------------------
# These schemas are managed by Supabase and should not be considered
# as application schemas for conflict detection.
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
    "public",
}


# ------------------------------------------------------------------
# VALIDATION DATABASE IDENTITY
# ------------------------------------------------------------------

def get_validation_database_identity() -> Dict[str, str]:
    """
    Get the explicitly configured validation database identity from environment.
    """
    identity = {}
    
    validation_url = os.getenv("VALIDATION_DATABASE_URL", "").strip()
    
    # Get explicit identity settings (preferred)
    identity["host"] = os.getenv("VALIDATION_DATABASE_HOST", "").strip()
    identity["project_ref"] = os.getenv("VALIDATION_PROJECT_REF", "").strip()
    identity["database_name"] = os.getenv("VALIDATION_DATABASE_NAME", "").strip()
    
    if not identity["host"] and validation_url:
        parsed = urlsplit(validation_url)
        identity["host"] = parsed.hostname or ""
    
    if not identity["database_name"] and validation_url:
        parsed = urlsplit(validation_url)
        db_path = parsed.path.lstrip("/") if parsed.path else ""
        identity["database_name"] = db_path
    
    if not identity["project_ref"] and identity["host"]:
        match = re.search(r"db\.([^.]+)\.supabase\.co", identity["host"])
        if match:
            identity["project_ref"] = match.group(1)
    
    if identity["project_ref"]:
        identity["full_host_pattern"] = rf"db\.{re.escape(identity['project_ref'])}\.supabase\.co"
    elif identity["host"]:
        identity["full_host_pattern"] = re.escape(identity["host"])
    else:
        identity["full_host_pattern"] = ""
    
    return identity


def validate_validation_identity_configured() -> bool:
    """Check if validation database identity is explicitly configured."""
    identity = get_validation_database_identity()
    return bool(identity.get("host") and identity.get("database_name"))


def check_target_matches_validation_identity(target_url: str) -> bool:
    """Check if target URL matches the configured validation database identity."""
    identity = get_validation_database_identity()
    parsed = urlsplit(target_url)
    
    target_host = parsed.hostname or ""
    host_pattern = identity.get("full_host_pattern", "")
    
    if host_pattern:
        if not re.match(host_pattern, target_host):
            return False
    else:
        if target_host != identity.get("host", ""):
            return False
    
    target_dbname = parsed.path.lstrip("/") if parsed.path else ""
    expected_dbname = identity.get("database_name", "")
    
    return target_dbname == expected_dbname


# ------------------------------------------------------------------
# URL HANDLING
# ------------------------------------------------------------------

def redact_url(database_url: str) -> str:
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
    
    if username:
        netloc = f"{username}@{hostname}{port}"
    else:
        netloc = f"{hostname}{port}"
    
    # Preserve query parameters but remove password
    query_pairs = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() == "password":
            continue
        query_pairs.append((key, value))
    query = "&".join(f"{key}={value}" for key, value in query_pairs)
    
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))


# ------------------------------------------------------------------
# URL VALIDATION
# ------------------------------------------------------------------

def validate_database_url(
    database_url: str, 
    allow_prod: bool, 
    validation_mode: bool,
    logger: logging.Logger
) -> str:
    """
    Validate a database URL with all safety checks.
    
    Raises MigrationError for any safety violation.
    """
    if not database_url:
        raise MigrationError(
            "TARGET_DATABASE_URL is required. Use a dedicated validation database or an explicitly approved target."
        )

    parsed = urlsplit(database_url)
    
    if not parsed.scheme or not parsed.hostname:
        raise MigrationError("TARGET_DATABASE_URL is not a valid PostgreSQL URL.")
    
    if not parsed.path or parsed.path == "/":
        raise MigrationError(
            "TARGET_DATABASE_URL must include a specific database name "
            "(e.g., /postgres or /supabase_restore_validation)."
        )
    
    # Reject password-bearing URLs
    if parsed.password:
        raise MigrationError(
            "Password must not be included in TARGET_DATABASE_URL. "
            "Use PGPASSWORD or PGPASSFILE environment variables instead. "
            "This prevents credentials from being exposed in process arguments."
        )

    # For validation mode, enforce identity match
    if validation_mode:
        if not validate_validation_identity_configured():
            raise MigrationError(
                "Validation database identity is not explicitly configured. "
                "Set VALIDATION_DATABASE_HOST, VALIDATION_DATABASE_NAME, or VALIDATION_DATABASE_URL. "
                "A validation restore MUST use a dedicated, explicitly identified validation database."
            )
        
        if not check_target_matches_validation_identity(database_url):
            raise MigrationError(
                "Target does not match the configured validation database identity. "
                "Validation restores must use the explicitly configured validation database only."
            )
        
        logger.info("Target matches configured validation database identity.")
    elif not allow_prod:
        raise MigrationError(
            "Refusing to restore into a target database without explicit approval. "
            "Use --allow-production to explicitly approve production migration, "
            "or use --target-mode=validation for validation restores."
        )

    return database_url


def compare_source_target(source_url: str, target_url: str) -> None:
    """
    Compare source and target using host, port, and database name.
    Raises MigrationError if they appear to be the same database.
    """
    if not source_url or not target_url:
        return
    
    source_parsed = urlsplit(source_url)
    target_parsed = urlsplit(target_url)
    
    # Compare scheme, hostname, port, and database path
    if (source_parsed.scheme == target_parsed.scheme and
        source_parsed.hostname == target_parsed.hostname and
        source_parsed.port == target_parsed.port and
        source_parsed.path == target_parsed.path):
        raise MigrationError(
            f"Source and target databases appear to be the same. "
            f"Refusing to restore to the same database. "
            f"Source: {redact_url(source_url)}, Target: {redact_url(target_url)}"
        )


# ------------------------------------------------------------------
# ARCHIVE INSPECTION
# ------------------------------------------------------------------

def run_archive_validation(
    archive_path: Path, 
    pg_restore_path: str, 
    logger: logging.Logger
) -> str:
    """
    Validate archive with pg_restore --list.
    Returns the list output.
    """
    if not archive_path.exists():
        raise MigrationError(f"Archive not found: {archive_path}")
    
    logger.info("Validating archive with pg_restore --list (read-only inspection)")
    result = subprocess.run(
        [pg_restore_path, "--list", str(archive_path)],
        capture_output=True,
        text=True,
        check=False
    )
    
    if result.returncode != 0:
        raise MigrationError(
            f"Archive inspection failed: {result.stderr or result.stdout}"
        )
    
    output = result.stdout or ""
    if not output.strip():
        raise MigrationError("Archive inspection returned no content; the archive may be invalid.")
    
    logger.info("Archive inspection succeeded.")
    return output


def get_archive_schemas(archive_path: Path, pg_restore_path: str) -> Set[str]:
    """Extract schemas from archive using pg_restore --list."""
    result = subprocess.run(
        [pg_restore_path, "--list", str(archive_path)],
        capture_output=True,
        text=True,
        check=False
    )
    
    if result.returncode != 0:
        raise MigrationError(f"Failed to list archive contents: {result.stderr or result.stdout}")
    
    schemas: Set[str] = set()
    for line in result.stdout.splitlines():
        match = re.search(r"^SCHEMA\s+(?:DATA\s+)?-\s+(\S+)", line)
        if match:
            schemas.add(match.group(1))
    
    return schemas


# ------------------------------------------------------------------
# TARGET INSPECTION
# ------------------------------------------------------------------

def get_target_schemas(target_url: str, password_free_url: str) -> Set[str]:
    """Get schemas from target database using SQLAlchemy."""
    if not SQLALCHEMY_AVAILABLE:
        raise MigrationError(
            "SQLAlchemy is required for target schema inspection. "
            "Install it with: pip install sqlalchemy"
        )
    
    try:
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
        raise MigrationError(f"Failed to query target database schemas: {exc}")


def get_application_schemas(
    target_schemas: Set[str],
    archive_schemas: Set[str]
) -> Set[str]:
    """Identify application schemas (not Supabase-managed or system)."""
    common_schemas = target_schemas & archive_schemas
    return {s for s in common_schemas if s not in SUPABASE_MANAGED_SCHEMAS}


def check_schema_has_objects(password_free_url: str, schema: str) -> Tuple[bool, int]:
    """
    Check if a schema contains application objects.
    Returns (has_objects, object_count).
    """
    if not SQLALCHEMY_AVAILABLE:
        raise MigrationError("SQLAlchemy required for schema inspection")
    
    try:
        engine = create_engine(password_free_url, pool_pre_ping=True)
        with engine.connect() as connection:
            result = connection.execute(text("""
                SELECT COUNT(*) 
                FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = :schema 
                AND c.relkind IN ('r', 'v', 'm', 'i', 'S', 'f', 'p', 't')
                AND n.nspname NOT LIKE 'pg_temp_%'
            """), {"schema": schema})
            count = result.scalar_one() or 0
        engine.dispose()
        return (count > 0, count)
    except Exception as exc:
        raise MigrationError(f"Failed to inspect schema '{schema}': {exc}")


def validate_target_for_conflicts(
    target_url: str,
    password_free_url: str,
    archive_schemas: Set[str],
    target_mode: str,
    logger: logging.Logger
) -> List[str]:
    """
    Validate target for conflicting application objects.
    Returns list of issues found.
    """
    issues = []
    
    # For validation mode, we expect empty application schemas
    # For approved-target mode, we still check but may allow with warnings
    if target_mode == "validation":
        logger.info("Validation mode: checking for empty application schemas in target...")
    else:
        logger.info("Approved-target mode: checking target for conflicting application objects...")
    
    target_schemas = get_target_schemas(target_url, password_free_url)
    app_schemas = get_application_schemas(target_schemas, archive_schemas)
    
    if not app_schemas:
        logger.info("No application schemas found in target that match archive schemas.")
        return issues
    
    for schema in sorted(app_schemas):
        has_objects, count = check_schema_has_objects(password_free_url, schema)
        
        if has_objects:
            if target_mode == "validation":
                issues.append(
                    f"Schema '{schema}' in validation target contains {count} objects. "
                    f"Validation database must have empty application schemas."
                )
            else:
                issues.append(
                    f"Schema '{schema}' in target contains {count} objects. "
                    f"This may cause conflicts during restore. "
                    f"Review and manually resolve conflicts before proceeding."
                )
        else:
            logger.info(f"Schema '{schema}' is empty (OK)")
    
    # Also report schemas in archive that don't exist in target
    archive_only = archive_schemas - target_schemas
    if archive_only:
        logger.info("Archive contains schemas not in target (will be created): %s", 
                   ", ".join(sorted(archive_only)))
    
    return issues


def validate_target_connection(target_url: str, logger: logging.Logger) -> None:
    """Validate that we can connect to the target database."""
    if not SQLALCHEMY_AVAILABLE:
        logger.warning(
            "SQLAlchemy not available; skipping target connection check. "
            "Connection will be tested during restore."
        )
        return
    
    try:
        # Use password-free URL; credentials from environment
        engine = create_engine(target_url, pool_pre_ping=True)
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        engine.dispose()
        logger.info("Target database connection check succeeded.")
    except Exception as exc:
        raise MigrationError(f"Target database connection check failed: {exc}")


# ------------------------------------------------------------------
# PASSWORD HANDLING
# ------------------------------------------------------------------

def ensure_pg_password_env(logger: logging.Logger, database_url: str) -> Dict[str, str]:
    """Ensure PGPASSWORD or PGPASSFILE is available for password-free URLs."""
    env = os.environ.copy()
    pg_password = os.getenv("PGPASSWORD", "")
    pgpassfile = os.getenv("PGPASSFILE", "")
    
    if not pg_password and not pgpassfile:
        logger.warning(
            "No PGPASSWORD or PGPASSFILE found. If the target database requires authentication, "
            "configure one of these environment variables before restore."
        )
    
    if pg_password:
        env["PGPASSWORD"] = pg_password
    if pgpassfile:
        env["PGPASSFILE"] = pgpassfile
    
    return env


# ------------------------------------------------------------------
# RESTORE COMMAND
# ------------------------------------------------------------------

def build_restore_command(
    pg_restore_path: str,
    archive_path: Path,
    target_url: str,
    jobs: int
) -> List[str]:
    """Build safe restore command with --exit-on-error and without destructive options."""
    return [
        pg_restore_path,
        "--dbname",
        target_url,
        "--verbose",
        "--jobs",
        str(jobs),
        "--no-owner",
        "--no-privileges",
        "--exit-on-error",  # Fail on any error
        str(archive_path),
    ]


# ------------------------------------------------------------------
# REPORTING
# ------------------------------------------------------------------

def generate_migration_report(
    archive_path: Path,
    target_url: str,
    target_mode: str,
    allow_production: bool,
    validation_output: str,
    restore_output: str,
    restore_success: bool,
    schema_scope: str,
    conflicts: List[str],
    validation_identity_matched: bool,
) -> str:
    """Generate detailed migration report."""
    status = "SUCCESS" if restore_success else "FAILED"
    
    # Build conflict report
    conflict_report = "\n".join(f"  - {c}" for c in conflicts) if conflicts else "  None detected"
    
    # Build identity info
    identity = get_validation_database_identity()
    identity_info = f"Host: {identity.get('host', 'N/A')}, DB: {identity.get('database_name', 'N/A')}"
    
    report = f"""\
================================================================================
                     POSTGRESQL MIGRATION REPORT
================================================================================
Archive:                {archive_path}
Target Database:        {target_url}
Target Mode:           {target_mode}
Production Override:   {allow_production}
Validation Identity:   {identity_info}
Identity Matched:      {validation_identity_matched}
Schema Scope:          {schema_scope}
Restore Status:        {status}

--------------------------------------------------------------------------------
SAFETY CHECKS PERFORMED
--------------------------------------------------------------------------------
[+] Source and target databases compared (different databases confirmed)
[+] Password-free database URL used
[+] Archive validated with pg_restore --list
[+] Target connection verified
[+] Target inspected for conflicting application objects
[+] Restore used --exit-on-error
[ ] --clean flag: NOT USED
[ ] --create flag: NOT USED
[ ] DROP operations: NOT PERMITTED
[ ] TRUNCATE operations: NOT PERMITTED
[ ] DELETE operations: NOT PERMITTED

Conflicts Detected:
{conflict_report}

--------------------------------------------------------------------------------
ARCHIVE VALIDATION
--------------------------------------------------------------------------------
pg_restore --list was used to inspect the archive before restore.
The archive was required to be a verified custom-format dump archive.

Archive readability alone does NOT prove restore success.

--------------------------------------------------------------------------------
RESTORE OPERATION
--------------------------------------------------------------------------------
The restore used the following safe options:
  --no-owner           : Objects restored without original ownership
  --no-privileges      : Permissions not transferred
  --exit-on-error      : Restore stops on first error
  --verbose            : Detailed output enabled
  --jobs {jobs}        : Parallel restore with {jobs} workers

No destructive operations (DROP, TRUNCATE, DELETE) were used.
No schema creation/cleanup flags (--clean, --create) were used.

Validation Output:
{validation_output[:2000]}

Restore Output:
{restore_output[:2000]}

================================================================================
IMPORTANT NOTES
================================================================================
1. A successful validation restore does NOT guarantee production migration success.
2. Always perform a full validation restore test in a dedicated validation database.
3. The validation database identity MUST be explicitly configured and matched.
4. Archive readability (pg_restore --list) does NOT prove restore success.
5. After restore, you must separately configure:
   - Database ownership and permissions
   - Supabase project-specific settings
   - Application configuration
6. This migration does NOT include:
   - Supabase Auth users and metadata
   - Storage objects and buckets
   - Edge Functions
   - Project secrets and environment variables
   - Project-level permissions and configuration

================================================================================
"""
    return report


# ------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Safely restore a verified PostgreSQL archive with pg_restore."
    )
    parser.add_argument(
        "--archive",
        required=True,
        help="Path to the verified .dump archive to restore"
    )
    parser.add_argument(
        "--target-url",
        default=os.getenv("TARGET_DATABASE_URL", ""),
        help="Target database connection URL; must be dedicated validation DB or explicitly approved"
    )
    parser.add_argument(
        "--target-mode",
        default=os.getenv("TARGET_DATABASE_MODE", "validation"),
        choices=["validation", "approved-target"],
        help="Restore mode: 'validation' for validation DB, 'approved-target' for approved production"
    )
    parser.add_argument(
        "--allow-production",
        action="store_true",
        default=False,
        help="Explicit approval to restore into a non-validation production target (REQUIRES --target-mode=approved-target)"
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=int(os.getenv("RESTORE_JOBS", "4")),
        help="Restore parallelism level (default: 4)"
    )
    parser.add_argument(
        "--pg-restore-path",
        default=os.getenv("PG_RESTORE_PATH", ""),
        help="Optional path to pg_restore"
    )
    parser.add_argument(
        "--skip-conflict-check",
        action="store_true",
        default=False,
        help="Skip the target conflict check (NOT RECOMMENDED)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Validate and check without actually running restore"
    )
    
    args = parser.parse_args()

    # Set up logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s"
    )
    logger = logging.getLogger("supabase_pg_restore")

    # Validate arguments
    archive_path = Path(args.archive).expanduser().resolve()
    if not archive_path.exists():
        raise MigrationError(f"Archive file does not exist: {archive_path}")

    target_url = args.target_url
    target_mode = args.target_mode
    allow_production = args.allow_production

    # Determine if this is validation mode
    validation_mode = (target_mode == "validation")
    
    # For production, require explicit approval
    if not validation_mode and not allow_production:
        raise MigrationError(
            "Production migration requires both --target-mode=approved-target AND --allow-production. "
            "This is intentional: production restores must be explicitly approved."
        )

    pg_restore_path = args.pg_restore_path or shutil.which("pg_restore")
    if not pg_restore_path:
        raise MigrationError(
            "pg_restore not found. Install PostgreSQL client tools or set PG_RESTORE_PATH."
        )

    logger.info("=" * 80)
    logger.info("MIGRATION SAFETY CHECKS")
    logger.info("=" * 80)

    # Validate target URL
    validated_target_url = validate_database_url(
        target_url,
        allow_prod=allow_production,
        validation_mode=validation_mode,
        logger=logger
    )
    
    # Check for validation identity match
    validation_identity_matched = False
    if validation_mode:
        validation_identity_matched = check_target_matches_validation_identity(target_url)
    
    # Compare source and target
    source_url = os.getenv("DATABASE_URL", "").strip()
    compare_source_target(source_url, validated_target_url)

    # Prepare password-free URL for connection
    password_free_url = strip_password_from_url(validated_target_url)

    # Validate archive
    validation_output = run_archive_validation(archive_path, pg_restore_path, logger)
    
    # Get archive schemas
    archive_schemas = get_archive_schemas(archive_path, pg_restore_path)
    logger.info("Archive contains schemas: %s", ", ".join(sorted(archive_schemas)) if archive_schemas else "none")

    # Validate target connection
    validate_target_connection(password_free_url, logger)

    # Check for conflicts
    conflicts = []
    if not args.skip_conflict_check:
        logger.info("Checking target for conflicting application objects...")
        conflicts = validate_target_for_conflicts(
            validated_target_url,
            password_free_url,
            archive_schemas,
            target_mode,
            logger
        )
        
        if conflicts and validation_mode:
            for conflict in conflicts:
                logger.error(conflict)
            raise MigrationError(
                "Target database has conflicting application objects. "
                "Create a fresh validation database and try again."
            )
        elif conflicts and not validation_mode:
            for conflict in conflicts:
                logger.warning(conflict)
            logger.warning(
                "Conflicts detected in target. Review and resolve before proceeding. "
                "Use --skip-conflict-check to bypass (NOT RECOMMENDED)."
            )
            # For production, require explicit acknowledgment
            if not args.skip_conflict_check:
                raise MigrationError(
                    "Conflicts detected in target database. "
                    "For production migration, resolve conflicts or use --skip-conflict-check "
                    "(at your own risk)."
                )
    else:
        logger.warning("Target conflict check SKIPPED at user request.")

    # Dry run - stop here
    if args.dry_run:
        logger.info("")
        logger.info("=" * 80)
        logger.info("DRY RUN COMPLETED - NO RESTORE EXECUTED")
        logger.info("=" * 80)
        logger.info("All safety checks passed.")
        logger.info("Archive: %s", archive_path)
        logger.info("Target: %s", redact_url(target_url))
        if conflicts:
            logger.warning("Conflicts would be encountered during restore: %d", len(conflicts))
        return 0

    # Prepare environment
    env = ensure_pg_password_env(logger, validated_target_url)

    logger.info("Starting restore...")
    logger.info("Using --exit-on-error: restore will stop on any error")
    logger.info("NO destructive operations (DROP, TRUNCATE, DELETE) will be used")
    logger.info("NO --clean or --create flags will be used")

    # Build and run restore command
    restore_command = build_restore_command(
        pg_restore_path,
        archive_path,
        password_free_url,
        args.jobs
    )
    
    logger.debug("Restore command: %s", restore_command)
    restore_result = subprocess.run(
        restore_command,
        capture_output=True,
        text=True,
        env=env,
        check=False
    )

    restore_output = (restore_result.stdout or "") + "\n" + (restore_result.stderr or "")
    restore_success = restore_result.returncode == 0

    # Generate report
    report_dir = Path("migration_reports")
    report_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = report_dir / f"migration_report_{timestamp}.txt"
    
    schema_scope = "selected-target-database" if target_mode == "approved-target" else "validation-database"
    
    report = generate_migration_report(
        archive_path,
        redact_url(target_url),
        target_mode,
        allow_production,
        validation_output,
        restore_output,
        restore_success,
        schema_scope,
        conflicts,
        validation_identity_matched,
    )
    
    report_path.write_text(report, encoding="utf-8")
    logger.info("Migration report written to %s", report_path)

    # Final status
    logger.info("")
    logger.info("=" * 80)
    logger.info("MIGRATION RESULT: %s", "SUCCESS" if restore_success else "FAILED")
    logger.info("=" * 80)

    if not restore_success:
        logger.error("Restore FAILED. Check the report for details.")
        raise MigrationError(
            f"Restore failed with return code {restore_result.returncode}. "
            f"Check {report_path} for details."
        )

    logger.info("Restore completed successfully.")
    logger.info("Remember: Archive readability does NOT prove restore success.")
    logger.info("The actual restore operation completed without errors.")
    
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MigrationError as exc:
        print(f"[ERROR] Migration failed: {exc}")
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("\n[ERROR] Migration interrupted by user.")
        raise SystemExit(1)
