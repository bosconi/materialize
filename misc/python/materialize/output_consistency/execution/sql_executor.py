# Copyright Materialize, Inc. and contributors. All rights reserved.
#
# Use of this software is governed by the Business Source License
# included in the LICENSE file at the root of this repository.
#
# As of the Change Date specified in that file, in accordance with
# the Business Source License, use of this software will be governed
# by the Apache License, Version 2.0.
from collections import deque
from collections.abc import Sequence
from typing import Any

import dateutil  # type: ignore
from pg8000 import Connection
from pg8000.dbapi import ProgrammingError
from pg8000.exceptions import DatabaseError, InterfaceError

from materialize.output_consistency.output.output_printer import OutputPrinter


class SqlExecutionError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        # storing it here as well makes it easier to access the message
        self.message = message


class SqlExecutor:
    """Base class of `PgWireDatabaseSqlExecutor` and `DryRunSqlExecutor`"""

    def __init__(
        self,
        name: str,
    ):
        self.name = name

    def __str__(self) -> str:
        return self.__class__.__name__

    def ddl(self, sql: str) -> None:
        raise NotImplementedError

    def begin_tx(self, isolation_level: str) -> None:
        raise NotImplementedError

    def commit(self) -> None:
        raise NotImplementedError

    def rollback(self) -> None:
        raise NotImplementedError

    def query(self, sql: str) -> Sequence[Sequence[Any]]:
        raise NotImplementedError

    def query_version(self) -> str:
        raise NotImplementedError

    def before_query_execution(self) -> None:
        pass

    def after_query_execution(self) -> None:
        pass

    def before_new_tx(self):
        pass

    def after_new_tx(self):
        pass


class PgWireDatabaseSqlExecutor(SqlExecutor):
    def __init__(
        self,
        connection: Connection,
        use_autocommit: bool,
        output_printer: OutputPrinter,
        name: str,
    ):
        super().__init__(name)
        connection.autocommit = use_autocommit
        self.cursor = connection.cursor()
        self.output_printer = output_printer
        self.last_statements = deque[str](maxlen=5)

    def ddl(self, sql: str) -> None:
        self._execute_with_cursor(sql)

    def begin_tx(self, isolation_level: str) -> None:
        self._execute_with_cursor(f"BEGIN ISOLATION LEVEL {isolation_level};")

    def commit(self) -> None:
        self._execute_with_cursor("COMMIT;")

    def rollback(self) -> None:
        self._execute_with_cursor("ROLLBACK;")

    def query(self, sql: str) -> Sequence[Sequence[Any]]:
        try:
            self._execute_with_cursor(sql)
            return self.cursor.fetchall()
        except (ProgrammingError, DatabaseError) as err:
            raise SqlExecutionError(self._extract_message_from_error(err))

    def query_version(self) -> str:
        return self.query("SELECT version();")[0][0]

    def _execute_with_cursor(self, sql: str) -> None:
        try:
            self.last_statements.append(sql)
            self.cursor.execute(sql)
        except (ProgrammingError, DatabaseError) as err:
            raise SqlExecutionError(self._extract_message_from_error(err))
        except dateutil.parser._parser.ParserError as err:  # type: ignore
            raise SqlExecutionError(err.args[0])
        except ValueError as err:
            self.output_printer.print_error(f"Query with value error is: {sql}")
            raise err
        except InterfaceError:
            print("A network error occurred! Aborting!")
            # The current or one of previous queries might have broken the database.
            last_statements_desc = self.last_statements.copy()
            last_statements_desc.reverse()
            statements_str = "\n".join(
                f"  {statement}" for statement in last_statements_desc
            )
            print(
                f"Last {len(last_statements_desc)} queries in descending order:\n{statements_str}"
            )
            exit(1)
        except Exception:
            self.output_printer.print_error(f"Query with unexpected error is: {sql}")
            raise

    def _extract_message_from_error(
        self, error: ProgrammingError | DatabaseError
    ) -> str:
        error_args = error.args[0]

        message = error_args.get("M") if "M" in error_args else str(error_args)
        details = error_args.get("H") if "H" in error_args else None

        if details is None:
            return f"{message}"
        else:
            return f"{message} ({details})"


class MzDatabaseSqlExecutor(PgWireDatabaseSqlExecutor):
    def __init__(
        self,
        default_connection: Connection,
        mz_system_connection: Connection,
        use_autocommit: bool,
        output_printer: OutputPrinter,
        name: str,
    ):
        super().__init__(default_connection, use_autocommit, output_printer, name)
        self.mz_system_connection = mz_system_connection

    def query_version(self) -> str:
        return self.query("SELECT mz_version();")[0][0]


class DryRunSqlExecutor(SqlExecutor):
    def __init__(self, output_printer: OutputPrinter, name: str):
        super().__init__(name)
        self.output_printer = output_printer

    def consume_sql(self, sql: str) -> None:
        self.output_printer.print_sql(sql)

    def ddl(self, sql: str) -> None:
        self.consume_sql(sql)

    def begin_tx(self, isolation_level: str) -> None:
        self.consume_sql(f"BEGIN ISOLATION LEVEL {isolation_level};")

    def commit(self) -> None:
        self.consume_sql("COMMIT;")

    def rollback(self) -> None:
        self.consume_sql("ROLLBACK;")

    def query(self, sql: str) -> Sequence[Sequence[Any]]:
        self.consume_sql(sql)
        return []

    def query_version(self) -> str:
        return "(dry-run)"
