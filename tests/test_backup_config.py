"""GZ-086: guards for the nightly production backup, which runs unattended with read access
to every rider. The files are read as text, like tests/test_deploy_config.py; CI's
migrations job runs the two scripts for real (a dump as gize_backup, restored and checked).
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
BACKUP = (WORKFLOWS / "backup-production.yml").read_text()
CI = (WORKFLOWS / "ci.yml").read_text()
DUMP = (ROOT / "ops" / "backup" / "dump.sh").read_text()
VERIFY = (ROOT / "ops" / "backup" / "verify.sh").read_text()
RESTORE = (ROOT / "ops" / "backup" / "restore.sh").read_text()
LIB = (ROOT / "ops" / "backup" / "lib.sh").read_text()
# The dump's first step (tests/db/test_backup_check.py runs it against real tables).
READABLE_CHECK = "pg_get_expr(p.polqual, p.polrelid) = 'true'"
COMPOSE = (ROOT / "docker-compose.yml").read_text()


def _steps(workflow: str) -> list[str]:
    steps = workflow.split("\n    steps:\n", 1)[1]
    return re.split(r"\n      - ", "\n" + steps)[1:]


def _step(name: str) -> str:
    matches = [step for step in _steps(BACKUP) if step.startswith(f"name: {name}")]
    assert len(matches) == 1, name
    return matches[0]


# When and where it runs


def test_it_runs_nightly_and_on_demand_from_main_only() -> None:
    assert re.search(r'^  schedule:\n    - cron: "0 23 \* \* \*"  #', BACKUP, re.MULTILINE)
    assert re.search(r"^  workflow_dispatch:$", BACKUP, re.MULTILINE)
    assert "if: github.ref == 'refs/heads/main'" in BACKUP
    # Its own environment: production's requires an approval, which a night has no one for.
    assert re.findall(r"^    environment: (\S+)", BACKUP, re.MULTILINE) == ["production-backup"]
    assert "group: backup-production" in BACKUP
    assert "cancel-in-progress: false" in BACKUP


def test_it_holds_no_more_power_than_it_needs() -> None:
    assert re.findall(r"^permissions:\n  (.+)$", BACKUP, re.MULTILINE) == ["contents: read"]
    assert BACKUP.count("permissions:") == 1
    assert "persist-credentials: false" in BACKUP
    assert set(re.findall(r"secrets\.([A-Z_]+)", BACKUP)) == {"PRODUCTION_BACKUP_DATABASE_URL"}
    assert set(re.findall(r"vars\.([A-Z_]+)", BACKUP)) == {"PRODUCTION_BACKUP_AGE_RECIPIENT"}
    for line in BACKUP.splitlines():
        if ("secrets." in line or "vars." in line) and not line.lstrip().startswith("#"):
            # Only into a step's environment: never into a command line or a log.
            assert re.fullmatch(r"\s+[A-Z_]+: \$\{\{ (secrets|vars)\.[A-Z_]+ \}\}", line), line
    for forbidden in ("MIGRATOR", "STAGING", "DEPLOY_HOOK", "set -x"):
        assert forbidden not in BACKUP, forbidden


# What it does, in order


def test_it_dumps_then_restores_then_encrypts_then_uploads() -> None:
    names = [step.splitlines()[0] for step in _steps(BACKUP)]
    order = [
        "name: Install age",
        "name: Check the backup settings are set",
        "name: Dump the database (as gize_backup)",
        "name: Restore the copy and check it",
        "name: Encrypt the copy",
        "uses: actions/upload-artifact@",
    ]
    positions = [next(i for i, name in enumerate(names) if name.startswith(o)) for o in order]
    assert positions == sorted(positions)
    assert "ops/backup/dump.sh" in _step("Dump the database")
    assert "ops/backup/verify.sh" in _step("Restore the copy and check it")


def test_only_the_encrypted_copy_leaves_the_runner() -> None:
    encrypt = _step("Encrypt the copy")
    assert 'age --encrypt --recipient "${RECIPIENT}"' in encrypt
    assert 'rm -f "${PLAIN}"' in encrypt  # the plain copy is gone before the upload
    upload = next(step for step in _steps(BACKUP) if "actions/upload-artifact@" in step)
    assert re.search(r"^          path: \$\{\{ runner\.temp \}\}/encrypted/\*\.age$", upload,
                     re.MULTILINE)  # fmt: skip
    assert "retention-days: 90" in upload
    assert "if-no-files-found: error" in upload
    cleanup = _step("Remove the plain copy")
    assert "if: always()" in cleanup
    assert 'rm -rf "${RUNNER_TEMP}/plain"' in cleanup


def test_the_recipient_is_an_age_public_key() -> None:
    check = _step("Check the backup settings are set")
    assert "^age1[0-9a-z]{58}$" in check
    assert "PRODUCTION_BACKUP_DATABASE_URL" in check


def test_age_is_pinned_by_version_and_checksum() -> None:
    install = _step("Install age")
    version = re.search(r'AGE_VERSION: "(v[\d.]+)"', install)
    digest = re.search(r'AGE_SHA256: "([0-9a-f]{64})"', install)
    assert version is not None
    assert digest is not None
    assert (
        "https://github.com/FiloSottile/age/releases/download/"
        "${AGE_VERSION}/age-${AGE_VERSION}-linux-amd64.tar.gz" in install
    )
    assert "sha256sum --check --strict" in install


def test_the_restore_check_keeps_rows_out_of_the_public_log() -> None:
    """Postgres's messages can quote rows (a COPY line, a duplicate key), and this
    repository's workflow logs are public: the check's server is ours, logging nothing
    below FATAL (GitHub prints a service container's log), and its messages go to a file
    that's removed with the plain copy."""
    assert "services:" not in BACKUP
    scratch = _step("Start a throwaway Postgres for the check")
    assert "source ops/backup/lib.sh" in scratch
    assert '--publish 127.0.0.1:5432:5432 "${POSTGRES_IMAGE}"' in scratch
    assert "-c log_min_messages=fatal -c log_min_error_statement=panic" in scratch
    restore = _step("Restore the copy and check it")
    assert "SCRATCH_URL: postgresql://postgres:scratch-only@127.0.0.1:5432/postgres" in restore
    assert '2> "${RUNNER_TEMP}/plain/verify.log"' in restore
    assert "docker rm --force gize-scratch" in _step("Remove the plain copy")


def test_the_password_is_masked_on_its_own() -> None:
    check = _step("Check the backup settings are set")
    assert "pg_connection DUMP_URL" in check
    assert 'echo "::add-mask::${PGPASSWORD}"' in check


# The scripts, run nightly and in CI


def test_the_scripts_use_the_pinned_postgres_17_image() -> None:
    compose = re.search(r"image: (postgres:[^\s@]+@sha256:[0-9a-f]{64})", COMPOSE)
    assert compose is not None
    assert f'POSTGRES_IMAGE="{compose.group(1)}"' in LIB
    for script in (DUMP, VERIFY, RESTORE):
        assert script.startswith("#!/usr/bin/env bash\n")
        assert "\nset -euo pipefail\n" in script
        assert 'source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"' in script
        assert "POSTGRES_IMAGE=" not in script  # one pin, in lib.sh
        assert "docker run" not in script  # always through pg_docker_run
    for text in (LIB, DUMP, VERIFY, RESTORE):
        assert "set -x" not in text


def test_the_tools_get_the_connection_from_their_environment() -> None:
    run = LIB.split("pg_docker_run() {", 1)[1]
    assert "-e PGURL -e PGPASSWORD -e PGCONNECT_TIMEOUT=10" in run
    assert '-v "${mount}" "${POSTGRES_IMAGE}" "$@"' in run


def _connection(url: str) -> subprocess.CompletedProcess[str]:
    """pg_connection, run by bash as the scripts run it."""
    return subprocess.run(  # noqa: S603 - fixed arguments
        [  # noqa: S607 - bash from PATH, as the scripts' shebang
            "bash",
            "-c",
            'set -euo pipefail; source "$1"; pg_connection URL;'
            ' printf "%s|%s" "$PGURL" "$PGPASSWORD"',
            "bash",
            str(ROOT / "ops" / "backup" / "lib.sh"),
        ],
        env={"PATH": os.environ["PATH"], "URL": url},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("url", "pgurl"),
    [
        (
            "postgresql://gize_backup.ref:s3cret@pooler.supabase.com:5432/postgres?ssl=verify-full",
            "postgresql://gize_backup.ref@pooler.supabase.com:5432/postgres?sslmode=verify-full",
        ),
        (
            "postgresql://gize_backup.ref:s3cret@pooler.supabase.com:5432/postgres?a=1&ssl=verify-full",
            "postgresql://gize_backup.ref@pooler.supabase.com:5432/postgres?a=1&sslmode=verify-full",
        ),
        (
            "postgresql://gize_backup:s3cret@127.0.0.1:5432/gize",
            "postgresql://gize_backup@127.0.0.1:5432/gize",
        ),
    ],
)
def test_the_password_travels_apart_from_the_url(url: str, pgurl: str) -> None:
    result = _connection(url)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{pgurl}|s3cret"


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("postgresql://gize_backup:s3cret@db.example.com:5432/postgres", "ssl=verify-full"),
        ("postgresql://gize_backup:s3cret@db.example.com/postgres?ssl=require", "ssl=verify-full"),
        ("gize_backup:s3cret", "must look like postgresql://"),
        ("", "set URL"),
    ],
)
def test_a_remote_server_must_be_verified(url: str, message: str) -> None:
    result = _connection(url)
    assert result.returncode == 2
    assert message in result.stderr
    assert "s3cret" not in result.stderr


