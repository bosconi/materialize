# Copyright Materialize, Inc. and contributors. All rights reserved.
#
# Use of this software is governed by the Business Source License
# included in the LICENSE file at the root of this repository.
#
# As of the Change Date specified in that file, in accordance with
# the Business Source License, use of this software will be governed
# by the Apache License, Version 2.0.
from enum import Enum

from materialize.output_consistency.data_type.data_type_with_values import (
    DataTypeWithValues,
)
from materialize.output_consistency.execution.sql_dialect_adjuster import (
    MzSqlDialectAdjuster,
    SqlDialectAdjuster,
)
from materialize.output_consistency.execution.value_storage_layout import (
    ROW_INDEX_COL_NAME,
    ValueStorageLayout,
)
from materialize.output_consistency.input_data.test_input_types import (
    ConsistencyTestTypesInput,
)
from materialize.output_consistency.selection.selection import (
    ALL_ROWS_SELECTION,
    ALL_TABLE_COLUMNS_BY_NAME_SELECTION,
    DataRowSelection,
    TableColumnByNameSelection,
)

EVALUATION_STRATEGY_NAME_DFR = "dataflow_rendering"
EVALUATION_STRATEGY_NAME_CTF = "constant_folding"
INTERNAL_EVALUATION_STRATEGY_NAMES = [
    EVALUATION_STRATEGY_NAME_DFR,
    EVALUATION_STRATEGY_NAME_CTF,
]


class EvaluationStrategyKey(Enum):
    DUMMY = 1
    MZ_DATAFLOW_RENDERING = 2
    MZ_CONSTANT_FOLDING = 3
    POSTGRES = 4
    MZ_DATAFLOW_RENDERING_OTHER_DB = 5
    MZ_CONSTANT_FOLDING_OTHER_DB = 6


