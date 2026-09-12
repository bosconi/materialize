---
headless: true
---
- Ownership of the connection.
- In addition, to set, reset, or drop connection options:
  - `USAGE` privileges on all connections and secrets referenced by the
    resulting connection definition.
  - `USAGE` privileges on the schemas that contain those connections and
    secrets.
  - `USAGE` privileges on secrets used directly or transitively by dependent
    connections, and on the schemas containing those secrets. This applies even
    when the dependent connection has no active source or sink and validation is
    disabled.
- In addition, to change owners:
  - Role membership in `new_owner`.
  - `CREATE` privileges on the containing schema if the connection is namespaced
  by a schema.

Owning a shared SSH tunnel or AWS PrivateLink connection does not grant authority
to redirect credentials used by its dependent connections. A dependent connection
whose secrets the route owner cannot use prevents that owner from altering route
options. To resolve that, have a role that can use those secrets alter the route,
or remove the dependency first. Granting the route owner `USAGE` on the secrets
also works, but it hands them the access this restriction exists to withhold, so
prefer it only where that access is already intended.

`USAGE` on an unchanged connection still permits `VALIDATE CONNECTION` without
access to its secrets, and altering a connection never requires `USAGE` on
secrets held by the connections it references, only on those its own definition
names.
