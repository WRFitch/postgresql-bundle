# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

import asyncio
import logging
from pathlib import Path

import pytest
import yaml
from pytest_operator.plugin import OpsTest
from tenacity import RetryError, Retrying, stop_after_delay, wait_fixed

from tests.integration.helpers.helpers import (
    deploy_postgres_bundle,
    get_app_relation_databag,
    get_backend_user_pass,
    get_cfg,
    wait_for_relation_joined_between,
    wait_for_relation_removed_between,
)
from tests.integration.helpers.postgresql_helpers import (
    check_database_users_existence,
    enable_connections_logging,
    get_postgres_primary,
    run_command_on_unit,
)

logger = logging.getLogger(__name__)

METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())
MAILMAN3 = "mailman3-core"
PGB = METADATA["name"]
PG = "postgresql"
TLS = "tls-certificates-operator"
RELATION = "backend-database"


@pytest.mark.backend
async def test_relate_pgbouncer_to_postgres(ops_test: OpsTest):
    """Test that the pgbouncer and postgres charms can relate to one another."""
    # Build, deploy, and relate charms.
    relation = await deploy_postgres_bundle(ops_test)

    cfg = await get_cfg(ops_test, f"{PGB}/0")
    logger.info(cfg.render())
    pgb_user, pgb_password = await get_backend_user_pass(ops_test, relation)
    assert pgb_user in cfg["pgbouncer"]["admin_users"]
    assert cfg["pgbouncer"]["auth_query"]
    await check_database_users_existence(ops_test, [pgb_user], [], pgb_user, pgb_password)


@pytest.mark.backend
async def test_tls_encrypted_connection_to_postgres(ops_test: OpsTest):
    async with ops_test.fast_forward():
        # Relate PgBouncer to PostgreSQL.
        relation = await ops_test.model.add_relation(f"{PGB}:{RELATION}", f"{PG}:database")
        wait_for_relation_joined_between(ops_test, PG, PGB)
        await ops_test.model.wait_for_idle(apps=[PGB, PG], status="active", timeout=1000)
        pgb_user, _ = await get_backend_user_pass(ops_test, relation)

        # Deploy TLS Certificates operator.
        config = {"generate-self-signed-certificates": "true", "ca-common-name": "Test CA"}
        await ops_test.model.deploy(TLS, channel="edge", config=config)
        await ops_test.model.wait_for_idle(apps=[TLS], status="active", timeout=1000)

        # Relate it to the PostgreSQL to enable TLS.
        await ops_test.model.relate(PG, TLS)
        await ops_test.model.wait_for_idle(status="active", timeout=1000)

        # Enable additional logs on the PostgreSQL instance to check TLS
        # being used in a later step.
        enable_connections_logging(ops_test, f"{PG}/0")

        # Deploy an app and relate it to PgBouncer to open a connection
        # between PgBouncer and PostgreSQL.
        await ops_test.model.deploy(MAILMAN3)
        await ops_test.model.add_relation(f"{PGB}:db", f"{MAILMAN3}:db")
        await ops_test.model.wait_for_idle(apps=[PG, PGB, MAILMAN3], status="active", timeout=1000)

        # Check the logs to ensure TLS is being used by PgBouncer.
        postgresql_primary_unit = await get_postgres_primary(ops_test)
        logs = await run_command_on_unit(
            ops_test, postgresql_primary_unit, "journalctl -u patroni.service"
        )
        assert (
            f"connection authorized: user={pgb_user} database=mailman3 SSL enabled"
            " (protocol=TLSv1.3, cipher=TLS_AES_256_GCM_SHA384, bits=256, compression=off)" in logs
        ), "TLS is not being used on connections to PostgreSQL"
