# Copyright Materialize, Inc. and contributors. All rights reserved.
#
# Use of this software is governed by the Business Source License
# included in the LICENSE file at the root of this repository.
#
# As of the Change Date specified in that file, in accordance with
# the Business Source License, use of this software will be governed
# by the Apache License, Version 2.0.

import os

from materialize.mz_env_util import get_cloud_hostname
from materialize.mzcompose.composition import Composition
from materialize.test_analytics.config.mz_db_config import MzDbConfig


def create_test_analytics_config(c: Composition) -> MzDbConfig:
    username = os.getenv(
        "CANARY_LOADTEST_USERNAME", "infra+qacanaryload@materialize.io"
    )
    app_password = os.environ["CANARY_LOADTEST_APP_PASSWORD"]
    hostname = get_cloud_hostname(c, app_password=app_password)
    database = "test_analytics"
    search_path = "public"

    return MzDbConfig(
        hostname=hostname,
        username=username,
        app_password=app_password,
        database=database,
        search_path=search_path,
    )


def create_local_dev_config() -> MzDbConfig:
    hostname = os.environ["MATERIALIZE_PROD_SANDBOX_HOSTNAME"]
    username = os.environ["MATERIALIZE_PROD_SANDBOX_USERNAME"]
    app_password = os.environ["MATERIALIZE_PROD_SANDBOX_APP_PASSWORD"]

    database = "test_analytics"
    search_path = "public"

    return MzDbConfig(
        hostname=hostname,
        username=username,
        app_password=app_password,
        database=database,
        search_path=search_path,
    )