class EvaluationStrategy:
    """Strategy how to execute a `QueryTemplate`"""

    def __init__(
        self,
        identifier: EvaluationStrategyKey,
        name: str,
        object_name_base: str,
        simple_db_object_name: str,
        sql_adjuster: SqlDialectAdjuster = MzSqlDialectAdjuster(),
    ):
        """
        :param identifier: identifier of this strategy
        :param name: readable name
        :param object_name_base: the db object name will be derived from this
        :param simple_db_object_name: only used by the reproduction code printer
        """
        self.identifier = identifier
        self.name = name
        self.object_name_base = object_name_base
        self.simple_db_object_name = simple_db_object_name
        self.sql_adjuster = sql_adjuster
        self.additional_setup_info: str | None = None

    def generate_sources(
        self,
        types_input: ConsistencyTestTypesInput,
    ) -> list[str]:
        statements = []
        statements.extend(
            self.generate_source_for_storage_layout(
                types_input,
                ValueStorageLayout.HORIZONTAL,
                ALL_ROWS_SELECTION,
                ALL_TABLE_COLUMNS_BY_NAME_SELECTION,
            )
        )
        statements.extend(
            self.generate_source_for_storage_layout(
                types_input,
                ValueStorageLayout.VERTICAL,
                ALL_ROWS_SELECTION,
                ALL_TABLE_COLUMNS_BY_NAME_SELECTION,
            )
        )
        return statements

    def generate_source_for_storage_layout(
        self,
        types_input: ConsistencyTestTypesInput,
        storage_layout: ValueStorageLayout,
        row_selection: DataRowSelection,
        table_column_selection: TableColumnByNameSelection,
        override_db_object_name: str | None = None,
    ) -> list[str]:
        raise NotImplementedError

    def get_db_object_name(
        self,
        storage_layout: ValueStorageLayout,
        override_db_object_name: str | None = None,
    ) -> str:
        if storage_layout == ValueStorageLayout.ANY:
            raise RuntimeError(f"{storage_layout} has not been resolved to a real one")

        if override_db_object_name is not None:
            return override_db_object_name

        storage_suffix = (
            "horiz" if storage_layout == ValueStorageLayout.HORIZONTAL else "vert"
        )
        return f"{self.object_name_base}_{storage_suffix}"

    def __str__(self) -> str:
        return self.name

    def _create_column_specs(
        self,
        types_input: ConsistencyTestTypesInput,
        storage_layout: ValueStorageLayout,
        include_type: bool,
        table_column_selection: TableColumnByNameSelection,
    ) -> list[str]:
        column_specs = []

        # row index as first column (also for horizontal layout helpful to simplify aggregate functions with order spec)
        int_type_name = self.sql_adjuster.adjust_type("INT")
        type_info = f" {int_type_name}" if include_type else ""
        column_specs.append(f"{ROW_INDEX_COL_NAME}{type_info}")

        for type_with_values in types_input.all_data_types_with_values:
            type_name = self.sql_adjuster.adjust_type(
                type_with_values.data_type.type_name
            )
            type_info = f" {type_name}" if include_type else ""

            if storage_layout == ValueStorageLayout.HORIZONTAL:
                for data_value in type_with_values.raw_values:
                    if table_column_selection.is_included(data_value.column_name):
                        column_specs.append(f"{data_value.column_name}{type_info}")
            elif storage_layout == ValueStorageLayout.VERTICAL:
                column_name = (
                    type_with_values.create_vertical_storage_column().column_name
                )
                if table_column_selection.is_included(column_name):
                    column_specs.append(f"{column_name}{type_info}")
            else:
                raise RuntimeError(f"Unsupported storage layout: {storage_layout}")

        return column_specs

    def _adjust_type_name(self, type_name: str) -> str:
        return type_name

    def _create_value_rows(
        self,
        types_input: ConsistencyTestTypesInput,
        storage_layout: ValueStorageLayout,
        row_selection: DataRowSelection,
        table_column_selection: TableColumnByNameSelection,
    ) -> list[str]:
        if storage_layout == ValueStorageLayout.HORIZONTAL:
            return [
                self.__create_horizontal_value_row(
                    types_input.all_data_types_with_values, table_column_selection
                )
            ]
        elif storage_layout == ValueStorageLayout.VERTICAL:
            return self.__create_vertical_value_rows(
                types_input.all_data_types_with_values,
                types_input.max_value_count,
                row_selection,
                table_column_selection,
            )
        else:
            raise RuntimeError(f"Unsupported storage layout: {storage_layout}")

    def __create_horizontal_value_row(
        self,
        data_type_with_values: list[DataTypeWithValues],
        table_column_selection: TableColumnByNameSelection,
    ) -> str:
        row_values = []

        # row index
        row_values.append("0")

        for type_with_values in data_type_with_values:
            for data_value in type_with_values.raw_values:
                if table_column_selection.is_included(data_value.column_name):
                    row_values.append(data_value.to_sql_as_value(self.sql_adjuster))

        return f"{', '.join(row_values)}"

    def __create_vertical_value_rows(
        self,
        data_type_with_values: list[DataTypeWithValues],
        row_count: int,
        row_selection: DataRowSelection,
        table_column_selection: TableColumnByNameSelection,
    ) -> list[str]:
        """Creates table rows with the values of each type in a column. For types with fewer values, values are repeated."""
        rows = []

        for row_index in range(0, row_count):
            # the first column holds the row index
            row_values = [str(row_index)]

            for type_with_values in data_type_with_values:
                data_column = type_with_values.create_vertical_storage_column()
                column_name = data_column.column_name

                if not table_column_selection.is_included(column_name):
                    continue

                data_value = data_column.get_value_at_row(row_index)
                row_values.append(data_value.to_sql_as_value(self.sql_adjuster))

            if row_selection.is_included(row_index):
                rows.append(f"{', '.join(row_values)}")

        return rows


