// Copyright Materialize, Inc. and contributors. All rights reserved.
//
// Use of this software is governed by the Business Source License
// included in the LICENSE file.
//
// As of the Change Date specified in that file, in accordance with
// the Business Source License, use of this software will be governed
// by the Apache License, Version 2.0.

//! Provides a publicly available interface to transform our SQL ASTs.

use std::collections::{BTreeMap, BTreeSet};

use mz_ore::str::StrExt;
use mz_repr::GlobalId;
use mz_sql_parser::ast::CreateTableFromSourceStatement;

use crate::ast::visit::{self, Visit};
use crate::ast::visit_mut::{self, VisitMut};
use crate::ast::{
    AstInfo, CreateConnectionStatement, CreateIndexStatement, CreateMaterializedViewStatement,
    CreateSecretStatement, CreateSinkStatement, CreateSourceStatement, CreateSubsourceStatement,
    CreateTableStatement, CreateViewStatement, CreateWebhookSourceStatement, Expr, Ident, Query,
    Raw, RawItemName, Statement, UnresolvedItemName, ViewDefinition,
};
use crate::names::FullItemName;

/// Given a [`Statement`] rewrites all references of the schema name `cur_schema_name` to
/// `new_schema_name`.
pub fn create_stmt_rename_schema_refs(
    create_stmt: &mut Statement<Raw>,
    database: &str,
    cur_schema: &str,
    new_schema: &str,
) -> Result<(), (String, String)> {
    match create_stmt {
        stmt @ Statement::CreateConnection(_)
        | stmt @ Statement::CreateDatabase(_)
        | stmt @ Statement::CreateSchema(_)
        | stmt @ Statement::CreateWebhookSource(_)
        | stmt @ Statement::CreateSource(_)
        | stmt @ Statement::CreateSubsource(_)
        | stmt @ Statement::CreateSink(_)
        | stmt @ Statement::CreateView(_)
        | stmt @ Statement::CreateMaterializedView(_)
        | stmt @ Statement::CreateTable(_)
        | stmt @ Statement::CreateTableFromSource(_)
        | stmt @ Statement::CreateIndex(_)
        | stmt @ Statement::CreateType(_)
        | stmt @ Statement::CreateSecret(_) => {
            let mut visitor = CreateSqlRewriteSchema {
                database,
                cur_schema,
                new_schema,
                error: None,
            };
            visitor.visit_statement_mut(stmt);

            if let Some(e) = visitor.error.take() {
                Err(e)
            } else {
                Ok(())
            }
        }
        stmt => {
            unreachable!("Internal error: only catalog items need to update item refs. {stmt:?}")
        }
    }
}

struct CreateSqlRewriteSchema<'a> {
    database: &'a str,
    cur_schema: &'a str,
    new_schema: &'a str,
    error: Option<(String, String)>,
}

impl<'a> CreateSqlRewriteSchema<'a> {
    fn maybe_rewrite_idents(&mut self, name: &mut [Ident]) {
        match name {
            [schema, item] if schema.as_str() == self.cur_schema => {
                // TODO(parkmycar): I _think_ when the database component is not specified we can
                // always infer we're using the current database. But I'm not positive, so for now
                // we'll bail in this case.
                if self.error.is_none() {
                    self.error = Some((schema.to_string(), item.to_string()));
                }
            }
            [database, schema, _item] => {
                if database.as_str() == self.database && schema.as_str() == self.cur_schema {
                    *schema = Ident::new_unchecked(self.new_schema);
                }
            }
            _ => (),
        }
    }
}

impl<'a, 'ast> VisitMut<'ast, Raw> for CreateSqlRewriteSchema<'a> {
    fn visit_expr_mut(&mut self, e: &'ast mut Expr<Raw>) {
        match e {
            Expr::Identifier(id) => {
                // The last ID component is a column name that should not be
                // considered in the rewrite.
                let i = id.len() - 1;
                self.maybe_rewrite_idents(&mut id[..i]);
            }
            Expr::QualifiedWildcard(id) => {
                self.maybe_rewrite_idents(id);
            }
            _ => visit_mut::visit_expr_mut(self, e),
        }
    }

    fn visit_unresolved_item_name_mut(
        &mut self,
        unresolved_item_name: &'ast mut UnresolvedItemName,
    ) {
        self.maybe_rewrite_idents(&mut unresolved_item_name.0);
    }

    fn visit_item_name_mut(
        &mut self,
        item_name: &'ast mut <mz_sql_parser::ast::Raw as AstInfo>::ItemName,
    ) {
        match item_name {
            RawItemName::Name(n) | RawItemName::Id(_, n, _) => self.maybe_rewrite_idents(&mut n.0),
        }
    }
}