def test_the_dump_is_the_gize_schema_read_through_its_policies() -> None:
    for option in (
        "--schema=gize",
        "--format=custom",
        "--enable-row-security",
        "--lock-wait-timeout=5s",
        "--no-password",
    ):
        assert option in DUMP, option
    assert "pg_connection DUMP_URL" in DUMP
    # Through RLS a table without the read policy would dump short: checked first.
    assert DUMP.index(READABLE_CHECK) < DUMP.index("exec pg_dump")
    assert "Nothing was dumped." in DUMP


def test_the_restore_check_is_strict() -> None:
    for check in (
        "ON_ERROR_STOP=1",
        "--single-transaction",
        "gize.alembic_version",
        "TABLE DATA gize",
    ):
        assert check in VERIFY, check
    assert "pg_connection SCRATCH_URL" in VERIFY


@pytest.mark.parametrize(
    ("line", "dropped"),
    [
        (
            "ALTER TABLE ONLY gize.alembic_version ALTER COLUMN version_num SET"
            " (pg_attver=302045696);",
            True,
        ),
        ('ALTER TABLE ONLY gize.riders ALTER COLUMN "from" SET (pg_attver=1);', True),
        ("ALTER TABLE ONLY gize.riders ALTER COLUMN name SET (n_distinct=100);", False),
        ("ALTER TABLE ONLY gize.riders ADD CONSTRAINT riders_pkey PRIMARY KEY (id);", False),
        ("x\tALTER TABLE ONLY gize.riders ALTER COLUMN a SET (pg_attver=1);", False),
    ],
)
def test_the_check_leaves_out_only_supabase_s_column_stamp(line: str, dropped: bool) -> None:
    """Supabase stamps each column with pg_attver, which plain Postgres refuses (found on
    the first production run): the check's restore leaves out exactly those lines, never a
    data row (COPY rows are tab-separated, so never a whole line like this)."""
    pattern = re.search(r"grep -vE '(\^ALTER TABLE ONLY gize[^']+)'", VERIFY)
    assert pattern is not None
    assert bool(re.search(pattern.group(1), line)) is dropped
    assert 'psql -X --dbname="$target" --single-transaction -v ON_ERROR_STOP=1' in VERIFY