class DummyEvaluation(EvaluationStrategy):
    def __init__(self) -> None:
        super().__init__(EvaluationStrategyKey.DUMMY, "Dummy", "<source>", "dummy")

    def generate_sources(
        self,
        types_input: ConsistencyTestTypesInput,
    ) -> list[str]:
        return []


class DataFlowRenderingEvaluation(EvaluationStrategy):
    def __init__(self) -> None:
        super().__init__(
            EvaluationStrategyKey.MZ_DATAFLOW_RENDERING,
            "Dataflow rendering",
            "t_dfr",
            "dataflow_rendering",
        )

    def generate_source_for_storage_layout(
        self,
        types_input: ConsistencyTestTypesInput,
        storage_layout: ValueStorageLayout,
        row_selection: DataRowSelection,
        table_column_selection: TableColumnByNameSelection,
        override_db_object_name: str | None = None,
    ) -> list[str]:
        db_object_name = self.get_db_object_name(
            storage_layout, override_db_object_name
        )

        statements = []

        column_specs = self._create_column_specs(
            types_input, storage_layout, True, table_column_selection
        )
        statements.append(f"DROP TABLE IF EXISTS {db_object_name};")
        statements.append(f"CREATE TABLE {db_object_name} ({', '.join(column_specs)});")

        value_rows = self._create_value_rows(
            types_input, storage_layout, row_selection, table_column_selection
        )

        for value_row in value_rows:
            statements.append(f"INSERT INTO {db_object_name} VALUES ({value_row});")

        return statements


class ConstantFoldingEvaluation(EvaluationStrategy):
    def __init__(self) -> None:
        super().__init__(
            EvaluationStrategyKey.MZ_CONSTANT_FOLDING,
            "Constant folding",
            "v_ctf",
            "constant_folding",
        )

    def generate_source_for_storage_layout(
        self,
        types_input: ConsistencyTestTypesInput,
        storage_layout: ValueStorageLayout,
        row_selection: DataRowSelection,
        table_column_selection: TableColumnByNameSelection,
        override_db_object_name: str | None = None,
    ) -> list[str]:
        db_object_name = self.get_db_object_name(
            storage_layout, override_db_object_name
        )

        column_specs = self._create_column_specs(
            types_input, storage_layout, False, table_column_selection
        )

        value_rows = self._create_value_rows(
            types_input, storage_layout, row_selection, table_column_selection
        )
        value_specification = "\n    UNION SELECT ".join(value_rows)

        create_view_statement = (
            f"CREATE OR REPLACE VIEW {db_object_name} ({', '.join(column_specs)})\n"
            f" AS SELECT {value_specification};"
        )

        return [create_view_statement]


def create_internal_evaluation_strategy_twice(
    evaluation_strategy_name: str,
) -> list[EvaluationStrategy]:
    strategies: list[EvaluationStrategy]

    if evaluation_strategy_name == EVALUATION_STRATEGY_NAME_DFR:
        strategies = [DataFlowRenderingEvaluation(), DataFlowRenderingEvaluation()]
        strategies[1].identifier = EvaluationStrategyKey.MZ_DATAFLOW_RENDERING_OTHER_DB
        return strategies

    if evaluation_strategy_name == EVALUATION_STRATEGY_NAME_CTF:
        strategies = [ConstantFoldingEvaluation(), ConstantFoldingEvaluation()]
        strategies[1].identifier = EvaluationStrategyKey.MZ_CONSTANT_FOLDING_OTHER_DB
        return strategies

    raise RuntimeError(f"Unexpected strategy name: { evaluation_strategy_name}")


def is_other_db_evaluation_strategy(evaluation_key: EvaluationStrategyKey) -> bool:
    return evaluation_key in {
        EvaluationStrategyKey.MZ_DATAFLOW_RENDERING_OTHER_DB,
        EvaluationStrategyKey.MZ_CONSTANT_FOLDING_OTHER_DB,
    }