/// Changes the `name` used in an item's `CREATE` statement. To complete a
/// rename operation, you must also call `create_stmt_rename_refs` on all dependent
/// items.
pub fn create_stmt_rename(create_stmt: &mut Statement<Raw>, to_item_name: String) {
    // TODO(sploiselle): Support renaming schemas and databases.
    match create_stmt {
        Statement::CreateIndex(CreateIndexStatement { name, .. }) => {
            *name = Some(Ident::new_unchecked(to_item_name));
        }
        Statement::CreateSink(CreateSinkStatement {
            name: Some(name), ..
        })
        | Statement::CreateSource(CreateSourceStatement { name, .. })
        | Statement::CreateSubsource(CreateSubsourceStatement { name, .. })
        | Statement::CreateView(CreateViewStatement {
            definition: ViewDefinition { name, .. },
            ..
        })
        | Statement::CreateMaterializedView(CreateMaterializedViewStatement { name, .. })
        | Statement::CreateTable(CreateTableStatement { name, .. })
        | Statement::CreateTableFromSource(CreateTableFromSourceStatement { name, .. })
        | Statement::CreateSecret(CreateSecretStatement { name, .. })
        | Statement::CreateConnection(CreateConnectionStatement { name, .. })
        | Statement::CreateWebhookSource(CreateWebhookSourceStatement { name, .. }) => {
            // The last name in an ItemName is the item name. The item name
            // does not have a fixed index.
            // TODO: https://github.com/MaterializeInc/database-issues/issues/1721
            let item_name_len = name.0.len() - 1;
            name.0[item_name_len] = Ident::new_unchecked(to_item_name);
        }
        item => unreachable!("Internal error: only catalog items can be renamed {item:?}"),
    }
}

/// Updates all references of `from_name` in `create_stmt` to `to_name` or
/// errors if request is ambiguous.
///
/// Requests are considered ambiguous if `create_stmt` is a
/// `Statement::CreateView`, and any of the following apply to its `query`:
/// - `to_name.item` is used as an [`Ident`] in `query`.
/// - `from_name.item` does not unambiguously refer to an item in the query,
///   e.g. it is also used as a schema, or not all references to the item are
///   sufficiently qualified.
/// - `to_name.item` does not unambiguously refer to an item in the query after
///   the rename. Right now, given the first condition, this is just a coherence
///   check, but will be more meaningful once the first restriction is lifted.
pub fn create_stmt_rename_refs(
    create_stmt: &mut Statement<Raw>,
    from_name: FullItemName,
    to_item_name: String,
) -> Result<(), String> {
    let from_item = UnresolvedItemName::from(from_name.clone());
    let maybe_update_item_name = |item_name: &mut UnresolvedItemName| {
        if item_name.0 == from_item.0 {
            // The last name in an ItemName is the item name. The item name
            // does not have a fixed index.
            // TODO: https://github.com/MaterializeInc/database-issues/issues/1721
            let item_name_len = item_name.0.len() - 1;
            item_name.0[item_name_len] = Ident::new_unchecked(to_item_name.clone());
        }
    };

    // TODO(sploiselle): Support renaming schemas and databases.
    match create_stmt {
        Statement::CreateIndex(CreateIndexStatement { on_name, .. }) => {
            maybe_update_item_name(on_name.name_mut());
        }
        Statement::CreateSink(CreateSinkStatement { from, .. }) => {
            maybe_update_item_name(from.name_mut());
        }
        Statement::CreateView(CreateViewStatement {
            definition: ViewDefinition { query, .. },
            ..
        })
        | Statement::CreateMaterializedView(CreateMaterializedViewStatement { query, .. }) => {
            rewrite_query(from_name, to_item_name, query)?;
        }
        Statement::CreateSource(_)
        | Statement::CreateSubsource(_)
        | Statement::CreateTable(_)
        | Statement::CreateTableFromSource(_)
        | Statement::CreateSecret(_)
        | Statement::CreateConnection(_)
        | Statement::CreateWebhookSource(_) => {}
        item => {
            unreachable!("Internal error: only catalog items need to update item refs {item:?}")
        }
    }

    Ok(())
}

/// Rewrites `query`'s references of `from` to `to` or errors if too ambiguous.
fn rewrite_query(from: FullItemName, to: String, query: &mut Query<Raw>) -> Result<(), String> {
    let from_ident = Ident::new_unchecked(from.item.clone());
    let to_ident = Ident::new_unchecked(to);
    let qual_depth =
        QueryIdentAgg::determine_qual_depth(&from_ident, Some(to_ident.clone()), query)?;
    CreateSqlRewriter::rewrite_query_with_qual_depth(from, to_ident.clone(), qual_depth, query);
    // Ensure that our rewrite didn't didn't introduce ambiguous
    // references to `to_name`.
    match QueryIdentAgg::determine_qual_depth(&to_ident, None, query) {
        Ok(_) => Ok(()),
        Err(e) => Err(e),
    }
}