def test_restoring_replaces_the_schema_s_contents_in_one_transaction() -> None:
    for option in ("--clean", "--if-exists", "--single-transaction", "--exit-on-error"):
        assert option in RESTORE, option
    # gize_migrator owns gize but may not create schemas: the schema itself stays.
    assert "grep -vE '^[0-9]+; [0-9]+ [0-9]+ SCHEMA - gize '" in RESTORE
    assert "--use-list=/tmp/contents" in RESTORE
    assert "pg_connection RESTORE_URL" in RESTORE


def test_restoring_asks_first_and_never_over_another_migration() -> None:
    """--clean drops only what the copy holds: over a newer schema, newer objects would stay
    (or block the drops). So the versions must match, and a person must type restore."""
    refuse = RESTORE.index('if [ -n "${current}" ] && [ "${current}" != "${copy}" ]; then')
    ask = RESTORE.index("read -r -p 'Type restore to go ahead: ' answer < /dev/tty")
    replace = RESTORE.index("--use-list=/tmp/contents")
    assert refuse < ask < replace
    assert 'if [ "${RESTORE_CONFIRM:-}" != "restore" ]; then' in RESTORE
    assert "This replaces everything in schema gize on ${where}." in RESTORE


def test_ci_backs_up_and_restores_on_every_change() -> None:
    migrations = CI.split("\n  migrations:\n", 1)[1].split("\n  docker:\n", 1)[0]
    assert "BACKUP_DATABASE_URL: postgresql://gize_backup:" in migrations
    dump = migrations.index("ops/backup/dump.sh")
    assert migrations.index("ops/backup/verify.sh") > dump
    assert migrations.index("alembic upgrade head") < dump
    restore = migrations.index("ops/backup/restore.sh")
    assert restore > dump
    assert migrations.rindex("alembic check") > restore  # still the models' schema after
    assert "RESTORE_CONFIRM: restore" in migrations
