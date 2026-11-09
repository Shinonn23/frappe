"""Offline regression checks for lossless decimal metadata sync."""

import unittest
from decimal import Decimal
from unittest.mock import Mock, patch

import frappe
from frappe.database.schema import DbColumn


class TestDecimalSchemaCapacity(unittest.TestCase):
	def setUp(self):
		self.db = Mock(
			db_type="mariadb",
			type_map={
				"Currency": ("decimal", "21,9"),
				"Float": ("decimal", "21,9"),
				"Rating": ("decimal", "3,2"),
				"Duration": ("decimal", "21,9"),
				"Data": ("varchar", "140"),
			},
			DEFAULT_SHORTCUTS=(),
		)
		self.db.escape.side_effect = lambda value: repr(value)
		self.patcher = patch.object(frappe, "db", self.db)
		self.patcher.start()
		self.addCleanup(self.patcher.stop)

	def column(self, precision="6", fieldtype="Currency"):
		table = Mock(doctype="Example")
		for name in (
			"add_column",
			"change_type",
			"add_unique",
			"drop_unique",
			"set_default",
			"change_nullability",
			"drop_index",
			"add_index",
		):
			setattr(table, name, [])
		return DbColumn(
			table=table,
			fieldname="value",
			fieldtype=fieldtype,
			precision=precision,
			length=None,
			default=None,
			set_index=False,
			options=None,
			unique=False,
			not_nullable=None,
		)

	def sync(self, current_type, precision="6", fieldtype="Currency"):
		column = self.column(precision, fieldtype)
		column.build_for_alter_table({"type": current_type, "default": None})
		return column

	def test_preserves_integer_and_fraction_capacity_and_is_stable_on_second_sync(self):
		for backend in ("mariadb", "postgres"):
			self.db.db_type = backend
			for old, precision, expected in (
				("decimal(21,4)", "6", "decimal(23,6)"),
				("decimal(21,9)", "6", "decimal(24,9)"),
				("decimal(21,9)", "2", "decimal(28,9)"),
				("decimal(23,6)", "6", "decimal(23,6)"),
			):
				with self.subTest(backend=backend, old=old, precision=precision):
					column = self.sync(old, precision)
					self.assertTrue(column.get_definition().startswith(expected))
					self.assertEqual(bool(column.table.change_type), old != expected)
					second = self.sync(expected, precision)
					self.assertEqual(second.table.change_type, [])

	def test_old_exact_values_fit_the_union_without_rounding(self):
		values = (Decimal("99999999999999999.9999"), Decimal("-0.123456789"))
		for old, value in zip(("decimal(21,4)", "decimal(21,9)"), values, strict=True):
			column = self.sync(old)
			self.assertEqual(value.quantize(Decimal(1).scaleb(-column.precision)), value)
			self.assertLess(abs(value), Decimal(10) ** (column.length - column.precision))

	def test_fixed_decimal_types_also_preserve_capacity_in_emitted_definition(self):
		column = self.sync("decimal(5,3)", None, "Rating")
		self.assertTrue(column.get_definition().startswith("decimal(5,3)"))
		self.assertEqual(column.table.change_type, [])

	def test_new_columns_and_changes_to_or_from_other_types_keep_native_behavior(self):
		column = self.column()
		column.build_for_alter_table(None)
		self.assertEqual(column.table.add_column, [column])
		self.assertTrue(column.get_definition().startswith("decimal(21,6)"))
		self.assertTrue(self.sync("varchar(140)").get_definition().startswith("decimal(21,6)"))
		column = self.sync("decimal(21,9)", fieldtype="Data")
		self.assertEqual(column.get_definition(), "varchar(140)")

	def test_default_index_and_nullability_comparison_still_runs(self):
		column = self.column()
		column.default = "1"
		column.set_index = True
		column.unique = True
		column.build_for_alter_table({"type": "decimal(23,6)", "default": "0"})
		self.assertEqual(column.table.change_type, [])
		self.assertEqual(column.table.set_default, [column])
		self.assertEqual(column.table.add_index, [column])
		self.assertEqual(column.table.add_unique, [column])
		self.assertIn("NOT NULL DEFAULT 1.0", column.get_definition(for_modification=True))

	def test_unsupported_union_fails_instead_of_clipping(self):
		with patch.object(frappe, "throw", side_effect=frappe.ValidationError) as throw:
			with self.assertRaises(frappe.ValidationError):
				self.sync("decimal(65,0)")
			self.assertIn("decimal(71,6)", throw.call_args.args[0])

	def test_actual_backend_alter_writers_emit_preserved_decimal_types_then_no_ddl(self):
		from frappe.database.mariadb.schema import MariaDBTable
		from frappe.database.postgres.schema import PostgresTable

		for backend, table_class, writer in (
			("mariadb", MariaDBTable, self.db.sql_ddl),
			("postgres", PostgresTable, self.db.sql),
		):
			self.db.db_type = backend
			for fieldtype, old, precision, expected in (
				("Currency", "decimal(21,4)", "6", "decimal(23,6)"),
				("Currency", "decimal(21,9)", "6", "decimal(24,9)"),
				("Float", "decimal(21,9)", "2", "decimal(28,9)"),
				("Rating", "decimal(5,1)", None, "decimal(6,2)"),
				("Duration", "decimal(21,4)", None, "decimal(26,9)"),
			):
				with self.subTest(backend=backend, fieldtype=fieldtype):
					for current in (old, expected):
						meta = frappe._dict(istable=1, sort_field="creation")
						meta.get_fieldnames_with_value = Mock(return_value=[])
						table = table_class("Example", meta=meta)
						column = self.column(precision, fieldtype)
						column.table = table
						table.columns = {"value": column}
						table.current_columns = {"value": {"type": current, "default": None}}
						writer.reset_mock()
						with patch.object(table, "alter_primary_key", return_value=None):
							table.alter()
						if current == expected:
							writer.assert_not_called()
						else:
							writer.assert_called_once()
							sql = writer.call_args.args[0]
							if backend == "postgres":
								self.assertEqual(
									sql, f"ALTER TABLE `tabExample` ALTER COLUMN `value` TYPE {expected} "
								)
							else:
								self.assertIn(f"MODIFY `value` {expected}", sql)