fn ambiguous_err(n: &Ident, t: &str) -> String {
    format!(
        "{} potentially used ambiguously as item and {}",
        n.as_str().quoted(),
        t
    )
}

/// Visits a [`Query`], assessing catalog item [`Ident`]s' use of a specified `Ident`.
struct QueryIdentAgg<'a> {
    /// The name whose usage you want to assess.
    name: &'a Ident,
    /// Tracks all second-level qualifiers used on `name` in a `BTreeMap`, as
    /// well as any third-level qualifiers used on those second-level qualifiers
    /// in a `BTreeSet`.
    qualifiers: BTreeMap<Ident, BTreeSet<Ident>>,
    /// Tracks the least qualified instance of `name` seen.
    min_qual_depth: usize,
    /// Provides an option to fail the visit if encounters a specified `Ident`.
    fail_on: Option<Ident>,
    err: Option<String>,
}

impl<'a> QueryIdentAgg<'a> {
    /// Determines the depth of qualification needed to unambiguously reference
    /// catalog items in a [`Query`].
    ///
    /// Includes an option to fail if a given `Ident` is encountered.
    ///
    /// `Result`s of `Ok(usize)` indicate that `name` can be unambiguously
    /// referred to with `usize` parts, e.g. 2 requires schema and item name
    /// qualification.
    ///
    /// `Result`s of `Err` indicate that we cannot unambiguously reference
    /// `name` or encountered `fail_on`, if it's provided.
    fn determine_qual_depth(
        name: &Ident,
        fail_on: Option<Ident>,
        query: &Query<Raw>,
    ) -> Result<usize, String> {
        let mut v = QueryIdentAgg {
            qualifiers: BTreeMap::new(),
            min_qual_depth: usize::MAX,
            err: None,
            name,
            fail_on,
        };

        // Aggregate identities in `v`.
        v.visit_query(query);
        // Not possible to have a qualification depth of 0;
        assert!(v.min_qual_depth > 0);

        if let Some(e) = v.err {
            return Err(e);
        }

        // Check if there was more than one 3rd-level (e.g.
        // database) qualification used for any reference to `name`.
        let req_depth = if v.qualifiers.values().any(|v| v.len() > 1) {
            3
        // Check if there was more than one 2nd-level (e.g. schema)
        // qualification used for any reference to `name`.
        } else if v.qualifiers.len() > 1 {
            2
        } else {
            1
        };

        if v.min_qual_depth < req_depth {
            Err(format!(
                "{} is not sufficiently qualified to support renaming",
                name.as_str().quoted()
            ))
        } else {
            Ok(req_depth)
        }
    }

    // Assesses `v` for uses of `self.name` and `self.fail_on`.
    fn check_failure(&mut self, v: &[Ident]) {
        // Fail if we encounter `self.fail_on`.
        if let Some(f) = &self.fail_on {
            if v.iter().any(|i| i == f) {
                self.err = Some(format!(
                    "found reference to {}; cannot rename {} to any identity \
                    used in any existing view definitions",
                    f.as_str().quoted(),
                    self.name.as_str().quoted()
                ));
            }
        }
    }
}

impl<'a, 'ast> Visit<'ast, Raw> for QueryIdentAgg<'a> {
    fn visit_expr(&mut self, e: &'ast Expr<Raw>) {
        match e {
            Expr::Identifier(i) => {
                self.check_failure(i);
                if let Some(p) = i.iter().rposition(|e| e == self.name) {
                    if p == i.len() - 1 {
                        // `self.name` used as a column if it's in the final
                        // position here, e.g. `SELECT view.col FROM ...`
                        self.err = Some(ambiguous_err(self.name, "column"));
                        return;
                    }
                    self.min_qual_depth = std::cmp::min(p + 1, self.min_qual_depth);
                }
            }
            Expr::QualifiedWildcard(i) => {
                self.check_failure(i);
                if let Some(p) = i.iter().rposition(|e| e == self.name) {
                    self.min_qual_depth = std::cmp::min(p + 1, self.min_qual_depth);
                }
            }
            _ => visit::visit_expr(self, e),
        }
    }

    fn visit_ident(&mut self, ident: &'ast Ident) {
        self.check_failure(&[ident.clone()]);
        // This is an unqualified item using `self.name`, e.g. an alias, which
        // we cannot unambiguously resolve.
        if ident == self.name {
            self.err = Some(ambiguous_err(self.name, "alias or column"));
        }
    }

    fn visit_unresolved_item_name(&mut self, unresolved_item_name: &'ast UnresolvedItemName) {
        let names = &unresolved_item_name.0;
        self.check_failure(names);
        // Every item is used as an `ItemName` at least once, which
        // lets use track all items named `self.name`.
        if let Some(p) = names.iter().rposition(|e| e == self.name) {
            // Name used as last element of `<db>.<schema>.<item>`
            if p == names.len() - 1 && names.len() == 3 {
                self.qualifiers
                    .entry(names[1].clone())
                    .or_default()
                    .insert(names[0].clone());
                self.min_qual_depth = std::cmp::min(3, self.min_qual_depth);
            } else {
                // Any other use is a database or schema
                self.err = Some(ambiguous_err(self.name, "database, schema, or function"))
            }
        }
    }

    fn visit_item_name(&mut self, item_name: &'ast <Raw as AstInfo>::ItemName) {
        match item_name {
            RawItemName::Name(n) | RawItemName::Id(_, n, _) => self.visit_unresolved_item_name(n),
        }
    }
}

struct CreateSqlRewriter {
    from: Vec<Ident>,
    to: Ident,
}

impl CreateSqlRewriter {
    fn rewrite_query_with_qual_depth(
        from_name: FullItemName,
        to_name: Ident,
        qual_depth: usize,
        query: &mut Query<Raw>,
    ) {
        let from = match qual_depth {
            1 => vec![Ident::new_unchecked(from_name.item)],
            2 => vec![
                Ident::new_unchecked(from_name.schema),
                Ident::new_unchecked(from_name.item),
            ],
            3 => vec![
                Ident::new_unchecked(from_name.database.to_string()),
                Ident::new_unchecked(from_name.schema),
                Ident::new_unchecked(from_name.item),
            ],
            _ => unreachable!(),
        };
        let mut v = CreateSqlRewriter { from, to: to_name };
        v.visit_query_mut(query);
    }

    fn maybe_rewrite_idents(&self, name: &mut [Ident]) {
        if name.len() > 0 && name.ends_with(&self.from) {
            name[name.len() - 1] = self.to.clone();
        }
    }
}

impl<'ast> VisitMut<'ast, Raw> for CreateSqlRewriter {
    fn visit_expr_mut(&mut self, e: &'ast mut Expr<Raw>) {
        match e {
            Expr::Identifier(id) => {
                // The last ID component is a column name that should not be
                // considered in the rewrite.
                let i = id.len() - 1;
                self.maybe_rewrite_idents(&mut id[..i]);
            }
            Expr::QualifiedWildcard(id) => {
                self.maybe_rewrite_idents(id);
            }
            _ => visit_mut::visit_expr_mut(self, e),
        }
    }
    fn visit_unresolved_item_name_mut(
        &mut self,
        unresolved_item_name: &'ast mut UnresolvedItemName,
    ) {
        self.maybe_rewrite_idents(&mut unresolved_item_name.0);
    }
    fn visit_item_name_mut(
        &mut self,
        item_name: &'ast mut <mz_sql_parser::ast::Raw as AstInfo>::ItemName,
    ) {
        match item_name {
            RawItemName::Name(n) | RawItemName::Id(_, n, _) => self.maybe_rewrite_idents(&mut n.0),
        }
    }
}

/// Updates all `GlobalId`s from the keys of `ids` to the values of `ids` within `create_stmt`.
pub fn create_stmt_replace_ids(
    create_stmt: &mut Statement<Raw>,
    ids: &BTreeMap<GlobalId, GlobalId>,
) {
    let mut id_replacer = CreateSqlIdReplacer { ids };
    id_replacer.visit_statement_mut(create_stmt);
}

struct CreateSqlIdReplacer<'a> {
    ids: &'a BTreeMap<GlobalId, GlobalId>,
}

impl<'ast> VisitMut<'ast, Raw> for CreateSqlIdReplacer<'_> {
    fn visit_item_name_mut(
        &mut self,
        item_name: &'ast mut <mz_sql_parser::ast::Raw as AstInfo>::ItemName,
    ) {
        match item_name {
            RawItemName::Id(id, _, _) => {
                let old_id = match id.parse() {
                    Ok(old_id) => old_id,
                    Err(_) => panic!("invalid persisted global id {id}"),
                };
                if let Some(new_id) = self.ids.get(&old_id) {
                    *id = new_id.to_string();
                }
            }
            RawItemName::Name(_) => {}
        }
    }
}
